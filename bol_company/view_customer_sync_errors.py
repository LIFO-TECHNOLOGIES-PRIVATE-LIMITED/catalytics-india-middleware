#!/usr/bin/env python3
"""
View detailed customer sync errors from SQLite database.
Shows error messages, error types, and full error responses.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import db
import json
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def view_customer_sync_errors():
    """View all customer sync errors with detailed information."""
    
    print("\n" + "="*80)
    print("CUSTOMER SYNC ERRORS")
    print("="*80)
    
    database = db.Database()
    conn = database.conn
    cursor = conn.cursor()
    
    # Get all customers with sync errors
    cursor.execute("""
        SELECT 
            id,
            name,
            tally_company,
            is_synced,
            last_sync_error,
            last_response_json,
            sync_request_json
        FROM customers
        WHERE last_sync_error IS NOT NULL
        ORDER BY id DESC
    """)
    
    errors = cursor.fetchall()
    
    if not errors:
        print("\n✅ No customer sync errors found!")
        return
    
    print(f"\n❌ Found {len(errors)} customers with sync errors\n")
    
    for idx, error_row in enumerate(errors, 1):
        customer_id, name, company, is_synced, sync_error, response_json, request_json = error_row
        
        print("=" * 80)
        print(f"ERROR #{idx}")
        print("=" * 80)
        print(f"Customer ID: {customer_id}")
        print(f"Name: {name}")
        print(f"Company: {company}")
        print(f"Is Synced: {is_synced}")
        print(f"\n📋 Error Message:")
        print(f"  {sync_error}")
        
        # Parse and display error response
        if response_json:
            try:
                response_data = json.loads(response_json)
                print(f"\n📄 Error Response Details:")
                
                # Check if it's the new detailed error format
                if isinstance(response_data, dict):
                    if response_data.get('data'):
                        data = response_data['data']
                        results = data.get('results', [])
                        
                        for result in results:
                            if isinstance(result, dict):
                                error_type = result.get('error_type')
                                error_message = result.get('message')
                                error_details = result.get('error_details')
                                status = result.get('status')
                                
                                if error_type:
                                    print(f"  Error Type: {error_type}")
                                if error_message:
                                    print(f"  Message: {error_message}")
                                if status:
                                    print(f"  Status: {status}")
                                if error_details:
                                    print(f"  Details:")
                                    print(f"    {json.dumps(error_details, indent=6)}")
                    else:
                        # Old format or raw error
                        print(f"  {json.dumps(response_data, indent=4)}")
                else:
                    print(f"  {response_json}")
                    
            except json.JSONDecodeError:
                print(f"  (Could not parse JSON)")
                print(f"  Raw: {response_json[:200]}")
        
        # Show request payload if available
        if request_json:
            try:
                request_data = json.loads(request_json)
                print(f"\n📤 Request Payload (summary):")
                if isinstance(request_data, dict):
                    if request_data.get('ledger'):
                        ledger = request_data['ledger']
                        print(f"  Ledger Name: {ledger.get('NAME', 'N/A')}")
                        print(f"  Parent: {ledger.get('PARENT', 'N/A')}")
                        print(f"  GSTIN: {ledger.get('GSTIN', 'N/A')}")
                        print(f"  Mobile: {ledger.get('MOBILE', 'N/A')}")
                        print(f"  Email: {ledger.get('EMAIL', 'N/A')}")
                    print(f"  Entity ID: {request_data.get('entity_id', 'N/A')}")
            except:
                pass
        
        print()
    
    # Summary by error type
    print("=" * 80)
    print("ERROR SUMMARY BY TYPE")
    print("=" * 80)
    
    error_types = {}
    for error_row in errors:
        sync_error = error_row[4]  # last_sync_error
        response_json = error_row[5] # last_response_json
        
        error_type = "UNKNOWN"
        if response_json:
            try:
                response_data = json.loads(response_json)
                if isinstance(response_data, dict) and response_data.get('data'):
                    results = response_data['data'].get('results', [])
                    for result in results:
                        if isinstance(result, dict):
                            error_type = result.get('error_type', 'UNKNOWN')
                            break
            except:
                pass
        
        if error_type not in error_types:
            error_types[error_type] = []
        error_types[error_type].append(error_row[1])  # customer name
    
    for error_type, customers in sorted(error_types.items()):
        print(f"\n{error_type}: {len(customers)} customers")
        for customer_name in customers[:5]:  # Show first 5
            print(f"  - {customer_name}")
        if len(customers) > 5:
            print(f"  ... and {len(customers) - 5} more")
    
    print("\n" + "=" * 80)
    print("RECOMMENDATIONS")
    print("=" * 80)
    print("\nCommon error types and solutions:")
    print("\n1. MISSING_NAME:")
    print("   - Customer ledger in Tally has no NAME field")
    print("   - Check Tally ledger data")
    
    print("\n2. MISSING_PARENT:")
    print("   - Customer ledger has no PARENT group")
    print("   - Ensure customer is under Sundry Debtors in Tally")
    
    print("\n3. INVALID_PARENT_GROUP:")
    print("   - Customer parent group is not Sundry Debtors")
    print("   - Move customer to Sundry Debtors group in Tally")
    
    print("\n4. VALIDATION_ERROR:")
    print("   - Customer data failed Django model validation")
    print("   - Check error details for specific field issues")
    print("   - Common issues: invalid email, mobile number format, etc.")
    
    print("\n" + "=" * 80)
    print("\nTo retry failed customers:")
    print("  1. Fix the issue in Tally (if needed)")
    print("  2. Run: python reset_failed_invoices.py")
    print("  3. Run: python sync_to_catalytics.py")
    print("=" * 80)

if __name__ == "__main__":
    view_customer_sync_errors()
