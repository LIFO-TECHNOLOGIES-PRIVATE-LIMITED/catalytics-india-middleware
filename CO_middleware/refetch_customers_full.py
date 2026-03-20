#!/usr/bin/env python3
"""Manually re-fetch customers with full details"""
import os
import sys

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, ROOT_DIR)

from fetch_customers import build_config, run_once
from types import SimpleNamespace
import config as cfg

cfg.load_env_file(cfg.resolve_env_path(ROOT_DIR))

print("=" * 70)
print("RE-FETCHING CUSTOMERS WITH FULL DETAILS")
print("=" * 70)
print("This will fetch GST, PAN, address, and contact details")
print("Estimated time: ~75 minutes (5 customers × 10s cooldown)")
print("=" * 70)
print()

args = SimpleNamespace(
    config=cfg.resolve_env_path(ROOT_DIR),
    db_path=cfg.get_env("TALLY_DB_PATH"),
    tally_url=cfg.get_env("TALLY_URL"),
    company=cfg.get_env("TALLY_COMPANY"),
    entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
    fetch_full=True,  # ENABLE FULL FETCH
    log_level="INFO",
    log_json=False,
    log_file=None
)

config = build_config(args)

print(f"Database: {config.db_path}")
print(f"Tally URL: {config.tally_url}")
print(f"Company: {config.company}")
print(f"Fetch Full: {config.fetch_full}")
print()
print("Starting fetch...")
print()

try:
    result = run_once(config)
    print()
    print("=" * 70)
    print("FETCH COMPLETE")
    print("=" * 70)
    print(f"Created: {result.get('created', 0)}")
    print(f"Updated: {result.get('updated', 0)}")
    print(f"Skipped: {result.get('skipped', 0)}")
    print(f"Deleted: {result.get('deleted', 0)}")
    print()
    print("Now run: python check_customer_data.py")
    print("To verify full details are saved")
except Exception as e:
    print()
    print("=" * 70)
    print("FETCH FAILED")
    print("=" * 70)
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
