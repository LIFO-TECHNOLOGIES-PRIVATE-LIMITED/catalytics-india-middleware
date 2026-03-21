"""
Test: Fetch Delivery Notes using Voucher Register
--------------------------------------------------

WHY THIS SCRIPT?
================

We tried 3 methods to fetch DCs from Tally with a date filter:

  METHOD 1 - Day Book report        : FAILED  (Tally ignores our date, returns wrong DC)
  METHOD 2 - Collection + FETCH     : FAILED  (Tally filters but sends empty data)
  METHOD 3 - Voucher Register       : WORKS!  (Tally filters by date AND returns full data)

HOW VOUCHER REGISTER WORKS:
============================
We send this XML to Tally:

  <REPORTNAME>Voucher Register</REPORTNAME>
  <SVFROMDATE>20260201</SVFROMDATE>   <-- from date
  <SVTODATE>20260201</SVTODATE>       <-- to date
  <VOUCHERTYPENAME>Delivery Note</VOUCHERTYPENAME>  <-- only DCs

Tally reads this and returns ONLY Delivery Notes in that date range.
No Python-side filtering needed. Tally does the work.

Usage:
    python test_voucher_register.py
"""

import os
import sys
import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime

# ── Load .env ─────────────────────────────────────────────────────────────────
ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config as cfg
cfg.load_env_file(cfg.resolve_env_path(ROOT_DIR))

TALLY_URL    = os.environ.get("TALLY_URL", "http://localhost:9000/")
COMPANY_NAME = os.environ.get("TALLY_COMPANY", "")
# ─────────────────────────────────────────────────────────────────────────────


def clean_xml(text):
    """Fix broken XML that Tally sometimes returns."""
    # Remove invalid control characters
    text = re.sub(r'&#(\d+);', lambda m: '' if (int(m.group(1)) < 32 and int(m.group(1)) not in (9, 10, 13)) else m.group(0), text)
    # Fix bare & that are not proper XML entities
    text = re.sub(r'&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9._:-]*;)', '&amp;', text)
    # Remove raw control chars
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    return text


def fetch_delivery_notes(tally_url, company, from_date, to_date):
    """
    Fetch Delivery Notes from Tally using Voucher Register report.

    This is the CORRECT method — unlike Day Book, Voucher Register
    respects SVFROMDATE / SVTODATE and filters at source in Tally.
    """
    xml = f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Voucher Register</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVFROMDATE>{from_date}</SVFROMDATE>
          <SVTODATE>{to_date}</SVTODATE>
          <VOUCHERTYPENAME>Delivery Note</VOUCHERTYPENAME>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>"""

    print(f"Sending request to Tally...")
    print(f"  URL     : {tally_url}")
    print(f"  Company : {company}")
    print(f"  From    : {from_date}")
    print(f"  To      : {to_date}")
    print(f"  Method  : Voucher Register")
    print()

    resp = requests.post(
        tally_url.rstrip("/"),
        data=xml.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8", "Connection": "close"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.text


def parse_vouchers(xml_text):
    """Parse Tally XML response and return list of voucher dicts."""
    cleaned = clean_xml(xml_text)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as e:
        print(f"ERROR: Could not parse Tally response: {e}")
        return []

    vouchers = []
    for v in root.findall('.//VOUCHER'):
        dc = {
            "vch_type"  : v.get("VCHTYPE") or v.findtext("VOUCHERTYPENAME") or "-",
            "vch_number": v.findtext("VOUCHERNUMBER") or "-",
            "date"      : v.findtext("DATE") or "-",
            "party"     : v.findtext("PARTYLEDGERNAME") or v.findtext("PARTYNAME") or "-",
            "narration" : v.findtext("NARRATION") or "",
            "items"     : [],
        }
        for item in v.findall('.//ALLINVENTORYENTRIES.LIST'):
            dc["items"].append({
                "name"  : item.findtext("STOCKITEMNAME") or "-",
                "qty"   : item.findtext("ACTUALQTY") or "-",
                "rate"  : item.findtext("RATE") or "-",
                "amount": item.findtext("AMOUNT") or "-",
            })
        vouchers.append(dc)
    return vouchers


def main():
    print("=" * 60)
    print("  Delivery Notes Fetch Test  (Voucher Register Method)")
    print("=" * 60)
    print()

    # ── Show loaded config ─────────────────────────────────────────────────
    print(f"Tally URL : {TALLY_URL}")
    print(f"Company   : {COMPANY_NAME}")
    print()

    if not COMPANY_NAME:
        print("ERROR: TALLY_COMPANY is not set in .env")
        sys.exit(1)

    # ── Prompt user for date range ─────────────────────────────────────────
    from_date = input("From date (YYYYMMDD, e.g. 20260201): ").strip()
    to_date   = input("To date   (YYYYMMDD, e.g. 20260228): ").strip()

    if not from_date or not to_date:
        print("ERROR: Both from_date and to_date are required.")
        sys.exit(1)

    try:
        datetime.strptime(from_date, "%Y%m%d")
        datetime.strptime(to_date, "%Y%m%d")
    except ValueError:
        print("ERROR: Dates must be in YYYYMMDD format (e.g. 20260201).")
        sys.exit(1)

    print()

    # Step 1: Fetch from Tally
    try:
        raw_xml = fetch_delivery_notes(TALLY_URL, COMPANY_NAME, from_date, to_date)
    except Exception as e:
        print(f"ERROR: Cannot connect to Tally: {e}")
        sys.exit(1)

    print(f"Response size : {len(raw_xml)} bytes")
    print()

    # Step 2: Parse
    vouchers = parse_vouchers(raw_xml)

    # Step 3: Print results
    print(f"Total DCs found: {len(vouchers)}")
    print()

    if not vouchers:
        print("No Delivery Notes found for this date range.")
        print()
        print("Possible reasons:")
        print("  1. No DCs exist for this date in Tally")
        print("  2. The voucher type name is different (not 'Delivery Note')")
        print("  3. Tally company name is wrong")
        return

    print(f"{'#':<4} {'DC No':<15} {'Date':<12} {'Party':<35} {'Items'}")
    print("-" * 75)
    for i, dc in enumerate(vouchers, 1):
        print(f"{i:<4} {dc['vch_number']:<15} {dc['date']:<12} {dc['party'][:34]:<35} {len(dc['items'])}")

    print()
    print("─" * 60)
    print("  Detailed View")
    print("─" * 60)
    for i, dc in enumerate(vouchers, 1):
        print(f"\nDC #{i}")
        print(f"  Voucher No : {dc['vch_number']}")
        print(f"  Date       : {dc['date']}")
        print(f"  Party      : {dc['party']}")
        print(f"  Type       : {dc['vch_type']}")
        if dc['narration']:
            print(f"  Narration  : {dc['narration']}")
        print(f"  Items ({len(dc['items'])}):")
        for item in dc['items']:
            print(f"    - {item['name']}")
            print(f"      Qty   : {item['qty']}")
            print(f"      Rate  : {item['rate']}")
            print(f"      Amount: {item['amount']}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
