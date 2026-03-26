"""
Sync products to Catalytics using tally-product_name-payload endpoint.
Reads from 'products' table (populated by fetch_products.py).
Uses pre-parsed fields (product_master_name, variant_name, unit_name, etc.)
â€” no re-parsing needed. Matches arasan_gas sync_to_catalytics.sync_products() logic.
"""
import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import os
import sys

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import requests
import config as cfg
from config import config, BASE_DIR
from db import Database
from logging_utils import setup_logging

DEFAULT_ENV_PATH = cfg.resolve_env_path(os.path.dirname(__file__))
logger = logging.getLogger("tally_sync_products")


def _attach_file_handler():
    log_path = Path(str(BASE_DIR)) / 'logs' / 'product_sync.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'product_sync_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'product_sync_file'
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_file_handler()


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


def build_config(args: argparse.Namespace) -> SyncConfig:
    env_path = getattr(args, 'config', None) or DEFAULT_ENV_PATH
    cfg.load_env_file(env_path)
    # Use SQLITE_DB_PATH for master data (products table), not TALLY_DB_PATH (DC tables)
    db_path = cfg.config.SQLITE_DB_PATH or ""
    if db_path and not os.path.isabs(db_path):
        db_path = str(Path(ROOT_DIR) / db_path)
    return SyncConfig(
        db_path=db_path,
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


def run_once(sync_config: SyncConfig) -> Dict[str, int]:
    setup_logging(level=sync_config.log_level, json_output=sync_config.log_json, file_path=sync_config.log_file)

    if not sync_config.db_path or not sync_config.api_base_url:
        raise ValueError("db_path and api_base_url are required")

    db = Database(sync_config.db_path)
    products = db.get_unsynced_products(sync_config.limit)

    logger.info("=" * 60)
    logger.info("PRODUCT SYNC (tally-product_name-payload)")
    logger.info("=" * 60)
    logger.info(f"Found {len(products)} unsynced products")

    if not products:
        logger.info("No products to sync")
        db.close()
        return {'sent': 0, 'ok': 0, 'failed': 0}

    endpoint = sync_config.api_base_url.rstrip('/') + '/tally-product_name-payload/'
    # Payload endpoints use AllowAny permission â€” no auth header needed
    headers = {'Content-Type': 'application/json'}

    total_sent = 0
    total_ok = 0
    total_fail = 0

    for product in products:
        product_id = product['id']
        name = product['name']
        company = product['tally_company']

        try:
            logger.info(f"Syncing product '{name}' (company: {company})...")

            product_master_name = product['product_master_name']
            variant_name = product['variant_name']
            unit_name = product['unit_name']
            product_type_code = product['product_type_code']
            product_type_name = product['product_type_name']

            if not product_master_name:
                raise ValueError(
                    f"Product '{name}' has no parsed fields â€” "
                    f"re-run fetch_products.py to populate"
                )

            # Extract GUID from data_json
            guid = ''
            if product['data_json']:
                try:
                    stock_data = json.loads(product['data_json'])
                    guid = (
                        stock_data.get('GUID')
                        or stock_data.get('MASTERID')
                        or stock_data.get('REMOTEID')
                        or ''
                    )
                except Exception:
                    pass

            payload = {
                'entity_id': sync_config.entity_id,
                'stock_item_name': name,
                'product_master_name': product_master_name,
                'unit_master_name': unit_name,
                'variant_name': variant_name,
                'product_type_code': product_type_code,
                'product_type_name': product_type_name,
                'hsn_code': product['hsn_code'] or '',
                'guid': str(guid).strip(),
                'rate': product['rate'] or 0.0,
                'gst_applicable': product['gst_applicable'] or '',
                'gst_rate': product['gst_rate'] or 0.0,
                'igst_rate': product['igst_rate'] or 0.0,
                'cgst_rate': product['cgst_rate'] or 0.0,
                'sgst_rate': product['sgst_rate'] or 0.0,
                'tally_company': company,
            }

            logger.info(
                f"  Payload: product={product_master_name}, "
                f"variant={variant_name}, unit={unit_name}, "
                f"type={product_type_code}({product_type_name}), "
                f"HSN={product['hsn_code']}, GST={product['gst_rate']}%, "
                f"GUID={guid or 'N/A'}"
            )

            # Save request payload to DB before sending (for debugging/audit)
            request_json = json.dumps(payload)
            try:
                db.execute(
                    "UPDATE products SET sync_request_json = ? WHERE id = ?",
                    (request_json, product_id)
                )
            except Exception as _e:
                logger.warning(f"Failed to save sync_request_json for '{name}': {_e}")

            if sync_config.dry_run:
                logger.info(f"Dry-run: would send product '{name}'")
                db.mark_product_sync_failed(product_id, 'dry_run')
                continue

            resp = requests.post(endpoint, json=payload, headers=headers, timeout=30)
            total_sent += 1

            if resp.status_code in (200, 201):
                result = resp.json()
                response_json = json.dumps(result)

                if result.get('status') != 'success':
                    raise ValueError(f"API returned non-success: {result.get('message')}")

                data = result.get('data', {})
                created = data.get('created', 0)
                updated = data.get('updated', 0)
                errors = data.get('errors', 0)

                if errors > 0:
                    results_list = data.get('results', [{}])
                    error_msg = results_list[0].get('message', 'Unknown error') if results_list else 'Unknown error'
                    raise ValueError(f"API error: {error_msg}")

                if created == 0 and updated == 0:
                    raise ValueError("Product not created or updated")

                catalytics_id = None

                db.mark_product_synced(product_id, catalytics_id, response_json)
                total_ok += 1
                logger.info(
                    f"SUCCESS: '{name}' synced "
                    f"(SQLite ID={product_id}, GUID={guid or 'N/A'}, "
                    f"status={'created' if created else 'updated'})"
                )
            else:
                error_msg = f"HTTP {resp.status_code}"
                error_response_json = None
                try:
                    error_result = resp.json()
                    error_response_json = json.dumps(error_result)
                    error_msg = f"{error_msg} - {error_result.get('message', resp.text[:200])}"
                except Exception:
                    pass
                logger.error(f"Sync failed for '{name}': {error_msg}")
                db.mark_product_sync_failed(product_id, error_msg, error_response_json)
                total_fail += 1

        except Exception as e:
            logger.error(f"Error syncing '{name}': {e}", exc_info=True)
            db.mark_product_sync_failed(product_id, str(e))
            total_fail += 1

    logger.info("-" * 60)
    logger.info("PRODUCT SYNC SUMMARY")
    logger.info(f"Total: {total_sent + total_fail}")
    logger.info(f"OK: {total_ok}")
    logger.info(f"Failed: {total_fail}")
    logger.info("-" * 60)

    db.close()
    return {'sent': total_sent, 'ok': total_ok, 'failed': total_fail}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync products to Catalytics.")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--db-path", help="SQLite database path (unused, SQLITE_DB_PATH used instead)")
    parser.add_argument("--api-base-url", help="Catalytics base URL")
    parser.add_argument("--api-key", help="API key (X-API-Key)")
    parser.add_argument("--entity-id", type=int, help="Entity ID")
    parser.add_argument("--company", help="Company name")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level")
    parser.add_argument("--log-json", action="store_true")
    parser.add_argument("--log-file")
    args = parser.parse_args()

    sync_config = build_config(args)
    try:
        run_once(sync_config)
    except Exception:
        logger.exception("Product sync run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


