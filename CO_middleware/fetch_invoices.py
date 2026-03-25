import argparse
from dataclasses import dataclass
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import os
import sqlite3
import sys

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import json
import requests
import config as cfg
from config import BASE_DIR
import db
from db import Database as MasterDatabase
import tally_api
from logging_utils import setup_logging

DEFAULT_ENV_PATH = cfg.resolve_env_path(os.path.dirname(__file__))

logger = logging.getLogger("tally_invoice_fetcher")


def _attach_dc_fetch_file_handler():
    """Log DC fetch operations to a dedicated file."""
    log_path = BASE_DIR / 'logs' / 'dc_fetch.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'dc_fetch_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'dc_fetch_file'
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


def _attach_dc_fetch_error_handler():
    """Log DC fetch errors to a dedicated file."""
    log_path = BASE_DIR / 'logs' / 'dc_fetch_errors.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'dc_fetch_error_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'dc_fetch_error_file'
    fh.setLevel(logging.ERROR)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_dc_fetch_file_handler()
_attach_dc_fetch_error_handler()

def _ensure_dc_log_files():
    for name in ('dc_fetch.log', 'dc_fetch_errors.log'):
        try:
            log_path = BASE_DIR / 'logs' / name
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if not log_path.exists():
                log_path.touch()
        except Exception:
            pass


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
    reference_keywords: List[str]  # only fetch DCs whose reference fields contain one of these
    master_db_path: str  # SQLITE_DB_PATH â€” has customers + products tables


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
    # Use `is not None` â€” days_back=0 means "today only" which is valid and must not fall through
    if days_back is not None:
        from_dt = now - timedelta(days=days_back)
        return from_dt.strftime("%Y%m%d"), now.strftime("%Y%m%d")
    if now.month >= 4:
        fy_start = datetime(now.year, 4, 1)
    else:
        fy_start = datetime(now.year - 1, 4, 1)
    return fy_start.strftime("%Y%m%d"), now.strftime("%Y%m%d")


def _normalize_name_key(value: str) -> str:
    """Normalize a name for lookup: strip, lowercase, remove spaces."""
    return str(value or '').replace(' ', '').strip().lower()


_TANK_PRODUCT_KEY = _normalize_name_key("Tank of (Tnk)")


def _override_tank_qty(inventory_items: List[Dict[str, Any]]) -> int:
    """Force qty=1 for the Tank of (Tnk) item when Tally sends blank/invalid qty."""
    if not inventory_items:
        return 0
    changed = 0
    for item in inventory_items:
        stock_name = (item.get("STOCKITEMNAME") or item.get("ITEMNAME") or "").strip()
        if not stock_name:
            continue
        if _normalize_name_key(stock_name) == _TANK_PRODUCT_KEY:
            # Ensure numeric qty for DB + payloads.
            item["BILLEDQTY"] = "1"
            item["ACTUALQTY"] = "1"
            changed += 1
    return changed


def _parse_billedqty(qty_str: str):
    """
    Parse Tally's BILLEDQTY string into (qty, unit).

    Examples:
      "7.00 Kl"  -> ("7.00", "Kl")
      "3000 Ltr" -> ("3000", "Ltr")
      "7"        -> ("7", "")
    """
    s = (qty_str or '').strip()
    if not s:
        return '', ''
    parts = s.split(None, 1)
    qty = parts[0]
    unit = parts[1].strip() if len(parts) > 1 else ''
    return qty, unit


def _ensure_liquid_product(
    master_db_path: str,
    stock_name: str,
    item: Dict[str, Any],
    company_name: str,
    entity_id: Optional[int],
    api_base_url: str,
) -> bool:
    """
    For a DC line item whose stock name starts with 'liquid':
      1. Build canonical name from stock_name + variant (NO type-code suffix)
         e.g.  "LIQUID OXYGEN" + "3000 Ltr" -> "LIQUID OXYGEN 3000 Ltr"
      2. Check local SQLite first — if already there, return True (no API call)
      3. If not found: POST to /tally-product_name-payload/ with the same
         payload format used by sync_products.py
      4. On API success: insert into SQLite with is_synced = 1
      5. Return True on success, False on any failure
    """
    qty_str = (item.get('BILLEDQTY') or item.get('ACTUALQTY') or '').strip()
    qty, unit = _parse_billedqty(qty_str)

    if not qty:
        logger.warning(
            "[LIQUID PRODUCT] '%s' — cannot determine variant: no qty in DC item. DC will be skipped.",
            stock_name,
        )
        return False

    variant_name = f"{qty} {unit}".strip()

    # Liquid products in Tally have no type-code suffix in their name.
    # canonical_name = the name exactly as it should exist in SQLite and server.
    tank_type_code = 'TNK'
    tank_type_name = cfg.config.PRODUCT_TYPE_MAP.get(tank_type_code, 'TANK')
    canonical_name = f"{stock_name} {variant_name}"   # NO "(TNK)" suffix

    master_db = None
    try:
        master_db = MasterDatabase(master_db_path)

        # --- 1. Check local SQLite first ---
        existing = master_db.product_exists_normalized(canonical_name)
        if existing:
            logger.info(
                "[LIQUID PRODUCT] '%s' already in local DB (canonical='%s') — skipping API call",
                stock_name, canonical_name,
            )
            return True

        # --- 2. Save to SQLite first (is_synced=0) ---
        product_data = {
            'tally_guid': '',
            'name': canonical_name,
            'name_canonical': canonical_name,
            'tally_company': company_name,
            'hsn_code': '',
            'unit': unit,
            'rate': 0.0,
            'description': f'Auto-created from DC liquid product: {stock_name}',
            'data_json': json.dumps({'source': 'liquid_dc', 'original_name': stock_name}),
            'product_master_name': stock_name,
            'variant_name': variant_name,
            'unit_name': unit,
            'product_type_code': tank_type_code,
            'product_type_name': tank_type_name,
            'gst_applicable': '',
            'gst_rate': 0.0,
            'igst_rate': 0.0,
            'cgst_rate': 0.0,
            'sgst_rate': 0.0,
        }
        master_db.insert_product(product_data)
        logger.info(
            "[LIQUID PRODUCT] '%s' saved to local SQLite (canonical='%s', is_synced=0)",
            stock_name, canonical_name,
        )

        # --- 3. Sync to Catalytics server ---
        endpoint = api_base_url.rstrip('/') + '/tally-product_name-payload/'
        payload = {
            'entity_id': entity_id,
            'stock_item_name': canonical_name,        # "LIQUID OXYGEN 3000 Ltr"
            'product_master_name': stock_name,        # "LIQUID OXYGEN"
            'unit_master_name': unit,                 # "Ltr"
            'variant_name': variant_name,             # "3000 Ltr"
            'product_type_code': tank_type_code,      # "TNK"
            'product_type_name': tank_type_name,      # "TANK"
            'hsn_code': '',
            'guid': '',
            'rate': 0.0,
            'gst_applicable': '',
            'gst_rate': 0.0,
            'igst_rate': 0.0,
            'cgst_rate': 0.0,
            'sgst_rate': 0.0,
            'tally_company': company_name,
        }
        logger.info(
            "[LIQUID PRODUCT] Syncing '%s' to server — variant='%s', unit='%s', type=%s",
            stock_name, variant_name, unit, tank_type_code,
        )

        resp = requests.post(
            endpoint,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )

        prod_row = master_db.product_exists_normalized(canonical_name)

        if resp.status_code not in (200, 201):
            logger.error(
                "[LIQUID PRODUCT] Server sync failed for '%s': HTTP %d — %s. "
                "Product saved in SQLite (is_synced=0), sync_products will retry.",
                stock_name, resp.status_code, resp.text[:300],
            )
            return True   # SQLite has it — DC can proceed, sync will retry

        result = resp.json()
        if result.get('status') != 'success':
            logger.error(
                "[LIQUID PRODUCT] Server returned non-success for '%s': %s. "
                "Product saved in SQLite (is_synced=0), sync_products will retry.",
                stock_name, result.get('message'),
            )
            return True   # SQLite has it — DC can proceed, sync will retry

        data = result.get('data', {})
        if data.get('errors', 0) > 0:
            results_list = data.get('results', [{}])
            err_msg = results_list[0].get('message', 'Unknown') if results_list else 'Unknown'
            logger.error(
                "[LIQUID PRODUCT] Server error for '%s': %s. "
                "Product saved in SQLite (is_synced=0), sync_products will retry.",
                stock_name, err_msg,
            )
            return True   # SQLite has it — DC can proceed, sync will retry

        # --- 4. API success: mark as synced ---
        if prod_row:
            master_db.mark_product_synced(prod_row['id'], None, json.dumps(result))
        logger.info(
            "[LIQUID PRODUCT] '%s' synced to server and marked is_synced=1 (canonical='%s')",
            stock_name, canonical_name,
        )
        return True

    except Exception as exc:
        logger.error(
            "[LIQUID PRODUCT] Unexpected error for '%s': %s", stock_name, exc, exc_info=True,
        )
        return False
    finally:
        if master_db is not None:
            master_db.close()


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


def _parse_reference_keywords(raw: Optional[str]) -> List[str]:
    """Parse comma-separated keywords from env var, lowercase stripped."""
    if not raw:
        return []
    return [kw.strip().lower() for kw in raw.split(',') if kw.strip()]



def _extract_reference_text(voucher: Dict[str, Any]) -> str:
    """Combine reference-like fields for keyword filtering."""
    if not voucher:
        return ""
    fields = (
        "PONUMBER",
        "REFERENCE",
        "OTHERREFERENCE",
        "BASICORDERREF",
        "VOUCHERREFERENCE",
        "ORDERREF",
        "ORDERINGNO",
    )
    parts = []
    for key in fields:
        value = voucher.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts).strip().lower()


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
        days_back=args.days_back if args.days_back is not None else cfg.get_env_int("TALLY_DAYS_BACK"),
        fetch_stock=bool(args.fetch_stock) or cfg.get_env_bool("TALLY_FETCH_STOCK", False),
        dry_run=bool(args.dry_run) or cfg.get_env_bool("TALLY_DRY_RUN", False),
        log_level=args.log_level or cfg.get_env("LOG_LEVEL", "INFO"),
        log_json=bool(args.log_json) or cfg.get_env_bool("LOG_JSON", False),
        log_file=args.log_file or cfg.get_env("LOG_FILE"),
        reference_keywords=_parse_reference_keywords(
            cfg.get_env("DC_REFERENCE_KEYWORDS", "delivery,customer pickup,supplier,traders,dealers pickup")
        ),
        master_db_path=(
            str(BASE_DIR / cfg.config.SQLITE_DB_PATH)
            if cfg.config.SQLITE_DB_PATH and not os.path.isabs(cfg.config.SQLITE_DB_PATH)
            else cfg.config.SQLITE_DB_PATH or ""
        ),
    )


def run_once(config: FetchConfig) -> Dict[str, int]:
    _ensure_dc_log_files()
    setup_logging(level=config.log_level, json_output=config.log_json, file_path=config.log_file)

    if not config.db_path or not config.company:
        raise ValueError("db_path and company are required")

    conn = db.connect(config.db_path)
    db.init_db(conn)

    # Master DB (chennai4.sqlite) has customers + products tables for validation
    master_conn = None
    if config.master_db_path and os.path.exists(config.master_db_path):
        master_conn = sqlite3.connect(config.master_db_path)
        master_conn.row_factory = sqlite3.Row
    else:
        logger.warning("master_db_path not found (%s) â€” customer/product validation disabled", config.master_db_path)

    # Get company
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

    # Get date range
    if config.from_date and config.to_date:
        from_date = config.from_date
        to_date = config.to_date
        logger.info("Using user-specified date range %s to %s", from_date, to_date)
    else:
        # Use default range (days_back or financial year)
        from_date, to_date = _default_date_range(config.days_back)
        logger.info("Using default date range %s to %s (days_back=%s)", from_date, to_date, config.days_back)

    logger.info("=" * 70)
    logger.info("DC FETCH DEBUG INFO")
    logger.info("=" * 70)
    logger.info("Tally URL: %s", config.tally_url)
    logger.info("Company: %s", company_name)
    logger.info("Date Range: %s to %s", from_date, to_date)
    logger.info("Days Back: %s", config.days_back)
    logger.info("Fetch Stock: %s", config.fetch_stock)
    logger.info("=" * 70)

    # get_delivery_notes already applies Python-level date filtering internally.
    # It returns only DCs within from_date..to_date.
    vouchers = tally_api.get_delivery_notes(company_name, config.tally_url, from_date, to_date)
    logger.info("Fetched %d delivery notes (DCs) from Tally for range %s to %s", len(vouchers), from_date, to_date)
    
    if vouchers:
        logger.info("Sample DC dates:")
        for i, v in enumerate(vouchers[:5]):  # Show first 5
            logger.info("  DC #%d: %s (Date: %s, Party: %s)", 
                       i+1, 
                       v.get("VOUCHERNUMBER", "?"),
                       v.get("DATE", "?"),
                       v.get("PARTYLEDGERNAME", "?"))
    else:
        logger.info("No DCs found in date range %s to %s", from_date, to_date)

    created = 0
    updated = 0
    skipped = 0
    skipped_ref_filter = 0
    skipped_missing_customer = 0
    skipped_missing_product = 0
    ledgers_fetched = 0
    stock_items_fetched = 0

    # Per-run caches: avoid re-querying SQLite for the same name
    ledger_cache: Dict[str, bool] = {}   # normalized name -> exists in ledgers table
    stock_cache: Dict[str, bool] = {}    # normalized name -> exists in stock_items table

    for voucher_raw in vouchers:
        voucher = dict(voucher_raw)
        tally_guid = _extract_tally_guid(voucher)

        source_doc_no = _normalize_dc_no(voucher)
        dc_no = source_doc_no
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

        # Keep a copy of original Tally data for stable hash computation
        voucher_for_hash = dict(voucher)
        voucher["SOURCE_DOC_TYPE"] = "SALES_INVOICE"
        voucher["SOURCE_DOC_NO"] = source_doc_no
        voucher["VOUCHERNUMBER"] = dc_no

        if existing_json is None:
            existing = conn.execute(
                "SELECT data_json FROM delivery_notes WHERE company_id = ? AND dc_no = ?",
                (company_id, dc_no),
            ).fetchone()
            existing_json = existing["data_json"] if existing else None

        voucher_date = voucher.get("DATE") or ""
        party_name = voucher.get("PARTYLEDGERNAME") or voucher.get("PARTYNAME") or ""
        reference = voucher.get("REFERENCE") or voucher.get("PONUMBER") or ""

        # --- Liquid product pre-creation (BEFORE any validation so it always runs) ---
        inventory_items = voucher.get("INVENTORY") or []
        _api_url = cfg.get_env('CATALYTICS_API_BASE_URL', '') or ''
        if not _api_url:
            logger.warning("[LIQUID] CATALYTICS_API_BASE_URL not set — liquid product creation skipped for DC %s", dc_no)
        elif not config.master_db_path:
            logger.warning("[LIQUID] master_db_path (SQLITE_DB_PATH) not set — liquid product creation skipped for DC %s", dc_no)
        else:
            for item in inventory_items:
                _sname = (item.get("STOCKITEMNAME") or item.get("ITEMNAME") or "").strip()
                if not _sname.lower().startswith('liquid'):
                    continue

                _nkey = _normalize_name_key(_sname)
                _qty_str = (item.get('BILLEDQTY') or item.get('ACTUALQTY') or '').strip()

                logger.info(
                    "[LIQUID] DC %s | Found liquid product '%s' | BILLEDQTY='%s' | master_db='%s'",
                    dc_no, _sname, _qty_str, config.master_db_path,
                )

                if stock_cache.get(_nkey):
                    logger.info("[LIQUID] DC %s | '%s' already handled this run — skipping", dc_no, _sname)
                    continue

                _created = _ensure_liquid_product(
                    master_db_path=config.master_db_path,
                    stock_name=_sname,
                    item=item,
                    company_name=company_name,
                    entity_id=config.entity_id,
                    api_base_url=_api_url,
                )
                stock_cache[_nkey] = _created
                logger.info(
                    "[LIQUID] DC %s | '%s' result: %s",
                    dc_no, _sname, "OK" if _created else "FAILED",
                )

        # --- Reference keyword filter ---
        if config.reference_keywords:
            ref_text = _extract_reference_text(voucher)
            if not any(kw in ref_text for kw in config.reference_keywords):
                skipped += 1
                skipped_ref_filter += 1
                logger.info(
                    "[SKIP REF FILTER] DC %s (%s) - REF='%s' has no keyword match %s",
                    dc_no, party_name, ref_text or "(empty)", config.reference_keywords,
                )
                continue

        # --- Customer must exist in customers table (master DB: chennai4.sqlite) ---
        normalized_party = _normalize_name_key(party_name)
        if not normalized_party:
            logger.warning("Skipping DC %s: empty party name", dc_no)
            skipped += 1
            continue

        if master_conn is not None:
            if normalized_party not in ledger_cache:
                row = master_conn.execute(
                    "SELECT id FROM customers WHERE lower(replace(name, ' ', '')) = ?",
                    (normalized_party,),
                ).fetchone()
                ledger_cache[normalized_party] = row is not None

            if not ledger_cache[normalized_party]:
                skipped_missing_customer += 1
                logger.info(
                    "[SKIP MISSING CUSTOMER] DC %s (%s) - customer not found in master customers table",
                    dc_no, party_name,
                )
                continue

        # --- All products must exist in products table (master DB: chennai4.sqlite) ---
        tank_overrides = _override_tank_qty(inventory_items)
        if tank_overrides:
            logger.info("Adjusted qty=1 for %d item(s) Tank of (Tnk) in DC %s", tank_overrides, dc_no)

        missing_products = []
        for item in inventory_items:
            stock_name = (item.get("STOCKITEMNAME") or item.get("ITEMNAME") or "").strip()
            if not stock_name:
                missing_products.append("<empty>")
                continue
            normalized_stock = _normalize_name_key(stock_name)
            if master_conn is not None:
                if normalized_stock not in stock_cache:
                    row = master_conn.execute(
                        "SELECT id FROM products WHERE lower(replace(name, ' ', '')) = ?",
                        (normalized_stock,),
                    ).fetchone()
                    stock_cache[normalized_stock] = row is not None
            if master_conn is not None and not stock_cache.get(normalized_stock, True):
                missing_products.append(stock_name)

        if missing_products:
            skipped_missing_product += 1
            logger.info(
                "[SKIP MISSING PRODUCT] DC %s (%s) - missing products: %s",
                dc_no, party_name,
                ", ".join(sorted(set(missing_products)))[:200],
            )
            continue

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

        db.replace_delivery_note_items(conn, delivery_note_id=dn_id, items=inventory_items)

        # Only fetch full ledger/stock details from Tally if fetch_stock is enabled
        ledger_data = None
        if config.fetch_stock and party_name:
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

        payload_hash = _build_payload_hash(voucher_for_hash, inventory_items, party_name, ledger_data, stock_items_map)
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

        hash_changed = existing_hash != payload_hash
        if hash_changed or existing_hash is None:
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
    for voucher_raw in vouchers:
        final_dc_no = _normalize_dc_no(voucher_raw)
        if final_dc_no:
            tally_dc_nos.add(final_dc_no.strip().lower())

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
        "Done. created=%d updated=%d skipped=%d "
        "(ref_filter=%d missing_customer=%d missing_product=%d) "
        "deleted=%d ledgers=%d stock_items=%d",
        created, updated, skipped,
        skipped_ref_filter, skipped_missing_customer, skipped_missing_product,
        deleted, ledgers_fetched, stock_items_fetched,
    )
    
    if master_conn is not None:
        master_conn.close()

    return {"created": created, "updated": updated, "skipped": skipped, "deleted": deleted}


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Tally Sales Invoices and store in SQLite.")
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












