import logging
import os
import re
import time
import threading
from typing import List, Dict, Optional

import requests
import xml.etree.ElementTree as ET

from config import config

DEFAULT_URL = "http://localhost:9000/"
logger = logging.getLogger(__name__)

# Global lock to serialize all Tally requests.
# Tally ERP9's XML API cannot handle concurrent connections and crashes with
# "Software Exception c0000005 (Memory Access Violation)" when multiple
# requests arrive simultaneously (e.g. from automation threads).
_tally_lock = threading.Lock()

# Cooldown in seconds between consecutive Tally requests.
# Tally needs time to free memory between requests, especially on remote access.
# Configure via TALLY_REQUEST_COOLDOWN env var (default 2.0 seconds).
def _read_tally_cooldown():
    raw = os.getenv("TALLY_REQUEST_COOLDOWN", "").strip()
    if not raw:
        return 2.0
    try:
        value = float(raw)
    except ValueError:
        return 2.0
    return max(0.0, value)

_TALLY_REQUEST_COOLDOWN = _read_tally_cooldown()


def send_request(xml_request: str, url: Optional[str] = None, timeout: int = 60) -> str:
    target = (url or DEFAULT_URL).rstrip("/")
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "Connection": "close",
    }

    logger.debug("Tally request URL=%s headers=%s xml=%s", target, headers, xml_request)
    with _tally_lock:
        try:
            resp = requests.post(
                target,
                data=xml_request.encode("utf-8"),
                headers=headers,
                timeout=timeout,
            )
            logger.debug("Tally response status=%s body=%s", resp.status_code, (resp.text or "")[:5000])
            resp.raise_for_status()
            return resp.text
        except requests.RequestException:
            logger.exception("Tally HTTP request failed for URL=%s", target)
            raise
        finally:
            # Give Tally time to free memory before the next request.
            # Without this cooldown, rapid consecutive requests cause
            # c0000005 Memory Access Violation on the Tally server.
            time.sleep(_TALLY_REQUEST_COOLDOWN)


def parse_companies(response_xml: str) -> List[Dict]:
    """
    Parse a Tally response and return a list of company dicts with at least 'name'.
    This parser is resilient to several XML shapes (COMPANY, COMPANYNAME, NAME).
    """
    companies = []
    try:
        root = ET.fromstring(response_xml)
    except ET.ParseError:
        logger.exception("Failed to parse companies XML response")
        return companies

    # Try to find company names in the TallyPrime specific response structure
    for collection in root.findall(".//COLLECTION[@NAME='List of Companies']"):
        for name_node in collection.findall("NATIVEMETHOD"):
            if name_node.text:
                companies.append({"name": name_node.text.strip()})

    if not companies:
        # Try common shapes (fallback for older Tally versions or different request types)
        for comp in root.findall(".//COMPANY"):
            name = comp.findtext("NAME") or comp.findtext("COMPANYNAME") or comp.findtext("DSPNAME") or comp.findtext("MAILINGNAME")
            if name:
                companies.append({"name": name.strip()})

    if not companies:
        for node in root.findall(".//COMPANYNAME"):
            if node.text:
                companies.append({"name": node.text.strip()})

    if not companies:
        for node in root.findall(".//DSPNAME"):
            if node.text:
                companies.append({"name": node.text.strip()})

    if not companies:
        for node in root.findall(".//MAILINGNAME"):
            if node.text:
                companies.append({"name": node.text.strip()})

    if not companies:
        # last-resort: any NAME fields under TDLMESSAGE
        for node in root.findall(".//NAME"):
            if node.text:
                companies.append({"name": node.text.strip()})

    return companies


def get_companies(url: Optional[str] = None):
    xml = """
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>List of Companies</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVIsSimpleCompany>No</SVIsSimpleCompany>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" ISFIXED="No" ISINITIALIZE="Yes" ISOPTION="No" ISINTERNAL="No" NAME="List of Companies">
            <TYPE>Company</TYPE>
            <NATIVEMETHOD>Name</NATIVEMETHOD>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    resp = send_request(xml, url)
    # print(resp)  # Disabled for cleaner logs
    return parse_companies(resp)



def _clean_invalid_char_refs(xml_text: str) -> str:
    """
    Sanitize malformed XML snippets returned by Tally so ElementTree can parse.
    - Drop numeric entities below 32 except whitespace
    - Escape bare '&' that are not valid entities
    - Remove raw control chars below 32 except tab/newline/carriage-return
    - Drop namespace-like prefixes from tags/attributes when xmlns is missing
      (for example UDF:USERDESCRIPTION), which causes "unbound prefix".
    """
    def repl(match):
        value = int(match.group(1))
        return "" if (value < 32 and value not in (9, 10, 13)) else match.group(0)
    cleaned = re.sub(r"&#(\d+);", repl, xml_text)
    cleaned = re.sub(
        r"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9._:-]*;)",
        "&amp;",
        cleaned,
    )
    cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", cleaned)
    # Tally can return prefixed tags/attrs (UDF:*) without xmlns declarations.
    # ElementTree rejects these with "unbound prefix", so strip the prefix.
    cleaned = re.sub(
        r"(<\/?)([A-Za-z_][\w.-]*):([A-Za-z_][\w.-]*)([^>]*>)",
        r"\1\3\4",
        cleaned,
    )
    cleaned = re.sub(
        r"(\s)([A-Za-z_][\w.-]*):([A-Za-z_][\w.-]*)(=)",
        r"\1\3\4",
        cleaned,
    )
    return cleaned



def get_sales_voucher_types(company_name: str, url: Optional[str] = None) -> List[str]:
    """
    Return all active voucher type names whose parent/base type is Sales.
    Supports custom sales voucher types like IO-, HO - SALES, CASH SALES.
    """
    from xml.sax.saxutils import escape as xml_escape
    safe_company = xml_escape(company_name)
    xml = f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>VoucherTypeList</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" NAME="VoucherTypeList">
            <TYPE>VoucherType</TYPE>
            <NATIVEMETHOD>Name</NATIVEMETHOD>
            <NATIVEMETHOD>Parent</NATIVEMETHOD>
            <NATIVEMETHOD>IsActive</NATIVEMETHOD>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    resp = send_request(xml, url)
    root = ET.fromstring(_clean_invalid_char_refs(resp))

    voucher_types: List[str] = []
    for vt in root.findall(".//VOUCHERTYPE"):
        name = (vt.get("NAME") or "").strip()
        parent = (vt.findtext("PARENT") or "").strip()
        is_active = (vt.findtext("ISACTIVE") or "Yes").strip()

        if not name:
            continue
        # BHOX uses 'Z-Invoice Manufacturing' as parent for sales
        parent_lower = parent.lower()
        if parent_lower != "sales" and "invoice" not in parent_lower and "manufacturing" not in parent_lower:
            continue
        if is_active.lower() == "no":
            continue
        voucher_types.append(name)

    # Preserve order while removing duplicates.
    deduped: List[str] = []
    seen = set()
    for name in voucher_types:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(name)
    return deduped


def parse_ledgers(response_xml: str):
    ledgers = []
    root = ET.fromstring(_clean_invalid_char_refs(response_xml))

    # Parse from Collection-based response (current Tally format)
    for collection in root.findall(".//COLLECTION"):
        for ledger in collection:
            if ledger.tag == "LEDGER":
                data = {}

                # Extract the NAME attribute from the LEDGER element
                ledger_name = ledger.get("NAME", "")
                if ledger_name:
                    data["NAME"] = ledger_name

                # Extract StateName and CountryName directly from the ledger element
                state_name = ledger.findtext("STATENAME")
                if state_name:
                    data["STATENAME"] = state_name.strip()
                country_name = ledger.findtext("COUNTRYNAME")
                if country_name:
                    data["COUNTRYNAME"] = country_name.strip()

                for child in ledger:
                    if child.tag.endswith(".LIST"):
                        continue
                    # Skip NAME child element - we already got it from the attribute
                    if child.tag.upper() == "NAME":
                        continue
                    # Handle special character encoding in PARENT
                    if child.tag == "PARENT" and child.text:
                        # Clean up special characters like &#4;
                        cleaned_text = child.text.replace('&#4;', '').strip()
                        data[child.tag.upper()] = cleaned_text
                    else:
                        data[child.tag.upper()] = (child.text or "").strip()

                # Handle addresses
                address_blocks = []
                for addr_list in ledger.findall("ADDRESS.LIST"):
                    lines = [a.text.strip() for a in addr_list.findall("ADDRESS") if a.text]
                    if lines:
                        address_blocks.append(", ".join(lines))

                if address_blocks:
                    data["ADDRESSES"] = address_blocks

                ledgers.append(data)

    # Fallback: try old format if no ledgers found
    if not ledgers:
        for ledger in root.findall(".//LEDGER"):
            data = {}

            # Extract the NAME attribute from the LEDGER element
            ledger_name = ledger.get("NAME", "")
            if ledger_name:
                data["NAME"] = ledger_name

            for child in ledger:
                if child.tag.endswith(".LIST"):
                    continue
                # Skip NAME child element - we already got it from the attribute
                if child.tag.upper() == "NAME":
                    continue
                data[child.tag.upper()] = (child.text or "").strip()

            # Correct way to read addresses
            address_blocks = []
            for addr_list in ledger.findall("ADDRESS.LIST"):
                lines = [a.text.strip() for a in addr_list.findall("ADDRESS") if a.text]
                if lines:
                    address_blocks.append(", ".join(lines))

            if address_blocks:
                data["ADDRESSES"] = address_blocks

            ledgers.append(data)

    return ledgers



def get_sundry_debtors(company_name: str, url: Optional[str] = None, group_name: str = "Sundry Debtors"):
    """Fetch only Sundry Debtors ledgers (including sub-groups) from Tally.
    Uses CHILDOF + BELONGSTO to filter at Tally level â€" much more efficient
    than fetching all ledgers and filtering in Python."""
    from xml.sax.saxutils import escape as xml_escape
    safe_company = xml_escape(company_name)
    safe_group = xml_escape(group_name or "Sundry Debtors")
    xml = f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>SundryDebtorLedgers</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" ISFIXED="No" ISINITIALIZE="Yes" ISOPTION="No" ISINTERNAL="No" NAME="SundryDebtorLedgers">
            <TYPE>Ledger</TYPE>
            <CHILDOF>{safe_group}</CHILDOF>
            <BELONGSTO>Yes</BELONGSTO>
            <NATIVEMETHOD>Name</NATIVEMETHOD>
            <NATIVEMETHOD>GUID</NATIVEMETHOD>
            <NATIVEMETHOD>MasterID</NATIVEMETHOD>
            <NATIVEMETHOD>Parent</NATIVEMETHOD>
            <NATIVEMETHOD>Mobile</NATIVEMETHOD>
            <NATIVEMETHOD>Email</NATIVEMETHOD>
            <NATIVEMETHOD>PANNumber</NATIVEMETHOD>
            <NATIVEMETHOD>IncomeTaxNumber</NATIVEMETHOD>
            <NATIVEMETHOD>GSTRegistration</NATIVEMETHOD>
            <NATIVEMETHOD>PartyGSTIN</NATIVEMETHOD>
            <NATIVEMETHOD>GSTIN</NATIVEMETHOD>
            <NATIVEMETHOD>Address</NATIVEMETHOD>
            <NATIVEMETHOD>StateName</NATIVEMETHOD>
            <NATIVEMETHOD>PinCode</NATIVEMETHOD>
            <NATIVEMETHOD>CountryName</NATIVEMETHOD>
            <NATIVEMETHOD>LedgerMobile</NATIVEMETHOD>
            <NATIVEMETHOD>LedgerEmail</NATIVEMETHOD>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    resp = send_request(xml, url)
    return parse_ledgers(resp)

def get_all_customer_ledgers(company_name: str, url: Optional[str] = None):
    """Fetch ALL ledgers from Tally (no group filter).
    Returns every ledger with full fields (GUID, address, GST, etc.)."""
    from xml.sax.saxutils import escape as xml_escape
    safe_company = xml_escape(company_name)
    xml = f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>AllCustomerLedgers</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" ISFIXED="No" ISINITIALIZE="Yes" ISOPTION="No" ISINTERNAL="No" NAME="AllCustomerLedgers">
            <TYPE>Ledger</TYPE>
            <NATIVEMETHOD>Name</NATIVEMETHOD>
            <NATIVEMETHOD>GUID</NATIVEMETHOD>
            <NATIVEMETHOD>MasterID</NATIVEMETHOD>
            <NATIVEMETHOD>Parent</NATIVEMETHOD>
            <NATIVEMETHOD>Mobile</NATIVEMETHOD>
            <NATIVEMETHOD>Email</NATIVEMETHOD>
            <NATIVEMETHOD>PANNumber</NATIVEMETHOD>
            <NATIVEMETHOD>IncomeTaxNumber</NATIVEMETHOD>
            <NATIVEMETHOD>GSTRegistration</NATIVEMETHOD>
            <NATIVEMETHOD>PartyGSTIN</NATIVEMETHOD>
            <NATIVEMETHOD>GSTIN</NATIVEMETHOD>
            <NATIVEMETHOD>Address</NATIVEMETHOD>
            <NATIVEMETHOD>StateName</NATIVEMETHOD>
            <NATIVEMETHOD>PinCode</NATIVEMETHOD>
            <NATIVEMETHOD>CountryName</NATIVEMETHOD>
            <NATIVEMETHOD>LedgerMobile</NATIVEMETHOD>
            <NATIVEMETHOD>LedgerEmail</NATIVEMETHOD>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    resp = send_request(xml, url)
    return parse_ledgers(resp)


def get_customer_ledgers(company_name: str, url: Optional[str] = None, group_names: Optional[List[str]] = None):
    """Fetch customer ledgers from Tally — all groups, no filter."""
    return get_all_customer_ledgers(company_name, url)


def get_ledgers(company_name: str, url: Optional[str] = None):
    from xml.sax.saxutils import escape as xml_escape
    safe_company = xml_escape(company_name)
    xml = f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>Ledger</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" ISFIXED="No" ISINITIALIZE="Yes" ISOPTION="No" ISINTERNAL="No" NAME="Ledger">
            <TYPE>Ledger</TYPE>
            <NATIVEMETHOD>Name</NATIVEMETHOD>
            <NATIVEMETHOD>GUID</NATIVEMETHOD>
            <NATIVEMETHOD>MasterID</NATIVEMETHOD>
            <NATIVEMETHOD>Parent</NATIVEMETHOD>
            <NATIVEMETHOD>Mobile</NATIVEMETHOD>
            <NATIVEMETHOD>Email</NATIVEMETHOD>
            <NATIVEMETHOD>GSTRegistration</NATIVEMETHOD>
            <NATIVEMETHOD>Address</NATIVEMETHOD>
            <NATIVEMETHOD>StateName</NATIVEMETHOD>
            <NATIVEMETHOD>CountryName</NATIVEMETHOD>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    resp = send_request(xml, url)
    # print(resp)  # Disabled for cleaner logs
    return parse_ledgers(resp)


def parse_stock_items(response_xml: str):
    items = []
    root = ET.fromstring(_clean_invalid_char_refs(response_xml))

    def parse_list_element(element):
        entries = []
        child_lists = [c for c in element if c.tag.endswith(".LIST")]
        if child_lists:
            for child in child_lists:
                data = {}
                for sub in child:
                    if sub.tag.endswith(".LIST"):
                        continue
                    data[sub.tag.upper()] = (sub.text or "").strip()
                if data:
                    entries.append(data)
            return entries
        data = {}
        for sub in element:
            if sub.tag.endswith(".LIST"):
                continue
            data[sub.tag.upper()] = (sub.text or "").strip()
        return [data] if data else []

    for collection in root.findall(".//COLLECTION"):
        for stock in collection:
            if stock.tag == "STOCKITEM":
                data = {}
                item_name = stock.get("NAME", "")
                if item_name:
                    data["NAME"] = item_name

                for child in stock:
                    if child.tag.endswith(".LIST"):
                        tag_upper = child.tag.upper()
                        if "ADDITIONALUNITS" in tag_upper or "ALTERNATE" in tag_upper or "ALTUNITS" in tag_upper:
                            data["ALT_UNITS"] = parse_list_element(child)
                        continue
                    data[child.tag.upper()] = (child.text or "").strip()

                items.append(data)

    if not items:
        for stock in root.findall(".//STOCKITEM"):
            data = {}
            item_name = stock.get("NAME", "")
            if item_name:
                data["NAME"] = item_name
            for child in stock:
                if child.tag.endswith(".LIST"):
                    tag_upper = child.tag.upper()
                    if "ADDITIONALUNITS" in tag_upper or "ALTERNATE" in tag_upper or "ALTUNITS" in tag_upper:
                        data["ALT_UNITS"] = parse_list_element(child)
                    continue
                data[child.tag.upper()] = (child.text or "").strip()
            items.append(data)

    return items


def parse_stock_items_full(response_xml: str) -> List[Dict]:
    """
    Parse stock item response with ALL fields including nested lists like:
    - HSNDETAILS.LIST (for HSN code)
    - GSTDETAILS.LIST > STATEWISEDETAILS.LIST > RATEDETAILS.LIST (for GST rates)
    - ALTERNATEUNITS.LIST (for unit conversion)
    """
    items = []
    root = ET.fromstring(_clean_invalid_char_refs(response_xml))

    def _parse_rate_value(raw_value) -> Optional[float]:
        if raw_value is None:
            return None
        text = str(raw_value).strip()
        if not text:
            return None
        text = text.replace(",", "")
        try:
            return float(text)
        except (TypeError, ValueError):
            match = re.search(r"-?\d+(?:\.\d+)?", text)
            if match:
                try:
                    return float(match.group(0))
                except (TypeError, ValueError):
                    return None
        return None

    def _assign_higher_rate(target: Dict, key: str, rate_val: Optional[float]) -> None:
        if rate_val is None:
            return
        current = target.get(key)
        if current is None or rate_val > current:
            target[key] = rate_val

    def _extract_hsn_and_gst(data: Dict) -> None:
        if "HSNDETAILS_LIST" in data and data["HSNDETAILS_LIST"]:
            hsn_detail = data["HSNDETAILS_LIST"][0]
            if "HSNCODE" in hsn_detail:
                data["HSNCODE"] = hsn_detail["HSNCODE"]

        gst_details = data.get("GSTDETAILS_LIST")
        if not gst_details:
            return

        for gst_detail in gst_details:
            state_details = gst_detail.get("STATEWISEDETAILS_LIST") or []
            for state_detail in state_details:
                for rate_detail in state_detail.get("RATEDETAILS_LIST") or []:
                    duty_head = (rate_detail.get("GSTRATEDUTYHEAD", "") or "").upper()
                    rate_val = _parse_rate_value(rate_detail.get("GSTRATE"))
                    if rate_val is None:
                        continue
                    if "IGST" in duty_head:
                        _assign_higher_rate(data, "IGST_RATE", rate_val)
                    elif "CGST" in duty_head:
                        _assign_higher_rate(data, "CGST_RATE", rate_val)
                    elif "SGST" in duty_head or "UTGST" in duty_head:
                        _assign_higher_rate(data, "SGST_RATE", rate_val)

        if "CGST_RATE" in data and "SGST_RATE" in data and "IGST_RATE" not in data:
            data["IGST_RATE"] = data["CGST_RATE"] + data["SGST_RATE"]
        if "IGST_RATE" in data and "CGST_RATE" not in data:
            data["CGST_RATE"] = data["IGST_RATE"] / 2
            data["SGST_RATE"] = data["IGST_RATE"] / 2
        if "IGST_RATE" in data:
            data["GST_RATE"] = data["IGST_RATE"]

    def parse_nested_lists(element, depth=0):
        """Recursively parse nested .LIST elements."""
        result = {}
        for child in element:
            tag = child.tag.upper()
            if child.tag.endswith(".LIST"):
                # Parse child lists recursively
                list_name = tag.replace(".LIST", "_LIST")
                if list_name not in result:
                    result[list_name] = []
                list_item = parse_nested_lists(child, depth + 1)
                if list_item:
                    result[list_name].append(list_item)
            else:
                # Simple element
                result[tag] = (child.text or "").strip()
        return result

    # Find STOCKITEM elements
    for collection in root.findall(".//COLLECTION"):
        for stock in collection:
            if stock.tag == "STOCKITEM":
                data = {}
                # Try to get NAME from attribute first
                item_name = stock.get("NAME", "")
                
                # Parse all children including nested lists
                for child in stock:
                    tag = child.tag.upper()
                    if child.tag.endswith(".LIST"):
                        list_name = tag.replace(".LIST", "_LIST")
                        if list_name not in data:
                            data[list_name] = []
                        list_item = parse_nested_lists(child)
                        if list_item:
                            data[list_name].append(list_item)
                    else:
                        data[tag] = (child.text or "").strip()

                # Set NAME from attribute if not already set by child element
                if not data.get("NAME") and item_name:
                    data["NAME"] = item_name

                _extract_hsn_and_gst(data)

                # Extract conversion info from ALTERNATEUNITS_LIST
                if "ALTERNATEUNITS_LIST" in data and data["ALTERNATEUNITS_LIST"]:
                    alt_unit = data["ALTERNATEUNITS_LIST"][0]
                    data["ALTERNATE_UNIT"] = alt_unit.get("ALTERNATEUNIT", "")
                    data["CONVERSION"] = alt_unit.get("CONVERSION", "")

                items.append(data)

    # Fallback: direct STOCKITEM search
    if not items:
        for stock in root.findall(".//STOCKITEM"):
            data = {}
            # Try to get NAME from attribute first
            item_name = stock.get("NAME", "")

            for child in stock:
                tag = child.tag.upper()
                if child.tag.endswith(".LIST"):
                    list_name = tag.replace(".LIST", "_LIST")
                    if list_name not in data:
                        data[list_name] = []
                    list_item = parse_nested_lists(child)
                    if list_item:
                        data[list_name].append(list_item)
                else:
                    data[tag] = (child.text or "").strip()

            # Set NAME from attribute if not already set by child element
            if not data.get("NAME") and item_name:
                data["NAME"] = item_name

            _extract_hsn_and_gst(data)

            # Extract conversion info
            if "ALTERNATEUNITS_LIST" in data and data["ALTERNATEUNITS_LIST"]:
                alt_unit = data["ALTERNATEUNITS_LIST"][0]
                data["ALTERNATE_UNIT"] = alt_unit.get("ALTERNATEUNIT", "")
                data["CONVERSION"] = alt_unit.get("CONVERSION", "")

            items.append(data)

    return items


def get_stock_items(company_name: str, url: Optional[str] = None):
    from xml.sax.saxutils import escape as xml_escape
    safe_company = xml_escape(company_name)
    logger.info(f"[get_stock_items] Requesting stock items for company: '{company_name}'")
    xml = f"""
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
        <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
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
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    resp = send_request(xml, url)
    items = parse_stock_items_full(resp)
    logger.info(f"[get_stock_items] Got {len(items)} stock items from '{company_name}'")
    return items



def parse_delivery_notes(response_xml: str):
    vouchers = []
    root = ET.fromstring(_clean_invalid_char_refs(response_xml))
    for voucher in root.findall(".//VOUCHER"):
        # Skip count-only <VOUCHER>N</VOUCHER> elements from <CMPINFO> â€" they have no child elements
        if not list(voucher):
            continue
        data = {}
        for attr_key, attr_val in voucher.attrib.items():
            if attr_key and attr_val:
                data[attr_key.upper()] = str(attr_val).strip()
        for child in voucher:
            if child.tag.endswith(".LIST"):
                continue
            data[child.tag.upper()] = (child.text or "").strip()

        def first_text(names):
            for name in names:
                val = voucher.findtext(f".//{name}")
                if val:
                    return val.strip()
            return ""

        for key, names in [
            ("DATE", ["DATE", "VOUCHERDATE", "DSPVCHDATE"]),
            ("VOUCHERNUMBER", ["VOUCHERNUMBER", "VCHNUMBER", "VOUCHERNO", "VCHNO", "NUMBER", "DSPVCHNO", "DSPVCHNUMBER"]),
            ("PARTYLEDGERNAME", ["PARTYLEDGERNAME", "PARTYNAME"]),
            ("REFERENCE", ["REFERENCE", "VOUCHERREFERENCE"]),
        ]:
            if not data.get(key):
                val = first_text(names)
                if val:
                    data[key] = val

        data["INVENTORY"] = []
        # Handle both INVENTORYENTRIES.LIST and ALLINVENTORYENTRIES.LIST
        for inv_tag in ["INVENTORYENTRIES.LIST", "ALLINVENTORYENTRIES.LIST"]:
            for inv in voucher.findall(f".//{inv_tag}"):
                item = {}
                for child in inv:
                    if child.tag == 'BASICUSERDESCRIPTION.LIST':
                        descs = [el.text.strip() for el in child.findall('BASICUSERDESCRIPTION') if el.text and el.text.strip()]
                        if descs:
                            item['BASICUSERDESCRIPTION'] = ','.join(descs)
                    elif child.tag.endswith(".LIST"):
                        continue
                    else:
                        item[child.tag.upper()] = (child.text or "").strip()

                # Extract GODOWNNAME from BATCHALLOCATIONS.LIST
                for batch in inv.findall(".//BATCHALLOCATIONS.LIST"):
                    godown = batch.findtext("GODOWNNAME")
                    if godown:
                        item["GODOWNNAME"] = godown.strip()
                        break

                if item:
                    data["INVENTORY"].append(item)

        # Collect billing address lines if present (ADDRESS.LIST / BASICBUYERADDRESS.LIST)
        addresses = []
        for addr_list in voucher.findall(".//ADDRESS.LIST"):
            for addr in addr_list.findall("ADDRESS"):
                if addr.text:
                    addresses.append(addr.text.strip())
        if not addresses:
            for addr_list in voucher.findall(".//BASICBUYERADDRESS.LIST"):
                for addr in addr_list.findall("BASICBUYERADDRESS"):
                    if addr.text:
                        addresses.append(addr.text.strip())
        if addresses:
            data["ADDRESSES"] = addresses  # This is the billing address

        # Extract consignee (delivery) address from separate fields
        # These fields are used for "Ship To" / "Consignee" in Tally DC
        consignee_data = {}

        # Consignee name
        consignee_name = (
            voucher.findtext(".//CONSIGNEEMAILINGNAME") or
            voucher.findtext(".//BASICSHIPTONAME") or
            voucher.findtext(".//SHIPTONAME") or
            ""
        ).strip()
        if consignee_name:
            consignee_data["NAME"] = consignee_name

        # Consignee address lines - try multiple sources
        consignee_address = []

        # Try CONSIGNEEADDRESS.LIST first
        for addr_list in voucher.findall(".//CONSIGNEEADDRESS.LIST"):
            for addr in addr_list.findall("CONSIGNEEADDRESS"):
                if addr.text:
                    consignee_address.append(addr.text.strip())

        # Fallback to BASICSHIPTOADDRESS.LIST
        if not consignee_address:
            for addr_list in voucher.findall(".//BASICSHIPTOADDRESS.LIST"):
                for addr in addr_list.findall("BASICSHIPTOADDRESS"):
                    if addr.text:
                        consignee_address.append(addr.text.strip())

        # Fallback to BASICBUYERADDRESS (billing address) if no separate delivery address
        if not consignee_address:
            for addr_list in voucher.findall(".//BASICBUYERADDRESS.LIST"):
                for addr in addr_list.findall("BASICBUYERADDRESS"):
                    if addr.text and not addr.text.lower().startswith(('email:', 'phone:', 'mobile:')):
                        consignee_address.append(addr.text.strip())

        if consignee_address:
            consignee_data["ADDRESS"] = ", ".join(consignee_address)

        # Consignee pincode
        consignee_pincode = (
            voucher.findtext(".//CONSIGNEEPINCODE") or
            voucher.findtext(".//BASICSHIPTOPINCODE") or
            voucher.findtext(".//SHIPTOPINCODE") or
            ""
        ).strip()
        if consignee_pincode:
            consignee_data["PINCODE"] = consignee_pincode

        # Consignee state
        consignee_state = (
            voucher.findtext(".//CONSIGNEESTATENAME") or
            voucher.findtext(".//BASICSHIPTOSTATENAME") or
            voucher.findtext(".//SHIPTOSTATENAME") or
            ""
        ).strip()
        if consignee_state:
            consignee_data["STATE"] = consignee_state

        # Consignee GST number
        consignee_gstin = (
            voucher.findtext(".//CONSIGNEEGSTIN") or
            voucher.findtext(".//BASICSHIPTOGSTIN") or
            voucher.findtext(".//SHIPTOGSTIN") or
            ""
        ).strip()
        if consignee_gstin:
            consignee_data["GSTIN"] = consignee_gstin

        # Consignee place (city)
        consignee_place = (
            voucher.findtext(".//CONSIGNEEPLACE") or
            voucher.findtext(".//SHIPTOPLACE") or
            voucher.findtext(".//BASICSHIPTOPLACE") or
            ""
        ).strip()
        if consignee_place:
            consignee_data["PLACE"] = consignee_place

        if consignee_data:
            data["CONSIGNEE"] = consignee_data

        # Extract dispatch/delivery details for vehicle sync
        # "Dispatched through" field - vehicle number (BASICSHIPPEDBY in TallyPrime)
        dispatched_through = (
            voucher.findtext(".//BASICSHIPPEDBY") or
            voucher.findtext(".//DISPATCHEDTHROUGH") or
            voucher.findtext(".//BASICDISPATCHEDTHROUGH") or
            voucher.findtext(".//DESPATCHEDTHROUGH") or
            ""
        ).strip()
        if dispatched_through:
            data["DISPATCHEDTHROUGH"] = dispatched_through

        # "Motor Vehicle No." field - alternative vehicle number
        motor_vehicle_no = (
            voucher.findtext(".//GOODSVEHICLENUMBER") or
            voucher.findtext(".//MOTORVEHICLENO") or
            voucher.findtext(".//BASICMOTORVEHICLENO") or
            voucher.findtext(".//BASICSHIPVESSELNO") or
            voucher.findtext(".//VATVEHICLENUMBER") or
            voucher.findtext(".//VATVEHICLENO") or
            voucher.findtext(".//VEHICLENO") or
            ""
        ).strip()
        if motor_vehicle_no:
            data["MOTORVEHICLENO"] = motor_vehicle_no

        # "Terms of Delivery" field - determines challan type (BASICORDERTERMS in TallyPrime)
        terms_of_delivery = (
            voucher.findtext(".//BASICORDERTERMS") or
            voucher.findtext(".//TERMSOFDELIVERY") or
            voucher.findtext(".//BASICTERMSOFDELIVERY") or
            voucher.findtext(".//DELIVERYTERMS") or
            ""
        ).strip()
        if terms_of_delivery:
            data["TERMSOFDELIVERY"] = terms_of_delivery

        # "PO Number" / "Reference" field - Purchase Order reference
        po_number = (
            voucher.findtext(".//REFERENCE") or
            voucher.findtext(".//BASICPURCHASEORDERNO") or
            voucher.findtext(".//PURCHASEORDERNO") or
            voucher.findtext(".//BASICORDERREF") or
            voucher.findtext(".//ORDERREF") or
            voucher.findtext(".//VOUCHERREFERENCE") or
            voucher.findtext(".//BASICBUYERORDERNO") or
            ""
        ).strip()
        if po_number:
            data["PONUMBER"] = po_number

        # "Order No(s)" field -- Tally Prime stores in INVOICEORDERLIST.LIST
        _skip_order_vals = {'not applicable', 'n/a', 'na', 'nil', 'none', '-', ''}
        order_no = ''
        order_date = ''
        # 1) INVOICEORDERLIST.LIST (primary location for Order Details in Tally Prime)
        for iol in voucher.findall(".//INVOICEORDERLIST.LIST"):
            _on = (iol.findtext("BASICPURCHASEORDERNO") or iol.findtext("ORDERNO") or "").strip()
            if _on and _on.lower() not in _skip_order_vals:
                order_no = _on
                _od = (iol.findtext("BASICORDERDATE") or iol.findtext("ORDERDATE") or "").strip()
                if _od:
                    order_date = _od
                break
        # 2) ORDERCOLLECTION.LIST / SALESORDERDETAILS.LIST (alternate locations)
        if not order_no:
            for list_tag in (".//ORDERCOLLECTION.LIST", ".//SALESORDERDETAILS.LIST"):
                for sod in voucher.findall(list_tag):
                    _on = (sod.findtext("BASICPURCHASEORDERNO") or sod.findtext("BASICORDERNO") or sod.findtext("ORDERNO") or sod.findtext("PARTYORDERNO") or "").strip()
                    if _on and _on.lower() not in _skip_order_vals:
                        order_no = _on
                        if not order_date:
                            order_date = (sod.findtext("BASICORDERDATE") or sod.findtext("ORDERDATE") or sod.findtext("PARTYORDERDATE") or "").strip()
                        break
                if order_no:
                    break
        # 3) Top-level fields (fallback)
        if not order_no:
            for _field in (".//PARTYORDERNO", ".//BASICPURCHASEORDERNO", ".//SALESORDERNO", ".//ORDERNO"):
                _val = (voucher.findtext(_field) or "").strip()
                if _val and _val.lower() not in _skip_order_vals:
                    order_no = _val
                    break
        if not order_date:
            for _field in (".//PARTYORDERDATE", ".//BASICORDERDATE", ".//ORDERDATE", ".//SALESORDERDATE"):
                _val = (voucher.findtext(_field) or "").strip()
                if _val:
                    order_date = _val
                    break
        if order_no:
            data["PARTYORDERNO"] = order_no
        if order_date:
            data["PARTYORDERDATE"] = order_date

        # "Other References" field â€" free text reference (e.g. "Delivery")
        other_ref = (
            voucher.findtext(".//VOUCHERREFERENCE") or
            voucher.findtext(".//REFERENCE2") or
            ""
        ).strip()
        if other_ref:
            data["OTHERREFERENCE"] = other_ref

        # Unified vehicle number: prefer DISPATCHEDTHROUGH, fallback to MOTORVEHICLENO
        vehicle_no = data.get("DISPATCHEDTHROUGH") or data.get("MOTORVEHICLENO") or ""
        if vehicle_no:
            data["VEHICLENO"] = vehicle_no

        # Extract ledger entries for tax information
        ledger_entries = []
        for ledger_tag in ["LEDGERENTRIES.LIST", "ALLLEDGERENTRIES.LIST"]:
            for ledger in voucher.findall(f".//{ledger_tag}"):
                entry = {}
                for child in ledger:
                    if not child.tag.endswith(".LIST"):
                        entry[child.tag.upper()] = (child.text or "").strip()
                if entry:
                    ledger_entries.append(entry)
        if ledger_entries:
            data["LEDGERENTRIES"] = ledger_entries

        vouchers.append(data)
    return vouchers


def get_ledger_by_name(company_name: str, ledger_name: str, url: Optional[str] = None) -> Optional[Dict]:
    """Fetch a single ledger by name from Tally with ALL fields including mailing details."""
    # Escape XML special characters in names
    from xml.sax.saxutils import escape as xml_escape
    safe_company = xml_escape(company_name)
    safe_ledger = xml_escape(ledger_name)
    xml = f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>SingleLedger</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" ISFIXED="No" ISINITIALIZE="Yes" ISOPTION="No" ISINTERNAL="No" NAME="SingleLedger">
            <TYPE>Ledger</TYPE>
            <FILTERS>ByName</FILTERS>
            <NATIVEMETHOD>Name</NATIVEMETHOD>
            <NATIVEMETHOD>GUID</NATIVEMETHOD>
            <NATIVEMETHOD>MasterID</NATIVEMETHOD>
            <NATIVEMETHOD>Parent</NATIVEMETHOD>
            <NATIVEMETHOD>Mobile</NATIVEMETHOD>
            <NATIVEMETHOD>Email</NATIVEMETHOD>
            <NATIVEMETHOD>LedgerMobile</NATIVEMETHOD>
            <NATIVEMETHOD>LedgerEmail</NATIVEMETHOD>
            <NATIVEMETHOD>IncomeTaxNumber</NATIVEMETHOD>
            <NATIVEMETHOD>GSTRegistration</NATIVEMETHOD>
            <NATIVEMETHOD>PartyGSTIN</NATIVEMETHOD>
            <NATIVEMETHOD>GSTIN</NATIVEMETHOD>
            <NATIVEMETHOD>Address</NATIVEMETHOD>
            <NATIVEMETHOD>StateName</NATIVEMETHOD>
            <NATIVEMETHOD>CountryName</NATIVEMETHOD>
            <NATIVEMETHOD>PinCode</NATIVEMETHOD>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="ByName">$NAME = "{safe_ledger}"</SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    try:
        resp = send_request(xml, url)
        ledgers = parse_ledgers_full(resp)
        return ledgers[0] if ledgers else None
    except Exception:
        logger.exception("Failed to fetch ledger %s", ledger_name)
        return None


def parse_ledgers_full(response_xml: str) -> List[Dict]:
    """
    Parse ledger response with ALL fields including nested lists like:
    - LEDGERMAILINGDETAILS.LIST (for multiple mailing/delivery addresses)
    - ADDRESS.LIST (for primary address)
    """
    ledgers = []
    root = ET.fromstring(_clean_invalid_char_refs(response_xml))

    def parse_nested_lists(element):
        """Recursively parse nested .LIST elements, handling multiple elements with same tag."""
        result = {}
        for child in element:
            tag = child.tag.upper()
            if child.tag.endswith(".LIST"):
                list_name = tag.replace(".LIST", "_LIST")
                if list_name not in result:
                    result[list_name] = []
                list_item = parse_nested_lists(child)
                if list_item:
                    result[list_name].append(list_item)
            else:
                text = (child.text or "").strip()
                # Handle multiple elements with same tag (e.g., multiple ADDRESS elements)
                if tag in result:
                    # Convert to list if not already
                    if not isinstance(result[tag], list):
                        result[tag] = [result[tag]]
                    result[tag].append(text)
                else:
                    result[tag] = text
        return result

    def extract_address_lines(element):
        """Extract all ADDRESS elements from an ADDRESS.LIST element."""
        addresses = []
        for addr in element.findall("ADDRESS"):
            if addr.text:
                addresses.append(addr.text.strip())
        return addresses

    def filter_address_parts(address_lines: list) -> tuple:
        """
        Filter address lines to separate actual address from email/phone.
        Returns (address_str, extracted_email, extracted_phone)
        """
        addr_parts = []
        extracted_email = ""
        extracted_phone = ""

        for line in address_lines:
            line_lower = line.lower()
            # Check if this line is email
            if line_lower.startswith("email:") or (("@" in line) and ("." in line.split("@")[-1])):
                email_match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', line)
                if email_match:
                    extracted_email = email_match.group(0)
            # Check if this line is phone
            elif line_lower.startswith("phone:") or line_lower.startswith("ph:") or line_lower.startswith("mobile:") or line_lower.startswith("tel:"):
                phone_match = re.search(r'[\d\s\-+]+', line)
                if phone_match:
                    phone = re.sub(r'[^\d+]', '', phone_match.group(0))
                    if len(phone) >= 10:
                        extracted_phone = phone[:15]
            else:
                # Regular address line
                addr_parts.append(line)

        return (", ".join(addr_parts), extracted_email, extracted_phone)

    # Parse from Collection-based response
    for collection in root.findall(".//COLLECTION"):
        for ledger in collection:
            if ledger.tag == "LEDGER":
                data = {}

                # Extract the NAME attribute from the LEDGER element
                ledger_name = ledger.get("NAME", "")
                if ledger_name:
                    data["NAME"] = ledger_name

                # Parse all children including nested lists
                for child in ledger:
                    tag = child.tag.upper()
                    if child.tag.endswith(".LIST"):
                        list_name = tag.replace(".LIST", "_LIST")
                        if list_name not in data:
                            data[list_name] = []
                        list_item = parse_nested_lists(child)
                        if list_item:
                            data[list_name].append(list_item)
                    else:
                        # Handle special character encoding
                        text = (child.text or "").strip()
                        if text:
                            text = text.replace('&#4;', '').strip()
                        data[tag] = text

                # Extract primary address from LEDMAILINGDETAILS.LIST (primary mailing details)
                # This list contains the main mailing address with potentially embedded email/phone
                for mailing_elem in ledger.findall("LEDMAILINGDETAILS.LIST"):
                    # Get address from nested ADDRESS.LIST
                    for addr_list in mailing_elem.findall("ADDRESS.LIST"):
                        address_lines = extract_address_lines(addr_list)
                        if address_lines:
                            addr_str, extracted_email, extracted_phone = filter_address_parts(address_lines)
                            if addr_str:
                                data["PRIMARY_ADDRESS"] = addr_str
                            if extracted_email and not data.get("EMAIL"):
                                data["EMAIL"] = extracted_email
                            if extracted_phone and not data.get("MOBILE"):
                                data["MOBILE"] = extracted_phone
                            break
                    # Also get pincode, state, country from LEDMAILINGDETAILS.LIST
                    pincode = (mailing_elem.findtext("PINCODE") or "").strip()
                    state = (mailing_elem.findtext("STATE") or mailing_elem.findtext("PRIORSTATENAME") or "").strip()
                    country = (mailing_elem.findtext("COUNTRY") or mailing_elem.findtext("COUNTRYNAME") or "").strip()
                    if pincode and not data.get("PINCODE"):
                        data["PINCODE"] = pincode
                    if state and not data.get("STATE"):
                        data["STATE"] = state
                    if country and not data.get("COUNTRY"):
                        data["COUNTRY"] = country
                    break  # Use first LEDMAILINGDETAILS.LIST as primary

                # Fallback: try direct ADDRESS.LIST at ledger level
                if not data.get("PRIMARY_ADDRESS"):
                    for addr_list in ledger.findall("ADDRESS.LIST"):
                        address_lines = extract_address_lines(addr_list)
                        if address_lines:
                            addr_str, extracted_email, extracted_phone = filter_address_parts(address_lines)
                            if addr_str:
                                data["PRIMARY_ADDRESS"] = addr_str
                            if extracted_email and not data.get("EMAIL"):
                                data["EMAIL"] = extracted_email
                            if extracted_phone and not data.get("MOBILE"):
                                data["MOBILE"] = extracted_phone
                            break

                # Extract delivery addresses from LEDMULTIADDRESSLIST.LIST
                delivery_addresses = []
                for multi_addr_elem in ledger.findall("LEDMULTIADDRESSLIST.LIST"):
                    addr_info = {
                        "name": (multi_addr_elem.findtext("ADDRESSNAME") or multi_addr_elem.findtext("MAILINGNAME") or "").strip(),
                        "address": "",
                        "state": (multi_addr_elem.findtext("STATE") or multi_addr_elem.findtext("PRIORSTATENAME") or "").strip(),
                        "country": (multi_addr_elem.findtext("COUNTRYNAME") or multi_addr_elem.findtext("COUNTRY") or "").strip(),
                        "pincode": (multi_addr_elem.findtext("PINCODE") or "").strip(),
                        "gstin": (multi_addr_elem.findtext("PARTYGSTIN") or "").strip(),
                    }
                    # Get address from nested ADDRESS.LIST
                    for addr_list in multi_addr_elem.findall("ADDRESS.LIST"):
                        address_lines = extract_address_lines(addr_list)
                        if address_lines:
                            # Filter email/phone from delivery addresses too
                            addr_str, _, _ = filter_address_parts(address_lines)
                            if addr_str:
                                addr_info["address"] = addr_str
                            break
                    if addr_info["address"] or addr_info["name"]:
                        delivery_addresses.append(addr_info)
                if delivery_addresses:
                    data["DELIVERY_ADDRESSES"] = delivery_addresses

                # Extract GSTIN from LEDGSTREGDETAILS.LIST if not already set
                if not data.get("GSTIN"):
                    for gst_elem in ledger.findall("LEDGSTREGDETAILS.LIST"):
                        gstin = (gst_elem.findtext("GSTIN") or "").strip()
                        if gstin:
                            data["GSTIN"] = gstin
                            break

                # Extract email and phone
                data["EMAIL"] = data.get("EMAIL", "") or data.get("LEDGEREMAIL", "") or data.get("EMAILID", "")
                data["MOBILE"] = data.get("LEDGERMOBILE", "") or data.get("MOBILE", "") or data.get("PHONENUMBER", "")
                data["PAN"] = data.get("INCOMETAXNUMBER", "") or data.get("PANNUMBER", "")
                data["GSTIN"] = (data.get("GSTIN", "") or data.get("PARTYGSTIN", "") or data.get("GSTREGISTRATION", "")).lstrip(":")
                data["STATE"] = data.get("PRIORSTATENAME", "") or data.get("STATENAME", "") or data.get("LEDSTATENAME", "")
                data["COUNTRY"] = data.get("COUNTRYNAME", "") or data.get("COUNTRYOFRESIDENCE", "")
                data["PINCODE"] = data.get("PINCODE", "") or data.get("LEDGERPINCODE", "")

                ledgers.append(data)

    # Fallback: try direct LEDGER search if no ledgers found
    if not ledgers:
        for ledger in root.findall(".//LEDGER"):
            data = {}
            ledger_name = ledger.get("NAME", "")
            if ledger_name:
                data["NAME"] = ledger_name

            for child in ledger:
                tag = child.tag.upper()
                if child.tag.endswith(".LIST"):
                    list_name = tag.replace(".LIST", "_LIST")
                    if list_name not in data:
                        data[list_name] = []
                    list_item = parse_nested_lists(child)
                    if list_item:
                        data[list_name].append(list_item)
                else:
                    text = (child.text or "").strip()
                    if text:
                        text = text.replace('&#4;', '').strip()
                    data[tag] = text

            # Extract primary address from LEDMAILINGDETAILS.LIST (primary mailing details)
            for mailing_elem in ledger.findall("LEDMAILINGDETAILS.LIST"):
                for addr_list in mailing_elem.findall("ADDRESS.LIST"):
                    address_lines = extract_address_lines(addr_list)
                    if address_lines:
                        addr_str, extracted_email, extracted_phone = filter_address_parts(address_lines)
                        if addr_str:
                            data["PRIMARY_ADDRESS"] = addr_str
                        if extracted_email and not data.get("EMAIL"):
                            data["EMAIL"] = extracted_email
                        if extracted_phone and not data.get("MOBILE"):
                            data["MOBILE"] = extracted_phone
                        break
                # Get pincode, state, country from LEDMAILINGDETAILS.LIST
                pincode = (mailing_elem.findtext("PINCODE") or "").strip()
                state = (mailing_elem.findtext("STATE") or mailing_elem.findtext("PRIORSTATENAME") or "").strip()
                country = (mailing_elem.findtext("COUNTRY") or mailing_elem.findtext("COUNTRYNAME") or "").strip()
                if pincode and not data.get("PINCODE"):
                    data["PINCODE"] = pincode
                if state and not data.get("STATE"):
                    data["STATE"] = state
                if country and not data.get("COUNTRY"):
                    data["COUNTRY"] = country
                break  # Use first LEDMAILINGDETAILS.LIST as primary

            # Fallback: try direct ADDRESS.LIST at ledger level
            if not data.get("PRIMARY_ADDRESS"):
                for addr_list in ledger.findall("ADDRESS.LIST"):
                    address_lines = extract_address_lines(addr_list)
                    if address_lines:
                        addr_str, extracted_email, extracted_phone = filter_address_parts(address_lines)
                        if addr_str:
                            data["PRIMARY_ADDRESS"] = addr_str
                        if extracted_email and not data.get("EMAIL"):
                            data["EMAIL"] = extracted_email
                        if extracted_phone and not data.get("MOBILE"):
                            data["MOBILE"] = extracted_phone
                        break

            # Extract delivery addresses from LEDMULTIADDRESSLIST.LIST
            delivery_addresses = []
            for multi_addr_elem in ledger.findall("LEDMULTIADDRESSLIST.LIST"):
                addr_info = {
                    "name": (multi_addr_elem.findtext("ADDRESSNAME") or multi_addr_elem.findtext("MAILINGNAME") or "").strip(),
                    "address": "",
                    "state": (multi_addr_elem.findtext("STATE") or multi_addr_elem.findtext("PRIORSTATENAME") or "").strip(),
                    "country": (multi_addr_elem.findtext("COUNTRYNAME") or multi_addr_elem.findtext("COUNTRY") or "").strip(),
                    "pincode": (multi_addr_elem.findtext("PINCODE") or "").strip(),
                    "gstin": (multi_addr_elem.findtext("PARTYGSTIN") or "").strip(),
                }
                for addr_list in multi_addr_elem.findall("ADDRESS.LIST"):
                    address_lines = extract_address_lines(addr_list)
                    if address_lines:
                        addr_str, _, _ = filter_address_parts(address_lines)
                        if addr_str:
                            addr_info["address"] = addr_str
                        break
                if addr_info["address"] or addr_info["name"]:
                    delivery_addresses.append(addr_info)
            if delivery_addresses:
                data["DELIVERY_ADDRESSES"] = delivery_addresses

            # Extract GSTIN from LEDGSTREGDETAILS.LIST if not already set
            if not data.get("GSTIN"):
                for gst_elem in ledger.findall("LEDGSTREGDETAILS.LIST"):
                    gstin = (gst_elem.findtext("GSTIN") or "").strip()
                    if gstin:
                        data["GSTIN"] = gstin
                        break

            data["EMAIL"] = data.get("EMAIL", "") or data.get("LEDGEREMAIL", "") or data.get("EMAILID", "")
            data["MOBILE"] = data.get("LEDGERMOBILE", "") or data.get("MOBILE", "") or data.get("PHONENUMBER", "")
            data["PAN"] = data.get("INCOMETAXNUMBER", "") or data.get("PANNUMBER", "")
            data["GSTIN"] = (data.get("GSTIN", "") or data.get("PARTYGSTIN", "") or data.get("GSTREGISTRATION", "")).lstrip(":")
            data["STATE"] = data.get("PRIORSTATENAME", "") or data.get("STATENAME", "") or data.get("LEDSTATENAME", "")
            data["COUNTRY"] = data.get("COUNTRYNAME", "") or data.get("COUNTRYOFRESIDENCE", "")
            data["PINCODE"] = data.get("PINCODE", "") or data.get("LEDGERPINCODE", "")

            # Only add if ledger has meaningful data (NAME or GUID)
            if data.get("NAME") or data.get("GUID") or data.get("REMOTEGUID"):
                ledgers.append(data)

    return ledgers


def get_stock_item_by_name(company_name: str, item_name: str, url: Optional[str] = None) -> Optional[Dict]:
    """Fetch a single stock item by name from Tally with all details including HSN and GST."""
    from xml.sax.saxutils import escape as xml_escape
    safe_company = xml_escape(company_name)
    safe_item = xml_escape(item_name)
    xml = f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>SingleStockItem</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" NAME="SingleStockItem">
            <TYPE>StockItem</TYPE>
            <FILTERS>ByName</FILTERS>
            <NATIVEMETHOD>Name</NATIVEMETHOD>
            <NATIVEMETHOD>GUID</NATIVEMETHOD>
            <NATIVEMETHOD>MasterID</NATIVEMETHOD>
            <NATIVEMETHOD>Parent</NATIVEMETHOD>
            <NATIVEMETHOD>BaseUnits</NATIVEMETHOD>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="ByName">$NAME = "{safe_item}"</SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    try:
        resp = send_request(xml, url)
        items = parse_stock_items_full(resp)
        return items[0] if items else None
    except Exception:
        logger.exception("Failed to fetch stock item %s", item_name)
        return None


def get_delivery_notes(company_name: str, url: Optional[str] = None, from_date: str = "20240101", to_date: str = "20991231"):
    """
    Fetch Delivery Notes from Tally using multiple methods.

    Uses the Data Book report with VoucherTypeName filter (most reliable method).
    Falls back to Collection-based query if needed.
    """
    from xml.sax.saxutils import escape as xml_escape
    safe_company = xml_escape(company_name)
    # Method 1: Data Book with VoucherTypeName (most reliable)
    xml = f"""
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Day Book</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVFROMDATE>{from_date}</SVFROMDATE>
          <SVTODATE>{to_date}</SVTODATE>
          <VOUCHERTYPENAME>Delivery Note</VOUCHERTYPENAME>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>
"""
    resp = send_request(xml, url)
    logger.debug("Delivery Notes response (Data Book): %s", resp[:2000])
    vouchers = parse_delivery_notes(resp)

    if vouchers:
        logger.info("Found %d delivery notes using Data Book method", len(vouchers))
        return vouchers

    # Method 2: Collection with CONTAINS filter
    fallback_xml = f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>DeliveryNoteVouchers</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
        <SVFROMDATE>{from_date}</SVFROMDATE>
        <SVTODATE>{to_date}</SVTODATE>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" NAME="DeliveryNoteVouchers">
            <TYPE>Voucher</TYPE>
            <FILTERS>DeliveryNoteFilter</FILTERS>
            <FETCH>GUID</FETCH>
            <FETCH>VoucherNumber</FETCH>
            <FETCH>VouchertypeName</FETCH>
            <FETCH>Date</FETCH>
            <FETCH>PartyLedgerName</FETCH>
            <FETCH>Reference</FETCH>
            <FETCH>VoucherReference</FETCH>
            <FETCH>PartyOrderNo</FETCH>
            <FETCH>PartyOrderDate</FETCH>
            <FETCH>DispatchedThrough</FETCH>
            <FETCH>MotorVehicleNo</FETCH>
            <FETCH>BasicShippedBy</FETCH>
            <FETCH>TermsOfDelivery</FETCH>
            <FETCH>Narration</FETCH>
            <FETCH>BasicBuyerName</FETCH>
            <FETCH>ALLINVENTORYENTRIES.STOCKITEMNAME</FETCH>
            <FETCH>ALLINVENTORYENTRIES.ACTUALQTY</FETCH>
            <FETCH>ALLINVENTORYENTRIES.RATE</FETCH>
            <FETCH>ALLINVENTORYENTRIES.AMOUNT</FETCH>
            <FETCH>LEDGERENTRIES.LEDGERNAME</FETCH>
            <FETCH>LEDGERENTRIES.AMOUNT</FETCH>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="DeliveryNoteFilter">($VoucherTypeName CONTAINS "Delivery") AND ($$IsBetween:$Date:@@SVFROMDATE:@@SVTODATE)</SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    fallback_resp = send_request(fallback_xml, url)
    logger.debug("Delivery Notes response (Collection filter): %s", fallback_resp[:2000])
    vouchers = parse_delivery_notes(fallback_resp)
    logger.info("Found %d delivery notes using Collection filter method", len(vouchers))
    return vouchers


def get_sales_invoices(company_name: str, url: Optional[str] = None, from_date: str = "20240101", to_date: str = "20991231") -> List[Dict]:
    """
    Fetch all sales and delivery invoices for a company within a date range.
    Uses the standard Tally report export for maximum reliability on large databases.
    """
    def _pqty(val):
        """Parse quantity string to float, preferring NOS unit if available (e.g. '(5 NOS)' or '= 100 NOS')."""
        try:
            s_val = str(val).strip()
            if 'NOS' in s_val.upper():
                import re
                # Matches either "( 5 NOS )" or "= 100 NOS"
                match = re.search(r'[\(=]\s*([\d\.,]+)\s*[Nn][Oo][Ss]', s_val)
                if match:
                    return float(match.group(1).replace(',', ''))
                
                # Fallback if the pattern is slightly different but still contains NOS
                match = re.search(r'([\d\.,]+)\s*[Nn][Oo][Ss]', s_val)
                if match:
                    return float(match.group(1).replace(',', ''))

            # Default to the first number in the string (the CM/KG value)
            return float(s_val.split()[0].replace(',', ''))
        except Exception:
            return 0.0

    def _prate(val):
        try:
            s = str(val).replace(',', '').strip()
            if '/' in s:
                s = s.split('/')[0].strip()
            if s:
                s = s.split()[0]
            return float(s)
        except Exception:
            return 0.0

    from xml.sax.saxutils import escape as xml_escape
    safe_company = xml_escape(company_name)
    
    # Use standard Data Book report export which is highly optimized in Tally Prime
    # for large date ranges and high-volume databases like BHOX's.
    xml = f"""
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVFROMDATE>{from_date}</SVFROMDATE>
          <SVTODATE>{to_date}</SVTODATE>
        </STATICVARIABLES>
        <REPORTNAME>Day Book</REPORTNAME>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>
"""
    try:
        resp = send_request(xml, url, timeout=config.TALLY_TIMEOUT_VOUCHER)
        raw_vouchers = parse_delivery_notes(resp)
        logger.info("[get_sales_invoices] Company='%s' got %d total vouchers from Tally report", company_name, len(raw_vouchers))
        
        normalized = []
        for v in raw_vouchers:
            # Filter: keep Sales and Invoice voucher types, drop payments/receipts/journals
            vtype = (v.get('VOUCHERTYPENAME') or v.get('VOUCHERTYPE') or v.get('VCHTYPE') or '').strip().lower()

            if 'sale' not in vtype and 'invoice' not in vtype:
                continue
                
            vno = (v.get('VOUCHERNUMBER') or v.get('VCHNUMBER') or v.get('VOUCHERNO') or '').strip()
            if not vno:
                vno = (v.get('GUID') or f"AUTO-{len(normalized)+1}")
            
            # Party detection - handle entries if top-level party name is missing
            cust = (v.get('PARTYLEDGERNAME') or v.get('PARTYNAME') or '').strip()
            if not cust and v.get('LEDGERENTRIES'):
                for le in v.get('LEDGERENTRIES'):
                    l_name = (le.get('LEDGERNAME') or '').upper()
                    if not any(x in l_name for x in ['GST', 'TAX', 'CESS', 'DUTY', 'CASH', 'ROUND']):
                        cust = le.get('LEDGERNAME')
                        break
            
            inv = {
                'guid': v.get('GUID', ''),
                'voucher_no': vno,
                'voucher_date': v.get('DATE', '') or v.get('VOUCHERDATE', ''),
                'customer_name': cust or 'UNKNOWN',
                'customer_guid': '',
                'billing_address': '',
                'delivery_address': '',
                'total_amount': 0.0,
                'tax_amount': 0.0,
                'items': [],
                'raw_voucher': dict(v),
            }
            
            for it in v.get('INVENTORY', []) or []:
                item_dict = {
                    'item_name': it.get('STOCKITEMNAME', ''),
                    'quantity': _pqty(it.get('ACTUALQTY', '0')),
                    'rate': _prate(it.get('RATE', '0')),
                    'amount': _prate(it.get('AMOUNT', '0')),
                }
                # BILLEDQTY holds the number-of-cylinders (NOS) when Tally uses
                # compound units (e.g. CUM for stock, NOS for billing).
                billed_raw = it.get('BILLEDQTY', '').strip()
                if billed_raw:
                    nos = _pqty(billed_raw)
                    if nos and nos > 0:
                        item_dict['nos_qty'] = nos
                if it.get('BASICUSERDESCRIPTION'):
                    item_dict['user_description'] = it['BASICUSERDESCRIPTION']
                inv['items'].append(item_dict)
            
            for le in v.get('LEDGERENTRIES', []) or []:
                name = (le.get('LEDGERNAME') or '').upper()
                amt = _prate(le.get('AMOUNT', '0'))
                if any(t in name for t in ['CGST','SGST','IGST','GST']):
                    inv['tax_amount'] += abs(amt)
                elif abs(amt) and not any(t in name for t in ['ROUND', 'ROUNDOFF', 'ROUND OFF']):
                    # The party/customer ledger entry is always the highest amount
                    # (it equals goods + GST + freight = the exact invoice total).
                    inv['total_amount'] = max(inv['total_amount'], abs(amt))

            if inv['total_amount'] == 0.0 and inv['items']:
                inv['total_amount'] = round(sum(abs(x.get('amount', 0)) for x in inv['items']), 2)
                
            normalized.append(inv)
            
        logger.info("[get_sales_invoices] Normalized %d Sales/Delivery invoices for '%s'", len(normalized), company_name)
        return normalized
    except Exception:
        logger.exception("[get_sales_invoices] FAILED for company '%s'", company_name)
        return []


def get_voucher_by_number(company_name: str, voucher_no: str, url: Optional[str] = None) -> Optional[Dict]:
    """
    Fetch a single voucher by number from Tally using a Collection filter.
    Useful for diagnosis and targeted sync.
    """
    from xml.sax.saxutils import escape as xml_escape
    safe_company = xml_escape(company_name)
    safe_no = xml_escape(voucher_no)
    
    xml = f"""
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
        <REPORTNAME>Day Book</REPORTNAME>
      </REQUESTDESC>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="TargetVoucher" ISMODIFY="No">
            <TYPE>Voucher</TYPE>
            <FILTER>VoucherNumberFilter</FILTER>
            <FETCH>*</FETCH>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="VoucherNumberFilter">$VoucherNumber = "{safe_no}"</SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>
"""
    try:
        resp = send_request(xml, url, timeout=config.TALLY_TIMEOUT_LEDGER)
        vouchers = parse_delivery_notes(resp)
        if vouchers:
            # Also normalize to common schema
            v = vouchers[0]
            # Since parse_delivery_notes already returns a normalized list, just return the first
            return v
    except Exception:
        logger.exception("[get_voucher_by_number] FAILED to fetch '%s' for '%s'", voucher_no, company_name)
    return None

# ============================================================================
# TallyClient Class Wrapper for BHOX Middleware
# ============================================================================

class TallyClient:
    """
    Class wrapper around Tally API functions for compatibility with middleware.
    Maps customer/product terminology to Tally's ledger/stock_item terminology.
    """
    
    def __init__(self, url: str = "http://localhost:9000/"):
        self.url = url.rstrip("/") + "/"
    
    def get_customers(self, company_name: str) -> List[Dict]:
        """
        Fetch ALL customers (all ledger groups) from Tally company.

        Args:
            company_name: Tally company name

        Returns:
            List of customer dictionaries with normalized fields
        """
        ledgers = get_all_customer_ledgers(company_name, self.url)

        customers = []
        for ledger in ledgers:
            customer = {
                'guid': ledger.get("GUID", ""),
                'name': ledger.get("NAME", ""),
                'parent_group': ledger.get("PARENT", ""),
                'gstin': (ledger.get("GSTIN", "") or ledger.get("PARTYGSTIN", "") or ledger.get("GSTREGISTRATION", "")).lstrip(":"),
                'pan': ledger.get("INCOMETAXNUMBER", "") or ledger.get("PANNUMBER", ""),
                'address': ", ".join(ledger.get("ADDRESSES", [])) if ledger.get("ADDRESSES") else "",
                'state': ledger.get("STATENAME", "") or ledger.get("PRIORSTATENAME", ""),
                'city': "",
                'pincode': ledger.get("PINCODE", ""),
                'phone': ledger.get("MOBILE", "") or ledger.get("LEDGERMOBILE", ""),
                'email': ledger.get("EMAIL", "") or ledger.get("LEDGEREMAIL", "")
            }
            customers.append(customer)

        return customers
    
    def get_products(self, company_name: str) -> List[Dict]:
        """
        Fetch products (stock items) from Tally company.
        Includes HSN code and GST rates.

        Args:
            company_name: Tally company name

        Returns:
            List of product dictionaries with normalized fields
        """
        # Fetch stock items (uses parse_stock_items_full for HSN/GST)
        stock_items = get_stock_items(company_name, self.url)

        # Normalize field names
        products = []
        for item in stock_items:
            product = {
                'guid': item.get("GUID", ""),
                'name': item.get("NAME", ""),
                'hsn_code': item.get("HSNCODE", ""),
                'unit': item.get("BASEUNITS", ""),
                'rate': self._prate(item.get("STANDARDPRICE", "0")),
                'gst_applicable': item.get("GSTAPPLICABLE", ""),
                'gst_rate': item.get("GST_RATE", 0.0),
                'igst_rate': item.get("IGST_RATE", 0.0),
                'cgst_rate': item.get("CGST_RATE", 0.0),
                'sgst_rate': item.get("SGST_RATE", 0.0),
                'description': item.get("PARENT", "")  # Use parent as description
            }
            products.append(product)

        return products
    
    def _prate(self, rate_str: str) -> float:
        """Parse rate string to float.
        Handles Tally formats: "500.00", "-1000.00", "500.00/Cyl", "1,000.00/Nos"
        """
        try:
            rate_str = str(rate_str).replace(",", "").strip()
            # Tally RATE field is "amount/unit", e.g. "500.00/Cyl" â€" take only the numeric part
            if "/" in rate_str:
                rate_str = rate_str.split("/")[0].strip()
            # Tally AMOUNT fields can have a trailing unit separated by space
            rate_str = rate_str.split()[0] if rate_str else rate_str
            return float(rate_str)
        except (ValueError, TypeError, IndexError):
            return 0.0

    # get_sales_invoices delegates to the module-level function of the same name
    # which accepts (company_name, url, from_date, to_date).
    def get_sales_invoices(self, company_name: str, from_date: str = "20240101", to_date: str = "20991231") -> List[Dict]:
        """
        Fetch sales invoices from Tally company.
        
        Args:
            company_name: Tally company name
            from_date: Start date in YYYYMMDD format
            to_date: End date in YYYYMMDD format
            
        Returns:
            List of invoice dictionaries
        """
        return get_sales_invoices(company_name, self.url, from_date, to_date)
        # Call the module-level function
        vouchers = get_sales_invoices(company_name, self.url, from_date, to_date)
        
        # Normalize invoice data for middleware
        invoices = []
        for voucher in vouchers:
            invoice = {
                'guid': voucher.get("GUID", ""),
                'voucher_no': voucher.get("VOUCHERNUMBER", "") or voucher.get("VOUCHERNO", "") or voucher.get("VCHNUMBER", "") or voucher.get("VCHNO", "") or voucher.get("NUMBER", ""),
                'voucher_date': voucher.get("DATE", "") or voucher.get("VOUCHERDATE", "") or voucher.get("DSPVCHDATE", ""),
                'customer_name': voucher.get("PARTYLEDGERNAME", "") or voucher.get("PARTYNAME", ""),
                'customer_guid': "",  # Not in voucher data
                'billing_address': ", ".join(voucher.get("ADDRESSES", [])) if voucher.get("ADDRESSES") else "",
                'delivery_address': voucher.get("CONSIGNEE", {}).get("ADDRESS", "") if voucher.get("CONSIGNEE") else "",
                'total_amount': 0.0,  # Calculate from ledger entries
                'tax_amount': 0.0,   # Calculate from ledger entries
                'items': [],
                # Preserve full voucher so fetch layer can store complete payload for sync
                'raw_voucher': dict(voucher)
            }
            
            # Process inventory items
            for inv_item in voucher.get("INVENTORY", []):
                item = {
                    'item_name': inv_item.get("STOCKITEMNAME", ""),
                    'quantity': self._pqty(inv_item.get("ACTUALQTY", "0")),
                    'rate': self._prate(inv_item.get("RATE", "0")),
                    'amount': self._prate(inv_item.get("AMOUNT", "0"))
                }
                invoice['items'].append(item)
            
            # Calculate totals from ledger entries
            for ledger_entry in voucher.get("LEDGERENTRIES", []):
                amount_str = ledger_entry.get("AMOUNT", "0")
                amount = self._prate(amount_str)

                ledger_name = ledger_entry.get("LEDGERNAME", "").upper()
                if any(tax in ledger_name for tax in ["CGST", "SGST", "IGST", "GST"]):
                    invoice['tax_amount'] += abs(amount)
                elif amount != 0:  # Non-zero non-tax entry â€" party debit (positive) or sales credit (negative)
                    candidate = abs(amount)
                    if candidate > invoice['total_amount']:
                        invoice['total_amount'] = candidate

            # Fallback: LEDGERENTRIES may be empty or all-tax; sum inventory item amounts
            if invoice['total_amount'] == 0.0 and invoice['items']:
                invoice['total_amount'] = sum(abs(item['amount']) for item in invoice['items'])

            invoices.append(invoice)
        
        return invoices
    
    def _pqty(self, qty_str: str) -> float:
        """Parse quantity string to float, preferring NOS in parentheses if available."""
        try:
            s_val = str(qty_str).strip()
            # If value contains parentheses with NOS (e.g. "35.00 CM (5 NOS)"), take the NOS value
            if '(' in s_val and ')' in s_val:
                import re
                # Match numeric value before 'NOS' or similar inside parentheses
                match = re.search(r'\(([\d\.,]+)\s*[Nn][Oo][Ss]\)', s_val)
                if match:
                    return float(match.group(1).replace(',', ''))
                # Fallback: take any numeric value inside parentheses if NOS is mentioned but not perfectly matched
                if 'NOS' in s_val.upper():
                    match = re.search(r'\(([\d\.,]+)', s_val)
                    if match:
                        return float(match.group(1).replace(',', ''))

            # Standard parsing: take the first numeric part
            qty_str = s_val.split()[0].replace(",", "").strip()
            return float(qty_str)
        except (ValueError, TypeError, IndexError):
            return 0.0


