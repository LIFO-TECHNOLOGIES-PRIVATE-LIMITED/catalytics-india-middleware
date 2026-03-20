"""
test_dc_date_filter.py
======================
Test that SVFROMDATE / SVTODATE filtering is actually working in get_delivery_notes.

What this test does:
  1. Fetches DCs for the FULL financial year  -> should return N vouchers
  2. Fetches DCs for a NARROW window (last 7 days) -> should return <= N
  3. Fetches DCs for an IMPOSSIBLE future date    -> should return 0
  4. Verifies every returned DC actually falls inside the requested date range

Run:
  python test_dc_date_filter.py
  python test_dc_date_filter.py --url http://10.152.29.28:9000/ --company "chennai oxygen"
"""

import argparse
import os
import sys
from datetime import datetime, timedelta

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config as cfg
import tally_client

ENV_PATH = cfg.resolve_env_path(ROOT_DIR)
cfg.load_env_file(ENV_PATH)

DEFAULT_URL     = cfg.get_env("TALLY_URL", "http://localhost:9000/")
DEFAULT_COMPANY = cfg.get_env("TALLY_COMPANY", "")

# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def fy_range():
    """Return (from_date, to_date) for the current financial year (Apr–Mar)."""
    now = datetime.now()
    if now.month >= 4:
        fy_start = datetime(now.year, 4, 1)
    else:
        fy_start = datetime(now.year - 1, 4, 1)
    return fy_start.strftime("%Y%m%d"), now.strftime("%Y%m%d")


def last_n_days(n: int):
    now = datetime.now()
    return (now - timedelta(days=n)).strftime("%Y%m%d"), now.strftime("%Y%m%d")


def check_all_dates_in_range(vouchers, from_date, to_date):
    """Returns list of vouchers whose date falls OUTSIDE the range."""
    bad = []
    for v in vouchers:
        d = v.get("DATE", "")
        if not d or d < from_date or d > to_date:
            bad.append(v)
    return bad


def print_sep(label=""):
    print("\n" + "=" * 65)
    if label:
        print(f"  {label}")
        print("=" * 65)


def print_vouchers(vouchers, limit=10):
    for i, v in enumerate(vouchers[:limit]):
        print(f"  [{i+1}] DC#: {v.get('VOUCHERNUMBER','?'):20s}  Date: {v.get('DATE','?')}  Party: {v.get('PARTYLEDGERNAME','?')}")
    if len(vouchers) > limit:
        print(f"  ... and {len(vouchers) - limit} more")


# ─────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────

def run_tests(url: str, company: str):
    print(f"\nTally URL : {url}")
    print(f"Company   : {company}")

    results = []

    # ── Test 1: Full financial year ──────────────────────
    print_sep("TEST 1 — Full financial year fetch")
    fy_from, fy_to = fy_range()
    print(f"  Date range: {fy_from} → {fy_to}")

    fy_vouchers = tally_client.get_delivery_notes(company, url, fy_from, fy_to)
    bad = check_all_dates_in_range(fy_vouchers, fy_from, fy_to)

    print(f"  Returned : {len(fy_vouchers)} DCs")
    print_vouchers(fy_vouchers)

    if bad:
        print(f"  FAIL — {len(bad)} DCs have dates OUTSIDE the requested range!")
        for b in bad:
            print(f"    DC#: {b.get('VOUCHERNUMBER','?')}  Date: {b.get('DATE','?')}")
        results.append(("Full FY fetch", "FAIL", f"{len(bad)} out-of-range DCs"))
    else:
        print(f"  PASS — All {len(fy_vouchers)} DCs are within {fy_from}–{fy_to}")
        results.append(("Full FY fetch", "PASS", f"{len(fy_vouchers)} DCs"))

    # ── Test 2: Last 7 days (narrow window) ──────────────
    print_sep("TEST 2 — Last 7 days (narrow window)")
    w_from, w_to = last_n_days(7)
    print(f"  Date range: {w_from} → {w_to}")

    week_vouchers = tally_client.get_delivery_notes(company, url, w_from, w_to)
    bad2 = check_all_dates_in_range(week_vouchers, w_from, w_to)

    print(f"  Returned : {len(week_vouchers)} DCs  (expected ≤ {len(fy_vouchers)} from FY test)")
    print_vouchers(week_vouchers)

    if bad2:
        print(f"  FAIL — {len(bad2)} DCs have dates OUTSIDE the 7-day window!")
        for b in bad2:
            print(f"    DC#: {b.get('VOUCHERNUMBER','?')}  Date: {b.get('DATE','?')}")
        results.append(("Last 7 days", "FAIL", f"{len(bad2)} out-of-range DCs"))
    elif len(week_vouchers) > len(fy_vouchers):
        print("  FAIL — Narrow window returned MORE DCs than the full FY. Filter not working.")
        results.append(("Last 7 days", "FAIL", "Narrow > FY count"))
    else:
        print(f"  PASS — All {len(week_vouchers)} DCs are within the 7-day window")
        results.append(("Last 7 days", "PASS", f"{len(week_vouchers)} DCs"))

    # ── Test 3: Impossible future date range ─────────────
    print_sep("TEST 3 — Impossible future date (should return 0 DCs)")
    future_from = "20991201"
    future_to   = "20991231"
    print(f"  Date range: {future_from} → {future_to}")

    future_vouchers = tally_client.get_delivery_notes(company, url, future_from, future_to)
    print(f"  Returned : {len(future_vouchers)} DCs")

    if len(future_vouchers) == 0:
        print("  PASS — Correctly returned 0 DCs for a future date range")
        results.append(("Future date (expect 0)", "PASS", "0 DCs"))
    else:
        print(f"  FAIL — Got {len(future_vouchers)} DCs for a future date. Filter is NOT working!")
        print_vouchers(future_vouchers)
        results.append(("Future date (expect 0)", "FAIL", f"{len(future_vouchers)} DCs returned"))

    # ── Test 4: Single day ────────────────────────────────
    print_sep("TEST 4 — Single day (today)")
    today = datetime.now().strftime("%Y%m%d")
    print(f"  Date range: {today} → {today}")

    today_vouchers = tally_client.get_delivery_notes(company, url, today, today)
    bad4 = check_all_dates_in_range(today_vouchers, today, today)

    print(f"  Returned : {len(today_vouchers)} DCs")
    print_vouchers(today_vouchers)

    if bad4:
        print(f"  FAIL — {len(bad4)} DCs have dates outside today!")
        results.append(("Single day (today)", "FAIL", f"{len(bad4)} wrong-date DCs"))
    else:
        print(f"  PASS — All {len(today_vouchers)} DCs are dated today")
        results.append(("Single day (today)", "PASS", f"{len(today_vouchers)} DCs"))

    # ── Summary ───────────────────────────────────────────
    print_sep("SUMMARY")
    all_pass = True
    for test_name, status, detail in results:
        icon = "✓" if status == "PASS" else "✗"
        print(f"  {icon}  {status}  |  {test_name:<30s}  |  {detail}")
        if status != "PASS":
            all_pass = False

    print()
    if all_pass:
        print("  ALL TESTS PASSED — Date filtering is working correctly.")
        print("  Client Tally will only receive DCs within the requested range.")
    else:
        print("  SOME TESTS FAILED — Date filtering may not be fully respected.")
        print("  Check Tally version. Python-level safety filter is still active.")
    print()

    return all_pass


# ─────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test DC date filter in Tally")
    parser.add_argument("--url",     default=DEFAULT_URL,     help="Tally URL (default from .env)")
    parser.add_argument("--company", default=DEFAULT_COMPANY, help="Tally company name (default from .env)")
    args = parser.parse_args()

    if not args.company:
        print("ERROR: No company name. Set TALLY_COMPANY in .env or pass --company")
        sys.exit(1)

    success = run_tests(args.url, args.company)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
