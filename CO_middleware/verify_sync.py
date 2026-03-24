"""
Sync verification utility.
Verifies data exists in Catalytics PostgreSQL after sync.
"""
import logging
import time
from datetime import datetime

import psycopg2
import requests
from psycopg2 import sql

from config import config

logger = logging.getLogger(__name__)


class SyncVerifier:
    """Verify sync operations between SQLite and Catalytics PostgreSQL."""

    def __init__(self):
        self.api_base = config.CATALYTICS_API_BASE
        self.api_key = config.CATALYTICS_API_KEY
        self.entity_id = config.ENTITY_ID
        self.timeout = config.VERIFY_TIMEOUT_SECONDS
        self.retry_count = config.VERIFY_RETRY_COUNT
        self.retry_delay = config.VERIFY_RETRY_DELAY_SECONDS

        self.pg_host = config.POSTGRES_HOST
        self.pg_port = config.POSTGRES_PORT
        self.pg_db = config.POSTGRES_DB
        self.pg_user = config.POSTGRES_USER
        self.pg_password = config.POSTGRES_PASSWORD

    def _make_request(self, method, endpoint, **kwargs):
        """Make HTTP request to Catalytics API."""
        url = f"{self.api_base}{endpoint}"
        headers = kwargs.pop('headers', {})
        headers.update({
            'Authorization': f'Bearer {self.api_key}',
            'Entity-Id': str(self.entity_id),
            'Content-Type': 'application/json'
        })

        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=self.timeout,
                **kwargs
            )
            return response

        except requests.RequestException as e:
            logger.error(f"API request failed: {e}")
            raise

    def _get_pg_connection(self):
        """Get PostgreSQL connection."""
        return psycopg2.connect(
            host=self.pg_host,
            port=self.pg_port,
            database=self.pg_db,
            user=self.pg_user,
            password=self.pg_password,
            connect_timeout=10
        )

    def _parse_yyyymmdd(self, value):
        """Parse YYYYMMDD into date object, return None if invalid."""
        if not value:
            return None
        try:
            return datetime.strptime(str(value), '%Y%m%d').date()
        except Exception:
            return None

    def verify_customer(self, customer_id, customer_name):
        """
        Verify customer exists in PostgreSQL.

        Args:
            customer_id: Catalytics customer ID
            customer_name: Customer name for verification

        Returns:
            dict: {'exists': bool, 'id': int or None, 'details': dict or None}
        """
        for attempt in range(self.retry_count):
            try:
                if attempt > 0:
                    time.sleep(self.retry_delay)
                    logger.debug(f"Verification retry {attempt + 1}/{self.retry_count}")

                response = self._make_request(
                    'GET',
                    f'/api/master/customer/{customer_id}/'
                )

                if response.status_code == 200:
                    data = response.json()

                    if data.get('name') == customer_name:
                        logger.info(
                            f"VERIFIED: Customer '{customer_name}' "
                            f"exists in PostgreSQL (ID={customer_id})"
                        )
                        return {
                            'exists': True,
                            'id': customer_id,
                            'details': data
                        }

                    logger.warning(
                        f"Name mismatch for customer ID={customer_id}: "
                        f"expected '{customer_name}', got '{data.get('name')}'"
                    )

                elif response.status_code == 404:
                    logger.error(
                        f"NOT FOUND: Customer '{customer_name}' "
                        f"(ID={customer_id}) not in PostgreSQL"
                    )

            except Exception as e:
                logger.error(f"Verification error: {e}")

        return {'exists': False, 'id': None, 'details': None}

    def verify_product(self, product_id, product_name):
        """
        Verify product exists in PostgreSQL.

        Args:
            product_id: Catalytics product ID
            product_name: Product name for verification

        Returns:
            dict: {'exists': bool, 'id': int or None, 'details': dict or None}
        """
        for attempt in range(self.retry_count):
            try:
                if attempt > 0:
                    time.sleep(self.retry_delay)

                response = self._make_request(
                    'GET',
                    f'/api/master/product/{product_id}/'
                )

                if response.status_code == 200:
                    data = response.json()

                    if data.get('name') == product_name:
                        logger.info(
                            f"VERIFIED: Product '{product_name}' "
                            f"exists in PostgreSQL (ID={product_id})"
                        )
                        return {
                            'exists': True,
                            'id': product_id,
                            'details': data
                        }

                elif response.status_code == 404:
                    logger.error(
                        f"NOT FOUND: Product '{product_name}' "
                        f"(ID={product_id}) not in PostgreSQL"
                    )

            except Exception as e:
                logger.error(f"Verification error: {e}")

        return {'exists': False, 'id': None, 'details': None}

    def verify_dc(self, dc_id, dc_no):
        """
        Verify delivery challan exists in PostgreSQL by ID through API.

        Args:
            dc_id: Catalytics DC ID
            dc_no: DC number for verification

        Returns:
            dict: {'exists': bool, 'id': int or None, 'details': dict or None}
        """
        for attempt in range(self.retry_count):
            try:
                if attempt > 0:
                    time.sleep(self.retry_delay)

                response = self._make_request(
                    'GET',
                    f'/api/transaction/delivery-challan/{dc_id}/'
                )

                if response.status_code == 200:
                    data = response.json()

                    if str(data.get('dc_no') or '').strip() == str(dc_no or '').strip():
                        logger.info(
                            f"VERIFIED: DC #{dc_no} exists in PostgreSQL (ID={dc_id})"
                        )
                        return {
                            'exists': True,
                            'id': dc_id,
                            'details': data
                        }

                elif response.status_code == 404:
                    logger.error(f"NOT FOUND: DC #{dc_no} (ID={dc_id}) not in PostgreSQL")

            except Exception as e:
                logger.error(f"Verification error: {e}")

        return {'exists': False, 'id': None, 'details': None}

    def verify_dc_by_number(self, dc_no, expected_date=None, expected_customer=None):
        """
        Verify delivery challan exists in PostgreSQL by dc_no using direct DB query.

        Args:
            dc_no: Delivery challan number to verify.
            expected_date: Optional voucher date in YYYYMMDD format.
            expected_customer: Optional customer name for stronger match.

        Returns:
            dict: {'exists': bool, 'id': int or None, 'details': dict or None}
        """
        normalized_dc_no = str(dc_no or '').strip()
        if not normalized_dc_no:
            return {'exists': False, 'id': None, 'details': None}

        target_date = self._parse_yyyymmdd(expected_date)
        target_customer = (expected_customer or '').strip().lower()

        query = sql.SQL(
            """
            SELECT
                dc.id,
                dc.dc_no,
                dc.dc_date,
                dc.customer_id,
                cust.name AS customer_name,
                dc.vehicle,
                dc.modified_on
            FROM {} dc
            LEFT JOIN {} cust ON cust.id = dc.customer_id
            WHERE dc.dc_no = %s
            ORDER BY dc.modified_on DESC NULLS LAST, dc.id DESC
            LIMIT 10
            """
        ).format(
            sql.Identifier('transaction.delivery_challan'),
            sql.Identifier('master.Customer'),
        )

        for attempt in range(self.retry_count):
            conn = None
            try:
                if attempt > 0:
                    time.sleep(self.retry_delay)

                conn = self._get_pg_connection()
                cur = conn.cursor()
                cur.execute(query, (normalized_dc_no,))
                rows = cur.fetchall()

                if not rows:
                    logger.warning(f"DB verify miss: dc_no={normalized_dc_no} not found")
                    continue

                matched = None
                for row in rows:
                    row_id, row_dc_no, row_dc_date, row_customer_id, row_customer_name, row_vehicle, row_modified = row

                    date_ok = True
                    if target_date is not None and row_dc_date is not None:
                        date_ok = row_dc_date == target_date

                    customer_ok = True
                    if target_customer:
                        customer_ok = (str(row_customer_name or '').strip().lower() == target_customer)

                    if date_ok and customer_ok:
                        matched = {
                            'id': row_id,
                            'dc_no': row_dc_no,
                            'dc_date': row_dc_date.isoformat() if row_dc_date else None,
                            'customer_id': row_customer_id,
                            'customer_name': row_customer_name,
                            'vehicle': row_vehicle,
                            'modified_on': str(row_modified) if row_modified else None,
                        }
                        break

                if matched is None:
                    row = rows[0]
                    matched = {
                        'id': row[0],
                        'dc_no': row[1],
                        'dc_date': row[2].isoformat() if row[2] else None,
                        'customer_id': row[3],
                        'customer_name': row[4],
                        'vehicle': row[5],
                        'modified_on': str(row[6]) if row[6] else None,
                    }
                    logger.warning(
                        "DB verify partial match for dc_no=%s; using latest row id=%s",
                        normalized_dc_no,
                        matched['id'],
                    )

                logger.info(
                    "VERIFIED: DC #%s exists in PostgreSQL (ID=%s, customer=%s, date=%s)",
                    normalized_dc_no,
                    matched['id'],
                    matched.get('customer_name'),
                    matched.get('dc_date'),
                )
                return {'exists': True, 'id': matched['id'], 'details': matched}

            except Exception as e:
                logger.error(f"DB verification error for dc_no={normalized_dc_no}: {e}")
            finally:
                if conn is not None:
                    conn.close()

        return {'exists': False, 'id': None, 'details': None}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    verifier = SyncVerifier()

    print("=== Sync Verifier Test ===\n")
    print(f"API Base: {verifier.api_base}")
    print(f"Entity ID: {verifier.entity_id}")
    print(f"Retry Count: {verifier.retry_count}")
    print(f"Retry Delay: {verifier.retry_delay}s")
    print("\nVerifier initialized successfully")
