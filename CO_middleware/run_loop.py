import argparse
import logging
import time
import os
import sys

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config as cfg
from fetch_invoices import build_config as build_fetch_config, run_once as fetch_once
from sync_catalytics import build_config as build_sync_config, run_once as sync_once
from logging_utils import setup_logging

DEFAULT_ENV_PATH = cfg.resolve_env_path(os.path.dirname(__file__))

logger = logging.getLogger("tally_loop")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Tally fetch and Catalytics sync in a loop.")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--mode", choices=["fetch", "sync", "both"], default="both")
    parser.add_argument("--interval", type=int, default=300, help="Interval seconds between runs")
    parser.add_argument("--once", action="store_true", help="Run once and exit")

    # Shared overrides for fetch
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--tally-url", help="Tally HTTP URL")
    parser.add_argument("--company", help="Tally company name")
    parser.add_argument("--entity-id", type=int, help="Catalytics entity id")
    parser.add_argument("--from-date", help="From date YYYYMMDD")
    parser.add_argument("--to-date", help="To date YYYYMMDD")
    parser.add_argument("--days-back", type=int, help="Days back from today")
    parser.add_argument("--fetch-stock", action="store_true", help="Fetch stock item details")

    # Shared overrides for sync
    parser.add_argument("--api-base-url", help="Catalytics base URL")
    parser.add_argument("--api-key", help="Catalytics API key")
    parser.add_argument("--batch-size", type=int, help="Batch size for sync")
    parser.add_argument("--limit", type=int, help="Max vouchers per run")
    parser.add_argument("--max-attempts", type=int, help="Max retry attempts")
    parser.add_argument("--allow-tally-fetch", action="store_true", help="Allow API to fetch Tally data")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run sync")

    # Logging
    parser.add_argument("--log-level", help="Logging level")
    parser.add_argument("--log-json", action="store_true", help="JSON log output")

    args = parser.parse_args()
    cfg.load_env_file(args.config or DEFAULT_ENV_PATH)

    setup_logging(
        level=args.log_level or cfg.get_env("LOG_LEVEL", "INFO"),
        json_output=bool(args.log_json) or cfg.get_env_bool("LOG_JSON", False),
        file_path=cfg.get_env("LOG_FILE"),
    )

    while True:
        if args.mode in ("fetch", "both"):
            fetch_config = build_fetch_config(args)
            logger.info("Starting fetch run")
            fetch_stats = fetch_once(fetch_config)
            logger.info("Fetch complete: %s", fetch_stats)

        if args.mode in ("sync", "both"):
            sync_config = build_sync_config(args)
            logger.info("Starting sync run")
            sync_stats = sync_once(sync_config)
            logger.info("Sync complete: %s", sync_stats)

        if args.once:
            break

        logger.info("Sleeping for %d seconds", args.interval)
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
