#!/bin/bash
# Database recovery script for corrupted SQLite database

DB_PATH="/home/thiru/Documents/Github/Local/catalytics-india-middleware/CO_middleware/latest.sqlite"
BACKUP_PATH="${DB_PATH}.backup_$(date +%Y%m%d_%H%M%S)"
RECOVERED_PATH="${DB_PATH}.recovered"

echo "=== SQLite Database Recovery ==="
echo "Database: $DB_PATH"
echo ""

# Check if database exists
if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: Database file not found: $DB_PATH"
    exit 1
fi

# Backup corrupted database
echo "1. Creating backup of corrupted database..."
cp "$DB_PATH" "$BACKUP_PATH"
echo "   Backup saved: $BACKUP_PATH"
echo ""

# Try to recover using sqlite3 dump
echo "2. Attempting recovery using sqlite3 dump..."
if sqlite3 "$DB_PATH" ".dump" | sqlite3 "$RECOVERED_PATH" 2>/dev/null; then
    echo "   Recovery successful!"
    echo ""
    
    # Replace corrupted database with recovered one
    echo "3. Replacing corrupted database with recovered version..."
    mv "$DB_PATH" "${DB_PATH}.corrupted"
    mv "$RECOVERED_PATH" "$DB_PATH"
    
    # Remove WAL and SHM files
    rm -f "${DB_PATH}-wal" "${DB_PATH}-shm"
    
    echo "   Done!"
    echo ""
    echo "=== Recovery Complete ==="
    echo "Corrupted file moved to: ${DB_PATH}.corrupted"
    echo "Backup available at: $BACKUP_PATH"
    echo ""
    echo "You can now restart the dashboard."
else
    echo "   ERROR: Recovery failed. Database is too corrupted."
    echo ""
    echo "=== Creating Fresh Database ==="
    echo "Moving corrupted database and starting fresh..."
    
    mv "$DB_PATH" "${DB_PATH}.corrupted"
    rm -f "${DB_PATH}-wal" "${DB_PATH}-shm"
    
    echo "   Done!"
    echo ""
    echo "Corrupted file moved to: ${DB_PATH}.corrupted"
    echo "A new database will be created when you restart the dashboard."
    echo "All data will be re-fetched from Tally."
fi
