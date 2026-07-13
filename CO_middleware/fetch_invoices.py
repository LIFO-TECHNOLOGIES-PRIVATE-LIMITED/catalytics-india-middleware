import argparse
from dataclasses import dataclass
import logging
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
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
from fetch_products import parse_stock_item_name
from sync_catalytics import create_auto_ticket

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
    master_db_path: str  # SQLITE_DB_PATH â€" has customers + products tables


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


DEFAULT_DC_PAST_DAYS = 3
DEFAULT_DC_FUTURE_DAYS = 1


def _default_date_range(days_back: Optional[int] = None) -> Tuple[str, str]:
    now = datetime.now()
    # DC_PAST_DAYS / DC_FUTURE_DAYS control the fetch window.
    # TALLY_DAYS_BACK (days_back param) overrides DC_PAST_DAYS if set.
    from config import get_env_int
    if days_back is not None and days_back > 0:
        past = days_back
    else:
        past = get_env_int("DC_PAST_DAYS", DEFAULT_DC_PAST_DAYS)
    future = get_env_int("DC_FUTURE_DAYS", DEFAULT_DC_FUTURE_DAYS)
    from_dt = now - timedelta(days=past)
    to_dt = now + timedelta(days=future)
    return from_dt.strftime("%Y%m%d"), to_dt.strftime("%Y%m%d")

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


import re as _re

_LIQUID_NAME_VARIANT_RE = _re.compile(
    r'^(.*?)\s+(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*(?:\(TNK\))?\s*$',
    _re.IGNORECASE,
)

# Words at the end of liquid product names that are NOT units but part of the product name
# e.g. "LIQUID NITROGEN TANK" → "TANK" is part of name, not a unit
_LIQUID_TRAILING_WORDS = {'TANK', 'TNK', 'TANKER', 'CYLINDER', 'CYL', 'CONTAINER', 'CON'}

# Pattern for names like "LIQUID NITROGEN TANK" or "LIQUID OXYGEN TANK (TNK)"
_LIQUID_NAME_TRAILING_RE = _re.compile(
    r'^(.*?)\s+(TANK|TNK|TANKER|CYLINDER|CYL|CONTAINER|CON)\s*(?:\(TNK\))?\s*$',
    _re.IGNORECASE,
)


def _parse_liquid_name_variant(stock_name: str):
    """
    Try to extract variant from a liquid product name like:
      "LIQUID NITROGEN 130 LIT"       → base="LIQUID NITROGEN", qty="130", unit="LIT"
      "LIQUID NITROGEN 130 LIT (TNK)" → base="LIQUID NITROGEN", qty="130", unit="LIT"
      "LIQUID NITROGEN TANK"           → base="LIQUID NITROGEN", qty=None, unit=None
      "LIQUID NITROGEN TANK (TNK)"     → base="LIQUID NITROGEN", qty=None, unit=None
    Returns (base_name, qty, unit) or (stock_name, None, None) if no variant found.
    """
    name = stock_name.strip()

    # First check if name ends with a trailing word like TANK -- strip it to get base name
    # but return no variant (caller will use BILLEDQTY)
    m_trail = _LIQUID_NAME_TRAILING_RE.match(name)
    if m_trail:
        base = m_trail.group(1).strip().upper()
        logger.info("[LIQUID PARSE] '%s' → trailing word '%s' stripped, base='%s' (variant from BILLEDQTY)",
                    stock_name, m_trail.group(2), base)
        return base, None, None

    # Standard pattern: name has numeric variant
    m = _LIQUID_NAME_VARIANT_RE.match(name)
    if m:
        base = m.group(1).strip().upper()
        qty = m.group(2)
        unit = m.group(3).strip().upper()
        # Guard: if the "unit" is actually a trailing word, treat as no variant
        if unit in _LIQUID_TRAILING_WORDS:
            logger.info("[LIQUID PARSE] '%s' → '%s' is a trailing word not a unit, base='%s' (variant from BILLEDQTY)",
                        stock_name, unit, base)
            return base, None, None
        return base, qty, unit

    return stock_name, None, None


def _ensure_liquid_product(
    master_db_path: str,
    stock_name: str,
    item: Dict[str, Any],
    original_qty: str,
    company_name: str,
    entity_id: Optional[int],
    api_base_url: str,
    master_db: Optional['MasterDatabase'] = None,
) -> bool:
    """
    For a DC line item whose stock name starts with 'liquid':
      1. If stock_name already contains variant (e.g. "LIQUID NITROGEN 130 LIT (TNK)"),
         extract base name + variant from the name itself.
         Otherwise fall back to BILLEDQTY for variant.
      2. Always force item BILLEDQTY/ACTUALQTY = 1 (tank is a physical asset).
      3. Check local SQLite first -- if already there, return True (no API call)
      4. If not found: INSERT to SQLite (is_synced=0) then POST to /tally-product_name-payload/
      5. On API success: mark is_synced=1
      6. Return True on success (or if product in SQLite), False on hard failure
    """
    # Normalize stock_name to uppercase -- Tally may send mixed/lower case
    stock_name = stock_name.strip().upper()

    # Save original qty before overwriting -- needed for fallback variant lookup.
    # Use caller-provided original DC qty first (captured before any qty=1 override).
    _orig_billedqty = (original_qty or item.get('BILLEDQTY') or item.get('ACTUALQTY') or '').strip()

    # Always set DC item qty to 1 -- tank product, mandatory
    item['BILLEDQTY'] = '1'
    item['ACTUALQTY'] = '1'

    # Try to get variant from the product name itself
    base_name, name_qty, name_unit = _parse_liquid_name_variant(stock_name)
    if name_qty:
        qty = name_qty
        unit = name_unit
        stock_name = base_name  # use base name (without variant) as product master name
    else:
        # Plain name (e.g. "LIQUID NITROGEN") -- get variant from original BILLEDQTY
        qty, unit = _parse_billedqty(_orig_billedqty)

    if not qty:
        logger.warning(
            "[LIQUID PRODUCT] '%s' -- cannot determine variant: no qty in DC item. DC will be skipped.",
            stock_name,
        )
        return False

    unit = unit.upper()
    variant_name = f"{qty} {unit}".strip()

    tank_type_code = 'TNK'
    tank_type_name = cfg.config.PRODUCT_TYPE_MAP.get(tank_type_code, 'TANK')
    # Canonical name includes (TNK) suffix
    canonical_name = f"{stock_name} {variant_name} (TNK)"

    logger.info(
        "[PARSED] '%s' -> product=%s, variant=%s, unit=%s, type=%s(%s), canonical='%s'",
        stock_name, stock_name, variant_name, unit, tank_type_code, tank_type_name, canonical_name,
    )

    _own_db = False
    try:
        if master_db is None:
            master_db = MasterDatabase(master_db_path)
            _own_db = True

        # --- 1. Check local SQLite first ---
        existing = master_db.product_exists_normalized(canonical_name)
        if existing:
            logger.info(
                "[LIQUID PRODUCT] '%s' already in local DB (canonical='%s') -- skipping API call",
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
            "[NEW PRODUCT] '%s' saved (company: %s, product=%s, variant=%s, type=%s, GUID=N/A, HSN=)",
            canonical_name, company_name, stock_name, variant_name, tank_type_name,
        )

        # --- 3. Sync to Catalytics server ---
        _base = api_base_url.rstrip('/')
        endpoint = (_base + '/tally-product_name-payload/') if _base.endswith('/import') else (_base + '/import/tally-product_name-payload/')
        payload = {
            'entity_id': entity_id,
            'stock_item_name': canonical_name,
            'product_master_name': stock_name,
            'unit_master_name': unit,
            'variant_name': variant_name,
            'product_type_code': tank_type_code,
            'product_type_name': tank_type_name,
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
            "[LIQUID PRODUCT] Syncing '%s' to server (variant=%s, unit=%s, type=%s)",
            canonical_name, variant_name, unit, tank_type_code,
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
                "[LIQUID PRODUCT] Server sync failed for '%s': HTTP %d -- %s. "
                "Product saved in SQLite (is_synced=0), sync_products will retry.",
                canonical_name, resp.status_code, resp.text[:300],
            )
            return True   # SQLite has it -- DC can proceed, sync will retry

        result = resp.json()
        if result.get('status') != 'success':
            logger.error(
                "[LIQUID PRODUCT] Server returned non-success for '%s': %s. "
                "Product saved in SQLite (is_synced=0), sync_products will retry.",
                canonical_name, result.get('message'),
            )
            return True   # SQLite has it -- DC can proceed, sync will retry

        data = result.get('data', {})
        if data.get('errors', 0) > 0:
            results_list = data.get('results', [{}])
            err_msg = results_list[0].get('message', 'Unknown') if results_list else 'Unknown'
            logger.error(
                "[LIQUID PRODUCT] Server error for '%s': %s. "
                "Product saved in SQLite (is_synced=0), sync_products will retry.",
                canonical_name, err_msg,
            )
            return True   # SQLite has it -- DC can proceed, sync will retry

        # --- 4. API success: mark as synced ---
        if prod_row:
            master_db.mark_product_synced(prod_row['id'], None, json.dumps(result))
        logger.info(
            "[LIQUID PRODUCT] '%s' synced to server -- marked is_synced=1",
            canonical_name,
        )
        return True

    except Exception as exc:
        logger.error(
            "[LIQUID PRODUCT] Unexpected error for '%s': %s", stock_name, exc, exc_info=True,
        )
        return False
    finally:
        if _own_db and master_db is not None:
            master_db.close()


def _safe_rate(value) -> float:
    """Extract numeric rate from Tally rate string like '200.00/nos' or '5000'."""
    if not value:
        return 0.0
    s = str(value).split('/')[0].strip().replace(',', '')
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _ensure_product(
    master_db_path: str,
    stock_name: str,
    company_name: str,
    entity_id: Optional[int],
    api_base_url: str,
    dc_item: Optional[Dict[str, Any]] = None,
    tally_url: Optional[str] = None,
    master_db: Optional['MasterDatabase'] = None,
) -> bool:
    """
    For any non-liquid DC product not found in SQLite:
      1. Parse stock_name using the standard Tally name parser
      2. If unparseable -- return False (caller will skip the DC)
      3. Check local SQLite by canonical name -- return True if already there
      4. Use DC inventory item data (rate, qty) for product fields
      5. Fetch full stock item from Tally for HSN, GST, GUID if available
      6. INSERT to SQLite (is_synced=0) then POST to /tally-product_name-payload/
      7. Mark is_synced=1 on API success; return True either way (SQLite has it)
    """
    parsed = parse_stock_item_name(stock_name)
    if not parsed:
        logger.warning(
            "[PRODUCT CREATE] '%s' -- cannot parse product name (no recognised type code). "
            "Product will not be auto-created.",
            stock_name,
        )
        return False

    canonical_name = parsed['canonical_name']
    dc_item = dc_item or {}
    _own_db = False
    try:
        if master_db is None:
            master_db = MasterDatabase(master_db_path)
            _own_db = True

        # Already in SQLite? (may have been created by a previous DC this run)
        existing = master_db.product_exists_normalized(canonical_name)
        if existing:
            logger.info(
                "[PRODUCT CREATE] '%s' already in local DB (canonical='%s') -- skipping API call",
                stock_name, canonical_name,
            )
            return True

        # --- Extract data from DC inventory item ---
        dc_rate = _safe_rate(dc_item.get('RATE') or dc_item.get('AMOUNT') or 0)
        dc_unit = parsed['unit_name']

        # --- Try to fetch full stock item from Tally for HSN, GST, GUID ---
        tally_guid = ''
        hsn_code = ''
        gst_applicable = ''
        gst_rate = 0.0
        igst_rate = 0.0
        cgst_rate = 0.0
        sgst_rate = 0.0
        stock_data_json = {'source': 'dc_fetch', 'original_name': stock_name}

        if tally_url:
            try:
                stock_item = tally_api.get_stock_item_by_name(company_name, stock_name, tally_url)
                if stock_item:
                    tally_guid = (
                        stock_item.get('GUID')
                        or stock_item.get('MASTERID')
                        or stock_item.get('REMOTEID')
                        or ''
                    )
                    hsn_code = (stock_item.get('HSNCODE') or '').strip()
                    gst_applicable = (
                        stock_item.get('GSTAPPLICABLE')
                        or stock_item.get('GSTAPPLICABILITY')
                        or ''
                    ).strip()
                    gst_rate = _safe_rate(stock_item.get('GST_RATE') or stock_item.get('GSTRATIOOFDUTY'))
                    igst_rate = _safe_rate(stock_item.get('IGST_RATE') or stock_item.get('IGSTRATIO'))
                    cgst_rate = _safe_rate(stock_item.get('CGST_RATE') or stock_item.get('CGSTTAXRATE'))
                    sgst_rate = _safe_rate(stock_item.get('SGST_RATE') or stock_item.get('SGSTTAXRATE'))
                    # Use Tally rate if DC rate is 0
                    if dc_rate == 0.0:
                        dc_rate = _safe_rate(
                            stock_item.get('STDCOST')
                            or stock_item.get('LASTPURCHASEPRICE')
                        )
                    # Use Tally unit if available
                    tally_unit = (stock_item.get('BASEUNITS') or stock_item.get('UOM') or '').strip()
                    if tally_unit:
                        dc_unit = tally_unit
                    stock_data_json = stock_item
                    logger.info(
                        "[PRODUCT CREATE] Fetched stock item '%s' from Tally -- GUID=%s, HSN=%s, GST=%s%%",
                        stock_name, tally_guid or 'N/A', hsn_code or 'N/A', gst_rate,
                    )
                else:
                    logger.info(
                        "[PRODUCT CREATE] Stock item '%s' not found in Tally -- using DC item data only",
                        stock_name,
                    )
            except Exception as exc:
                logger.warning(
                    "[PRODUCT CREATE] Failed to fetch stock item '%s' from Tally: %s -- using DC item data",
                    stock_name, exc,
                )

        # Insert with is_synced=0
        product_data = {
            'tally_guid': tally_guid,
            'name': stock_name,
            'name_canonical': canonical_name,
            'tally_company': company_name,
            'hsn_code': hsn_code,
            'unit': dc_unit,
            'rate': dc_rate,
            'description': f'Auto-created from DC fetch: {stock_name}',
            'data_json': json.dumps(stock_data_json),
            'product_master_name': parsed['product_master_name'],
            'variant_name': parsed['variant_name'],
            'unit_name': parsed['unit_name'],
            'product_type_code': parsed['product_type_code'],
            'product_type_name': parsed['product_type_name'],
            'gst_applicable': gst_applicable,
            'gst_rate': gst_rate,
            'igst_rate': igst_rate,
            'cgst_rate': cgst_rate,
            'sgst_rate': sgst_rate,
        }
        master_db.insert_product(product_data)
        logger.info(
            "[PRODUCT CREATE] '%s' saved to local SQLite (canonical='%s', rate=%s, HSN=%s, GST=%s%%, is_synced=0)",
            stock_name, canonical_name, dc_rate, hsn_code or 'N/A', gst_rate,
        )

        # Sync to Catalytics server
        _base = api_base_url.rstrip('/')
        endpoint = (_base + '/tally-product_name-payload/') if _base.endswith('/import') else (_base + '/import/tally-product_name-payload/')
        payload = {
            'entity_id': entity_id,
            'stock_item_name': canonical_name,
            'product_master_name': parsed['product_master_name'],
            'unit_master_name': parsed['unit_name'],
            'variant_name': parsed['variant_name'],
            'product_type_code': parsed['product_type_code'],
            'product_type_name': parsed['product_type_name'],
            'hsn_code': hsn_code,
            'guid': str(tally_guid).strip(),
            'rate': dc_rate,
            'gst_applicable': gst_applicable,
            'gst_rate': gst_rate,
            'igst_rate': igst_rate,
            'cgst_rate': cgst_rate,
            'sgst_rate': sgst_rate,
            'tally_company': company_name,
        }
        logger.info(
            "[PRODUCT CREATE] Syncing '%s' to server -- variant='%s', type=%s, rate=%s, HSN=%s",
            stock_name, parsed['variant_name'], parsed['product_type_code'], dc_rate, hsn_code or 'N/A',
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
                "[PRODUCT CREATE] Server sync failed for '%s': HTTP %d -- %s. "
                "Product in SQLite (is_synced=0), sync_products will retry.",
                stock_name, resp.status_code, resp.text[:300],
            )
            return True  # SQLite has it -- DC can proceed

        result = resp.json()
        data = result.get('data', {})
        has_error = (
            result.get('status') != 'success'
            or (isinstance(data, dict) and data.get('errors', 0) > 0)
        )
        if has_error:
            err_msg = (data.get('results') or [{}])[0].get('message') if isinstance(data, dict) else result.get('message')
            logger.error(
                "[PRODUCT CREATE] Server error for '%s': %s. "
                "Product in SQLite (is_synced=0), sync_products will retry.",
                stock_name, err_msg,
            )
            return True  # SQLite has it -- DC can proceed

        if prod_row:
            master_db.mark_product_synced(prod_row['id'], None, json.dumps(result))
        logger.info(
            "[PRODUCT CREATE] '%s' synced to server and marked is_synced=1 (canonical='%s')",
            stock_name, canonical_name,
        )
        return True

    except Exception as exc:
        logger.error(
            "[PRODUCT CREATE] Unexpected error for '%s': %s", stock_name, exc, exc_info=True,
        )
        return False
    finally:
        if _own_db and master_db is not None:
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


def _sync_customer_now(
    *,
    api_base_url: str,
    entity_id: Optional[int],
    company_name: str,
    ledger: Dict[str, Any],
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Try immediate customer sync; returns (ok, response_json, error_msg)."""
    if not api_base_url:
        return False, None, "CATALYTICS_API_BASE_URL not set"
    _base = api_base_url.rstrip('/')
    endpoint = (_base + '/tally-customer-payload/') if _base.endswith('/import') else (_base + '/import/tally-customer-payload/')
    payload = {"entity_id": entity_id, "ledger": ledger}
    if company_name:
        payload["company_name"] = company_name
    try:
        resp = requests.post(endpoint, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
        if resp.status_code not in (200, 201):
            return False, None, f"HTTP {resp.status_code} - {resp.text[:200]}"
        result = resp.json()
        response_json = json.dumps(result)
        if result.get("status") != "success":
            return False, response_json, f"API non-success: {result.get('message')}"
        data = result.get("data", {}) or {}
        if data.get("errors", 0) > 0:
            return False, response_json, "API returned customer errors"
        if data.get("created", 0) == 0 and data.get("updated", 0) == 0:
            return False, response_json, "Customer not created/updated"
        return True, response_json, None
    except Exception as exc:
        return False, None, str(exc)


# ---------------------------------------------------------------------------
# Delivery-term resolution: maps abbreviations/typos to standard terms.
# Users in Tally may type single letters or short words in OTHERREFERENCE.
# Single-letter mapping: d→delivery, c→customer pickup, s→supplier, t→traders
# ---------------------------------------------------------------------------
_REF_TERM_EXACT: Dict[str, str] = {
    # single letters
    'd': 'delivery',
    'c': 'customer pickup',
    's': 'supplier',
    't': 'traders',
    # dealer shorthand used by Tally users; map to traders as requested
    'de': 'traders',
    'ds': 'traders',
    # common short forms
    'del': 'delivery',
    'deliv': 'delivery',
    'cust': 'customer pickup',
    'sup': 'supplier',
    'supp': 'supplier',
    'tr': 'traders',
    'trd': 'traders',
}

_REF_TERM_CONTAINS: List[tuple] = [
    # order matters -- most specific first
    ('dealers pickup',   'dealers pickup'),
    ('dealer pickup',    'dealers pickup'),
    ('customer pik up',  'customer pickup'),
    ('customer pickup',  'customer pickup'),
    ('cust pickup',      'customer pickup'),
    ('pickup',           'customer pickup'),
    ('supplier',         'supplier'),
    ('traders',          'traders'),
    ('trader',           'traders'),
    ('delivery',         'delivery'),
]


def _resolve_ref_term(ref: str) -> str:
    """
    Resolve a raw OTHERREFERENCE value to a standard delivery term.
    Handles single-letter abbreviations, short forms, and full words.
    Returns '' if the value cannot be mapped.

    Examples:
      "d"               → "delivery"
      "c"               → "customer pickup"
      "del"             → "delivery"
      "delivery"        → "delivery"
      "customer pik up" → "customer pickup"
      "suppliers"       → "supplier"
    """
    t = ref.strip().lower()
    if not t:
        return ''
    # Exact / short-form match on the full string (e.g. "d", "del", "delivery")
    if t in _REF_TERM_EXACT:
        return _REF_TERM_EXACT[t]
    # Contains match for full words/phrases (e.g. "customer pickup", "supplier")
    for keyword, term in _REF_TERM_CONTAINS:
        if keyword in t:
            return term
    # Token-level match: handle repeated/spaced abbreviations like "d d", "D D"
    for token in t.split():
        if token in _REF_TERM_EXACT:
            return _REF_TERM_EXACT[token]
    return ''


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
        # Reference filtering is optional. Empty means "fetch all DCs".
        reference_keywords=_parse_reference_keywords(
            cfg.get_env("DC_REFERENCE_KEYWORDS", "")
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

    # Master DB has customers + products tables for validation.
    # If master_db_path is the same file as db_path (single DB setup),
    # reuse the same connection to avoid "database is locked" errors.
    master_db_instance = None
    master_conn = None
    _same_db = (
        config.master_db_path
        and os.path.exists(config.master_db_path)
        and os.path.abspath(config.master_db_path) == os.path.abspath(config.db_path)
    )
    if _same_db:
        # Same file -- wrap conn in a MasterDatabase-like object for compatibility
        master_db_instance = MasterDatabase.__new__(MasterDatabase)
        master_db_instance.db_path = config.master_db_path
        master_db_instance.conn = conn
        master_conn = conn
        logger.info("Master DB and DC DB are same file, using single connection")
    elif config.master_db_path and os.path.exists(config.master_db_path):
        master_db_instance = MasterDatabase(config.master_db_path)
        master_conn = master_db_instance.conn
    else:
        logger.warning("master_db_path not found (%s) -- customer/product validation disabled", config.master_db_path)

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
        _auto_url = cfg.get_env("CATALYTICS_API_BASE_URL", "") or cfg.get_env("CATALYTICS_API_BASE", "") or ""
        create_auto_ticket(
            api_base_url=_auto_url,
            subject='DC Fetch: Tally company name mismatch',
            description=(
                f'Company "{config.company}" not found in Tally.\n'
                f'Available companies: {available}\n\n'
                'Update the TALLY_COMPANY setting in .env to match one of the available companies.'
            ),
            entity_id=config.entity_id,
            priority=1,
            category=11,
            error_code='DC_FETCH_COMPANY_MISMATCH',
        )
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
    user_specified_range = bool(config.from_date and config.to_date)
    if user_specified_range:
        from_date = config.from_date
        to_date = config.to_date
        logger.info("Using user-specified date range %s to %s", from_date, to_date)
    else:
        # Use default range (days_back or financial year)
        from_date, to_date = _default_date_range(config.days_back)
        logger.info("Using default date range %s to %s (days_back=%s)", from_date, to_date, config.days_back)

    logger.info("\n%s", "=" * 60)
    logger.info("DC FETCH STARTED")
    logger.info("=" * 60)
    logger.info("Tally URL:    %s", config.tally_url)
    logger.info("Company:      %s", company_name)
    logger.info("Date Range:   %s to %s", from_date, to_date)
    logger.info("Days Back:    %s", config.days_back)
    logger.info("Fetch Stock:  %s", config.fetch_stock)
    logger.info(
        "Reference Filter: %s",
        "ENABLED (user-specified range)" if (config.reference_keywords and user_specified_range)
        else "DISABLED (default 3-day Day Book window)",
    )
    logger.info("=" * 60)

    # get_delivery_notes uses Day Book (current date only)
    vouchers = tally_api.get_delivery_notes(company_name, config.tally_url, from_date, to_date)
    logger.info("Fetched %d delivery notes (DCs) from Tally", len(vouchers))

    if vouchers:
        for i, v in enumerate(vouchers):
            items = v.get("INVENTORY") or []
            item_names = ", ".join(
                (it.get("STOCKITEMNAME") or it.get("ITEMNAME") or "?")
                for it in items
            )[:100]
            logger.info(
                "  DC #%d: No=%s | Date=%s | Party=%s | Items=%d [%s]",
                i + 1,
                v.get("VOUCHERNUMBER", "?"),
                v.get("DATE", "?"),
                v.get("PARTYLEDGERNAME", "?"),
                len(items),
                item_names,
            )
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

    # import json at module level is already done; ensure it's available here
    import json

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
        # Only process DCs that have OTHERREFERENCE set.
        # PONUMBER / REFERENCE / VOUCHERREFERENCE are NOT accepted as substitutes.
        reference = (
            voucher.get("OTHERREFERENCE")
            or voucher.get("BASICORDERREF")
            or ""
        ).strip()

        if not reference:
            skipped += 1
            skipped_ref_filter += 1
            logger.info("Skipping DC %s (%s): OTHERREFERENCE is empty", dc_no, party_name or "?")
            continue

        # --- Liquid product pre-creation (BEFORE any validation so it always runs) ---
        inventory_items = voucher.get("INVENTORY") or []
        _api_url = cfg.get_env('CATALYTICS_API_BASE_URL', '') or ''
        liquid_item_created: Dict[int, bool] = {}

        if not _api_url:
            logger.warning("[LIQUID] CATALYTICS_API_BASE_URL not set -- liquid product creation skipped for DC %s", dc_no)
        elif not config.master_db_path:
            logger.warning("[LIQUID] master_db_path (SQLITE_DB_PATH) not set -- liquid product creation skipped for DC %s", dc_no)
        else:
            for item in inventory_items:
                _sname = (item.get("STOCKITEMNAME") or item.get("ITEMNAME") or "").strip()
                if not _sname.lower().startswith('liquid'):
                    continue

                _qty_str = (item.get('BILLEDQTY') or item.get('ACTUALQTY') or '').strip()
                if _qty_str and not item.get("ORIGINAL_BILLEDQTY"):
                    item["ORIGINAL_BILLEDQTY"] = _qty_str

                item['BILLEDQTY'] = '1'
                item['ACTUALQTY'] = '1'

                _nkey = _normalize_name_key(_sname + _qty_str)

                logger.info(
                    "[LIQUID] DC %s | Found liquid product '%s' | BILLEDQTY='%s' (forced→1)",
                    dc_no, _sname, _qty_str,
                )

                if _nkey in stock_cache:
                    liquid_item_created[id(item)] = bool(stock_cache.get(_nkey))
                    logger.info("[LIQUID] DC %s | '%s' already handled this run -- skipping", dc_no, _sname)
                    continue

                _created = _ensure_liquid_product(
                    master_db_path=config.master_db_path,
                    stock_name=_sname,
                    item=item,
                    original_qty=_qty_str,
                    company_name=company_name,
                    entity_id=config.entity_id,
                    api_base_url=_api_url,
                    master_db=master_db_instance,
                )
                stock_cache[_nkey] = _created
                liquid_item_created[id(item)] = _created
                logger.info(
                    "[LIQUID] DC %s | '%s' result: %s",
                    dc_no, _sname, "OK" if _created else "FAILED",
                )

        # Reference resolution
        _REF_FIELDS_PRIORITY = (
            "OTHERREFERENCE", "REFERENCE", "BASICORDERREF",
            "PONUMBER", "VOUCHERREFERENCE", "ORDERREF", "ORDERINGNO",
        )
        _other_ref_raw = ''
        _resolved_term = ''
        for _rf in _REF_FIELDS_PRIORITY:
            _rfval = (voucher.get(_rf) or "").strip()
            if not _rfval:
                continue
            _candidate = _resolve_ref_term(_rfval)
            if _candidate:
                _other_ref_raw = _rfval
                _resolved_term = _candidate
                break

        # --- Reference keyword filter ---
        if config.reference_keywords and user_specified_range:
            ref_text = _extract_reference_text(voucher)
            # Normal keyword match (full words in combined reference fields)
            _ref_matches = any(kw in ref_text for kw in config.reference_keywords)
            # Also accept if OTHERREFERENCE resolves to any known delivery term
            # (handles abbreviations like "d", "c", "s", "t")
            if not _ref_matches and _resolved_term:
                _ref_matches = True
                logger.info(
                    "[REF RESOLVED] DC %s (%s) - OTHERREFERENCE='%s' resolved to '%s'",
                    dc_no, party_name, _other_ref_raw, _resolved_term,
                )
            if not _ref_matches:
                skipped += 1
                skipped_ref_filter += 1
                logger.info(
                    "[SKIP REF FILTER] DC %s (%s) - REF='%s' has no keyword match %s",
                    dc_no, party_name, ref_text or "(empty)", config.reference_keywords,
                )
                continue

        # --- Validate party name ---
        normalized_party = _normalize_name_key(party_name)
        if not normalized_party:
            logger.warning("Skipping DC %s: empty party name", dc_no)
            skipped += 1
            continue

        # --- Tank qty override ---
        tank_overrides = _override_tank_qty(inventory_items)
        if tank_overrides:
            logger.info("Adjusted qty=1 for %d item(s) Tank of (Tnk) in DC %s", tank_overrides, dc_no)

        # --- DC-DRIVEN: Create missing customer from DC voucher data ---
        if master_db_instance is not None:
            if normalized_party not in ledger_cache:
                row = master_db_instance.conn.execute(
                    "SELECT id FROM customers WHERE lower(replace(name, ' ', '')) = ?",
                    (normalized_party,),
                ).fetchone()
                ledger_cache[normalized_party] = row is not None

            if not ledger_cache[normalized_party]:
                # Extract customer data from DC voucher fields
                _cust_gstin = (voucher.get('PARTYGSTIN') or voucher.get('CONSIGNEEGSTIN') or '').strip()
                _cust_pan = (voucher.get('BUYERPINNUMBER') or '').strip()
                _cust_state = (voucher.get('STATENAME') or voucher.get('CONSIGNEESTATENAME') or '').strip()
                _cust_pincode = (voucher.get('PARTYPINCODE') or voucher.get('CONSIGNEEPINCODE') or '').strip()
                _cust_country = (voucher.get('COUNTRYOFRESIDENCE') or '').strip()

                # Parse phone/email from ADDRESSES text
                _cust_phone = ''
                _cust_email = ''
                _cust_address = ''
                _addr_lines = voucher.get('ADDRESSES') or []
                if isinstance(_addr_lines, list):
                    _addr_parts = []
                    for _aline in _addr_lines:
                        _astr = str(_aline).strip()
                        if _astr.lower().startswith('phone:'):
                            _cust_phone = _astr[6:].strip()
                        elif _astr.lower().startswith('email:'):
                            _cust_email = _astr[6:].strip()
                        else:
                            _addr_parts.append(_astr)
                    _cust_address = ', '.join(_addr_parts)

                # Consignee as delivery address
                _consignee = voucher.get('CONSIGNEE') or {}
                _delivery_addresses = []
                if isinstance(_consignee, dict) and (_consignee.get('ADDRESS') or _consignee.get('NAME')):
                    _delivery_addresses.append({
                        'name': _consignee.get('NAME', ''),
                        'address': _consignee.get('ADDRESS', ''),
                        'state': _consignee.get('STATE', ''),
                        'country': _cust_country or 'India',
                        'pincode': _consignee.get('PINCODE', ''),
                        'gstin': _consignee.get('GSTIN', ''),
                    })

                _cust_data = {
                    'tally_guid': '',
                    'name': party_name,
                    'tally_company': company_name,
                    'gstin': _cust_gstin,
                    'pan': _cust_pan,
                    'address': _cust_address,
                    'state': _cust_state,
                    'city': '',
                    'pincode': _cust_pincode,
                    'phone': _cust_phone,
                    'email': _cust_email,
                    'delivery_addresses_json': json.dumps(_delivery_addresses) if _delivery_addresses else None,
                    'data_json': json.dumps({
                        'NAME': party_name, 'PARTYGSTIN': _cust_gstin,
                        'INCOMETAXNUMBER': _cust_pan, 'STATE': _cust_state,
                        'PINCODE': _cust_pincode, 'MOBILE': _cust_phone,
                        'EMAIL': _cust_email, 'COUNTRY': _cust_country,
                        '_source': 'dc_voucher', '_dc_no': dc_no,
                    }),
                }
                try:
                    master_db_instance.insert_customer(_cust_data)
                    ledger_cache[normalized_party] = True
                    logger.info(
                        "[DC-DRIVEN NEW CUSTOMER] '%s' created from DC %s "
                        "(GSTIN: %s, state: %s, pincode: %s)",
                        party_name, dc_no, _cust_gstin or 'N/A',
                        _cust_state or 'N/A', _cust_pincode or 'N/A',
                    )

                    # Immediate sync for auto-created customer.
                    _cust_row = master_db_instance.conn.execute(
                        "SELECT id FROM customers WHERE lower(replace(name, ' ', '')) = ? ORDER BY id DESC LIMIT 1",
                        (normalized_party,),
                    ).fetchone()
                    _cust_id = int(_cust_row["id"]) if _cust_row else None
                    _ledger_for_sync = {
                        "NAME": party_name,
                        "PARTYGSTIN": _cust_gstin,
                        "GSTIN": _cust_gstin,
                        "INCOMETAXNUMBER": _cust_pan,
                        "STATENAME": _cust_state,
                        "STATE": _cust_state,
                        "PINCODE": _cust_pincode,
                        "MOBILE": _cust_phone,
                        "EMAIL": _cust_email,
                        "PRIMARY_ADDRESS": _cust_address,
                        "COUNTRY": _cust_country,
                    }
                    if _delivery_addresses:
                        _ledger_for_sync["DELIVERY_ADDRESSES"] = _delivery_addresses
                    _ok, _resp_json, _err = _sync_customer_now(
                        api_base_url=_api_url,
                        entity_id=config.entity_id,
                        company_name=company_name,
                        ledger=_ledger_for_sync,
                    )
                    if _cust_id is not None:
                        if _ok:
                            master_db_instance.mark_customer_synced(_cust_id, None, _resp_json)
                            logger.info("[DC-DRIVEN NEW CUSTOMER] '%s' synced to server", party_name)
                        else:
                            master_db_instance.mark_customer_sync_failed(_cust_id, _err or "sync failed", _resp_json)
                            logger.warning(
                                "[DC-DRIVEN NEW CUSTOMER] '%s' local only; server sync failed: %s",
                                party_name, _err or "unknown error",
                            )
                except Exception as exc:
                    logger.error("[DC-DRIVEN] Failed to create customer '%s' from DC %s: %s",
                                 party_name, dc_no, exc)
                    skipped_missing_customer += 1
                    continue

        # --- DC-DRIVEN: Create missing products from DC inventory data ---
        if master_db_instance is not None:
            for item in inventory_items:
                stock_name = (item.get("STOCKITEMNAME") or item.get("ITEMNAME") or "").strip()
                if not stock_name:
                    continue
                normalized_stock = _normalize_name_key(stock_name)

                if normalized_stock not in stock_cache:
                    row = master_db_instance.conn.execute(
                        "SELECT id FROM products WHERE lower(replace(name, ' ', '')) = ?",
                        (normalized_stock,),
                    ).fetchone()
                    stock_cache[normalized_stock] = row is not None

                if stock_cache.get(normalized_stock, True):
                    continue  # Already exists

                # Liquid products already handled by pre-creation above
                if stock_name.lower().startswith('liquid'):
                    if liquid_item_created.get(id(item)) is True:
                        stock_cache[normalized_stock] = True
                        continue

                # Parse product name using same logic as fetch_products.py
                from fetch_products import parse_stock_item_name, _safe_float
                parsed = parse_stock_item_name(stock_name)
                if not parsed:
                    logger.info("[DC-DRIVEN] Product '%s' not parseable -- skipping (DC %s)", stock_name, dc_no)
                    continue

                _hsn = (item.get('GSTHSNNAME') or '').strip()
                _rate_str = (item.get('RATE') or '').strip()
                # Parse rate: "25.00/ltr" → 25.0
                _rate = 0.0
                if _rate_str:
                    import re as _re
                    _rate_match = _re.search(r'[\d.]+', _rate_str)
                    if _rate_match:
                        try:
                            _rate = float(_rate_match.group())
                        except ValueError:
                            pass

                _prod_data = {
                    'tally_guid': '',
                    'name': stock_name,
                    'name_canonical': parsed['canonical_name'],
                    'tally_company': company_name,
                    'hsn_code': _hsn,
                    'unit': parsed.get('unit_name', ''),
                    'rate': _rate,
                    'description': '',
                    'data_json': json.dumps({
                        'NAME': stock_name, 'HSNCODE': _hsn,
                        '_source': 'dc_voucher', '_dc_no': dc_no,
                    }),
                    'product_master_name': parsed['product_master_name'],
                    'variant_name': parsed['variant_name'],
                    'unit_name': parsed['unit_name'],
                    'product_type_code': parsed['product_type_code'],
                    'product_type_name': parsed['product_type_name'],
                    'gst_applicable': '',
                    'gst_rate': 0.0,
                    'igst_rate': 0.0,
                    'cgst_rate': 0.0,
                    'sgst_rate': 0.0,
                }
                try:
                    master_db_instance.insert_product(_prod_data)
                    stock_cache[normalized_stock] = True
                    logger.info(
                        "[DC-DRIVEN NEW PRODUCT] '%s' created from DC %s "
                        "(canonical: %s, HSN: %s, type: %s)",
                        stock_name, dc_no, parsed['canonical_name'],
                        _hsn or 'N/A', parsed['product_type_code'] or 'N/A',
                    )
                except Exception as exc:
                    logger.error("[DC-DRIVEN] Failed to create product '%s' from DC %s: %s",
                                 stock_name, dc_no, exc)

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

        # Change detection: compare raw Tally data only.
        # DO NOT compare against sync_status.payload_hash -- that is the sync
        # payload hash written by sync_catalytics and uses a different format.
        # Comparing them always produces a mismatch, causing every DC to be
        # re-synced on every fetch run even when nothing changed.
        new_data_json = db.json_dumps(voucher)
        data_changed = (existing_json is None) or (existing_json != new_data_json)

        item_names = ", ".join(
            (it.get("STOCKITEMNAME") or it.get("ITEMNAME") or "?")
            for it in inventory_items
        )[:150]

        if existing_json is None:
            created += 1
            logger.info(
                "[NEW DC] '%s' saved (date: %s, party: %s, items: %d [%s], ref: %s)",
                dc_no, voucher_date, party_name, len(inventory_items), item_names,
                reference or 'N/A',
            )
        elif data_changed:
            updated += 1
            logger.info(
                "[UPDATED DC] '%s' updated (date: %s, party: %s, items: %d [%s])",
                dc_no, voucher_date, party_name, len(inventory_items), item_names,
            )
        else:
            logger.info(
                "[UNCHANGED DC] '%s' no changes (date: %s, party: %s)",
                dc_no, voucher_date, party_name,
            )

        if data_changed:
            # Data changed in Tally -- mark as pending sync (reset is_synced)
            # Keep payload_hash=NULL so sync_catalytics recomputes it fresh.
            conn.execute(
                """INSERT INTO sync_status
                       (delivery_note_id, is_synced, attempts, payload_hash, created_at, updated_at)
                   VALUES (?, 0, 0, NULL, ?, ?)
                   ON CONFLICT(delivery_note_id) DO UPDATE SET
                       is_synced = 0, attempts = 0, payload_hash = NULL,
                       last_error = NULL, synced_at = NULL,
                       updated_at = excluded.updated_at""",
                (dn_id, db.now_ts(), db.now_ts()),
            )
        else:
            # Data unchanged -- ensure sync_status row exists but do NOT reset is_synced
            conn.execute(
                """INSERT INTO sync_status
                       (delivery_note_id, is_synced, attempts, payload_hash, created_at, updated_at)
                   VALUES (?, 0, 0, NULL, ?, ?)
                   ON CONFLICT(delivery_note_id) DO NOTHING""",
                (dn_id, db.now_ts(), db.now_ts()),
            )

        if not config.dry_run:
            conn.commit()

    # --- Delete detection DISABLED ---
    # Auto-delete detection is disabled because false positives (partial Tally
    # response, date-range mismatch, Day Book timeout) cause DCs to be
    # incorrectly marked as deleted and then cancelled on the server.
    # DCs removed from Tally will simply stop being updated in SQLite.
    deleted = 0

    logger.info("\n%s", "=" * 60)
    logger.info("DC FETCH SUMMARY")
    logger.info("=" * 60)
    logger.info("Total Fetched from Tally:   %d", len(vouchers))
    logger.info("New DCs Saved:              %d", created)
    logger.info("Updated DCs:                %d", updated)
    logger.info("Skipped - Ref Filter:       %d", skipped_ref_filter)
    logger.info("Skipped - Missing Customer: %d", skipped_missing_customer)
    logger.info("Skipped - Missing Product:  %d", skipped_missing_product)
    logger.info("Deleted DCs:                %d", deleted)
    logger.info("Ledgers Fetched:            %d", ledgers_fetched)
    logger.info("Stock Items Fetched:        %d", stock_items_fetched)
    logger.info("=" * 60)

    # DB stats
    total_dcs = conn.execute(
        "SELECT COUNT(*) FROM delivery_notes WHERE company_id = ?", (company_id,)
    ).fetchone()[0]
    synced_dcs = conn.execute(
        """SELECT COUNT(*) FROM sync_status ss
           JOIN delivery_notes dn ON dn.id = ss.delivery_note_id
           WHERE ss.is_synced = 1 AND dn.company_id = ?""",
        (company_id,),
    ).fetchone()[0]
    pending_dcs = conn.execute(
        """SELECT COUNT(*) FROM sync_status ss
           JOIN delivery_notes dn ON dn.id = ss.delivery_note_id
           WHERE ss.is_synced = 0 AND dn.company_id = ?""",
        (company_id,),
    ).fetchone()[0]
    logger.info("Total DCs in DB:    %d", total_dcs)
    logger.info("Synced DCs:         %d", synced_dcs)
    logger.info("Pending Sync DCs:   %d", pending_dcs)
    logger.info("=" * 60)

    if master_db_instance is not None and not _same_db:
        master_db_instance.close()

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
        _auto_url = cfg.get_env("CATALYTICS_API_BASE_URL", "") or cfg.get_env("CATALYTICS_API_BASE", "") or ""
        create_auto_ticket(
            api_base_url=_auto_url,
            subject='DC Fetch: unhandled exception during fetch run',
            description=traceback.format_exc(),
            entity_id=config.entity_id,
            priority=1,
            category=11,
            error_code='DC_FETCH_CRASH',
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


