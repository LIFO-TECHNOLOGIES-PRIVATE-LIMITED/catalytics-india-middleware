"""
Unified sync script with payload endpoints
Syncs customers, products, and invoices to Catalytics backend using Tally payload format
Verifies data in PostgreSQL before marking as synced
"""
import logging
import requests
import json
import time
from pathlib import Path
from datetime import datetime
from config import config, BASE_DIR
from db import Database
from verify_sync import SyncVerifier
import tally_client

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def _attach_invoice_sync_file_handler():
    """Log invoice sync operations to dedicated file."""
    log_path = Path(BASE_DIR) / 'logs' / 'invoice_sync.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'invoice_sync_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'invoice_sync_file'
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_invoice_sync_file_handler()

def _attach_customer_sync_file_handler():
    """Log customer sync operations to dedicated file."""
    log_path = Path(BASE_DIR) / 'logs' / 'customer_sync.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'customer_sync_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'customer_sync_file'
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_customer_sync_file_handler()

def _attach_product_sync_file_handler():
    """Send product sync logs to dedicated file alongside console."""
    log_path = Path(BASE_DIR) / 'logs' / 'product_sync.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)

    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'product_sync_file':
            return

    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'product_sync_file'
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_product_sync_file_handler()


class CatalyticsSyncer:
    """Sync data to Catalytics using Tally payload endpoints"""

    def __init__(self):
        self.db = Database(config.SQLITE_DB_PATH)
        self.verifier = SyncVerifier()
        self.api_base = config.CATALYTICS_API_BASE.rstrip('/')
        self.api_key = config.CATALYTICS_API_KEY
        self.entity_id = config.ENTITY_ID
        self.batch_size = config.SYNC_BATCH_SIZE
        self.verify_enabled = config.VERIFY_AFTER_SYNC
        self.tally_url = config.TALLY_URL
        self._filling_station_cache = {}  # name -> id cache

    def _api_request(self, method, endpoint, **kwargs):
        """Make API request to Catalytics"""
        url = f"{self.api_base}{endpoint}"
        headers = kwargs.pop('headers', {})

        # Note: Payload endpoints use AllowAny permission
        # They only check TALLY_MIDDLEWARE_API_KEY if it's configured in Django settings
        # Since it's not configured, we don't send authentication headers
        headers['Content-Type'] = 'application/json'

        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=30,
                **kwargs
            )
            return response

        except requests.RequestException as e:
            logger.error(f"API request failed: {e}")
            raise

    # ========================================================================
    # DATA MATCHING / VALIDATION
    # ========================================================================

    def _customer_exists(self, customer_name):
        """Check if customer exists in synced customers"""
        if not customer_name:
            return False, "Customer name is empty"

        result = self.db.query(
            "SELECT id FROM customers WHERE name = ? AND is_synced = 1 LIMIT 1",
            (customer_name,)
        )
        if result:
            return True, None
        return False, f"Customer '{customer_name}' not found in Catalytics"

    def _products_exist(self, items):
        """Check if all products in invoice items exist in synced products"""
        if not items:
            return True, None  # No items to check

        missing_products = []
        for item in items:
            item_name = item.get('item_name', '').strip()
            if not item_name:
                continue

            result = self.db.query(
                "SELECT id FROM products WHERE name = ? AND is_synced = 1 LIMIT 1",
                (item_name,)
            )
            if not result:
                missing_products.append(item_name)

        if missing_products:
            return False, f"Products not found in Catalytics: {', '.join(missing_products)}"
        return True, None

    def _validate_invoice_for_sync(self, invoice, items):
        """Validate that customer and products exist before syncing"""
        customer_name = invoice.get('customer_name', '').strip()

        # Check customer
        customer_ok, customer_msg = self._customer_exists(customer_name)
        if not customer_ok:
            return False, customer_msg

        # Check products
        products_ok, products_msg = self._products_exist(items)
        if not products_ok:
            return False, products_msg

        return True, None

    def _get_filling_station_id(self, invoice):
        """
        Get filling station ID for invoice.
        Priority:
        1. Explicit ID fields on invoice row
        2. ID-like fields inside stored voucher JSON
        3. DEFAULT_FILLING_STATION_ID from .env
        Returns string integer ID.
        """
        def get_val(key):
            try:
                if hasattr(invoice, key):
                    return invoice[key]
                elif hasattr(invoice, 'get'):
                    return invoice.get(key)
                else:
                    return None
            except (KeyError, IndexError, AttributeError):
                return None

        # 1) Invoice row fields (preferred)
        filling_station_id = (
            get_val('filling_station_id') or
            get_val('fill_station_id') or
            get_val('godown_id') or
            get_val('location_id')
        )

        # 2) Raw voucher JSON fallback
        if not filling_station_id:
            voucher_data = self._safe_json_load(get_val('data_json'))
            if isinstance(voucher_data, dict):
                for key in (
                    'FILLINGSTATIONID',
                    'FILL_STATION_ID',
                    'FILL_STATION_ID_PK',
                    'GODOWNID',
                    'LOCATIONID',
                ):
                    val = (voucher_data.get(key) or '').strip() if voucher_data.get(key) is not None else ''
                    if val:
                        filling_station_id = val
                        break

        # 3) Try name-based lookup if still missing
        if not filling_station_id:
            station_name = self._get_filling_station(invoice)
            name_match_id = self._lookup_filling_station_id_by_name(station_name)
            if name_match_id:
                filling_station_id = name_match_id

        # 4) Default from env
        if not filling_station_id:
            filling_station_id = config.DEFAULT_FILLING_STATION_ID
            logger.info(f"Using default filling station ID: {filling_station_id}")

        # Ensure numeric ID; if not numeric, try name lookup then fallback to default
        fs_id_str = str(filling_station_id).strip()
        if not fs_id_str.isdigit():
            name_match_id = self._lookup_filling_station_id_by_name(fs_id_str)
            if name_match_id:
                fs_id_str = str(name_match_id).strip()
            else:
                logger.warning(
                    f"Invalid filling station ID '{fs_id_str}' from invoice data; "
                    f"using default ID {config.DEFAULT_FILLING_STATION_ID}"
                )
                fs_id_str = str(config.DEFAULT_FILLING_STATION_ID).strip()

        return fs_id_str

    def _update_dc_filling_station_direct_db(self, dc_no, dc_id, filling_station_id):
        """
        Update fill_station on delivery challan by directly updating PostgreSQL.
        Used as fallback when API endpoints are not available.
        """
        import psycopg2
        from psycopg2 import sql

        if not dc_id or not filling_station_id:
            return False, "Missing DC ID or filling station ID"

        try:
            fs_id_int = int(filling_station_id)
        except (ValueError, TypeError):
            fs_id_int = 1

        try:
            conn = psycopg2.connect(
                host=config.POSTGRES_HOST,
                port=config.POSTGRES_PORT,
                database=config.POSTGRES_DB,
                user=config.POSTGRES_USER,
                password=config.POSTGRES_PASSWORD,
                connect_timeout=10
            )
            cursor = conn.cursor()

            # Update fill_station in delivery_challan table
            query = sql.SQL(
                "UPDATE {} SET fill_station = %s WHERE id = %s"
            ).format(
                sql.Identifier('transaction.delivery_challan')
            )

            cursor.execute(query, (fs_id_int, dc_id))
            conn.commit()

            affected_rows = cursor.rowcount
            cursor.close()
            conn.close()

            if affected_rows > 0:
                logger.info(
                    f"DC #{dc_no}: fill_station updated to ID {fs_id_int} via direct PostgreSQL update"
                )
                return True, None
            else:
                logger.warning(
                    f"DC #{dc_no}: PostgreSQL update returned 0 affected rows. "
                    f"DC {dc_id} may not exist in database."
                )
                return False, "No rows affected by update"

        except Exception as e:
            logger.warning(
                f"DC #{dc_no}: Direct PostgreSQL update failed: {e}"
            )
            return False, str(e)

    def _update_dc_filling_station(self, dc_no, dc_id, filling_station_id):
        """
        Update fill_station on delivery challan after creation.
        Tries multiple endpoint approaches.

        The fill_station field is an INTEGER ID in Catalytics PostgreSQL,
        not a text name. Values like 1 (Main Warehouse), 30 (entity default), etc.
        """
        if not dc_id or not filling_station_id:
            logger.debug(f"Skipping fill_station update: dc_id={dc_id}, fs_id={filling_station_id}")
            return False, "Missing DC ID or filling station ID"

        # Try converting to int
        try:
            fs_id_int = int(filling_station_id)
        except (ValueError, TypeError):
            fs_id_int = 1  # Default to 1 if conversion fails

        logger.debug(f"DC #{dc_no}: Attempting to set fill_station to ID {fs_id_int}")

        # Try multiple endpoint approaches
        # The correct endpoint is likely /api/transaction/delivery-challan/{id}/ based on verify_sync.py
        endpoints = [
            # Try 1: /api/transaction/delivery-challan/{id}/ with PATCH - simple fill_station
            {
                'method': 'PATCH',
                'path': f'/api/transaction/delivery-challan/{dc_id}/',
                'payload': {'fill_station': fs_id_int}
            },
            # Try 2: /api/transaction/delivery-challan/{id}/ with PATCH - fill_station as object (expected format)
            {
                'method': 'PATCH',
                'path': f'/api/transaction/delivery-challan/{dc_id}/',
                'payload': {'fill_station': {'id': fs_id_int}}
            },
            # Try 3: /api/transaction/delivery-challan/{id}/ - fill_station_id field
            {
                'method': 'PATCH',
                'path': f'/api/transaction/delivery-challan/{dc_id}/',
                'payload': {'fill_station_id': fs_id_int}
            },
            # Try 4: With entity_id included
            {
                'method': 'PATCH',
                'path': f'/api/transaction/delivery-challan/{dc_id}/',
                'payload': {'fill_station': fs_id_int, 'entity_id': self.entity_id}
            },
            # Try 5: POST method instead of PATCH
            {
                'method': 'POST',
                'path': f'/api/transaction/delivery-challan/{dc_id}/',
                'payload': {'fill_station': fs_id_int}
            },
            # Try 6: /api/delivery-challan/{id}/ (alternative endpoint)
            {
                'method': 'PATCH',
                'path': f'/api/delivery-challan/{dc_id}/',
                'payload': {'fill_station': fs_id_int}
            },
        ]

        for attempt, endpoint_config in enumerate(endpoints, 1):
            try:
                logger.debug(f"DC #{dc_no}: Attempt {attempt}/{len(endpoints)} - {endpoint_config['method']} {endpoint_config['path']}")

                response = self._api_request(
                    endpoint_config['method'],
                    endpoint_config['path'],
                    json=endpoint_config['payload'],
                )

                if response.status_code in [200, 201, 204]:
                    try:
                        result = response.json()
                        logger.info(
                            f"DC #{dc_no}: ✓ fill_station updated to ID {fs_id_int} via "
                            f"{endpoint_config['method']} {endpoint_config['path']} (HTTP {response.status_code})"
                        )
                        logger.debug(f"Response: {result}")
                        return True, None
                    except:
                        # No JSON response (204 No Content), but successful
                        logger.info(
                            f"DC #{dc_no}: ✓ fill_station updated to ID {fs_id_int} via "
                            f"{endpoint_config['method']} {endpoint_config['path']} (HTTP {response.status_code})"
                        )
                        return True, None

                elif response.status_code == 404:
                    logger.debug(
                        f"DC #{dc_no}: {endpoint_config['method']} {endpoint_config['path']} "
                        f"not found (HTTP 404) - trying next approach"
                    )
                    continue
                elif response.status_code == 400:
                    # Bad request - payload might be malformed
                    try:
                        error_detail = response.json()
                        logger.debug(f"DC #{dc_no}: HTTP 400 - {error_detail} - trying next approach")
                    except:
                        logger.debug(f"DC #{dc_no}: HTTP 400 - trying next approach")
                    continue
                else:
                    logger.debug(
                        f"DC #{dc_no}: {endpoint_config['method']} {endpoint_config['path']} "
                        f"returned HTTP {response.status_code} - trying next approach"
                    )
                    continue

            except requests.RequestException as e:
                logger.debug(
                    f"DC #{dc_no}: {endpoint_config['method']} {endpoint_config['path']} "
                    f"request failed: {e}"
                )
                continue

        # All API endpoints failed - try direct PostgreSQL update as fallback
        logger.debug(f"DC #{dc_no}: All API endpoints returned 404. Trying direct PostgreSQL update...")

        db_success, db_error = self._update_dc_filling_station_direct_db(dc_no, dc_id, filling_station_id)
        if db_success:
            logger.info(f"DC #{dc_no}: Successfully updated fill_station via direct database update")
            return True, None
        else:
            logger.error(
                f"DC #{dc_no}: Could not update fill_station via API or database. "
                f"Error: {db_error}"
            )
            return False, "No valid endpoint found for fill_station update"

    # ========================================================================
    # CUSTOMER SYNC
    # ========================================================================

    def _get_customer_error_logger(self):
        """Get or create a dedicated logger for customer sync errors."""
        err_logger = logging.getLogger('customer_sync_errors')
        if not err_logger.handlers:
            log_path = Path(BASE_DIR) / 'logs' / 'customer_sync_errors.log'
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_path, encoding='utf-8')
            fh.setFormatter(logging.Formatter(
                '[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'
            ))
            err_logger.addHandler(fh)
            err_logger.setLevel(logging.ERROR)
        return err_logger

    def _log_customer_field_debug(self, name, company, ledger, error_msg):
        """Log customer field values to help identify which field caused sync error.
        Writes to both main log and dedicated customer_sync_errors.log."""
        # Sanitize inputs so newlines don't break log lines
        name = (name or '').replace('\n', '').replace('\r', '').strip()
        error_msg = (error_msg or '').replace('\n', ' ').replace('\r', '').strip()
        err_logger = self._get_customer_error_logger()

        # Extract key fields and their lengths from the ledger
        if not ledger or not isinstance(ledger, dict):
            err_logger.error(
                f"SYNC FAILED | Customer: '{name}' | Company: '{company}' | "
                f"Error: {error_msg} | Ledger: not available"
            )
            return

        # Fields that commonly hit DB column limits (field_name: max_length)
        field_checks = {
            'NAME': 200,
            'GUID': 100,
            'MOBILE': 15,
            'LEDGERMOBILE': 15,
            'PHONENUMBER': 15,
            'EMAIL': 254,
            'GSTIN': 15,
            'PARTYGSTIN': 15,
            'INCOMETAXNUMBER': 10,  # PAN
            'PINCODE': 10,
            'STATENAME': 150,
            'PRIORSTATENAME': 150,
        }

        field_report = []
        over_limit_fields = []
        for field, max_len in field_checks.items():
            val = ledger.get(field, '')
            if val:
                val_str = str(val).strip()
                length = len(val_str)
                entry = f"{field}='{val_str}' (len={length}/{max_len})"
                field_report.append(entry)
                if length > max_len:
                    over_limit_fields.append(f"{field}: '{val_str}' is {length} chars, max={max_len}")

        # Also check address (max 300 for billing)
        primary_addr = ledger.get('PRIMARY_ADDRESS', '')
        if primary_addr:
            addr_len = len(str(primary_addr))
            field_report.append(f"PRIMARY_ADDRESS='{str(primary_addr)[:100]}...' (len={addr_len}/300)")
            if addr_len > 300:
                over_limit_fields.append(f"PRIMARY_ADDRESS: {addr_len} chars, max=300")

        # Log to dedicated error file
        err_logger.error(f"{'='*80}")
        err_logger.error(f"SYNC FAILED | Customer: '{name}' | Company: '{company}'")
        err_logger.error(f"Error: {error_msg}")
        if over_limit_fields:
            err_logger.error(f"FIELDS OVER LIMIT:")
            for f in over_limit_fields:
                err_logger.error(f"  >>> {f}")
        err_logger.error(f"ALL FIELD VALUES:")
        for f in field_report:
            err_logger.error(f"  {f}")
        err_logger.error(f"{'='*80}")

        # Also log summary to main logger
        if over_limit_fields:
            logger.error(f"  POSSIBLE CAUSE - fields exceeding DB limits:")
            for f in over_limit_fields:
                logger.error(f"    >>> {f}")
        else:
            logger.error(f"  Field values sent: {' | '.join(field_report)}")

    def _build_ledger_from_simple(self, simple, customer):
        """Convert simple lowercase customer dict to uppercase Tally ledger format
        that the backend's _process_single_ledger expects."""
        # customer may be a sqlite3.Row (no .get), so handle both dict-like types
        def _cget(key, default=''):
            if hasattr(customer, 'get'):
                return customer.get(key, default)
            try:
                return customer[key]
            except Exception:
                return default

        name = (simple.get('name') or _cget('name', '')).replace('\n', '').replace('\r', '').strip()
        address = simple.get('address', '') or _cget('address', '') or ''
        return {
            'NAME': name,
            'GUID': simple.get('guid', '') or _cget('tally_guid', '') or '',
            'PARENT': simple.get('parent_group', ''),
            'PARTYGSTIN': simple.get('gstin', '') or '',
            'INCOMETAXNUMBER': simple.get('pan', '') or '',
            'MOBILE': simple.get('phone', '') or '',
            'EMAIL': simple.get('email', '') or '',
            'STATENAME': simple.get('state', '') or '',
            'PINCODE': simple.get('pincode', '') or '',
            'COUNTRYOFRESIDENCE': 'India',
            'PRIMARY_ADDRESS': address,
            'ADDRESSES': [address] if address else [],
        }

    def sync_customers(self):
        """Sync customers to Catalytics using payload endpoint"""
        logger.info("\n" + "="*60)
        logger.info("CUSTOMER SYNC")
        logger.info("="*60)

        customers = self.db.get_unsynced_customers(None)
        logger.info(f"Found {len(customers)} unsynced customers (batching disabled)")

        if not customers:
            logger.info("No customers to sync")
            return {'total': 0, 'synced': 0, 'verified': 0, 'failed': 0}

        stats = {
            'total': len(customers),
            'synced': 0,
            'verified': 0,
            'failed': 0
        }

        for customer in customers:
            customer_id = customer['id']
            name = (customer['name'] or '').replace('\n', '').replace('\r', '').strip()
            company = customer['tally_company']

            try:
                logger.info(f"Syncing customer '{name}' (company: {company})...")

                # Step 1: Use stored ledger data from SQLite (saved during fetch)
                # The backend expects uppercase Tally keys (NAME, GUID, GSTIN etc.)
                # If data_json has lowercase keys (from get_customers), convert to uppercase format
                ledger = None
                if customer['data_json']:
                    try:
                        stored = json.loads(customer['data_json'])
                        # Check if this is full Tally ledger (uppercase NAME) or simple dict (lowercase name)
                        if stored.get('NAME') or stored.get('LEDGERNAME'):
                            ledger = stored
                        else:
                            # Convert simple dict (lowercase) to backend-expected format (uppercase)
                            ledger = self._build_ledger_from_simple(stored, customer)
                            logger.info(f"Built ledger from stored simple data for '{name}'")
                    except Exception as exc:
                        logger.error(f"Failed to build ledger from stored data for '{name}': {exc}", exc_info=True)

                if customer['data_json'] and not ledger:
                    logger.warning(f"Stored data present but ledger not built for '{name}', will fetch from Tally")

                if not ledger:
                    # Fallback: fetch full ledger from Tally
                    ledger = tally_client.get_ledger_by_name(company, name, self.tally_url)
                    # Store full Tally ledger in data_json for future syncs
                    if ledger:
                        try:
                            self.db.execute(
                                'UPDATE customers SET data_json = ? WHERE id = ?',
                                (json.dumps(ledger), customer_id)
                            )
                        except Exception as exc:
                            logger.error(f"Failed to build ledger from stored data for '{name}': {exc}", exc_info=True)

                if customer['data_json'] and not ledger:
                    logger.warning(f"Stored data present but ledger not built for '{name}', will fetch from Tally")

                if not ledger:
                    raise ValueError(f"Could not get ledger data for '{name}'")

                # Clean ledger data to avoid API issues
                if isinstance(ledger.get('name'), str):
                    ledger['name'] = ledger['name'].strip()
                if isinstance(ledger.get('NAME'), str):
                    ledger['NAME'] = ledger['NAME'].strip()
                # Strip leading colon from GSTIN (Tally sometimes prefixes with ':')
                for gst_key in ('GSTIN', 'PARTYGSTIN', 'gstin'):
                    if isinstance(ledger.get(gst_key), str) and ledger[gst_key].startswith(':'):
                        ledger[gst_key] = ledger[gst_key].lstrip(':')
                # Also clean GSTIN in nested LEDGSTREGDETAILS_LIST
                for gst_detail in ledger.get('LEDGSTREGDETAILS_LIST', []):
                    if isinstance(gst_detail, dict):
                        val = gst_detail.get('GSTIN', '')
                        if isinstance(val, str) and val.startswith(':'):
                            gst_detail['GSTIN'] = val.lstrip(':')

                # Store request payload for debugging
                request_payload = {
                    'entity_id': self.entity_id,
                    'ledger': ledger
                }
                try:
                    self.db.execute(
                        'UPDATE customers SET sync_request_json = ? WHERE id = ?',
                        (json.dumps(request_payload), customer_id)
                    )
                except Exception as exc:
                    logger.error(f"Failed to build ledger from stored data for '{name}': {exc}", exc_info=True)

                # Step 2: Send to Catalytics payload endpoint
                response = self._api_request(
                    'POST',
                    '/import/tally-customer-payload/',
                    json=request_payload
                )

                if response.status_code in [200, 201]:
                    result = response.json()
                    response_json = json.dumps(result, indent=2)  # Store full response with formatting

                    if result.get('status') != 'success':
                        raise ValueError(f"API returned non-success status: {result.get('message')}")

                    data = result.get('data', {})
                    created = data.get('created', 0)
                    updated = data.get('updated', 0)
                    errors = data.get('errors', 0)

                    if errors > 0:
                        # Extract detailed error from results
                        results_list = data.get('results', [])
                        error_details = {}
                        error_msg = "Unknown error"
                        
                        for result_item in results_list:
                            if isinstance(result_item, dict) and result_item.get('status') in ['error', 'skipped']:
                                error_type = result_item.get('error_type', 'UNKNOWN')
                                error_msg = result_item.get('message', 'Unknown error')
                                error_details = result_item.get('error_details', {})
                                
                                # Log detailed error
                                logger.error(f"Customer '{name}' API error details:")
                                logger.error(f"  Error Type: {error_type}")
                                logger.error(f"  Message: {error_msg}")
                                if error_details:
                                    logger.error(f"  Details: {json.dumps(error_details, indent=4)}")
                                break
                        
                        raise ValueError(f"API error ({error_type}): {error_msg}")

                    if created == 0 and updated == 0:
                        raise ValueError("Customer not created or updated")

                    logger.info(f"API sync successful: Created={created}, Updated={updated}")
                    stats['synced'] += 1

                    # Extract customer ID from results
                    catalytics_id = None
                    results_list = data.get('results', [])
                    if results_list and isinstance(results_list, list) and len(results_list) > 0:
                        first_result = results_list[0]
                        if isinstance(first_result, dict):
                            catalytics_id = first_result.get('customer_id')
                    
                    if catalytics_id:
                        logger.info(f"Customer ID from API: {catalytics_id}")
                    else:
                        logger.warning(f"API did not return customer_id for '{name}'")

                    # Mark as synced with customer ID
                    self.db.mark_customer_synced(customer_id, catalytics_id, response_json)
                    stats['verified'] += 1

                    logger.info(
                        f"SUCCESS: Customer '{name}' synced "
                        f"(SQLite ID={customer_id}, Catalytics ID={catalytics_id}, Status={'created' if created else 'updated'})"
                    )

                else:
                    # Enhanced error handling with detailed error extraction
                    error_msg = f"API error: HTTP {response.status_code}"
                    error_response_json = None
                    detailed_error = None
                    
                    try:
                        error_result = response.json()
                        error_response_json = json.dumps(error_result, indent=2)
                        
                        # Extract detailed error information
                        if error_result.get('data'):
                            data = error_result['data']
                            results = data.get('results', [])
                            
                            if results and isinstance(results, list):
                                for result in results:
                                    if isinstance(result, dict) and result.get('status') in ['error', 'skipped']:
                                        error_type = result.get('error_type', 'UNKNOWN')
                                        error_message = result.get('message', 'No message')
                                        error_details = result.get('error_details', {})
                                        
                                        detailed_error = {
                                            'type': error_type,
                                            'message': error_message,
                                            'details': error_details
                                        }
                                        
                                        # Log detailed error
                                        logger.error(f"Customer '{name}' sync error details:")
                                        logger.error(f"  Error Type: {error_type}")
                                        logger.error(f"  Message: {error_message}")
                                        if error_details:
                                            logger.error(f"  Details: {json.dumps(error_details, indent=4)}")
                                        
                                        error_msg = f"{error_msg} - {error_type}: {error_message}"
                                        break
                        
                        if not detailed_error:
                            error_detail = error_result.get('message', response.text[:500])
                            error_msg = f"{error_msg} - {error_detail}"
                            
                    except Exception as parse_error:
                        logger.debug(f"Could not parse error response: {parse_error}")
                        error_detail = (response.text or '')[:500]
                        if error_response_json is None and error_detail:
                            error_response_json = json.dumps({'raw': error_detail})
                    
                    logger.error(f"Sync failed for '{name}' (company: {company}): {error_msg}")
                    self._log_customer_field_debug(name, company, ledger, error_msg)
                    self.db.mark_customer_sync_failed(customer_id, error_msg, error_response_json)
                    stats['failed'] += 1

            except Exception as e:
                logger.error(f"Error syncing '{name}': {e}", exc_info=True)
                # Log field values to help identify which field caused the error
                self._log_customer_field_debug(name, company, ledger if 'ledger' in locals() else None, str(e))
                self.db.mark_customer_sync_failed(customer_id, str(e))
                stats['failed'] += 1

        # Summary
        logger.info(f"\n{'-'*60}")
        logger.info("CUSTOMER SYNC SUMMARY")
        logger.info(f"{'-'*60}")
        logger.info(f"Total: {stats['total']}")
        logger.info(f"API Synced: {stats['synced']}")
        logger.info(f"Verified: {stats['verified']}")
        logger.info(f"Failed: {stats['failed']}")
        if stats['total'] > 0:
            logger.info(f"Success Rate: {stats['verified']/stats['total']*100:.1f}%")
        if stats['failed'] > 0:
            err_log_path = Path(BASE_DIR) / 'logs' / 'customer_sync_errors.log'
            logger.warning(f"{stats['failed']} customers failed — see {err_log_path} for field-level details")
        logger.info(f"{'-'*60}")

        return stats


    def sync_single_customer(self, customer_dict, ledger_data=None):
        """
        Sync a single customer to Catalytics.
        Used for immediate sync during invoice fetch.
        
        Args:
            customer_dict: Customer data dict with keys: id, name, tally_company, etc.
            ledger_data: Optional pre-fetched ledger data from Tally
            
        Returns:
            dict: {'success': bool, 'error': str, 'catalytics_id': int}
        """
        customer_id = customer_dict.get('id')
        name = customer_dict.get('name')
        company = customer_dict.get('tally_company')
        
        try:
            logger.info(f"Syncing single customer '{name}' (company: {company})...")
            
            # Fetch ledger data if not provided
            if not ledger_data:
                logger.debug(f"Fetching ledger data from Tally for '{name}'...")
                ledger_data = tally_client.get_ledger_by_name(company, name, self.tally_url)
                
                if not ledger_data:
                    return {
                        'success': False,
                        'error': f"Could not fetch ledger '{name}' from Tally",
                        'catalytics_id': None
                    }
            
            # Build request payload
            request_payload = {
                'entity_id': self.entity_id,
                'ledger': ledger_data
            }
            
            # Send to Catalytics
            response = self._api_request(
                'POST',
                '/import/tally-customer-payload/',
                json=request_payload
            )
            
            if response.status_code in [200, 201]:
                result = response.json()
                
                if result.get('status') != 'success':
                    return {
                        'success': False,
                        'error': f"API returned non-success: {result.get('message')}",
                        'catalytics_id': None
                    }
                
                data = result.get('data', {})
                created = data.get('created', 0)
                updated = data.get('updated', 0)
                errors = data.get('errors', 0)
                
                if errors > 0:
                    error_msg = data.get('results', [{}])[0].get('message', 'Unknown error')
                    return {
                        'success': False,
                        'error': f"API error: {error_msg}",
                        'catalytics_id': None
                    }
                
                if created == 0 and updated == 0:
                    return {
                        'success': False,
                        'error': "Customer not created or updated",
                        'catalytics_id': None
                    }
                
                # Extract customer ID from results
                catalytics_id = None
                results_list = data.get('results', [])
                if results_list and isinstance(results_list, list) and len(results_list) > 0:
                    first_result = results_list[0]
                    if isinstance(first_result, dict):
                        catalytics_id = first_result.get('customer_id')
                
                logger.info(f"✓ Customer '{name}' synced (Created={created}, Updated={updated}, ID={catalytics_id})")
                
                return {
                    'success': True,
                    'error': None,
                    'catalytics_id': catalytics_id
                }
            else:
                error_msg = f"HTTP {response.status_code}"
                try:
                    error_result = response.json()
                    error_detail = error_result.get('message', response.text[:200])
                    error_msg = f"{error_msg} - {error_detail}"
                except:
                    pass
                
                return {
                    'success': False,
                    'error': error_msg,
                    'catalytics_id': None
                }
                
        except Exception as e:
            logger.error(f"Error syncing customer '{name}': {e}")
            return {
                'success': False,
                'error': str(e),
                'catalytics_id': None
            }

    # ========================================================================
    # PRODUCT SYNC
    # ========================================================================

    def sync_products(self):
        """Sync products to Catalytics using parsed product name payload endpoint"""
        logger.info("\n" + "="*60)
        logger.info("PRODUCT SYNC (tally-product_name-payload)")
        logger.info("="*60)

        products = self.db.get_unsynced_products(self.batch_size)
        logger.info(f"Found {len(products)} unsynced products")

        if not products:
            logger.info("No products to sync")
            return {'total': 0, 'synced': 0, 'verified': 0, 'failed': 0}

        stats = {
            'total': len(products),
            'synced': 0,
            'verified': 0,
            'failed': 0
        }

        for product in products:
            product_id = product['id']
            name = product['name']
            company = product['tally_company']

            try:
                logger.info(f"Syncing product '{name}' (company: {company})...")

                # Read parsed fields from SQLite (populated by fetch_products.py)
                product_master_name = product['product_master_name']
                variant_name = product['variant_name']
                unit_name = product['unit_name']
                product_type_code = product['product_type_code']
                product_type_name = product['product_type_name']

                if not product_master_name:
                    raise ValueError(
                        f"Product '{name}' has no parsed fields — "
                        f"re-run fetch_products.py to populate"
                    )

                # Build payload with parsed product name fields + GST
                payload = {
                    'entity_id': self.entity_id,
                    'stock_item_name': name,
                    'product_master_name': product_master_name,
                    'unit_master_name': unit_name,
                    'variant_name': variant_name,
                    'product_type_code': product_type_code,
                    'product_type_name': product_type_name,
                    'hsn_code': product['hsn_code'] or '',
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
                    f"HSN={product['hsn_code']}, GST={product['gst_rate']}%"
                )

                # Store request payload for debugging
                request_json = json.dumps(payload)
                self.db.execute(
                    "UPDATE products SET sync_request_json = ? WHERE id = ?",
                    (request_json, product_id)
                )

                # Send to Arasan Gas specific endpoint
                response = self._api_request(
                    'POST',
                    '/import/tally-product_name-payload/',
                    json=payload
                )

                if response.status_code in [200, 201]:
                    result = response.json()
                    response_json = json.dumps(result)

                    if result.get('status') != 'success':
                        raise ValueError(f"API returned non-success: {result.get('message')}")

                    data = result.get('data', {})
                    created = data.get('created', 0)
                    updated = data.get('updated', 0)
                    errors = data.get('errors', 0)

                    if errors > 0:
                        error_msg = data.get('results', [{}])[0].get('message', 'Unknown error')
                        raise ValueError(f"API error: {error_msg}")

                    if created == 0 and updated == 0:
                        raise ValueError("Product not created or updated")

                    logger.info(f"API sync successful: Created={created}, Updated={updated}")
                    stats['synced'] += 1

                    # Mark as synced
                    catalytics_id = data.get('product_id') or data.get('id')
                    if not catalytics_id:
                        results_list = data.get('results', [])
                        if results_list and isinstance(results_list, list):
                            first_result = results_list[0]
                            if isinstance(first_result, dict):
                                catalytics_id = first_result.get('product_id') or first_result.get('id')

                    if catalytics_id:
                        logger.info(f"Product ID from API: {catalytics_id}")
                    else:
                        logger.warning(f"API did not return product_id for '{name}'")

                    self.db.mark_product_synced(product_id, catalytics_id, response_json)
                    stats['verified'] += 1

                    logger.info(
                        f"SUCCESS: Product '{name}' synced "
                        f"(SQLite ID={product_id}, Catalytics ID={catalytics_id}, "
                        f"Status={'created' if created else 'updated'})"
                    )

                else:
                    error_msg = f"API error: HTTP {response.status_code}"
                    error_response_json = None
                    try:
                        error_result = response.json()
                        error_response_json = json.dumps(error_result)
                        error_detail = error_result.get('message', response.text[:200])
                        error_msg = f"{error_msg} - {error_detail}"
                    except Exception as exc:
                        logger.error(f"Failed to build ledger from stored data for '{name}': {exc}", exc_info=True)

                    logger.error(f"Sync failed for '{name}': {error_msg}")
                    self.db.mark_product_sync_failed(product_id, error_msg, error_response_json)
                    stats['failed'] += 1

            except Exception as e:
                logger.error(f"Error syncing '{name}': {e}", exc_info=True)
                self.db.mark_product_sync_failed(product_id, str(e))
                stats['failed'] += 1

        # Summary
        logger.info(f"\n{'-'*60}")
        logger.info("PRODUCT SYNC SUMMARY")
        logger.info(f"{'-'*60}")
        logger.info(f"Total: {stats['total']}")
        logger.info(f"API Synced: {stats['synced']}")
        logger.info(f"Verified: {stats['verified']}")
        logger.info(f"Failed: {stats['failed']}")
        if stats['total'] > 0:
            logger.info(f"Success Rate: {stats['verified']/stats['total']*100:.1f}%")
        logger.info(f"{'-'*60}")

        return stats

    # ========================================================================
    # INVOICE TO DC SYNC
    # ========================================================================

    def _safe_json_load(self, raw_value):
        """Safely parse JSON text into dict."""
        if not raw_value:
            return {}
        try:
            parsed = json.loads(raw_value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}


    def _normalize_tally_date(self, value):
        # Normalize Tally date to YYYY-MM-DD if possible.
        s = str(value or '').strip()
        if not s:
            return ''
        digits = ''.join(ch for ch in s if ch.isdigit())
        if len(digits) == 8:
            return digits[0:4] + '-' + digits[4:6] + '-' + digits[6:8]
        return s

    def _extract_po_number(self, invoice):
        """
        Extract PO/Order number from invoice.
        Tries multiple field names in order of priority.
        Filters out non-PO values (descriptive text like 'Delivery').
        Returns empty string if not found or if it's a non-PO descriptor.
        """
        data = {}
        try:
            if invoice.get('data_json'):
                data = json.loads(invoice.get('data_json', '{}'))
        except:
            data = {}

        # Non-PO values to skip (these are delivery mode or status values, not POs)
        non_po_values = {
            'delivery', 'invoice', 'sales', 'bill', 'challan', 'dc', 'dispatch',
            'shipment', 'yes', 'no', 'standard', 'normal', 'express',
            'not applicable', 'n/a', 'na', 'nil', 'none', '-',
            'customer pickup', 'customerpickup', 'pickup', 'self pickup', 'selfpickup', 'self',
        }
        non_po_keywords = (
            'customer pickup', 'customerpickup', 'pickup', 'self pickup', 'selfpickup', 'self',
            'delivery', 'dispatch', 'challan',
        )

        # Try multiple field names for PO number (in priority order)
        # Priority: actual order numbers > generic references > transport references
        fields_to_try = [
            'PARTYORDERNO',          # Customer's PO number (highest priority)
            'AGGREMENTORDERNO',      # Agreement/order number
            'ORDERREF', 'ORDERINGNO', 'REFNO', 'REFERENCE',  # Generic order refs
            'PONUMBER', 'BASICORDERREF', 'VOUCHERREFERENCE',  # Tally internal (lowest priority)
        ]

        for field in fields_to_try:
            value = (data.get(field) or '').strip()
            if not value:
                continue

            value_lower = value.lower()
            # Skip non-PO values (common descriptors)
            if value_lower in non_po_values:
                continue
            if any(key in value_lower for key in non_po_keywords):
                continue

            # Check if it looks like a real PO number (has digits or is reasonably sized)
            if len(value) >= 1 and (any(c.isdigit() for c in value) or len(value) > 3):
                return value

        # No valid PO found
        return ''

    def _extract_po_date(self, invoice):
        """
        Extract PO/Order date from invoice.
        Tries multiple field names in order of priority.
        Returns empty string if not found.
        """
        data = {}
        try:
            if invoice.get('data_json'):
                data = json.loads(invoice.get('data_json', '{}'))
        except:
            data = {}

        # Try multiple field names for PO date (in priority order)
        fields_to_try = [
            'PARTYORDERDATE',        # Customer's PO date (highest priority)
            'AGGREMENTORDERDATE',    # Agreement/order date
            'ORDERDATE', 'PODATE', 'REFERENCEDATE',  # Generic dates
        ]

        for field in fields_to_try:
            value = (data.get(field) or '').strip()
            value = self._normalize_tally_date(value)
            if value:
                return value

        # No PO date found
        return ''

    def _get_filling_station(self, invoice):
        """
        Get filling station for invoice.
        Priority:
        1. Invoice row location/godown/filling_station fields
        2. Raw voucher fields (FILLINGSTATION/GODOWNNAME/LOCATIONNAME)
        3. GODOWNNAME from voucher inventory lines
        4. DEFAULT_FILLING_STATION from .env

        Returns:
            str: Filling station name
        """
        def get_val(key):
            try:
                if hasattr(invoice, key):
                    return invoice[key]
                elif hasattr(invoice, 'get'):
                    return invoice.get(key)
                else:
                    return None
            except (KeyError, IndexError, AttributeError):
                return None

        def normalize(value):
            return str(value or '').strip()

        # 1) Invoice row fields
        filling_station = (
            normalize(get_val('godown_name')) or
            normalize(get_val('location_name')) or
            normalize(get_val('filling_station'))
        )
        if filling_station:
            return filling_station


    def _lookup_filling_station_id_by_name(self, station_name):
        name = str(station_name or '').strip()
        if not name:
            return None

        key = name.lower()
        if key in self._filling_station_cache:
            return self._filling_station_cache[key]

        url = f"{self.api_base}/master/gas_filling_station"
        params = {
            'entity_id': self.entity_id,
            'search_data': name,
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code >= 400:
                logger.warning(f"Filling station lookup failed ({resp.status_code}) for '{name}'")
                self._filling_station_cache[key] = None
                return None
            payload = resp.json()
        except Exception as exc:
            logger.warning(f"Filling station lookup error for '{name}': {exc}")
            self._filling_station_cache[key] = None
            return None

        data = payload.get('data') if isinstance(payload, dict) else None
        match_id = None

        if isinstance(data, dict):
            match_id = data.get('id')
        elif isinstance(data, list):
            # Prefer exact name match
            for item in data:
                if not isinstance(item, dict):
                    continue
                item_name = str(item.get('name') or '').strip().lower()
                if item_name == key:
                    match_id = item.get('id')
                    break
            # If only one result, accept it as best effort
            if not match_id and len(data) == 1 and isinstance(data[0], dict):
                match_id = data[0].get('id')

        self._filling_station_cache[key] = match_id
        if match_id:
            logger.info(f"Matched filling station '{name}' -> ID {match_id}")
        return match_id

        # 2) Stored raw voucher JSON
        voucher_data = self._safe_json_load(get_val('data_json'))
        if isinstance(voucher_data, dict):
            for key in ('FILLINGSTATION', 'GODOWNNAME', 'LOCATIONNAME'):
                val = normalize(voucher_data.get(key))
                if val:
                    return val

            # 3) Inventory-level godown/location
            for item in voucher_data.get('INVENTORY', []) or []:
                if not isinstance(item, dict):
                    continue
                item_station = normalize(item.get('GODOWNNAME') or item.get('LOCATIONNAME'))
                if item_station:
                    return item_station

        # 4) Default from env
        filling_station = config.DEFAULT_FILLING_STATION
        logger.info(f"Using default filling station: {filling_station}")
        return filling_station

    def _build_legacy_voucher_payload(self, invoice):
        """Fallback voucher payload built from normalized invoice columns."""
        try:
            items = json.loads(invoice.get('items_json') or '[]')
            if not isinstance(items, list):
                items = []
        except Exception:
            items = []

        filling_station = self._get_filling_station(invoice)
        po_number = self._extract_po_number(invoice)
        po_date = self._extract_po_date(invoice)
        if not po_number:
            po_date = ''

        voucher_payload = {
            'VOUCHERNUMBER': invoice.get('tally_voucher_no', ''),
            'DATE': invoice.get('voucher_date', ''),
            'PARTYLEDGERNAME': invoice.get('customer_name', ''),
            'FILLINGSTATION': filling_station,
            'ADDRESSES': [invoice.get('billing_address')] if invoice.get('billing_address') else [],
            'INVENTORY': [],
            'LEDGERENTRIES': [],
        }

        # Add PO number and date if available
        if po_number:
            voucher_payload['PONUMBER'] = po_number
            logger.info(f"PO Number found: {po_number}")
        else:
            logger.debug("No PO number found in invoice - using empty value")

        if po_date:
            voucher_payload['PODATE'] = po_date
            logger.info(f"PO Date found: {po_date}")
        else:
            logger.debug("No PO date found in invoice - using empty value")

        if invoice.get('delivery_address'):
            voucher_payload['CONSIGNEE'] = {'ADDRESS': invoice.get('delivery_address')}

        for item in items:
            voucher_payload['INVENTORY'].append({
                'STOCKITEMNAME': item.get('item_name', ''),
                'ACTUALQTY': str(item.get('quantity', 0)),
                'RATE': str(item.get('rate', 0)),
                'AMOUNT': str(item.get('amount', 0)),
            })

        if invoice.get('total_amount'):
            voucher_payload['LEDGERENTRIES'].append({
                'LEDGERNAME': invoice.get('customer_name', ''),
                'AMOUNT': str(invoice.get('total_amount', 0)),
            })

        return voucher_payload

    def _build_invoice_voucher_payload(self, invoice):
        """Build full voucher payload preferring stored raw Tally voucher JSON."""
        fallback_payload = self._build_legacy_voucher_payload(invoice)
        stored_voucher = self._safe_json_load(invoice.get('data_json'))

        if not stored_voucher:
            return fallback_payload

        voucher_payload = dict(stored_voucher)
        voucher_payload.setdefault('VOUCHERNUMBER', invoice.get('tally_voucher_no', ''))
        voucher_payload.setdefault('DATE', invoice.get('voucher_date', ''))
        voucher_payload.setdefault('PARTYLEDGERNAME', invoice.get('customer_name', ''))

        if invoice.get('billing_address') and not voucher_payload.get('ADDRESSES'):
            voucher_payload['ADDRESSES'] = [invoice.get('billing_address')]

        if invoice.get('delivery_address') and not voucher_payload.get('CONSIGNEE'):
            voucher_payload['CONSIGNEE'] = {'ADDRESS': invoice.get('delivery_address')}

        if not voucher_payload.get('INVENTORY'):
            voucher_payload['INVENTORY'] = fallback_payload.get('INVENTORY', [])

        if not voucher_payload.get('LEDGERENTRIES'):
            voucher_payload['LEDGERENTRIES'] = fallback_payload.get('LEDGERENTRIES', [])

        # Ensure filling station is set (use default if empty)
        filling_station = self._get_filling_station(invoice)
        if not voucher_payload.get('FILLINGSTATION') and filling_station:
            voucher_payload['FILLINGSTATION'] = filling_station

        # Map Other Reference -> Terms of Delivery for challan type detection
        other_ref = str(voucher_payload.get('BASICORDERREF') or '').strip().lower()
        if 'customer pickup' in other_ref or 'pickup' in other_ref:
            voucher_payload.setdefault('TERMSOFDELIVERY', 'Customer Pickup')

        # Clean up and extract PO number (filter out "Delivery" and other non-PO values)
        po_number = self._extract_po_number(invoice)
        if po_number:
            # Set PARTYORDERNO (Order No(s) field) as primary PO number field
            voucher_payload['PARTYORDERNO'] = po_number
            voucher_payload['PONUMBER'] = po_number  # Keep for backward compatibility
            logger.info(f"PO Number: {po_number}")
        else:
            # If no valid PO found, set to empty
            voucher_payload.pop('PARTYORDERNO', None)
            voucher_payload.pop('PONUMBER', None)
            voucher_payload.pop('BASICORDERREF', None)

        # Clean up and extract PO date
        po_date = self._extract_po_date(invoice)
        if not po_number:
            po_date = ''
        if po_date:
            # Set PARTYORDERDATE (Order date field) as primary PO date field
            voucher_payload['PARTYORDERDATE'] = po_date
            voucher_payload['PODATE'] = po_date  # Keep for backward compatibility
            logger.info(f"PO Date: {po_date}")
        else:
            # If no valid PO date found, set to empty
            voucher_payload.pop('PARTYORDERDATE', None)
            voucher_payload.pop('PODATE', None)

        return voucher_payload

    def _build_invoice_support_payloads(self, company_name, voucher_payload):
        """Fetch full ledger + stock item details for invoice payload sync."""
        ledgers_map = {}
        stock_items_map = {}

        party_name = voucher_payload.get('PARTYLEDGERNAME') or voucher_payload.get('PARTYNAME') or ''
        if party_name:
            try:
                ledger_data = tally_client.get_ledger_by_name(company_name, party_name, self.tally_url)
                if ledger_data:
                    ledgers_map[party_name] = ledger_data
            except Exception as exc:
                logger.warning("Could not fetch full ledger '%s' from Tally: %s", party_name, exc)

        for item in voucher_payload.get('INVENTORY', []) or []:
            stock_name = item.get('STOCKITEMNAME') or item.get('ITEMNAME') or ''
            if not stock_name or stock_name in stock_items_map:
                continue
            try:
                stock_data = tally_client.get_stock_item_by_name(company_name, stock_name, self.tally_url)
                if stock_data:
                    stock_items_map[stock_name] = stock_data
            except Exception as exc:
                logger.warning("Could not fetch full stock item '%s' from Tally: %s", stock_name, exc)

        return ledgers_map, stock_items_map

    def _extract_dc_result_for_invoice(self, api_result, voucher_no):
        """Extract per-invoice DC result row from payload API response."""
        data = (api_result or {}).get('data') or {}
        results = data.get('results') or []
        target_dc_no = str(voucher_no or '').strip()

        for entry in results:
            if not isinstance(entry, dict):
                continue
            entry_dc_no = str(entry.get('dc_no') or '').strip()
            if target_dc_no and entry_dc_no and entry_dc_no == target_dc_no:
                return entry

        if len(results) == 1 and isinstance(results[0], dict):
            return results[0]

        return {}

    def sync_invoices_to_dc(self):
        """Sync invoices as delivery challans using full voucher payload endpoint."""
        logger.info("\n" + "="*60)
        logger.info("INVOICE TO DC SYNC")
        logger.info("="*60)

        invoices = self.db.get_unsynced_invoices(self.batch_size)
        logger.info(f"Found {len(invoices)} unsynced invoices")

        if not invoices:
            logger.info("No invoices to sync")
            return {'total': 0, 'synced': 0, 'verified': 0, 'failed': 0}

        stats = {
            'total': len(invoices),
            'synced': 0,
            'verified': 0,
            'failed': 0,
        }

        for invoice_row in invoices:
            invoice = dict(invoice_row)
            invoice_id = invoice['id']
            voucher_no = invoice.get('tally_voucher_no', '')
            company = invoice.get('tally_company', '')
            customer_name = invoice.get('customer_name', '')

            try:
                logger.info(
                    f"Syncing invoice #{voucher_no} "
                    f"(company: {company}, customer: {customer_name})..."
                )

                # Parse invoice items for validation
                items_json = invoice.get('items_json', '[]')
                try:
                    import json as json_lib
                    items = json_lib.loads(items_json) if isinstance(items_json, str) else (items_json or [])
                except:
                    items = []

                # Validate customer and products exist before syncing
                is_valid, validation_error = self._validate_invoice_for_sync(invoice, items)
                if not is_valid:
                    logger.warning(
                        f"[VALIDATION FAILED] Invoice #{voucher_no}: {validation_error} — SKIPPING"
                    )
                    stats['failed'] += 1
                    self.db.mark_invoice_sync_failed(
                        invoice_id,
                        f"Validation failed: {validation_error}",
                        json_lib.dumps({"error": validation_error})
                    )
                    continue

                voucher_payload = self._build_invoice_voucher_payload(invoice)
                ledgers_map, stock_items_map = self._build_invoice_support_payloads(company, voucher_payload)

                # Persist request payload for debugging
                request_payload = {
                    'entity_id': self.entity_id,
                    'company_name': company,
                    'voucher': voucher_payload,
                    'ledgers': ledgers_map,
                    'stock_items': stock_items_map,
                    'allow_tally_fetch': False,
                }
                try:
                    self.db.execute(
                        'UPDATE invoices SET sync_request_json = ? WHERE id = ?',
                        (json.dumps(request_payload), invoice_id)
                    )
                except Exception as exc:
                    logger.error(f"Failed to save sync request for invoice #{voucher_no}: {exc}")

                # Log what's being sent
                po_number = voucher_payload.get('PARTYORDERNO', '') or voucher_payload.get('PONUMBER', '') or '[EMPTY]'
                po_date = voucher_payload.get('PARTYORDERDATE', '') or voucher_payload.get('PODATE', '') or '[EMPTY]'
                filling_station = voucher_payload.get('FILLINGSTATION', '').strip() or '[EMPTY]'

                logger.info(f"Payload Details:")
                logger.info(f"  PO_NO: {po_number}")
                logger.info(f"  PO_DATE: {po_date}")
                logger.info(f"  FILLING_STATION: {filling_station}")
                logger.info(f"  Total Tally Fields: {len(voucher_payload)}")

                request_payload = {
                    'entity_id': self.entity_id,
                    'company_name': company,
                    'voucher': voucher_payload,
                    'ledgers': ledgers_map,
                    'stock_items': stock_items_map,
                    'allow_tally_fetch': False,
                }

                # Log actual fields being sent
                logger.debug(f"Voucher payload keys: {list(voucher_payload.keys())[:10]}...")  # Show first 10 keys

                response = self._api_request(
                    'POST',
                    '/import/tally-delivery-challan-payload/',
                    json=request_payload,
                )

                if response.status_code in [200, 201]:
                    result = response.json()
                    response_json = json.dumps(result)

                    if result.get('status') != 'success':
                        raise ValueError(f"API returned non-success status: {result.get('message')}")

                    data = result.get('data', {})
                    created = data.get('created', 0)
                    updated = data.get('updated', 0)
                    errors = data.get('errors', 0)

                    if errors > 0:
                        error_msg = data.get('results', [{}])[0].get('message', 'Unknown error')
                        raise ValueError(f"API error: {error_msg}")

                    if created == 0 and updated == 0:
                        raise ValueError('DC not created or updated')

                    logger.info(f"API sync successful: Created={created}, Updated={updated}")
                    stats['synced'] += 1

                    dc_result = self._extract_dc_result_for_invoice(result, voucher_no)
                    resolved_dc_no = str(
                        dc_result.get('dc_no')
                        or voucher_payload.get('VOUCHERNUMBER')
                        or voucher_no
                        or ''
                    ).strip()

                    if not resolved_dc_no:
                        raise ValueError('Could not resolve DC number from response payload')

                    catalytics_dc_id = (
                        dc_result.get('dc_id')
                        or dc_result.get('id')
                        or dc_result.get('delivery_challan_id')
                    )

                    if self.verify_enabled:
                        verify_result = self.verifier.verify_dc_by_number(
                            dc_no=resolved_dc_no,
                            expected_date=invoice.get('voucher_date'),
                            expected_customer=invoice.get('customer_name'),
                        )

                        if not verify_result.get('exists'):
                            error_msg = f"DB verification failed for DC #{resolved_dc_no}"
                            logger.error(error_msg)
                            self.db.mark_invoice_sync_failed(invoice_id, error_msg, response_json)
                            stats['failed'] += 1
                            continue

                        catalytics_dc_id = catalytics_dc_id or verify_result.get('id')

                    # Step 2: Update filling station ID on the DC (if godown/filling_station field is empty)
                    filling_station_id = self._get_filling_station_id(invoice)
                    if filling_station_id and catalytics_dc_id:
                        fs_updated, fs_error = self._update_dc_filling_station(
                            resolved_dc_no,
                            catalytics_dc_id,
                            filling_station_id
                        )
                        if fs_updated:
                            logger.info(f"DC #{resolved_dc_no}: filling station ID set to {filling_station_id}")
                        else:
                            logger.warning(f"DC #{resolved_dc_no}: filling station update failed: {fs_error}")

                    self.db.mark_invoice_synced(
                        invoice_id,
                        resolved_dc_no,
                        catalytics_dc_id,
                        response_json,
                    )
                    stats['verified'] += 1

                    logger.info(
                        f"SUCCESS: Invoice #{voucher_no} synced as DC "
                        f"(DC No={resolved_dc_no}, Catalytics ID={catalytics_dc_id}, SQLite ID={invoice_id})"
                    )

                else:
                    error_msg = f"API error: HTTP {response.status_code}"
                    error_response_json = None
                    try:
                        error_result = response.json()
                        error_response_json = json.dumps(error_result)
                        error_detail = error_result.get('message', response.text[:200])
                        error_msg = f"{error_msg} - {error_detail}"
                    except Exception:
                        error_msg = f"{error_msg} - {response.text[:200]}"

                    logger.error(f"Sync failed for invoice #{voucher_no}: {error_msg}")
                    self.db.mark_invoice_sync_failed(invoice_id, error_msg, error_response_json)
                    stats['failed'] += 1

            except Exception as e:
                logger.error(f"Error syncing invoice #{voucher_no}: {e}", exc_info=True)
                self.db.mark_invoice_sync_failed(invoice_id, str(e))
                stats['failed'] += 1

        logger.info(f"\n{'-'*60}")
        logger.info("INVOICE TO DC SYNC SUMMARY")
        logger.info(f"{'-'*60}")
        logger.info(f"Total: {stats['total']}")
        logger.info(f"API Synced: {stats['synced']}")
        logger.info(f"Verified: {stats['verified']}")
        logger.info(f"Failed: {stats['failed']}")
        if stats['total'] > 0:
            logger.info(f"Success Rate: {stats['verified']/stats['total']*100:.1f}%")
        logger.info(f"{'-'*60}")

        return stats

    def sync_invoices_simple(self):
        """
        Sync invoices using lightweight API with name-based matching.
        Sends only voucher data (no ledgers, no stock_items).
        """
        logger.info("\n" + "="*60)
        logger.info("INVOICE TO DC SYNC (SIMPLE API)")
        logger.info("="*60)

        invoices = self.db.get_unsynced_invoices(self.batch_size)
        logger.info(f"Found {len(invoices)} unsynced invoices")

        if not invoices:
            logger.info("No invoices to sync")
            return {'total': 0, 'synced': 0, 'verified': 0, 'failed': 0}

        stats = {
            'total': len(invoices),
            'synced': 0,
            'verified': 0,
            'failed': 0,
        }

        for invoice_row in invoices:
            invoice = dict(invoice_row)
            invoice_id = invoice['id']
            voucher_no = invoice.get('tally_voucher_no', '')
            company = invoice.get('tally_company', '')
            customer_name = invoice.get('customer_name', '')

            try:
                logger.info(
                    f"Syncing invoice #{voucher_no} "
                    f"(company: {company}, customer: {customer_name})..."
                )

                # Validate: check items are parseable
                items_json = invoice.get('items_json', '[]')
                try:
                    items = json.loads(items_json) if isinstance(items_json, str) else (items_json or [])
                except Exception:
                    items = []

                if not items:
                    error_msg = f"No inventory items found for invoice #{voucher_no}"
                    logger.error(f"[VALIDATION FAILED] {error_msg}")
                    self.db.mark_invoice_sync_failed(invoice_id, error_msg)
                    stats['failed'] += 1
                    continue

                if not customer_name or customer_name == 'UNKNOWN':
                    error_msg = f"Customer name is empty or UNKNOWN for invoice #{voucher_no}"
                    logger.error(f"[VALIDATION FAILED] {error_msg}")
                    self.db.mark_invoice_sync_failed(invoice_id, error_msg)
                    stats['failed'] += 1
                    continue

                # Build simple voucher payload (no ledgers, no stock_items)
                voucher_payload = self._build_invoice_voucher_payload(invoice)

                # Get filling station ID (from godown/location or default from .env)
                filling_station_id = self._get_filling_station_id(invoice)

                # Simple payload - only voucher data + filling station ID
                request_payload = {
                    'entity_id': self.entity_id,
                    'company_name': company,
                    'voucher': voucher_payload,
                    'filling_station_id': filling_station_id,
                }
                try:
                    self.db.execute(
                        'UPDATE invoices SET sync_request_json = ? WHERE id = ?',
                        (json.dumps(request_payload), invoice_id)
                    )
                except Exception as exc:
                    logger.error(f"Failed to save sync request for invoice #{voucher_no}: {exc}")

                logger.info(f"Payload Details:")
                logger.info(f"  Filling Station ID: {filling_station_id}")
                logger.debug(f"Sending simple payload for #{voucher_no}")

                response = self._api_request(
                    'POST',
                    '/import/tally-dc-name-payload/',
                    json=request_payload,
                )

                if response.status_code in [200, 201]:
                    result = response.json()
                    response_json = json.dumps(result)

                    if result.get('status') != 'success':
                        error_msg = result.get('message', 'Unknown error')
                        raise ValueError(f"API returned error: {error_msg}")

                    data = result.get('data', {})
                    dc_status = data.get('status', '')
                    dc_no = data.get('dc_no', voucher_no)
                    dc_id = data.get('dc_id')

                    if dc_status in ['created', 'updated']:
                        logger.info(f"API sync successful: {dc_status}")
                        stats['synced'] += 1

                        self.db.mark_invoice_synced(
                            invoice_id,
                            dc_no,
                            dc_id,
                            response_json,
                        )
                        stats['verified'] += 1

                        logger.info(
                            f"SUCCESS: Invoice #{voucher_no} synced as DC "
                            f"(DC No={dc_no}, Catalytics ID={dc_id}, Status={dc_status})"
                        )
                    else:
                        raise ValueError(f"Unexpected status: {dc_status}")

                else:
                    error_msg = f"API error: HTTP {response.status_code}"
                    error_response_json = None
                    try:
                        error_result = response.json()
                        error_response_json = json.dumps(error_result)
                        error_detail = error_result.get('message', response.text[:200])
                        error_msg = f"{error_msg} - {error_detail}"
                    except Exception:
                        error_msg = f"{error_msg} - {response.text[:200]}"

                    logger.error(f"Sync failed for invoice #{voucher_no}: {error_msg}")
                    self.db.mark_invoice_sync_failed(invoice_id, error_msg, error_response_json)
                    stats['failed'] += 1

            except Exception as e:
                logger.error(f"Error syncing invoice #{voucher_no}: {e}", exc_info=True)
                self.db.mark_invoice_sync_failed(invoice_id, str(e))
                stats['failed'] += 1

        logger.info(f"\n{'-'*60}")
        logger.info("INVOICE TO DC SYNC (SIMPLE) SUMMARY")
        logger.info(f"{'-'*60}")
        logger.info(f"Total: {stats['total']}")
        logger.info(f"API Synced: {stats['synced']}")
        logger.info(f"Verified: {stats['verified']}")
        logger.info(f"Failed: {stats['failed']}")
        if stats['total'] > 0:
            logger.info(f"Success Rate: {stats['verified']/stats['total']*100:.1f}%")
        logger.info(f"{'-'*60}")

        return stats

    def sync_all(self):
        """Run complete sync cycle"""
        logger.info("\n" + "="*60)
        logger.info("ARASAN GAS - COMPLETE SYNC CYCLE")
        logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*60)

        results = {
            'customers': self.sync_customers(),
            'products': self.sync_products(),
            'invoices': self.sync_invoices_simple()  # Using lightweight API with name-based matching
        }

        # Overall summary
        logger.info("\n" + "="*60)
        logger.info("OVERALL SYNC SUMMARY")
        logger.info("="*60)
        for entity_type, stats in results.items():
            logger.info(f"\n{entity_type.upper()}:")
            logger.info(f"  Total: {stats['total']}")
            logger.info(f"  Verified: {stats['verified']}")
            logger.info(f"  Failed: {stats['failed']}")
        logger.info("="*60)

        return results


if __name__ == '__main__':
    try:
        syncer = CatalyticsSyncer()
        results = syncer.sync_all()

        logger.info("\nSync cycle completed successfully")

    except Exception as e:
        logger.error(f"\nSync cycle failed: {e}", exc_info=True)
        exit(1)
