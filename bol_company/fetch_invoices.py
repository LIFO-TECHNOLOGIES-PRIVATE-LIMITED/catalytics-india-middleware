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
from fetch_products import parse_stock_item_name

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
    """Return True only when Other References indicates delivery/pickup."""
    raw = invoice.get('raw_voucher', {}) or {}
    vtype = str(raw.get('VOUCHERTYPENAME') or raw.get('VOUCHERTYPE') or '').strip().lower()
    if 'sale' in vtype or 'invoice' in vtype:
        return True

    other_ref = str(raw.get('BASICORDERREF') or raw.get('OTHERREFERENCE') or '').strip().lower()
    if not other_ref:
        return False

    other_ref_norm = ' '.join(other_ref.split())
    other_ref_compact = other_ref_norm.replace(' ', '')

    exact = {'dc'}
    substrings = ('delivery', 'delivery challan', 'dispatch', 'customer pickup', 'pickup', 'self pickup', 'self')

    if other_ref_norm in exact: return True
    if any(key in other_ref_norm for key in substrings): return True
    if 'customerpickup' in other_ref_compact or 'deliverychallan' in other_ref_compact: return True
    return False


def _normalize_name_key(value):
    return str(value or '').replace(' ', '').strip().lower()


def _process_invoice_batch(db, tally, company_name, invoices, from_date, to_date, ledger_cache, stock_cache, overall_stats):
    """Process a batch of invoices and save to database."""
    overall_stats['total_fetched'] += len(invoices)
    
    for invoice in invoices:
        voucher_no = invoice.get('voucher_no', '')
        customer_name = invoice.get('customer_name', '')

        if not voucher_no or not customer_name: continue

        if not _invoice_in_date_range(invoice, from_date, to_date):
            overall_stats['skipped_date'] += 1
            continue

        if not _has_delivery_info(invoice):
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

        if missing_products:
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
                overall_stats['new_saved'] += 1
            elif (existing['payload_hash'] != payload_hash) or (existing['data_json'] != data_json):
                db.update_invoice(existing['id'], invoice_record)
                overall_stats['updated_saved'] += 1
            else:
                overall_stats['already_exists'] += 1

        except Exception as e:
            logger.error(f"Failed to process invoice #{voucher_no}: {e}")
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
    logger.info(f"Total Fetched:    {overall_stats['total_fetched']}")
    logger.info(f"New Saved:        {overall_stats['new_saved']}")
    logger.info(f"Updated Saved:    {overall_stats['updated_saved']}")
    logger.info(f"Skipped Date:     {overall_stats['skipped_date']}")
    logger.info(f"Skipped No Del:   {overall_stats['skipped_no_delivery']}")
    logger.info(f"Skipped Cust:     {overall_stats['skipped_missing_customer']}")
    logger.info(f"Skipped Prod:     {overall_stats['skipped_missing_product']}")
    logger.info(f"Already Exist:    {overall_stats['already_exists']}")
    logger.info(f"Errors:           {overall_stats['errors']}")
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
