#!/bin/bash
# Quick script to show what's in the database

echo "=========================================="
echo "DATABASE CONTENTS"
echo "=========================================="
echo ""

echo "CUSTOMERS:"
sqlite3 new.sqlite "SELECT name FROM ledgers ORDER BY name;"
echo ""

echo "PRODUCTS:"
sqlite3 new.sqlite "SELECT name FROM stock_items ORDER BY name;"
echo ""

echo "DCs:"
sqlite3 new.sqlite "SELECT dc_no, voucher_date, party_ledger_name FROM delivery_notes ORDER BY voucher_date DESC;"
echo ""

echo "=========================================="
echo "SUMMARY"
echo "=========================================="
sqlite3 new.sqlite "SELECT 
    (SELECT COUNT(*) FROM ledgers) as customers,
    (SELECT COUNT(*) FROM stock_items) as products,
    (SELECT COUNT(*) FROM delivery_notes) as dcs;"
