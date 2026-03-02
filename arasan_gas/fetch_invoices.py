"""
Fetch invoices from multiple Tally companies with enhanced change detection.
- Stores complete Tally data (voucher, ledger, stock items) as JSON
- Computes payload hash for robust change detection (like CO_middleware)
- Supports soft delete tracking for deleted invoices
- Filters by config start date (from-date onward) and delivery information
"""
import logging
from datetime import datetime

from config import config
from db import Database, json_dumps, json_loads, sha256_text
from tally_client import TallyClient

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
    """Return True if voucher_date (YYYYMMDD) is on/after from_date.
    Tally ERP9 Collection does not reliably respect SVFROMDATE/SVTODATE, so
    we must filter in Python after fetching all vouchers.
    """
    vd = str(invoice.get('voucher_date', '') or '').replace('-', '').strip()
    if not vd or len(vd) != 8:
        return True  # unknown date — include and let DB decide
    return vd >= from_date


def _has_delivery_info(invoice):
    """Return True only if this Tally invoice contains delivery information.
    Only invoices with a vehicle number, consignee address, delivery address,
    order number, or 'Other References=Delivery' correspond to a Delivery Challan
    in Catalytics and should be synced.
    Plain sales invoices without any delivery data are skipped.
    """
    raw = invoice.get('raw_voucher', {}) or {}

    # ── Dispatch / vehicle fields ─────────────────────────────────────────────
    if raw.get('DISPATCHEDTHROUGH') or raw.get('MOTORVEHICLENO') or raw.get('BASICSHIPPEDBY'):
        return True

    # ── Delivery / consignee address from normalised invoice ─────────────────
    if invoice.get('delivery_address'):
        return True

    # ── Consignee block from raw voucher ─────────────────────────────────────
    consignee = raw.get('CONSIGNEE') or {}
    if isinstance(consignee, dict) and (consignee.get('ADDRESS') or consignee.get('NAME')):
        return True

    # ── Order No(s) field (PARTYORDERNO) ─────────────────────────────────────
    # Invoice has a customer order number → linked to a DC
    if raw.get('PARTYORDERNO'):
        return True

    # ── PO Number / Reference fields ─────────────────────────────────────────
    if raw.get('PONUMBER') or raw.get('REFERENCE') or raw.get('VOUCHERREFERENCE'):
        return True

    # ── Other References = "Delivery" ────────────────────────────────────────
    # Tally "Other References" field set to "Delivery" explicitly marks this as a DC
    other_ref = str(raw.get('OTHERREFERENCE') or raw.get('VOUCHERREFERENCE') or '').strip().lower()
    if other_ref in ('delivery', 'dc', 'delivery challan', 'dispatch'):
        return True

    # ── Terms of delivery / narration hint ───────────────────────────────────
    if raw.get('TERMSOFDELIVERY') or raw.get('NARRATION'):
        narration = str(raw.get('NARRATION', '')).lower()
        if 'delivery' in narration or 'dispatch' in narration or 'dc' in narration or 'order' in narration:
            return True

    return False



def fetch_invoices_from_all_companies(from_date=None, to_date=None):
    """
    Fetch invoices from all active Tally companies.

    Args:
        from_date: Start date (YYYYMMDD), defaults to config or today.
        to_date: End date (YYYYMMDD), optional override. If not provided,
                 a far-future date is used so effective behavior is
                 "from start date onward".
    """
    # Always re-read .env so changes to INVOICE_FETCH_START_DATE
    # take effect without restarting dashboard.py
    try:
        config.reload_from_env()
    except AttributeError:
        pass  # Older dashboard instance — use cached config values

    if not from_date:
        from_date = config.INVOICE_FETCH_START_DATE or datetime.now().strftime('%Y%m%d')

    if not to_date:
        to_date = '20991231'

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
    logger.info(f"Date filter: from {from_date} onward (query upper bound: {to_date})")
    logger.info(f"Active companies: {', '.join(active_companies)}")

    overall_stats = {
        'total_fetched': 0,
        'skipped_date': 0,
        'skipped_no_delivery': 0,
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
            invoices = tally.get_sales_invoices(company_name, from_date, to_date)
            overall_stats['total_fetched'] += len(invoices)

            if not invoices:
                logger.info(f"No invoices found for {company_name} in date range")
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
                    logger.info(
                        f"[DATE SKIP] #{voucher_no} ({company_name}): "
                        f"date {invoice.get('voucher_date')} before start date {from_date}"
                    )
                    overall_stats['skipped_date'] += 1
                    continue

                # --- Delivery filter: only invoices with delivery information ---
                if not _has_delivery_info(invoice):
                    logger.info(
                        f"[NO DELIVERY] #{voucher_no} ({company_name}): "
                        f"no vehicle/consignee/delivery address — skipping"
                    )
                    overall_stats['skipped_no_delivery'] += 1
                    continue

                try:
                    # Prepare invoice data
                    items_json = json_dumps(invoice.get('items', []))
                    full_voucher_payload = _build_full_voucher_payload(invoice)
                    data_json = json_dumps(full_voucher_payload)

                    # ── Fetch ledger data for customer (cached per company+name) ──
                    ledger_cache_key = f"{company_name}::{customer_name}"
                    if ledger_cache_key not in ledger_cache:
                        try:
                            ld = tally.get_ledger_by_name(company_name, customer_name)
                            ledger_cache[ledger_cache_key] = ld
                        except Exception as e:
                            logger.debug(f"Could not fetch ledger for '{customer_name}': {e}")
                            ledger_cache[ledger_cache_key] = None
                    ledger_data = ledger_cache[ledger_cache_key]
                    ledger_data_json = json_dumps(ledger_data) if ledger_data else None

                    # ── Fetch stock items (cached per company+item_name) ──────────
                    stock_items_map = {}
                    inventory_items = invoice.get('items', [])
                    for item in inventory_items:
                        item_name = item.get('item_name', '')
                        if not item_name:
                            continue
                        stock_cache_key = f"{company_name}::{item_name}"
                        if stock_cache_key not in stock_cache:
                            try:
                                sd = tally.get_stock_item_by_name(company_name, item_name)
                                stock_cache[stock_cache_key] = sd
                            except Exception as e:
                                logger.debug(f"Could not fetch stock item '{item_name}': {e}")
                                stock_cache[stock_cache_key] = None
                        if stock_cache[stock_cache_key]:
                            stock_items_map[item_name] = stock_cache[stock_cache_key]

                    stock_items_json = json_dumps(stock_items_map) if stock_items_map else None

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
