import argparse
from dataclasses import dataclass
import logging
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
          AND COALESCE(dn.is_deleted, 0) = 0
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
    default_fs = os.getenv("DEFAULT_FILLING_STATION", "").strip()

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
            # Send numeric ID so backend can look up the station directly
            if default_fs.isdigit():
                voucher["FILLINGSTATIONID"] = default_fs

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


def _build_liquid_name_map(master_db_path: str) -> Dict[str, str]:
    """
    Build a mapping of  liquid product_master_name -> canonical name
    from the local products table.

    e.g.  "LIQUID OXYGEN" -> "LIQUID OXYGEN 3000 Ltr"

    If multiple variants exist for the same master name, the most recently
    inserted one (highest id) is used — in practice there should be only one.
    """
    name_map: Dict[str, str] = {}
    if not master_db_path or not os.path.exists(master_db_path):
        return name_map
    try:
        mconn = sqlite3.connect(master_db_path, timeout=10)
        mconn.row_factory = sqlite3.Row
        rows = mconn.execute(
            """
            SELECT product_master_name, name_canonical
            FROM products
            WHERE lower(product_master_name) LIKE 'liquid%'
              AND name_canonical IS NOT NULL
              AND name_canonical != ''
            ORDER BY id ASC
            """
        ).fetchall()
        for row in rows:
            master = (row["product_master_name"] or "").strip()
            canonical = (row["name_canonical"] or "").strip()
            if master and canonical:
                # Later rows overwrite earlier ones (most recent variant wins)
                name_map[master.lower()] = canonical
        mconn.close()
    except Exception as exc:
        logger.warning("Could not build liquid name map from master DB: %s", exc)
    return name_map


def _remap_liquid_inventory_items(
    items: List[Dict[str, Any]],
    liquid_name_map: Dict[str, str],
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

        canonical = liquid_name_map.get(stock_name.lower())
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


def _build_payload_for_note(
    conn,
    note: Dict[str, Any],
    *,
    entity_id: Optional[int],
    company_name: Optional[str],
    allow_tally_fetch: bool,
    liquid_name_map: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Any], str]:
    voucher = db.json_loads(note["data_json"]) or {}
    items = _load_items(conn, note["id"])

    # Remap liquid product names to their canonical form before syncing
    if liquid_name_map:
        _remap_liquid_inventory_items(items, liquid_name_map)

    voucher["INVENTORY"] = items

    # Enrich voucher with all fields the backend expects
    _enrich_voucher(voucher, note, items)

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


def _fetch_deleted_unsynced(
    conn,
    *,
    company_id: Optional[int],
    limit: int,
    max_attempts: int,
) -> List[Dict[str, Any]]:
    """Fetch delivery notes that are deleted but not yet synced to Catalytics."""
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
        WHERE COALESCE(dn.is_deleted, 0) = 1
          AND COALESCE(ss.is_synced, 0) = 0
          AND COALESCE(ss.attempts, 0) < ?
          {where_company}
        ORDER BY dn.updated_at ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


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
    """POST /transaction/delivery_challan/<id> with fill_station to update."""
    if not dc_id or not fill_station_id:
        return False
    try:
        fs_int = int(fill_station_id)
    except (ValueError, TypeError):
        return False
    try:
        url = _server_base_url(api_base_url) + f"/transaction/delivery_challan/{dc_id}"
        resp = requests.post(url, json={"id": dc_id, "fill_station": fs_int},
                             headers={"Content-Type": "application/json"}, timeout=15)
        if resp.status_code in (200, 201):
            logger.info("Fill station updated: DC server_id=%s fill_station=%s", dc_id, fs_int)
            return True
        logger.warning("Fill station update HTTP %s for DC server_id=%s: %s",
                       resp.status_code, dc_id, resp.text[:200])
    except Exception as exc:
        logger.warning("Fill station update failed for DC server_id=%s: %s", dc_id, exc)
    return False


def _sync_deleted_dcs(
    conn,
    *,
    config: "SyncConfig",
    company_id: Optional[int],
    company_name: Optional[str],
) -> Dict[str, int]:
    """Sync deleted DC records to Catalytics delete endpoint."""
    deleted_notes = _fetch_deleted_unsynced(
        conn,
        company_id=company_id,
        limit=config.limit,
        max_attempts=config.max_attempts,
    )
    if not deleted_notes:
        return {"sent": 0, "ok": 0, "failed": 0}

    endpoint = config.api_base_url.rstrip("/") + "/tally-delivery-challan-delete/"
    # Payload endpoints use AllowAny permission â€" no auth header needed
    headers = {"Content-Type": "application/json"}

    total_sent = 0
    total_ok = 0
    total_fail = 0

    for i in range(0, len(deleted_notes), config.batch_size):
        batch = deleted_notes[i : i + config.batch_size]
        delete_items = []
        for note in batch:
            dc_no = note.get("dc_no") or ""
            delete_items.append({"dc_no": dc_no})

        batch_payload: Dict[str, Any] = {"delete_dcs": delete_items}
        if config.entity_id:
            batch_payload["entity_id"] = config.entity_id
        if company_name:
            batch_payload["company_name"] = company_name

        if config.dry_run:
            logger.info("Dry-run: would delete %d DCs", len(delete_items))
            continue

        try:
            resp = requests.post(endpoint, json=batch_payload, headers=headers, timeout=60)
            total_sent += len(delete_items)
        except Exception as exc:
            logger.exception("Delete API request failed")
            for note in batch:
                _update_sync_status(
                    conn,
                    delivery_note_id=note["id"],
                    success=False,
                    payload_hash="DELETED",
                    response_json=None,
                    error_text=str(exc),
                )
            conn.commit()
            total_fail += len(batch)
            continue

        response_json = None
        try:
            response_json = resp.json()
        except Exception:
            response_json = {"status": "error", "message": f"HTTP {resp.status_code} non-JSON"}

        results = (response_json.get("data") or {}).get("results") or []
        results_map = {
            _norm_dc_no(r.get("dc_no")): r
            for r in results
            if isinstance(r, dict) and r.get("dc_no")
        }

        for note in batch:
            dc_no = _norm_dc_no(note.get("dc_no"))
            res = results_map.get(dc_no) if dc_no else None
            status_val = (res or {}).get("status")
            success = status_val in ("deleted", "skipped")
            error_text = None
            if not success:
                error_text = (res or {}).get("message") or response_json.get("message") or "delete_sync_failed"
            _update_sync_status(
                conn,
                delivery_note_id=note["id"],
                success=success,
                payload_hash="DELETED",
                response_json=response_json,
                error_text=error_text,
            )
            if success:
                total_ok += 1
            else:
                total_fail += 1

        conn.commit()

    logger.info("DC delete sync complete. sent=%d ok=%d failed=%d", total_sent, total_ok, total_fail)
    return {"sent": total_sent, "ok": total_ok, "failed": total_fail}


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
    liquid_name_map = _build_liquid_name_map(config.master_db_path)
    if liquid_name_map:
        logger.info("Liquid product name map loaded: %s", liquid_name_map)

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
                entity_id=config.entity_id,
                company_name=company_name,
                allow_tally_fetch=config.allow_tally_fetch,
                liquid_name_map=liquid_name_map,
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
                results_list = (response_json.get("data") or {}).get("results") or []
                if isinstance(results_list, list):
                    for entry in results_list:
                        if isinstance(entry, dict) and entry.get("dc_no"):
                            resolved_dc_no = str(entry.get("dc_no")).strip()
                            break
                if not resolved_dc_no:
                    resolved_dc_no = str(voucher.get("VOUCHERNUMBER") or "").strip() or dc_no
                logger.info("SUCCESS DC #%s (%s) | party=%s", resolved_dc_no or dc_no, status_word, voucher.get("PARTYLEDGERNAME"))
                _update_sync_status(conn, delivery_note_id=note["id"], success=True,
                                    payload_hash=payload_hash, response_json=response_json, error_text=None)
                total_ok += 1

                # Step 2: Update fill_station (same as arasan) when Tally godown is default/main
                default_fs = os.getenv("DEFAULT_FILLING_STATION", "").strip()
                voucher_fs = str(voucher.get("FILLINGSTATION") or "").strip()
                _needs_fs_update = (
                    default_fs
                    and (not voucher_fs or voucher_fs.lower() in _TALLY_DEFAULT_GODOWNS_SET or voucher_fs == default_fs)
                )
                if _needs_fs_update:
                    _dc_server_id = _lookup_dc_id_by_no(config.api_base_url, config.entity_id, resolved_dc_no or dc_no)
                    if _dc_server_id:
                        _update_dc_fill_station(config.api_base_url, _dc_server_id, default_fs)
                    else:
                        logger.debug("Could not look up server DC id for fill_station update (dc_no=%s)", resolved_dc_no or dc_no)
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

    # --- Delete sync phase ---
    delete_stats = _sync_deleted_dcs(
        conn,
        config=config,
        company_id=company_id,
        company_name=company_name,
    )

    return {
        "sent": total_sent,
        "ok": total_ok,
        "failed": total_fail,
        "delete_sent": delete_stats.get("sent", 0),
        "delete_ok": delete_stats.get("ok", 0),
        "delete_failed": delete_stats.get("failed", 0),
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













