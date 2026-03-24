import logging
import os
import re
import time
import threading
from typing import List, Dict, Optional

import requests
import xml.etree.ElementTree as ET

DEFAULT_URL = "http://localhost:9000/"
logger = logging.getLogger(__name__)

# Global lock to serialize all Tally requests.
# Tally ERP9's XML API cannot handle concurrent connections and crashes with
# "Software Exception c0000005 (Memory Access Violation)" when multiple
# requests arrive simultaneously (e.g. from automation threads).
_tally_lock = threading.Lock()

# Cooldown in seconds between consecutive Tally requests.
# Tally needs time to free memory between requests, especially on remote access.
# CRITICAL: DO NOT REDUCE THIS VALUE!
# On 8GB RAM systems, reducing cooldown causes "c0000005 Memory Access Violation" crashes.
# 10 seconds is the MINIMUM safe interval for remote Tally access.
def _read_tally_cooldown() -> float:
    raw = os.getenv("TALLY_REQUEST_COOLDOWN", "").strip()
    if not raw:
        return 10.0
    try:
        value = float(raw)
    except ValueError:
        return 10.0
    return max(10.0, value)

_TALLY_REQUEST_COOLDOWN = _read_tally_cooldown()

def send_request(xml_request: str, url: Optional[str] = None, timeout: int = 60) -> str:
    """Send request to Tally with cooldown period to prevent crashes"""
    target = (url or DEFAULT_URL).rstrip("/")
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "Connection": "close",  # Properly close connection after each request
    }

    logger.debug("Tally request URL=%s headers=%s xml=%s", target, headers, xml_request)
    
    # Serialize all Tally requests with global lock
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
        <SVCURRENTCOMPANY>{company_name}</SVCURRENTCOMPANY>
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
        if parent.lower() != "sales":
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
    Uses CHILDOF + BELONGSTO to filter at Tally level â€” much more efficient
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
            <NATIVEMETHOD>*</NATIVEMETHOD>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    resp = send_request(xml, url)
    return parse_ledgers_full(resp)


def get_ledgers(company_name: str, url: Optional[str] = None):
    """Fetch all ledgers with FULL details in one request (like get_stock_items)"""
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
        <SVCURRENTCOMPANY>{company_name}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" ISFIXED="No" ISINITIALIZE="Yes" ISOPTION="No" ISINTERNAL="No" NAME="Ledger">
            <TYPE>Ledger</TYPE>
            <NATIVEMETHOD>*</NATIVEMETHOD>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
    resp = send_request(xml, url)
    return parse_ledgers_full(resp)


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
                item_name = stock.get("NAME", "")
                if item_name:
                    data["NAME"] = item_name

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
            item_name = stock.get("NAME", "")
            if item_name:
                data["NAME"] = item_name

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
            <NATIVEMETHOD>*</NATIVEMETHOD>
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
        # Skip count-only <VOUCHER>N</VOUCHER> elements from <CMPINFO> â€” they have no child elements
        if not list(voucher):
            continue
        data = {}
        for attr_key, attr_val in voucher.attrib.items():
            if attr_key and attr_val:
                data[attr_key.upper()] = str(attr_val).strip()
        for child in voucher:
            if child.tag.endswith(".LIST"):
                continue
            key = child.tag.upper()
            if key.startswith("TEMP"):
                continue
            val = (child.text or "").strip()
            if val:
                data[key] = val

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
                    if child.tag.endswith(".LIST"):
                        continue
                    k = child.tag.upper()
                    if k.startswith("TEMP"):
                        continue
                    v = (child.text or "").strip()
                    if v:
                        item[k] = v

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

        # "Order No(s)" field â€” customer's PO / order number in Tally Prime
        # Tally stores this in SALESORDERDETAILS.LIST â†’ ORDERNO or PARTYORDERNO
        order_no = (
            voucher.findtext(".//PARTYORDERNO") or
            voucher.findtext(".//SALESORDERNO") or
            voucher.findtext(".//ORDERNO") or
            voucher.findtext(".//BASICPURCHASEORDERNO") or
            ""
        ).strip()
        # Also check inside SALESORDERDETAILS.LIST
        if not order_no:
            for sod in voucher.findall(".//SALESORDERDETAILS.LIST"):
                order_no = (sod.findtext("ORDERNO") or sod.findtext("PARTYORDERNO") or "").strip()
                if order_no:
                    break
        if order_no:
            data["PARTYORDERNO"] = order_no

        # "Order Date" field â€” PO date matching the Order No(s)
        order_date = (
            voucher.findtext(".//PARTYORDERDATE") or
            voucher.findtext(".//BASICORDERDATE") or
            voucher.findtext(".//ORDERDATE") or
            voucher.findtext(".//SALESORDERDATE") or
            ""
        ).strip()
        if not order_date:
            for sod in voucher.findall(".//SALESORDERDETAILS.LIST"):
                order_date = (sod.findtext("ORDERDATE") or sod.findtext("PARTYORDERDATE") or "").strip()
                if order_date:
                    break
        if order_date:
            data["PARTYORDERDATE"] = order_date

        # "Other References" field â€” free text reference (e.g. "Delivery")
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
                    if child.tag.endswith(".LIST"):
                        continue
                    k = child.tag.upper()
                    if k.startswith("TEMP"):
                        continue
                    v = (child.text or "").strip()
                    if v:
                        entry[k] = v
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
    # Use * to get ALL fields including nested lists like LEDGERMAILINGDETAILS.LIST
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
            <NATIVEMETHOD>*</NATIVEMETHOD>
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
                data["GSTIN"] = data.get("GSTIN", "") or data.get("PARTYGSTIN", "") or data.get("GSTREGISTRATION", "")
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
            data["GSTIN"] = data.get("GSTIN", "") or data.get("PARTYGSTIN", "") or data.get("GSTREGISTRATION", "")
            data["STATE"] = data.get("PRIORSTATENAME", "") or data.get("STATENAME", "") or data.get("LEDSTATENAME", "")
            data["COUNTRY"] = data.get("COUNTRYNAME", "") or data.get("COUNTRYOFRESIDENCE", "")
            data["PINCODE"] = data.get("PINCODE", "") or data.get("LEDGERPINCODE", "")

            ledgers.append(data)

    return ledgers


def get_stock_item_by_name(company_name: str, item_name: str, url: Optional[str] = None) -> Optional[Dict]:
    """Fetch a single stock item by name from Tally with all details including HSN and GST."""
    # Use * to get ALL fields including nested lists like HSNDETAILS.LIST and GSTDETAILS.LIST
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
        <SVCURRENTCOMPANY>{company_name}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" NAME="SingleStockItem">
            <TYPE>StockItem</TYPE>
            <FILTERS>ByName</FILTERS>
            <NATIVEMETHOD>*</NATIVEMETHOD>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="ByName">$NAME = "{item_name}"</SYSTEM>
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
    Fetch Delivery Notes from TallyPrime using the Day Book report export.

    TallyPrime's custom TDL Collection filters (SVFROMDATE/SVTODATE, $$IsInRange)
    are NOT executed via the HTTP API â€” Tally ignores them and dumps all vouchers,
    causing memory crashes on large datasets.

    The Day Book report is a built-in TallyPrime report that natively respects
    SVFROMDATE/SVTODATE, so Tally filters at the source before sending data.
    We then filter the result by voucher type to keep only delivery notes.
    """
    from xml.sax.saxutils import escape as xml_escape
    from datetime import datetime as _dt

    safe_company = xml_escape(company_name)
    def _to_tally_date(d: str) -> str:
        dt = _dt.strptime(d, "%Y%m%d")
        return f"{dt.day}-{dt.strftime('%b-%Y')}"
    _from_tally = _to_tally_date(from_date)
    _to_tally   = _to_tally_date(to_date)

    logger.info("get_delivery_notes called with from_date=%s (%s), to_date=%s (%s)",
                from_date, _from_tally, to_date, _to_tally)

    # Use Day Book report â€” TallyPrime's built-in date-aware report.
    # Unlike custom TDL collections, this DOES filter by SVFROMDATE/SVTODATE at source.
    xml = f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Day Book</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVFROMDATE>{_from_tally}</SVFROMDATE>
          <SVTODATE>{_to_tally}</SVTODATE>
          <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>"""

    # Voucher type keywords that identify delivery notes/challans
    _DC_KEYWORDS = ("delivery", "delivary", "challan", " dc", "dc ")

    def _is_delivery_note(voucher: dict) -> bool:
        vtype = (
            voucher.get("VCHTYPE") or
            voucher.get("VOUCHERTYPENAME") or ""
        ).lower()
        return any(kw in vtype for kw in _DC_KEYWORDS)

    try:
        logger.info("Sending Day Book request to Tally (date range: %s to %s)...", from_date, to_date)
        resp = send_request(xml, url, timeout=180)
        logger.info("Received Day Book response from Tally, parsing...")
        all_vouchers = parse_delivery_notes(resp)
        logger.info("Parsed %d total vouchers from Day Book", len(all_vouchers))

        # Filter to delivery notes only
        dc_vouchers = [v for v in all_vouchers if _is_delivery_note(v)]
        logger.info("Filtered to %d delivery notes (DC/Challan) from %d total vouchers",
                    len(dc_vouchers), len(all_vouchers))

        # Python safety filter
        date_filtered = [v for v in dc_vouchers if from_date <= (v.get("DATE") or "") <= to_date]
        extra = len(dc_vouchers) - len(date_filtered)

        if extra > 0:
            # Day Book did NOT filter by date on this Tally instance â€” it returned all DCs.
            # If total DCs returned is large (>50), this is crash-prone. Switch to chunked fetch.
            logger.warning(
                "Day Book ignored SVFROMDATE/SVTODATE â€” returned %d DCs, only %d are in %sâ€“%s. "
                "Tally is not filtering by date at source.",
                len(dc_vouchers), len(date_filtered), from_date, to_date,
            )
            if len(dc_vouchers) > 50:
                logger.warning(
                    "Large dataset (%d DCs) with no Tally-side date filtering â€” "
                    "crash risk on low-RAM systems. Consider upgrading TallyPrime.",
                    len(dc_vouchers),
                )

        logger.info("After date filter (%s to %s): %d delivery notes", from_date, to_date, len(date_filtered))
        return date_filtered

    except Exception as e:
        logger.error("Failed to fetch delivery notes: %s", e, exc_info=True)
        return []



def get_sales_invoices(company_name: str, url: Optional[str] = None, from_date: str = "20240101", to_date: str = "20991231"):
    """
    Fetch Sales Invoices from Tally company using Collection object export.

    Strategy:
      1. Fetch all active voucher types whose parent is Sales, then query each
         voucher type with exact match (supports custom sales voucher types).
      2. Fall back to CONTAINS "Invoice" and CONTAINS "Sales" filters.

    The old "Export Data / Voucher Register" approach is NOT used here because
    it returns a tabular report (not VOUCHER objects), so parse_delivery_notes
    cannot reliably parse voucher objects.
    """
    def _collection_xml(filter_formula: str) -> str:
        return f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>SalesVouchers</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{company_name}</SVCURRENTCOMPANY>
        <SVFROMDATE>{from_date}</SVFROMDATE>
        <SVTODATE>{to_date}</SVTODATE>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" NAME="SalesVouchers">
            <TYPE>Voucher</TYPE>
            <FILTERS>InvFilter</FILTERS>
            <FETCH>*</FETCH>
            <FETCH>INVENTORYENTRIES.STOCKITEMNAME</FETCH>
            <FETCH>INVENTORYENTRIES.RATE</FETCH>
            <FETCH>INVENTORYENTRIES.AMOUNT</FETCH>
            <FETCH>INVENTORYENTRIES.ACTUALQTY</FETCH>
            <FETCH>ALLINVENTORYENTRIES.STOCKITEMNAME</FETCH>
            <FETCH>ALLINVENTORYENTRIES.RATE</FETCH>
            <FETCH>ALLINVENTORYENTRIES.AMOUNT</FETCH>
            <FETCH>ALLINVENTORYENTRIES.ACTUALQTY</FETCH>
            <FETCH>LEDGERENTRIES.LEDGERNAME</FETCH>
            <FETCH>LEDGERENTRIES.AMOUNT</FETCH>
            <FETCH>LEDGERENTRIES.ISDEEMEDPOSITIVE</FETCH>
            <FETCH>ALLLEDGERENTRIES.LEDGERNAME</FETCH>
            <FETCH>ALLLEDGERENTRIES.AMOUNT</FETCH>
            <FETCH>ALLLEDGERENTRIES.ISDEEMEDPOSITIVE</FETCH>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="InvFilter">{filter_formula}</SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""

    def _dedupe_merge(base: List[Dict], new_rows: List[Dict]) -> List[Dict]:
        seen = {
            (
                (r.get("GUID") or "").strip(),
                (r.get("VOUCHERNUMBER") or "").strip(),
                (r.get("DATE") or "").strip(),
                (r.get("PARTYLEDGERNAME") or "").strip(),
            )
            for r in base
        }
        for r in new_rows:
            key = (
                (r.get("GUID") or "").strip(),
                (r.get("VOUCHERNUMBER") or "").strip(),
                (r.get("DATE") or "").strip(),
                (r.get("PARTYLEDGERNAME") or "").strip(),
            )
            if key in seen:
                continue
            seen.add(key)
            base.append(r)
        return base

    collected: List[Dict] = []

    # Method 1: dynamic exact match for all active Sales voucher types.
    # NOTE: Do NOT add $$IsBetween here. Tally Collection type=Voucher already
    # respects SVFROMDATE/SVTODATE set in STATICVARIABLES.
    dynamic_types: List[str] = []
    try:
        dynamic_types = get_sales_voucher_types(company_name, url)
        if dynamic_types:
            logger.info(
                "Detected %d active Sales voucher types for company '%s': %s",
                len(dynamic_types), company_name, ", ".join(dynamic_types)
            )
    except Exception as exc:
        logger.warning("Could not fetch dynamic Sales voucher types for '%s': %s", company_name, exc)

    # Safe fallback list if dynamic fetch fails/returns empty.
    if not dynamic_types:
        dynamic_types = ["Sales Invoice", "Tax Invoice", "Sales"]

    for voucher_type in dynamic_types:
        formula = f'$VoucherTypeName = "{voucher_type}"'
        try:
            resp = send_request(_collection_xml(formula), url)
            logger.debug("Sales invoices response (type=%s, company=%s): %s",
                         voucher_type, company_name, resp[:500])
            vouchers = parse_delivery_notes(resp)
            if vouchers:
                logger.info("Found %d invoices for company '%s' using exact type '%s'",
                            len(vouchers), company_name, voucher_type)
                collected = _dedupe_merge(collected, vouchers)
        except Exception as exc:
            logger.warning("Fetch failed for type '%s' company '%s': %s",
                           voucher_type, company_name, exc)

    # Method 2: CONTAINS fallback (catches edge/custom naming).
    for term in ("Invoice", "Sales"):
        formula = f'$VoucherTypeName CONTAINS "{term}"'
        try:
            resp = send_request(_collection_xml(formula), url)
            logger.debug("Sales invoices response (CONTAINS '%s', company=%s): %s",
                         term, company_name, resp[:500])
            vouchers = parse_delivery_notes(resp)
            if vouchers:
                logger.info("Found %d invoices for company '%s' using CONTAINS '%s'",
                            len(vouchers), company_name, term)
                collected = _dedupe_merge(collected, vouchers)
        except Exception as exc:
            logger.warning("CONTAINS '%s' fetch failed for company '%s': %s",
                           term, company_name, exc)

    if collected:
        logger.info("Returning %d merged invoices for company '%s'", len(collected), company_name)
        return collected

    logger.warning(
        "No invoices found for company '%s' between %s and %s. "
        "Check that Tally is running and the company name matches exactly.",
        company_name, from_date, to_date,
    )
    return []


# ============================================================================
# TallyClient Class Wrapper for Arasan Gas Middleware
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
        Fetch customers (Sundry Debtors + all sub-groups) from Tally company.
        Uses CHILDOF + BELONGSTO to fetch only Sundry Debtors at Tally XML level.

        Args:
            company_name: Tally company name

        Returns:
            List of customer dictionaries with normalized fields
        """
        ledgers = get_sundry_debtors(company_name, self.url)

        customers = []
        for ledger in ledgers:
            customer = {
                'guid': ledger.get("GUID", ""),
                'name': ledger.get("NAME", ""),
                'parent_group': ledger.get("PARENT", ""),
                'gstin': ledger.get("GSTIN", "") or ledger.get("PARTYGSTIN", "") or ledger.get("GSTREGISTRATION", ""),
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
                'rate': self._parse_rate(item.get("STANDARDPRICE", "0")),
                'gst_applicable': item.get("GSTAPPLICABLE", ""),
                'gst_rate': item.get("GST_RATE", 0.0),
                'igst_rate': item.get("IGST_RATE", 0.0),
                'cgst_rate': item.get("CGST_RATE", 0.0),
                'sgst_rate': item.get("SGST_RATE", 0.0),
                'description': item.get("PARENT", "")  # Use parent as description
            }
            products.append(product)

        return products
    
    def _parse_rate(self, rate_str: str) -> float:
        """Parse rate string to float.
        Handles Tally formats: "500.00", "-1000.00", "500.00/Cyl", "1,000.00/Nos"
        """
        try:
            rate_str = str(rate_str).replace(",", "").strip()
            # Tally RATE field is "amount/unit", e.g. "500.00/Cyl" â€” take only the numeric part
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
                    'quantity': self._parse_quantity(inv_item.get("ACTUALQTY", "0")),
                    'rate': self._parse_rate(inv_item.get("RATE", "0")),
                    'amount': self._parse_rate(inv_item.get("AMOUNT", "0"))
                }
                invoice['items'].append(item)
            
            # Calculate totals from ledger entries
            for ledger_entry in voucher.get("LEDGERENTRIES", []):
                amount_str = ledger_entry.get("AMOUNT", "0")
                amount = self._parse_rate(amount_str)

                ledger_name = ledger_entry.get("LEDGERNAME", "").upper()
                if any(tax in ledger_name for tax in ["CGST", "SGST", "IGST", "GST"]):
                    invoice['tax_amount'] += abs(amount)
                elif amount != 0:  # Non-zero non-tax entry â€” party debit (positive) or sales credit (negative)
                    candidate = abs(amount)
                    if candidate > invoice['total_amount']:
                        invoice['total_amount'] = candidate

            # Fallback: LEDGERENTRIES may be empty or all-tax; sum inventory item amounts
            if invoice['total_amount'] == 0.0 and invoice['items']:
                invoice['total_amount'] = sum(abs(item['amount']) for item in invoice['items'])

            invoices.append(invoice)
        
        return invoices
    
    def _parse_quantity(self, qty_str: str) -> float:
        """Parse quantity string to float"""
        try:
            # Remove units and commas
            qty_str = str(qty_str).split()[0].replace(",", "").strip()
            return float(qty_str)
        except (ValueError, TypeError, IndexError):
            return 0.0



