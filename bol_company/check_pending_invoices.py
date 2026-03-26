"""
Check pending invoices in SQLite database.
Run from middleware directory: python check_pending_invoices.py
"""
import sys
import os
import sqlite3

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import config
import config

# Load configuration
config.reload_from_env()

print("=" * 70)
print("PENDING INVOICES CHECK")
print("=" * 70)
print()

# Connect to SQLite
db_path = config.SQLITE_DB_PATH
print(f"Database: {db_path}")
print()

try:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get total invoices
    cursor.execute("SELECT COUNT(*) as total FROM invoices")
    total = cursor.fetchone()['total']
    
    # Get synced invoices
    cursor.execute("SELECT COUNT(*) as synced FROM invoices WHERE is_synced = 1")
    synced = cursor.fetchone()['synced']
    
    # Get unsynced invoices
    cursor.execute("SELECT COUNT(*) as unsynced FROM invoices WHERE is_synced = 0")
    unsynced = cursor.fetchone()['unsynced']
    
    print(f"Total Invoices:    {total}")
    print(f"Synced:            {synced}")
    print(f"Pending (Unsynced): {unsynced}")
    print()
    
    if unsynced > 0:
        print("=" * 70)
        print(f"PENDING INVOICES ({unsynced})")
        print("=" * 70)
        print()
        
        # Get details of unsynced invoices
        cursor.execute("""
            SELECT 
                voucher_no,
                customer_name,
                tally_company,
                voucher_date,
                total_amount,
                sync_error
            FROM invoices 
            WHERE is_synced = 0
            ORDER BY voucher_date DESC
        """)
        
        pending = cursor.fetchall()
        
        for idx, inv in enumerate(pending, 1):
            print(f"{idx}. {inv['voucher_no']}")
            print(f"   Customer: {inv['customer_name']}")
            print(f"   Company:  {inv['tally_company']}")
            print(f"   Date:     {inv['voucher_date']}")
            print(f"   Amount:   {inv['total_amount']}")
            if inv['sync_error']:
                print(f"   Error:    {inv['sync_error'][:100]}...")
            print()
    
    # Check for failed syncs
    cursor.execute("""
        SELECT COUNT(*) as failed 
        FROM invoices 
        WHERE is_synced = 0 AND sync_error IS NOT NULL
    """)
    failed = cursor.fetchone()['failed']
    
    if failed > 0:
        print("=" * 70)
        print(f"FAILED SYNCS ({failed})")
        print("=" * 70)
        print()
        
        cursor.execute("""
            SELECT 
                voucher_no,
                customer_name,
                sync_error
            FROM invoices 
            WHERE is_synced = 0 AND sync_error IS NOT NULL
            LIMIT 5
        """)
        
        failed_invoices = cursor.fetchall()
        
        for idx, inv in enumerate(failed_invoices, 1):
            print(f"{idx}. {inv['voucher_no']} - {inv['customer_name']}")
            print(f"   Error: {inv['sync_error']}")
            print()
    
    conn.close()
    
except Exception as e:
    print(f"❌ Database error: {e}")
    import traceback
    traceback.print_exc()

print("=" * 70)
