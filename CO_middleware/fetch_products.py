import argparse
from dataclasses import dataclass
import logging
from typing import Any, Dict, Optional
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

logger = logging.getLogger("tally_fetch_products")


@dataclass
class FetchConfig:
    db_path: str
    tally_url: str
    company: str
    entity_id: Optional[int]
    fetch_full: bool
    auto_full_if_missing_hsn: bool
    auto_full_if_missing_code: bool
    log_level: str
    log_json: bool
    log_file: Optional[str]


def build_config(args: argparse.Namespace) -> FetchConfig:
    env_path = getattr(args, "config", None) or DEFAULT_ENV_PATH
    cfg.load_env_file(env_path)
    return FetchConfig(
        db_path=args.db_path or cfg.get_env("TALLY_DB_PATH") or "",
        tally_url=args.tally_url or cfg.get_env("TALLY_URL", "http://localhost:9000/"),
        company=args.company or cfg.get_env("TALLY_COMPANY") or "",
        entity_id=args.entity_id or cfg.get_env_int("CATALYTICS_ENTITY_ID"),
        fetch_full=bool(args.fetch_full) or cfg.get_env_bool("TALLY_FETCH_FULL_PRODUCTS", False),
        auto_full_if_missing_hsn=cfg.get_env_bool("TALLY_FETCH_FULL_PRODUCTS_AUTO_HSN", True),
        auto_full_if_missing_code=cfg.get_env_bool("TALLY_FETCH_FULL_PRODUCTS_AUTO_CODE", True),
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

    stock_items = tally_api.get_stock_items(company_name, config.tally_url)
    logger.info("Fetched %d stock items from Tally", len(stock_items))

    created = 0
    updated = 0
    skipped = 0

    for item in stock_items:
        name = (item.get("NAME") or "").strip()
        if not name:
            skipped += 1
            continue

        stock_data = item
        if config.fetch_full:
            full = tally_api.get_stock_item_by_name(company_name, name, config.tally_url)
            if full:
                stock_data = full
        elif config.auto_full_if_missing_hsn or config.auto_full_if_missing_code:
            # If basic fetch does not include HSNCODE or GUID/MASTERID, upgrade to full fetch for this item.
            hsn = (item.get("HSNCODE") or "").strip()
            code_val = str(item.get("GUID") or item.get("MASTERID") or item.get("REMOTEALTGUID") or item.get("REMOTEID") or "").strip()
            if (config.auto_full_if_missing_hsn and not hsn) or (config.auto_full_if_missing_code and not code_val):
                full = tally_api.get_stock_item_by_name(company_name, name, config.tally_url)
                if full:
                    stock_data = full

        existing = conn.execute(
            "SELECT data_json FROM stock_items WHERE company_id = ? AND name = ?",
            (company_id, name),
        ).fetchone()
        existing_json = existing["data_json"] if existing else None

        stock_id = db.upsert_stock_item(conn, company_id=company_id, name=name, data=stock_data)
        payload_hash = db.sha256_text(db.json_dumps(stock_data))

        hash_row = conn.execute(
            "SELECT payload_hash FROM stock_sync_status WHERE stock_item_id = ?",
            (stock_id,),
        ).fetchone()
        existing_hash = hash_row["payload_hash"] if hash_row else None

        is_changed = (existing_json is None) or (existing_json != db.json_dumps(stock_data)) or (existing_hash != payload_hash)
        if existing_json is None:
            created += 1
        elif is_changed:
            updated += 1

        if is_changed or existing_hash is None:
            db.ensure_stock_sync_status(
                conn,
                stock_item_id=stock_id,
                is_synced=0,
                payload_hash=payload_hash,
            )

    # --- Delete detection ---
    # Collect all stock item names from Tally response
    deleted = 0
    tally_names = set()
    for item in stock_items:
        name = (item.get("NAME") or "").strip()
        if name:
            tally_names.add(name)

    # Safety: only detect deletions if Tally returned >0 items
    # and there are previously synced records (not a first-run scenario)
    has_synced_records = conn.execute(
        """SELECT 1 FROM stock_sync_status ss
           JOIN stock_items si ON si.id = ss.stock_item_id
           WHERE ss.is_synced = 1 AND si.company_id = ? LIMIT 1""",
        (company_id,),
    ).fetchone() is not None

    if tally_names and has_synced_records:
        # Find active stock items in SQLite that are NOT in the Tally response
        sqlite_items = conn.execute(
            "SELECT id, name FROM stock_items WHERE company_id = ? AND COALESCE(is_deleted, 0) = 0",
            (company_id,),
        ).fetchall()

        names_to_delete = []
        for row in sqlite_items:
            if row["name"] not in tally_names:
                names_to_delete.append(row["name"])

        if names_to_delete:
            deleted = db.mark_records_deleted(conn, "stock_items", company_id, names_to_delete)
            logger.info("Marked %d stock items as deleted (not in Tally response)", deleted)
            # Mark deleted records as unsynced so they get propagated
            for row_name in names_to_delete:
                row = conn.execute(
                    "SELECT id FROM stock_items WHERE company_id = ? AND name = ?",
                    (company_id, row_name),
                ).fetchone()
                if row:
                    db.ensure_stock_sync_status(
                        conn,
                        stock_item_id=row["id"],
                        is_synced=0,
                        payload_hash="DELETED",
                    )

    conn.commit()
    logger.info("Done. created=%d updated=%d skipped=%d deleted=%d", created, updated, skipped, deleted)
    return {"created": created, "updated": updated, "skipped": skipped, "deleted": deleted}


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Tally products (stock items) and store in SQLite.")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--tally-url", help="Tally HTTP URL")
    parser.add_argument("--company", help="Tally company name")
    parser.add_argument("--entity-id", type=int, help="Catalytics entity id (stored for sync)")
    parser.add_argument("--fetch-full", action="store_true", help="Fetch full stock item details")
    parser.add_argument("--log-level", help="Logging level")
    parser.add_argument("--log-json", action="store_true", help="JSON log output")
    parser.add_argument("--log-file", help="Log file path")
    args = parser.parse_args()

    config = build_config(args)
    try:
        run_once(config)
    except Exception:
        logger.exception("Product fetch run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
