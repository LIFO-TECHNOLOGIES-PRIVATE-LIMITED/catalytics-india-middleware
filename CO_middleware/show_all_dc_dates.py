#!/usr/bin/env python3
"""Show all DC dates in Tally to understand the date format"""
import os
import sys

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, ROOT_DIR)

import config as cfg
import tally_api

cfg.load_env_file(cfg.resolve_env_path(ROOT_DIR))

tally_url = cfg.get_env("TALLY_URL")
company = cfg.get_env("TALLY_COMPANY")

print("Fetching ALL DCs to show dates...")
print()

# Fetch with very wide date range to get everything
vouchers = tally_api.get_delivery_notes(company, tally_url, "20200101", "20991231")

print(f"Found {len(vouchers)} DCs")
print()

if vouchers:
    print("DC Dates:")
    print("-" * 70)
    for i, v in enumerate(vouchers, 1):
        dc_no = v.get("VOUCHERNUMBER") or v.get("VOUCHERNO") or "?"
        date = v.get("DATE") or "?"
        vtype = v.get("VOUCHERTYPENAME") or "?"
        party = v.get("PARTYLEDGERNAME") or "?"
        print(f"{i:2d}. DC#{dc_no:10s} Date:{date:10s} Type:{vtype:20s} Party:{party}")
    
    print()
    print("Today's date for comparison:")
    from datetime import datetime
    print(f"  Today: {datetime.now().strftime('%Y%m%d')}")
    print(f"  Yesterday: {(datetime.now() - __import__('datetime').timedelta(days=1)).strftime('%Y%m%d')}")
else:
    print("No DCs found in Tally")
