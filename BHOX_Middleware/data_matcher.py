"""
Data Matcher: Compare SQLite middleware DB with Catalytics PostgreSQL DB
Verifies data consistency and identifies mismatches
"""

import sqlite3
import psycopg2
import logging
import os
from datetime import datetime
from typing import Dict, List, Tuple
import requests
from config import config
from db import Database

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class DataMatcher:
    """Compare local SQLite data with Catalytics PostgreSQL data"""

    def __init__(self):
        self.db = Database(config.SQLITE_DB_PATH)
        self.api_base = config.CATALYTICS_API_BASE
        self.api_key = config.CATALYTICS_API_KEY
        self.auth_mode = os.getenv('CATALYTICS_USE_AUTH', 'auto').strip().lower()
        self.entity_id = config.ENTITY_ID

        # PostgreSQL connection details from .env
        self.pg_host = config.POSTGRES_HOST
        self.pg_port = config.POSTGRES_PORT
        self.pg_db = config.POSTGRES_DB
        self.pg_user = config.POSTGRES_USER
        self.pg_password = config.POSTGRES_PASSWORD

    def _get_pg_connection(self):
        """Get PostgreSQL connection"""
        return psycopg2.connect(
            host=self.pg_host,
            port=self.pg_port,
            database=self.pg_db,
            user=self.pg_user,
            password=self.pg_password
        )

    def _api_request(self, method, endpoint, **kwargs):
        """Make API request to Catalytics"""
        url = f"{self.api_base}{endpoint}"
        headers = kwargs.pop('headers', {})
        headers['Content-Type'] = 'application/json'

        try:
            if self.auth_mode in ('1', 'true', 'yes', 'on'):
                if self.api_key:
                    headers['Authorization'] = f'Bearer {self.api_key}'
                return requests.request(
                    method,
                    url,
                    headers=headers,
                    timeout=30,
                    **kwargs
                )

            if self.auth_mode == 'auto':
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    timeout=30,
                    **kwargs
                )
                if response.status_code in (401, 403) and self.api_key:
                    retry_headers = dict(headers)
                    retry_headers['Authorization'] = f'Bearer {self.api_key}'
                    return requests.request(
                        method,
                        url,
                        headers=retry_headers,
                        timeout=30,
                        **kwargs
                    )
                return response

            return requests.request(
                method,
                url,
                headers=headers,
                timeout=30,
                **kwargs
            )
        except requests.RequestException as e:
            logger.error(f"API request failed: {e}")
            raise

    # ========================================================================
    # CUSTOMER MATCHING
    # ========================================================================

    def match_customers(self) -> Dict:
        """
        Compare customers between SQLite and Catalytics

        Returns:
            dict: Matching report with counts and mismatches
        """
        logger.info("\n" + "="*70)
        logger.info("CUSTOMER DATA MATCHING")
        logger.info("="*70)

        # Step 1: Get SQLite counts
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as total FROM customers")
        sqlite_total = cursor.fetchone()['total']

        cursor.execute("SELECT COUNT(*) as synced FROM customers WHERE is_synced = 1")
        sqlite_synced = cursor.fetchone()['synced']

        cursor.execute("SELECT COUNT(*) as unsynced FROM customers WHERE is_synced = 0")
        sqlite_unsynced = cursor.fetchone()['unsynced']

        logger.info(f"SQLite Local DB:")
        logger.info(f"  Total Customers: {sqlite_total}")
        logger.info(f"  Marked as Synced: {sqlite_synced}")
        logger.info(f"  Marked as Unsynced: {sqlite_unsynced}")

        # Step 2: Get Catalytics counts - Query PostgreSQL directly
        try:
            pg_conn = self._get_pg_connection()
            pg_cursor = pg_conn.cursor()

            # Count customers in master.Customer table
            pg_cursor.execute('SELECT COUNT(*) FROM "master.Customer"')
            catalytics_total = pg_cursor.fetchone()[0]

            pg_conn.close()

            logger.info(f"\nCatalytics Server DB:")
            logger.info(f"  Total Customers: {catalytics_total}")
        except Exception as e:
            logger.error(f"Error fetching from Catalytics PostgreSQL: {e}")
            catalytics_total = 0

        # Step 3: Compare counts
        logger.info(f"\n" + "-"*70)
        logger.info("COUNT COMPARISON")
        logger.info("-"*70)

        count_match = sqlite_synced == catalytics_total
        difference = sqlite_synced - catalytics_total

        if count_match:
            logger.info(f"✅ MATCH: Both databases have {sqlite_synced} customers")
        else:
            logger.warning(f"⚠️  MISMATCH:")
            logger.warning(f"   SQLite (synced): {sqlite_synced}")
            logger.warning(f"   Catalytics: {catalytics_total}")
            logger.warning(f"   Difference: {difference:+d}")

        # Step 4: Check individual records (sample)
        logger.info(f"\n" + "-"*70)
        logger.info("INDIVIDUAL RECORD VERIFICATION (Sample)")
        logger.info("-"*70)

        cursor.execute("""
            SELECT id, name, gstin, catalytics_id, is_synced
            FROM customers
            WHERE is_synced = 1
            LIMIT 10
        """)

        verified = 0
        not_found = 0
        errors = 0

        for row in cursor.fetchall():
            customer_id = row['id']
            name = row['name']
            catalytics_id = row['catalytics_id']

            if not catalytics_id:
                logger.warning(f"⚠️  Customer '{name}' marked synced but no Catalytics ID")
                continue

            try:
                response = self._api_request('GET', f'/api/master/customer/{catalytics_id}/')

                if response.status_code == 200:
                    logger.info(f"✅ Verified: '{name}' (Catalytics ID: {catalytics_id})")
                    verified += 1
                elif response.status_code == 404:
                    logger.error(f"❌ NOT FOUND: '{name}' (Catalytics ID: {catalytics_id})")
                    not_found += 1
                else:
                    logger.error(f"⚠️  ERROR: '{name}' - HTTP {response.status_code}")
                    errors += 1
            except Exception as e:
                logger.error(f"⚠️  ERROR verifying '{name}': {e}")
                errors += 1

        conn.close()

        # Summary
        result = {
            'entity_type': 'customers',
            'sqlite_total': sqlite_total,
            'sqlite_synced': sqlite_synced,
            'sqlite_unsynced': sqlite_unsynced,
            'catalytics_total': catalytics_total,
            'count_match': count_match,
            'difference': difference,
            'sample_verified': verified,
            'sample_not_found': not_found,
            'sample_errors': errors,
            'timestamp': datetime.now().isoformat()
        }

        logger.info(f"\n" + "="*70)
        logger.info("CUSTOMER MATCHING SUMMARY")
        logger.info("="*70)
        logger.info(f"Count Match: {'✅ YES' if count_match else '❌ NO'}")
        logger.info(f"Sample Verification: {verified} verified, {not_found} not found, {errors} errors")
        logger.info("="*70)

        return result

    # ========================================================================
    # PRODUCT MATCHING
    # ========================================================================

    def match_products(self) -> Dict:
        """
        Compare products between SQLite and Catalytics

        Returns:
            dict: Matching report
        """
        logger.info("\n" + "="*70)
        logger.info("PRODUCT DATA MATCHING")
        logger.info("="*70)

        # SQLite counts
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as total FROM products")
        sqlite_total = cursor.fetchone()['total']

        cursor.execute("SELECT COUNT(*) as synced FROM products WHERE is_synced = 1")
        sqlite_synced = cursor.fetchone()['synced']

        cursor.execute("SELECT COUNT(*) as unsynced FROM products WHERE is_synced = 0")
        sqlite_unsynced = cursor.fetchone()['unsynced']

        logger.info(f"SQLite Local DB:")
        logger.info(f"  Total Products: {sqlite_total}")
        logger.info(f"  Marked as Synced: {sqlite_synced}")
        logger.info(f"  Marked as Unsynced: {sqlite_unsynced}")

        # Catalytics counts - Query PostgreSQL directly
        try:
            pg_conn = self._get_pg_connection()
            pg_cursor = pg_conn.cursor()

            # Count products in master.product table
            pg_cursor.execute('SELECT COUNT(*) FROM "master.product"')
            catalytics_total = pg_cursor.fetchone()[0]

            pg_conn.close()

            logger.info(f"\nCatalytics Server DB:")
            logger.info(f"  Total Products: {catalytics_total}")
        except Exception as e:
            logger.error(f"Error fetching from Catalytics PostgreSQL: {e}")
            catalytics_total = 0

        # Compare
        logger.info(f"\n" + "-"*70)
        logger.info("COUNT COMPARISON")
        logger.info("-"*70)

        count_match = sqlite_synced == catalytics_total
        difference = sqlite_synced - catalytics_total

        if count_match:
            logger.info(f"[OK] MATCH: Both databases have {sqlite_synced} products")
        else:
            logger.warning(f"[MISMATCH]:")
            logger.warning(f"   SQLite (synced): {sqlite_synced}")
            logger.warning(f"   Catalytics: {catalytics_total}")
            logger.warning(f"   Difference: {difference:+d}")

        conn.close()

        result = {
            'entity_type': 'products',
            'sqlite_total': sqlite_total,
            'sqlite_synced': sqlite_synced,
            'sqlite_unsynced': sqlite_unsynced,
            'catalytics_total': catalytics_total,
            'count_match': count_match,
            'difference': difference,
            'timestamp': datetime.now().isoformat()
        }

        logger.info(f"\n" + "="*70)
        logger.info("PRODUCT MATCHING SUMMARY")
        logger.info("="*70)
        logger.info(f"Count Match: {'✅ YES' if count_match else '❌ NO'}")
        logger.info("="*70)

        return result

    # ========================================================================
    # INVOICE MATCHING
    # ========================================================================

    def match_invoices(self) -> Dict:
        """
        Compare invoices between SQLite and Catalytics

        Returns:
            dict: Matching report
        """
        logger.info("\n" + "="*70)
        logger.info("INVOICE DATA MATCHING")
        logger.info("="*70)

        # SQLite counts
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as total FROM invoices")
        sqlite_total = cursor.fetchone()['total']

        cursor.execute("SELECT COUNT(*) as synced FROM invoices WHERE is_synced = 1")
        sqlite_synced = cursor.fetchone()['synced']

        cursor.execute("SELECT COUNT(*) as unsynced FROM invoices WHERE is_synced = 0")
        sqlite_unsynced = cursor.fetchone()['unsynced']

        logger.info(f"SQLite Local DB:")
        logger.info(f"  Total Invoices: {sqlite_total}")
        logger.info(f"  Marked as Synced: {sqlite_synced}")
        logger.info(f"  Marked as Unsynced: {sqlite_unsynced}")

        # Catalytics counts - Query PostgreSQL directly
        try:
            pg_conn = self._get_pg_connection()
            pg_cursor = pg_conn.cursor()

            # Count delivery challans in transaction.delivery_challan table
            pg_cursor.execute('SELECT COUNT(*) FROM "transaction.delivery_challan"')
            catalytics_total = pg_cursor.fetchone()[0]

            pg_conn.close()

            logger.info(f"\nCatalytics Server DB:")
            logger.info(f"  Total Invoices/DCs: {catalytics_total}")
        except Exception as e:
            logger.error(f"Error fetching from Catalytics PostgreSQL: {e}")
            catalytics_total = 0

        # Compare
        logger.info(f"\n" + "-"*70)
        logger.info("COUNT COMPARISON")
        logger.info("-"*70)

        count_match = sqlite_synced == catalytics_total
        difference = sqlite_synced - catalytics_total

        if count_match:
            logger.info(f"[OK] MATCH: Both databases have {sqlite_synced} invoices")
        else:
            logger.warning(f"[MISMATCH]:")
            logger.warning(f"   SQLite (synced): {sqlite_synced}")
            logger.warning(f"   Catalytics: {catalytics_total}")
            logger.warning(f"   Difference: {difference:+d}")

        conn.close()

        result = {
            'entity_type': 'invoices',
            'sqlite_total': sqlite_total,
            'sqlite_synced': sqlite_synced,
            'sqlite_unsynced': sqlite_unsynced,
            'catalytics_total': catalytics_total,
            'count_match': count_match,
            'difference': difference,
            'timestamp': datetime.now().isoformat()
        }

        logger.info(f"\n" + "="*70)
        logger.info("INVOICE MATCHING SUMMARY")
        logger.info("="*70)
        logger.info(f"Count Match: {'✅ YES' if count_match else '❌ NO'}")
        logger.info("="*70)

        return result

    # ========================================================================
    # FULL MATCHING
    # ========================================================================

    def match_all(self) -> Dict:
        """
        Run complete data matching for all entity types

        Returns:
            dict: Complete matching report
        """
        logger.info("\n" + "="*70)
        logger.info("DATA MATCHING: SQLite ↔ Catalytics")
        logger.info(f"Entity: {config.ENTITY_NAME} (ID: {config.ENTITY_ID})")
        logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*70)

        results = {
            'entity_name': config.ENTITY_NAME,
            'entity_id': config.ENTITY_ID,
            'timestamp': datetime.now().isoformat(),
            'customers': None,
            'products': None,
            'invoices': None,
            'overall_match': False
        }

        # Match customers
        try:
            results['customers'] = self.match_customers()
        except Exception as e:
            logger.error(f"Customer matching failed: {e}", exc_info=True)
            results['customers'] = {'error': str(e)}

        # Match products
        try:
            results['products'] = self.match_products()
        except Exception as e:
            logger.error(f"Product matching failed: {e}", exc_info=True)
            results['products'] = {'error': str(e)}

        # Match invoices
        try:
            results['invoices'] = self.match_invoices()
        except Exception as e:
            logger.error(f"Invoice matching failed: {e}", exc_info=True)
            results['invoices'] = {'error': str(e)}

        # Overall summary
        logger.info("\n" + "="*70)
        logger.info("OVERALL DATA MATCHING SUMMARY")
        logger.info("="*70)

        customer_match = results.get('customers', {}).get('count_match', False)
        product_match = results.get('products', {}).get('count_match', False)
        invoice_match = results.get('invoices', {}).get('count_match', False)

        results['overall_match'] = customer_match and product_match and invoice_match

        logger.info(f"Customers: {'✅ MATCH' if customer_match else '❌ MISMATCH'}")
        logger.info(f"Products: {'✅ MATCH' if product_match else '❌ MISMATCH'}")
        logger.info(f"Invoices: {'✅ MATCH' if invoice_match else '❌ MISMATCH'}")
        logger.info(f"\nOverall: {'✅ ALL MATCH' if results['overall_match'] else '⚠️  MISMATCHES FOUND'}")
        logger.info("="*70)

        return results


if __name__ == '__main__':
    try:
        matcher = DataMatcher()
        results = matcher.match_all()

        print("\n" + "="*70)
        print("DATA MATCHING COMPLETED")
        print("="*70)
        print(f"Overall Status: {'✅ ALL DATA MATCHES' if results['overall_match'] else '⚠️  MISMATCHES DETECTED'}")
        print("="*70)

        if not results['overall_match']:
            print("\n⚠️  Action Required:")
            print("   - Check sync logs for errors")
            print("   - Verify Catalytics API connectivity")
            print("   - Run sync again for unsynced records")
            print("   - Contact support if mismatches persist")

    except Exception as e:
        logger.error(f"Data matching failed: {e}", exc_info=True)
        exit(1)
