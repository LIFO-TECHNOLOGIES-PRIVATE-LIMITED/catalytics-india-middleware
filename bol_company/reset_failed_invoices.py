"""
Reset failed invoices to allow retry.
This clears the sync_error field so invoices can be synced again.
Run from middleware directory: python reset_failed_invoices.py
"""
import sys
import os
import sqlite3

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import config
import config

# Load configuration
config.config.reload_from_env()

print("=" * 70)
print("RESET FAILED INVOICES")
print("=" * 70)
print()

# Connect to SQLite
db_path = config.SQLITE_DB_PATH
print(f"Database: {db_path}")
print()

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Count failed invoices
    cursor.execute("""
        SELECT COUNT(*) as failed 
        FROM invoices 
        WHERE is_synced = 0 AND last_sync_error IS NOT NULL
    """)
    failed_count = cursor.fetchone()[0]
    
    if failed_count == 0:
        print("✅ No failed invoices to reset")
        conn.close()
        sys.exit(0)
    
    print(f"Found {failed_count} failed invoices")
    print()
    
    # Ask for confirmation
    response = input(f"Reset {failed_count} failed invoices? (yes/no): ").strip().lower()
    
    if response not in ['yes', 'y']:
        print("❌ Cancelled")
        conn.close()
        sys.exit(0)
    
    # Reset failed invoices
    cursor.execute("""
        UPDATE invoices 
        SET last_sync_error = NULL,
            last_response_json = NULL
        WHERE is_synced = 0 AND last_sync_error IS NOT NULL
    """)
    
    conn.commit()
    
    print(f"✅ Reset {failed_count} failed invoices")
    print()
    print("These invoices will be retried on the next sync cycle.")
    
    conn.close()
    
except Exception as e:
    print(f"❌ Database error: {e}")
    import traceback
    traceback.print_exc()

print()
print("=" * 70)
