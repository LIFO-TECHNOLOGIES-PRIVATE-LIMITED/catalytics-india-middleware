from config import config
from tally_client import send_request, parse_delivery_notes
import logging
from collections import Counter
from xml.sax.saxutils import escape as xml_escape
import sys
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def fetch_vouchers_summary(company, from_date, to_date):
    safe_company = xml_escape(company)
    xml = f"""
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVFROMDATE>{from_date}</SVFROMDATE>
          <SVTODATE>{to_date}</SVTODATE>
        </STATICVARIABLES>
        <REPORTNAME>Day Book</REPORTNAME>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>
"""
    logger.info(f"\n{'='*60}")
    logger.info(f"Fetching: {company}")
    logger.info(f"Period:   {from_date} to {to_date}")
    logger.info(f"{'='*60}")

    try:
        resp = send_request(xml, config.TALLY_URL, timeout=300)
        raw_vouchers = parse_delivery_notes(resp)
        
        types = Counter()
        dates = Counter()
        for v in raw_vouchers:
            vtype = (v.get('VOUCHERTYPENAME') or v.get('VOUCHERTYPE') or v.get('VCHTYPE') or 'Unknown').strip()
            types[vtype] += 1
            vd = (v.get('DATE') or v.get('VOUCHERDATE') or 'unknown').strip()
            dates[vd] += 1
        
        logger.info(f"Total Vouchers: {len(raw_vouchers)}")
        logger.info("\nVoucher Types Breakdown:")
        for t, count in types.most_common():
            logger.info(f"  - {t:<30}: {count}")
            
        logger.info("\nDates Summary (Top 10):")
        for d, count in dates.most_common(10):
            logger.info(f"  - {d}: {count}")
            
    except Exception as e:
        logger.error(f"Error fetching data for {company}: {e}")

if __name__ == '__main__':
    # Use dates from command line or defaults
    from_date = sys.argv[1] if len(sys.argv) > 1 else "20220401"
    to_date = sys.argv[2] if len(sys.argv) > 2 else "20230331"
    
    active_companies = config.get_active_companies()
    if not active_companies:
        logger.error("No active companies found in .env")
        sys.exit(1)
        
    for company in active_companies:
        fetch_vouchers_summary(company, from_date, to_date)
