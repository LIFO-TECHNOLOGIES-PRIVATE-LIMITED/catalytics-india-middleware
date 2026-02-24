import argparse
from dataclasses import dataclass
import logging
from typing import Any, Dict, List, Optional
import os
import sys

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import requests

import config as cfg
import db
from logging_utils import setup_logging

DEFAULT_ENV_PATH = cfg.resolve_env_path(os.path.dirname(__file__))

logger = logging.getLogger("tally_sync_customers")


@dataclass
class SyncConfig:
    db_path: str
    api_base_url: str
    api_key: Optional[str]
    entity_id: Optional[int]
    company: Optional[str]
    batch_size: int
    limit: int
    max_attempts: int
    dry_run: bool
    log_level: str
    log_json: bool
    log_file: Optional[str]


def _fetch_unsynced(
    conn,
    *,
    company_id: Optional[int],
    limit: int,
    max_attempts: int,
) -> List[Dict[str, Any]]:
    params: List[Any] = [max_attempts]
    where_company = ""
    if company_id:
        where_company = "AND l.company_id = ?"
        params.append(company_id)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT l.*, ls.is_synced, ls.attempts, ls.payload_hash
        FROM ledgers l
        LEFT JOIN ledger_sync_status ls ON ls.ledger_id = l.id
        WHERE COALESCE(ls.is_synced, 0) = 0
          AND COALESCE(ls.attempts, 0) < ?
          AND COALESCE(l.is_deleted, 0) = 0
          {where_company}
        ORDER BY l.updated_at ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        data_json = row["data_json"]
        ledger_data = db.json_loads(data_json) if data_json else {}
        if _is_sundry_debtor(ledger_data):
            filtered.append(dict(row))
    return filtered


def _build_payload_for_ledger(
    ledger_row: Dict[str, Any],
    *,
    entity_id: Optional[int],
    company_name: Optional[str],
) -> Dict[str, Any]:
    ledger = db.json_loads(ledger_row["data_json"]) or {}
    payload: Dict[str, Any] = {"ledger": ledger}
    if entity_id:
        payload["entity_id"] = entity_id
    if company_name:
        payload["company_name"] = company_name
    return payload


def _norm_name(value: Any) -> str:
    if value is None:
        return ""
    # Collapse whitespace and normalize case
    return " ".join(str(value).split()).strip().casefold()


def _is_sundry_debtor(ledger_data: Dict[str, Any]) -> bool:
    parent = (ledger_data.get("PARENT") or ledger_data.get("PARENTNAME") or "").strip()
    return bool(parent) and parent.casefold() == "sundry debtors"


def _status_is_success(status_val: Optional[str]) -> bool:
    return status_val in ("created", "updated", "skipped")


def _response_indicates_success(response_json: Optional[Dict[str, Any]]) -> bool:
    if not response_json or response_json.get("status") != "success":
        return False
    data = response_json.get("data") or {}
    errors = data.get("errors")
    if isinstance(errors, int):
        return errors == 0
    # Fall back to presence of created/updated counts
    created = data.get("created")
    updated = data.get("updated")
    if isinstance(created, int) or isinstance(updated, int):
        return (created or 0) + (updated or 0) > 0
    return True


def _clean_error_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    msg = str(text).strip()
    lower = msg.lower()
    if "<html" in lower or "<!doctype" in lower:
        return "Non-JSON HTML response from API"
    return msg


def _update_sync_status(
    conn,
    *,
    ledger_id: int,
    success: bool,
    payload_hash: str,
    response_json: Optional[Dict[str, Any]],
    error_text: Optional[str],
) -> None:
    ts = db.now_ts()
    conn.execute(
        """
        INSERT INTO ledger_sync_status
            (ledger_id, is_synced, attempts, last_attempt_at, synced_at,
             last_error, last_response_json, payload_hash, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ledger_id) DO UPDATE SET
            is_synced = excluded.is_synced,
            attempts = ledger_sync_status.attempts + 1,
            last_attempt_at = excluded.last_attempt_at,
            synced_at = excluded.synced_at,
            last_error = excluded.last_error,
            last_response_json = excluded.last_response_json,
            payload_hash = excluded.payload_hash,
            updated_at = excluded.updated_at
        """,
        (
            ledger_id,
            1 if success else 0,
            ts,
            ts if success else None,
            error_text,
            db.json_dumps(response_json) if response_json else None,
            payload_hash,
            ts,
            ts,
        ),
    )


def _fetch_deleted_unsynced(
    conn,
    *,
    company_id: Optional[int],
    limit: int,
    max_attempts: int,
) -> List[Dict[str, Any]]:
    """Fetch ledgers that are deleted but not yet synced to Catalytics."""
    params: List[Any] = [max_attempts]
    where_company = ""
    if company_id:
        where_company = "AND l.company_id = ?"
        params.append(company_id)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT l.*, ls.is_synced, ls.attempts, ls.payload_hash
        FROM ledgers l
        LEFT JOIN ledger_sync_status ls ON ls.ledger_id = l.id
        WHERE COALESCE(l.is_deleted, 0) = 1
          AND COALESCE(ls.is_synced, 0) = 0
          AND COALESCE(ls.attempts, 0) < ?
          {where_company}
        ORDER BY l.updated_at ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        data_json = row["data_json"]
        ledger_data = db.json_loads(data_json) if data_json else {}
        if _is_sundry_debtor(ledger_data):
            filtered.append(dict(row))
    return filtered


def _sync_deleted_customers(
    conn,
    *,
    config: "SyncConfig",
    company_id: Optional[int],
    company_name: Optional[str],
) -> Dict[str, int]:
    """Sync deleted customer records to Catalytics delete endpoint."""
    deleted_ledgers = _fetch_deleted_unsynced(
        conn,
        company_id=company_id,
        limit=config.limit,
        max_attempts=config.max_attempts,
    )
    if not deleted_ledgers:
        return {"sent": 0, "ok": 0, "failed": 0}

    endpoint = config.api_base_url.rstrip("/") + "/import/tally-customer-delete/"
    headers = {}
    if config.api_key:
        headers["X-API-Key"] = config.api_key

    total_sent = 0
    total_ok = 0
    total_fail = 0

    for i in range(0, len(deleted_ledgers), config.batch_size):
        batch = deleted_ledgers[i : i + config.batch_size]
        delete_items = []
        for row in batch:
            ledger_data = db.json_loads(row["data_json"]) or {}
            guid = (
                ledger_data.get("GUID")
                or ledger_data.get("MASTERID")
                or ledger_data.get("REMOTEALTGUID")
                or ledger_data.get("REMOTEID")
                or ""
            )
            delete_items.append({"name": row["name"], "guid": str(guid).strip()})

        batch_payload: Dict[str, Any] = {"delete_customers": delete_items}
        if config.entity_id:
            batch_payload["entity_id"] = config.entity_id
        if company_name:
            batch_payload["company_name"] = company_name

        if config.dry_run:
            logger.info("Dry-run: would delete %d customers", len(delete_items))
            continue

        try:
            resp = requests.post(endpoint, json=batch_payload, headers=headers, timeout=60)
            total_sent += len(delete_items)
        except Exception as exc:
            logger.exception("Delete API request failed")
            for row in batch:
                _update_sync_status(
                    conn,
                    ledger_id=row["id"],
                    success=False,
                    payload_hash="DELETED",
                    response_json=None,
                    error_text=str(exc),
                )
            conn.commit()
            total_fail += len(batch)
            continue

        response_json = None
        try:
            response_json = resp.json()
        except Exception:
            response_json = {"status": "error", "message": f"HTTP {resp.status_code} non-JSON"}

        results = (response_json.get("data") or {}).get("results") or []
        results_map = {
            _norm_name(r.get("name")): r
            for r in results
            if isinstance(r, dict) and r.get("name")
        }

        for row in batch:
            name_key = _norm_name(row.get("name"))
            res = results_map.get(name_key) if name_key else None
            status_val = (res or {}).get("status")
            success = status_val in ("deleted", "skipped")
            error_text = None
            if not success:
                error_text = (res or {}).get("message") or response_json.get("message") or "delete_sync_failed"
            _update_sync_status(
                conn,
                ledger_id=row["id"],
                success=success,
                payload_hash="DELETED",
                response_json=response_json,
                error_text=error_text,
            )
            if success:
                total_ok += 1
            else:
                total_fail += 1

        conn.commit()

    logger.info("Customer delete sync complete. sent=%d ok=%d failed=%d", total_sent, total_ok, total_fail)
    return {"sent": total_sent, "ok": total_ok, "failed": total_fail}


def build_config(args: argparse.Namespace) -> SyncConfig:
    env_path = getattr(args, "config", None) or DEFAULT_ENV_PATH
    cfg.load_env_file(env_path)
    return SyncConfig(
        db_path=args.db_path or cfg.get_env("TALLY_DB_PATH") or "",
        api_base_url=args.api_base_url or cfg.get_env("CATALYTICS_API_BASE_URL") or "",
        api_key=args.api_key or cfg.get_env("CATALYTICS_API_KEY"),
        entity_id=args.entity_id or cfg.get_env_int("CATALYTICS_ENTITY_ID"),
        company=args.company or cfg.get_env("TALLY_COMPANY"),
        batch_size=args.batch_size or cfg.get_env_int("SYNC_BATCH_SIZE", 10) or 10,
        limit=args.limit or cfg.get_env_int("SYNC_LIMIT", 200) or 200,
        max_attempts=args.max_attempts or cfg.get_env_int("SYNC_MAX_ATTEMPTS", 5) or 5,
        dry_run=bool(args.dry_run) or cfg.get_env_bool("SYNC_DRY_RUN", False),
        log_level=args.log_level or cfg.get_env("LOG_LEVEL", "INFO"),
        log_json=bool(args.log_json) or cfg.get_env_bool("LOG_JSON", False),
        log_file=args.log_file or cfg.get_env("LOG_FILE"),
    )


def run_once(config: SyncConfig) -> Dict[str, int]:
    setup_logging(level=config.log_level, json_output=config.log_json, file_path=config.log_file)

    if not config.db_path or not config.api_base_url:
        raise ValueError("db_path and api_base_url are required")

    conn = db.connect(config.db_path)
    db.init_db(conn)

    company_id = None
    company_name = config.company
    if config.company:
        row = conn.execute(
            "SELECT id, name, entity_id FROM companies WHERE name = ?",
            (config.company,),
        ).fetchone()
        if row:
            company_id = int(row["id"])
            if not config.entity_id and row["entity_id"]:
                config.entity_id = int(row["entity_id"])
            company_name = row["name"]

    ledgers = _fetch_unsynced(conn, company_id=company_id, limit=config.limit, max_attempts=config.max_attempts)
    if not ledgers:
        logger.info("No unsynced ledgers found")
        return {"sent": 0, "ok": 0, "failed": 0}

    endpoint = config.api_base_url.rstrip("/") + "/import/tally-customer-payload/"
    headers = {}
    if config.api_key:
        headers["X-API-Key"] = config.api_key

    total_sent = 0
    total_ok = 0
    total_fail = 0

    for i in range(0, len(ledgers), config.batch_size):
        batch = ledgers[i : i + config.batch_size]
        payload_hashes: Dict[str, str] = {}
        ledger_payloads: List[Dict[str, Any]] = []

        for row in batch:
            name_key = _norm_name(row.get("name"))
            if not name_key:
                logger.warning("Skipping ledger id=%s with empty name", row.get("id"))
                continue
            try:
                payload = _build_payload_for_ledger(
                    row,
                    entity_id=config.entity_id,
                    company_name=company_name,
                )
            except Exception as exc:
                logger.exception("Failed to build payload for ledger id=%s name=%s", row.get("id"), row.get("name"))
                _update_sync_status(
                    conn,
                    ledger_id=row["id"],
                    success=False,
                    payload_hash="",
                    response_json=None,
                    error_text=f"payload_build_error: {exc}",
                )
                total_fail += 1
                continue
            ledger_data = payload["ledger"]
            ledger_payloads.append(ledger_data)
            payload_hashes[name_key] = db.sha256_text(db.json_dumps(payload))

        batch_payload = {"ledgers": ledger_payloads}
        if config.entity_id:
            batch_payload["entity_id"] = config.entity_id
        if company_name:
            batch_payload["company_name"] = company_name

        if config.dry_run:
            logger.info("Dry-run: would send %d ledgers", len(ledger_payloads))
            for row in batch:
                payload_hash = payload_hashes.get(_norm_name(row.get("name")), "")
                _update_sync_status(
                    conn,
                    ledger_id=row["id"],
                    success=False,
                    payload_hash=payload_hash,
                    response_json={"dry_run": True},
                    error_text="dry_run",
                )
            conn.commit()
            continue

        try:
            resp = requests.post(endpoint, json=batch_payload, headers=headers, timeout=60)
            total_sent += len(ledger_payloads)
        except Exception as exc:
            logger.exception("API request failed")
            for row in batch:
                payload_hash = payload_hashes.get(_norm_name(row.get("name")), "")
                _update_sync_status(
                    conn,
                    ledger_id=row["id"],
                    success=False,
                    payload_hash=payload_hash,
                    response_json=None,
                    error_text=str(exc),
                )
            conn.commit()
            total_fail += len(batch)
            continue

        response_json = None
        try:
            response_json = resp.json()
        except Exception:
            message = f"HTTP {resp.status_code} non-JSON response"
            response_json = {"status": "error", "message": message, "raw_preview": (resp.text or "")[:500]}

        results = (response_json.get("data") or {}).get("results") or []
        results_map: Dict[str, Dict[str, Any]] = {}
        for r in results:
            if not isinstance(r, dict):
                continue
            key = _norm_name(r.get("name") or r.get("ledger_name") or r.get("NAME"))
            if key:
                results_map[key] = r
        index_results: Optional[List[Dict[str, Any]]] = None
        dict_results = [r for r in results if isinstance(r, dict)]
        if dict_results and len(dict_results) == len(batch):
            index_results = dict_results

        for idx, row in enumerate(batch):
            name_key = _norm_name(row.get("name"))
            res = results_map.get(name_key) if name_key else None
            if res is None and index_results is not None:
                res = index_results[idx]
            if res is None and len(results) == 1 and len(batch) == 1:
                res = results[0] if isinstance(results[0], dict) else None
            status_val = (res or {}).get("status")
            success = _status_is_success(status_val)
            if not success and res is None and _response_indicates_success(response_json):
                # No per-item results but API reports success without errors
                success = True
            payload_hash = payload_hashes.get(name_key, "")
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
                error_text = _clean_error_text(raw_error) or "sync_failed"
            _update_sync_status(
                conn,
                ledger_id=row["id"],
                success=success,
                payload_hash=payload_hash,
                response_json=response_json,
                error_text=error_text,
            )
            if success:
                total_ok += 1
            else:
                total_fail += 1

        conn.commit()

    logger.info("Customer sync complete. sent=%d ok=%d failed=%d", total_sent, total_ok, total_fail)

    # --- Delete sync phase ---
    delete_stats = _sync_deleted_customers(
        conn,
        config=config,
        company_id=company_id,
        company_name=company_name,
    )

    return {
        "sent": total_sent,
        "ok": total_ok,
        "failed": total_fail,
        "delete_sent": delete_stats.get("sent", 0),
        "delete_ok": delete_stats.get("ok", 0),
        "delete_failed": delete_stats.get("failed", 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync SQLite-staged customers to Catalytics.")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--api-base-url", help="Catalytics base URL (e.g. http://localhost:8000)")
    parser.add_argument("--api-key", help="API key for Catalytics (X-API-Key)")
    parser.add_argument("--entity-id", type=int, help="Catalytics entity id")
    parser.add_argument("--company", help="Company name for payload fallback")
    parser.add_argument("--batch-size", type=int, default=10, help="Number of ledgers per API call")
    parser.add_argument("--limit", type=int, default=200, help="Max ledgers per run")
    parser.add_argument("--max-attempts", type=int, default=5, help="Max retry attempts per ledger")
    parser.add_argument("--dry-run", action="store_true", help="Build payloads but do not send")
    parser.add_argument("--log-level", help="Logging level")
    parser.add_argument("--log-json", action="store_true", help="JSON log output")
    parser.add_argument("--log-file", help="Log file path")
    args = parser.parse_args()

    config = build_config(args)
    try:
        run_once(config)
    except Exception:
        logger.exception("Customer sync run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
