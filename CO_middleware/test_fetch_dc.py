"""
Manual DC Fetch Test Script
----------------------------
Run this script to fetch Delivery Chalans from Tally for a custom date range.
It prints the results to the console without saving to the database.

Usage:
    python test_fetch_dc.py

You will be prompted to enter:
  - Tally URL       (default: http://localhost:9000/)
  - Company name
  - From date       (format: YYYYMMDD  e.g. 20250101)
  - To date         (format: YYYYMMDD  e.g. 20250331)
"""

import os
import sys

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config as cfg
import tally_api

# ── Load .env if present ──────────────────────────────────────────────────────
env_path = cfg.resolve_env_path(ROOT_DIR)
cfg.load_env_file(env_path)

# ── Prompt user for inputs ────────────────────────────────────────────────────
default_url     = os.environ.get("TALLY_URL", "http://localhost:9000/")
default_company = os.environ.get("TALLY_COMPANY", "")

print("=" * 60)
print("  Manual DC Fetch Test")
print("=" * 60)

tally_url = input(f"Tally URL [{default_url}]: ").strip() or default_url
company   = input(f"Company name [{default_company}]: ").strip() or default_company
from_date = input("From date (YYYYMMDD, e.g. 20250101): ").strip()
to_date   = input("To date   (YYYYMMDD, e.g. 20250331): ").strip()

if not company:
    print("ERROR: Company name is required.")
    sys.exit(1)

if not from_date or not to_date:
    print("ERROR: Both from_date and to_date are required.")
    sys.exit(1)

# ── Validate date format ──────────────────────────────────────────────────────
from datetime import datetime

def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%d")

try:
    _parse_date(from_date)
    _parse_date(to_date)
except ValueError:
    print("ERROR: Dates must be in YYYYMMDD format (e.g. 20250101).")
    sys.exit(1)

# ── Fetch DCs ─────────────────────────────────────────────────────────────────
print()
print(f"Fetching DCs from Tally...")
print(f"  URL     : {tally_url}")
print(f"  Company : {company}")
print(f"  From    : {from_date}")
print(f"  To      : {to_date}")
print("-" * 60)

try:
    vouchers = tally_api.get_delivery_notes(company, tally_url, from_date, to_date)
except Exception as e:
    print(f"ERROR: Failed to fetch DCs: {e}")
    sys.exit(1)

# ── Print results ─────────────────────────────────────────────────────────────
print(f"Total DCs fetched: {len(vouchers)}")
print()

if not vouchers:
    print("No delivery notes found for the given date range.")
    print()
    print("Fetching ALL DCs (no date filter) to show what dates exist in Tally...")
    try:
        import tally_client
        all_vouchers = tally_client.get_delivery_notes(company, tally_url, "20000101", "20991231")
        if all_vouchers:
            print(f"Found {len(all_vouchers)} total DCs in Tally. Dates available:")
            dates = sorted(set(v.get("DATE", "?") for v in all_vouchers))
            for d in dates:
                count = sum(1 for v in all_vouchers if v.get("DATE") == d)
                print(f"  {d}  ({count} DC{'s' if count > 1 else ''})")
            print()
            print("Use one of these dates as your from/to date and retry.")
        else:
            print("No DCs found in Tally at all — check company name and Tally connection.")
    except Exception as e:
        print(f"Could not fetch all DCs: {e}")
else:
    print(f"{'#':<5} {'DC Number':<20} {'Date':<12} {'Party':<30} {'Items':<6}")
    print("-" * 80)
    for idx, v in enumerate(vouchers, 1):
        dc_no   = v.get("VOUCHERNUMBER") or v.get("VOUCHERNO") or v.get("VCHNUMBER") or "-"
        date    = v.get("DATE") or "-"
        party   = (v.get("PARTYLEDGERNAME") or v.get("PARTYNAME") or "-")[:29]
        items   = len(v.get("INVENTORY") or [])
        print(f"{idx:<5} {dc_no:<20} {date:<12} {party:<30} {items:<6}")

    print()
    print("--- Detailed view of first DC ---")
    first = vouchers[0]
    for key, val in first.items():
        if key == "INVENTORY":
            print(f"  INVENTORY ({len(val)} items):")
            for i, item in enumerate(val[:5], 1):
                print(f"    [{i}] {item}")
            if len(val) > 5:
                print(f"    ... and {len(val)-5} more items")
        else:
            print(f"  {key}: {val}")

print()
print("Done.")
