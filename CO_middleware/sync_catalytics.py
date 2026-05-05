import argparse
from dataclasses import dataclass
from datetime import datetime
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import os
import sys

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import sqlite3

import requests

import config as cfg
from config import BASE_DIR
import db
from logging_utils import setup_logging

DEFAULT_ENV_PATH = cfg.resolve_env_path(os.path.dirname(__file__))

logger = logging.getLogger("catalytics_sync")


def _attach_dc_sync_file_handler():
    """Log DC sync operations to a dedicated file."""
    log_path = BASE_DIR / 'logs' / 'dc_sync.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'dc_sync_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'dc_sync_file'
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


def _attach_dc_sync_error_handler():
    """Log DC sync errors to a dedicated file."""
    log_path = BASE_DIR / 'logs' / 'dc_sync_errors.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'dc_sync_error_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'dc_sync_error_file'
    fh.setLevel(logging.ERROR)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_dc_sync_file_handler()
_attach_dc_sync_error_handler()

def _ensure_dc_sync_log_files():
    for name in ('dc_sync.log', 'dc_sync_errors.log'):
        try:
            log_path = BASE_DIR / 'logs' / name
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if not log_path.exists():
                log_path.touch()
        except Exception:
            pass


@dataclass
class SyncConfig:
    db_path: str
    master_db_path: str          # SQLITE_DB_PATH — has the products table
    api_base_url: str
    api_key: Optional[str]
    entity_id: Optional[int]
    company: Optional[str]
    batch_size: int
    limit: int
    max_attempts: int
    allow_tally_fetch: bool
    enable_instant_dc_matching: bool
    dry_run: bool
    log_level: str
    log_json: bool
    log_file: Optional[str]


def _fetch_unsynced(
    conn,
    *,
    company_id: Optional[int],
    limit: int,
    max_attempts: int,
) -> List[Dict[str, Any]]:
    params: List[Any] = [max_attempts]
    where_company = ""
    if company_id:
        where_company = "AND dn.company_id = ?"
        params.append(company_id)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT dn.*, ss.is_synced, ss.attempts, ss.payload_hash,
               dn.is_instant, dn.matched_dc_id
        FROM delivery_notes dn
        LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id
        WHERE COALESCE(ss.is_synced, 0) = 0
          AND COALESCE(ss.attempts, 0) < ?
          {where_company}
        ORDER BY dn.updated_at ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_items(conn, delivery_note_id: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT data_json, stock_name FROM delivery_note_items WHERE delivery_note_id = ? ORDER BY line_no",
        (delivery_note_id,),
    ).fetchall()
    items = []
    for row in rows:
        data = db.json_loads(row["data_json"])
        if data:
            items.append(data)
    return items


def _load_ledger(conn, company_id: int, ledger_name: Optional[str]) -> Optional[Dict[str, Any]]:
    if not ledger_name:
        return None
    row = conn.execute(
        "SELECT data_json FROM ledgers WHERE company_id = ? AND lower(name) = lower(?)",
        (company_id, ledger_name),
    ).fetchone()
    return db.json_loads(row["data_json"]) if row else None


def _load_stock_items(conn, company_id: int, inventory_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    stock_map: Dict[str, Any] = {}
    for item in inventory_items:
        stock_name = item.get("STOCKITEMNAME") or item.get("ITEMNAME") or ""
        if not stock_name or stock_name in stock_map:
            continue
        row = conn.execute(
            "SELECT data_json FROM stock_items WHERE company_id = ? AND lower(name) = lower(?)",
            (company_id, stock_name),
        ).fetchone()
        if row:
            stock_map[stock_name] = db.json_loads(row["data_json"])
    return stock_map


_FILL_STATION_LIST_CACHE: Dict[str, List[Dict[str, Any]]] = {}
_INSTANT_DC_MATCH_DISABLED_UNTIL_TS: float = 0.0


def _forced_dc_id_for_no(dc_no: Optional[str]) -> Optional[int]:
    """
    Optional hard override from env for problematic duplicate cases.
    Format:
      FORCE_MATCHED_DC_MAP=4:176,ABC-12:991
    """
    key = str(dc_no or "").strip()
    if not key:
        return None
    raw = (os.getenv("FORCE_MATCHED_DC_MAP") or "").strip()
    if not raw:
        return None
    for token in raw.split(","):
        part = token.strip()
        if not part or ":" not in part:
            continue
        left, right = part.split(":", 1)
        if left.strip() == key:
            try:
                return int(right.strip())
            except (TypeError, ValueError):
                return None
    return None


def _normalize_station_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _get_default_fill_station_value() -> str:
    return (
        os.getenv("DEFAULT_FILLING_STATION_ID", "").strip()
        or os.getenv("DEFAULT_FILLING_STATION", "").strip()
    )


def _get_default_fill_station_id() -> str:
    explicit = os.getenv("DEFAULT_FILLING_STATION_ID", "").strip()
    if explicit:
        return explicit
    fallback = os.getenv("DEFAULT_FILLING_STATION", "").strip()
    return fallback if fallback.isdigit() else ""


def _get_default_admin_user_id() -> Optional[int]:
    raw = (os.getenv("DEFAULT_ADMIN_USER_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# Cache: fill station ID -> station name from backend
_FILL_STATION_NAME_CACHE: Dict[str, str] = {}


def _resolve_fill_station_name_by_id(
    api_base_url: str,
    entity_id: Optional[int],
    station_id: str,
) -> str:
    """Look up a fill station's name from the backend by its ID.

    The backend's DC payload handler reads GODOWNNAME and passes it to
    _get_or_create_filling_station(godown_name) which matches by *name*.
    So we must send the station name, not its numeric ID.
    """
    if not station_id:
        return ""
    if station_id in _FILL_STATION_NAME_CACHE:
        return _FILL_STATION_NAME_CACHE[station_id]

    stations = _get_entity_fill_stations(api_base_url, entity_id)
    for station in stations:
        sid = str(station.get("id") or "").strip()
        if sid == station_id:
            name = str(station.get("name") or "").strip()
            _FILL_STATION_NAME_CACHE[station_id] = name
            return name

    _FILL_STATION_NAME_CACHE[station_id] = ""
    return ""


def _extract_payload_fill_station_name(
    voucher: Dict[str, Any],
    items: List[Dict[str, Any]],
) -> str:
    for key in ("FILLINGSTATION", "FILLINGSTATIONNAME", "GODOWNNAME", "LOCATIONNAME", "LOCATION", "GODOWN"):
        value = str(voucher.get(key) or "").strip()
        if value:
            return value

    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("GODOWNNAME", "LOCATIONNAME", "LOCATION", "GODOWN"):
            value = str(item.get(key) or "").strip()
            if value:
                return value

    return ""


def _get_entity_fill_stations(api_base_url: str, entity_id: Optional[int]) -> List[Dict[str, Any]]:
    if not api_base_url or not entity_id:
        return []

    root_base = _server_base_url(api_base_url)
    for base in [root_base]:
        cache_key = f"{entity_id}|{base}"
        if cache_key in _FILL_STATION_LIST_CACHE:
            return _FILL_STATION_LIST_CACHE[cache_key]

        url = base + "/master/gas_filling_station"
        try:
            resp = requests.get(
                url,
                params={"entity_id": entity_id, "limit_end": 500},
                timeout=15,
            )
            if resp.status_code != 200:
                if resp.status_code == 404:
                    logger.debug(
                        "Fill-station endpoint not found for entity %s on %s",
                        entity_id,
                        base,
                    )
                else:
                    logger.warning(
                        "Could not fetch filling stations for entity %s on %s: HTTP %s",
                        entity_id,
                        base,
                        resp.status_code,
                    )
                _FILL_STATION_LIST_CACHE[cache_key] = []
                continue

            payload = resp.json()
            stations = payload if isinstance(payload, list) else payload.get("data") or []
            if not isinstance(stations, list):
                stations = []
            _FILL_STATION_LIST_CACHE[cache_key] = stations
            return stations
        except Exception as exc:
            logger.warning("Could not fetch filling stations for entity %s on %s: %s", entity_id, base, exc)
            _FILL_STATION_LIST_CACHE[cache_key] = []
    return []


def _match_backend_fill_station(
    api_base_url: str,
    entity_id: Optional[int],
    station_name: str,
) -> Optional[Dict[str, Any]]:
    normalized = _normalize_station_key(station_name)
    if not normalized:
        return None

    stations = _get_entity_fill_stations(api_base_url, entity_id)
    if not stations:
        return None

    for station in stations:
        name_key = _normalize_station_key(station.get("name"))
        code_key = _normalize_station_key(station.get("code"))
        if normalized == name_key or normalized == code_key:
            return station

    for station in stations:
        name_key = _normalize_station_key(station.get("name"))
        code_key = _normalize_station_key(station.get("code"))
        if name_key and (normalized in name_key or name_key in normalized):
            return station
        if code_key and (normalized in code_key or code_key in normalized):
            return station

    return None


def _apply_default_fill_station(
    voucher: Dict[str, Any],
    items: List[Dict[str, Any]],
    *,
    default_value: str,
    default_id: str,
) -> None:
    if default_value:
        voucher["FILLINGSTATION"] = default_value
    else:
        voucher.pop("FILLINGSTATION", None)

    if default_id:
        voucher["FILLINGSTATIONID"] = default_id
    else:
        voucher.pop("FILLINGSTATIONID", None)

    # The backend reads GODOWNNAME from inventory items to resolve fill station.
    # Set it on both voucher and items so the backend can find the station.
    godown_value = default_value or default_id
    if godown_value:
        voucher["GODOWNNAME"] = godown_value
        for item in items:
            if not isinstance(item, dict):
                continue
            item["GODOWNNAME"] = godown_value
            for key in ("LOCATIONNAME", "LOCATION", "GODOWN"):
                item.pop(key, None)
    else:
        for key in ("GODOWNNAME", "LOCATIONNAME", "FILLINGSTATIONNAME", "LOCATION", "GODOWN"):
            voucher.pop(key, None)
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("GODOWNNAME", "LOCATIONNAME", "LOCATION", "GODOWN"):
                item.pop(key, None)


def _apply_matched_fill_station(
    voucher: Dict[str, Any],
    items: List[Dict[str, Any]],
    station: Dict[str, Any],
) -> None:
    canonical_name = str(station.get("name") or station.get("code") or "").strip()
    station_id = str(station.get("id") or "").strip()
    if not canonical_name:
        return

    voucher["FILLINGSTATION"] = canonical_name
    voucher["GODOWNNAME"] = canonical_name
    if station_id:
        voucher["FILLINGSTATIONID"] = station_id

    for item in items:
        if not isinstance(item, dict):
            continue
        has_station_key = any(str(item.get(key) or "").strip() for key in ("GODOWNNAME", "LOCATIONNAME", "LOCATION", "GODOWN"))
        if has_station_key:
            item["GODOWNNAME"] = canonical_name
        for key in ("LOCATIONNAME", "LOCATION", "GODOWN"):
            item.pop(key, None)


def _resolve_fill_station_payload(
    voucher: Dict[str, Any],
    items: List[Dict[str, Any]],
    *,
    api_base_url: str,
    entity_id: Optional[int],
) -> None:
    station_name = _extract_payload_fill_station_name(voucher, items)
    default_value = _get_default_fill_station_value()
    default_id = _get_default_fill_station_id()

    # When default_value is a numeric ID (e.g. "23"), resolve it to the
    # actual station name from the backend.  The backend's payload handler
    # reads GODOWNNAME and matches by *name*, not by ID.
    if default_id and (not default_value or default_value == default_id):
        resolved_name = _resolve_fill_station_name_by_id(api_base_url, entity_id, default_id)
        if resolved_name:
            default_value = resolved_name

    if not station_name or station_name.lower() in _TALLY_DEFAULT_GODOWNS_SET:
        if default_value or default_id:
            _apply_default_fill_station(
                voucher,
                items,
                default_value=default_value,
                default_id=default_id,
            )
        return

    station = _match_backend_fill_station(api_base_url, entity_id, station_name)
    if station:
        _apply_matched_fill_station(voucher, items, station)
        logger.info(
            "Matched filling station '%s' to backend station '%s' (id=%s)",
            station_name,
            station.get("name"),
            station.get("id"),
        )
        return

    if default_value or default_id:
        logger.warning(
            "Filling station '%s' not matched for entity %s; using default station %s",
            station_name,
            entity_id,
            default_id or default_value,
        )
        _apply_default_fill_station(
            voucher,
            items,
            default_value=default_value,
            default_id=default_id,
        )


# ---------------------------------------------------------------------------
# Delivery-term resolution — identical to the one in fetch_invoices.py.
# Maps abbreviations / typos in OTHERREFERENCE to a standard term.
# Single-letter: d→delivery, c→customer pickup, s→supplier, t→traders
# ---------------------------------------------------------------------------
_REF_TERM_EXACT: Dict[str, str] = {
    'd': 'delivery',
    'c': 'customer pickup',
    's': 'supplier',
    't': 'traders',
    # dealer shorthand used by Tally users; map to traders
    'de': 'traders',
    'ds': 'traders',
    'del': 'delivery',
    'deliv': 'delivery',
    'cust': 'customer pickup',
    'sup': 'supplier',
    'supp': 'supplier',
    'tr': 'traders',
    'trd': 'traders',
}

_REF_TERM_CONTAINS: List[tuple] = [
    ('dealers pickup',   'dealers pickup'),
    ('dealer pickup',    'dealers pickup'),
    ('customer pik up',  'customer pickup'),
    ('customer pickup',  'customer pickup'),
    ('cust pickup',      'customer pickup'),
    ('pickup',           'customer pickup'),
    ('supplier',         'supplier'),
    ('traders',          'traders'),
    ('trader',           'traders'),
    ('delivery',         'delivery'),
]


def _resolve_ref_term(ref: str) -> str:
    """
    Resolve a raw OTHERREFERENCE value to a standard delivery term.
    Handles single-letter abbreviations, short forms, and full words.
    Returns '' if the value cannot be mapped.
    """
    t = ref.strip().lower()
    if not t:
        return ''
    if t in _REF_TERM_EXACT:
        return _REF_TERM_EXACT[t]
    for keyword, term in _REF_TERM_CONTAINS:
        if keyword in t:
            return term
    # Token-level match: handle repeated/spaced abbreviations like "d d", "D D"
    for token in t.split():
        if token in _REF_TERM_EXACT:
            return _REF_TERM_EXACT[token]
    return ''


_NON_PO_VALUES = {
    'delivery', 'invoice', 'sales', 'bill', 'challan', 'dc', 'dispatch',
    'shipment', 'yes', 'no', 'standard', 'normal', 'express',
    'not applicable', 'n/a', 'na', 'nil', 'none', '-',
    'customer pickup', 'customerpickup', 'pickup', 'self pickup', 'selfpickup', 'self',
    'dealer pickup', 'dealers pickup',
    # DC reference type keywords — delivery type indicators, not PO numbers
    'customer pik up', 'customerpikup', 'supplier', 'traders', 'trader',
    # Single-letter abbreviations used in OTHERREFERENCE
    'd', 'c', 's', 't',
    # Dealer shorthand abbreviations mapped to traders
    'de', 'ds',
    # Short-form abbreviations
    'del', 'deliv', 'cust', 'sup', 'supp', 'tr', 'trd',
}
_NON_PO_KEYWORDS = (
    'customer pickup', 'customerpickup', 'pickup', 'self pickup',
    'selfpickup', 'self', 'delivery', 'dispatch', 'challan',
    'dealer pickup', 'dealers pickup',
    # DC reference type keywords
    'customer pik up', 'supplier', 'traders',
)
_PO_FIELDS = [
    'PARTYORDERNO', 'AGGREMENTORDERNO',
    'ORDERREF', 'ORDERINGNO', 'REFNO', 'REFERENCE',
    'PONUMBER', 'BASICORDERREF', 'VOUCHERREFERENCE',
]
_PO_DATE_FIELDS = ['PARTYORDERDATE', 'AGGREMENTORDERDATE', 'ORDERDATE', 'PODATE', 'REFERENCEDATE']


def _extract_po_number(voucher: Dict[str, Any]) -> str:
    for field in _PO_FIELDS:
        value = (voucher.get(field) or "").strip()
        if not value:
            continue
        vl = value.lower()
        if vl in _NON_PO_VALUES:
            continue
        if any(k in vl for k in _NON_PO_KEYWORDS):
            continue
        if len(value) >= 1 and (any(c.isdigit() for c in value) or len(value) > 3):
            return value
    return ""


def _extract_po_date(voucher: Dict[str, Any]) -> str:
    for field in _PO_DATE_FIELDS:
        val = (voucher.get(field) or "").strip()
        if val:
            digits = ''.join(ch for ch in val if ch.isdigit())
            if len(digits) == 8:
                return digits[0:4] + '-' + digits[4:6] + '-' + digits[6:8]
            return val
    return ""


def _enrich_voucher(
    voucher: Dict[str, Any],
    note: Dict[str, Any],
    items: List[Dict[str, Any]],
) -> None:
    """Enrich voucher with fields the backend expects, matching Arasan patterns exactly."""
    # Ensure basic identity fields
    voucher.setdefault("VOUCHERNUMBER", note.get("dc_no") or "")
    voucher.setdefault("DATE", note.get("voucher_date") or "")
    voucher.setdefault("PARTYLEDGERNAME", note.get("party_ledger_name") or "")

    # Set backend audit ownership fields from default admin user.
    admin_user_id = _get_default_admin_user_id()
    if admin_user_id is not None:
        voucher.setdefault("created_by", admin_user_id)
        voucher.setdefault("modified_by", admin_user_id)

    # Use voucher/invoice date for creation timestamp fields.
    created_date = _normalize_date_yyyymmdd(voucher.get("DATE") or note.get("voucher_date"))
    if created_date:
        created_iso = f"{created_date[0:4]}-{created_date[4:6]}-{created_date[6:8]}"
        voucher.setdefault("created_at", created_iso)
        voucher.setdefault("created_on", created_iso)

    # ADDRESSES â€" billing address (from voucher data or party)
    if not voucher.get("ADDRESSES"):
        addr = (voucher.get("ADDRESS") or voucher.get("MAILINGNAME") or "").strip()
        if addr:
            voucher["ADDRESSES"] = [addr]

    # CONSIGNEE â€" delivery/ship-to address
    if not voucher.get("CONSIGNEE"):
        consignee_addr = (
            voucher.get("DELIVERYADDRESS") or
            voucher.get("SHIPPINGADDRESS") or
            voucher.get("CONSIGNEEADDRESS") or ""
        ).strip()
        if consignee_addr:
            voucher["CONSIGNEE"] = {"ADDRESS": consignee_addr}

    # FILLINGSTATION: voucher godown > item godown > env default
    # Tally fills GODOWNNAME with "Main Location" / "Main Godown" when no
    # specific location is selected — treat those as "not set" and fall
    # through to DEFAULT_FILLING_STATION so the server uses the correct ID.
    default_fs = _get_default_fill_station_value()
    default_fs_id = _get_default_fill_station_id()

    if not voucher.get("FILLINGSTATION"):
        for key in ("GODOWNNAME", "LOCATIONNAME"):
            val = (voucher.get(key) or "").strip()
            if val and val.lower() not in _TALLY_DEFAULT_GODOWNS_SET:
                voucher["FILLINGSTATION"] = val
                break
    if not voucher.get("FILLINGSTATION"):
        for item in items:
            godown = (item.get("GODOWNNAME") or "").strip()
            if godown and godown.lower() not in _TALLY_DEFAULT_GODOWNS_SET:
                voucher["FILLINGSTATION"] = godown
                break
    if not voucher.get("FILLINGSTATION"):
        if default_fs:
            voucher["FILLINGSTATION"] = default_fs
            if default_fs_id:
                voucher["FILLINGSTATIONID"] = default_fs_id

    # PO number
    po_number = _extract_po_number(voucher)
    if po_number:
        voucher["PARTYORDERNO"] = po_number
        voucher["PONUMBER"] = po_number
    else:
        voucher.pop("PARTYORDERNO", None)
        voucher.pop("PONUMBER", None)
        voucher.pop("BASICORDERREF", None)

    # PO date
    po_date = _extract_po_date(voucher) if po_number else ""
    if po_number and po_date:
        voucher["PARTYORDERDATE"] = po_date
        voucher["PODATE"] = po_date
    else:
        voucher.pop("PARTYORDERDATE", None)
        voucher.pop("PODATE", None)

    # TERMSOFDELIVERY — mandatory field, derived from reference fields.
    # Resolution order:
    #   1. Try OTHERREFERENCE first — supports abbreviations (d/c/s/t) and full words
    #   2. Fall back to combined reference text for full-word matching
    #   3. Default to "Delivery" if nothing matched
    _other_ref_raw = (voucher.get("OTHERREFERENCE") or "").strip()
    _resolved = _resolve_ref_term(_other_ref_raw)

    if not _resolved:
        # Fallback: check combined reference fields for full words
        all_refs = " ".join([
            str(voucher.get("BASICORDERREF") or ""),
            str(voucher.get("OTHERREFERENCE") or ""),
            str(voucher.get("TERMSOFDELIVERY") or ""),
            str(note.get("reference") or ""),
        ]).strip().lower()
        _resolved = _resolve_ref_term(all_refs) or ''

    _TERM_DISPLAY = {
        'dealers pickup':  'Dealers Pickup',
        'customer pickup': 'Customer Pickup',
        'supplier':        'Supplier',
        'traders':         'Traders',
        'delivery':        'Delivery',
    }
    if _resolved in _TERM_DISPLAY:
        voucher["TERMSOFDELIVERY"] = _TERM_DISPLAY[_resolved]
    elif not voucher.get("TERMSOFDELIVERY"):
        voucher["TERMSOFDELIVERY"] = "Delivery"

    # VEHICLENO — unified vehicle number for portal sync
    # Tally stores it in BASICSHIPPEDBY (→ DISPATCHEDTHROUGH) or GOODSVEHICLENUMBER (→ MOTORVEHICLENO)
    vehicle_no = (
        str(voucher.get("VEHICLENO") or "").strip() or
        str(voucher.get("DISPATCHEDTHROUGH") or "").strip() or
        str(voucher.get("MOTORVEHICLENO") or "").strip() or
        str(voucher.get("BASICSHIPPEDBY") or "").strip() or
        str(voucher.get("GOODSVEHICLENUMBER") or "").strip()
    )
    if vehicle_no:
        voucher["VEHICLENO"] = vehicle_no
        logger.info("Vehicle number set: %r for DC %s", vehicle_no, note.get("dc_no"))
    else:
        logger.info("No vehicle number found for DC %s (BASICSHIPPEDBY/DISPATCHEDTHROUGH/MOTORVEHICLENO all empty)", note.get("dc_no"))

    # INVENTORY â€" ensure items are attached
    if not voucher.get("INVENTORY"):
        voucher["INVENTORY"] = items

    # LEDGERENTRIES fallback
    if not voucher.get("LEDGERENTRIES"):
        party = voucher.get("PARTYLEDGERNAME") or ""
        amount = voucher.get("AMOUNT") or ""
        if party:
            voucher["LEDGERENTRIES"] = [{"LEDGERNAME": party, "AMOUNT": str(amount)}]


def _normalize_name_key(value: Any) -> str:
    return str(value or "").replace(" ", "").strip().lower()


def _customer_exists_in_db(conn, party_name: str) -> bool:
    """Check if a customer exists in the local SQLite customers table before syncing."""
    if not party_name:
        return True  # No party name — don't block the sync
    normalized = _normalize_name_key(party_name)
    row = conn.execute(
        "SELECT id FROM customers WHERE lower(replace(name, ' ', '')) = ?",
        (normalized,),
    ).fetchone()
    return row is not None


def _build_liquid_name_maps(master_db_path: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Build liquid canonical lookup maps from local products table:
      1) master map: product_master_name -> canonical
      2) variant map: product_master_name + variant_name -> canonical

    e.g.  "LIQUID OXYGEN" -> "LIQUID OXYGEN 3000 Ltr"

    If multiple variants exist for the same master name, the most recently
    inserted one (highest id) is used — in practice there should be only one.
    """
    master_map: Dict[str, str] = {}
    variant_map: Dict[str, str] = {}
    if not master_db_path or not os.path.exists(master_db_path):
        return master_map, variant_map
    try:
        mconn = sqlite3.connect(master_db_path, timeout=10)
        mconn.row_factory = sqlite3.Row
        rows = mconn.execute(
            """
            SELECT product_master_name, variant_name, name_canonical
            FROM products
            WHERE lower(product_master_name) LIKE 'liquid%'
              AND name_canonical IS NOT NULL
              AND name_canonical != ''
            ORDER BY id ASC
            """
        ).fetchall()
        for row in rows:
            master = (row["product_master_name"] or "").strip()
            variant = (row["variant_name"] or "").strip()
            canonical = (row["name_canonical"] or "").strip()
            if master and canonical:
                # Later rows overwrite earlier ones (most recent variant wins)
                master_map[master.lower()] = canonical
                if variant:
                    variant_map[_normalize_name_key(f"{master} {variant}")] = canonical
        mconn.close()
    except Exception as exc:
        logger.warning("Could not build liquid name maps from master DB: %s", exc)
    return master_map, variant_map


def _remap_liquid_inventory_items(
    items: List[Dict[str, Any]],
    liquid_master_map: Dict[str, str],
    liquid_variant_map: Dict[str, str],
) -> None:
    """
    For each inventory item whose STOCKITEMNAME starts with 'liquid':
      - Replace STOCKITEMNAME with the canonical product name from master DB
        (e.g. "LIQUID OXYGEN" -> "LIQUID OXYGEN 3000 Ltr (TNK)")
      - Always force BILLEDQTY and ACTUALQTY to 1 (tank is a physical asset)
    Mutates items in-place.
    """
    for item in items:
        stock_name = (item.get("STOCKITEMNAME") or item.get("ITEMNAME") or "").strip()
        if not stock_name.lower().startswith("liquid"):
            continue

        # Always force qty=1 for liquid/tank products — mandatory
        item["BILLEDQTY"] = "1"
        item["ACTUALQTY"] = "1"

        original_qty = (
            (item.get("ORIGINAL_BILLEDQTY") or item.get("ORIGINAL_ACTUALQTY") or "")
        ).strip()

        canonical = None
        if original_qty:
            canonical = liquid_variant_map.get(
                _normalize_name_key(f"{stock_name} {original_qty}")
            )
        if not canonical:
            canonical = liquid_master_map.get(stock_name.lower())
        if canonical and canonical != stock_name:
            logger.info(
                "[LIQUID REMAP] DC item '%s' -> '%s' (qty forced to 1)", stock_name, canonical
            )
            item["STOCKITEMNAME"] = canonical
            if "ITEMNAME" in item:
                item["ITEMNAME"] = canonical
        else:
            logger.info(
                "[LIQUID REMAP] DC item '%s' — qty forced to 1", stock_name
            )


_DA_GAS_PRODUCT_KEY = "dissolved acetylene gas (cyl)"


def _fix_da_gas_qty(items: List[Dict[str, Any]]) -> None:
    """
    For 'Dissolved Acetylene Gas (Cyl)' items, BILLEDQTY/ACTUALQTY in Tally is
    incorrect. The correct qty is stored in the item's NARRATION description field.
    Extracts the first number from NARRATION and sets it as the qty.
    Mutates items in-place.
    """
    for item in items:
        stock_name = (item.get("STOCKITEMNAME") or item.get("ITEMNAME") or "").strip()
        if stock_name.lower() != _DA_GAS_PRODUCT_KEY:
            continue
        narration = (item.get("NARRATION") or "").strip()
        if not narration:
            logger.warning("[DA GAS QTY] '%s' — no NARRATION found, keeping original qty", stock_name)
            continue
        match = re.search(r'\d+(?:\.\d+)?', narration)
        if match:
            qty_str = match.group(0)
            logger.info(
                "[DA GAS QTY] '%s' narration='%s' -> qty=%s (was BILLEDQTY=%s ACTUALQTY=%s)",
                stock_name, narration, qty_str,
                item.get("BILLEDQTY", ""), item.get("ACTUALQTY", ""),
            )
            item["BILLEDQTY"] = qty_str
            item["ACTUALQTY"] = qty_str
        else:
            logger.warning(
                "[DA GAS QTY] '%s' narration='%s' — no number found, keeping original qty",
                stock_name, narration,
            )


def _build_payload_for_note(
    conn,
    note: Dict[str, Any],
    *,
    api_base_url: str,
    entity_id: Optional[int],
    company_name: Optional[str],
    allow_tally_fetch: bool,
    liquid_master_map: Optional[Dict[str, str]] = None,
    liquid_variant_map: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Any], str]:
    voucher = db.json_loads(note["data_json"]) or {}
    items = _load_items(conn, note["id"])

    # Remap liquid product names to their canonical form before syncing
    if liquid_master_map is not None and liquid_variant_map is not None:
        _remap_liquid_inventory_items(items, liquid_master_map, liquid_variant_map)

    # Fix qty for Dissolved Acetylene Gas (Cyl) — correct qty is in NARRATION
    _fix_da_gas_qty(items)

    voucher["INVENTORY"] = items

    # Enrich voucher with all fields the backend expects
    _enrich_voucher(voucher, note, items)
    _resolve_fill_station_payload(
        voucher,
        items,
        api_base_url=api_base_url,
        entity_id=entity_id,
    )

    party_name = note.get("party_ledger_name") or voucher.get("PARTYLEDGERNAME") or voucher.get("PARTYNAME")
    ledger_data = _load_ledger(conn, note["company_id"], party_name)
    ledgers_map = {party_name: ledger_data} if ledger_data and party_name else {}

    stock_map = _load_stock_items(conn, note["company_id"], items)

    payload = {
        "voucher": voucher,
        "ledgers": ledgers_map,
        "stock_items": stock_map,
        "allow_tally_fetch": allow_tally_fetch,
    }

    if entity_id:
        payload["entity_id"] = entity_id
    if company_name:
        payload["company_name"] = company_name

    payload_hash = db.sha256_text(db.json_dumps(payload))
    return payload, payload_hash


def _norm_dc_no(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_date_yyyymmdd(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y%m%d")
        except Exception:
            continue
    return ""


def _safe_qty(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _build_item_qty_map(
    items: List[Dict[str, Any]],
    *,
    is_portal_item: bool = False,
) -> Dict[str, float]:
    product_map: Dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue

        if is_portal_item:
            product_obj = item.get("product")
            if isinstance(product_obj, dict):
                name = str(product_obj.get("name") or "").strip()
            else:
                name = str(item.get("product_name") or item.get("name") or "").strip()
            qty = _safe_qty(item.get("quantity") or item.get("qty"))
        else:
            name = str(item.get("STOCKITEMNAME") or item.get("ITEMNAME") or "").strip()
            qty = _safe_qty(item.get("BILLEDQTY") or item.get("ACTUALQTY") or item.get("QTY"))

        if not name or qty is None:
            continue

        key = _normalize_name_key(name)
        product_map[key] = product_map.get(key, 0.0) + qty

    return product_map


def _parse_instant_dc_results(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
        results = payload.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        if isinstance(results, dict):
            return [results]
    return []


def _build_optional_auth_headers(api_key: Optional[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _fetch_unsynced_instant_dcs(
    api_base_url: str,
    entity_id: Optional[int],
    api_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    global _INSTANT_DC_MATCH_DISABLED_UNTIL_TS
    now_ts = time.time()
    if _INSTANT_DC_MATCH_DISABLED_UNTIL_TS > now_ts:
        remaining = int(_INSTANT_DC_MATCH_DISABLED_UNTIL_TS - now_ts)
        logger.info(
            "Instant DC matching temporarily disabled due to previous backend 500 (cooldown %ss)",
            max(1, remaining),
        )
        return []

    logger.info("Fetching unsynced Instant DCs from portal for matching")
    params: Dict[str, Any] = {}
    # entity_id is NOT a query param — backend filters by auth/session context
    headers = _build_optional_auth_headers(api_key)

    last_error = None
    for base in [_server_base_url(api_base_url)]:
        path = "/transaction/delivery_challan/instant/unsynced"
        try:
            url = base + path
            response = requests.get(url, params=params, headers=headers, timeout=20)
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                if response.status_code >= 500:
                    _INSTANT_DC_MATCH_DISABLED_UNTIL_TS = time.time() + 900  # 15 minutes
                    logger.warning(
                        "Instant DC endpoint returned HTTP %s; disabling Instant DC matching for 15 minutes",
                        response.status_code,
                    )
                    try:
                        logger.warning("Unsynced Instant DC fetch response: %s", response.text[:800])
                    except Exception:
                        pass
                    return []
                if response.status_code == 404:
                    logger.debug("Instant DC endpoint not found on %s%s", base, path)
                else:
                    logger.warning(
                        "Unsynced Instant DC fetch failed on %s%s: HTTP %s",
                        base,
                        path,
                        response.status_code,
                    )
                try:
                    if response.status_code != 404:
                        logger.warning("Unsynced Instant DC fetch response: %s", response.text[:800])
                except Exception:
                    pass
                continue

            basic_results = _parse_instant_dc_results(response.json())
            if not basic_results:
                return []

            needs_detail_fetch = bool(basic_results and not basic_results[0].get("dc_date"))
            if not needs_detail_fetch:
                return basic_results

            logger.info("Instant DC list missing dc_date; fetching per-DC details")
            full_results: List[Dict[str, Any]] = []
            for basic in basic_results:
                dc_id = basic.get("id")
                if not dc_id:
                    full_results.append(basic)
                    continue

                detail_path = f"/transaction/delivery_challan/{dc_id}"
                detail_found = False
                try:
                    detail_url = base + detail_path
                    detail_resp = requests.get(detail_url, headers=headers, timeout=20)
                    if detail_resp.status_code == 200:
                        parsed_detail = _parse_instant_dc_results(detail_resp.json())
                        full_results.append(parsed_detail[0] if parsed_detail else basic)
                        detail_found = True
                except Exception:
                    pass
                if not detail_found:
                    full_results.append(basic)
            return full_results
        except Exception as exc:
            last_error = str(exc)
            logger.warning("Unsynced Instant DC fetch error on %s%s: %s", base, path, exc)

    if last_error:
        logger.warning("Failed to fetch unsynced Instant DCs after trying all bases: %s", last_error)
    return []


def _find_matching_instant_dc(
    note: Dict[str, Any],
    voucher: Dict[str, Any],
    items: List[Dict[str, Any]],
    instant_dcs: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not instant_dcs:
        return None

    invoice_date = _normalize_date_yyyymmdd(note.get("voucher_date") or voucher.get("DATE"))
    if not invoice_date:
        return None

    customer_name = (
        note.get("party_ledger_name")
        or voucher.get("PARTYLEDGERNAME")
        or voucher.get("PARTYNAME")
        or ""
    )
    customer_name_norm = _normalize_name_key(customer_name)
    if not customer_name_norm:
        return None

    invoice_products = _build_item_qty_map(items, is_portal_item=False)
    if not invoice_products:
        logger.info("Instant DC match: Tally voucher has no parseable products; skipping match")
        return None

    try:
        invoice_dt = datetime.strptime(invoice_date, "%Y%m%d")
    except Exception:
        return None

    logger.info(
        "Instant DC match: Tally dc_no=? date=%s customer=%r products=%s",
        invoice_date, customer_name_norm, list(invoice_products.keys()),
    )

    # Step 1: collect all DCs that pass date + customer filter
    candidates: List[Dict[str, Any]] = []
    for dc in instant_dcs:
        dc_id = dc.get("id")
        if not dc_id:
            continue
        if dc.get("is_instant_dc") is False:
            logger.info("  DC id=%s skipped: is_instant_dc=False", dc_id)
            continue
        if dc.get("dc_synced") is True:
            logger.info("  DC id=%s skipped: dc_synced=True", dc_id)
            continue

        dc_date_norm = _normalize_date_yyyymmdd(dc.get("dc_date") or dc.get("date"))
        if not dc_date_norm:
            logger.info("  DC id=%s skipped: no dc_date", dc_id)
            continue

        try:
            dc_dt = datetime.strptime(dc_date_norm, "%Y%m%d")
            delta = (invoice_dt - dc_dt).days
            if delta < 0 or delta > 7:
                logger.info("  DC id=%s skipped: date delta=%d (dc=%s tally=%s)", dc_id, delta, dc_date_norm, invoice_date)
                continue
        except Exception:
            if dc_date_norm != invoice_date:
                logger.info("  DC id=%s skipped: date mismatch (%s vs %s)", dc_id, dc_date_norm, invoice_date)
                continue

        customer_obj = dc.get("customer")
        if isinstance(customer_obj, dict):
            dc_customer_norm = _normalize_name_key(customer_obj.get("name"))
        else:
            dc_customer_norm = _normalize_name_key(dc.get("customer_name"))
        if dc_customer_norm != customer_name_norm:
            logger.info("  DC id=%s skipped: customer mismatch (portal=%r tally=%r)", dc_id, dc_customer_norm, customer_name_norm)
            continue

        logger.info("  DC id=%s passed date+customer filter (dc_date=%s customer=%r)", dc_id, dc_date_norm, dc_customer_norm)
        candidates.append(dc)

    if not candidates:
        return None

    # Step 2: try to narrow down by product name + quantity match
    def _score_dc(dc: Dict[str, Any]) -> int:
        """Return match score: 2=full qty match, 1=name-only match, 0=no product data."""
        dc_items = dc.get("order_details") or dc.get("items") or []
        dc_products = _build_item_qty_map(dc_items, is_portal_item=True)

        if dc_products:
            # Full product+quantity check
            if len(dc_products) != len(invoice_products):
                return -1  # product count mismatch — eliminate
            for tally_key, expected_qty in invoice_products.items():
                got_qty = dc_products.get(tally_key)
                if got_qty is None:
                    candidates_qty = [
                        qty for pk, qty in dc_products.items()
                        if pk.startswith(tally_key) or tally_key.startswith(pk)
                    ]
                    got_qty = candidates_qty[0] if len(candidates_qty) == 1 else None
                if got_qty is None or abs(got_qty - expected_qty) >= 1e-6:
                    return -1  # qty mismatch — eliminate
            return 2  # full match

        if dc_items:
            # Items exist but no quantity field — try name-only match
            dc_names: set = set()
            for it in dc_items:
                if isinstance(it, dict):
                    pobj = it.get("product")
                    name = (isinstance(pobj, dict) and pobj.get("name")) or it.get("product_name") or it.get("name") or ""
                elif isinstance(it, str):
                    name = it
                else:
                    name = ""
                if name:
                    dc_names.add(_normalize_name_key(str(name)))
            tally_names = set(invoice_products.keys())
            has_overlap = bool(dc_names & tally_names) or any(
                any(pk.startswith(tk) or tk.startswith(pk) for pk in dc_names)
                for tk in tally_names
            )
            if has_overlap:
                return 1  # name-only match
            return -1  # names don't match either — eliminate

        # No items at all — can't verify products, treat as weak match
        return 0

    scored = [(dc, _score_dc(dc)) for dc in candidates]
    logger.info(
        "  Candidate scores: %s",
        [(dc.get("id"), score) for dc, score in scored],
    )

    # Eliminate any DC with score -1 (definite mismatch)
    viable = [(dc, score) for dc, score in scored if score >= 0]
    if not viable:
        # All candidates were eliminated by product check — fall back to all candidates
        logger.info("  All candidates eliminated by product check; falling back to best date+customer match")
        viable = [(dc, 0) for dc in candidates]

    # Pick the highest-scoring candidate; ties broken by id (prefer higher/latest)
    viable.sort(key=lambda x: (x[1], x[0].get("id") or 0), reverse=True)
    best_dc, best_score = viable[0]
    logger.info("  Selected DC id=%s with score=%d", best_dc.get("id"), best_score)
    return best_dc


def _mark_instant_dc_synced_on_portal(
    api_base_url: str,
    dc_pk: Any,
    tally_voucher_no: Optional[str] = None,
    api_key: Optional[str] = None,
) -> bool:
    """Mark matched instant DC as synced on the portal."""
    dc_id = str(dc_pk or "").strip()
    if not dc_id:
        return False

    payload: Dict[str, Any] = {}
    if tally_voucher_no:
        payload["tally_voucher_no"] = str(tally_voucher_no).strip()
    headers = _build_optional_auth_headers(api_key)

    endpoint_paths = [f"/transaction/delivery_challan/instant/{dc_id}/mark-synced"]

    for base in [_server_base_url(api_base_url)]:
        for path in endpoint_paths:
            try:
                url = base + path
                resp = requests.post(url, json=payload, headers=headers, timeout=20)
                if resp.status_code == 200:
                    return True
                logger.warning(
                    "Instant DC mark-synced failed for id=%s on %s%s: HTTP %s",
                    dc_id, base, path, resp.status_code
                )
            except Exception as exc:
                logger.warning(
                    "Instant DC mark-synced error for id=%s on %s%s: %s",
                    dc_id, base, path, exc
                )
    return False


def _update_sync_status(
    conn,
    *,
    delivery_note_id: int,
    success: bool,
    payload_hash: str,
    response_json: Optional[Dict[str, Any]],
    error_text: Optional[str],
) -> None:
    ts = db.now_ts()
    conn.execute(
        """
        INSERT INTO sync_status
            (delivery_note_id, is_synced, attempts, last_attempt_at, synced_at,
             last_error, last_response_json, payload_hash, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(delivery_note_id) DO UPDATE SET
            is_synced = excluded.is_synced,
            attempts = sync_status.attempts + 1,
            last_attempt_at = excluded.last_attempt_at,
            synced_at = excluded.synced_at,
            last_error = excluded.last_error,
            last_response_json = excluded.last_response_json,
            payload_hash = excluded.payload_hash,
            updated_at = excluded.updated_at
        """,
        (
            delivery_note_id,
            1 if success else 0,
            ts,
            ts if success else None,
            error_text,
            db.json_dumps(response_json) if response_json else None,
            payload_hash,
            ts,
            ts,
        ),
    )



_TALLY_DEFAULT_GODOWNS_SET = {'main location', 'main godown', 'main', 'not applicable', 'n/a'}


def _api_base_candidates(api_base_url: str) -> List[str]:
    """Return candidate API bases supporting both '/import' and root forms."""
    base = (api_base_url or "").rstrip("/")
    if not base:
        return []

    candidates: List[str] = [base]
    if base.endswith("/import"):
        candidates.append(base[: -len("/import")])
    else:
        candidates.append(base + "/import")

    # Deduplicate while preserving order
    seen = set()
    ordered: List[str] = []
    for item in candidates:
        norm = item.rstrip("/")
        if not norm or norm in seen:
            continue
        seen.add(norm)
        ordered.append(norm)
    return ordered


def _server_base_url(api_base_url: str) -> str:
    """Strip /import suffix to get the server root URL."""
    return api_base_url.rstrip("/").rsplit("/import", 1)[0]


def _import_base_url(api_base_url: str) -> str:
    """Return base URL guaranteed to end with /import."""
    base = (api_base_url or "").rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/import") else (base + "/import")


def _lookup_dc_id_by_no(
    api_base_url: str,
    entity_id: Optional[int],
    dc_no: str,
    api_key: Optional[str] = None,
    expected_date: Optional[str] = None,
    expected_customer: Optional[str] = None,
) -> Optional[int]:
    """GET /transaction/delivery_challan?dc_no=... to find the DC's server ID."""
    if not dc_no:
        return None
    forced = _forced_dc_id_for_no(dc_no)
    if forced:
        logger.info("Using forced MATCHED_DC_ID for dc_no=%s -> id=%s", dc_no, forced)
        return forced
    params: Dict[str, Any] = {"dc_no": dc_no}
    # entity_id is NOT a query param on DeliveryChallan model — sending it causes Django FieldError 500
    headers = _build_optional_auth_headers(api_key)
    for base in [_server_base_url(api_base_url)]:
        try:
            url = base + "/transaction/delivery_challan"
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("data") or data.get("results") or []
                if isinstance(items, list) and items:
                    # Prefer exact dc_no entries first.
                    exact = []
                    dc_no_norm = str(dc_no).strip()
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        if str(it.get("dc_no") or "").strip() == dc_no_norm:
                            exact.append(it)
                    candidates = exact or [it for it in items if isinstance(it, dict)]

                    expected_date_norm = _normalize_date_yyyymmdd(expected_date or "")
                    expected_customer_norm = _normalize_name_key(expected_customer or "")

                    # Rank candidates:
                    # 1) exact customer+date match
                    # 2) exact customer match
                    # 3) is_instant_dc=True and dc_synced=False
                    # 4) is_instant_dc=True
                    # 5) latest/highest id fallback
                    best = None
                    best_rank = (-1, -1, -1)
                    for it in candidates:
                        try:
                            _id = int(it.get("id"))
                        except (TypeError, ValueError):
                            continue
                        is_instant = bool(it.get("is_instant_dc") is True)
                        not_synced = bool(it.get("dc_synced") is False)
                        item_customer = _normalize_name_key(
                            (it.get("customer_name") or ((it.get("customer") or {}).get("name") if isinstance(it.get("customer"), dict) else "")) or ""
                        )
                        item_date = _normalize_date_yyyymmdd(it.get("dc_date") or it.get("date") or "")

                        customer_date_match = int(
                            bool(expected_customer_norm and expected_date_norm and item_customer == expected_customer_norm and item_date == expected_date_norm)
                        )
                        customer_match = int(bool(expected_customer_norm and item_customer == expected_customer_norm))
                        instant_rank = 2 if (is_instant and not_synced) else (1 if is_instant else 0)
                        rank = (customer_date_match, customer_match + instant_rank, _id)
                        if rank > best_rank:
                            best_rank = rank
                            best = _id
                    if best is not None:
                        return best
                logger.debug("DC id lookup: no results for dc_no=%s (response: %s)", dc_no, str(data)[:200])
        except Exception as exc:
            logger.debug("DC id lookup failed for dc_no=%s on %s: %s", dc_no, base, exc)
    return None


def _update_dc_fill_station(
    api_base_url: str,
    dc_id: int,
    fill_station_id: str,
    api_key: Optional[str] = None,
) -> bool:
    """Update fill_station on a DC via PATCH, PUT, or POST (tries in order)."""
    if not dc_id or not fill_station_id:
        return False
    try:
        fs_int = int(fill_station_id)
    except (ValueError, TypeError):
        return False
    payload = {"id": dc_id, "fill_station": fs_int}
    headers = {"Content-Type": "application/json"}
    headers.update(_build_optional_auth_headers(api_key))
    for base in [_server_base_url(api_base_url)]:
        try:
            url = base + f"/transaction/delivery_challan/{dc_id}"
            # Try PATCH first (partial update), then PUT, then POST
            for method in (requests.patch, requests.put, requests.post):
                resp = method(url, json=payload, headers=headers, timeout=15)
                if resp.status_code in (200, 201):
                    logger.info(
                        "Fill station updated: DC server_id=%s fill_station=%s (via %s %s)",
                        dc_id, fs_int, method.__name__.upper(), base
                    )
                    return True
                if resp.status_code == 405:
                    continue
                logger.warning(
                    "Fill station update %s HTTP %s for DC server_id=%s on %s: %s",
                    method.__name__.upper(), resp.status_code, dc_id, base, resp.text[:300]
                )
        except Exception as exc:
            logger.warning("Fill station update failed for DC server_id=%s on %s: %s", dc_id, base, exc)
    logger.warning("Fill station update: all API base candidates failed for DC server_id=%s", dc_id)
    return False



def build_config(args: argparse.Namespace) -> SyncConfig:
    env_path = getattr(args, "config", None) or DEFAULT_ENV_PATH
    cfg.load_env_file(env_path)
    _master_db = cfg.config.SQLITE_DB_PATH or ""
    if _master_db and not os.path.isabs(_master_db):
        _master_db = str(BASE_DIR / _master_db)
    return SyncConfig(
        db_path=args.db_path or cfg.get_env("TALLY_DB_PATH") or "",
        master_db_path=_master_db,
        api_base_url=args.api_base_url or cfg.get_env("CATALYTICS_API_BASE_URL") or "",
        api_key=args.api_key or cfg.get_env("CATALYTICS_API_KEY"),
        entity_id=args.entity_id or cfg.get_env_int("CATALYTICS_ENTITY_ID"),
        company=args.company or cfg.get_env("TALLY_COMPANY"),
        batch_size=args.batch_size or cfg.get_env_int("SYNC_BATCH_SIZE", 10) or 10,
        limit=args.limit or cfg.get_env_int("SYNC_LIMIT", 200) or 200,
        max_attempts=args.max_attempts or cfg.get_env_int("SYNC_MAX_ATTEMPTS", 5) or 5,
        allow_tally_fetch=bool(args.allow_tally_fetch) or cfg.get_env_bool("SYNC_ALLOW_TALLY_FETCH", False),
        enable_instant_dc_matching=cfg.get_env_bool("ENABLE_INSTANT_DC_MATCHING", True),
        dry_run=bool(args.dry_run) or cfg.get_env_bool("SYNC_DRY_RUN", False),
        log_level=args.log_level or cfg.get_env("LOG_LEVEL", "INFO"),
        log_json=bool(args.log_json) or cfg.get_env_bool("LOG_JSON", False),
        log_file=args.log_file or cfg.get_env("LOG_FILE"),
    )


def run_once(config: SyncConfig) -> Dict[str, int]:
    _ensure_dc_sync_log_files()
    setup_logging(level=config.log_level, json_output=config.log_json, file_path=config.log_file)


    if not config.db_path or not config.api_base_url:
        raise ValueError("db_path and api_base_url are required")

    conn = db.connect(config.db_path)
    db.init_db(conn)

    company_id = None
    company_name = config.company
    if config.company:
        row = conn.execute("SELECT id, name, entity_id FROM companies WHERE name = ?", (config.company,)).fetchone()
        if row:
            company_id = int(row["id"])
            if not config.entity_id and row["entity_id"]:
                config.entity_id = int(row["entity_id"])

    notes = _fetch_unsynced(conn, company_id=company_id, limit=config.limit, max_attempts=config.max_attempts)
    if not notes:
        logger.info("No unsynced delivery notes found")
        return {"sent": 0, "ok": 0, "failed": 0}

    instant_dcs: List[Dict[str, Any]] = []
    if config.enable_instant_dc_matching:
        instant_dcs = _fetch_unsynced_instant_dcs(config.api_base_url, config.entity_id, config.api_key)
        logger.info("Retrieved %d unsynced Instant DCs for matching", len(instant_dcs))
    else:
        logger.info("Instant DC matching disabled (ENABLE_INSTANT_DC_MATCHING=false)")

    # Build liquid product name map once for this sync run
    liquid_master_map, liquid_variant_map = _build_liquid_name_maps(config.master_db_path)
    if liquid_master_map or liquid_variant_map:
        logger.info(
            "Liquid product maps loaded: master=%d variant=%d",
            len(liquid_master_map),
            len(liquid_variant_map),
        )

    endpoint = _import_base_url(config.api_base_url) + "/tally-delivery-challan-payload/"
    # Payload endpoints use AllowAny permission â€" no auth header needed
    headers = {"Content-Type": "application/json"}

    total_sent = 0
    total_ok = 0
    total_fail = 0

    for note in notes:
        dc_no = _norm_dc_no(note.get("dc_no"))
        # Restore previously persisted matched_dc_id if sync failed on a prior attempt
        matched_dc_id: Optional[int] = note.get("matched_dc_id") or None
        try:
            payload, payload_hash = _build_payload_for_note(
                conn,
                note,
                api_base_url=config.api_base_url,
                entity_id=config.entity_id,
                company_name=company_name,
                allow_tally_fetch=config.allow_tally_fetch,
                liquid_master_map=liquid_master_map,
                liquid_variant_map=liquid_variant_map,
            )
        except Exception as exc:
            logger.exception("Failed to build payload for DC id=%s dc_no=%s", note.get("id"), dc_no)
            _update_sync_status(
                conn,
                delivery_note_id=note["id"],
                success=False,
                payload_hash="",
                response_json=None,
                error_text=f"payload_build_error: {exc}",
            )
            conn.commit()
            total_fail += 1
            continue

        voucher = payload["voucher"]
        items = voucher.get("INVENTORY") or []
        tally_voucher_no = str(voucher.get("VOUCHERNUMBER") or dc_no).strip()

        # Pre-sync customer validation (like BOL middleware)
        party_name = note.get("party_ledger_name") or voucher.get("PARTYLEDGERNAME") or ""
        if not _customer_exists_in_db(conn, party_name):
            logger.warning(
                "[SKIP:MISSING_CUSTOMER] DC #%s | customer='%s' not in local SQLite — skipping sync",
                dc_no or "?", party_name,
            )
            _update_sync_status(
                conn,
                delivery_note_id=note["id"],
                success=False,
                payload_hash=payload_hash,
                response_json=None,
                error_text=f"customer_not_found: '{party_name}' not in local DB",
            )
            conn.commit()
            total_fail += 1
            continue

        if matched_dc_id:
            # Previously persisted match from a failed prior attempt — reuse it
            voucher["MATCHED_DC_ID"] = matched_dc_id
            logger.info("Reusing persisted MATCHED_DC_ID=%s for dc_no=%s", matched_dc_id, dc_no or "?")
            instant_dcs = [dc for dc in instant_dcs if dc.get("id") != matched_dc_id]
        else:
            matched_dc = _find_matching_instant_dc(note, voucher, items, instant_dcs)
            if matched_dc and matched_dc.get("id"):
                matched_dc_id = matched_dc.get("id")
                voucher["MATCHED_DC_ID"] = matched_dc_id
                logger.info("Matched Instant DC for %s -> portal DC %s (id=%s)",
                            dc_no or "?", matched_dc.get("dc_no"), matched_dc_id)
                # Persist matched DC ID immediately so it survives a failed sync (like BOL middleware)
                db.update_delivery_note_matched_dc(conn, delivery_note_id=note["id"], matched_dc_id=matched_dc_id)
                instant_dcs = [dc for dc in instant_dcs if dc.get("id") != matched_dc_id]

        if not matched_dc_id and dc_no:
            logger.info("No instant match for dc_no=%s; trying dc_no lookup fallback", dc_no)
            # Fallback: if instant-list endpoint is unavailable, attempt direct dc_no lookup
            # so payload can update existing DC instead of creating duplicate.
            existing_dc_id = _lookup_dc_id_by_no(
                config.api_base_url,
                config.entity_id,
                dc_no,
                config.api_key,
                expected_date=note.get("voucher_date") or voucher.get("DATE"),
                expected_customer=note.get("party_ledger_name") or voucher.get("PARTYLEDGERNAME") or voucher.get("PARTYNAME"),
            )
            if existing_dc_id:
                matched_dc_id = existing_dc_id
                voucher["MATCHED_DC_ID"] = existing_dc_id
                logger.info("Matched existing portal DC by dc_no=%s (id=%s)", dc_no, existing_dc_id)
            else:
                logger.info("No existing portal DC found by dc_no=%s; payload may create new DC", dc_no)

        if matched_dc_id:
            logger.info("Using MATCHED_DC_ID=%s for dc_no=%s", matched_dc_id, dc_no or "?")
        else:
            logger.info("MATCHED_DC_ID not set for dc_no=%s", dc_no or "?")

        logger.info(
            "Syncing DC #%s | party=%s | date=%s | filling_station=%s | po=%s | items=%d | terms=%s | instant=%s",
            dc_no or "?",
            voucher.get("PARTYLEDGERNAME") or "?",
            voucher.get("DATE") or "?",
            voucher.get("FILLINGSTATION") or "[EMPTY]",
            voucher.get("PARTYORDERNO") or "[EMPTY]",
            len(voucher.get("INVENTORY") or []),
            voucher.get("TERMSOFDELIVERY") or "[EMPTY]",
            bool(note.get("is_instant")),
        )

        # Send single voucher per request â€" matching Arasan's sync_invoices_to_dc pattern
        request_payload = {
            "entity_id": config.entity_id,
            "company_name": company_name,
            "voucher": voucher,
            "ledgers": payload.get("ledgers") or {},
            "stock_items": payload.get("stock_items") or {},
            "allow_tally_fetch": config.allow_tally_fetch,
        }

        if config.dry_run:
            logger.info("Dry-run: would send DC #%s", dc_no)
            _update_sync_status(
                conn,
                delivery_note_id=note["id"],
                success=False,
                payload_hash=payload_hash,
                response_json={"dry_run": True},
                error_text="dry_run",
            )
            conn.commit()
            continue

        try:
            resp = requests.post(endpoint, json=request_payload, headers=headers, timeout=60)
            total_sent += 1
        except Exception as exc:
            logger.exception("API request failed for DC #%s", dc_no)
            _update_sync_status(
                conn,
                delivery_note_id=note["id"],
                success=False,
                payload_hash=payload_hash,
                response_json=None,
                error_text=str(exc),
            )
            conn.commit()
            total_fail += 1
            continue

        response_json = None
        try:
            response_json = resp.json()
        except Exception:
            response_json = {"status": "error", "message": resp.text[:500]}

        if resp.status_code in (200, 201) and response_json.get("status") == "success":
            data = response_json.get("data", {})
            created = data.get("created", 0)
            updated = data.get("updated", 0)
            errors = data.get("errors", 0)

            if errors > 0:
                results_list = data.get("results", [{}])
                first = results_list[0] if results_list else {}
                error_msg = first.get("message") or first.get("error_details") or "API error"
                logger.error("Sync error DC #%s: %s", dc_no, error_msg)
                _update_sync_status(conn, delivery_note_id=note["id"], success=False,
                                    payload_hash=payload_hash, response_json=response_json, error_text=error_msg)
                total_fail += 1
            elif created == 0 and updated == 0:
                error_msg = "DC not created or updated"
                logger.error("Sync failed DC #%s: %s", dc_no, error_msg)
                _update_sync_status(conn, delivery_note_id=note["id"], success=False,
                                    payload_hash=payload_hash, response_json=response_json, error_text=error_msg)
                total_fail += 1
            else:
                status_word = "created" if created else "updated"

                resolved_dc_no = dc_no
                _dc_server_id = None
                results_list = (response_json.get("data") or {}).get("results") or []
                if isinstance(results_list, list):
                    for entry in results_list:
                        if isinstance(entry, dict):
                            if entry.get("dc_no"):
                                resolved_dc_no = str(entry.get("dc_no")).strip()
                            # Extract server-side DC id from response to avoid extra lookup
                            if entry.get("id"):
                                try:
                                    _dc_server_id = int(entry["id"])
                                except (ValueError, TypeError):
                                    pass
                            break
                if not resolved_dc_no:
                    resolved_dc_no = str(voucher.get("VOUCHERNUMBER") or "").strip() or dc_no
                logger.info("SUCCESS DC #%s (%s) | party=%s", resolved_dc_no or dc_no, status_word, voucher.get("PARTYLEDGERNAME"))
                _update_sync_status(conn, delivery_note_id=note["id"], success=True,
                                    payload_hash=payload_hash, response_json=response_json, error_text=None)
                total_ok += 1

                if matched_dc_id:
                    mark_ok = _mark_instant_dc_synced_on_portal(
                        config.api_base_url,
                        matched_dc_id,
                        tally_voucher_no=tally_voucher_no,
                        api_key=config.api_key,
                    )
                    if mark_ok:
                        logger.info("Marked Instant DC as synced on portal (id=%s)", matched_dc_id)
                    else:
                        logger.warning("Could not mark Instant DC as synced on portal (id=%s)", matched_dc_id)

                # Step 2: Always update fill_station after successful DC sync
                default_fs_id = _get_default_fill_station_id()
                if default_fs_id:
                    # Use server ID from response if available, otherwise look it up
                    if not _dc_server_id:
                        _dc_server_id = _lookup_dc_id_by_no(
                            config.api_base_url,
                            config.entity_id,
                            resolved_dc_no or dc_no,
                            config.api_key,
                        )
                    if _dc_server_id:
                        fs_ok = _update_dc_fill_station(
                            config.api_base_url,
                            _dc_server_id,
                            default_fs_id,
                            config.api_key,
                        )
                        if not fs_ok:
                            logger.warning("Fill station update failed for DC #%s (server_id=%s, fill_station=%s)", resolved_dc_no or dc_no, _dc_server_id, default_fs_id)
                    else:
                        logger.warning("Could not determine server DC id for fill_station update (dc_no=%s)", resolved_dc_no or dc_no)
        else:
            error_msg = response_json.get("message") or f"HTTP {resp.status_code}"
            logger.error("Sync failed DC #%s: %s", dc_no, error_msg)
            _update_sync_status(conn, delivery_note_id=note["id"], success=False,
                                payload_hash=payload_hash, response_json=response_json, error_text=error_msg)
            total_fail += 1

        conn.commit()

    logger.info(
        "Sync complete. sent=%d ok=%d failed=%d",
        total_sent,
        total_ok,
        total_fail,
    )

    return {
        "sent": total_sent,
        "ok": total_ok,
        "failed": total_fail,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync SQLite-staged DC payloads to Catalytics.")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--api-base-url", help="Catalytics base URL (e.g. http://localhost:8000)")
    parser.add_argument("--api-key", help="API key for Catalytics (X-API-Key)")
    parser.add_argument("--entity-id", type=int, help="Catalytics entity id")
    parser.add_argument("--company", help="Company name for payload fallback")
    parser.add_argument("--batch-size", type=int, default=10, help="Number of vouchers per API call")
    parser.add_argument("--limit", type=int, default=200, help="Max vouchers per run")
    parser.add_argument("--max-attempts", type=int, default=5, help="Max retry attempts per DC")
    parser.add_argument("--allow-tally-fetch", action="store_true", help="Allow API to fetch Tally data")
    parser.add_argument("--dry-run", action="store_true", help="Build payloads but do not send")
    parser.add_argument("--log-level", help="Logging level")
    parser.add_argument("--log-json", action="store_true", help="JSON log output")
    parser.add_argument("--log-file", help="Log file path")
    args = parser.parse_args()

    config = build_config(args)
    try:
        run_once(config)
    except Exception:
        logger.exception("Sync run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
