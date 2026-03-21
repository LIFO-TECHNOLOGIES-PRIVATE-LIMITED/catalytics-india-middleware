import argparse
from dataclasses import dataclass
import logging
from typing import Any, Dict, List, Optional, Tuple
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

logger = logging.getLogger("catalytics_sync")


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
    allow_tally_fetch: bool
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
        where_company = "AND dn.company_id = ?"
        params.append(company_id)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT dn.*, ss.is_synced, ss.attempts, ss.payload_hash
        FROM delivery_notes dn
        LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id
        WHERE COALESCE(ss.is_synced, 0) = 0
          AND COALESCE(ss.attempts, 0) < ?
          AND COALESCE(dn.is_deleted, 0) = 0
          {where_company}
        ORDER BY dn.updated_at ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_items(conn, delivery_note_id: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT data_json, stock_name FROM delivery_note_items WHERE delivery_note_id = ? ORDER BY line_no",
        (delivery_note_id,),
    ).fetchall()
    items = []
    for row in rows:
        data = db.json_loads(row["data_json"])
        if data:
            items.append(data)
    return items


def _load_ledger(conn, company_id: int, ledger_name: Optional[str]) -> Optional[Dict[str, Any]]:
    if not ledger_name:
        return None
    row = conn.execute(
        "SELECT data_json FROM ledgers WHERE company_id = ? AND lower(name) = lower(?)",
        (company_id, ledger_name),
    ).fetchone()
    return db.json_loads(row["data_json"]) if row else None


def _load_stock_items(conn, company_id: int, inventory_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    stock_map: Dict[str, Any] = {}
    for item in inventory_items:
        stock_name = item.get("STOCKITEMNAME") or item.get("ITEMNAME") or ""
        if not stock_name or stock_name in stock_map:
            continue
        row = conn.execute(
            "SELECT data_json FROM stock_items WHERE company_id = ? AND lower(name) = lower(?)",
            (company_id, stock_name),
        ).fetchone()
        if row:
            stock_map[stock_name] = db.json_loads(row["data_json"])
    return stock_map


_NON_PO_VALUES = {
    'delivery', 'invoice', 'sales', 'bill', 'challan', 'dc', 'dispatch',
    'shipment', 'yes', 'no', 'standard', 'normal', 'express',
    'not applicable', 'n/a', 'na', 'nil', 'none', '-',
    'customer pickup', 'customerpickup', 'pickup', 'self pickup', 'selfpickup', 'self',
}
_NON_PO_KEYWORDS = (
    'customer pickup', 'customerpickup', 'pickup', 'self pickup',
    'selfpickup', 'self', 'delivery', 'dispatch', 'challan',
)
_PO_FIELDS = [
    'PARTYORDERNO', 'AGGREMENTORDERNO',
    'ORDERREF', 'ORDERINGNO', 'REFNO', 'REFERENCE',
    'PONUMBER', 'BASICORDERREF', 'VOUCHERREFERENCE',
]
_PO_DATE_FIELDS = ['PARTYORDERDATE', 'AGGREMENTORDERDATE', 'ORDERDATE', 'PODATE', 'REFERENCEDATE']


def _extract_po_number(voucher: Dict[str, Any]) -> str:
    for field in _PO_FIELDS:
        value = (voucher.get(field) or "").strip()
        if not value:
            continue
        vl = value.lower()
        if vl in _NON_PO_VALUES:
            continue
        if any(k in vl for k in _NON_PO_KEYWORDS):
            continue
        if len(value) >= 1 and (any(c.isdigit() for c in value) or len(value) > 3):
            return value
    return ""


def _extract_po_date(voucher: Dict[str, Any]) -> str:
    for field in _PO_DATE_FIELDS:
        val = (voucher.get(field) or "").strip()
        if val:
            digits = ''.join(ch for ch in val if ch.isdigit())
            if len(digits) == 8:
                return digits[0:4] + '-' + digits[4:6] + '-' + digits[6:8]
            return val
    return ""


def _enrich_voucher(
    voucher: Dict[str, Any],
    note: Dict[str, Any],
    items: List[Dict[str, Any]],
) -> None:
    """Enrich voucher with fields the backend expects, matching arasan patterns."""
    # Ensure basic identity fields
    voucher.setdefault("VOUCHERNUMBER", note.get("dc_no") or "")
    voucher.setdefault("DATE", note.get("voucher_date") or "")
    voucher.setdefault("PARTYLEDGERNAME", note.get("party_ledger_name") or "")

    # FILLINGSTATION: voucher godown > item godown > env default
    if not voucher.get("FILLINGSTATION"):
        for key in ("GODOWNNAME", "LOCATIONNAME"):
            val = (voucher.get(key) or "").strip()
            if val:
                voucher["FILLINGSTATION"] = val
                break
    if not voucher.get("FILLINGSTATION"):
        for item in items:
            godown = (item.get("GODOWNNAME") or "").strip()
            if godown:
                voucher["FILLINGSTATION"] = godown
                break
    if not voucher.get("FILLINGSTATION"):
        default_fs = os.getenv("DEFAULT_FILLING_STATION", "").strip()
        if default_fs:
            voucher["FILLINGSTATION"] = default_fs

    # PO number
    po_number = _extract_po_number(voucher)
    if po_number:
        voucher["PARTYORDERNO"] = po_number
        voucher["PONUMBER"] = po_number
    else:
        voucher.pop("PARTYORDERNO", None)
        voucher.pop("PONUMBER", None)
        voucher.pop("BASICORDERREF", None)

    # PO date
    po_date = _extract_po_date(voucher) if po_number else ""
    if po_number and po_date:
        voucher["PARTYORDERDATE"] = po_date
        voucher["PODATE"] = po_date
    else:
        voucher.pop("PARTYORDERDATE", None)
        voucher.pop("PODATE", None)

    # Terms of delivery (customer pickup detection)
    other_ref = str(voucher.get("BASICORDERREF") or "").strip().lower()
    if "customer pickup" in other_ref or "pickup" in other_ref:
        voucher.setdefault("TERMSOFDELIVERY", "Customer Pickup")

    # LEDGERENTRIES fallback
    if not voucher.get("LEDGERENTRIES"):
        party = voucher.get("PARTYLEDGERNAME") or ""
        amount = voucher.get("AMOUNT") or ""
        if party:
            voucher["LEDGERENTRIES"] = [{"LEDGERNAME": party, "AMOUNT": str(amount)}]


def _build_payload_for_note(
    conn,
    note: Dict[str, Any],
    *,
    entity_id: Optional[int],
    company_name: Optional[str],
    allow_tally_fetch: bool,
) -> Tuple[Dict[str, Any], str]:
    voucher = db.json_loads(note["data_json"]) or {}
    items = _load_items(conn, note["id"])
    voucher["INVENTORY"] = items

    # Enrich voucher with all fields the backend expects
    _enrich_voucher(voucher, note, items)

    party_name = note.get("party_ledger_name") or voucher.get("PARTYLEDGERNAME") or voucher.get("PARTYNAME")
    ledger_data = _load_ledger(conn, note["company_id"], party_name)
    ledgers_map = {party_name: ledger_data} if ledger_data and party_name else {}

    stock_map = _load_stock_items(conn, note["company_id"], items)

    payload = {
        "voucher": voucher,
        "ledgers": ledgers_map,
        "stock_items": stock_map,
        "allow_tally_fetch": allow_tally_fetch,
    }

    if entity_id:
        payload["entity_id"] = entity_id
    if company_name:
        payload["company_name"] = company_name

    payload_hash = db.sha256_text(db.json_dumps(payload))
    return payload, payload_hash


def _norm_dc_no(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _update_sync_status(
    conn,
    *,
    delivery_note_id: int,
    success: bool,
    payload_hash: str,
    response_json: Optional[Dict[str, Any]],
    error_text: Optional[str],
) -> None:
    ts = db.now_ts()
    conn.execute(
        """
        INSERT INTO sync_status
            (delivery_note_id, is_synced, attempts, last_attempt_at, synced_at,
             last_error, last_response_json, payload_hash, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(delivery_note_id) DO UPDATE SET
            is_synced = excluded.is_synced,
            attempts = sync_status.attempts + 1,
            last_attempt_at = excluded.last_attempt_at,
            synced_at = excluded.synced_at,
            last_error = excluded.last_error,
            last_response_json = excluded.last_response_json,
            payload_hash = excluded.payload_hash,
            updated_at = excluded.updated_at
        """,
        (
            delivery_note_id,
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
    """Fetch delivery notes that are deleted but not yet synced to Catalytics."""
    params: List[Any] = [max_attempts]
    where_company = ""
    if company_id:
        where_company = "AND dn.company_id = ?"
        params.append(company_id)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT dn.*, ss.is_synced, ss.attempts, ss.payload_hash
        FROM delivery_notes dn
        LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id
        WHERE COALESCE(dn.is_deleted, 0) = 1
          AND COALESCE(ss.is_synced, 0) = 0
          AND COALESCE(ss.attempts, 0) < ?
          {where_company}
        ORDER BY dn.updated_at ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _sync_deleted_dcs(
    conn,
    *,
    config: "SyncConfig",
    company_id: Optional[int],
    company_name: Optional[str],
) -> Dict[str, int]:
    """Sync deleted DC records to Catalytics delete endpoint."""
    deleted_notes = _fetch_deleted_unsynced(
        conn,
        company_id=company_id,
        limit=config.limit,
        max_attempts=config.max_attempts,
    )
    if not deleted_notes:
        return {"sent": 0, "ok": 0, "failed": 0}

    endpoint = config.api_base_url.rstrip("/") + "/tally-delivery-challan-delete/"
    headers = {}
    if config.api_key:
        headers["X-API-Key"] = config.api_key

    total_sent = 0
    total_ok = 0
    total_fail = 0

    for i in range(0, len(deleted_notes), config.batch_size):
        batch = deleted_notes[i : i + config.batch_size]
        delete_items = []
        for note in batch:
            dc_no = note.get("dc_no") or ""
            delete_items.append({"dc_no": dc_no})

        batch_payload: Dict[str, Any] = {"delete_dcs": delete_items}
        if config.entity_id:
            batch_payload["entity_id"] = config.entity_id
        if company_name:
            batch_payload["company_name"] = company_name

        if config.dry_run:
            logger.info("Dry-run: would delete %d DCs", len(delete_items))
            continue

        try:
            resp = requests.post(endpoint, json=batch_payload, headers=headers, timeout=60)
            total_sent += len(delete_items)
        except Exception as exc:
            logger.exception("Delete API request failed")
            for note in batch:
                _update_sync_status(
                    conn,
                    delivery_note_id=note["id"],
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
            _norm_dc_no(r.get("dc_no")): r
            for r in results
            if isinstance(r, dict) and r.get("dc_no")
        }

        for note in batch:
            dc_no = _norm_dc_no(note.get("dc_no"))
            res = results_map.get(dc_no) if dc_no else None
            status_val = (res or {}).get("status")
            success = status_val in ("deleted", "skipped")
            error_text = None
            if not success:
                error_text = (res or {}).get("message") or response_json.get("message") or "delete_sync_failed"
            _update_sync_status(
                conn,
                delivery_note_id=note["id"],
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

    logger.info("DC delete sync complete. sent=%d ok=%d failed=%d", total_sent, total_ok, total_fail)
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
        allow_tally_fetch=bool(args.allow_tally_fetch) or cfg.get_env_bool("SYNC_ALLOW_TALLY_FETCH", False),
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
        row = conn.execute("SELECT id, name, entity_id FROM companies WHERE name = ?", (config.company,)).fetchone()
        if row:
            company_id = int(row["id"])
            if not config.entity_id and row["entity_id"]:
                config.entity_id = int(row["entity_id"])

    notes = _fetch_unsynced(conn, company_id=company_id, limit=config.limit, max_attempts=config.max_attempts)
    if not notes:
        logger.info("No unsynced delivery notes found")
        return {"sent": 0, "ok": 0, "failed": 0}

    endpoint = config.api_base_url.rstrip("/") + "/tally-delivery-challan-payload/"
    headers = {}
    if config.api_key:
        headers["X-API-Key"] = config.api_key

    total_sent = 0
    total_ok = 0
    total_fail = 0

    for i in range(0, len(notes), config.batch_size):
        batch = notes[i : i + config.batch_size]
        payload_hashes: Dict[str, str] = {}
        vouchers: List[Dict[str, Any]] = []
        ledgers_map: Dict[str, Any] = {}
        stock_map: Dict[str, Any] = {}

        for note in batch:
            try:
                payload, payload_hash = _build_payload_for_note(
                    conn,
                    note,
                    entity_id=config.entity_id,
                    company_name=company_name,
                    allow_tally_fetch=config.allow_tally_fetch,
                )
            except Exception as exc:
                logger.exception("Failed to build payload for DC id=%s dc_no=%s", note.get("id"), note.get("dc_no"))
                _update_sync_status(
                    conn,
                    delivery_note_id=note["id"],
                    success=False,
                    payload_hash="",
                    response_json=None,
                    error_text=f"payload_build_error: {exc}",
                )
                total_fail += 1
                continue
            voucher = payload["voucher"]
            vouchers.append(voucher)
            ledgers_map.update(payload.get("ledgers") or {})
            stock_map.update(payload.get("stock_items") or {})
            dc_no_raw = note.get("dc_no") or voucher.get("VOUCHERNUMBER") or ""
            dc_no = _norm_dc_no(dc_no_raw)
            if dc_no:
                payload_hashes[dc_no] = payload_hash
            logger.info(
                "  DC #%s | party=%s | date=%s | filling_station=%s | po=%s | items=%d",
                dc_no or "?",
                voucher.get("PARTYLEDGERNAME") or "?",
                voucher.get("DATE") or "?",
                voucher.get("FILLINGSTATION") or "[EMPTY]",
                voucher.get("PARTYORDERNO") or "[EMPTY]",
                len(voucher.get("INVENTORY") or []),
            )

        batch_payload = {
            "vouchers": vouchers,
            "ledgers": ledgers_map,
            "stock_items": stock_map,
            "allow_tally_fetch": config.allow_tally_fetch,
        }
        if config.entity_id:
            batch_payload["entity_id"] = config.entity_id
        if company_name:
            batch_payload["company_name"] = company_name

        if config.dry_run:
            logger.info("Dry-run: would send %d vouchers", len(vouchers))
            for note in batch:
                payload_hash = payload_hashes.get(_norm_dc_no(note.get("dc_no")), "")
                _update_sync_status(
                    conn,
                    delivery_note_id=note["id"],
                    success=False,
                    payload_hash=payload_hash,
                    response_json={"dry_run": True},
                    error_text="dry_run",
                )
            conn.commit()
            continue

        try:
            resp = requests.post(endpoint, json=batch_payload, headers=headers, timeout=60)
            total_sent += len(vouchers)
        except Exception as exc:
            logger.exception("API request failed")
            for note in batch:
                payload_hash = payload_hashes.get(_norm_dc_no(note.get("dc_no")), "")
                _update_sync_status(
                    conn,
                    delivery_note_id=note["id"],
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
            response_json = {"status": "error", "message": resp.text}

        results = (response_json.get("data") or {}).get("results") or []
        results_map = {
            _norm_dc_no(r.get("dc_no")): r
            for r in results
            if isinstance(r, dict) and r.get("dc_no") is not None
        }

        for note in batch:
            dc_no = _norm_dc_no(note.get("dc_no"))
            res = results_map.get(dc_no) if dc_no else None
            if res is None and len(results) == 1 and len(batch) == 1:
                res = results[0]
            status_val = (res or {}).get("status")
            success = status_val in ("created", "updated")
            payload_hash = payload_hashes.get(dc_no, "")
            error_text = None
            if not success:
                error_text = (res or {}).get("message") or response_json.get("message") or "sync_failed"
            _update_sync_status(
                conn,
                delivery_note_id=note["id"],
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

    logger.info(
        "Sync complete. sent=%d ok=%d failed=%d",
        total_sent,
        total_ok,
        total_fail,
    )

    # --- Delete sync phase ---
    delete_stats = _sync_deleted_dcs(
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
    parser = argparse.ArgumentParser(description="Sync SQLite-staged DC payloads to Catalytics.")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--api-base-url", help="Catalytics base URL (e.g. http://localhost:8000)")
    parser.add_argument("--api-key", help="API key for Catalytics (X-API-Key)")
    parser.add_argument("--entity-id", type=int, help="Catalytics entity id")
    parser.add_argument("--company", help="Company name for payload fallback")
    parser.add_argument("--batch-size", type=int, default=10, help="Number of vouchers per API call")
    parser.add_argument("--limit", type=int, default=200, help="Max vouchers per run")
    parser.add_argument("--max-attempts", type=int, default=5, help="Max retry attempts per DC")
    parser.add_argument("--allow-tally-fetch", action="store_true", help="Allow API to fetch Tally data")
    parser.add_argument("--dry-run", action="store_true", help="Build payloads but do not send")
    parser.add_argument("--log-level", help="Logging level")
    parser.add_argument("--log-json", action="store_true", help="JSON log output")
    parser.add_argument("--log-file", help="Log file path")
    args = parser.parse_args()

    config = build_config(args)
    try:
        run_once(config)
    except Exception:
        logger.exception("Sync run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
