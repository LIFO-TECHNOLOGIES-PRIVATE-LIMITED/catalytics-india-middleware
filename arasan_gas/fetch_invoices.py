"""
Fetch invoices from multiple Tally companies with enhanced change detection.
- Stores complete Tally data (voucher, ledger, stock items) as JSON
- Computes payload hash for robust change detection (like CO_middleware)
- Supports soft delete tracking for deleted invoices
- Filters by config start date (from-date onward) and delivery information
"""
import os
import logging
from pathlib import Path
from datetime import datetime, timedelta

from config import config, BASE_DIR
from db import Database, json_dumps, json_loads, sha256_text
from tally_client import TallyClient
from fetch_products import parse_stock_item_name, _PRODUCT_VARIANTS

# Try to import dashboard logger (optional - may not be available)
try:
    from log_capture import dashboard_logger
    HAS_DASHBOARD_LOGGER = True
except ImportError:
    HAS_DASHBOARD_LOGGER = False
    dashboard_logger = None

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

def _attach_invoice_fetch_file_handler():
    """Log invoice fetch operations to a dedicated file."""
    log_path = Path(BASE_DIR) / 'logs' / 'invoice_fetch.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'invoice_fetch_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'invoice_fetch_file'
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_invoice_fetch_file_handler()

def _attach_invoice_fetch_error_handler():
    """Log invoice fetch errors to a dedicated file."""
    log_path = Path(BASE_DIR) / 'logs' / 'invoice_fetch_errors.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'invoice_fetch_error_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'invoice_fetch_error_file'
    fh.setLevel(logging.ERROR)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_invoice_fetch_error_handler()

def log_message(message):
    """Log to both terminal and dashboard"""
    logger.info(message)
    if HAS_DASHBOARD_LOGGER and dashboard_logger:
        dashboard_logger.write_log(message)


def _compute_payload_hash(voucher, inventory_items, ledger_data, stock_items_map):
    """
    Compute SHA256 hash of complete invoice payload.
    Includes: voucher, inventory items, ledger data, stock item data.
    Hash changes trigger re-sync (like CO_middleware).
    """
    payload = {
        'voucher': voucher,
        'inventory': inventory_items or [],
        'ledger': ledger_data or {},
        'stock_items': stock_items_map or {},
    }
    payload_json = json_dumps(payload)
    return sha256_text(payload_json)


# CO2 cylinder weights to try (in priority order) when Tally records qty in kg.
# Whichever weight divides the kg qty exactly (zero remainder) wins.
_CO2_CYL_WEIGHTS = [30, 27]


def _is_co2_item(item_name: str) -> bool:
    """Return True if item_name identifies a CO2 / Carbon-Di-Oxide product."""
    nl = item_name.lower().replace('-', ' ').replace('_', ' ')
    return (
        'co2' in nl
        or 'carbon dioxide' in nl
        or 'carbondioxide' in nl
        or 'carbon di oxide' in nl
    )


def _parse_tally_qty_unit(raw_qty_str: str):
    """
    Extract (qty_float, unit_lower) from a Tally qty string.
    Examples: '60.00 Kgs' -> (60.0, 'kgs'),  '2.00 Nos' -> (2.0, 'nos')
    """
    parts = str(raw_qty_str or '').strip().split()
    if not parts:
        return 0.0, ''
    try:
        qty = abs(float(parts[0].replace(',', '')))
    except (ValueError, IndexError):
        qty = 0.0
    unit = parts[1].lower() if len(parts) > 1 else ''
    return qty, unit


def _build_fallback_voucher(invoice):
    """Build a minimal voucher payload when full raw voucher is unavailable."""
    voucher = {
        'VOUCHERNUMBER': invoice.get('voucher_no', ''),
        'DATE': invoice.get('voucher_date', ''),
        'PARTYLEDGERNAME': invoice.get('customer_name', ''),
        'ADDRESSES': [invoice.get('billing_address', '')] if invoice.get('billing_address') else [],
        'INVENTORY': [],
        'LEDGERENTRIES': [],
    }

    delivery_address = invoice.get('delivery_address', '')
    if delivery_address:
        voucher['CONSIGNEE'] = {'ADDRESS': delivery_address}

    for item in invoice.get('items', []) or []:
        voucher['INVENTORY'].append({
            'STOCKITEMNAME': item.get('item_name', ''),
            'ACTUALQTY': str(item.get('quantity', 0)),
            'RATE': str(item.get('rate', 0)),
            'AMOUNT': str(item.get('amount', 0)),
        })

    total_amount = invoice.get('total_amount', 0.0)
    if total_amount:
        voucher['LEDGERENTRIES'].append({
            'LEDGERNAME': invoice.get('customer_name', ''),
            'AMOUNT': str(total_amount),
        })

    return voucher


def _build_full_voucher_payload(invoice):
    """Prefer full Tally voucher payload; fallback to normalized invoice reconstruction."""
    raw_voucher = invoice.get('raw_voucher')
    fallback_voucher = _build_fallback_voucher(invoice)

    if not isinstance(raw_voucher, dict) or not raw_voucher:
        return fallback_voucher

    voucher = dict(raw_voucher)

    voucher.setdefault('VOUCHERNUMBER', invoice.get('voucher_no', ''))
    voucher.setdefault('DATE', invoice.get('voucher_date', ''))
    voucher.setdefault('PARTYLEDGERNAME', invoice.get('customer_name', ''))

    if invoice.get('billing_address') and not voucher.get('ADDRESSES'):
        voucher['ADDRESSES'] = [invoice.get('billing_address')]

    if invoice.get('delivery_address') and not voucher.get('CONSIGNEE'):
        voucher['CONSIGNEE'] = {'ADDRESS': invoice.get('delivery_address')}

    if not voucher.get('INVENTORY'):
        voucher['INVENTORY'] = fallback_voucher.get('INVENTORY', [])

    if not voucher.get('LEDGERENTRIES') and fallback_voucher.get('LEDGERENTRIES'):
        voucher['LEDGERENTRIES'] = fallback_voucher['LEDGERENTRIES']

    return voucher


def _invoice_in_date_range(invoice, from_date, to_date):
    """Return True if voucher_date (YYYYMMDD) is on/after from_date.
    Tally ERP9 Collection does not reliably respect SVFROMDATE/SVTODATE, so
    we must filter in Python after fetching all vouchers.
    """
    vd = str(invoice.get('voucher_date', '') or '').replace('-', '').strip()
    if not vd or len(vd) != 8:
        return True  # unknown date â€" include and let DB decide
    return vd >= from_date


def _has_delivery_info(invoice):
    """Return True only when Other References indicates delivery/pickup.

    This is a strict filter: vehicle/PO/delivery address are NOT considered.
    """
    raw = invoice.get('raw_voucher', {}) or {}
    other_ref = str(raw.get('BASICORDERREF') or raw.get('OTHERREFERENCE') or '').strip().lower()
    if not other_ref:
        return False

    other_ref_norm = ' '.join(other_ref.split())
    other_ref_compact = other_ref_norm.replace(' ', '')

    exact = {'dc', 'd', 'c'}
    substrings = (
        'delivery',
        'delivery challan',
        'dispatch',
        'customer pickup',
        'pickup',
        'self pickup',
        'self',
    )

    if other_ref_norm in exact:
        return True
    if any(key in other_ref_norm for key in substrings):
        return True
    if 'customerpickup' in other_ref_compact or 'deliverychallan' in other_ref_compact:
        return True
    return False


def _normalize_name_key(value):
    return str(value or '').replace(' ', '').strip().lower()


def fetch_invoices_from_all_companies(from_date=None, to_date=None):
    """
    Fetch invoices from all active Tally companies.
    Uses configurable rolling window (CO_middleware pattern):
      from_date = now - DC_PAST_DAYS   (default 3)
      to_date   = now + DC_FUTURE_DAYS (default 1)
    """
    try:
        config.reload_from_env()
    except AttributeError:
        pass

    today_dt = datetime.now()
    past_days = int(os.getenv('DC_PAST_DAYS', '3'))
    future_days = int(os.getenv('DC_FUTURE_DAYS', '1'))
    from_date = (today_dt - timedelta(days=past_days)).strftime('%Y%m%d')
    to_date = (today_dt + timedelta(days=future_days)).strftime('%Y%m%d')
    logger.info(f"Day Book fetch (Reference: {today_dt.strftime('%Y%m%d')})")
    logger.info(f" -> From Date: {from_date} (-{past_days} days)")
    logger.info(f" -> To Date:   {to_date} (+{future_days} days)")

    db = Database(config.SQLITE_DB_PATH)
    tally = TallyClient(config.TALLY_URL)
    active_companies = config.get_active_companies()

    if not active_companies:
        logger.error("No active Tally companies configured")
        return {
            'total_fetched': 0,
            'new_saved': 0,
            'updated_saved': 0,
            'already_exists': 0,
            'errors': 0,
        }

    logger.info(f"Starting invoice fetch from {len(active_companies)} companies")
    logger.info(f"Date range: {from_date} to {to_date}")
    logger.info(f"Active companies: {', '.join(active_companies)}")

    overall_stats = {
        'total_fetched': 0,
        'skipped_date': 0,
        'skipped_no_delivery': 0,
        'skipped_missing_customer': 0,
        'skipped_missing_product': 0,
        'new_saved': 0,
        'updated_saved': 0,
        'already_exists': 0,
        'deleted': 0,
        'errors': 0,
    }

    # Per-run caches: avoid re-fetching the same ledger/stock item from Tally
    # Key: "company_name::ledger_or_item_name"
    ledger_cache = {}   # customer ledger data
    stock_cache = {}    # stock item data

    for company_name in active_companies:
        company_key = config.get_company_key(company_name)
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing: {company_name} ({company_key})")
        logger.info(f"{'='*60}")

        try:
            # Batch in 30-day chunks to handle large date ranges (bol_company pattern)
            from_dt = datetime.strptime(from_date, '%Y%m%d')
            to_dt = datetime.strptime(to_date, '%Y%m%d') if to_date != '20991231' else datetime.now()

            all_invoices = []
            curr_start = from_dt
            while curr_start <= to_dt:
                curr_end = curr_start + timedelta(days=29)
                if curr_end > to_dt:
                    curr_end = to_dt

                s_date = curr_start.strftime('%Y%m%d')
                e_date = curr_end.strftime('%Y%m%d')

                logger.info(f" -> Batch: {s_date} to {e_date}")
                try:
                    batch = tally.get_sales_invoices(company_name, s_date, e_date)
                    if batch:
                        all_invoices.extend(batch)
                except Exception as tally_err:
                    logger.error(
                        f"[TALLY CONNECTION FAILED] Batch {s_date}-{e_date} from '{company_name}': {tally_err}",
                        exc_info=True
                    )
                    overall_stats['errors'] += 1

                curr_start = curr_start + timedelta(days=30)
                import time
                time.sleep(2)

            invoices = all_invoices
            overall_stats['total_fetched'] += len(invoices)

            if not invoices:
                logger.warning(f"No invoices returned from Tally for {company_name} in date range {from_date}-{to_date}")
                continue

            logger.info(f"Fetched {len(invoices)} invoices from Tally")

            for invoice in invoices:
                voucher_no = invoice.get('voucher_no', '')
                customer_name = invoice.get('customer_name', '')

                if not voucher_no or not customer_name:
                    logger.warning("Skipping invoice with missing voucher_no or customer")
                    continue

                # --- Date range filter (Tally ERP9 SVFROMDATE/SVTODATE is unreliable) ---
                if not _invoice_in_date_range(invoice, from_date, to_date):
                    overall_stats['skipped_date'] += 1
                    continue

                # --- Delivery ref filter (only when REQUIRE_DELIVERY_REF=true) ---
                if config.REQUIRE_DELIVERY_REF and not _has_delivery_info(invoice):
                    overall_stats['skipped_no_delivery'] += 1
                    raw = invoice.get('raw_voucher', {}) or {}
                    other_ref = (raw.get('BASICORDERREF') or raw.get('OTHERREFERENCE') or '').strip()
                    logger.info(
                        f"[SKIP NO OTHER REF] #{voucher_no} ({customer_name}) - "
                        f"Other Reference: '{other_ref or '<empty>'}'"
                    )
                    continue

                # --- Customer: lookup or auto-create from voucher ---
                # Invoice is NEVER skipped. ledger_data_json in invoices table
                # holds customer data from the voucher itself. Daily master sync
                # will later update the customers table with full Tally data.
                normalized_customer = _normalize_name_key(customer_name)
                ledger_cache_key = f"{company_name}::{normalized_customer}"
                if ledger_cache_key not in ledger_cache:
                    try:
                        row = db.query(
                            "SELECT name, data_json FROM customers WHERE lower(replace(name, ' ', '')) = ?",
                            (normalized_customer,)
                        )
                        row_dict = dict(row) if row else {}
                        if row_dict.get('data_json'):
                            ledger_cache[ledger_cache_key] = json_loads(row_dict.get('data_json'))
                            db_name = str(row_dict.get('name') or '').strip()
                            if db_name and db_name != customer_name:
                                logger.info(
                                    f"[CUSTOMER MATCH NORMALIZED] Invoice='{customer_name}' matched DB='{db_name}'"
                                )
                        else:
                            ledger_cache[ledger_cache_key] = None
                    except Exception as e:
                        logger.warning(f"[LEDGER LOOKUP FAILED] Could not lookup ledger for '{customer_name}': {e}")
                        ledger_cache[ledger_cache_key] = None

                ledger_data = ledger_cache[ledger_cache_key]
                if not ledger_data:
                    # Build ledger_data from invoice voucher (stored in ledger_data_json)
                    raw = invoice.get('raw_voucher', {}) or {}
                    _cust_gstin = (raw.get('PARTYGSTIN') or raw.get('CONSIGNEEGSTIN') or '').strip().lstrip(':')
                    _cust_pan = (raw.get('BUYERPINNUMBER') or raw.get('INCOMETAXNUMBER') or '').strip()
                    _cust_state = (raw.get('STATENAME') or raw.get('CONSIGNEESTATENAME') or '').strip()
                    _cust_pincode = (raw.get('PARTYPINCODE') or raw.get('CONSIGNEEPINCODE') or '').strip()
                    _cust_phone = ''
                    _cust_email = ''
                    _cust_address = ''
                    _addr_lines = raw.get('ADDRESSES') or []
                    if isinstance(_addr_lines, list):
                        _addr_parts = []
                        for _aline in _addr_lines:
                            _astr = str(_aline).strip()
                            if _astr.lower().startswith('phone:') or _astr.lower().startswith('mobile:'):
                                _cust_phone = _astr.split(':', 1)[1].strip()
                            elif _astr.lower().startswith('email:'):
                                _cust_email = _astr.split(':', 1)[1].strip()
                            else:
                                _addr_parts.append(_astr)
                        _cust_address = ', '.join(_addr_parts)

                    # ledger_data for invoice's ledger_data_json column
                    ledger_data = {
                        'NAME': customer_name, 'PARTYGSTIN': _cust_gstin,
                        'INCOMETAXNUMBER': _cust_pan, 'STATE': _cust_state,
                        'PINCODE': _cust_pincode, 'MOBILE': _cust_phone,
                        'EMAIL': _cust_email, '_source': 'dc_voucher',
                    }
                    ledger_cache[ledger_cache_key] = ledger_data

                    # Auto-create customer in SQLite (is_synced=0, synced in combined loop)
                    _cust_data = {
                        'tally_guid': None,
                        'name': customer_name,
                        'tally_company': company_name,
                        'gstin': _cust_gstin,
                        'pan': _cust_pan,
                        'address': _cust_address,
                        'state': _cust_state,
                        'city': '',
                        'pincode': _cust_pincode,
                        'phone': _cust_phone,
                        'email': _cust_email,
                        'data_json': json_dumps(ledger_data),
                    }
                    try:
                        existing_customer = db.query(
                            "SELECT id FROM customers WHERE name = ? AND tally_company = ? LIMIT 1",
                            (customer_name, company_name),
                        )
                        if existing_customer:
                            db.update_customer(existing_customer['id'], _cust_data)
                        else:
                            db.insert_customer(_cust_data)
                        overall_stats.setdefault('auto_fetched_customers', 0)
                        overall_stats['auto_fetched_customers'] += 1
                        logger.info(
                            f"[DC-DRIVEN:CUSTOMER] #{voucher_no} | '{customer_name}' "
                            f"created from voucher data (GSTIN: {_cust_gstin or 'N/A'})"
                        )
                    except Exception as exc:
                        # Customer insert failed but invoice still proceeds -
                        # ledger_data is already set from voucher for ledger_data_json
                        logger.warning(f"[DC-DRIVEN] Customer insert failed for '{customer_name}': {exc}")

                # --- Products: lookup or auto-create from voucher ---
                # Invoice is NEVER skipped. stock_items_json in invoices table
                # holds product data from the voucher. Daily master sync
                # will later update the products table with full Tally data.
                inventory_items = invoice.get('items', [])
                stock_items_map = {}
                missing_products = []
                # weight hints built during CO2 conversion: {item_name: weight_kg}
                # used later in auto-create to pick the correct CO2 variant
                _co2_weight_hints = {}

                for item in inventory_items:
                    item_name = (item.get('item_name') or '').strip()
                    if not item_name:
                        continue

                    # CO2 qty conversion: Tally may record qty in kg instead of cylinders.
                    # Try _CO2_CYL_WEIGHTS in order; use the weight that divides evenly.
                    # e.g. 54 kg ÷ 27 = 2.0 (whole) → variant=27kg, qty=2
                    #      60 kg ÷ 30 = 2.0 (whole) → variant=30kg, qty=2
                    if _is_co2_item(item_name):
                        raw_inv_lines = (invoice.get('raw_voucher') or {}).get('INVENTORY') or []
                        for _raw_inv in raw_inv_lines:
                            if (_raw_inv.get('STOCKITEMNAME') or '').strip() == item_name:
                                raw_qty_str = str(
                                    _raw_inv.get('ACTUALQTY') or _raw_inv.get('BILLEDQTY') or ''
                                )
                                _, _unit = _parse_tally_qty_unit(raw_qty_str)
                                if _unit in ('kg', 'kgs', 'kilogram', 'kilograms'):
                                    _orig_qty = item.get('quantity', 0.0)
                                    # Determine cylinder weight: pick whichever divides evenly
                                    _det_weight = _CO2_CYL_WEIGHTS[0]  # fallback
                                    for _w in _CO2_CYL_WEIGHTS:
                                        if _orig_qty > 0:
                                            _ratio = _orig_qty / _w
                                            if abs(_ratio - round(_ratio)) < 0.001:
                                                _det_weight = _w
                                                break
                                    _cyl_qty = float(round(_orig_qty / _det_weight))
                                    item['quantity'] = _cyl_qty
                                    _co2_weight_hints[item_name] = _det_weight
                                    # Patch raw INVENTORY so data_json stores corrected qty
                                    _raw_inv['ACTUALQTY'] = str(_cyl_qty)
                                    _raw_inv['BILLEDQTY'] = str(_cyl_qty)
                                    logger.info(
                                        f"[CO2 QTY] #{voucher_no} | '{item_name}': "
                                        f"{_orig_qty} kg ÷ {_det_weight} "
                                        f"= {_cyl_qty} cylinders ({_det_weight}kg variant)"
                                    )
                                    # Rename item to the correct variant so product lookup
                                    # uses the right name (e.g. "...30kg" → "...27kg")
                                    _parsed_co2 = parse_stock_item_name(item_name)
                                    if _parsed_co2:
                                        _correct_name = f"{_parsed_co2['product_master_name']} {_det_weight}kg (CYL)"
                                        if _correct_name != item_name:
                                            item['item_name'] = _correct_name
                                            _raw_inv['STOCKITEMNAME'] = _correct_name
                                            _co2_weight_hints[_correct_name] = _det_weight
                                            item_name = _correct_name
                                            logger.info(
                                                f"[CO2 RENAME] #{voucher_no} | renamed to '{_correct_name}'"
                                            )
                                break

                    normalized_item_name = _normalize_name_key(item_name)
                    stock_cache_key = f"{company_name}::{normalized_item_name}"
                    if stock_cache_key not in stock_cache:
                        try:
                            # Try exact name match first, then fall back to product_master_name match
                            # so "Carbon-Di-Oxide" finds "Carbon-Di-Oxide 30kg (CYL)" already in DB
                            row = db.query(
                                "SELECT name, data_json, product_type_code, product_type_name, "
                                "variant_name, unit_name FROM products "
                                "WHERE lower(replace(name, ' ', '')) = ? "
                                "OR lower(replace(product_master_name, ' ', '')) = ? "
                                "ORDER BY (lower(replace(name, ' ', '')) = ?) DESC LIMIT 1",
                                (normalized_item_name, normalized_item_name, normalized_item_name)
                            )
                            row_dict = dict(row) if row else {}
                            if row_dict.get('data_json'):
                                parsed_data = json_loads(row_dict.get('data_json')) or {}
                                db_name = str(row_dict.get('name') or '').strip()
                                if isinstance(parsed_data, dict):
                                    parsed_data = dict(parsed_data)
                                    # Override NAME with display_name so backend uses formatted name
                                    if db_name:
                                        parsed_data['NAME'] = db_name
                                        parsed_data['stock_item_name'] = db_name
                                        parsed_data['_display_name'] = db_name
                                    # Enrich with product type info from DB columns
                                    if row_dict.get('product_type_code'):
                                        parsed_data['product_type_code'] = row_dict['product_type_code']
                                    if row_dict.get('product_type_name'):
                                        parsed_data['product_type_name'] = row_dict['product_type_name']
                                    if row_dict.get('variant_name'):
                                        parsed_data['variant_name'] = row_dict['variant_name']
                                    if row_dict.get('unit_name'):
                                        parsed_data['unit_name'] = row_dict['unit_name']
                                stock_cache[stock_cache_key] = parsed_data
                                if db_name and db_name != item_name:
                                    logger.info(
                                        f"[PRODUCT MATCH NORMALIZED] Invoice='{item_name}' matched DB='{db_name}'"
                                    )
                            else:
                                stock_cache[stock_cache_key] = None
                        except Exception as e:
                            logger.warning(f"[STOCK LOOKUP FAILED] Could not lookup stock item '{item_name}': {e}")
                            stock_cache[stock_cache_key] = None

                    if stock_cache.get(stock_cache_key):
                        product_data = stock_cache[stock_cache_key]
                        display_name = (
                            product_data.get('_display_name') if isinstance(product_data, dict) else None
                        ) or item_name
                        if display_name != item_name:
                            logger.info(f"[NAME-NORMALIZE] #{voucher_no} | '{item_name}' -> '{display_name}'")
                            item['item_name'] = display_name
                            # Update raw_voucher INVENTORY STOCKITEMNAME to match display_name
                            for raw_inv in (invoice.get('raw_voucher', {}).get('INVENTORY') or []):
                                if (raw_inv.get('STOCKITEMNAME') or '').strip() == item_name:
                                    raw_inv['STOCKITEMNAME'] = display_name
                                    break
                        stock_items_map[display_name] = product_data
                    else:
                        missing_products.append(item_name)

                # Auto-create missing products + build stock_items_map from voucher
                if missing_products:
                    raw = invoice.get('raw_voucher', {}) or {}
                    for mp_name in missing_products:
                        # Extract HSN/rate from matching INVENTORY line in voucher
                        _hsn = ''
                        _rate = 0.0
                        for _inv_item in (raw.get('INVENTORY') or []):
                            if ((_inv_item.get('STOCKITEMNAME') or _inv_item.get('ITEMNAME') or '').strip() == mp_name):
                                _hsn = (_inv_item.get('GSTHSNNAME') or '').strip()
                                try:
                                    _rate_str = (_inv_item.get('RATE') or '').strip()
                                    if '/' in _rate_str:
                                        _rate_str = _rate_str.split('/')[0].strip()
                                    _rate = float(_rate_str.split()[0]) if _rate_str else 0.0
                                except (ValueError, IndexError):
                                    _rate = 0.0
                                break

                        parsed = parse_stock_item_name(mp_name) or {
                            'product_master_name': mp_name,
                            'product_type': 'CYLINDER',
                            'extracted_variant': None,
                        }

                        # stock_items_map entry for invoice's stock_items_json column
                        # (NAME and key will be updated to display_name after variant resolution)
                        _prod_json = {
                            'NAME': mp_name, 'HSNCODE': _hsn,
                            '_source': 'dc_voucher', '_dc_no': voucher_no,
                        }
                        stock_items_map[mp_name] = _prod_json  # raw key for INVENTORY lookup

                        # Auto-create one DB row per variant (BHOX pattern)
                        base_name    = parsed['product_master_name']
                        product_type = parsed['product_type']
                        ev           = parsed.get('extracted_variant')
                        variants     = _PRODUCT_VARIANTS.get(product_type, _PRODUCT_VARIANTS['CYLINDER'])

                        # If Tally name has a specific size, only create that variant.
                        # If size not in predefined variants, create a custom single variant for it.
                        if ev:
                            try:
                                ev_f = float(ev)
                                if product_type == 'CYLINDER' and abs(ev_f - 7.5) < 0.01:
                                    ev_f = 10.0
                                ev_str = str(int(ev_f)) if ev_f == int(ev_f) else str(ev_f)
                                filtered = [v for v in variants if str(v[0]) == ev_str]
                                if filtered:
                                    variants = filtered
                                else:
                                    # Non-standard size — create a custom variant using the
                                    # unit/type from the predefined list for this product type
                                    _, default_unit, default_type_code, default_type_name = variants[0]
                                    variants = [(ev_str, default_unit, default_type_code, default_type_name)]
                            except (ValueError, TypeError):
                                pass
                        else:
                            # No size in name:
                            #   CO2 → use weight hint from qty conversion (30kg or 27kg),
                            #          fall back to default 30kg
                            #   everything else → 7cum (CYL)
                            if product_type == 'CO2':
                                _hint_w = _co2_weight_hints.get(mp_name)
                                if _hint_w:
                                    # qty was in kg — use the specific variant determined
                                    filtered = [v for v in variants if str(v[0]) == str(_hint_w)]
                                    variants = filtered if filtered else [variants[0]]
                                else:
                                    # qty was in Nos — default to 30kg
                                    variants = [variants[0]]
                            else:
                                variants = [('7', 'cum', 'CYL', 'CYLINDER')]

                        try:
                            for size, unit, type_code, type_name in variants:
                                # ltr unit always means container (liquid/bulk storage)
                                if unit == 'ltr':
                                    type_code, type_name = 'CON', 'CONTAINER'
                                variant_label = f"{size}{unit}"
                                display_name  = f"{base_name} {variant_label} ({type_code})"
                                variant_guid  = f"|{type_code}_{size}{unit}"  # no tally_guid yet

                                # Update stock_items_map and raw INVENTORY to use formatted display_name
                                _prod_json['NAME'] = display_name
                                _prod_json['stock_item_name'] = display_name
                                _prod_json['_display_name'] = display_name
                                _prod_json['product_type_code'] = type_code
                                _prod_json['product_type_name'] = type_name
                                _prod_json['variant_name'] = variant_label
                                _prod_json['unit_name'] = unit
                                stock_items_map[display_name] = _prod_json
                                # Update item['item_name'] so items_json stores the variant name
                                for _inv_item in inventory_items:
                                    if (_inv_item.get('item_name') or '').strip() == mp_name:
                                        _inv_item['item_name'] = display_name
                                        break
                                # Update raw_voucher INVENTORY STOCKITEMNAME to match display_name
                                for raw_inv in (invoice.get('raw_voucher', {}).get('INVENTORY') or []):
                                    if (raw_inv.get('STOCKITEMNAME') or '').strip() == mp_name:
                                        raw_inv['STOCKITEMNAME'] = display_name
                                        break

                                _prod_data = {
                                    'tally_guid':          variant_guid,
                                    'name':                display_name,
                                    'name_canonical':      display_name,
                                    'tally_company':       company_name,
                                    'hsn_code':            _hsn,
                                    'unit':                unit,
                                    'rate':                _rate,
                                    'description':         '',
                                    'data_json':           json_dumps(_prod_json),
                                    'product_master_name': base_name,
                                    'variant_name':        variant_label,
                                    'unit_name':           unit,
                                    'product_type_code':   type_code,
                                    'product_type_name':   type_name,
                                    'gst_applicable':      '',
                                    'gst_rate':            0.0,
                                    'igst_rate':           0.0,
                                    'cgst_rate':           0.0,
                                    'sgst_rate':           0.0,
                                }
                                existing_product = db.product_exists_by_canonical(display_name)
                                if not existing_product:
                                    existing_product = db.product_exists_by_master_variant(base_name, variant_label)
                                if existing_product:
                                    db.update_product(existing_product['id'], _prod_data)
                                else:
                                    db.insert_product(_prod_data)

                            normalized_mp = _normalize_name_key(mp_name)
                            stock_cache[f"{company_name}::{normalized_mp}"] = _prod_json
                            overall_stats.setdefault('auto_fetched_products', 0)
                            overall_stats['auto_fetched_products'] += 1
                            logger.info(
                                f"[DC-DRIVEN:PRODUCT] #{voucher_no} | '{mp_name}' -> "
                                f"base='{base_name}', type={product_type}, "
                                f"variants={len(variants)} (HSN: {_hsn or 'N/A'})"
                            )
                        except Exception as exc:
                            # Product insert failed but invoice still proceeds -
                            # stock_items_map already has voucher data for stock_items_json
                            logger.warning(f"[DC-DRIVEN] Product insert failed for '{mp_name}': {exc}")

                try:
                    # Prepare invoice data
                    items_json = json_dumps(invoice.get('items', []))
                    full_voucher_payload = _build_full_voucher_payload(invoice)
                    data_json = json_dumps(full_voucher_payload)

                    ledger_data_json = json_dumps(ledger_data) if ledger_data else None
                    stock_items_json = json_dumps(stock_items_map) if stock_items_map else None

                    # Enrich stored voucher JSON with ledger + stock item GST details
                    enriched_voucher = dict(full_voucher_payload) if isinstance(full_voucher_payload, dict) else {}
                    if ledger_data:
                        enriched_voucher['LEDGERDATA'] = ledger_data
                    if stock_items_map:
                        enriched_voucher['STOCKITEMS'] = stock_items_map

                        # Enrich inventory lines with GST metadata (best-effort)
                        inv_lines = enriched_voucher.get('INVENTORY') or []
                        if isinstance(inv_lines, list):
                            for inv in inv_lines:
                                if not isinstance(inv, dict):
                                    continue
                                stock_name = inv.get('STOCKITEMNAME') or inv.get('ITEMNAME') or ''
                                stock_name = str(stock_name).strip()
                                if not stock_name:
                                    continue
                                stock_data = stock_items_map.get(stock_name) or {}
                                if not isinstance(stock_data, dict):
                                    continue

                                # Attach raw GST fields if present
                                for key in ('GST_RATE', 'IGST_RATE', 'CGST_RATE', 'SGST_RATE', 'HSNCODE'):
                                    if key in stock_data and key not in inv:
                                        inv[key] = stock_data.get(key)

                                # Compute estimated tax amount from rate (if available)
                                try:
                                    qty_val = float(str(inv.get('BILLEDQTY') or inv.get('ACTUALQTY') or '0').split()[0].replace(',', ''))
                                except Exception:
                                    qty_val = 0.0
                                try:
                                    rate_val = float(str(inv.get('RATE') or '0').replace(',', ''))
                                except Exception:
                                    rate_val = 0.0
                                try:
                                    amount_val = float(str(inv.get('AMOUNT') or '0').replace(',', ''))
                                except Exception:
                                    amount_val = qty_val * rate_val

                                gst_rate = stock_data.get('GST_RATE')
                                try:
                                    gst_rate_val = float(gst_rate) if gst_rate is not None else 0.0
                                except Exception:
                                    gst_rate_val = 0.0

                                if gst_rate_val and 'EST_TAX_AMOUNT' not in inv:
                                    inv['TAX_RATE_TOTAL'] = gst_rate_val
                                    inv['EST_TAX_AMOUNT'] = round(amount_val * (gst_rate_val / 100.0), 2)
                    data_json = json_dumps(enriched_voucher)

                    # Compute payload hash (like CO_middleware)
                    payload_hash = _compute_payload_hash(
                        full_voucher_payload,
                        inventory_items,
                        ledger_data,
                        stock_items_map
                    )

                    invoice_data = {
                        'voucher_no': voucher_no,
                        'tally_company': company_name,
                        'tally_guid': invoice.get('guid', ''),
                        'voucher_date': invoice.get('voucher_date', ''),
                        'customer_name': customer_name,
                        'customer_guid': invoice.get('customer_guid', ''),
                        'billing_address': invoice.get('billing_address', ''),
                        'delivery_address': invoice.get('delivery_address', ''),
                        'total_amount': invoice.get('total_amount', 0.0),
                        'tax_amount': invoice.get('tax_amount', 0.0),
                        'items_json': items_json,
                        'data_json': data_json,
                        'ledger_data_json': ledger_data_json,
                        'stock_items_json': stock_items_json,
                        'payload_hash': payload_hash,
                    }

                    existing = db.invoice_exists(voucher_no, company_name)

                    if not existing:
                        db.insert_invoice(invoice_data)
                        overall_stats['new_saved'] += 1
                        logger.info(
                            f"[NEW INVOICE] #{voucher_no} saved "
                            f"(company: {company_name}, customer: {customer_name}, "
                            f"date: {invoice.get('voucher_date', '')}, "
                            f"items: {len(inventory_items)}, hash: {payload_hash[:8]}...)"
                        )
                        continue

                    # Check for changes using both data_json and payload_hash
                    existing_data_json = existing['data_json'] if existing['data_json'] else ''
                    try:
                        existing_hash = existing['payload_hash'] if existing['payload_hash'] else None
                    except (KeyError, TypeError):
                        existing_hash = None
                    is_changed = (existing_data_json != data_json) or (existing_hash != payload_hash)

                    if is_changed:
                        db.update_invoice(existing['id'], invoice_data)
                        overall_stats['updated_saved'] += 1
                        logger.info(
                            f"[UPDATED INVOICE] #{voucher_no} refreshed "
                            f"(company: {company_name}, new hash: {payload_hash[:8]}...)"
                        )
                    else:
                        overall_stats['already_exists'] += 1
                        logger.debug(f"[ALREADY EXISTS] Invoice #{voucher_no} unchanged")

                except Exception as e:
                    logger.error(f"[ERROR] Failed to save invoice #{voucher_no}: {e}", exc_info=True)
                    overall_stats['errors'] += 1

            # --- Deletion detection (date-range scoped) ---
            # Collect dc_nos from Tally for this company
            tally_voucher_nos = set()
            for voucher_raw in invoices:
                final_voucher_no = voucher_raw.get('voucher_no', '').strip()
                if final_voucher_no:
                    tally_voucher_nos.add(final_voucher_no.lower())

            # Only detect deletions if: (1) Tally returned invoices, (2) we have synced records
            has_synced = db.query(
                """SELECT 1 FROM invoices
                   WHERE tally_company = ? AND is_synced = 1 LIMIT 1""",
                (company_name,)
            ) is not None

            if tally_voucher_nos and has_synced:
                # Find invoices in SQLite within date range that are NOT in Tally response
                sqlite_rows = db.query_all(
                    """SELECT tally_voucher_no FROM invoices
                       WHERE tally_company = ?
                         AND COALESCE(is_deleted, 0) = 0
                         AND voucher_date IS NOT NULL
                         AND voucher_date >= ? AND voucher_date <= ?""",
                    (company_name, from_date, to_date)
                )

                voucher_nos_to_delete = []
                for row in sqlite_rows:
                    if (row['tally_voucher_no'] or '').strip().lower() not in tally_voucher_nos:
                        voucher_nos_to_delete.append(row['tally_voucher_no'])

                if voucher_nos_to_delete:
                    deleted_count = db.mark_invoices_deleted(company_name, voucher_nos_to_delete)
                    overall_stats['deleted'] = overall_stats.get('deleted', 0) + deleted_count
                    logger.info(
                        f"[DELETION DETECTION] Marked {deleted_count} invoices as deleted "
                        f"(not in Tally response for {from_date} onward)"
                    )

        except Exception as e:
            logger.error(f"[ERROR] Failed to process company '{company_name}': {e}", exc_info=True)
            overall_stats['errors'] += 1

    logger.info(f"\n{'='*60}")
    logger.info("INVOICE FETCH SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Date filter: from {from_date} onward (query upper bound: {to_date})")
    logger.info(f"Total Fetched from Tally: {overall_stats['total_fetched']}")
    logger.info(f"Skipped (out of date range): {overall_stats['skipped_date']}")
    logger.info(f"Skipped (no delivery info): {overall_stats['skipped_no_delivery']}")
    logger.info(f"Skipped (missing customer): {overall_stats['skipped_missing_customer']}")
    logger.info(f"Skipped (missing product): {overall_stats['skipped_missing_product']}")
    logger.info(f"Auto-Fetch Cust:     {overall_stats.get('auto_fetched_customers', 0)}")
    logger.info(f"Auto-Fetch Prod:     {overall_stats.get('auto_fetched_products', 0)}")
    logger.info(f"New Invoices Saved: {overall_stats['new_saved']}")
    logger.info(f"Updated Invoices (hash/data changed): {overall_stats['updated_saved']}")
    logger.info(f"Already Exists (Unchanged): {overall_stats['already_exists']}")
    logger.info(f"Marked as Deleted: {overall_stats['deleted']}")
    logger.info(f"Errors: {overall_stats['errors']}")
    logger.info(f"{'='*60}")

    db_stats = db.get_statistics()
    logger.info("\nDatabase Statistics:")
    logger.info(f"Total Invoices: {db_stats['total_invoices']}")
    logger.info(f"Invoices by Company: {db_stats['invoices_by_company']}")
    logger.info(f"Synced Invoices: {db_stats['synced_invoices']}")
    logger.info(f"{'='*60}")

    db.close()
    return overall_stats


if __name__ == '__main__':
    import sys

    try:
        logger.info("=" * 60)
        logger.info("ARASAN GAS - INVOICE FETCH")
        logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        from_date = None
        to_date = None

        if len(sys.argv) >= 2:
            from_date = sys.argv[1]
            logger.info(f"Using command-line from_date: {from_date}")
        elif config.INVOICE_FETCH_START_DATE:
            from_date = config.INVOICE_FETCH_START_DATE
            logger.info(f"Using configured INVOICE_FETCH_START_DATE: {from_date}")

        if len(sys.argv) >= 3:
            to_date = sys.argv[2]
            logger.info(f"Using command-line to_date: {to_date}")

        fetch_invoices_from_all_companies(from_date, to_date)
        logger.info("\nInvoice fetch completed successfully")

    except Exception as e:
        logger.error(f"\nInvoice fetch failed: {e}", exc_info=True)
        exit(1)

