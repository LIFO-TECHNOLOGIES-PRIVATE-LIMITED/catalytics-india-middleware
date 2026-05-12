"""
BHOX Tally Check Script
-----------------------
Step 1: Test Tally connection
Step 2: Fetch all masters (customers + products)
Step 3: Fetch all invoices

Uses config from .env — no arguments needed.
Run: python check_tally.py
"""
import logging
import sys
from datetime import datetime

from config import config
from tally_client import TallyClient
from fetch_customers import fetch_customers_from_all_companies
from fetch_products import fetch_products_from_all_companies
from fetch_invoices import fetch_invoices_from_all_companies

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def _separator(title=''):
    line = '=' * 60
    if title:
        logger.info(line)
        logger.info(f'  {title}')
        logger.info(line)
    else:
        logger.info(line)


def check_tally_connection(tally_url, active_companies):
    """Ping Tally and verify each active company is reachable."""
    _separator('STEP 0: TALLY CONNECTION CHECK')
    logger.info(f'Tally URL      : {tally_url}')
    logger.info(f'Active companies: {active_companies}')

    tally = TallyClient(tally_url)
    all_ok = True

    for company in active_companies:
        try:
            # A lightweight XML request — fetch company list to confirm Tally is alive
            xml = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
        <REPORTNAME>List of Companies</REPORTNAME>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>"""
            resp = tally.send_request(xml)
            if resp and len(resp.strip()) > 0:
                logger.info(f'  [OK] Tally responded for company: {company}')
            else:
                logger.warning(f'  [WARN] Empty response for company: {company}')
                all_ok = False
        except Exception as e:
            logger.error(f'  [FAIL] Cannot reach Tally for company "{company}": {e}')
            all_ok = False

    return all_ok


def check_masters():
    """Fetch all customers and products from Tally into SQLite."""
    _separator('STEP 1: FETCH MASTERS (CUSTOMERS)')
    logger.info('Fetching all customers from Tally...')
    cust_stats = fetch_customers_from_all_companies()
    logger.info(
        f'Customers -> Fetched: {cust_stats["total_fetched"]} | '
        f'Ledger-products: {cust_stats.get("skipped_non_customer", 0)} detected, '
        f'{cust_stats.get("ledger_products_saved", 0)} variants saved | '
        f'New: {cust_stats["new_saved"]} | '
        f'Updated: {cust_stats["updated"]} | '
        f'Errors: {cust_stats["errors"]}'
    )

    _separator('STEP 2: FETCH MASTERS (PRODUCTS)')
    logger.info('Fetching all products (stock items) from Tally...')
    prod_stats = fetch_products_from_all_companies()
    logger.info(
        f'Products  -> Fetched: {prod_stats["total_fetched"]} | '
        f'New: {prod_stats["new_saved"]} | '
        f'Updated: {prod_stats["duplicates_skipped"]} | '
        f'Errors: {prod_stats["errors"]}'
    )

    return cust_stats, prod_stats


def check_invoices():
    """Fetch all invoices from Tally into SQLite (uses INVOICE_FETCH_START_DATE from .env)."""
    start_date = config.get_invoice_fetch_start_date()
    _separator('STEP 3: FETCH INVOICES')
    if start_date:
        logger.info(f'Fetching invoices from {start_date} to today...')
        inv_stats = fetch_invoices_from_all_companies(from_date=start_date)
    else:
        logger.info('No INVOICE_FETCH_START_DATE set — fetching yesterday to tomorrow (Day Book).')
        inv_stats = fetch_invoices_from_all_companies()

    logger.info(
        f'Invoices  -> Fetched: {inv_stats["total_fetched"]} | '
        f'New: {inv_stats["new_saved"]} | '
        f'Updated: {inv_stats["updated_saved"]} | '
        f'Skipped (no delivery): {inv_stats["skipped_no_delivery"]} | '
        f'Errors: {inv_stats["errors"]}'
    )
    return inv_stats


def main():
    started_at = datetime.now()
    _separator('BHOX TALLY CHECK')
    logger.info(f'Started at: {started_at.strftime("%Y-%m-%d %H:%M:%S")}')
    logger.info(f'Entity    : {config.ENTITY_NAME} (ID: {config.ENTITY_ID})')
    logger.info(f'Tally URL : {config.TALLY_URL}')
    logger.info(f'SQLite DB : {config.SQLITE_DB_PATH}')

    active_companies = config.get_active_companies()
    if not active_companies:
        logger.error('No active Tally companies configured in .env — aborting.')
        sys.exit(1)

    # Step 0: connection check
    conn_ok = check_tally_connection(config.TALLY_URL, active_companies)
    if not conn_ok:
        logger.error('Tally connection failed. Make sure Tally is running and the URL is correct.')
        sys.exit(1)

    # Step 1 + 2: masters
    cust_stats, prod_stats = check_masters()

    # Step 3: invoices
    inv_stats = check_invoices()

    # Final summary
    elapsed = (datetime.now() - started_at).total_seconds()
    _separator('SUMMARY')
    logger.info(
        f'Customers : {cust_stats["total_fetched"]} fetched | '
        f'{cust_stats.get("skipped_non_customer", 0)} ledger-products detected '
        f'({cust_stats.get("ledger_products_saved", 0)} variant rows saved) | '
        f'{cust_stats["new_saved"]} customers new | '
        f'{cust_stats["errors"]} errors'
    )
    logger.info(f'Products  : {prod_stats["total_fetched"]} fetched, {prod_stats["new_saved"]} new, {prod_stats["errors"]} errors')
    logger.info(f'Invoices  : {inv_stats["total_fetched"]} fetched, {inv_stats["new_saved"]} new, {inv_stats["errors"]} errors')
    logger.info(f'Total time: {elapsed:.1f}s')
    _separator()
    logger.info('Done.')


if __name__ == '__main__':
    main()
