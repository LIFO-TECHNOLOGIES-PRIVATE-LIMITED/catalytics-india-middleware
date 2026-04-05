#!/usr/bin/env python3
"""
Fix LPG products with empty variant_name and unit_name.
This script deletes LPG products with empty variant/unit so they get re-fetched and re-parsed.
"""
import sqlite3
import sys

def fix_lpg_products(db_path):
    """Delete LPG products with empty variant_name/unit_name so they get re-fetched."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Find LPG products with empty variant_name or unit_name
        cursor.execute("""
            SELECT id, name, variant_name, unit_name 
            FROM products 
            WHERE name LIKE 'LPG%' 
            AND (variant_name = '' OR variant_name IS NULL OR unit_name = '' OR unit_name IS NULL)
        """)
        
        broken_products = cursor.fetchall()
        
        if not broken_products:
            print("✓ No broken LPG products found")
            return True
        
        print(f"Found {len(broken_products)} LPG products with empty variant/unit:")
        for row in broken_products:
            print(f"  - ID {row['id']}: {row['name']} (variant: '{row['variant_name']}', unit: '{row['unit_name']}')")
        
        # Delete them
        cursor.execute("""
            DELETE FROM products 
            WHERE name LIKE 'LPG%' 
            AND (variant_name = '' OR variant_name IS NULL OR unit_name = '' OR unit_name IS NULL)
        """)
        
        deleted_count = cursor.rowcount
        conn.commit()
        
        print(f"\n✓ Deleted {deleted_count} broken LPG products")
        print("\nNext steps:")
        print("1. Run: python fetch_products.py --company 'CHENNAI OXYGEN'")
        print("2. Verify: sqlite3 local_db.sqlite \"SELECT name, variant_name, unit_name FROM products WHERE name LIKE 'LPG%';\"")
        
        return True
        
    except Exception as e:
        print(f"✗ Error: {e}")
        return False
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    db_path = 'local_db.sqlite'
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    
    print(f"Fixing LPG products in {db_path}...\n")
    success = fix_lpg_products(db_path)
    sys.exit(0 if success else 1)
