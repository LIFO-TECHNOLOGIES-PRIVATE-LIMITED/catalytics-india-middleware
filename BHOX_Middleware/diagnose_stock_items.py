#!/usr/bin/env python3
"""
Diagnose why Tally is only returning 1 stock item when 50+ exist.
"""
import os
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from config import config
from tally_client import send_request
import xml.etree.ElementTree as ET

def diagnose_stock_items():
    """Check what Tally is returning for stock items."""
    
    company_name = config.get_active_companies()[0] if config.get_active_companies() else None
    if not company_name:
        print("❌ No active companies configured")
        return
    
    print(f"📊 Diagnosing Stock Items for: {company_name}")
    print("=" * 60)
    
    # Query 1: Simple count query
    print("\n1️⃣  SIMPLE COUNT QUERY")
    print("-" * 60)
    xml_count = f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>StockItem</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{company_name}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" ISFIXED="No" ISINITIALIZE="Yes" ISOPTION="No" ISINTERNAL="No" NAME="StockItem">
            <TYPE>StockItem</TYPE>
            <NATIVEMETHOD>Name</NATIVEMETHOD>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    
    try:
        resp = send_request(xml_count, config.TALLY_URL)
        root = ET.fromstring(resp)
        
        # Count STOCKITEM elements
        stock_items = root.findall(".//STOCKITEM")
        print(f"✅ Total STOCKITEM elements found: {len(stock_items)}")
        
        # Show first 5 items
        print("\n📋 First 5 Stock Items:")
        for i, item in enumerate(stock_items[:5], 1):
            name = item.findtext("NAME", "N/A")
            guid = item.get("NAME", "N/A")  # NAME attribute
            print(f"   {i}. Name: {name}, GUID: {guid}")
        
        if len(stock_items) > 5:
            print(f"   ... and {len(stock_items) - 5} more items")
            
    except Exception as e:
        print(f"❌ Error: {e}")
    
    # Query 2: With all fields
    print("\n\n2️⃣  FULL QUERY (with HSN and GST)")
    print("-" * 60)
    xml_full = f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>StockItem</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{company_name}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" ISFIXED="No" ISINITIALIZE="Yes" ISOPTION="No" ISINTERNAL="No" NAME="StockItem">
            <TYPE>StockItem</TYPE>
            <NATIVEMETHOD>Name</NATIVEMETHOD>
            <NATIVEMETHOD>GUID</NATIVEMETHOD>
            <NATIVEMETHOD>MasterID</NATIVEMETHOD>
            <NATIVEMETHOD>Parent</NATIVEMETHOD>
            <NATIVEMETHOD>BaseUnits</NATIVEMETHOD>
            <NATIVEMETHOD>HSNCode</NATIVEMETHOD>
            <NATIVEMETHOD>GSTApplicable</NATIVEMETHOD>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    
    try:
        resp = send_request(xml_full, config.TALLY_URL)
        root = ET.fromstring(resp)
        
        stock_items = root.findall(".//STOCKITEM")
        print(f"✅ Total STOCKITEM elements found: {len(stock_items)}")
        
        # Show first 5 items with details
        print("\n📋 First 5 Stock Items (with details):")
        for i, item in enumerate(stock_items[:5], 1):
            name = item.findtext("NAME", "N/A")
            hsn = item.findtext("HSNCODE", "N/A")
            gst = item.findtext("GSTAPPLICABLE", "N/A")
            print(f"   {i}. Name: {name}")
            print(f"      HSN: {hsn}, GST: {gst}")
        
        if len(stock_items) > 5:
            print(f"   ... and {len(stock_items) - 5} more items")
            
    except Exception as e:
        print(f"❌ Error: {e}")
    
    # Query 3: Check for filters
    print("\n\n3️⃣  CHECK FOR INACTIVE ITEMS")
    print("-" * 60)
    xml_inactive = f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>StockItem</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{company_name}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" ISFIXED="No" ISINITIALIZE="Yes" ISOPTION="No" ISINTERNAL="No" NAME="StockItem">
            <TYPE>StockItem</TYPE>
            <NATIVEMETHOD>Name</NATIVEMETHOD>
            <NATIVEMETHOD>IsDeleted</NATIVEMETHOD>
            <NATIVEMETHOD>IsInactive</NATIVEMETHOD>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    
    try:
        resp = send_request(xml_inactive, config.TALLY_URL)
        root = ET.fromstring(resp)
        
        stock_items = root.findall(".//STOCKITEM")
        active = sum(1 for item in stock_items if item.findtext("ISINACTIVE", "No") == "No")
        inactive = sum(1 for item in stock_items if item.findtext("ISINACTIVE", "No") == "Yes")
        deleted = sum(1 for item in stock_items if item.findtext("ISDELETED", "No") == "Yes")
        
        print(f"✅ Total items: {len(stock_items)}")
        print(f"   Active: {active}")
        print(f"   Inactive: {inactive}")
        print(f"   Deleted: {deleted}")
        
    except Exception as e:
        print(f"❌ Error: {e}")
    
    print("\n" + "=" * 60)
    print("✅ Diagnosis complete")

if __name__ == '__main__':
    diagnose_stock_items()
