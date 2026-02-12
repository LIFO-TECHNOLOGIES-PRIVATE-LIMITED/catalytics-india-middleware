import argparse
from dataclasses import dataclass
import logging
from typing import Any, Dict, Optional
import os
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from tally_middleware import config as cfg
from tally_middleware import db
from tally_middleware import tally_api
from tally_middleware.logging_utils import setup_logging

DEFAULT_ENV_PATH = cfg.resolve_env_path(os.path.dirname(__file__))

logger = logging.getLogger("tally_fetch_customers")


@dataclass
class FetchConfig:
    db_path: str
    tally_url: str
    company: str
    entity_id: Optional[int]
    fetch_full: bool
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
        fetch_full=bool(args.fetch_full) or cfg.get_env_bool("TALLY_FETCH_FULL_CUSTOMERS", False),
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

    ledgers = tally_api.get_ledgers(company_name, config.tally_url)
    logger.info("Fetched %d ledgers from Tally", len(ledgers))

    created = 0
    updated = 0
    skipped = 0

    for ledger in ledgers:
        name = (ledger.get("NAME") or ledger.get("LEDGERNAME") or "").strip()
        if not name:
            skipped += 1
            continue

        parent = (ledger.get("PARENT") or ledger.get("PARENTNAME") or "").strip()
        ledger_data = ledger
        if config.fetch_full and (not parent or parent.strip().casefold() != "sundry debtors"):
            full = tally_api.get_ledger_by_name(company_name, name, config.tally_url)
            if full:
                ledger_data = full
                parent = (ledger_data.get("PARENT") or ledger_data.get("PARENTNAME") or "").strip()

        # Only keep Party Ledgers where Ledger Group = Sundry Debtors (Customers)
        if not parent or parent.strip().casefold() != "sundry debtors":
            skipped += 1
            continue

        existing = conn.execute(
            "SELECT data_json FROM ledgers WHERE company_id = ? AND name = ?",
            (company_id, name),
        ).fetchone()
        existing_json = existing["data_json"] if existing else None

        ledger_id = db.upsert_ledger(conn, company_id=company_id, name=name, data=ledger_data)
        payload_hash = db.sha256_text(db.json_dumps(ledger_data))

        hash_row = conn.execute(
            "SELECT payload_hash FROM ledger_sync_status WHERE ledger_id = ?",
            (ledger_id,),
        ).fetchone()
        existing_hash = hash_row["payload_hash"] if hash_row else None

        is_changed = (existing_json is None) or (existing_json != db.json_dumps(ledger_data)) or (existing_hash != payload_hash)
        if existing_json is None:
            created += 1
        elif is_changed:
            updated += 1

        if is_changed or existing_hash is None:
            db.ensure_ledger_sync_status(
                conn,
                ledger_id=ledger_id,
                is_synced=0,
                payload_hash=payload_hash,
            )

    conn.commit()
    logger.info("Done. created=%d updated=%d skipped=%d", created, updated, skipped)
    return {"created": created, "updated": updated, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Tally customers (ledgers) and store in SQLite.")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--tally-url", help="Tally HTTP URL")
    parser.add_argument("--company", help="Tally company name")
    parser.add_argument("--entity-id", type=int, help="Catalytics entity id (stored for sync)")
    parser.add_argument("--fetch-full", action="store_true", help="Fetch full ledger details per customer")
    parser.add_argument("--log-level", help="Logging level")
    parser.add_argument("--log-json", action="store_true", help="JSON log output")
    parser.add_argument("--log-file", help="Log file path")
    args = parser.parse_args()

    config = build_config(args)
    try:
        run_once(config)
    except Exception:
        logger.exception("Customer fetch run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
