import argparse
import logging
import os
import sys
import time
import traceback

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config as cfg
from fetch_customers import build_config as build_fetch_customers_config, run_once as fetch_customers_once
from fetch_products import build_config as build_fetch_products_config, run_once as fetch_products_once
from sync_customers import build_config as build_sync_customers_config, run_once as sync_customers_once
from sync_products import build_config as build_sync_products_config, run_once as sync_products_once
from sync_catalytics import create_auto_ticket
from logging_utils import setup_logging

DEFAULT_ENV_PATH = cfg.resolve_env_path(os.path.dirname(__file__))

logger = logging.getLogger("tally_masters_loop")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Tally master fetch+sync (customers/products) in a loop.")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--mode", choices=["customers", "products", "both"], default="both")
    parser.add_argument("--interval", type=int, default=3600, help="Interval seconds between runs")
    parser.add_argument("--once", action="store_true", help="Run once and exit")

    # Shared overrides
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--tally-url", help="Tally HTTP URL")
    parser.add_argument("--company", help="Tally company name")
    parser.add_argument("--entity-id", type=int, help="Catalytics entity id")
    parser.add_argument("--fetch-full", action="store_true", help="Fetch full master details")

    parser.add_argument("--api-base-url", help="Catalytics base URL")
    parser.add_argument("--api-key", help="Catalytics API key")
    parser.add_argument("--batch-size", type=int, help="Batch size for sync")
    parser.add_argument("--limit", type=int, help="Max records per run")
    parser.add_argument("--max-attempts", type=int, help="Max retry attempts")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run sync")

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
        if args.mode in ("customers", "both"):
            try:
                fetch_config = build_fetch_customers_config(args)
                logger.info("Starting customer fetch run")
                fetch_stats = fetch_customers_once(fetch_config)
                logger.info("Customer fetch complete: %s", fetch_stats)

                sync_config = build_sync_customers_config(args)
                logger.info("Starting customer sync run")
                sync_stats = sync_customers_once(sync_config)
                logger.info("Customer sync complete: %s", sync_stats)
            except Exception as exc:
                logger.exception("Customer fetch/sync run failed")
                _sync_cfg = build_sync_customers_config(args)
                create_auto_ticket(
                    api_base_url=_sync_cfg.api_base_url,
                    subject='Customer Sync: unhandled exception in loop run',
                    description=traceback.format_exc(),
                    entity_id=_sync_cfg.entity_id,
                    priority=1,
                    category=11,
                    error_code='CUSTOMER_SYNC_CRASH',
                )

        if args.mode in ("products", "both"):
            try:
                fetch_config = build_fetch_products_config(args)
                logger.info("Starting product fetch run")
                fetch_stats = fetch_products_once(fetch_config)
                logger.info("Product fetch complete: %s", fetch_stats)

                sync_config = build_sync_products_config(args)
                logger.info("Starting product sync run")
                sync_stats = sync_products_once(sync_config)
                logger.info("Product sync complete: %s", sync_stats)
            except Exception as exc:
                logger.exception("Product fetch/sync run failed")
                _sync_cfg = build_sync_products_config(args)
                create_auto_ticket(
                    api_base_url=_sync_cfg.api_base_url,
                    subject='Product Sync: unhandled exception in loop run',
                    description=traceback.format_exc(),
                    entity_id=_sync_cfg.entity_id,
                    priority=1,
                    category=11,
                    error_code='PRODUCT_SYNC_CRASH',
                )

        if args.once:
            break

        logger.info("Sleeping for %d seconds", args.interval)
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
