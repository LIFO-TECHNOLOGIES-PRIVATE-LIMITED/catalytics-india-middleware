"""
Sync customers to Catalytics using tally-customer-payload endpoint.
Reads from 'customers' table (populated by fetch_customers.py).
Matches arasan_gas sync_to_catalytics.sync_customers() logic.
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
logger = logging.getLogger("tally_sync_customers")


def _attach_file_handler():
    log_path = Path(str(BASE_DIR)) / 'logs' / 'customer_sync.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'customer_sync_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'customer_sync_file'
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


def _build_ledger_from_customer(customer_row) -> Optional[Dict[str, Any]]:
    """
    Extract ledger data from customer row's data_json.
    Returns the stored Tally ledger dict (uppercase keys), or None.
    """
    data_json = customer_row['data_json']
    if not data_json:
        return None
    try:
        stored = json.loads(data_json)
        if isinstance(stored, dict) and (stored.get('NAME') or stored.get('LEDGERNAME')):
            return stored
    except Exception:
        pass
    return None


def _clean_gstin(ledger: Dict[str, Any]) -> None:
    """Strip leading colon from GSTIN fields (Tally sometimes prefixes with ':')."""
    for gst_key in ('GSTIN', 'PARTYGSTIN', 'gstin'):
        if isinstance(ledger.get(gst_key), str) and ledger[gst_key].startswith(':'):
            ledger[gst_key] = ledger[gst_key].lstrip(':')
    for gst_detail in ledger.get('LEDGSTREGDETAILS_LIST', []):
        if isinstance(gst_detail, dict):
            val = gst_detail.get('GSTIN', '')
            if isinstance(val, str) and val.startswith(':'):
                gst_detail['GSTIN'] = val.lstrip(':')


def build_config(args: argparse.Namespace) -> SyncConfig:
    env_path = getattr(args, 'config', None) or DEFAULT_ENV_PATH
    cfg.load_env_file(env_path)
    # Use SQLITE_DB_PATH for master data (customers table), not TALLY_DB_PATH (DC tables)
    db_path = cfg.get_env("SQLITE_DB_PATH") or ""
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


def run_once(config: SyncConfig) -> Dict[str, int]:
    setup_logging(level=config.log_level, json_output=config.log_json, file_path=config.log_file)

    if not config.db_path or not config.api_base_url:
        raise ValueError("db_path and api_base_url are required")

    db = Database(config.db_path)
    customers = db.get_unsynced_customers(config.limit)

    logger.info("=" * 60)
    logger.info("CUSTOMER SYNC")
    logger.info("=" * 60)
    logger.info(f"Found {len(customers)} unsynced customers")

    if not customers:
        logger.info("No customers to sync")
        db.close()
        return {'sent': 0, 'ok': 0, 'failed': 0}

    endpoint = config.api_base_url.rstrip('/') + '/tally-customer-payload/'
    headers = {'Content-Type': 'application/json'}
    if config.api_key:
        headers['X-API-Key'] = config.api_key

    total_sent = 0
    total_ok = 0
    total_fail = 0

    for customer in customers:
        customer_id = customer['id']
        name = (customer['name'] or '').strip()
        company = customer['tally_company']

        try:
            logger.info(f"Syncing customer '{name}' (company: {company})...")

            ledger = _build_ledger_from_customer(customer)

            if not ledger:
                raise ValueError(f"No ledger data in data_json for '{name}' — re-run fetch_customers.py")

            # Clean GSTIN fields
            _clean_gstin(ledger)

            # Normalize NAME
            if isinstance(ledger.get('NAME'), str):
                ledger['NAME'] = ledger['NAME'].strip()

            # Enrich ledger with all extracted fields from the customer row
            # This ensures the latest fetched data is sent even if data_json is stale
            gstin = (customer['gstin'] or '').strip()
            if gstin:
                ledger['PARTYGSTIN'] = gstin
                ledger['GSTIN'] = gstin

            phone = (customer['phone'] or '').strip()
            if phone:
                ledger['MOBILE'] = phone

            email = (customer['email'] or '').strip()
            if email:
                ledger['EMAIL'] = email

            state = (customer['state'] or '').strip()
            if state:
                ledger['STATENAME'] = state
                if not ledger.get('STATE'):
                    ledger['STATE'] = state

            pincode = (customer['pincode'] or '').strip()
            if pincode:
                ledger['PINCODE'] = pincode

            address = (customer['address'] or '').strip()
            if address:
                ledger['PRIMARY_ADDRESS'] = address
                if not ledger.get('ADDRESSES'):
                    ledger['ADDRESSES'] = [address]

            delivery_addresses_json = customer['delivery_addresses_json']
            if delivery_addresses_json:
                try:
                    delivery_addresses = json.loads(delivery_addresses_json)
                    if isinstance(delivery_addresses, list) and delivery_addresses:
                        ledger['DELIVERY_ADDRESSES'] = delivery_addresses
                except Exception:
                    pass

            tally_guid = (customer['tally_guid'] or '').strip()
            if tally_guid:
                ledger['GUID'] = tally_guid

            request_payload = {'entity_id': config.entity_id, 'ledger': ledger}
            if config.company:
                request_payload['company_name'] = config.company

            # Log enriched fields
            logger.info(
                f"  GSTIN={gstin or 'N/A'}, phone={phone or 'N/A'}, "
                f"email={email or 'N/A'}, state={state or 'N/A'}, "
                f"pincode={pincode or 'N/A'}, "
                f"delivery_addresses={len(json.loads(delivery_addresses_json)) if delivery_addresses_json else 0}"
            )

            # Save sync_request_json before sending
            request_json = json.dumps(request_payload)
            try:
                db.execute(
                    "UPDATE customers SET sync_request_json = ? WHERE id = ?",
                    (request_json, customer_id)
                )
            except Exception as _e:
                logger.warning(f"Could not save sync_request_json for '{name}': {_e}")

            if config.dry_run:
                logger.info(f"Dry-run: would send customer '{name}'")
                db.mark_customer_sync_failed(customer_id, 'dry_run')
                continue

            resp = requests.post(endpoint, json=request_payload, headers=headers, timeout=30)
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
                    if results_list and isinstance(results_list, list):
                        first = results_list[0]
                        error_type = first.get('error_type', '')
                        error_details = first.get('error_details') or first.get('message', 'Unknown error')
                        error_msg = f"{error_type}: {error_details}" if error_type else str(error_details)
                    else:
                        error_msg = 'Unknown error'
                    raise ValueError(f"API error: {error_msg}")

                if created == 0 and updated == 0:
                    raise ValueError("Customer not created or updated")

                # Extract catalytics_id from results
                catalytics_id = None
                results_list = data.get('results', [])
                if results_list and isinstance(results_list, list):
                    first = results_list[0]
                    if isinstance(first, dict):
                        catalytics_id = first.get('customer_id')

                if catalytics_id:
                    logger.info(f"  Customer ID from API: {catalytics_id}")
                else:
                    logger.warning(f"  No customer_id returned from API for '{name}'")

                db.mark_customer_synced(customer_id, catalytics_id, response_json)
                total_ok += 1
                logger.info(
                    f"SUCCESS: '{name}' synced "
                    f"(SQLite ID={customer_id}, Catalytics ID={catalytics_id}, "
                    f"GSTIN={gstin or 'N/A'}, "
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
                db.mark_customer_sync_failed(customer_id, error_msg, error_response_json)
                total_fail += 1

        except Exception as e:
            logger.error(f"Error syncing '{name}': {e}", exc_info=True)
            db.mark_customer_sync_failed(customer_id, str(e))
            total_fail += 1

    logger.info("-" * 60)
    logger.info("CUSTOMER SYNC SUMMARY")
    logger.info(f"Total: {total_sent + total_fail}")
    logger.info(f"OK: {total_ok}")
    logger.info(f"Failed: {total_fail}")
    logger.info("-" * 60)

    db.close()
    return {'sent': total_sent, 'ok': total_ok, 'failed': total_fail}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync customers to Catalytics.")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--db-path", help="SQLite database path (unused, SQLITE_DB_PATH used instead)")
    parser.add_argument("--api-base-url", help="Catalytics base URL")
    parser.add_argument("--api-key", help="API key (X-API-Key)")
    parser.add_argument("--entity-id", type=int, help="Entity ID")
    parser.add_argument("--company", help="Company name")
    parser.add_argument("--batch-size", type=int, default=10)
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
        logger.exception("Customer sync run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
