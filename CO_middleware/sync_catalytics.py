import argparse
from dataclasses import dataclass
import logging
import re
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
        SELECT dn.*, ss.is_synced, ss.attempts, ss.payload_hash
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

    cache_key = str(entity_id)
    if cache_key in _FILL_STATION_LIST_CACHE:
        return _FILL_STATION_LIST_CACHE[cache_key]

    url = _server_base_url(api_base_url) + "/master/gas_filling_station"
    try:
        resp = requests.get(
            url,
            params={"entity_id": entity_id, "limit_end": 500},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(
                "Could not fetch filling stations for entity %s: HTTP %s",
                entity_id,
                resp.status_code,
            )
            _FILL_STATION_LIST_CACHE[cache_key] = []
            return []

        payload = resp.json()
        stations = payload if isinstance(payload, list) else payload.get("data") or []
        if not isinstance(stations, list):
            stations = []
        _FILL_STATION_LIST_CACHE[cache_key] = stations
        return stations
    except Exception as exc:
        logger.warning("Could not fetch filling stations for entity %s: %s", entity_id, exc)
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


def _server_base_url(api_base_url: str) -> str:
    """Strip /import suffix to get the server root URL."""
    return api_base_url.rstrip("/").rsplit("/import", 1)[0]


def _lookup_dc_id_by_no(api_base_url: str, entity_id: Optional[int], dc_no: str) -> Optional[int]:
    """GET /transaction/delivery_challan?dc_no=... to find the DC's server ID."""
    if not dc_no:
        return None
    try:
        params: Dict[str, Any] = {"dc_no": dc_no}
        if entity_id:
            params["entity_id"] = entity_id
        url = _server_base_url(api_base_url) + "/transaction/delivery_challan"
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            items = data if isinstance(data, list) else data.get("data") or data.get("results") or []
            if isinstance(items, list) and items:
                dc_id = items[0].get("id")
                if dc_id:
                    return int(dc_id)
            logger.debug("DC id lookup: no results for dc_no=%s (response: %s)", dc_no, str(data)[:200])
    except Exception as exc:
        logger.debug("DC id lookup failed for dc_no=%s: %s", dc_no, exc)
    return None


def _update_dc_fill_station(api_base_url: str, dc_id: int, fill_station_id: str) -> bool:
    """Update fill_station on a DC via PATCH, PUT, or POST (tries in order)."""
    if not dc_id or not fill_station_id:
        return False
    try:
        fs_int = int(fill_station_id)
    except (ValueError, TypeError):
        return False
    try:
        url = _server_base_url(api_base_url) + f"/transaction/delivery_challan/{dc_id}"
        payload = {"id": dc_id, "fill_station": fs_int}
        headers = {"Content-Type": "application/json"}

        # Try PATCH first (partial update), then PUT, then POST
        for method in (requests.patch, requests.put, requests.post):
            resp = method(url, json=payload, headers=headers, timeout=15)
            if resp.status_code in (200, 201):
                logger.info("Fill station updated: DC server_id=%s fill_station=%s (via %s)", dc_id, fs_int, method.__name__.upper())
                return True
            if resp.status_code == 405:
                # Method not allowed — try next
                continue
            logger.warning("Fill station update %s HTTP %s for DC server_id=%s: %s",
                           method.__name__.upper(), resp.status_code, dc_id, resp.text[:300])
            return False

        logger.warning("Fill station update: all HTTP methods failed for DC server_id=%s", dc_id)
    except Exception as exc:
        logger.warning("Fill station update failed for DC server_id=%s: %s", dc_id, exc)
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

    # Build liquid product name map once for this sync run
    liquid_master_map, liquid_variant_map = _build_liquid_name_maps(config.master_db_path)
    if liquid_master_map or liquid_variant_map:
        logger.info(
            "Liquid product maps loaded: master=%d variant=%d",
            len(liquid_master_map),
            len(liquid_variant_map),
        )

    endpoint = config.api_base_url.rstrip("/") + "/tally-delivery-challan-payload/"
    # Payload endpoints use AllowAny permission â€" no auth header needed
    headers = {"Content-Type": "application/json"}

    total_sent = 0
    total_ok = 0
    total_fail = 0

    for note in notes:
        dc_no = _norm_dc_no(note.get("dc_no"))
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
        logger.info(
            "Syncing DC #%s | party=%s | date=%s | filling_station=%s | po=%s | items=%d | terms=%s",
            dc_no or "?",
            voucher.get("PARTYLEDGERNAME") or "?",
            voucher.get("DATE") or "?",
            voucher.get("FILLINGSTATION") or "[EMPTY]",
            voucher.get("PARTYORDERNO") or "[EMPTY]",
            len(voucher.get("INVENTORY") or []),
            voucher.get("TERMSOFDELIVERY") or "[EMPTY]",
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

                # Step 2: Always update fill_station after successful DC sync
                default_fs_id = _get_default_fill_station_id()
                if default_fs_id:
                    # Use server ID from response if available, otherwise look it up
                    if not _dc_server_id:
                        _dc_server_id = _lookup_dc_id_by_no(config.api_base_url, config.entity_id, resolved_dc_no or dc_no)
                    if _dc_server_id:
                        fs_ok = _update_dc_fill_station(config.api_base_url, _dc_server_id, default_fs_id)
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










