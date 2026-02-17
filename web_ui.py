import os
import socket
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from types import SimpleNamespace
from typing import Dict, Optional, Tuple, List, Any
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request, Response
from werkzeug.serving import make_server

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import db
import config as cfg
from fetch_tally import build_config as build_fetch_config, run_once as fetch_once
from fetch_invoices import build_config as build_fetch_invoice_config, run_once as fetch_invoice_once
from sync_catalytics import build_config as build_sync_config, run_once as sync_once
from fetch_customers import build_config as build_fetch_customers_config, run_once as fetch_customers_once
from sync_customers import build_config as build_sync_customers_config, run_once as sync_customers_once
from fetch_products import build_config as build_fetch_products_config, run_once as fetch_products_once
from sync_products import build_config as build_sync_products_config, run_once as sync_products_once
from logging_utils import setup_logging
import sync_catalytics as sync_dc_mod
import sync_customers as sync_cust_mod
import sync_products as sync_prod_mod


DEFAULT_ENV_PATH = cfg.resolve_env_path(os.path.dirname(__file__))
DEFAULT_LOG_FILE = os.path.join(os.path.dirname(__file__), "logs", "app.log")

app = Flask(__name__)


def _normalize_source_doc(value: Optional[str]) -> str:
    token = (value or "").strip().lower()
    if token in {"sales_invoice", "sales-invoice", "invoice", "sales"}:
        return "sales_invoice"
    return "delivery_note"


class RunnerState:
    MAX_ACTIVITY = 50

    def __init__(self):
        self.lock = threading.Lock()
        self.busy_lock = threading.Lock()
        self.loop_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.running = False
        self.busy = False
        self.status = "idle"
        self.current_step: Optional[str] = None
        self.step_index: int = 0
        self.step_total: int = 0
        self.last_fetch_at: Optional[str] = None
        self.last_sync_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.errors: List[str] = []
        self.fetch_stats: Dict[str, int] = {}
        self.sync_stats: Dict[str, int] = {}
        self.last_customer_fetch_at: Optional[str] = None
        self.last_customer_sync_at: Optional[str] = None
        self.customer_fetch_stats: Dict[str, int] = {}
        self.customer_sync_stats: Dict[str, int] = {}
        self.last_product_fetch_at: Optional[str] = None
        self.last_product_sync_at: Optional[str] = None
        self.product_fetch_stats: Dict[str, int] = {}
        self.product_sync_stats: Dict[str, int] = {}
        self.interval_sec = 300
        self.customer_interval_sec: Optional[int] = None
        self.product_interval_sec: Optional[int] = None
        self.source_doc = _normalize_source_doc(cfg.get_env("TALLY_SOURCE_DOC", "delivery_note"))
        self.activity: List[Dict[str, Any]] = []

    def add_activity(self, action: str, result: str, detail: Optional[str] = None):
        entry = {"time": _now_iso(), "action": action, "result": result}
        if detail:
            entry["detail"] = detail
        self.activity.insert(0, entry)
        if len(self.activity) > self.MAX_ACTIVITY:
            self.activity = self.activity[: self.MAX_ACTIVITY]

    def set_step(self, label: str, index: int, total: int):
        self.current_step = label
        self.step_index = index
        self.step_total = total

    def clear_step(self):
        self.current_step = None
        self.step_index = 0
        self.step_total = 0

    def to_dict(self):
        return {
            "running": self.running,
            "busy": self.busy,
            "status": self.status,
            "current_step": self.current_step,
            "step_index": self.step_index,
            "step_total": self.step_total,
            "last_fetch_at": self.last_fetch_at,
            "last_sync_at": self.last_sync_at,
            "last_error": self.last_error,
            "errors": self.errors[:5],
            "fetch_stats": self.fetch_stats,
            "sync_stats": self.sync_stats,
            "last_customer_fetch_at": self.last_customer_fetch_at,
            "last_customer_sync_at": self.last_customer_sync_at,
            "customer_fetch_stats": self.customer_fetch_stats,
            "customer_sync_stats": self.customer_sync_stats,
            "last_product_fetch_at": self.last_product_fetch_at,
            "last_product_sync_at": self.last_product_sync_at,
            "product_fetch_stats": self.product_fetch_stats,
            "product_sync_stats": self.product_sync_stats,
            "interval_sec": self.interval_sec,
            "customer_interval_sec": self.customer_interval_sec,
            "product_interval_sec": self.product_interval_sec,
            "source_doc": self.source_doc,
            "activity": self.activity[:20],
        }


STATE = RunnerState()


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_interval(name: str, fallback: int) -> int:
    value = cfg.get_env_int(name)
    if value is None:
        return fallback
    if value < 10:
        return fallback
    return value


def _env_namespace() -> SimpleNamespace:
    return SimpleNamespace(
        config=None,
        db_path=None,
        tally_url=None,
        company=None,
        entity_id=None,
        from_date=None,
        to_date=None,
        days_back=None,
        fetch_stock=False,
        fetch_full=False,
        dry_run=False,
        log_level=None,
        log_json=False,
        log_file=None,
        api_base_url=None,
        api_key=None,
        batch_size=None,
        limit=None,
        max_attempts=None,
        allow_tally_fetch=False,
    )


def _load_env():
    cfg.load_env_file(DEFAULT_ENV_PATH)


def _log_file_path() -> str:
    return cfg.get_env("LOG_FILE", DEFAULT_LOG_FILE)


def _run_fetch():
    args = _env_namespace()
    with STATE.lock:
        source_doc = STATE.source_doc

    if source_doc == "sales_invoice":
        fetch_config = build_fetch_invoice_config(args)
        if not fetch_config.log_file:
            fetch_config.log_file = _log_file_path()
        stats = fetch_invoice_once(fetch_config)
    else:
        fetch_config = build_fetch_config(args)
        if not fetch_config.log_file:
            fetch_config.log_file = _log_file_path()
        stats = fetch_once(fetch_config)
    return stats


def _run_sync():
    args = _env_namespace()
    sync_config = build_sync_config(args)
    if not sync_config.log_file:
        sync_config.log_file = _log_file_path()
    stats = sync_once(sync_config)
    return stats


def _run_fetch_customers():
    args = _env_namespace()
    fetch_config = build_fetch_customers_config(args)
    if not fetch_config.log_file:
        fetch_config.log_file = _log_file_path()
    stats = fetch_customers_once(fetch_config)
    return stats


def _run_sync_customers():
    args = _env_namespace()
    sync_config = build_sync_customers_config(args)
    if not sync_config.log_file:
        sync_config.log_file = _log_file_path()
    stats = sync_customers_once(sync_config)
    return stats


def _run_fetch_products():
    args = _env_namespace()
    fetch_config = build_fetch_products_config(args)
    if not fetch_config.log_file:
        fetch_config.log_file = _log_file_path()
    stats = fetch_products_once(fetch_config)
    return stats


def _run_sync_products():
    args = _env_namespace()
    sync_config = build_sync_products_config(args)
    if not sync_config.log_file:
        sync_config.log_file = _log_file_path()
    stats = sync_products_once(sync_config)
    return stats


def _loop_worker():
    next_customer_ts = time.time()
    next_product_ts = time.time()
    while not STATE.stop_event.is_set():
        acquired = STATE.busy_lock.acquire(timeout=1)
        if not acquired:
            time.sleep(1)
            continue

        do_customers = cfg.get_env_bool("AUTO_SYNC_CUSTOMERS", False)
        do_products = cfg.get_env_bool("AUTO_SYNC_PRODUCTS", False)
        total_steps = 2  # fetch DC + sync DC
        if do_customers and time.time() >= next_customer_ts:
            total_steps += 2
        if do_products and time.time() >= next_product_ts:
            total_steps += 2
        step_num = 0
        cycle_errors: List[str] = []

        with STATE.lock:
            STATE.busy = True
            STATE.status = "running"
            STATE.errors = []

        try:
            # --- DC Fetch ---
            step_num += 1
            with STATE.lock:
                STATE.set_step("Fetching DCs from Tally", step_num, total_steps)
            try:
                stats = _run_fetch()
                with STATE.lock:
                    STATE.fetch_stats = stats
                    STATE.last_fetch_at = _now_iso()
                    STATE.add_activity("Fetch DCs", "ok", f"created={stats.get('created',0)} updated={stats.get('updated',0)} deleted={stats.get('deleted',0)}")
            except Exception as exc:
                cycle_errors.append(f"DC Fetch: {exc}")
                with STATE.lock:
                    STATE.add_activity("Fetch DCs", "error", str(exc))

            # --- DC Sync ---
            step_num += 1
            with STATE.lock:
                STATE.set_step("Syncing DCs to Catalytics", step_num, total_steps)
            try:
                stats = _run_sync()
                with STATE.lock:
                    STATE.sync_stats = stats
                    STATE.last_sync_at = _now_iso()
                    STATE.add_activity("Sync DCs", "ok", f"sent={stats.get('sent',0)} ok={stats.get('ok',0)} failed={stats.get('failed',0)}")
            except Exception as exc:
                cycle_errors.append(f"DC Sync: {exc}")
                with STATE.lock:
                    STATE.add_activity("Sync DCs", "error", str(exc))

            # --- Customer Fetch + Sync ---
            if do_customers:
                customer_interval = _get_interval("AUTO_SYNC_CUSTOMERS_INTERVAL_SEC", STATE.interval_sec)
                with STATE.lock:
                    STATE.customer_interval_sec = customer_interval
                if time.time() >= next_customer_ts:
                    step_num += 1
                    with STATE.lock:
                        STATE.set_step("Fetching Customers from Tally", step_num, total_steps)
                    try:
                        stats = _run_fetch_customers()
                        with STATE.lock:
                            STATE.customer_fetch_stats = stats
                            STATE.last_customer_fetch_at = _now_iso()
                            STATE.add_activity("Fetch Customers", "ok", f"created={stats.get('created',0)} updated={stats.get('updated',0)} deleted={stats.get('deleted',0)}")
                    except Exception as exc:
                        cycle_errors.append(f"Customer Fetch: {exc}")
                        with STATE.lock:
                            STATE.add_activity("Fetch Customers", "error", str(exc))

                    step_num += 1
                    with STATE.lock:
                        STATE.set_step("Syncing Customers to Catalytics", step_num, total_steps)
                    try:
                        stats = _run_sync_customers()
                        with STATE.lock:
                            STATE.customer_sync_stats = stats
                            STATE.last_customer_sync_at = _now_iso()
                            STATE.add_activity("Sync Customers", "ok", f"sent={stats.get('sent',0)} ok={stats.get('ok',0)} failed={stats.get('failed',0)}")
                    except Exception as exc:
                        cycle_errors.append(f"Customer Sync: {exc}")
                        with STATE.lock:
                            STATE.add_activity("Sync Customers", "error", str(exc))
                    next_customer_ts = time.time() + customer_interval

            # --- Product Fetch + Sync ---
            if do_products:
                product_interval = _get_interval("AUTO_SYNC_PRODUCTS_INTERVAL_SEC", STATE.interval_sec)
                with STATE.lock:
                    STATE.product_interval_sec = product_interval
                if time.time() >= next_product_ts:
                    step_num += 1
                    with STATE.lock:
                        STATE.set_step("Fetching Products from Tally", step_num, total_steps)
                    try:
                        stats = _run_fetch_products()
                        with STATE.lock:
                            STATE.product_fetch_stats = stats
                            STATE.last_product_fetch_at = _now_iso()
                            STATE.add_activity("Fetch Products", "ok", f"created={stats.get('created',0)} updated={stats.get('updated',0)} deleted={stats.get('deleted',0)}")
                    except Exception as exc:
                        cycle_errors.append(f"Product Fetch: {exc}")
                        with STATE.lock:
                            STATE.add_activity("Fetch Products", "error", str(exc))

                    step_num += 1
                    with STATE.lock:
                        STATE.set_step("Syncing Products to Catalytics", step_num, total_steps)
                    try:
                        stats = _run_sync_products()
                        with STATE.lock:
                            STATE.product_sync_stats = stats
                            STATE.last_product_sync_at = _now_iso()
                            STATE.add_activity("Sync Products", "ok", f"sent={stats.get('sent',0)} ok={stats.get('ok',0)} failed={stats.get('failed',0)}")
                    except Exception as exc:
                        cycle_errors.append(f"Product Sync: {exc}")
                        with STATE.lock:
                            STATE.add_activity("Sync Products", "error", str(exc))
                    next_product_ts = time.time() + product_interval

            # --- Finalize cycle ---
            with STATE.lock:
                STATE.clear_step()
                if cycle_errors:
                    STATE.errors = cycle_errors
                    STATE.last_error = cycle_errors[-1]
                    STATE.status = "error"
                else:
                    STATE.last_error = None
                    STATE.errors = []
                    STATE.status = "idle"
        finally:
            with STATE.lock:
                STATE.busy = False
                STATE.clear_step()
            STATE.busy_lock.release()

        if STATE.stop_event.wait(STATE.interval_sec):
            break

    with STATE.lock:
        STATE.running = False
        STATE.status = "stopped"


def _start_loop(interval_sec: Optional[int] = None) -> bool:
    with STATE.lock:
        if STATE.running:
            return False
        STATE.interval_sec = interval_sec or STATE.interval_sec
        STATE.stop_event.clear()
        STATE.running = True
        STATE.status = "running"
        STATE.loop_thread = threading.Thread(target=_loop_worker, daemon=True)
        STATE.loop_thread.start()
    return True


def _stop_loop() -> bool:
    with STATE.lock:
        if not STATE.running:
            return False
        STATE.stop_event.set()
    return True


def _tail_log(lines: int = 200) -> str:
    log_path = _log_file_path()
    if not os.path.exists(log_path):
        return ""
    with open(log_path, "r", encoding="utf-8", errors="ignore") as handle:
        data = handle.readlines()
    return "".join(data[-lines:])


def _connect_db() -> Tuple[Any, str]:
    db_path = cfg.get_env("TALLY_DB_PATH") or ""
    if not db_path:
        raise ValueError("db_path is required")
    conn = db.connect(db_path)
    db.init_db(conn)
    return conn, db_path


def _sync_state(is_synced: Optional[int], attempts: Optional[int]) -> str:
    if is_synced:
        return "synced"
    if attempts and attempts > 0:
        return "failed"
    return "pending"


def _short_error(value: Optional[str], max_len: int = 160) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _error_signature(error_text: Optional[str]) -> Tuple[str, str]:
    text = (error_text or "").strip()
    if not text:
        return "No error details", "Open details and logs to capture exact server response."
    lower = text.lower()

    if "max attempts reached" in lower:
        return (
            "Max attempts reached",
            "Click Retry after fixing root cause, or Mark to reset attempts.",
        )
    if any(token in lower for token in ("unauthorized", "forbidden", "401", "403", "api key")):
        return (
            "API authentication/authorization failure",
            "Verify `CATALYTICS_API_KEY` in middleware and API permissions on server.",
        )
    if any(token in lower for token in ("connection refused", "failed to establish", "newconnectionerror", "timed out", "timeout")):
        return (
            "Network/API connectivity issue",
            "Check `CATALYTICS_API_BASE_URL`, network route, firewall, and whether API server is running.",
        )
    if any(token in lower for token in ("name or service not known", "nodename nor servname", "temporary failure in name resolution")):
        return (
            "DNS/host resolution issue",
            "Verify API/Tally hostnames and local DNS resolution.",
        )
    if any(token in lower for token in ("404", "not found")):
        return (
            "API endpoint path mismatch",
            "Check `CATALYTICS_API_BASE_URL` and endpoint paths in middleware config.",
        )
    if any(token in lower for token in ("company", "not found")):
        return (
            "Tally company mismatch",
            "Set `TALLY_COMPANY` exactly as shown in Tally.",
        )
    if any(token in lower for token in ("xml", "parseerror", "mismatched tag", "syntax error")):
        return (
            "Invalid XML from Tally",
            "Validate Tally XML response and middleware sanitization logic.",
        )
    if any(token in lower for token in ("json", "decode", "expecting value")):
        return (
            "Invalid JSON response",
            "Inspect server response body and API error handler.",
        )
    if any(token in lower for token in ("db_path is required", "no such table", "sqlite", "database is locked", "column")):
        return (
            "Local database/schema issue",
            "Verify `TALLY_DB_PATH`, DB file permissions, and schema/version alignment.",
        )
    if "tally" in lower and any(token in lower for token in ("refused", "unable", "connect", "timeout")):
        return (
            "Tally connectivity issue",
            "Ensure Tally is running and XML interface is reachable on configured URL.",
        )
    return (
        "Unhandled sync error",
        "Open record details and logs; capture response JSON and stack trace for fix.",
    )


def _error_hint(error_text: Optional[str]) -> str:
    _, hint = _error_signature(error_text)
    return hint


def _check_tcp_endpoint(url: Optional[str], default_port: int) -> Dict[str, Any]:
    value = (url or "").strip()
    if not value:
        return {"url": value, "reachable": False, "error": "not configured"}
    parsed = urlparse(value)
    host = parsed.hostname
    port = parsed.port or default_port
    if not host:
        return {"url": value, "reachable": False, "error": "invalid URL"}
    try:
        with socket.create_connection((host, port), timeout=2):
            return {"url": value, "host": host, "port": port, "reachable": True, "error": None}
    except Exception as exc:
        return {"url": value, "host": host, "port": port, "reachable": False, "error": str(exc)}


def _sync_counts(conn, table: str, sync_table: str, id_col: str) -> Dict[str, int]:
    total_row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    total = int(total_row["c"]) if total_row else 0

    synced_row = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM {table} t
        LEFT JOIN {sync_table} ss ON ss.{id_col} = t.id
        WHERE COALESCE(ss.is_synced, 0) = 1
        """
    ).fetchone()
    failed_row = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM {table} t
        LEFT JOIN {sync_table} ss ON ss.{id_col} = t.id
        WHERE COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) > 0
        """
    ).fetchone()
    pending_row = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM {table} t
        LEFT JOIN {sync_table} ss ON ss.{id_col} = t.id
        WHERE COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) = 0
        """
    ).fetchone()
    deleted_row = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM {table} t
        WHERE COALESCE(t.is_deleted, 0) = 1
        """
    ).fetchone()
    return {
        "total": total,
        "synced": int(synced_row["c"]) if synced_row else 0,
        "failed": int(failed_row["c"]) if failed_row else 0,
        "pending": int(pending_row["c"]) if pending_row else 0,
        "deleted": int(deleted_row["c"]) if deleted_row else 0,
    }


def _collect_error_buckets(conn, sync_table: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT last_error, COUNT(*) AS c
        FROM {sync_table}
        WHERE COALESCE(is_synced, 0) = 0
          AND COALESCE(attempts, 0) > 0
          AND COALESCE(TRIM(last_error), '') <> ''
        GROUP BY last_error
        ORDER BY c DESC
        LIMIT 50
        """
    ).fetchall()
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        raw = str(row["last_error"] or "").strip()
        count = int(row["c"] or 0)
        signature, hint = _error_signature(raw)
        if signature not in buckets:
            buckets[signature] = {
                "reason": signature,
                "count": 0,
                "hint": hint,
                "sample_error": raw,
            }
        buckets[signature]["count"] += count
    result = sorted(buckets.values(), key=lambda x: x["count"], reverse=True)
    return result[:10]


def _collect_diagnostics() -> Dict[str, Any]:
    api_base_url = (
        cfg.get_env("CATALYTICS_API_BASE_URL")
        or cfg.get_env("API_BASE_URL")
        or ""
    ).strip()
    entity_id = (
        cfg.get_env("CATALYTICS_ENTITY_ID")
        or cfg.get_env("ENTITY_ID")
        or ""
    ).strip()
    required_env = {
        "TALLY_DB_PATH": (cfg.get_env("TALLY_DB_PATH") or "").strip(),
        "TALLY_URL": (cfg.get_env("TALLY_URL") or "").strip(),
        "TALLY_COMPANY": (cfg.get_env("TALLY_COMPANY") or "").strip(),
        "CATALYTICS_API_BASE_URL": api_base_url,
        "CATALYTICS_ENTITY_ID": entity_id,
    }
    missing_env = [name for name, value in required_env.items() if not value]

    with STATE.lock:
        source_doc = STATE.source_doc

    tally_check = _check_tcp_endpoint(required_env["TALLY_URL"], default_port=9000)
    api_check = _check_tcp_endpoint(required_env["CATALYTICS_API_BASE_URL"], default_port=80)

    queue = {
        "dc": {"total": 0, "synced": 0, "failed": 0, "pending": 0},
        "customers": {"total": 0, "synced": 0, "failed": 0, "pending": 0},
        "products": {"total": 0, "synced": 0, "failed": 0, "pending": 0},
    }
    reasons: List[Dict[str, Any]] = []
    db_error = None
    try:
        conn, _ = _connect_db()
        queue["dc"] = _sync_counts(conn, "delivery_notes", "sync_status", "delivery_note_id")
        queue["customers"] = _sync_counts(conn, "ledgers", "ledger_sync_status", "ledger_id")
        queue["products"] = _sync_counts(conn, "stock_items", "stock_sync_status", "stock_item_id")

        reason_counter: Counter = Counter()
        merged: Dict[str, Dict[str, Any]] = {}
        for sync_table in ("sync_status", "ledger_sync_status", "stock_sync_status"):
            for item in _collect_error_buckets(conn, sync_table):
                reason_counter[item["reason"]] += item["count"]
                if item["reason"] not in merged:
                    merged[item["reason"]] = item
        conn.close()
        for reason, count in reason_counter.most_common(10):
            record = merged[reason]
            record["count"] = count
            reasons.append(record)
    except Exception as exc:
        db_error = str(exc)

    recommendations: List[str] = []
    if missing_env:
        recommendations.append("Fill required env keys: " + ", ".join(missing_env))
    if not tally_check.get("reachable"):
        recommendations.append("Tally endpoint not reachable. Start Tally and verify TALLY_URL/port.")
    if not api_check.get("reachable"):
        recommendations.append("API endpoint not reachable. Verify CATALYTICS_API_BASE_URL/network/firewall.")
    for item in reasons[:5]:
        hint = item.get("hint")
        if hint and hint not in recommendations:
            recommendations.append(str(hint))
    if db_error:
        recommendations.append("DB diagnostics failed: " + db_error)

    return {
        "generated_at": _now_iso(),
        "source_doc": source_doc,
        "environment": {"values": required_env, "missing": missing_env},
        "connectivity": {"tally": tally_check, "api": api_check},
        "queue": queue,
        "top_reasons": reasons,
        "recommendations": recommendations[:8],
        "db_error": db_error,
    }


def _is_sundry_debtor_json(data_json: Optional[str]) -> bool:
    if not data_json:
        return False
    try:
        ledger = db.json_loads(data_json) or {}
    except Exception:
        return False
    parent = (ledger.get("PARENT") or ledger.get("PARENTNAME") or "").strip()
    return bool(parent) and parent.casefold() == "sundry debtors"


def _max_attempts() -> int:
    return cfg.get_env_int("SYNC_MAX_ATTEMPTS", 5) or 5


def _list_dc(status_filter: str, search: str, limit: int, offset: int) -> Dict[str, Any]:
    conn, _ = _connect_db()
    try:
        params: List[Any] = []
        where = []
        if status_filter == "synced":
            where.append("COALESCE(ss.is_synced, 0) = 1")
        elif status_filter == "failed":
            where.append("COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) > 0")
        elif status_filter == "pending":
            where.append("COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) = 0")
        elif status_filter == "deleted":
            where.append("COALESCE(dn.is_deleted, 0) = 1")
        if search:
            where.append("(dn.dc_no LIKE ? OR dn.party_ledger_name LIKE ? OR dn.reference LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        count_row = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM delivery_notes dn
            LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id
            {where_sql}
            """,
            tuple(params),
        ).fetchone()
        total = int(count_row[0]) if count_row else 0

        params.extend([limit, offset])
        rows = conn.execute(
            f"""
            SELECT dn.id, dn.dc_no, dn.voucher_date, dn.party_ledger_name, dn.reference,
                   dn.updated_at, COALESCE(dn.is_deleted, 0) as is_deleted,
                   ss.is_synced, ss.attempts, ss.last_attempt_at, ss.synced_at, ss.last_error
            FROM delivery_notes dn
            LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id
            {where_sql}
            ORDER BY dn.updated_at DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        ).fetchall()
        max_attempts = _max_attempts()
        data = []
        for row in rows:
            item = dict(row)
            item["sync_state"] = _sync_state(item.get("is_synced"), item.get("attempts"))
            if not item.get("last_error") and item["sync_state"] == "failed":
                if max_attempts and (item.get("attempts") or 0) >= max_attempts:
                    item["last_error"] = f"Max attempts reached ({max_attempts}). Click Retry or Mark."
            item["last_error_short"] = _short_error(item.get("last_error"))
            item["error_hint"] = _error_hint(item.get("last_error"))
            data.append(item)
        return {"total": total, "items": data}
    finally:
        conn.close()


def _select_dc_ids(status_filter: str, search: str, limit: int) -> List[int]:
    conn, _ = _connect_db()
    try:
        params: List[Any] = []
        where = []
        if status_filter == "synced":
            where.append("COALESCE(ss.is_synced, 0) = 1")
        elif status_filter == "failed":
            where.append("COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) > 0")
        elif status_filter == "pending":
            where.append("COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) = 0")
        elif status_filter == "deleted":
            where.append("COALESCE(dn.is_deleted, 0) = 1")
        if search:
            where.append("(dn.dc_no LIKE ? OR dn.party_ledger_name LIKE ? OR dn.reference LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT dn.id
            FROM delivery_notes dn
            LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id
            {where_sql}
            ORDER BY dn.updated_at DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        return ids
    finally:
        conn.close()


def _list_simple(
    *,
    table: str,
    sync_table: str,
    id_col: str,
    status_filter: str,
    search: str,
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    conn, _ = _connect_db()
    try:
        params: List[Any] = []
        where = []
        if status_filter == "synced":
            where.append("COALESCE(ss.is_synced, 0) = 1")
        elif status_filter == "failed":
            where.append("COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) > 0")
        elif status_filter == "pending":
            where.append("COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) = 0")
        elif status_filter == "deleted":
            where.append("COALESCE(t.is_deleted, 0) = 1")
        if search:
            where.append("t.name LIKE ?")
            params.append(f"%{search}%")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        count_row = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM {table} t
            LEFT JOIN {sync_table} ss ON ss.{id_col} = t.id
            {where_sql}
            """,
            tuple(params),
        ).fetchone()
        total = int(count_row[0]) if count_row else 0

        params.extend([limit, offset])
        rows = conn.execute(
            f"""
            SELECT t.id, t.name, t.updated_at, COALESCE(t.is_deleted, 0) as is_deleted,
                   ss.is_synced, ss.attempts, ss.last_attempt_at, ss.synced_at, ss.last_error
            FROM {table} t
            LEFT JOIN {sync_table} ss ON ss.{id_col} = t.id
            {where_sql}
            ORDER BY t.updated_at DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        ).fetchall()
        max_attempts = _max_attempts()
        data = []
        for row in rows:
            item = dict(row)
            item["sync_state"] = _sync_state(item.get("is_synced"), item.get("attempts"))
            if not item.get("last_error") and item["sync_state"] == "failed":
                if max_attempts and (item.get("attempts") or 0) >= max_attempts:
                    item["last_error"] = f"Max attempts reached ({max_attempts}). Click Retry or Mark."
            item["last_error_short"] = _short_error(item.get("last_error"))
            item["error_hint"] = _error_hint(item.get("last_error"))
            data.append(item)
        return {"total": total, "items": data}
    finally:
        conn.close()


def _list_customers(status_filter: str, search: str, limit: int, offset: int) -> Dict[str, Any]:
    conn, _ = _connect_db()
    try:
        params: List[Any] = []
        where = []
        if status_filter == "synced":
            where.append("COALESCE(ss.is_synced, 0) = 1")
        elif status_filter == "failed":
            where.append("COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) > 0")
        elif status_filter == "pending":
            where.append("COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) = 0")
        elif status_filter == "deleted":
            where.append("COALESCE(t.is_deleted, 0) = 1")
        if search:
            where.append("t.name LIKE ?")
            params.append(f"%{search}%")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        rows = conn.execute(
            f"""
            SELECT t.id, t.name, t.updated_at, t.data_json, COALESCE(t.is_deleted, 0) as is_deleted,
                   ss.is_synced, ss.attempts, ss.last_attempt_at, ss.synced_at, ss.last_error
            FROM ledgers t
            LEFT JOIN ledger_sync_status ss ON ss.ledger_id = t.id
            {where_sql}
            ORDER BY t.updated_at DESC
            """,
            tuple(params),
        ).fetchall()

        filtered: List[Dict[str, Any]] = []
        for row in rows:
            if _is_sundry_debtor_json(row["data_json"]):
                filtered.append(dict(row))

        total = len(filtered)
        page = filtered[offset : offset + limit]
        max_attempts = _max_attempts()
        data = []
        for row in page:
            item = dict(row)
            item["sync_state"] = _sync_state(item.get("is_synced"), item.get("attempts"))
            if not item.get("last_error") and item["sync_state"] == "failed":
                if max_attempts and (item.get("attempts") or 0) >= max_attempts:
                    item["last_error"] = f"Max attempts reached ({max_attempts}). Click Retry or Mark."
            item["last_error_short"] = _short_error(item.get("last_error"))
            item["error_hint"] = _error_hint(item.get("last_error"))
            data.append(item)
        return {"total": total, "items": data}
    finally:
        conn.close()


def _select_simple_ids(
    *,
    table: str,
    sync_table: str,
    id_col: str,
    status_filter: str,
    search: str,
    limit: int,
) -> List[int]:
    conn, _ = _connect_db()
    try:
        params: List[Any] = []
        where = []
        if status_filter == "synced":
            where.append("COALESCE(ss.is_synced, 0) = 1")
        elif status_filter == "failed":
            where.append("COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) > 0")
        elif status_filter == "pending":
            where.append("COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) = 0")
        elif status_filter == "deleted":
            where.append("COALESCE(t.is_deleted, 0) = 1")
        if search:
            where.append("t.name LIKE ?")
            params.append(f"%{search}%")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT t.id
            FROM {table} t
            LEFT JOIN {sync_table} ss ON ss.{id_col} = t.id
            {where_sql}
            ORDER BY t.updated_at DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        return ids
    finally:
        conn.close()


def _select_customer_ids(status_filter: str, search: str, limit: int) -> List[int]:
    conn, _ = _connect_db()
    try:
        params: List[Any] = []
        where = []
        if status_filter == "synced":
            where.append("COALESCE(ss.is_synced, 0) = 1")
        elif status_filter == "failed":
            where.append("COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) > 0")
        elif status_filter == "pending":
            where.append("COALESCE(ss.is_synced, 0) = 0 AND COALESCE(ss.attempts, 0) = 0")
        elif status_filter == "deleted":
            where.append("COALESCE(t.is_deleted, 0) = 1")
        if search:
            where.append("t.name LIKE ?")
            params.append(f"%{search}%")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        rows = conn.execute(
            f"""
            SELECT t.id, t.data_json
            FROM ledgers t
            LEFT JOIN ledger_sync_status ss ON ss.ledger_id = t.id
            {where_sql}
            ORDER BY t.updated_at DESC
            """,
            tuple(params),
        ).fetchall()

        ids: List[int] = []
        for row in rows:
            if _is_sundry_debtor_json(row["data_json"]):
                ids.append(int(row["id"]))
            if len(ids) >= limit:
                break

        return ids
    finally:
        conn.close()


def _dc_detail(note_id: int) -> Dict[str, Any]:
    conn, _ = _connect_db()
    try:
        note = conn.execute(
            """
            SELECT dn.*, ss.is_synced, ss.attempts, ss.last_attempt_at, ss.synced_at,
                   ss.last_error, ss.last_response_json
            FROM delivery_notes dn
            LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id
            WHERE dn.id = ?
            """,
            (note_id,),
        ).fetchone()
        if not note:
            raise ValueError("DC not found")

        items = conn.execute(
            "SELECT data_json FROM delivery_note_items WHERE delivery_note_id = ? ORDER BY line_no",
            (note_id,),
        ).fetchall()
        item_payloads = [db.json_loads(r["data_json"]) for r in items if r["data_json"]]
        result = dict(note)
        result["payload"] = db.json_loads(note["data_json"]) if note["data_json"] else None
        result["items"] = item_payloads
        result["last_response_json"] = db.json_loads(note["last_response_json"]) if note["last_response_json"] else None
        result["sync_state"] = _sync_state(note["is_synced"], note["attempts"])
        result["error_hint"] = _error_hint(note["last_error"])
        return result
    finally:
        conn.close()


def _simple_detail(table: str, sync_table: str, id_col: str, item_id: int) -> Dict[str, Any]:
    conn, _ = _connect_db()
    try:
        row = conn.execute(
            f"""
            SELECT t.*, ss.is_synced, ss.attempts, ss.last_attempt_at, ss.synced_at,
                   ss.last_error, ss.last_response_json
            FROM {table} t
            LEFT JOIN {sync_table} ss ON ss.{id_col} = t.id
            WHERE t.id = ?
            """,
            (item_id,),
        ).fetchone()
        if not row:
            raise ValueError("Record not found")
        result = dict(row)
        result["payload"] = db.json_loads(row["data_json"]) if row["data_json"] else None
        result["last_response_json"] = db.json_loads(row["last_response_json"]) if row["last_response_json"] else None
        result["sync_state"] = _sync_state(row["is_synced"], row["attempts"])
        result["error_hint"] = _error_hint(row["last_error"])
        return result
    finally:
        conn.close()


def _mark_unsynced(table: str, id_col: str, item_id: int) -> None:
    conn, _ = _connect_db()
    try:
        ts = db.now_ts()
        conn.execute(
            f"""
            UPDATE {table}
            SET is_synced = 0,
                attempts = 0,
                last_attempt_at = NULL,
                synced_at = NULL,
                last_error = NULL,
                last_response_json = NULL,
                updated_at = ?
            WHERE {id_col} = ?
            """,
            (ts, item_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_unsynced_many(table: str, id_col: str, ids: List[int]) -> int:
    if not ids:
        return 0
    conn, _ = _connect_db()
    try:
        ts = db.now_ts()
        placeholders = ",".join(["?"] * len(ids))
        conn.execute(
            f"""
            UPDATE {table}
            SET is_synced = 0,
                attempts = 0,
                last_attempt_at = NULL,
                synced_at = NULL,
                last_error = NULL,
                last_response_json = NULL,
                updated_at = ?
            WHERE {id_col} IN ({placeholders})
            """,
            tuple([ts] + ids),
        )
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def _retry_dc(note_id: int) -> Dict[str, Any]:
    args = _env_namespace()
    sync_config = build_sync_config(args)
    if not sync_config.log_file:
        sync_config.log_file = _log_file_path()

    if not sync_config.api_base_url or not sync_config.db_path:
        raise ValueError("db_path and api_base_url are required")

    conn = db.connect(sync_config.db_path)
    try:
        db.init_db(conn)
        note = conn.execute("SELECT * FROM delivery_notes WHERE id = ?", (note_id,)).fetchone()
        if not note:
            raise ValueError("DC not found")

        payload, payload_hash = sync_dc_mod._build_payload_for_note(
            conn,
            dict(note),
            entity_id=sync_config.entity_id,
            company_name=sync_config.company,
            allow_tally_fetch=sync_config.allow_tally_fetch,
        )

        endpoint = sync_config.api_base_url.rstrip("/") + "/import/tally-delivery-challan-payload/"
        headers = {}
        if sync_config.api_key:
            headers["X-API-Key"] = sync_config.api_key

        resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)
        try:
            response_json = resp.json()
        except Exception:
            response_json = {"status": "error", "message": resp.text}

        result = (response_json.get("data") or {}).get("results") or []
        status_val = None
        if result:
            status_val = (result[0] or {}).get("status")
        success = status_val in ("created", "updated")

        sync_dc_mod._update_sync_status(
            conn,
            delivery_note_id=note_id,
            success=success,
            payload_hash=payload_hash,
            response_json=response_json,
            error_text=None if success else response_json.get("message") or "sync_failed",
        )
        conn.commit()
        return {"success": success, "response": response_json}
    finally:
        conn.close()


def _retry_customer(ledger_id: int) -> Dict[str, Any]:
    args = _env_namespace()
    sync_config = build_sync_customers_config(args)
    if not sync_config.log_file:
        sync_config.log_file = _log_file_path()

    if not sync_config.api_base_url or not sync_config.db_path:
        raise ValueError("db_path and api_base_url are required")

    conn = db.connect(sync_config.db_path)
    try:
        db.init_db(conn)
        row = conn.execute("SELECT * FROM ledgers WHERE id = ?", (ledger_id,)).fetchone()
        if not row:
            raise ValueError("Customer not found")

        payload = sync_cust_mod._build_payload_for_ledger(
            dict(row),
            entity_id=sync_config.entity_id,
            company_name=sync_config.company,
        )
        payload_hash = db.sha256_text(db.json_dumps(payload))

        endpoint = sync_config.api_base_url.rstrip("/") + "/import/tally-customer-payload/"
        headers = {}
        if sync_config.api_key:
            headers["X-API-Key"] = sync_config.api_key

        resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)
        try:
            response_json = resp.json()
        except Exception:
            response_json = {"status": "error", "message": resp.text}

        results = (response_json.get("data") or {}).get("results") or []
        res = None
        if results and isinstance(results[0], dict):
            res = results[0]
        status_val = (res or {}).get("status")
        success = sync_cust_mod._status_is_success(status_val)
        if not success and res is None and sync_cust_mod._response_indicates_success(response_json):
            success = True

        error_text = None
        if not success:
            raw_error = (res or {}).get("message")
            if not raw_error:
                data = response_json.get("data") or {}
                errors = data.get("errors")
                if response_json.get("status") != "success" or (isinstance(errors, int) and errors > 0):
                    raw_error = response_json.get("message") or "sync_failed"
                else:
                    raw_error = "sync_failed"
            error_text = sync_cust_mod._clean_error_text(raw_error) or "sync_failed"

        sync_cust_mod._update_sync_status(
            conn,
            ledger_id=ledger_id,
            success=success,
            payload_hash=payload_hash,
            response_json=response_json,
            error_text=error_text,
        )
        conn.commit()
        return {"success": success, "response": response_json}
    finally:
        conn.close()


def _retry_product(stock_item_id: int) -> Dict[str, Any]:
    args = _env_namespace()
    sync_config = build_sync_products_config(args)
    if not sync_config.log_file:
        sync_config.log_file = _log_file_path()

    if not sync_config.api_base_url or not sync_config.db_path:
        raise ValueError("db_path and api_base_url are required")

    conn = db.connect(sync_config.db_path)
    try:
        db.init_db(conn)
        row = conn.execute("SELECT * FROM stock_items WHERE id = ?", (stock_item_id,)).fetchone()
        if not row:
            raise ValueError("Product not found")

        payload = sync_prod_mod._build_payload_for_item(
            dict(row),
            entity_id=sync_config.entity_id,
            company_name=sync_config.company,
        )
        payload_hash = db.sha256_text(db.json_dumps(payload))

        endpoint = sync_config.api_base_url.rstrip("/") + "/import/tally-product-payload/"
        headers = {}
        if sync_config.api_key:
            headers["X-API-Key"] = sync_config.api_key

        resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)
        try:
            response_json = resp.json()
        except Exception:
            response_json = {"status": "error", "message": resp.text}

        results = (response_json.get("data") or {}).get("results") or []
        status_val = None
        if results:
            status_val = (results[0] or {}).get("status")
        success = status_val in ("created", "updated")

        sync_prod_mod._update_sync_status(
            conn,
            stock_item_id=stock_item_id,
            success=success,
            payload_hash=payload_hash,
            response_json=response_json,
            error_text=None if success else response_json.get("message") or "sync_failed",
        )
        conn.commit()
        return {"success": success, "response": response_json}
    finally:
        conn.close()


def _check_auth() -> Optional[Response]:
    api_key = cfg.get_env("UI_API_KEY")
    if not api_key:
        return None
    provided = request.headers.get("X-API-Key") or request.args.get("api_key")
    if provided != api_key:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    return None


def _parse_list_args() -> Tuple[str, str, int, int]:
    status = (request.args.get("status") or "all").lower()
    if status not in ("all", "synced", "failed", "pending", "deleted"):
        status = "all"
    search = (request.args.get("search") or "").strip()
    try:
        limit = int(request.args.get("limit") or 25)
    except ValueError:
        limit = 25
    if limit < 1:
        limit = 25
    if limit > 200:
        limit = 200
    try:
        offset = int(request.args.get("offset") or 0)
    except ValueError:
        offset = 0
    if offset < 0:
        offset = 0
    return status, search, limit, offset


def _parse_bulk_args() -> Tuple[str, str, int]:
    status, search, _, _ = _parse_list_args()
    try:
        limit = int(request.args.get("limit") or 200)
    except ValueError:
        limit = 200
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500
    return status, search, limit


@app.route("/")
def index():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    return Response(_html_page(), mimetype="text/html")


@app.route("/api/status")
def api_status():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    with STATE.lock:
        payload = STATE.to_dict()
    return jsonify({"status": "success", "data": payload})


@app.route("/api/diagnostics")
def api_diagnostics():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    try:
        data = _collect_diagnostics()
        return jsonify({"status": "success", "data": data})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/source-mode", methods=["POST"])
def api_source_mode():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    data = request.get_json(silent=True) or {}
    source_doc = _normalize_source_doc(data.get("source_doc"))
    with STATE.lock:
        STATE.source_doc = source_doc
        payload = STATE.to_dict()
    return jsonify({"status": "success", "data": payload})


@app.route("/api/dc/list")
def api_dc_list():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    try:
        status, search, limit, offset = _parse_list_args()
        data = _list_dc(status, search, limit, offset)
        return jsonify({"status": "success", "data": data})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/customers/list")
def api_customer_list():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    try:
        status, search, limit, offset = _parse_list_args()
        data = _list_customers(status, search, limit, offset)
        return jsonify({"status": "success", "data": data})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/products/list")
def api_product_list():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    try:
        status, search, limit, offset = _parse_list_args()
        data = _list_simple(
            table="stock_items",
            sync_table="stock_sync_status",
            id_col="stock_item_id",
            status_filter=status,
            search=search,
            limit=limit,
            offset=offset,
        )
        return jsonify({"status": "success", "data": data})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/dc/bulk-retry", methods=["POST"])
def api_dc_bulk_retry():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
    try:
        status, search, limit = _parse_bulk_args()
        ids = _select_dc_ids(status, search, limit)
        ok = 0
        failed = 0
        errors: List[Dict[str, Any]] = []
        for note_id in ids:
            try:
                result = _retry_dc(note_id)
                if result.get("success"):
                    ok += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                errors.append({"id": note_id, "error": str(exc)})
        return jsonify({"status": "success", "data": {"matched": len(ids), "ok": ok, "failed": failed, "errors": errors[:10]}})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
        STATE.busy_lock.release()


@app.route("/api/customers/bulk-retry", methods=["POST"])
def api_customers_bulk_retry():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
    try:
        status, search, limit = _parse_bulk_args()
        ids = _select_customer_ids(status, search, limit)
        ok = 0
        failed = 0
        errors: List[Dict[str, Any]] = []
        for ledger_id in ids:
            try:
                result = _retry_customer(ledger_id)
                if result.get("success"):
                    ok += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                errors.append({"id": ledger_id, "error": str(exc)})
        return jsonify({"status": "success", "data": {"matched": len(ids), "ok": ok, "failed": failed, "errors": errors[:10]}})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
        STATE.busy_lock.release()


@app.route("/api/products/bulk-retry", methods=["POST"])
def api_products_bulk_retry():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
    try:
        status, search, limit = _parse_bulk_args()
        ids = _select_simple_ids(
            table="stock_items",
            sync_table="stock_sync_status",
            id_col="stock_item_id",
            status_filter=status,
            search=search,
            limit=limit,
        )
        ok = 0
        failed = 0
        errors: List[Dict[str, Any]] = []
        for stock_item_id in ids:
            try:
                result = _retry_product(stock_item_id)
                if result.get("success"):
                    ok += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                errors.append({"id": stock_item_id, "error": str(exc)})
        return jsonify({"status": "success", "data": {"matched": len(ids), "ok": ok, "failed": failed, "errors": errors[:10]}})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
        STATE.busy_lock.release()


@app.route("/api/dc/bulk-mark", methods=["POST"])
def api_dc_bulk_mark():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
    try:
        status, search, limit = _parse_bulk_args()
        ids = _select_dc_ids(status, search, limit)
        count = _mark_unsynced_many("sync_status", "delivery_note_id", ids)
        return jsonify({"status": "success", "data": {"matched": len(ids), "updated": count}})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
        STATE.busy_lock.release()


@app.route("/api/customers/bulk-mark", methods=["POST"])
def api_customers_bulk_mark():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
    try:
        status, search, limit = _parse_bulk_args()
        ids = _select_customer_ids(status, search, limit)
        count = _mark_unsynced_many("ledger_sync_status", "ledger_id", ids)
        return jsonify({"status": "success", "data": {"matched": len(ids), "updated": count}})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
        STATE.busy_lock.release()


@app.route("/api/products/bulk-mark", methods=["POST"])
def api_products_bulk_mark():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
    try:
        status, search, limit = _parse_bulk_args()
        ids = _select_simple_ids(
            table="stock_items",
            sync_table="stock_sync_status",
            id_col="stock_item_id",
            status_filter=status,
            search=search,
            limit=limit,
        )
        count = _mark_unsynced_many("stock_sync_status", "stock_item_id", ids)
        return jsonify({"status": "success", "data": {"matched": len(ids), "updated": count}})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
        STATE.busy_lock.release()


@app.route("/api/dc/<int:note_id>")
def api_dc_detail(note_id: int):
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    try:
        data = _dc_detail(note_id)
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/customers/<int:ledger_id>")
def api_customer_detail(ledger_id: int):
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    try:
        data = _simple_detail("ledgers", "ledger_sync_status", "ledger_id", ledger_id)
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/products/<int:stock_item_id>")
def api_product_detail(stock_item_id: int):
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    try:
        data = _simple_detail("stock_items", "stock_sync_status", "stock_item_id", stock_item_id)
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/api/dc/<int:note_id>/retry", methods=["POST"])
def api_dc_retry(note_id: int):
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
    try:
        result = _retry_dc(note_id)
        return jsonify({"status": "success", "data": result})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
        STATE.busy_lock.release()


@app.route("/api/customers/<int:ledger_id>/retry", methods=["POST"])
def api_customer_retry(ledger_id: int):
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
    try:
        result = _retry_customer(ledger_id)
        return jsonify({"status": "success", "data": result})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
        STATE.busy_lock.release()


@app.route("/api/products/<int:stock_item_id>/retry", methods=["POST"])
def api_product_retry(stock_item_id: int):
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
    try:
        result = _retry_product(stock_item_id)
        return jsonify({"status": "success", "data": result})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
        STATE.busy_lock.release()


@app.route("/api/dc/<int:note_id>/mark-unsynced", methods=["POST"])
def api_dc_mark_unsynced(note_id: int):
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
    try:
        _mark_unsynced("sync_status", "delivery_note_id", note_id)
        return jsonify({"status": "success"})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
        STATE.busy_lock.release()


@app.route("/api/customers/<int:ledger_id>/mark-unsynced", methods=["POST"])
def api_customer_mark_unsynced(ledger_id: int):
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
    try:
        _mark_unsynced("ledger_sync_status", "ledger_id", ledger_id)
        return jsonify({"status": "success"})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
        STATE.busy_lock.release()


@app.route("/api/products/<int:stock_item_id>/mark-unsynced", methods=["POST"])
def api_product_mark_unsynced(stock_item_id: int):
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
    try:
        _mark_unsynced("stock_sync_status", "stock_item_id", stock_item_id)
        return jsonify({"status": "success"})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
        STATE.busy_lock.release()


@app.route("/api/start", methods=["POST"])
def api_start():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    interval = request.json.get("interval_sec") if request.is_json else None
    started = _start_loop(interval)
    return jsonify({"status": "success", "started": started})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    stopped = _stop_loop()
    return jsonify({"status": "success", "stopped": stopped})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    _stop_loop()
    time.sleep(0.2)
    started = _start_loop()
    return jsonify({"status": "success", "started": started})


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
        STATE.current_step = "Fetching DCs (manual)"
    try:
        stats = _run_fetch()
        with STATE.lock:
            STATE.fetch_stats = stats
            STATE.last_fetch_at = _now_iso()
            STATE.last_error = None
            STATE.add_activity("Fetch DCs (manual)", "ok", f"created={stats.get('created',0)} updated={stats.get('updated',0)} deleted={stats.get('deleted',0)}")
        return jsonify({"status": "success", "data": stats})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
            STATE.add_activity("Fetch DCs (manual)", "error", str(exc))
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
            STATE.current_step = None
        STATE.busy_lock.release()


@app.route("/api/sync", methods=["POST"])
def api_sync():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
        STATE.current_step = "Syncing DCs (manual)"
    try:
        stats = _run_sync()
        with STATE.lock:
            STATE.sync_stats = stats
            STATE.last_sync_at = _now_iso()
            STATE.last_error = None
            STATE.add_activity("Sync DCs (manual)", "ok", f"sent={stats.get('sent',0)} ok={stats.get('ok',0)} failed={stats.get('failed',0)}")
        return jsonify({"status": "success", "data": stats})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
            STATE.add_activity("Sync DCs (manual)", "error", str(exc))
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
            STATE.current_step = None
        STATE.busy_lock.release()


@app.route("/api/logs")
def api_logs():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    lines = int(request.args.get("lines", "200"))
    return jsonify({"status": "success", "data": _tail_log(lines)})


@app.route("/api/fetch-customers", methods=["POST"])
def api_fetch_customers():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
        STATE.current_step = "Fetching Customers (manual)"
    try:
        stats = _run_fetch_customers()
        with STATE.lock:
            STATE.customer_fetch_stats = stats
            STATE.last_customer_fetch_at = _now_iso()
            STATE.last_error = None
            STATE.add_activity("Fetch Customers (manual)", "ok", f"created={stats.get('created',0)} updated={stats.get('updated',0)} deleted={stats.get('deleted',0)}")
        return jsonify({"status": "success", "data": stats})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
            STATE.add_activity("Fetch Customers (manual)", "error", str(exc))
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
            STATE.current_step = None
        STATE.busy_lock.release()


@app.route("/api/sync-customers", methods=["POST"])
def api_sync_customers():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
        STATE.current_step = "Syncing Customers (manual)"
    try:
        stats = _run_sync_customers()
        with STATE.lock:
            STATE.customer_sync_stats = stats
            STATE.last_customer_sync_at = _now_iso()
            STATE.last_error = None
            STATE.add_activity("Sync Customers (manual)", "ok", f"sent={stats.get('sent',0)} ok={stats.get('ok',0)} failed={stats.get('failed',0)}")
        return jsonify({"status": "success", "data": stats})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
            STATE.add_activity("Sync Customers (manual)", "error", str(exc))
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
            STATE.current_step = None
        STATE.busy_lock.release()


@app.route("/api/fetch-products", methods=["POST"])
def api_fetch_products():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
        STATE.current_step = "Fetching Products (manual)"
    try:
        stats = _run_fetch_products()
        with STATE.lock:
            STATE.product_fetch_stats = stats
            STATE.last_product_fetch_at = _now_iso()
            STATE.last_error = None
            STATE.add_activity("Fetch Products (manual)", "ok", f"created={stats.get('created',0)} updated={stats.get('updated',0)} deleted={stats.get('deleted',0)}")
        return jsonify({"status": "success", "data": stats})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
            STATE.add_activity("Fetch Products (manual)", "error", str(exc))
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
            STATE.current_step = None
        STATE.busy_lock.release()


@app.route("/api/sync-products", methods=["POST"])
def api_sync_products():
    auth_resp = _check_auth()
    if auth_resp:
        return auth_resp
    if not STATE.busy_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Busy"}), 409
    with STATE.lock:
        STATE.busy = True
        STATE.current_step = "Syncing Products (manual)"
    try:
        stats = _run_sync_products()
        with STATE.lock:
            STATE.product_sync_stats = stats
            STATE.last_product_sync_at = _now_iso()
            STATE.last_error = None
            STATE.add_activity("Sync Products (manual)", "ok", f"sent={stats.get('sent',0)} ok={stats.get('ok',0)} failed={stats.get('failed',0)}")
        return jsonify({"status": "success", "data": stats})
    except Exception as exc:
        with STATE.lock:
            STATE.last_error = str(exc)
            STATE.add_activity("Sync Products (manual)", "error", str(exc))
        return jsonify({"status": "error", "message": str(exc)}), 500
    finally:
        with STATE.lock:
            STATE.busy = False
            STATE.current_step = None
        STATE.busy_lock.release()


def _html_page() -> str:
    return """
<!doctype html>
<html>
<head>
  <title>Tally Middleware UI</title>
  <style>
    :root {
      --bg: #f6f7fb;
      --card: #ffffff;
      --text: #1c1c1c;
      --muted: #6b6b6b;
      --border: #e5e7eb;
      --accent: #2563eb;
      --accent-soft: #e8efff;
      --danger: #c2410c;
      --ok: #0f766e;
      --shadow: 0 6px 20px rgba(0,0,0,0.06);
    }
    * { box-sizing: border-box; }
    body { font-family: "Segoe UI", Tahoma, Arial, sans-serif; margin: 0; background: var(--bg); color: var(--text); }
    .container { max-width: 1100px; margin: 24px auto; padding: 0 16px 24px; }
    header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 16px; }
    header h1 { font-size: 24px; margin: 0; }
    header .sub { color: var(--muted); font-size: 13px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; box-shadow: var(--shadow); }
    .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 6px; }
    .value { font-size: 16px; font-weight: 600; }
    .value.status { padding: 4px 8px; display: inline-block; border-radius: 999px; font-size: 12px; text-transform: capitalize; }
    .status.idle { background: #fef3c7; color: #92400e; }
    .status.scheduled { background: #e0f2fe; color: #075985; }
    .status.running { background: #dcfce7; color: #166534; }
    .status.error { background: #fee2e2; color: #991b1b; }
    .status.stopped { background: #e5e7eb; color: #374151; }
    .stack { display: flex; gap: 8px; flex-wrap: wrap; }
    .btn { padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border); background: #fff; cursor: pointer; }
    .btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
    .btn.ghost { background: var(--accent-soft); border-color: var(--accent-soft); color: var(--accent); }
    .btn.danger { background: #fee2e2; border-color: #fecaca; color: #991b1b; }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .section { margin-top: 18px; }
    .section h2 { font-size: 16px; margin: 0 0 10px 0; }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
    .stat-line { font-size: 13px; color: var(--muted); }
    .stat-line strong { color: var(--text); }
    .toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    input[type="text"], input[type="number"] { padding: 6px 8px; border-radius: 6px; border: 1px solid var(--border); }
    #log { white-space: pre; background: #0b1220; color: #d1d5db; padding: 12px; height: 320px; overflow: auto; border-radius: 8px; border: 1px solid #0b1220; }
    .pill { padding: 2px 8px; border-radius: 999px; background: #eef2ff; color: #3730a3; font-size: 12px; }
    .error-text { color: var(--danger); font-weight: 600; }
    .hint-text { color: #0f766e; font-size: 11px; margin-top: 4px; }
    .diag-list { margin: 0; padding-left: 18px; font-size: 13px; color: var(--text); }
    .diag-list li { margin: 4px 0; }
    select { padding: 6px 8px; border-radius: 6px; border: 1px solid var(--border); background: #fff; }
    .tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
    .tab { padding: 6px 12px; border-radius: 999px; border: 1px solid var(--border); background: #fff; cursor: pointer; font-size: 13px; }
    .tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .table-wrap { overflow-x: auto; }
    .data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
    .data-table th { text-align: left; font-size: 11px; color: var(--muted); text-transform: uppercase; border-bottom: 1px solid var(--border); padding: 6px; letter-spacing: 0.04em; }
    .data-table td { padding: 6px; border-bottom: 1px solid var(--border); vertical-align: top; }
    .badge { display: inline-block; padding: 2px 6px; border-radius: 6px; font-size: 11px; text-transform: capitalize; }
    .badge.synced { background: #dcfce7; color: #166534; }
    .badge.failed { background: #fee2e2; color: #991b1b; }
    .badge.pending { background: #fef3c7; color: #92400e; }
    .badge.deleted { background: #e5e7eb; color: #6b7280; text-decoration: line-through; }
    .truncate { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .pagination { display: flex; align-items: center; justify-content: space-between; margin-top: 10px; gap: 8px; }
    .btn.small { padding: 4px 8px; font-size: 12px; }
    .hidden { display: none !important; }
    .modal { position: fixed; inset: 0; background: rgba(0,0,0,0.45); display: flex; align-items: center; justify-content: center; padding: 16px; }
    .modal-content { background: #fff; border-radius: 10px; max-width: 900px; width: 100%; max-height: 90vh; overflow: auto; box-shadow: var(--shadow); border: 1px solid var(--border); }
    .modal-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-bottom: 1px solid var(--border); }
    .modal-body { padding: 12px 16px; }
    .modal-section { margin-bottom: 12px; }
    .modal-section h3 { margin: 0 0 6px 0; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
    .modal-body pre { background: #0b1220; color: #d1d5db; padding: 12px; border-radius: 8px; white-space: pre-wrap; font-size: 12px; line-height: 1.4; }
    .modal-body .empty { color: var(--muted); font-style: italic; }
    .step-bar { display: flex; align-items: center; gap: 10px; padding: 8px 14px; background: #eef2ff; border: 1px solid #c7d2fe; border-radius: 8px; margin-bottom: 12px; }
    .step-bar .step-label { font-size: 13px; font-weight: 600; color: #3730a3; }
    .step-bar .step-progress { flex: 1; height: 6px; background: #c7d2fe; border-radius: 4px; overflow: hidden; }
    .step-bar .step-fill { height: 100%; background: #4f46e5; border-radius: 4px; transition: width 0.3s; }
    .step-bar .step-count { font-size: 12px; color: #6366f1; white-space: nowrap; }
    .activity-list { max-height: 240px; overflow-y: auto; }
    .activity-item { display: flex; gap: 8px; align-items: baseline; padding: 4px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
    .activity-item:last-child { border-bottom: none; }
    .activity-item .act-time { color: var(--muted); white-space: nowrap; min-width: 130px; }
    .activity-item .act-action { font-weight: 600; min-width: 160px; }
    .activity-item .act-detail { color: var(--muted); flex: 1; }
    .activity-item .act-ok { color: #166534; }
    .activity-item .act-error { color: #991b1b; }
    .errors-list { margin: 4px 0 0; padding-left: 16px; font-size: 12px; }
    .errors-list li { color: var(--danger); margin: 2px 0; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
    .pulse { animation: pulse 1.5s ease-in-out infinite; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>Tally Middleware UI</h1>
      <div class="sub">Live control panel for Tally fetch and Catalytics sync</div>
    </header>

    <div id="stepBar" class="step-bar hidden">
      <span class="step-label pulse" id="stepLabel">-</span>
      <div class="step-progress"><div class="step-fill" id="stepFill" style="width:0%"></div></div>
      <span class="step-count" id="stepCount">-</span>
    </div>

    <div id="errorsCard" class="card hidden" style="margin-bottom:12px; border-color: #fecaca;">
      <div class="label" style="color: var(--danger);">Errors in Last Cycle</div>
      <ul id="errorsList" class="errors-list"></ul>
    </div>

    <div class="grid">
      <div class="card">
        <div class="label">Status</div>
        <div id="status" class="value status idle">Idle</div>
      </div>
      <div class="card">
        <div class="label">Last Fetch</div>
        <div id="lastFetch" class="value">-</div>
      </div>
      <div class="card">
        <div class="label">Last Sync</div>
        <div id="lastSync" class="value">-</div>
      </div>
      <div class="card">
        <div class="label">Next Run</div>
        <div id="nextRun" class="value">-</div>
      </div>
      <div class="card">
        <div class="label">Interval (sec)</div>
        <input id="interval" type="number" min="10" step="10" value="300" />
      </div>
      <div class="card">
        <div class="label">Source Mode</div>
        <div id="sourceMode" class="value">-</div>
      </div>
      <div class="card">
        <div class="label">Last Error</div>
        <div id="lastError" class="value">-</div>
      </div>
      <div class="card">
        <div class="label">Customer Last Sync</div>
        <div id="lastCustomerSync" class="value">-</div>
      </div>
      <div class="card">
        <div class="label">Product Last Sync</div>
        <div id="lastProductSync" class="value">-</div>
      </div>
      <div class="card">
        <div class="label">Customer Last Fetch</div>
        <div id="lastCustomerFetch" class="value">-</div>
      </div>
      <div class="card">
        <div class="label">Product Last Fetch</div>
        <div id="lastProductFetch" class="value">-</div>
      </div>
    </div>

    <div class="section">
      <h2>Controls</h2>
      <div class="stack">
        <div class="card">
          <div class="label">Loop</div>
          <div class="stack">
            <button id="btnStart" class="btn primary" onclick="startLoop()">Start</button>
            <button id="btnStop" class="btn danger" onclick="stopLoop()">Stop</button>
            <button id="btnRestart" class="btn ghost" onclick="restartLoop()">Restart</button>
          </div>
        </div>
        <div class="card">
          <div class="label">DC Source</div>
          <div class="stack">
            <select id="sourceModeSelect">
              <option value="delivery_note">Delivery Note -> DC</option>
              <option value="sales_invoice">Sales Invoice -> DC</option>
            </select>
            <button id="btnSaveSource" class="btn" onclick="saveSourceMode()">Apply</button>
          </div>
        </div>
        <div class="card">
          <div class="label">Manual Actions (DC)</div>
          <div class="stack">
            <button id="btnFetch" class="btn" onclick="fetchNow()">Fetch Now</button>
            <button id="btnSync" class="btn" onclick="syncNow()">Sync Now</button>
          </div>
        </div>
        <div class="card">
          <div class="label">Manual Actions (Customers)</div>
          <div class="stack">
            <button id="btnFetchCustomers" class="btn" onclick="fetchCustomersNow()">Fetch Customers</button>
            <button id="btnSyncCustomers" class="btn" onclick="syncCustomersNow()">Sync Customers</button>
          </div>
        </div>
        <div class="card">
          <div class="label">Manual Actions (Products)</div>
          <div class="stack">
            <button id="btnFetchProducts" class="btn" onclick="fetchProductsNow()">Fetch Products</button>
            <button id="btnSyncProducts" class="btn" onclick="syncProductsNow()">Sync Products</button>
          </div>
        </div>
      </div>
    </div>

    <div class="section">
      <h2>Stats</h2>
      <div class="stats">
        <div class="card">
          <div class="label">Fetch</div>
          <div id="fetchStats" class="stat-line">-</div>
        </div>
        <div class="card">
          <div class="label">Sync</div>
          <div id="syncStats" class="stat-line">-</div>
        </div>
        <div class="card">
          <div class="label">Customer Fetch</div>
          <div id="customerFetchStats" class="stat-line">-</div>
        </div>
        <div class="card">
          <div class="label">Customer Sync</div>
          <div id="customerSyncStats" class="stat-line">-</div>
        </div>
        <div class="card">
          <div class="label">Product Fetch</div>
          <div id="productFetchStats" class="stat-line">-</div>
        </div>
        <div class="card">
          <div class="label">Product Sync</div>
          <div id="productSyncStats" class="stat-line">-</div>
        </div>
      </div>
    </div>

    <div class="section">
      <h2>Diagnostics</h2>
      <div class="card">
        <div class="toolbar">
          <span class="pill">Production Troubleshooting</span>
          <button id="btnAnalyze" class="btn" onclick="runDiagnostics()">Analyze Now</button>
        </div>
        <div id="diagSummary" class="stat-line" style="margin-top:10px;">-</div>
        <div style="margin-top:10px;">
          <div class="label">Top Reasons</div>
          <ul id="diagReasons" class="diag-list"><li>-</li></ul>
        </div>
        <div style="margin-top:10px;">
          <div class="label">Recommended Actions</div>
          <ul id="diagActions" class="diag-list"><li>-</li></ul>
        </div>
      </div>
    </div>

    <div class="section">
      <h2>Activity Log</h2>
      <div class="card">
        <div class="toolbar">
          <span class="pill">Recent Operations</span>
        </div>
        <div id="activityLog" class="activity-list" style="margin-top:8px;">
          <div class="stat-line">No activity yet</div>
        </div>
      </div>
    </div>

    <div class="section">
      <h2>Data</h2>
      <div class="tabs">
        <button class="tab active" data-tab="dc" onclick="switchTab('dc')">Delivery Challans</button>
        <button class="tab" data-tab="customers" onclick="switchTab('customers')">Customers</button>
        <button class="tab" data-tab="products" onclick="switchTab('products')">Products</button>
      </div>
      <div class="card">
        <div class="toolbar">
          <span class="pill">Records</span>
          <label>Status
            <select id="dataStatus">
              <option value="all">All</option>
              <option value="pending">Pending</option>
              <option value="failed">Failed</option>
              <option value="synced">Synced</option>
              <option value="deleted">Deleted</option>
            </select>
          </label>
          <label>Search <input id="dataSearch" type="text" placeholder="dc no, party, name..." /></label>
          <label>Page size <input id="dataPageSize" type="number" min="10" step="5" value="25" /></label>
          <button class="btn" onclick="refreshData(true)">Refresh</button>
          <button class="btn" onclick="bulkRetry()">Retry Failed</button>
          <button class="btn" onclick="bulkMark()">Mark Failed</button>
        </div>

        <div id="tableDc" class="table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>DC No</th>
                <th>Date</th>
                <th>Party</th>
                <th>Reference</th>
                <th>Fetched At</th>
                <th>Sync</th>
                <th>Attempts</th>
                <th>Last Error</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody id="dcTableBody"></tbody>
          </table>
        </div>

        <div id="tableCustomers" class="table-wrap hidden">
          <table class="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Fetched At</th>
                <th>Sync</th>
                <th>Attempts</th>
                <th>Last Error</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody id="customerTableBody"></tbody>
          </table>
        </div>

        <div id="tableProducts" class="table-wrap hidden">
          <table class="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Fetched At</th>
                <th>Sync</th>
                <th>Attempts</th>
                <th>Last Error</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody id="productTableBody"></tbody>
          </table>
        </div>

        <div class="pagination">
          <div id="pageInfo" class="stat-line">-</div>
          <div class="stack">
            <button id="btnPrevPage" class="btn" onclick="prevPage()">Prev</button>
            <button id="btnNextPage" class="btn" onclick="nextPage()">Next</button>
          </div>
        </div>
      </div>
    </div>

    <div id="modal" class="modal hidden" onclick="closeModal(event)">
      <div class="modal-content" onclick="event.stopPropagation()">
        <div class="modal-header">
          <strong id="modalTitle">Details</strong>
          <button class="btn small" onclick="closeModal()">Close</button>
        </div>
        <div class="modal-body" id="modalContent"></div>
      </div>
    </div>

    <div class="section">
      <h2>Logs</h2>
      <div class="toolbar">
        <span class="pill">Log Viewer</span>
        <label>Lines <input id="logLines" type="number" min="50" step="50" value="200" /></label>
        <label>Filter <input id="logFilter" type="text" placeholder="error, warning, dc_no..." /></label>
        <label><input id="autoRefresh" type="checkbox" checked /> Auto-refresh</label>
        <label><input id="autoScroll" type="checkbox" checked /> Auto-scroll</label>
        <button class="btn" onclick="refreshLogs(true)">Refresh Now</button>
        <button class="btn" onclick="copyLogs()">Copy</button>
        <button class="btn" onclick="clearLogView()">Clear View</button>
      </div>
      <div id="log"></div>
    </div>
  </div>

  <script>
    let lastRawLogs = '';
    const apiKey = new URLSearchParams(window.location.search).get('api_key') || '';
    const dataState = {
      currentTab: 'dc',
      pageSize: 25,
      pageIndex: { dc: 0, customers: 0, products: 0 },
      status: { dc: 'all', customers: 'all', products: 'all' },
      search: { dc: '', customers: '', products: '' },
      total: { dc: 0, customers: 0, products: 0 },
    };

    function apiFetch(path, options = {}) {
      const opts = options || {};
      opts.headers = opts.headers || {};
      if (apiKey) {
        opts.headers['X-API-Key'] = apiKey;
      }
      return fetch(path, opts);
    }

    function parseLocalTimestamp(ts) {
      if (!ts || ts === '-') return null;
      const parts = ts.split(' ');
      if (parts.length !== 2) return null;
      const dateParts = parts[0].split('-').map(Number);
      const timeParts = parts[1].split(':').map(Number);
      if (dateParts.length !== 3 || timeParts.length < 2) return null;
      return new Date(dateParts[0], dateParts[1] - 1, dateParts[2], timeParts[0], timeParts[1], timeParts[2] || 0);
    }

    function formatDate(d) {
      if (!d) return '-';
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }

    function updateButtons(running, busy) {
      document.getElementById('btnStart').disabled = running;
      document.getElementById('btnStop').disabled = !running;
      document.getElementById('btnRestart').disabled = !running;
      document.getElementById('sourceModeSelect').disabled = busy;
      document.getElementById('btnSaveSource').disabled = busy;
      document.getElementById('btnAnalyze').disabled = busy;
      document.getElementById('btnFetch').disabled = busy;
      document.getElementById('btnSync').disabled = busy;
      document.getElementById('btnFetchCustomers').disabled = busy;
      document.getElementById('btnSyncCustomers').disabled = busy;
      document.getElementById('btnFetchProducts').disabled = busy;
      document.getElementById('btnSyncProducts').disabled = busy;
    }

    async function refreshStatus() {
      const res = await apiFetch('/api/status');
      const data = await res.json();
      const s = data.data || {};
      const statusEl = document.getElementById('status');
      let statusText = s.status || '-';
      if (s.running && !s.busy && statusText === 'idle') {
        statusText = 'scheduled';
      }
      statusEl.innerText = statusText;
      statusEl.className = `value status ${statusText || 'idle'}`;
      document.getElementById('lastFetch').innerText = s.last_fetch_at || '-';
      document.getElementById('lastSync').innerText = s.last_sync_at || '-';
      document.getElementById('lastCustomerSync').innerText = s.last_customer_sync_at || '-';
      document.getElementById('lastProductSync').innerText = s.last_product_sync_at || '-';
      document.getElementById('lastCustomerFetch').innerText = s.last_customer_fetch_at || '-';
      document.getElementById('lastProductFetch').innerText = s.last_product_fetch_at || '-';
      const sourceDoc = s.source_doc || 'delivery_note';
      const modeLabel = sourceDoc === 'sales_invoice' ? 'Sales Invoice -> DC' : 'Delivery Note -> DC';
      document.getElementById('sourceMode').innerText = modeLabel;

      const sourceModeSelect = document.getElementById('sourceModeSelect');
      if (document.activeElement !== sourceModeSelect) {
        sourceModeSelect.value = sourceDoc;
      }

      const errEl = document.getElementById('lastError');
      if (s.last_error) {
        errEl.innerText = s.last_error;
        errEl.className = 'value error-text';
      } else {
        errEl.innerText = '-';
        errEl.className = 'value';
      }

      // Step progress bar
      const stepBar = document.getElementById('stepBar');
      if (s.current_step && s.busy) {
        stepBar.classList.remove('hidden');
        document.getElementById('stepLabel').innerText = s.current_step;
        const pct = s.step_total > 0 ? Math.round((s.step_index / s.step_total) * 100) : 0;
        document.getElementById('stepFill').style.width = pct + '%';
        document.getElementById('stepCount').innerText = `Step ${s.step_index}/${s.step_total}`;
      } else {
        stepBar.classList.add('hidden');
      }

      // Errors from last cycle
      const errorsCard = document.getElementById('errorsCard');
      const errorsList = document.getElementById('errorsList');
      const errors = s.errors || [];
      if (errors.length > 0) {
        errorsCard.classList.remove('hidden');
        errorsList.innerHTML = errors.map(e => `<li>${escapeHtml(e)}</li>`).join('');
      } else {
        errorsCard.classList.add('hidden');
      }

      // Activity log
      const activityLog = document.getElementById('activityLog');
      const activity = s.activity || [];
      if (activity.length > 0) {
        activityLog.innerHTML = activity.map(a => {
          const cls = a.result === 'ok' ? 'act-ok' : 'act-error';
          const icon = a.result === 'ok' ? '&#10003;' : '&#10007;';
          return `<div class="activity-item">
            <span class="act-time">${escapeHtml(a.time || '')}</span>
            <span class="act-action ${cls}">${icon} ${escapeHtml(a.action || '')}</span>
            <span class="act-detail">${escapeHtml(a.detail || '')}</span>
          </div>`;
        }).join('');
      } else {
        activityLog.innerHTML = '<div class="stat-line">No activity yet</div>';
      }

      const intervalInput = document.getElementById('interval');
      if (document.activeElement !== intervalInput) {
        intervalInput.value = s.interval_sec || 300;
      }

      const base = parseLocalTimestamp(s.last_sync_at || s.last_fetch_at);
      if (base) {
        const next = new Date(base.getTime() + (s.interval_sec || 300) * 1000);
        document.getElementById('nextRun').innerText = formatDate(next);
      } else {
        document.getElementById('nextRun').innerText = '-';
      }

      const fetchStats = s.fetch_stats || {};
      const syncStats = s.sync_stats || {};
      const customerFetchStats = s.customer_fetch_stats || {};
      const customerSyncStats = s.customer_sync_stats || {};
      const productFetchStats = s.product_fetch_stats || {};
      const productSyncStats = s.product_sync_stats || {};
      document.getElementById('fetchStats').innerHTML =
        `<strong>created</strong>: ${fetchStats.created ?? '-'} | <strong>updated</strong>: ${fetchStats.updated ?? '-'} | <strong>skipped</strong>: ${fetchStats.skipped ?? '-'}` +
        (fetchStats.deleted != null ? ` | <strong>deleted</strong>: ${fetchStats.deleted}` : '');
      document.getElementById('syncStats').innerHTML =
        `<strong>sent</strong>: ${syncStats.sent ?? '-'} | <strong>ok</strong>: ${syncStats.ok ?? '-'} | <strong>failed</strong>: ${syncStats.failed ?? '-'}` +
        (syncStats.delete_sent != null ? ` | <strong>del_sent</strong>: ${syncStats.delete_sent} | <strong>del_ok</strong>: ${syncStats.delete_ok ?? 0}` : '');
      document.getElementById('customerFetchStats').innerHTML =
        `<strong>created</strong>: ${customerFetchStats.created ?? '-'} | <strong>updated</strong>: ${customerFetchStats.updated ?? '-'} | <strong>skipped</strong>: ${customerFetchStats.skipped ?? '-'}` +
        (customerFetchStats.deleted != null ? ` | <strong>deleted</strong>: ${customerFetchStats.deleted}` : '');
      document.getElementById('customerSyncStats').innerHTML =
        `<strong>sent</strong>: ${customerSyncStats.sent ?? '-'} | <strong>ok</strong>: ${customerSyncStats.ok ?? '-'} | <strong>failed</strong>: ${customerSyncStats.failed ?? '-'}` +
        (customerSyncStats.delete_sent != null ? ` | <strong>del_sent</strong>: ${customerSyncStats.delete_sent} | <strong>del_ok</strong>: ${customerSyncStats.delete_ok ?? 0}` : '');
      document.getElementById('productFetchStats').innerHTML =
        `<strong>created</strong>: ${productFetchStats.created ?? '-'} | <strong>updated</strong>: ${productFetchStats.updated ?? '-'} | <strong>skipped</strong>: ${productFetchStats.skipped ?? '-'}` +
        (productFetchStats.deleted != null ? ` | <strong>deleted</strong>: ${productFetchStats.deleted}` : '');
      document.getElementById('productSyncStats').innerHTML =
        `<strong>sent</strong>: ${productSyncStats.sent ?? '-'} | <strong>ok</strong>: ${productSyncStats.ok ?? '-'} | <strong>failed</strong>: ${productSyncStats.failed ?? '-'}` +
        (productSyncStats.delete_sent != null ? ` | <strong>del_sent</strong>: ${productSyncStats.delete_sent} | <strong>del_ok</strong>: ${productSyncStats.delete_ok ?? 0}` : '');

      updateButtons(!!s.running, !!s.busy);
    }

    function renderList(elId, values, emptyText) {
      const el = document.getElementById(elId);
      if (!values || values.length === 0) {
        el.innerHTML = `<li>${escapeHtml(emptyText)}</li>`;
        return;
      }
      el.innerHTML = values.map((v) => `<li>${escapeHtml(v)}</li>`).join('');
    }

    async function runDiagnostics() {
      try {
        const res = await apiFetch('/api/diagnostics');
        const payload = await res.json();
        if (!res.ok || payload.status !== 'success') {
          document.getElementById('diagSummary').innerText = payload.message || 'Diagnostics failed';
          renderList('diagReasons', [], 'No reason data');
          renderList('diagActions', [], 'No actions available');
          return;
        }

        const data = payload.data || {};
        const env = data.environment || {};
        const connectivity = data.connectivity || {};
        const queue = data.queue || {};
        const reasons = data.top_reasons || [];
        const actions = data.recommendations || [];

        const missing = env.missing || [];
        const tallyReachable = !!(connectivity.tally && connectivity.tally.reachable);
        const apiReachable = !!(connectivity.api && connectivity.api.reachable);

        const summary = [
          `Mode: ${(data.source_doc === 'sales_invoice') ? 'Sales Invoice -> DC' : 'Delivery Note -> DC'}`,
          `Env missing: ${missing.length ? missing.join(', ') : 'none'}`,
          `Connectivity: Tally=${tallyReachable ? 'OK' : 'FAIL'}, API=${apiReachable ? 'OK' : 'FAIL'}`,
          `Queue: DC(F:${queue.dc?.failed ?? 0}/P:${queue.dc?.pending ?? 0}/D:${queue.dc?.deleted ?? 0}), Customers(F:${queue.customers?.failed ?? 0}/P:${queue.customers?.pending ?? 0}/D:${queue.customers?.deleted ?? 0}), Products(F:${queue.products?.failed ?? 0}/P:${queue.products?.pending ?? 0}/D:${queue.products?.deleted ?? 0})`,
          `Generated: ${data.generated_at || '-'}`
        ];
        document.getElementById('diagSummary').innerText = summary.join(' | ');

        const reasonLines = reasons.map((r) => `${r.reason} (${r.count})${r.sample_error ? ` e.g. ${r.sample_error}` : ''}`);
        const actionLines = actions.length ? actions : ['No immediate action needed'];
        renderList('diagReasons', reasonLines, 'No failed reason patterns found');
        renderList('diagActions', actionLines, 'No actions available');
      } catch (err) {
        document.getElementById('diagSummary').innerText = 'Diagnostics failed';
        renderList('diagReasons', [], 'No reason data');
        renderList('diagActions', [], 'No actions available');
      }
    }

    function applyLogFilter() {
      const filter = (document.getElementById('logFilter').value || '').toLowerCase();
      if (!filter) {
        document.getElementById('log').innerText = lastRawLogs;
        return;
      }
      const lines = lastRawLogs.split('\\n').filter((line) => line.toLowerCase().includes(filter));
      document.getElementById('log').innerText = lines.join('\\n');
    }

    async function refreshLogs(force=false) {
      const auto = document.getElementById('autoRefresh').checked;
      if (!auto && !force) return;
      const lines = parseInt(document.getElementById('logLines').value || '200');
      const res = await apiFetch(`/api/logs?lines=${lines}`);
      const data = await res.json();
      lastRawLogs = data.data || '';
      applyLogFilter();
      if (document.getElementById('autoScroll').checked) {
        const log = document.getElementById('log');
        log.scrollTop = log.scrollHeight;
      }
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function renderBadge(state, isDeleted) {
      if (isDeleted) {
        return `<span class="badge deleted">deleted</span>`;
      }
      const val = state || 'pending';
      return `<span class="badge ${val}">${val}</span>`;
    }

    function listEndpoint(tab) {
      if (tab === 'dc') return '/api/dc/list';
      if (tab === 'customers') return '/api/customers/list';
      return '/api/products/list';
    }

    function detailEndpoint(tab, id) {
      if (tab === 'dc') return `/api/dc/${id}`;
      if (tab === 'customers') return `/api/customers/${id}`;
      return `/api/products/${id}`;
    }

    function retryEndpoint(tab, id) {
      if (tab === 'dc') return `/api/dc/${id}/retry`;
      if (tab === 'customers') return `/api/customers/${id}/retry`;
      return `/api/products/${id}/retry`;
    }

    function markEndpoint(tab, id) {
      if (tab === 'dc') return `/api/dc/${id}/mark-unsynced`;
      if (tab === 'customers') return `/api/customers/${id}/mark-unsynced`;
      return `/api/products/${id}/mark-unsynced`;
    }

    function bulkEndpoint(tab, action) {
      if (tab === 'dc') return action === 'retry' ? '/api/dc/bulk-retry' : '/api/dc/bulk-mark';
      if (tab === 'customers') return action === 'retry' ? '/api/customers/bulk-retry' : '/api/customers/bulk-mark';
      return action === 'retry' ? '/api/products/bulk-retry' : '/api/products/bulk-mark';
    }

    function tableBodyForTab(tab) {
      if (tab === 'dc') return document.getElementById('dcTableBody');
      if (tab === 'customers') return document.getElementById('customerTableBody');
      return document.getElementById('productTableBody');
    }

    function applyStateToInputs() {
      document.getElementById('dataStatus').value = dataState.status[dataState.currentTab] || 'all';
      document.getElementById('dataSearch').value = dataState.search[dataState.currentTab] || '';
      document.getElementById('dataPageSize').value = dataState.pageSize || 25;
    }

    function applyInputsToState() {
      const statusEl = document.getElementById('dataStatus');
      const searchEl = document.getElementById('dataSearch');
      const sizeEl = document.getElementById('dataPageSize');
      dataState.status[dataState.currentTab] = statusEl.value || 'all';
      dataState.search[dataState.currentTab] = searchEl.value || '';
      const parsed = parseInt(sizeEl.value || '25');
      if (!Number.isNaN(parsed)) {
        dataState.pageSize = Math.min(Math.max(parsed, 10), 200);
      }
    }

    function switchTab(tab) {
      dataState.currentTab = tab;
      document.querySelectorAll('.tab').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.tab === tab);
      });
      document.getElementById('tableDc').classList.toggle('hidden', tab !== 'dc');
      document.getElementById('tableCustomers').classList.toggle('hidden', tab !== 'customers');
      document.getElementById('tableProducts').classList.toggle('hidden', tab !== 'products');
      applyStateToInputs();
      refreshData(true);
    }

    function renderTableMessage(tab, message) {
      const tbody = tableBodyForTab(tab);
      const colspan = tab === 'dc' ? 10 : 7;
      tbody.innerHTML = `<tr><td colspan="${colspan}">${escapeHtml(message)}</td></tr>`;
    }

    function renderDcRows(items) {
      const tbody = document.getElementById('dcTableBody');
      if (!items || items.length === 0) {
        renderTableMessage('dc', 'No delivery challans found');
        return;
      }
      tbody.innerHTML = items.map((item) => {
        const errFull = item.last_error || '-';
        const errDisplay = item.last_error_short || item.last_error || '-';
        const hintDisplay = item.error_hint || '';
        const errText = escapeHtml(errDisplay);
        const hintText = hintDisplay ? `<div class="hint-text" title="${escapeHtml(hintDisplay)}">${escapeHtml(hintDisplay)}</div>` : '';
        const errCell = errDisplay !== '-' ? `<span class="truncate" title="${escapeHtml(errFull)}">${errText}</span>${hintText}` : '-';
        const isDeleted = item.is_deleted === 1 || item.is_deleted === true;
        const rowStyle = isDeleted ? ' style="text-decoration: line-through; opacity: 0.6;"' : '';
        const actionBtns = isDeleted
          ? `<button class="btn small" onclick="viewDetail('dc', ${item.id})">View</button>`
          : `<button class="btn small" onclick="viewDetail('dc', ${item.id})">View</button>
             <button class="btn small" onclick="retryItem('dc', ${item.id})">Retry</button>
             <button class="btn small" onclick="markUnsynced('dc', ${item.id})">Mark</button>`;
        return `
          <tr${rowStyle}>
            <td>${item.id ?? '-'}</td>
            <td>${escapeHtml(item.dc_no ?? '-')}</td>
            <td>${escapeHtml(item.voucher_date ?? '-')}</td>
            <td>${escapeHtml(item.party_ledger_name ?? '-')}</td>
            <td>${escapeHtml(item.reference ?? '-')}</td>
            <td>${escapeHtml(item.updated_at ?? '-')}</td>
            <td>${renderBadge(item.sync_state, isDeleted)}</td>
            <td>${item.attempts ?? 0}</td>
            <td>${errCell}</td>
            <td>${actionBtns}</td>
          </tr>
        `;
      }).join('');
    }

    function renderSimpleRows(tab, items) {
      const tbody = tableBodyForTab(tab);
      if (!items || items.length === 0) {
        const label = tab === 'customers' ? 'customers' : 'products';
        renderTableMessage(tab, `No ${label} found`);
        return;
      }
      tbody.innerHTML = items.map((item) => {
        const errFull = item.last_error || '-';
        const errDisplay = item.last_error_short || item.last_error || '-';
        const hintDisplay = item.error_hint || '';
        const errText = escapeHtml(errDisplay);
        const hintText = hintDisplay ? `<div class="hint-text" title="${escapeHtml(hintDisplay)}">${escapeHtml(hintDisplay)}</div>` : '';
        const errCell = errDisplay !== '-' ? `<span class="truncate" title="${escapeHtml(errFull)}">${errText}</span>${hintText}` : '-';
        const isDeleted = item.is_deleted === 1 || item.is_deleted === true;
        const rowStyle = isDeleted ? ' style="text-decoration: line-through; opacity: 0.6;"' : '';
        const actionBtns = isDeleted
          ? `<button class="btn small" onclick="viewDetail('${tab}', ${item.id})">View</button>`
          : `<button class="btn small" onclick="viewDetail('${tab}', ${item.id})">View</button>
             <button class="btn small" onclick="retryItem('${tab}', ${item.id})">Retry</button>
             <button class="btn small" onclick="markUnsynced('${tab}', ${item.id})">Mark</button>`;
        return `
          <tr${rowStyle}>
            <td>${item.id ?? '-'}</td>
            <td>${escapeHtml(item.name ?? '-')}</td>
            <td>${escapeHtml(item.updated_at ?? '-')}</td>
            <td>${renderBadge(item.sync_state, isDeleted)}</td>
            <td>${item.attempts ?? 0}</td>
            <td>${errCell}</td>
            <td>${actionBtns}</td>
          </tr>
        `;
      }).join('');
    }

    function updatePagination(total, limit, offset) {
      const pageIndex = dataState.pageIndex[dataState.currentTab] || 0;
      const totalPages = Math.max(1, Math.ceil((total || 0) / limit));
      const start = total === 0 ? 0 : offset + 1;
      const end = Math.min(offset + limit, total);
      document.getElementById('pageInfo').innerText = `Showing ${start}-${end} of ${total} (Page ${pageIndex + 1}/${totalPages})`;
      document.getElementById('btnPrevPage').disabled = pageIndex <= 0;
      document.getElementById('btnNextPage').disabled = pageIndex + 1 >= totalPages;
    }

    async function refreshData(force) {
      applyInputsToState();
      const tab = dataState.currentTab;
      const limit = dataState.pageSize || 25;
      const offset = (dataState.pageIndex[tab] || 0) * limit;
      const status = encodeURIComponent(dataState.status[tab] || 'all');
      const search = encodeURIComponent(dataState.search[tab] || '');
      const url = `${listEndpoint(tab)}?status=${status}&search=${search}&limit=${limit}&offset=${offset}`;
      try {
        const res = await apiFetch(url);
        const payload = await res.json();
        if (!res.ok || payload.status !== 'success') {
          renderTableMessage(tab, payload.message || 'Failed to load data');
          updatePagination(0, limit, offset);
          return;
        }
        const data = payload.data || { total: 0, items: [] };
        dataState.total[tab] = data.total || 0;
        if (tab === 'dc') {
          renderDcRows(data.items || []);
        } else {
          renderSimpleRows(tab, data.items || []);
        }
        updatePagination(data.total || 0, limit, offset);
      } catch (err) {
        renderTableMessage(tab, 'Failed to load data');
        updatePagination(0, limit, offset);
      }
    }

    function prevPage() {
      const tab = dataState.currentTab;
      if (dataState.pageIndex[tab] > 0) {
        dataState.pageIndex[tab] -= 1;
        refreshData(true);
      }
    }

    function nextPage() {
      const tab = dataState.currentTab;
      const limit = dataState.pageSize || 25;
      const total = dataState.total[tab] || 0;
      const nextOffset = (dataState.pageIndex[tab] + 1) * limit;
      if (nextOffset < total) {
        dataState.pageIndex[tab] += 1;
        refreshData(true);
      }
    }

    async function viewDetail(tab, id) {
      try {
        const res = await apiFetch(detailEndpoint(tab, id));
        const payload = await res.json();
        if (!res.ok || payload.status !== 'success') {
          alert(payload.message || 'Failed to load details');
          return;
        }
        const title = tab === 'dc' ? `Delivery Challan #${id}` : tab === 'customers' ? `Customer #${id}` : `Product #${id}`;
        openModal(title, payload.data || {});
      } catch (err) {
        alert('Failed to load details');
      }
    }

    async function retryItem(tab, id) {
      if (!confirm('Retry sync for this record?')) return;
      try {
        const res = await apiFetch(retryEndpoint(tab, id), {method:'POST'});
        const payload = await res.json();
        if (!res.ok || payload.status !== 'success') {
          alert(payload.message || 'Retry failed');
          return;
        }
        await refreshStatus();
        await refreshData(true);
      } catch (err) {
        alert('Retry failed');
      }
    }

    async function markUnsynced(tab, id) {
      if (!confirm('Mark this record as unsynced?')) return;
      try {
        const res = await apiFetch(markEndpoint(tab, id), {method:'POST'});
        const payload = await res.json();
        if (!res.ok || payload.status !== 'success') {
          alert(payload.message || 'Update failed');
          return;
        }
        await refreshStatus();
        await refreshData(true);
      } catch (err) {
        alert('Update failed');
      }
    }

    async function bulkRetry() {
      const tab = dataState.currentTab;
      const search = encodeURIComponent(document.getElementById('dataSearch').value || '');
      const limit = 200;
      if (!confirm('Retry FAILED records (max 200)?')) return;
      try {
        const url = `${bulkEndpoint(tab, 'retry')}?status=failed&search=${search}&limit=${limit}`;
        const res = await apiFetch(url, {method:'POST'});
        const payload = await res.json();
        if (!res.ok || payload.status !== 'success') {
          alert(payload.message || 'Bulk retry failed');
          return;
        }
        const data = payload.data || {};
        alert(`Bulk retry done. matched=${data.matched || 0}, ok=${data.ok || 0}, failed=${data.failed || 0}`);
        await refreshStatus();
        await refreshData(true);
      } catch (err) {
        alert('Bulk retry failed');
      }
    }

    async function bulkMark() {
      const tab = dataState.currentTab;
      const search = encodeURIComponent(document.getElementById('dataSearch').value || '');
      const limit = 200;
      if (!confirm('Mark FAILED records as pending (max 200)?')) return;
      try {
        const url = `${bulkEndpoint(tab, 'mark')}?status=failed&search=${search}&limit=${limit}`;
        const res = await apiFetch(url, {method:'POST'});
        const payload = await res.json();
        if (!res.ok || payload.status !== 'success') {
          alert(payload.message || 'Bulk mark failed');
          return;
        }
        const data = payload.data || {};
        alert(`Marked ${data.updated || 0} records as pending.`);
        await refreshStatus();
        await refreshData(true);
      } catch (err) {
        alert('Bulk mark failed');
      }
    }

    function stringifySafe(value) {
      if (value === undefined) return '';
      try {
        const text = JSON.stringify(value, null, 2);
        return text === undefined ? '' : text;
      } catch (e) {
        return String(value);
      }
    }

    function openModal(title, data) {
      document.getElementById('modalTitle').innerText = title;
      const container = document.getElementById('modalContent');
      const record = data || {};

      const isDeleted = record.is_deleted === 1 || record.is_deleted === true;
      const meta = {
        id: record.id ?? null,
        dc_no: record.dc_no ?? null,
        name: record.name ?? null,
        party: record.party_ledger_name ?? null,
        reference: record.reference ?? null,
        is_deleted: isDeleted,
        deleted_at: record.deleted_at ?? null,
        sync_state: isDeleted ? 'deleted' : (record.sync_state ?? null),
        attempts: record.attempts ?? null,
        last_error: record.last_error ?? null,
        error_hint: record.error_hint ?? null,
        last_attempt_at: record.last_attempt_at ?? null,
        synced_at: record.synced_at ?? null,
        updated_at: record.updated_at ?? null,
      };

      const payloadText = stringifySafe(record.payload);
      const itemsText = stringifySafe(record.items);
      const responseText = stringifySafe(record.last_response_json);
      const metaText = stringifySafe(meta);

      const sections = [];
      sections.push(`
        <div class="modal-section">
          <h3>Summary</h3>
          ${metaText ? `<pre>${escapeHtml(metaText)}</pre>` : '<div class="empty">No summary data</div>'}
        </div>
      `);
      sections.push(`
        <div class="modal-section">
          <h3>Payload</h3>
          ${payloadText ? `<pre>${escapeHtml(payloadText)}</pre>` : '<div class="empty">No payload saved</div>'}
        </div>
      `);
      sections.push(`
        <div class="modal-section">
          <h3>Items</h3>
          ${itemsText ? `<pre>${escapeHtml(itemsText)}</pre>` : '<div class="empty">No items saved</div>'}
        </div>
      `);
      sections.push(`
        <div class="modal-section">
          <h3>Last Response</h3>
          ${responseText ? `<pre>${escapeHtml(responseText)}</pre>` : '<div class="empty">No response saved</div>'}
        </div>
      `);

      container.innerHTML = sections.join('');
      document.getElementById('modal').classList.remove('hidden');
    }

    function closeModal() {
      document.getElementById('modal').classList.add('hidden');
    }

    async function saveSourceMode() {
      const sourceDoc = (document.getElementById('sourceModeSelect').value || 'delivery_note');
      const res = await apiFetch('/api/source-mode', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({source_doc: sourceDoc})
      });
      const payload = await res.json();
      if (!res.ok || payload.status !== 'success') {
        alert(payload.message || 'Failed to update source mode');
        return;
      }
      await refreshStatus();
    }

    async function startLoop() {
      const interval = parseInt(document.getElementById('interval').value || '300');
      const res = await apiFetch('/api/start', {method:'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({interval_sec: interval})});
      if (!res.ok) { alert('Start failed'); }
      await refreshStatus();
    }
    async function stopLoop() {
      const res = await apiFetch('/api/stop', {method:'POST'});
      if (!res.ok) { alert('Stop failed'); }
      await refreshStatus();
    }
    async function restartLoop() {
      const res = await apiFetch('/api/restart', {method:'POST'});
      if (!res.ok) { alert('Restart failed'); }
      await refreshStatus();
    }
    async function fetchNow() {
      const res = await apiFetch('/api/fetch', {method:'POST'});
      if (!res.ok) { alert('Fetch failed (busy or error)'); }
      await refreshStatus();
    }
    async function syncNow() {
      const res = await apiFetch('/api/sync', {method:'POST'});
      if (!res.ok) { alert('Sync failed (busy or error)'); }
      await refreshStatus();
    }
    async function fetchCustomersNow() {
      const res = await apiFetch('/api/fetch-customers', {method:'POST'});
      if (!res.ok) { alert('Customer fetch failed (busy or error)'); }
      await refreshStatus();
    }
    async function syncCustomersNow() {
      const res = await apiFetch('/api/sync-customers', {method:'POST'});
      if (!res.ok) { alert('Customer sync failed (busy or error)'); }
      await refreshStatus();
    }
    async function fetchProductsNow() {
      const res = await apiFetch('/api/fetch-products', {method:'POST'});
      if (!res.ok) { alert('Product fetch failed (busy or error)'); }
      await refreshStatus();
    }
    async function syncProductsNow() {
      const res = await apiFetch('/api/sync-products', {method:'POST'});
      if (!res.ok) { alert('Product sync failed (busy or error)'); }
      await refreshStatus();
    }
    async function copyLogs() {
      try {
        await navigator.clipboard.writeText(document.getElementById('log').innerText || '');
      } catch (e) {
        alert('Copy failed');
      }
    }
    function clearLogView() {
      document.getElementById('log').innerText = '';
    }

    document.getElementById('logFilter').addEventListener('input', applyLogFilter);
    document.getElementById('dataStatus').addEventListener('change', () => {
      dataState.pageIndex[dataState.currentTab] = 0;
      refreshData(true);
    });
    document.getElementById('dataPageSize').addEventListener('change', () => {
      dataState.pageIndex[dataState.currentTab] = 0;
      refreshData(true);
    });
    let dataSearchTimer = null;
    document.getElementById('dataSearch').addEventListener('input', () => {
      if (dataSearchTimer) {
        clearTimeout(dataSearchTimer);
      }
      dataSearchTimer = setTimeout(() => {
        dataState.pageIndex[dataState.currentTab] = 0;
        refreshData(true);
      }, 350);
    });

    setInterval(refreshStatus, 3000);
    setInterval(refreshLogs, 3000);
    setInterval(() => refreshData(false), 5000);
    setInterval(runDiagnostics, 15000);
    refreshStatus();
    refreshLogs(true);
    runDiagnostics();
    switchTab('dc');
  </script>
</body>
</html>
"""


def main() -> int:
    _load_env()
    log_file = _log_file_path()
    setup_logging(
        level=cfg.get_env("LOG_LEVEL", "INFO"),
        json_output=cfg.get_env_bool("LOG_JSON", False),
        file_path=log_file,
    )

    host = cfg.get_env("UI_HOST", "0.0.0.0")
    port = int(cfg.get_env("UI_PORT", "8787"))

    interval_env = cfg.get_env_int("SYNC_INTERVAL")
    if interval_env and interval_env >= 10:
        STATE.interval_sec = interval_env

    auto_start = cfg.get_env_bool("UI_AUTO_START", False)
    if auto_start:
        _start_loop()

    server = make_server(host, port, app)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
