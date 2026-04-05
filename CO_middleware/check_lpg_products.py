#!/usr/bin/env python3
"""Quick check for LPG products in database"""
import sqlite3
import os

db_path = os.path.join(os.path.dirname(__file__), 'local_db.sqlite')

if not os.path.exists(db_path):
    print(f"Database not found: {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("\n" + "="*80)
print("LPG PRODUCTS IN DATABASE")
print("="*80)

# Check if products table exists
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='products'")
if not cursor.fetchone():
    print("Products table does not exist!")
    exit(1)

# Get all LPG products
cursor.execute("""
    SELECT id, name, product_master_name, variant_name, unit_name, product_type_code 
    FROM products 
    WHERE name LIKE '%LPG%' 
    ORDER BY name
""")

rows = cursor.fetchall()
print(f"\nTotal LPG products: {len(rows)}\n")

if rows:
    for row in rows:
        id_, name, master, variant, unit, type_code = row
        print(f"ID {id_}: {name}")
        print(f"  Master: {master}")
        print(f"  Variant: {variant}")
        print(f"  Unit: {unit}")
        print(f"  Type: {type_code}")
        print()
else:
    print("No LPG products found in database")

# Check for "LPG Gas 17 Kg" specifically
print("="*80)
print("SEARCHING FOR 'LPG Gas 17 Kg'")
print("="*80)

cursor.execute("""
    SELECT id, name, product_master_name, variant_name, unit_name 
    FROM products 
    WHERE name LIKE '%LPG Gas%' OR (name LIKE '%17%' AND name LIKE '%Kg%')
    ORDER BY name
""")

rows = cursor.fetchall()
if rows:
    print(f"\nFound {len(rows)} matching products:\n")
    for row in rows:
        id_, name, master, variant, unit = row
        print(f"ID {id_}: {name}")
        print(f"  Master: {master}, Variant: {variant}, Unit: {unit}")
        print()
else:
    print("\n'LPG Gas 17 Kg' not found in database")

# Check total products
cursor.execute("SELECT COUNT(*) FROM products")
total = cursor.fetchone()[0]
print("="*80)
print(f"Total products in database: {total}")
print("="*80)

conn.close()
