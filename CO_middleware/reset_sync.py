import argparse
from dataclasses import dataclass
import logging
from typing import Optional
import os
import sys

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config as cfg
import db
from logging_utils import setup_logging

DEFAULT_ENV_PATH = cfg.resolve_env_path(os.path.dirname(__file__))

logger = logging.getLogger("tally_reset_sync")


@dataclass
class ResetConfig:
    db_path: str
    company: Optional[str]
    reset_type: str
    log_level: str
    log_json: bool
    log_file: Optional[str]


def build_config(args: argparse.Namespace) -> ResetConfig:
    env_path = getattr(args, "config", None) or DEFAULT_ENV_PATH
    cfg.load_env_file(env_path)
    return ResetConfig(
        db_path=args.db_path or cfg.get_env("TALLY_DB_PATH") or "",
        company=args.company or cfg.get_env("TALLY_COMPANY"),
        reset_type=args.reset_type or "products",
        log_level=args.log_level or cfg.get_env("LOG_LEVEL", "INFO"),
        log_json=bool(args.log_json) or cfg.get_env_bool("LOG_JSON", False),
        log_file=args.log_file or cfg.get_env("LOG_FILE"),
    )


def _reset_table(conn, *, table: str, id_col: str, company_id: Optional[int], join_table: str) -> int:
    ts = db.now_ts()
    if company_id:
        sql = f"""
        UPDATE {table}
        SET is_synced = 0,
            attempts = 0,
            last_attempt_at = NULL,
            synced_at = NULL,
            last_error = NULL,
            last_response_json = NULL,
            updated_at = ?
        WHERE {id_col} IN (SELECT id FROM {join_table} WHERE company_id = ?)
        """
        cur = conn.execute(sql, (ts, company_id))
    else:
        sql = f"""
        UPDATE {table}
        SET is_synced = 0,
            attempts = 0,
            last_attempt_at = NULL,
            synced_at = NULL,
            last_error = NULL,
            last_response_json = NULL,
            updated_at = ?
        """
        cur = conn.execute(sql, (ts,))
    return cur.rowcount


def run_once(config: ResetConfig) -> dict:
    setup_logging(level=config.log_level, json_output=config.log_json, file_path=config.log_file)

    if not config.db_path:
        raise ValueError("db_path is required")

    conn = db.connect(config.db_path)
    db.init_db(conn)

    company_id = None
    if config.company:
        row = conn.execute(
            "SELECT id FROM companies WHERE name = ?",
            (config.company,),
        ).fetchone()
        if row:
            company_id = int(row["id"])

    counts = {"customers": 0, "products": 0, "dc": 0}
    reset_type = (config.reset_type or "products").lower()

    if reset_type in ("customers", "all"):
        counts["customers"] = _reset_table(
            conn,
            table="ledger_sync_status",
            id_col="ledger_id",
            company_id=company_id,
            join_table="ledgers",
        )

    if reset_type in ("products", "all"):
        counts["products"] = _reset_table(
            conn,
            table="stock_sync_status",
            id_col="stock_item_id",
            company_id=company_id,
            join_table="stock_items",
        )

    if reset_type in ("dc", "delivery", "delivery_notes", "all"):
        counts["dc"] = _reset_table(
            conn,
            table="sync_status",
            id_col="delivery_note_id",
            company_id=company_id,
            join_table="delivery_notes",
        )

    conn.commit()
    logger.info("Reset done: %s", counts)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset sync status flags in SQLite to force re-sync.")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--company", help="Tally company name (optional)")
    parser.add_argument("--reset-type", choices=["products", "customers", "dc", "all"], default="products")
    parser.add_argument("--log-level", help="Logging level")
    parser.add_argument("--log-json", action="store_true", help="JSON log output")
    parser.add_argument("--log-file", help="Log file path")
    args = parser.parse_args()

    config = build_config(args)
    try:
        run_once(config)
    except Exception:
        logger.exception("Reset sync failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
