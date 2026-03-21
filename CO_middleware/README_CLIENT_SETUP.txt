===========================================
CO MIDDLEWARE - CLIENT SETUP INSTRUCTIONS
===========================================

QUICK START
-----------
1. Extract this folder to a location on your computer
2. Edit the .env file with your settings (see below)
3. Double-click "Start_Dashboard.bat"
4. Dashboard opens automatically in your browser

CONFIGURATION (.env file)
--------------------------
Required settings:

# Tally Configuration
TALLY_URL=http://localhost:9000/
TALLY_COMPANY=Your Company Name
TALLY_DB_PATH=tally_dc.sqlite

# Catalytics API Configuration
CATALYTICS_API_BASE_URL=https://your-catalytics-server.com
CATALYTICS_API_KEY=your-api-key-here
CATALYTICS_ENTITY_ID=your-entity-id

# Source Document Type
TALLY_SOURCE_DOC=delivery_note
# Options: delivery_note or sales_invoice

# Auto-Sync Intervals (in seconds)
AUTO_SYNC_INTERVAL=60
AUTO_SYNC_CUSTOMERS_INTERVAL_SEC=600
AUTO_SYNC_PRODUCTS_INTERVAL_SEC=600

DASHBOARD FEATURES
------------------
- Real-time sync status monitoring
- Automatic background synchronization
- Manual fetch/sync controls
- Live terminal output
- Activity logging

AUTO-SYNC
---------
The middleware automatically:
1. Fetches new invoices/DCs from Tally every 5 minutes
2. Syncs pending data to Catalytics every 1 minute
3. Fetches master data (customers/products) every 10 minutes

You can start/stop auto-sync from the dashboard.

TROUBLESHOOTING
---------------
1. Dashboard won't start:
   - Check if port 8787 is available
   - Check .env file for errors
   - Check logs folder for error messages

2. Tally connection fails:
   - Ensure Tally is running
   - Verify TALLY_URL in .env
   - Check Tally XML interface is enabled

3. Catalytics sync fails:
   - Verify CATALYTICS_API_BASE_URL
   - Check CATALYTICS_API_KEY is correct
   - Ensure network connectivity

SUPPORT
-------
For issues or questions, contact your system administrator.

Version: 2.0
Last Updated: 2026-03-05
