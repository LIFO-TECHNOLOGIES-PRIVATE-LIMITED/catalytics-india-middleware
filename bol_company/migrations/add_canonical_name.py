"""
Database Migration: Add name_canonical column to products table

This migration:
1. Adds the name_canonical column to products table
2. Populates canonical names for existing products using normalization logic
3. Creates a UNIQUE index on name_canonical to prevent duplicates

Canonical name format: "{product_master_name} {quantity} {unit_name} ({type_code})"
Example: "ARGON B TYPE 1.5 CUM (CYL)"

This ensures products with spacing variations are treated as the same product:
- "ARGON B TYPE 1.5CUM (CYL)" → canonical: "ARGON B TYPE 1.5 CUM (CYL)"
- "ARGON B TYPE 1.5 CUM (CYL)" → canonical: "ARGON B TYPE 1.5 CUM (CYL)"
- "ARGON B TYPE  1.5  CUM  (CYL)" → canonical: "ARGON B TYPE 1.5 CUM (CYL)"
"""
import sqlite3
import re
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def normalize_spaces(text):
    """Collapse multiple spaces into single space"""
    if not text:
        return text
    return ' '.join(text.strip().split())


def generate_canonical_name(product_name, product_master_name, variant_name, type_code):
    """
    Generate canonical product name for uniqueness checking.
    
    If parsed fields are available, use them. Otherwise, try to parse from product_name.
    """
    # If we have parsed fields, use them
    if product_master_name and variant_name and type_code:
        return f"{product_master_name} {variant_name} ({type_code})"
    
    # Otherwise, try to parse from product_name
    if not product_name:
        return None
    
    # Normalize spaces
    normalized = normalize_spaces(product_name)
    
    # Try to extract components using regex
    # Pattern: <PRODUCT NAME> <QTY> <UNIT> (<TYPE_CODE>)
    pattern = re.compile(r'^(.+?)\s+(\d+(?:\.\d+)?)\s*(\w+)\s+\((\w+)\)$')
    match = pattern.match(normalized)
    
    if not match:
        # Can't parse - use normalized name as fallback
        logger.warning(f"Could not parse product name: {product_name}")
        return normalized
    
    product_master = match.group(1).strip()
    quantity = match.group(2).strip()
    unit = match.group(3).strip()
    type_cd = match.group(4).strip().upper()
    
    # Create canonical name with standardized spacing
    canonical_variant = f"{quantity} {unit}"
    canonical_name = f"{product_master} {canonical_variant} ({type_cd})"
    
    return canonical_name


def migrate(db_path):
    """Run the migration"""
    logger.info(f"Starting migration on database: {db_path}")
    
    if not Path(db_path).exists():
        logger.error(f"Database file not found: {db_path}")
        return False
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        # Step 1: Check if name_canonical column already exists
        cursor.execute("PRAGMA table_info(products)")
        columns = {row[1] for row in cursor.fetchall()}
        
        if 'name_canonical' in columns:
            logger.info("Column 'name_canonical' already exists - skipping creation")
        else:
            logger.info("Adding 'name_canonical' column to products table...")
            cursor.execute("ALTER TABLE products ADD COLUMN name_canonical TEXT")
            conn.commit()
            logger.info("✓ Column 'name_canonical' added")
        
        # Step 2: Populate canonical names for existing products
        logger.info("Populating canonical names for existing products...")
        
        cursor.execute("""
            SELECT id, name, product_master_name, variant_name, product_type_code
            FROM products
            WHERE name_canonical IS NULL OR name_canonical = ''
        """)
        
        products = cursor.fetchall()
        logger.info(f"Found {len(products)} products to update")
        
        updated_count = 0
        error_count = 0
        
        for product in products:
            try:
                product_id = product['id']
                product_name = product['name']
                product_master_name = product['product_master_name']
                variant_name = product['variant_name']
                type_code = product['product_type_code']
                
                # Generate canonical name
                canonical_name = generate_canonical_name(
                    product_name,
                    product_master_name,
                    variant_name,
                    type_code
                )
                
                if canonical_name:
                    cursor.execute(
                        "UPDATE products SET name_canonical = ? WHERE id = ?",
                        (canonical_name, product_id)
                    )
                    updated_count += 1
                    
                    if updated_count % 100 == 0:
                        logger.info(f"  Updated {updated_count} products...")
                else:
                    logger.warning(f"Could not generate canonical name for product {product_id}: {product_name}")
                    error_count += 1
                    
            except Exception as e:
                logger.error(f"Error updating product {product_id}: {e}")
                error_count += 1
        
        conn.commit()
        logger.info(f"✓ Updated {updated_count} products with canonical names")
        
        if error_count > 0:
            logger.warning(f"⚠ {error_count} products had errors during update")
        
        # Step 3: Create UNIQUE index on name_canonical
        logger.info("Creating UNIQUE index on name_canonical...")
        
        try:
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_products_name_canonical
                ON products(name_canonical)
            """)
            conn.commit()
            logger.info("✓ UNIQUE index created on name_canonical")
        except sqlite3.IntegrityError as e:
            logger.error(f"✗ Failed to create UNIQUE index - duplicate canonical names exist: {e}")
            logger.info("Attempting to identify and handle duplicates...")
            
            # Find duplicates
            cursor.execute("""
                SELECT name_canonical, COUNT(*) as count
                FROM products
                WHERE name_canonical IS NOT NULL
                GROUP BY name_canonical
                HAVING count > 1
            """)
            
            duplicates = cursor.fetchall()
            logger.warning(f"Found {len(duplicates)} duplicate canonical names:")
            
            for dup in duplicates:
                canonical = dup['name_canonical']
                count = dup['count']
                logger.warning(f"  - '{canonical}': {count} products")
                
                # Show which products have this canonical name
                cursor.execute("""
                    SELECT id, name, tally_company
                    FROM products
                    WHERE name_canonical = ?
                """, (canonical,))
                
                for prod in cursor.fetchall():
                    logger.warning(f"    - ID {prod['id']}: {prod['name']} (company: {prod['tally_company']})")
            
            logger.error("Please manually resolve duplicates before retrying migration")
            return False
        
        logger.info("\n" + "="*60)
        logger.info("MIGRATION COMPLETED SUCCESSFULLY")
        logger.info("="*60)
        logger.info(f"✓ Column 'name_canonical' added to products table")
        logger.info(f"✓ {updated_count} products updated with canonical names")
        logger.info(f"✓ UNIQUE index created on name_canonical")
        logger.info("="*60)
        
        return True
        
    except Exception as e:
        logger.error(f"Migration failed: {e}", exc_info=True)
        return False
    finally:
        conn.close()


if __name__ == '__main__':
    import sys
    
    # Get database path from command line or use default
    db_path = sys.argv[1] if len(sys.argv) > 1 else 'bol.sqlite'
    
    success = migrate(db_path)
    sys.exit(0 if success else 1)
