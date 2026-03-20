#!/bin/bash
# Start CO Middleware Dashboard

echo "=========================================="
echo "CO Middleware Dashboard Startup"
echo "=========================================="
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "ERROR: .env file not found!"
    echo "Please create .env file with configuration"
    exit 1
fi

echo "✓ Found .env file"
echo ""

# Load database path from .env
DB_PATH=$(grep TALLY_DB_PATH .env | cut -d '=' -f2)
echo "Database: $DB_PATH"
echo ""

# Check if database exists
if [ -f "$DB_PATH" ]; then
    echo "✓ Database file exists"
    
    # Check if it's a valid SQLite database
    if sqlite3 "$DB_PATH" "SELECT 1;" > /dev/null 2>&1; then
        echo "✓ Database is valid"
        
        # Show table count
        TABLE_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM sqlite_master WHERE type='table';")
        echo "✓ Database has $TABLE_COUNT tables"
    else
        echo "⚠ Database file is corrupted!"
        echo "  Backing up and recreating..."
        mv "$DB_PATH" "${DB_PATH}.backup.$(date +%Y%m%d_%H%M%S)"
        echo "  Old database backed up"
    fi
else
    echo "⚠ Database file doesn't exist (will be created automatically)"
fi

echo ""
echo "=========================================="
echo "Starting Dashboard..."
echo "=========================================="
echo ""
echo "Dashboard will be available at: http://localhost:8787"
echo ""
echo "Press Ctrl+C to stop"
echo ""

# Start dashboard
python dashboard.py
