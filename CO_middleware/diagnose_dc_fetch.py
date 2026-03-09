#!/usr/bin/env python3
"""
Diagnose DC fetch to understand what's causing Tally crashes
"""
import os
import sys
from datetime import datetime, timedelta

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, ROOT_DIR)

import config as cfg
import tally_api

cfg.load_env_file(cfg.resolve_env_path(ROOT_DIR))

print("=" * 70)
print("DC FETCH DIAGNOSTIC")
print("=" * 70)

tally_url = cfg.get_env("TALLY_URL")
company = cfg.get_env("TALLY_COMPANY")
days_back = cfg.get_env_int("TALLY_DAYS_BACK") or 7

print(f"Tally URL: {tally_url}")
print(f"Company: {company}")
print(f"Days Back: {days_back}")
print()

# Calculate date range
now = datetime.now()
from_dt = now - timedelta(days=days_back)
from_date = from_dt.strftime("%Y%m%d")
to_date = now.strftime("%Y%m%d")

print(f"Date Range: {from_date} to {to_date}")
print(f"  From: {from_dt.strftime('%Y-%m-%d')}")
print(f"  To: {now.strftime('%Y-%m-%d')}")
print()

print("=" * 70)
print("FETCHING DCs FROM TALLY (this will take ~10 seconds due to cooldown)")
print("=" * 70)

try:
    vouchers = tally_api.get_delivery_notes(company, tally_url, from_date, to_date)
    
    print(f"\n✓ Successfully fetched {len(vouchers)} DCs")
    print()
    
    if vouchers:
        print("Sample DC:")
        dc = vouchers[0]
        print(f"  DC No: {dc.get('VOUCHERNUMBER')}")
        print(f"  Date: {dc.get('DATE')}")
        print(f"  Party: {dc.get('PARTYLEDGERNAME')}")
        print(f"  Type: {dc.get('VOUCHERTYPENAME')}")
        
        # Count inventory items
        inventory = dc.get('INVENTORY') or []
        print(f"  Items: {len(inventory)}")
        
        # Check data size
        import json
        dc_json = json.dumps(dc)
        print(f"  Data size: {len(dc_json)} bytes")
    
    print()
    print("=" * 70)
    print("ANALYSIS")
    print("=" * 70)
    
    total_items = sum(len(v.get('INVENTORY') or []) for v in vouchers)
    print(f"Total DCs: {len(vouchers)}")
    print(f"Total Items: {total_items}")
    print(f"Avg Items per DC: {total_items / len(vouchers) if vouchers else 0:.1f}")
    
    # Estimate data size
    import json
    total_size = sum(len(json.dumps(v)) for v in vouchers)
    print(f"Total Data Size: {total_size / 1024:.1f} KB")
    print(f"Avg Size per DC: {total_size / len(vouchers) if vouchers else 0:.0f} bytes")
    
    print()
    print("=" * 70)
    print("RECOMMENDATIONS")
    print("=" * 70)
    
    if len(vouchers) > 100:
        print("⚠ WARNING: More than 100 DCs in date range")
        print("  Consider reducing TALLY_DAYS_BACK to 3-5 days")
    elif len(vouchers) > 50:
        print("⚠ CAUTION: 50-100 DCs in date range")
        print("  Monitor Tally for stability")
    else:
        print("✓ DC count is reasonable (<50)")
    
    if total_size > 500 * 1024:  # 500 KB
        print("⚠ WARNING: Large data size (>500 KB)")
        print("  This may cause Tally to crash on 8GB RAM systems")
    elif total_size > 200 * 1024:  # 200 KB
        print("⚠ CAUTION: Moderate data size (200-500 KB)")
    else:
        print("✓ Data size is reasonable (<200 KB)")
    
    print()
    print("Current settings:")
    print(f"  TALLY_DAYS_BACK={days_back}")
    print(f"  Cooldown=10 seconds")
    print(f"  Fetch interval=2 minutes")
    
    if len(vouchers) > 50:
        recommended_days = max(1, int(50 * days_back / len(vouchers)))
        print()
        print(f"Recommended: Set TALLY_DAYS_BACK={recommended_days} to fetch ~50 DCs")

except Exception as e:
    print(f"\n✗ Failed to fetch DCs: {e}")
    import traceback
    traceback.print_exc()
    print()
    print("This error indicates Tally crashed or is not responding.")
    print("Try:")
    print("  1. Restart Tally")
    print("  2. Reduce TALLY_DAYS_BACK to 3 or less")
    print("  3. Check Tally server has enough RAM")
