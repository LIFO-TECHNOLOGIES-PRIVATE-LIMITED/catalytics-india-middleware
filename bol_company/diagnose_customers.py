from config import config
from tally_client import send_request, parse_delivery_notes
from db import Database, json_loads
import logging
from xml.sax.saxutils import escape as xml_escape

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def check_missing_customers(company, date="20221201"):
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
          <SVFROMDATE>{date}</SVFROMDATE>
          <SVTODATE>{date}</SVTODATE>
        </STATICVARIABLES>
        <REPORTNAME>Day Book</REPORTNAME>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>
"""
    try:
        resp = send_request(xml, config.TALLY_URL, timeout=60)
        vouchers = parse_delivery_notes(resp)
        db = Database(config.SQLITE_DB_PATH)
        
        logger.info(f"\nChecking customers for {len(vouchers)} vouchers in {company}...")
        
        found = 0
        missing = set()
        
        for v in vouchers:
            vtype = (v.get('VOUCHERTYPENAME') or v.get('VOUCHERTYPE') or '').strip().lower()
            if 'sale' not in vtype and 'invoice' not in vtype:
                continue
                
            cust_name = (v.get('PARTYLEDGERNAME') or '').strip()
            if not cust_name:
                continue
                
            normalized = cust_name.replace(' ', '').lower()
            row = db.query("SELECT name FROM customers WHERE lower(replace(name, ' ', '')) = ?", (normalized,))
            
            if row:
                found += 1
            else:
                missing.add(cust_name)
                
        logger.info(f"Found: {found}")
        logger.info(f"Missing (Unique): {len(missing)}")
        if missing:
            logger.info("\nSample Missing Customer Names (First 20):")
            for name in sorted(list(missing))[:20]:
                logger.info(f"  - {name}")
                
        db.close()
    except Exception as e:
        logger.error(f"Failed: {e}")

if __name__ == '__main__':
    active = config.get_active_companies()
    if active:
        check_missing_customers(active[0])
