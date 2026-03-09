#!/usr/bin/env python3
"""
Single comprehensive test script for CO Middleware
Tests: Database, Tally connection, Products, Customers, DCs, Sync
"""
import os
import sys
import json

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, ROOT_DIR)

import config as cfg
import db
import tally_api

cfg.load_env_file(cfg.resolve_env_path(ROOT_DIR))

def print_section(title):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)

def test_config():
    print_section("1. CONFIGURATION CHECK")
    print(f"Tally URL: {cfg.get_env('TALLY_URL')}")
    print(f"Tally Company: {cfg.get_env('TALLY_COMPANY')}")
    print(f"Database: {cfg.get_env('TALLY_DB_PATH')}")
    print(f"Catalytics API: {cfg.get_env('CATALYTICS_API_BASE_URL')}")
    print(f"Entity ID: {cfg.get_env_int('CATALYTICS_ENTITY_ID')}")

def test_database():
    print_section("2. DATABASE CHECK")
    db_path = cfg.get_env("TALLY_DB_PATH")
    if not os.path.exists(db_path):
        print(f"✗ Database not found: {db_path}")
        return False
    
    conn = db.connect(db_path)
    
    # Check tables
    tables = {
        'companies': conn.execute("SELECT COUNT(*) as c FROM companies").fetchone()['c'],
        'ledgers': conn.execute("SELECT COUNT(*) as c FROM ledgers").fetchone()['c'],
        'stock_items': conn.execute("SELECT COUNT(*) as c FROM stock_items").fetchone()['c'],
        'delivery_notes': conn.execute("SELECT COUNT(*) as c FROM delivery_notes").fetchone()['c'],
    }
    
    print(f"✓ Database found: {db_path}")
    print(f"  Companies: {tables['companies']}")
    print(f"  Customers: {tables['ledgers']}")
    print(f"  Products: {tables['stock_items']}")
    print(f"  DCs: {tables['delivery_notes']}")
    
    return True

def test_tally_connection():
    print_section("3. TALLY CONNECTION CHECK")
    tally_url = cfg.get_env("TALLY_URL")
    company = cfg.get_env("TALLY_COMPANY")
    
    try:
        companies = tally_api.get_companies(tally_url)
        print(f"✓ Connected to Tally at {tally_url}")
        print(f"  Found {len(companies)} companies")
        
        company_names = [c.get('name') for c in companies]
        if company in company_names:
            print(f"✓ Company '{company}' found")
            return True
        else:
            print(f"✗ Company '{company}' not found")
            print(f"  Available: {', '.join(company_names)}")
            return False
    except Exception as e:
        print(f"✗ Failed to connect to Tally: {e}")
        return False

def test_product_data():
    print_section("4. PRODUCT DATA CHECK")
    db_path = cfg.get_env("TALLY_DB_PATH")
    conn = db.connect(db_path)
    
    product = conn.execute("SELECT * FROM stock_items LIMIT 1").fetchone()
    if not product:
        print("✗ No products in database")
        return
    
    data = json.loads(product['data_json'])
    
    print(f"Sample Product: {product['name']}")
    print(f"  GUID: {data.get('GUID', '(not set)')}")
    print(f"  MASTERID: {data.get('MASTERID', '(not set)')}")
    print(f"  BASEUNITS: {data.get('BASEUNITS', '(not set)')}")
    print(f"  HSNCODE: {data.get('HSNCODE', '(not set)')}")
    
    # Test payload generation
    from sync_products import _build_product_payload
    payload = _build_product_payload(
        dict(product),
        entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
        company_name=cfg.get_env("TALLY_COMPANY"),
    )
    
    print(f"\nSync Payload Preview:")
    print(f"  stock_item_name: {payload['stock_item_name']}")
    print(f"  product_master_name: {payload['product_master_name']}")
    print(f"  unit_master_name: {payload['unit_master_name']}")
    print(f"  variant_name: {payload['variant_name']}")
    print(f"  guid: {payload['guid']}")
    print(f"  hsn_code: {payload['hsn_code']}")

def test_customer_data():
    print_section("5. CUSTOMER DATA CHECK")
    db_path = cfg.get_env("TALLY_DB_PATH")
    conn = db.connect(db_path)
    
    customer = conn.execute("SELECT * FROM ledgers LIMIT 1").fetchone()
    if not customer:
        print("✗ No customers in database")
        return
    
    data = json.loads(customer['data_json'])
    
    print(f"Sample Customer: {customer['name']}")
    print(f"  GUID: {data.get('GUID', '(not set)')}")
    print(f"  PARENT: {data.get('PARENT', '(not set)')}")
    
    # Check for full details
    has_full = (
        "LEDMAILINGDETAILS_LIST" in data or
        "GSTDETAILS_LIST" in data or
        "CONTACTDETAILS_LIST" in data
    )
    
    if has_full:
        print(f"  ✓ Has full details (GST, PAN, address)")
        if "GSTDETAILS_LIST" in data and data["GSTDETAILS_LIST"]:
            gst = data["GSTDETAILS_LIST"][0]
            print(f"    GSTIN: {gst.get('GSTIN', '(not set)')}")
            print(f"    STATE: {gst.get('STATE', '(not set)')}")
        if "LEDMAILINGDETAILS_LIST" in data and data["LEDMAILINGDETAILS_LIST"]:
            mail = data["LEDMAILINGDETAILS_LIST"][0]
            print(f"    PINCODE: {mail.get('PINCODE', '(not set)')}")
            print(f"    COUNTRY: {mail.get('COUNTRY', '(not set)')}")
    else:
        print(f"  ✗ Basic details only (no GST/PAN/address)")
        print(f"    Run 'Fetch Customers' with fetch_full=True to get full details")

def test_dc_data():
    print_section("6. DELIVERY CHALLAN (DC) DATA CHECK")
    db_path = cfg.get_env("TALLY_DB_PATH")
    conn = db.connect(db_path)
    
    dc = conn.execute("SELECT * FROM delivery_notes LIMIT 1").fetchone()
    if not dc:
        print("✗ No DCs in database")
        return
    
    data = json.loads(dc['data_json'])
    
    print(f"Sample DC: {dc['dc_no']}")
    print(f"  Date: {dc['voucher_date']}")
    print(f"  Party: {dc['party_ledger_name']}")
    print(f"  Reference: {dc['reference']}")
    
    # Check items
    items = conn.execute(
        "SELECT * FROM delivery_note_items WHERE delivery_note_id = ?",
        (dc['id'],)
    ).fetchall()
    
    print(f"  Items: {len(items)}")
    if items:
        item = items[0]
        print(f"    Sample: {item['stock_name']} x {item['qty']} @ {item['rate']}")
        print(f"    Godown: {item['godown_name'] or '(not set)'}")

def test_sync_status():
    print_section("7. SYNC STATUS CHECK")
    db_path = cfg.get_env("TALLY_DB_PATH")
    conn = db.connect(db_path)
    
    # Products
    prod_unsynced = conn.execute(
        "SELECT COUNT(*) as c FROM stock_sync_status WHERE is_synced = 0"
    ).fetchone()['c']
    prod_synced = conn.execute(
        "SELECT COUNT(*) as c FROM stock_sync_status WHERE is_synced = 1"
    ).fetchone()['c']
    
    # Customers
    cust_unsynced = conn.execute(
        "SELECT COUNT(*) as c FROM ledger_sync_status WHERE is_synced = 0"
    ).fetchone()['c']
    cust_synced = conn.execute(
        "SELECT COUNT(*) as c FROM ledger_sync_status WHERE is_synced = 1"
    ).fetchone()['c']
    
    # DCs
    dc_unsynced = conn.execute(
        "SELECT COUNT(*) as c FROM sync_status WHERE is_synced = 0"
    ).fetchone()['c']
    dc_synced = conn.execute(
        "SELECT COUNT(*) as c FROM sync_status WHERE is_synced = 1"
    ).fetchone()['c']
    
    print(f"Products: {prod_synced} synced, {prod_unsynced} pending")
    print(f"Customers: {cust_synced} synced, {cust_unsynced} pending")
    print(f"DCs: {dc_synced} synced, {dc_unsynced} pending")

def main():
    print("\n" + "=" * 70)
    print("  CO MIDDLEWARE - COMPREHENSIVE TEST")
    print("=" * 70)
    
    test_config()
    test_database()
    test_tally_connection()
    test_product_data()
    test_customer_data()
    test_dc_data()
    test_sync_status()
    
    print("\n" + "=" * 70)
    print("  TEST COMPLETE")
    print("=" * 70)
    print("\nTo run specific operations:")
    print("  - Fetch data: Use dashboard buttons or automation")
    print("  - Sync data: Click 'Sync' buttons in dashboard")
    print("  - View logs: Check dashboard 'Logs' tab")
    print()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n✗ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
