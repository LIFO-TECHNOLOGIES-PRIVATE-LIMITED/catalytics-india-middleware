import argparse
from dataclasses import dataclass
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import os
import sys

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config as cfg
import db
import tally_api
from logging_utils import setup_logging

DEFAULT_ENV_PATH = cfg.resolve_env_path(os.path.dirname(__file__))

logger = logging.getLogger("tally_fetcher")


@dataclass
class FetchConfig:
    db_path: str
    tally_url: str
    company: str
    entity_id: Optional[int]
    from_date: Optional[str]
    to_date: Optional[str]
    days_back: Optional[int]
    fetch_stock: bool
    dry_run: bool
    log_level: str
    log_json: bool
    log_file: Optional[str]


def _normalize_dc_no(voucher: Dict[str, Any]) -> str:
    for key in (
        "VOUCHERNUMBER",
        "VOUCHERNO",
        "VCHNUMBER",
        "VCHNO",
        "NUMBER",
        "VOUCHERID",
        "VOUCHERKEY",
        "MASTERID",
        "REMOTEID",
    ):
        value = voucher.get(key)
        if value:
            return str(value).strip()
    return ""


def _extract_tally_guid(voucher: Dict[str, Any]) -> str:
    for key in (
        "GUID",
        "MASTERID",
        "REMOTEID",
        "REMOTEGUID",
        "REMOTEALTGUID",
        "VCHGUID",
        "VOUCHERGUID",
    ):
        value = voucher.get(key)
        if value:
            return str(value).strip()
    return ""


def _default_date_range(days_back: Optional[int] = None) -> Tuple[str, str]:
    now = datetime.now()
    if days_back:
        from_dt = now - timedelta(days=days_back)
        return from_dt.strftime("%Y%m%d"), now.strftime("%Y%m%d")
    if now.month >= 4:
        fy_start = datetime(now.year, 4, 1)
    else:
        fy_start = datetime(now.year - 1, 4, 1)
    return fy_start.strftime("%Y%m%d"), now.strftime("%Y%m%d")


def _build_payload_hash(
    voucher: Dict[str, Any],
    inventory_items: List[Dict[str, Any]],
    party_name: Optional[str],
    ledger_data: Optional[Dict[str, Any]],
    stock_items_map: Dict[str, Any],
) -> str:
    voucher_copy = dict(voucher)
    voucher_copy["INVENTORY"] = inventory_items
    ledger_key = party_name or (ledger_data or {}).get("NAME") or "PARTY"
    payload = {
        "voucher": voucher_copy,
        "ledgers": {ledger_key: ledger_data} if ledger_data else {},
        "stock_items": stock_items_map or {},
    }
    return db.sha256_text(db.json_dumps(payload))


def build_config(args: argparse.Namespace) -> FetchConfig:
    env_path = getattr(args, "config", None) or DEFAULT_ENV_PATH
    cfg.load_env_file(env_path)
    return FetchConfig(
        db_path=args.db_path or cfg.get_env("TALLY_DB_PATH") or "",
        tally_url=args.tally_url or cfg.get_env("TALLY_URL", "http://localhost:9000/"),
        company=args.company or cfg.get_env("TALLY_COMPANY") or "",
        entity_id=args.entity_id or cfg.get_env_int("CATALYTICS_ENTITY_ID"),
        from_date=args.from_date or cfg.get_env("TALLY_FROM_DATE"),
        to_date=args.to_date or cfg.get_env("TALLY_TO_DATE"),
        days_back=args.days_back or cfg.get_env_int("TALLY_DAYS_BACK"),
        fetch_stock=bool(args.fetch_stock) or cfg.get_env_bool("TALLY_FETCH_STOCK", False),
        dry_run=bool(args.dry_run) or cfg.get_env_bool("TALLY_DRY_RUN", False),
        log_level=args.log_level or cfg.get_env("LOG_LEVEL", "INFO"),
        log_json=bool(args.log_json) or cfg.get_env_bool("LOG_JSON", False),
        log_file=args.log_file or cfg.get_env("LOG_FILE"),
    )


def run_once(config: FetchConfig) -> Dict[str, int]:
    setup_logging(level=config.log_level, json_output=config.log_json, file_path=config.log_file)

    if not config.db_path or not config.company:
        raise ValueError("db_path and company are required")

    conn = db.connect(config.db_path)
    db.init_db(conn)

    from_date, to_date = _default_date_range(config.days_back)
    if config.from_date:
        from_date = config.from_date
    if config.to_date:
        to_date = config.to_date

    logger.info("Using date range %s to %s", from_date, to_date)
    companies = tally_api.get_companies(config.tally_url)
    available = [c.get("name") for c in companies]
    company_match = None
    for comp in companies:
        if (comp.get("name") or "").strip().lower() == config.company.strip().lower():
            company_match = comp
            break
    if not company_match:
        logger.error("Company '%s' not found in Tally. Available: %s", config.company, available)
        return {"created": 0, "updated": 0, "skipped": 0}

    company_name = company_match.get("name") or config.company
    company_id = db.ensure_company(
        conn,
        name=company_name,
        tally_name=company_name,
        entity_id=config.entity_id,
        tally_url=config.tally_url,
    )

    vouchers = tally_api.get_delivery_notes(company_name, config.tally_url, from_date, to_date)
    logger.info("Fetched %d delivery notes from Tally", len(vouchers))

    created = 0
    updated = 0
    skipped = 0
    ledgers_fetched = 0
    stock_items_fetched = 0

    for voucher in vouchers:
        tally_guid = _extract_tally_guid(voucher)
        dc_no = _normalize_dc_no(voucher)
        if not dc_no:
            skipped += 1
            continue

        existing_json = None
        if tally_guid:
            existing_by_guid = conn.execute(
                "SELECT id, dc_no, data_json FROM delivery_notes WHERE company_id = ? AND tally_guid = ?",
                (company_id, tally_guid),
            ).fetchone()
            if existing_by_guid:
                existing_json = existing_by_guid["data_json"]
                existing_dc_no = (existing_by_guid["dc_no"] or "").strip()
                if existing_dc_no and existing_dc_no != dc_no:
                    logger.warning(
                        "[GUID MATCH] DC GUID %s has db_no=%s, tally_no=%s - using db_no for update",
                        tally_guid, existing_dc_no, dc_no,
                    )
                    dc_no = existing_dc_no

        if existing_json is None:
            existing = conn.execute(
                "SELECT data_json FROM delivery_notes WHERE company_id = ? AND dc_no = ?",
                (company_id, dc_no),
            ).fetchone()
            existing_json = existing["data_json"] if existing else None

        voucher_date = voucher.get("DATE") or ""
        party_name = voucher.get("PARTYLEDGERNAME") or voucher.get("PARTYNAME") or ""
        reference = voucher.get("REFERENCE") or voucher.get("PONUMBER") or ""

        dn_id = db.upsert_delivery_note(
            conn,
            company_id=company_id,
            dc_no=dc_no,
            voucher_date=voucher_date,
            party_ledger_name=party_name,
            tally_guid=tally_guid,
            reference=reference,
            data=voucher,
        )

        inventory_items = voucher.get("INVENTORY") or []
        db.replace_delivery_note_items(conn, delivery_note_id=dn_id, items=inventory_items)

        ledger_data = None
        if party_name:
            ledger_data = tally_api.get_ledger_by_name(company_name, party_name, config.tally_url)
            if ledger_data:
                db.upsert_json_row(
                    conn,
                    table="ledgers",
                    company_id=company_id,
                    name=party_name,
                    data=ledger_data,
                )
                ledgers_fetched += 1

        stock_items_map: Dict[str, Any] = {}
        if config.fetch_stock:
            for item in inventory_items:
                stock_name = item.get("STOCKITEMNAME") or item.get("ITEMNAME") or ""
                if not stock_name or stock_name in stock_items_map:
                    continue
                stock_data = tally_api.get_stock_item_by_name(company_name, stock_name, config.tally_url)
                if stock_data:
                    stock_items_map[stock_name] = stock_data
                    db.upsert_json_row(
                        conn,
                        table="stock_items",
                        company_id=company_id,
                        name=stock_name,
                        data=stock_data,
                    )
                    stock_items_fetched += 1

        # If we didn't fetch stock items, try to use any existing cached stock data for hashing
        if not config.fetch_stock and inventory_items:
            for item in inventory_items:
                stock_name = item.get("STOCKITEMNAME") or item.get("ITEMNAME") or ""
                if not stock_name:
                    continue
                row = conn.execute(
                    "SELECT data_json FROM stock_items WHERE company_id = ? AND lower(name) = lower(?)",
                    (company_id, stock_name),
                ).fetchone()
                if row:
                    stock_items_map[stock_name] = db.json_loads(row["data_json"])

        payload_hash = _build_payload_hash(voucher, inventory_items, party_name, ledger_data, stock_items_map)
        existing_hash = None
        if dn_id:
            hash_row = conn.execute(
                "SELECT payload_hash FROM sync_status WHERE delivery_note_id = ?",
                (dn_id,),
            ).fetchone()
            existing_hash = hash_row["payload_hash"] if hash_row else None

        is_changed = (existing_json != db.json_dumps(voucher)) or (existing_hash != payload_hash)
        if existing_json is None:
            created += 1
        elif is_changed:
            updated += 1

        if is_changed or existing_hash is None:
            db.ensure_sync_status(
                conn,
                delivery_note_id=dn_id,
                is_synced=0,
                payload_hash=payload_hash,
            )

        if not config.dry_run:
            conn.commit()

    # --- Delete detection (date-range scoped) ---
    deleted = 0
    tally_dc_nos = set()
    for voucher in vouchers:
        dc_no = _normalize_dc_no(voucher)
        if dc_no:
            tally_dc_nos.add(dc_no.strip().lower())

    # Safety: only detect deletions if Tally returned >0 vouchers
    # and there are previously synced records (not a first-run scenario)
    has_synced_records = conn.execute(
        """SELECT 1 FROM sync_status ss
           JOIN delivery_notes dn ON dn.id = ss.delivery_note_id
           WHERE ss.is_synced = 1 AND dn.company_id = ? LIMIT 1""",
        (company_id,),
    ).fetchone() is not None

    if tally_dc_nos and has_synced_records:
        # Find active DCs in SQLite within the same date range
        sqlite_dcs = conn.execute(
            """SELECT id, dc_no FROM delivery_notes
               WHERE company_id = ?
                 AND COALESCE(is_deleted, 0) = 0
                 AND voucher_date IS NOT NULL
                 AND voucher_date >= ?
                 AND voucher_date <= ?""",
            (company_id, from_date, to_date),
        ).fetchall()

        dc_nos_to_delete = []
        for row in sqlite_dcs:
            if (row["dc_no"] or "").strip().lower() not in tally_dc_nos:
                dc_nos_to_delete.append(row["dc_no"])

        if dc_nos_to_delete:
            deleted = db.mark_dc_records_deleted(conn, company_id, dc_nos_to_delete)
            logger.info("Marked %d delivery notes as deleted (not in Tally response for date range %s-%s)", deleted, from_date, to_date)
            # Mark deleted records as unsynced so they get propagated
            for dc_no_del in dc_nos_to_delete:
                row = conn.execute(
                    "SELECT id FROM delivery_notes WHERE company_id = ? AND dc_no = ?",
                    (company_id, dc_no_del),
                ).fetchone()
                if row:
                    db.ensure_sync_status(
                        conn,
                        delivery_note_id=row["id"],
                        is_synced=0,
                        payload_hash="DELETED",
                    )

        if not config.dry_run:
            conn.commit()

    logger.info(
        "Done. created=%d updated=%d skipped=%d deleted=%d ledgers=%d stock_items=%d",
        created,
        updated,
        skipped,
        deleted,
        ledgers_fetched,
        stock_items_fetched,
    )
    return {"created": created, "updated": updated, "skipped": skipped, "deleted": deleted}


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Tally Delivery Notes and store in SQLite.")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--tally-url", help="Tally HTTP URL")
    parser.add_argument("--company", help="Tally company name")
    parser.add_argument("--entity-id", type=int, help="Catalytics entity id (stored for sync)")
    parser.add_argument("--from-date", help="From date YYYYMMDD")
    parser.add_argument("--to-date", help="To date YYYYMMDD")
    parser.add_argument("--days-back", type=int, help="Days back from today (overrides FY default)")
    parser.add_argument("--fetch-stock", action="store_true", help="Fetch stock item details")
    parser.add_argument("--dry-run", action="store_true", help="Do not commit changes")
    parser.add_argument("--log-level", help="Logging level")
    parser.add_argument("--log-json", action="store_true", help="JSON log output")
    parser.add_argument("--log-file", help="Log file path")
    args = parser.parse_args()

    config = build_config(args)
    try:
        run_once(config)
    except Exception:
        logger.exception("Fetch run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



