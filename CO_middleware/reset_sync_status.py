"""
Reset all sync statuses to pending (not synced)
Use this to test manual sync functionality
"""
import sqlite3
import sys
import os

# Get database path
db_path = os.path.join(os.path.dirname(__file__), 'tally_dc.sqlite')

if not os.path.exists(db_path):
    print(f"Error: Database not found at {db_path}")
    sys.exit(1)

conn = sqlite3.connect(db_path)

try:
    # Reset all sync statuses to pending (0)
    conn.execute("UPDATE ledger_sync_status SET is_synced = 0, synced_at = NULL")
    conn.execute("UPDATE stock_sync_status SET is_synced = 0, synced_at = NULL")
    conn.execute("UPDATE sync_status SET is_synced = 0, synced_at = NULL")
    
    conn.commit()
    
    # Check counts
    ledgers = conn.execute("SELECT COUNT(*) FROM ledger_sync_status WHERE is_synced = 0").fetchone()[0]
    stocks = conn.execute("SELECT COUNT(*) FROM stock_sync_status WHERE is_synced = 0").fetchone()[0]
    dcs = conn.execute("SELECT COUNT(*) FROM sync_status WHERE is_synced = 0").fetchone()[0]
    
    print("✓ Reset sync status successfully!")
    print(f"  - {ledgers} customers marked as pending")
    print(f"  - {stocks} products marked as pending")
    print(f"  - {dcs} DCs marked as pending")
    print("\nYou can now test manual sync from the dashboard.")
    
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)
finally:
    conn.close()
