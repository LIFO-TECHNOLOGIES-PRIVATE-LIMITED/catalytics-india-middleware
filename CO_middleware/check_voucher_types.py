#!/usr/bin/env python3
"""Check what voucher types exist in Tally"""
import sys
import os
ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, ROOT_DIR)

import config as cfg
import tally_client

cfg.load_env_file(cfg.resolve_env_path(ROOT_DIR))

url = cfg.get_env("TALLY_URL")
company = cfg.get_env("TALLY_COMPANY")

print(f"Checking voucher types in Tally...")
print(f"URL: {url}")
print(f"Company: {company}")
print()

# Try without date filter to see if ANY vouchers exist
xml = f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>AllVouchers</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" NAME="AllVouchers">
            <TYPE>Voucher</TYPE>
            <FETCH>VOUCHERTYPENAME</FETCH>
            <FETCH>VOUCHERNUMBER</FETCH>
            <FETCH>DATE</FETCH>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""

try:
    print("Fetching ALL vouchers (no date filter) - this may take time...")
    resp = tally_client.send_request(xml, url)
    vouchers = tally_client.parse_delivery_notes(resp)
    
    print(f"Found {len(vouchers)} total vouchers")
    print()
    
    # Show unique voucher types
    types = set()
    for v in vouchers:
        vt = v.get("VOUCHERTYPENAME", "")
        if vt:
            types.add(vt)
    
    print("Voucher types found:")
    for vt in sorted(types):
        count = sum(1 for v in vouchers if v.get("VOUCHERTYPENAME") == vt)
        print(f"  {vt}: {count} vouchers")
    
    print()
    print("Sample vouchers:")
    for i, v in enumerate(vouchers[:5]):
        print(f"  {i+1}. Type: {v.get('VOUCHERTYPENAME')}, No: {v.get('VOUCHERNUMBER')}, Date: {v.get('DATE')}")
        
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
