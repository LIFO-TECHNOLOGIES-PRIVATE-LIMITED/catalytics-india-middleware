"""
SQLite Database Schema for Arasan Gas Middleware
Tracks company ownership and sync status
"""
import sqlite3
import logging
import json
import hashlib
import time
import msvcrt
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


def json_dumps(value):
    """Serialize to JSON with sorted keys for consistency"""
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), sort_keys=True)


def json_loads(value):
    """Deserialize from JSON"""
    return json.loads(value) if value else None


def sha256_text(value):
    """Compute SHA256 hash of text"""
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class Database:
    """SQLite database manager with company tracking"""

    def __init__(self, db_path='arasan_gas.sqlite'):
        self.db_path = db_path
        self.conn = None
        self._lock_file = None
        self._lock_path = str(Path(self.db_path).with_suffix(".lock"))
        self.initialize()

    def initialize(self):
        """Create database and tables if they don't exist"""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent reads while writing (prevents "database is locked")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.create_tables()
        logger.info(f"Database initialized: {self.db_path}")

    def _acquire_write_lock(self, timeout=30.0, poll=0.1):
        """Acquire cross-process write lock to serialize SQLite writes."""
        if self._lock_file is None:
            self._lock_file = open(self._lock_path, 'a+b')
        end = time.time() + timeout
        while True:
            try:
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.time() >= end:
                    raise TimeoutError('Timed out waiting for SQLite write lock')
                time.sleep(poll)

    def _release_write_lock(self):
        """Release cross-process write lock."""
        if not self._lock_file:
            return
        try:
            self._lock_file.seek(0)
            msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

    def create_tables(self):
        """Create all required tables"""
        cursor = self.conn.cursor()

        # Customers table with company tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tally_guid TEXT,
                name TEXT UNIQUE NOT NULL,
                tally_company TEXT NOT NULL,
                gstin TEXT,
                pan TEXT,
                address TEXT,
                state TEXT,
                city TEXT,
                pincode TEXT,
                phone TEXT,
                email TEXT,
                data_json TEXT,
                sync_request_json TEXT,
                last_response_json TEXT,
                first_fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_synced INTEGER DEFAULT 0,
                catalytics_id INTEGER,
                sync_attempts INTEGER DEFAULT 0,
                last_sync_error TEXT,
                last_sync_at TIMESTAMP
            )
        """)

        # Products table with company tracking and canonical name for uniqueness
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tally_guid TEXT,
                name TEXT NOT NULL,
                name_canonical TEXT UNIQUE NOT NULL,
                tally_company TEXT NOT NULL,
                hsn_code TEXT,
                unit TEXT,
                rate REAL,
                description TEXT,
                data_json TEXT,
                product_master_name TEXT,
                variant_name TEXT,
                unit_name TEXT,
                product_type_code TEXT,
                product_type_name TEXT,
                gst_applicable TEXT,
                gst_rate REAL,
                igst_rate REAL,
                cgst_rate REAL,
                sgst_rate REAL,
                sync_request_json TEXT,
                last_response_json TEXT,
                first_fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_synced INTEGER DEFAULT 0,
                catalytics_id INTEGER,
                sync_attempts INTEGER DEFAULT 0,
                last_sync_error TEXT,
                last_sync_at TIMESTAMP
            )
        """)

        # Invoices table (multi-company support)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tally_voucher_no TEXT NOT NULL,
                tally_company TEXT NOT NULL,
                tally_guid TEXT,
                voucher_date TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                customer_guid TEXT,
                billing_address TEXT,
                delivery_address TEXT,
                total_amount REAL,
                tax_amount REAL,
                items_json TEXT,
                data_json TEXT,
                ledger_data_json TEXT,
                stock_items_json TEXT,
                payload_hash TEXT,
                sync_request_json TEXT,
                last_response_json TEXT,
                first_fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_synced INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0,
                deleted_at TIMESTAMP,
                dc_no TEXT,
                catalytics_dc_id INTEGER,
                sync_attempts INTEGER DEFAULT 0,
                last_sync_error TEXT,
                last_sync_at TIMESTAMP,
                UNIQUE(tally_voucher_no, tally_company)
            )
        """)

        # Sync status tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation TEXT NOT NULL,
                tally_company TEXT,
                status TEXT NOT NULL,
                records_processed INTEGER DEFAULT 0,
                records_success INTEGER DEFAULT 0,
                records_failed INTEGER DEFAULT 0,
                records_skipped INTEGER DEFAULT 0,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                error_message TEXT,
                details_json TEXT
            )
        """)

        # Duplicate tracking (for audit)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS duplicate_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_name TEXT NOT NULL,
                tally_company TEXT NOT NULL,
                owned_by_company TEXT NOT NULL,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                details TEXT
            )
        """)

        # Create indexes for performance
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_customers_name
            ON customers(name)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_customers_synced
            ON customers(is_synced)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_products_name
            ON products(name)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_products_synced
            ON products(is_synced)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_invoices_synced
            ON invoices(is_synced)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_invoices_company
            ON invoices(tally_company)
        """)

        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_duplicate_log_unique
            ON duplicate_log(entity_type, entity_name, tally_company, owned_by_company)
        """)

        self.conn.commit()
        logger.info("Database tables created successfully")

        # Run migrations
        self._migrate_db()

    def execute(self, query, params=(), retries=5, base_delay=0.2):
        """Execute a query with parameters (retries on database lock)."""
        last_err = None
        for attempt in range(retries):
            locked = False
            try:
                self._acquire_write_lock()
                locked = True
                cursor = self.conn.cursor()
                cursor.execute(query, params)
                self.conn.commit()
                return cursor
            except sqlite3.OperationalError as e:
                last_err = e
                if 'database is locked' in str(e).lower() and attempt < retries - 1:
                    time.sleep(base_delay * (2 ** attempt))
                    continue
                raise
            finally:
                if locked:
                    self._release_write_lock()
        if last_err:
            raise last_err
        return None

    def query(self, query, params=()):
        """Execute a SELECT query and return one result"""
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return cursor.fetchone()

    def query_all(self, query, params=()):
        """Execute a SELECT query and return all results"""
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return cursor.fetchall()

    def _migrate_db(self):
        """Add new columns if they don't exist (for backwards compatibility)"""
        cursor = self.conn.cursor()
        new_columns = {
            'invoices': [
                ('ledger_data_json', 'TEXT'),
                ('stock_items_json', 'TEXT'),
                ('payload_hash', 'TEXT'),
                ('is_deleted', 'INTEGER DEFAULT 0'),
                ('deleted_at', 'TIMESTAMP'),
                ('godown_name', 'TEXT'),
                ('location_name', 'TEXT'),
                ('filling_station', 'TEXT'),
            ],
            'products': [
                ('product_master_name', 'TEXT'),
                ('variant_name', 'TEXT'),
                ('unit_name', 'TEXT'),
                ('product_type_code', 'TEXT'),
                ('product_type_name', 'TEXT'),
                ('gst_applicable', 'TEXT'),
                ('gst_rate', 'REAL'),
                ('igst_rate', 'REAL'),
                ('cgst_rate', 'REAL'),
                ('sgst_rate', 'REAL'),
            ]
        }

        for table, columns in new_columns.items():
            try:
                # Get existing columns
                existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}

                # Add missing columns
                for col_name, col_type in columns:
                    if col_name not in existing:
                        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                        logger.info(f"Added column {col_name} to {table}")
            except Exception as e:
                logger.debug(f"Migration skipped for {table}: {e}")

        self.conn.commit()

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed")
        if self._lock_file:
            try:
                self._lock_file.close()
            except Exception:
                pass

    # ========================================================================
    # CUSTOMER OPERATIONS
    # ========================================================================

    def customer_exists(self, name):
        """Check if customer name already exists (ignores whitespace differences)"""
        normalized = ''.join((name or '').split())
        result = self.query(
            "SELECT id, tally_company FROM customers WHERE REPLACE(name, ' ', '') = ?",
            (normalized,)
        )
        return result if result else None

    def insert_customer(self, customer_data):
        """Insert new customer"""
        self.execute("""
            INSERT INTO customers (
                tally_guid, name, tally_company, gstin, pan,
                address, state, city, pincode, phone, email,
                data_json, first_fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            customer_data.get('tally_guid'),
            customer_data.get('name'),
            customer_data.get('tally_company'),
            customer_data.get('gstin'),
            customer_data.get('pan'),
            customer_data.get('address'),
            customer_data.get('state'),
            customer_data.get('city'),
            customer_data.get('pincode'),
            customer_data.get('phone'),
            customer_data.get('email'),
            customer_data.get('data_json')
        ))

    def update_customer(self, customer_id, customer_data):
        """Update existing customer with fresh data and mark for re-sync"""
        self.execute("""
            UPDATE customers
            SET tally_guid = ?, gstin = ?, pan = ?,
                address = ?, state = ?, city = ?, pincode = ?,
                phone = ?, email = ?, data_json = ?,
                is_synced = 0, sync_attempts = 0, last_sync_error = NULL,
                last_updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (
            customer_data.get('tally_guid'),
            customer_data.get('gstin'),
            customer_data.get('pan'),
            customer_data.get('address'),
            customer_data.get('state'),
            customer_data.get('city'),
            customer_data.get('pincode'),
            customer_data.get('phone'),
            customer_data.get('email'),
            customer_data.get('data_json'),
            customer_id,
        ))

    def get_unsynced_customers(self, limit=None):
        """Get customers that haven't been synced.

        Args:
            limit: max rows to return; if None, return all unsynced.
        """
        if limit is None:
            return self.query_all("""
                SELECT * FROM customers
                WHERE is_synced = 0
                ORDER BY first_fetched_at
            """)
        return self.query_all("""
            SELECT * FROM customers
            WHERE is_synced = 0
            ORDER BY first_fetched_at
            LIMIT ?
        """, (limit,))

    def mark_customer_synced(self, customer_id, catalytics_id, response_json=None):
        """Mark customer as successfully synced"""
        self.execute("""
            UPDATE customers
            SET is_synced = 1,
                catalytics_id = ?,
                last_response_json = ?,
                last_sync_at = CURRENT_TIMESTAMP,
                last_sync_error = NULL
            WHERE id = ?
        """, (catalytics_id, response_json, customer_id))

    def mark_customer_sync_failed(self, customer_id, error_msg, response_json=None):
        """Mark customer sync as failed"""
        self.execute("""
            UPDATE customers
            SET sync_attempts = sync_attempts + 1,
                last_sync_error = ?,
                last_response_json = ?,
                last_sync_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (error_msg, response_json, customer_id))

    # ========================================================================
    # PRODUCT OPERATIONS
    # ========================================================================

    def product_exists(self, name):
        """Check if product name already exists"""
        result = self.query(
            "SELECT id, tally_company FROM products WHERE name = ?",
            (name,)
        )
        return result if result else None

    def product_exists_normalized(self, canonical_name):
        """
        Check if product exists using canonical (normalized) name.
        Canonical name handles spacing variations like "1.5CUM" vs "1.5 CUM".
        
        Args:
            canonical_name: Normalized product name (e.g., "ARGON B TYPE 1.5 CUM (CYL)")
            
        Returns:
            Product record if exists, None otherwise
        """
        result = self.query(
            "SELECT id, tally_company, name FROM products WHERE name_canonical = ?",
            (canonical_name,)
        )
        return result if result else None

    def insert_product(self, product_data):
        """Insert new product with canonical name for uniqueness checking"""
        self.execute("""
            INSERT INTO products (
                tally_guid, name, name_canonical, tally_company, hsn_code, unit,
                rate, description, data_json,
                product_master_name, variant_name, unit_name,
                product_type_code, product_type_name,
                gst_applicable, gst_rate, igst_rate, cgst_rate, sgst_rate,
                first_fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            product_data.get('tally_guid'),
            product_data.get('name'),
            product_data.get('name_canonical'),  # ← Canonical name for uniqueness
            product_data.get('tally_company'),
            product_data.get('hsn_code'),
            product_data.get('unit'),
            product_data.get('rate'),
            product_data.get('description'),
            product_data.get('data_json'),
            product_data.get('product_master_name'),
            product_data.get('variant_name'),
            product_data.get('unit_name'),
            product_data.get('product_type_code'),
            product_data.get('product_type_name'),
            product_data.get('gst_applicable'),
            product_data.get('gst_rate', 0.0),
            product_data.get('igst_rate', 0.0),
            product_data.get('cgst_rate', 0.0),
            product_data.get('sgst_rate', 0.0),
        ))

    def update_product(self, product_id, product_data):
        """Update existing product with fresh data and mark for re-sync"""
        self.execute("""
            UPDATE products
            SET tally_guid = ?, hsn_code = ?, unit = ?,
                rate = ?, description = ?, data_json = ?,
                product_master_name = ?, variant_name = ?, unit_name = ?,
                product_type_code = ?, product_type_name = ?,
                gst_applicable = ?, gst_rate = ?, igst_rate = ?, cgst_rate = ?, sgst_rate = ?,
                is_synced = 0, sync_attempts = 0, last_sync_error = NULL,
                last_updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (
            product_data.get('tally_guid'),
            product_data.get('hsn_code'),
            product_data.get('unit'),
            product_data.get('rate'),
            product_data.get('description'),
            product_data.get('data_json'),
            product_data.get('product_master_name'),
            product_data.get('variant_name'),
            product_data.get('unit_name'),
            product_data.get('product_type_code'),
            product_data.get('product_type_name'),
            product_data.get('gst_applicable'),
            product_data.get('gst_rate', 0.0),
            product_data.get('igst_rate', 0.0),
            product_data.get('cgst_rate', 0.0),
            product_data.get('sgst_rate', 0.0),
            product_id,
        ))

    def get_unsynced_products(self, limit=50):
        """Get products that haven't been synced"""
        return self.query_all("""
            SELECT * FROM products
            WHERE is_synced = 0
            ORDER BY first_fetched_at
            LIMIT ?
        """, (limit,))

    def mark_product_synced(self, product_id, catalytics_id, response_json=None):
        """Mark product as successfully synced"""
        if catalytics_id is None and response_json:
            try:
                data = json.loads(response_json)
                payload = data.get('data', {}) if isinstance(data, dict) else {}
                catalytics_id = payload.get('product_id') or payload.get('id')
                if not catalytics_id:
                    results = payload.get('results', [])
                    if isinstance(results, list) and results:
                        first = results[0]
                        if isinstance(first, dict):
                            catalytics_id = first.get('product_id') or first.get('id')
            except Exception:
                pass
        self.execute("""
            UPDATE products
            SET is_synced = 1,
                catalytics_id = ?,
                last_response_json = ?,
                last_sync_at = CURRENT_TIMESTAMP,
                last_sync_error = NULL
            WHERE id = ?
        """, (catalytics_id, response_json, product_id))

    def mark_product_sync_failed(self, product_id, error_msg, response_json=None):
        """Mark product sync as failed"""
        self.execute("""
            UPDATE products
            SET sync_attempts = sync_attempts + 1,
                last_sync_error = ?,
                last_response_json = ?,
                last_sync_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (error_msg, response_json, product_id))

    # ========================================================================
    # INVOICE OPERATIONS
    # ========================================================================

    def invoice_exists(self, voucher_no, tally_company):
        """Get invoice row for voucher/company if present"""
        result = self.query("""
            SELECT id, data_json, payload_hash, is_synced, is_deleted FROM invoices
            WHERE tally_voucher_no = ? AND tally_company = ?
        """, (voucher_no, tally_company))
        return result if result else None

    def insert_invoice(self, invoice_data):
        """Insert new invoice"""
        self.execute("""
            INSERT INTO invoices (
                tally_voucher_no, tally_company, tally_guid,
                voucher_date, customer_name, customer_guid,
                billing_address, delivery_address,
                total_amount, tax_amount, items_json, data_json,
                ledger_data_json, stock_items_json, payload_hash,
                first_fetched_at, is_deleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0)
        """, (
            invoice_data.get('voucher_no'),
            invoice_data.get('tally_company'),
            invoice_data.get('tally_guid'),
            invoice_data.get('voucher_date'),
            invoice_data.get('customer_name'),
            invoice_data.get('customer_guid'),
            invoice_data.get('billing_address'),
            invoice_data.get('delivery_address'),
            invoice_data.get('total_amount'),
            invoice_data.get('tax_amount'),
            invoice_data.get('items_json'),
            invoice_data.get('data_json'),
            invoice_data.get('ledger_data_json'),
            invoice_data.get('stock_items_json'),
            invoice_data.get('payload_hash')
        ))

    def update_invoice(self, invoice_id, invoice_data):
        """Update invoice payload and reset sync state for re-sync"""
        self.execute("""
            UPDATE invoices
            SET tally_guid = ?,
                voucher_date = ?,
                customer_name = ?,
                customer_guid = ?,
                billing_address = ?,
                delivery_address = ?,
                total_amount = ?,
                tax_amount = ?,
                items_json = ?,
                data_json = ?,
                ledger_data_json = ?,
                stock_items_json = ?,
                payload_hash = ?,
                last_updated_at = CURRENT_TIMESTAMP,
                is_synced = 0,
                sync_attempts = 0,
                is_deleted = 0,
                deleted_at = NULL,
                dc_no = NULL,
                catalytics_dc_id = NULL,
                last_sync_error = NULL
            WHERE id = ?
        """, (
            invoice_data.get('tally_guid'),
            invoice_data.get('voucher_date'),
            invoice_data.get('customer_name'),
            invoice_data.get('customer_guid'),
            invoice_data.get('billing_address'),
            invoice_data.get('delivery_address'),
            invoice_data.get('total_amount'),
            invoice_data.get('tax_amount'),
            invoice_data.get('items_json'),
            invoice_data.get('data_json'),
            invoice_data.get('ledger_data_json'),
            invoice_data.get('stock_items_json'),
            invoice_data.get('payload_hash'),
            invoice_id
        ))

    def get_unsynced_invoices(self, limit=50, max_attempts=10):
        """Get invoices that haven't been synced.
        Excludes deleted invoices and invoices that failed too many times."""
        return self.query_all("""
            SELECT * FROM invoices
            WHERE is_synced = 0
              AND COALESCE(is_deleted, 0) = 0
              AND COALESCE(sync_attempts, 0) < ?
            ORDER BY first_fetched_at
            LIMIT ?
        """, (max_attempts, limit))

    def mark_invoice_synced(self, invoice_id, dc_no, catalytics_dc_id, response_json=None):
        """Mark invoice as successfully synced"""
        self.execute("""
            UPDATE invoices
            SET is_synced = 1,
                dc_no = ?,
                catalytics_dc_id = ?,
                last_response_json = ?,
                last_sync_at = CURRENT_TIMESTAMP,
                last_sync_error = NULL
            WHERE id = ?
        """, (dc_no, catalytics_dc_id, response_json, invoice_id))

    def mark_invoice_sync_failed(self, invoice_id, error_msg, response_json=None):
        """Mark invoice sync as failed"""
        self.execute("""
            UPDATE invoices
            SET sync_attempts = sync_attempts + 1,
                last_sync_error = ?,
                last_response_json = ?,
                last_sync_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (error_msg, response_json, invoice_id))

    # ========================================================================
    # INVOICE DELETION TRACKING
    # ========================================================================

    def mark_invoices_deleted(self, company_name, voucher_nos):
        """Mark invoices as deleted (soft delete) by company and voucher numbers"""
        if not voucher_nos:
            return 0

        voucher_list = list(voucher_nos)
        count = 0

        self._acquire_write_lock()
        try:
            # Batch in groups of 500 to stay within SQLite limits
            for i in range(0, len(voucher_list), 500):
                batch = voucher_list[i:i+500]
                placeholders = ','.join('?' for _ in batch)
                cursor = self.conn.cursor()
                cursor.execute(f"""
                    UPDATE invoices
                    SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP
                    WHERE tally_company = ?
                      AND tally_voucher_no IN ({placeholders})
                      AND COALESCE(is_deleted, 0) = 0
                """, [company_name] + batch)
                count += cursor.rowcount

            self.conn.commit()
        finally:
            self._release_write_lock()
        return count

    # ========================================================================
    # DUPLICATE LOGGING
    # ========================================================================

    def log_duplicate(self, entity_type, entity_name, tally_company, owned_by_company, details=''):
        """Log duplicate entity for audit (skip if already logged)"""
        existing = self.query(
            """SELECT 1 FROM duplicate_log
               WHERE entity_type = ? AND entity_name = ?
                 AND tally_company = ? AND owned_by_company = ?
               LIMIT 1""",
            (entity_type, entity_name, tally_company, owned_by_company)
        )
        if existing:
            return
        self.execute("""
            INSERT INTO duplicate_log (
                entity_type, entity_name, tally_company,
                owned_by_company, details, logged_at
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (entity_type, entity_name, tally_company, owned_by_company, details))

    # ========================================================================
    # STATISTICS
    # ========================================================================

    def get_statistics(self):
        """Get database statistics"""
        stats = {}

        # Customer stats
        cursor = self.conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM customers")
        stats['total_customers'] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM customers WHERE is_synced = 1")
        stats['synced_customers'] = cursor.fetchone()[0]

        cursor.execute("SELECT tally_company, COUNT(*) FROM customers GROUP BY tally_company")
        stats['customers_by_company'] = dict(cursor.fetchall())

        # Product stats
        cursor.execute("SELECT COUNT(*) FROM products")
        stats['total_products'] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM products WHERE is_synced = 1")
        stats['synced_products'] = cursor.fetchone()[0]

        cursor.execute("SELECT tally_company, COUNT(*) FROM products GROUP BY tally_company")
        stats['products_by_company'] = dict(cursor.fetchall())

        # Invoice stats
        cursor.execute("SELECT COUNT(*) FROM invoices")
        stats['total_invoices'] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM invoices WHERE is_synced = 1")
        stats['synced_invoices'] = cursor.fetchone()[0]

        cursor.execute("SELECT tally_company, COUNT(*) FROM invoices GROUP BY tally_company")
        stats['invoices_by_company'] = dict(cursor.fetchall())

        # Duplicate stats
        cursor.execute("SELECT COUNT(*) FROM duplicate_log")
        stats['total_duplicates'] = cursor.fetchone()[0]

        cursor.execute("""
            SELECT entity_type, COUNT(*) FROM duplicate_log
            GROUP BY entity_type
        """)
        stats['duplicates_by_type'] = dict(cursor.fetchall())

        return stats


if __name__ == '__main__':
    # Test database creation
    logging.basicConfig(level=logging.INFO)

    db = Database('test_arasan_gas.sqlite')

    print("=== Database Initialized ===")
    print(f"Tables created successfully in test_arasan_gas.sqlite")

    # Test operations
    print("\n=== Testing Customer Operations ===")

    # Insert test customer
    db.insert_customer({
        'tally_guid': 'TEST-GUID-001',
        'name': 'Test Customer Ltd',
        'tally_company': 'COMPANY_1',
        'gstin': '27AAAAA0000A1Z5',
        'pan': 'AAAAA0000A',
        'address': '123 Test Street',
        'state': 'Maharashtra'
    })
    print("✓ Customer inserted")

    # Check duplicate
    exists = db.customer_exists('Test Customer Ltd')
    if exists:
        print(f"✓ Customer exists check works: {dict(exists)}")

    # Get statistics
    stats = db.get_statistics()
    print("\n=== Database Statistics ===")
    for key, value in stats.items():
        print(f"{key}: {value}")

    db.close()
    print("\n✓ Database operations test completed")
