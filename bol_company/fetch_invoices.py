"""
Fetch invoices from multiple Tally companies with enhanced change detection.
- Stores complete Tally data (voucher, ledger, stock items) as JSON
- Computes payload hash for robust change detection (like CO_middleware)
- Supports soft delete tracking for deleted invoices
- Filters by config start date (from-date onward) and delivery information
"""
import logging
from pathlib import Path
from datetime import datetime, timedelta

from config import config, BASE_DIR
from db import Database, json_dumps, json_loads, sha256_text
from tally_client import TallyClient
from fetch_products import parse_stock_item_name, canonical_unit_name

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
    """Return True if voucher_date (YYYYMMDD) is on/after from_date."""
    vd = str(invoice.get('voucher_date', '') or '').replace('-', '').strip()
    if not vd or len(vd) != 8:
        return True
    return vd >= from_date


def _has_delivery_info(invoice):
    """
    Mandatory check: Other Reference (BASICORDERREF / OTHERREFERENCE) must indicate
    delivery or customer pickup.

    Accepted values:
      D / delivery / delivery challan / dispatch  → Delivery type
      C / customer pickup / pickup / self pickup  → Customer Pickup type

    Returns True only when Other Reference contains one of these values.
    If the field is empty or has a different value, returns False (invoice skipped).
    """
    raw = invoice.get('raw_voucher', {}) or {}
    other_ref = str(raw.get('BASICORDERREF') or raw.get('OTHERREFERENCE') or '').strip()

    if not other_ref:
        return False

    other_ref_lower = other_ref.lower()
    other_ref_norm = ' '.join(other_ref_lower.split())
    other_ref_compact = other_ref_norm.replace(' ', '')

    # D = Delivery, C = Customer Pickup (exact single-char match)
    if other_ref_norm == 'd' or other_ref_norm == 'c':
        return True

    # Delivery keywords
    if any(kw in other_ref_norm for kw in ('delivery', 'dispatch')):
        return True
    if 'deliverychallan' in other_ref_compact:
        return True

    # Customer Pickup keywords
    if any(kw in other_ref_norm for kw in ('customer pickup', 'pickup', 'self pickup')):
        return True
    if 'customerpickup' in other_ref_compact:
        return True

    return False


def _normalize_name_key(value):
    return str(value or '').replace(' ', '').strip().lower()


def _auto_fetch_customer(db, tally, company_name, customer_name):
    """Auto-fetch a single customer from Tally and save to SQLite.
    Returns the customer data_json dict on success, None on failure."""
    import json as _json
    from tally_client import get_ledger_by_name

    try:
        ledger = get_ledger_by_name(company_name, customer_name, tally.url)
        if not ledger:
            logger.warning(f"[AUTO-FETCH] Customer '{customer_name}' not found in Tally")
            return None

        guid = ledger.get('GUID') or ledger.get('MASTERID') or ''
        name = (ledger.get('NAME') or customer_name).strip()
        normalized_name = ' '.join(name.split())

        customer_data = {
            'tally_guid': str(guid).strip(),
            'name': normalized_name,
            'tally_company': company_name,
            'gstin': (ledger.get('GSTIN') or ledger.get('PARTYGSTIN') or '').lstrip(':'),
            'pan': ledger.get('INCOMETAXNUMBER') or ledger.get('PANNUMBER') or '',
            'address': ', '.join(ledger.get('ADDRESSES', [])) if ledger.get('ADDRESSES') else '',
            'state': ledger.get('STATENAME') or '',
            'city': '',
            'pincode': ledger.get('PINCODE') or '',
            'phone': ledger.get('MOBILE') or ledger.get('LEDGERMOBILE') or '',
            'email': ledger.get('EMAIL') or ledger.get('LEDGEREMAIL') or '',
        }
        # Simple dict for data_json (same format as fetch_customers)
        simple_data = {
            'guid': customer_data['tally_guid'],
            'name': customer_data['name'],
            'parent_group': ledger.get('PARENT') or '',
            'gstin': customer_data['gstin'],
            'pan': customer_data['pan'],
            'address': customer_data['address'],
            'state': customer_data['state'],
            'city': '',
            'pincode': customer_data['pincode'],
            'phone': customer_data['phone'],
            'email': customer_data['email'],
        }
        customer_data['data_json'] = _json.dumps(simple_data)

        # GUID-based: update if exists, insert if new
        existing = db.customer_exists_by_guid(customer_data['tally_guid']) if customer_data['tally_guid'] else None
        if existing:
            db.update_customer(existing['id'], customer_data)
            logger.info(f"[AUTO-FETCH] Customer '{normalized_name}' updated in SQLite (GUID: {customer_data['tally_guid']})")
        else:
            db.insert_customer(customer_data)
            logger.info(f"[AUTO-FETCH] Customer '{normalized_name}' saved to SQLite (GUID: {customer_data['tally_guid']})")

        return simple_data

    except Exception as e:
        logger.error(f"[AUTO-FETCH] Failed to fetch customer '{customer_name}': {e}")
        return None


def _auto_fetch_product(db, tally, company_name, product_name):
    """Auto-fetch a single product from Tally and save to SQLite.
    Returns the product data_json dict on success, None on failure."""
    import json as _json
    from tally_client import get_stock_item_by_name

    try:
        stock = get_stock_item_by_name(company_name, product_name, tally.url)
        if not stock:
            logger.warning(f"[AUTO-FETCH] Product '{product_name}' not found in Tally")
            return None

        guid = stock.get('GUID') or stock.get('MASTERID') or ''
        name = (stock.get('NAME') or product_name).strip()

        # Parse product type from name
        parsed = parse_stock_item_name(name)
        if not parsed:
            parsed = {
                'product_master_name': name,
                'unit_name': canonical_unit_name('cubic'),
                'variant_name': '7',
                'product_type_code': 'CYL',
                'product_type_name': 'CYLINDER',
                'canonical_name': f'{name} (CYL)',
            }

        # Simple dict for data_json (same format as fetch_products / TallyClient.get_products)
        simple_data = {
            'guid': str(guid).strip(),
            'name': name,
            'hsn_code': stock.get('HSNCODE') or '',
            'unit': stock.get('BASEUNITS') or '',
            'rate': 0.0,
            'gst_applicable': '',
            'gst_rate': stock.get('GST_RATE') or 0.0,
            'igst_rate': stock.get('IGST_RATE') or 0.0,
            'cgst_rate': stock.get('CGST_RATE') or 0.0,
            'sgst_rate': stock.get('SGST_RATE') or 0.0,
            'description': stock.get('PARENT') or '',
        }

        product_data = {
            'tally_guid': simple_data['guid'],
            'name': name,
            'name_canonical': parsed['canonical_name'],
            'tally_company': company_name,
            'hsn_code': simple_data['hsn_code'],
            'unit': simple_data['unit'],
            'rate': simple_data['rate'],
            'description': simple_data['description'],
            'data_json': _json.dumps(simple_data),
            'product_master_name': parsed['product_master_name'],
            'variant_name': parsed['variant_name'],
            'unit_name': parsed['unit_name'],
            'product_type_code': parsed['product_type_code'],
            'product_type_name': parsed['product_type_name'],
            'gst_applicable': simple_data['gst_applicable'],
            'gst_rate': simple_data['gst_rate'],
            'igst_rate': simple_data['igst_rate'],
            'cgst_rate': simple_data['cgst_rate'],
            'sgst_rate': simple_data['sgst_rate'],
        }

        # GUID-based: update if exists, insert if new
        existing = db.product_exists_by_guid(product_data['tally_guid']) if product_data['tally_guid'] else None
        if existing:
            db.update_product(existing['id'], product_data)
            logger.info(f"[AUTO-FETCH] Product '{name}' updated in SQLite (GUID: {product_data['tally_guid']})")
        else:
            db.insert_product(product_data)
            logger.info(f"[AUTO-FETCH] Product '{name}' saved to SQLite (GUID: {product_data['tally_guid']})")

        return simple_data

    except Exception as e:
        logger.error(f"[AUTO-FETCH] Failed to fetch product '{product_name}': {e}")
        return None


def _process_invoice_batch(db, tally, company_name, invoices, from_date, to_date, ledger_cache, stock_cache, overall_stats):
    """Process a batch of invoices and save to database."""
    overall_stats['total_fetched'] += len(invoices)

    for invoice in invoices:
        voucher_no = invoice.get('voucher_no', '')
        customer_name = invoice.get('customer_name', '')
        voucher_date = invoice.get('voucher_date', '')

        if not voucher_no or not customer_name:
            logger.warning(
                f"[SKIP:EMPTY] Invoice skipped — "
                f"voucher_no={'(empty)' if not voucher_no else voucher_no}, "
                f"customer={'(empty)' if not customer_name else customer_name}"
            )
            continue

        if not _invoice_in_date_range(invoice, from_date, to_date):
            logger.info(
                f"[SKIP:DATE] #{voucher_no} | date={voucher_date} | "
                f"customer='{customer_name}' | reason: before from_date {from_date}"
            )
            overall_stats['skipped_date'] += 1
            continue

        if not _has_delivery_info(invoice):
            raw = invoice.get('raw_voucher', {}) or {}
            vtype = str(raw.get('VOUCHERTYPENAME') or raw.get('VOUCHERTYPE') or '').strip()
            other_ref = str(raw.get('BASICORDERREF') or raw.get('OTHERREFERENCE') or '').strip()
            logger.info(
                f"[SKIP:NO_DELIVERY] #{voucher_no} | date={voucher_date} | "
                f"customer='{customer_name}' | voucher_type='{vtype}' | "
                f"other_ref='{other_ref}'"
            )
            overall_stats['skipped_no_delivery'] += 1
            continue

        normalized_customer = _normalize_name_key(customer_name)
        ledger_cache_key = f"{company_name}::{normalized_customer}"
        if ledger_cache_key not in ledger_cache:
            try:
                row = db.query("SELECT name, data_json FROM customers WHERE lower(replace(name, ' ', '')) = ?", (normalized_customer,))
                ledger_cache[ledger_cache_key] = json_loads(row['data_json']) if (row and row['data_json']) else None
            except Exception:
                ledger_cache[ledger_cache_key] = None

        ledger_data = ledger_cache[ledger_cache_key]
        if not ledger_data:
            # Auto-fetch customer from Tally and save to SQLite
            logger.info(
                f"[AUTO-FETCH:CUSTOMER] #{voucher_no} | customer='{customer_name}' "
                f"not in SQLite — fetching from Tally..."
            )
            fetched = _auto_fetch_customer(db, tally, company_name, customer_name)
            if fetched:
                ledger_cache[ledger_cache_key] = fetched
                ledger_data = fetched
                overall_stats.setdefault('auto_fetched_customers', 0)
                overall_stats['auto_fetched_customers'] += 1
            else:
                logger.warning(
                    f"[SKIP:MISSING_CUSTOMER] #{voucher_no} | date={voucher_date} | "
                    f"customer='{customer_name}' | reason: not found in SQLite or Tally"
                )
                overall_stats['skipped_missing_customer'] += 1
                continue

        inventory_items = invoice.get('items', [])
        stock_items_map = {}
        missing_products = []
        for item in inventory_items:
            item_name = (item.get('item_name') or '').strip()
            normalized_item_name = _normalize_name_key(item_name)
            stock_cache_key = f"{company_name}::{normalized_item_name}"
            if stock_cache_key not in stock_cache:
                try:
                    row = db.query("SELECT name, data_json FROM products WHERE lower(replace(name, ' ', '')) = ?", (normalized_item_name,))
                    stock_cache[stock_cache_key] = json_loads(row['data_json']) if (row and row['data_json']) else None
                except Exception:
                    stock_cache[stock_cache_key] = None

            if stock_cache.get(stock_cache_key):
                stock_items_map[item_name] = stock_cache[stock_cache_key]
            else:
                missing_products.append(item_name)

        # Auto-fetch missing products from Tally
        if missing_products:
            still_missing = []
            for mp_name in missing_products:
                logger.info(
                    f"[AUTO-FETCH:PRODUCT] #{voucher_no} | product='{mp_name}' "
                    f"not in SQLite — fetching from Tally..."
                )
                fetched = _auto_fetch_product(db, tally, company_name, mp_name)
                if fetched:
                    normalized_mp = _normalize_name_key(mp_name)
                    cache_key = f"{company_name}::{normalized_mp}"
                    stock_cache[cache_key] = fetched
                    stock_items_map[mp_name] = fetched
                    overall_stats.setdefault('auto_fetched_products', 0)
                    overall_stats['auto_fetched_products'] += 1
                else:
                    still_missing.append(mp_name)

            if still_missing:
                logger.warning(
                    f"[SKIP:MISSING_PRODUCT] #{voucher_no} | date={voucher_date} | "
                    f"customer='{customer_name}' | missing {len(still_missing)} product(s) "
                    f"(not found in SQLite or Tally): {still_missing}"
                )
                overall_stats['skipped_missing_product'] += 1
                continue

        try:
            full_voucher_payload = _build_full_voucher_payload(invoice)
            enriched_voucher = dict(full_voucher_payload)
            if ledger_data: enriched_voucher['LEDGERDATA'] = ledger_data
            if stock_items_map: enriched_voucher['STOCKITEMS'] = stock_items_map
            
            # Enrich items with GST metadata
            inv_lines = enriched_voucher.get('INVENTORY') or []
            if isinstance(inv_lines, list):
                for inv in inv_lines:
                    if not isinstance(inv, dict): continue
                    st_name = (inv.get('STOCKITEMNAME') or inv.get('ITEMNAME') or '').strip()
                    st_data = stock_items_map.get(st_name) or {}
                    if not st_data: continue
                    for k in ('GST_RATE', 'IGST_RATE', 'CGST_RATE', 'SGST_RATE', 'HSNCODE'):
                        if k in st_data: inv[k] = st_data[k]

            data_json = json_dumps(enriched_voucher)
            payload_hash = _compute_payload_hash(full_voucher_payload, inventory_items, ledger_data, stock_items_map)

            invoice_record = {
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
                'items_json': json_dumps(inventory_items),
                'data_json': data_json,
                'payload_hash': payload_hash,
            }

            existing = db.invoice_exists(voucher_no, company_name)
            if not existing:
                db.insert_invoice(invoice_record)
                logger.info(
                    f"[NEW] #{voucher_no} | date={voucher_date} | "
                    f"customer='{customer_name}' | items={len(inventory_items)} | "
                    f"amount={invoice.get('total_amount', 0.0)}"
                )
                overall_stats['new_saved'] += 1
            elif (existing['payload_hash'] != payload_hash) or (existing['data_json'] != data_json):
                db.update_invoice(existing['id'], invoice_record)
                logger.info(
                    f"[UPDATED] #{voucher_no} | date={voucher_date} | "
                    f"customer='{customer_name}' | SQLite ID={existing['id']} | "
                    f"reason: payload changed"
                )
                overall_stats['updated_saved'] += 1
            else:
                logger.debug(
                    f"[UNCHANGED] #{voucher_no} | date={voucher_date} | "
                    f"customer='{customer_name}' | no changes"
                )
                overall_stats['already_exists'] += 1

        except Exception as e:
            logger.error(
                f"[ERROR] #{voucher_no} | date={voucher_date} | "
                f"customer='{customer_name}' | error: {e}",
                exc_info=True
            )
            overall_stats['errors'] += 1


def fetch_invoices_from_all_companies(from_date=None, to_date=None):
    """Fetch invoices from all active Tally companies in monthly batches."""
    try:
        config.reload_from_env()
    except AttributeError:
        pass

    # If no dates provided, use Day Book logic (Yesterday to Tomorrow) as requested
    if not from_date and not to_date:
        today_dt = datetime.now()
        from_date = (today_dt - timedelta(days=1)).strftime('%Y%m%d')
        to_date = (today_dt + timedelta(days=1)).strftime('%Y%m%d')
        logger.info(f"Using Day Book fetch logic (Reference: {today_dt.strftime('%Y%m%d')})")
        logger.info(f" -> From Date: {from_date} (Yesterday)")
        logger.info(f" -> To Date:   {to_date} (Tomorrow)")
    else:
        if not from_date:
            from_date = config.INVOICE_FETCH_START_DATE or datetime.now().strftime('%Y%m%d')
        if not to_date:
            to_date = '20991231'

    db = Database(config.SQLITE_DB_PATH)
    tally = TallyClient(config.TALLY_URL)
    active_companies = config.get_active_companies()

    if not active_companies:
        logger.error("No active Tally companies configured")
        return {'total_fetched': 0, 'new_saved': 0, 'updated_saved': 0, 'already_exists': 0, 'errors': 0}

    logger.info(f"Starting batched invoice fetch from {len(active_companies)} companies")
    overall_stats = {'total_fetched': 0, 'skipped_date': 0, 'skipped_no_delivery': 0, 'skipped_missing_customer': 0, 'skipped_missing_product': 0, 'new_saved': 0, 'updated_saved': 0, 'already_exists': 0, 'deleted': 0, 'errors': 0}
    
    ledger_cache = {}
    stock_cache = {}

    for company_name in active_companies:
        logger.info(f"\n{'='*60}\nProcessing: {company_name}\n{'='*60}")
        
        try:
            from_dt = datetime.strptime(from_date, '%Y%m%d')
            to_dt = datetime.strptime(to_date, '%Y%m%d') if to_date != '20991231' else datetime.now()
            
            curr_start = from_dt
            while curr_start <= to_dt:
                curr_end = curr_start + timedelta(days=29)
                if curr_end > to_dt: curr_end = to_dt
                
                s_date = curr_start.strftime('%Y%m%d')
                e_date = curr_end.strftime('%Y%m%d')
                
                logger.info(f" -> Batch: {s_date} to {e_date}")
                try:
                    invoices = tally.get_sales_invoices(company_name, from_date=s_date, to_date=e_date)
                    if invoices:
                        _process_invoice_batch(db, tally, company_name, invoices, from_date, to_date, ledger_cache, stock_cache, overall_stats)
                except Exception as b_err:
                    logger.error(f"Batch failed: {b_err}")
                
                curr_start = curr_start + timedelta(days=30)
                import time
                time.sleep(2)

        except Exception as e:
            logger.error(f"Company process failed: {e}")
            overall_stats['errors'] += 1

    logger.info(f"\n{'='*60}")
    logger.info("FINAL FETCH SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Total Fetched:       {overall_stats['total_fetched']}")
    logger.info(f"New Saved:           {overall_stats['new_saved']}")
    logger.info(f"Updated Saved:       {overall_stats['updated_saved']}")
    logger.info(f"Auto-Fetch Cust:     {overall_stats.get('auto_fetched_customers', 0)}")
    logger.info(f"Auto-Fetch Prod:     {overall_stats.get('auto_fetched_products', 0)}")
    logger.info(f"Skipped Date:        {overall_stats['skipped_date']}")
    logger.info(f"Skipped No Del:      {overall_stats['skipped_no_delivery']}")
    logger.info(f"Skipped Cust:        {overall_stats['skipped_missing_customer']}")
    logger.info(f"Skipped Prod:        {overall_stats['skipped_missing_product']}")
    logger.info(f"Already Exist:       {overall_stats['already_exists']}")
    logger.info(f"Errors:              {overall_stats['errors']}")
    logger.info(f"{'='*60}")
    
    db.close()
    return overall_stats


if __name__ == '__main__':
    import sys
    try:
        from_date = sys.argv[1] if len(sys.argv) >= 2 else None
        to_date = sys.argv[2] if len(sys.argv) >= 3 else None
        
        if not from_date and not to_date:
            logger.info("Initializing BOL - DAY BOOK FETCH (Yesterday to Tomorrow)")
        elif from_date and not to_date:
            logger.info(f"BOL - INVOICE FETCH: from {from_date} onwards")
        else:
            logger.info(f"BOL - INVOICE FETCH: {from_date} to {to_date}")

        fetch_invoices_from_all_companies(from_date, to_date)
    except Exception as e:
        logger.error(f"Fetch failed: {e}")
        exit(1)
