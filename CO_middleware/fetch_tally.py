import argparse
from dataclasses import dataclass
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import os
import requests
import sys

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config as cfg
import db
import tally_api
from logging_utils import setup_logging

DEFAULT_ENV_PATH = cfg.resolve_env_path(os.path.dirname(__file__))

logger = logging.getLogger("tally_fetcher")


@dataclass
class FetchConfig:
    db_path: str
    tally_url: str
    company: str
    entity_id: Optional[int]
    from_date: Optional[str]
    to_date: Optional[str]
    days_back: Optional[int]
    fetch_stock: bool
    dry_run: bool
    log_level: str
    log_json: bool
    log_file: Optional[str]


def _normalize_dc_no(voucher: Dict[str, Any]) -> str:
    for key in (
        "VOUCHERNUMBER",
        "VOUCHERNO",
        "VCHNUMBER",
        "VCHNO",
        "NUMBER",
        "VOUCHERID",
        "VOUCHERKEY",
        "MASTERID",
        "REMOTEID",
    ):
        value = voucher.get(key)
        if value:
            return str(value).strip()
    return ""


def _extract_tally_guid(voucher: Dict[str, Any]) -> str:
    for key in (
        "GUID",
        "MASTERID",
        "REMOTEID",
        "REMOTEGUID",
        "REMOTEALTGUID",
        "VCHGUID",
        "VOUCHERGUID",
    ):
        value = voucher.get(key)
        if value:
            return str(value).strip()
    return ""


DEFAULT_DC_PAST_DAYS = 1
DEFAULT_DC_FUTURE_DAYS = 1


def _default_date_range(days_back: Optional[int] = None) -> Tuple[str, str]:
    now = datetime.now()
    # Dynamic 3-day window: yesterday to tomorrow.
    # Tally Day Book only returns data for the currently open date.
    # By covering yesterday+today+tomorrow, whichever date is open in
    # Tally will match and its DCs will be fetched.
    # Keep this fixed at 3 days only; do not expand into older daybooks.
    from_dt = now - timedelta(days=DEFAULT_DC_PAST_DAYS)
    to_dt = now + timedelta(days=DEFAULT_DC_FUTURE_DAYS)
    return from_dt.strftime("%Y%m%d"), to_dt.strftime("%Y%m%d")


def _build_payload_hash(
    voucher: Dict[str, Any],
    inventory_items: List[Dict[str, Any]],
    party_name: Optional[str],
    ledger_data: Optional[Dict[str, Any]],
    stock_items_map: Dict[str, Any],
) -> str:
    voucher_copy = dict(voucher)
    voucher_copy["INVENTORY"] = inventory_items
    ledger_key = party_name or (ledger_data or {}).get("NAME") or "PARTY"
    payload = {
        "voucher": voucher_copy,
        "ledgers": {ledger_key: ledger_data} if ledger_data else {},
        "stock_items": stock_items_map or {},
    }
    return db.sha256_text(db.json_dumps(payload))


def _is_instant_voucher(voucher: Dict[str, Any]) -> bool:
    """Return True if the DC appears to be an Instant DC based on Tally fields."""
    vtype = str(voucher.get("VOUCHERTYPENAME") or voucher.get("VOUCHERTYPE") or "").strip().lower()
    if "instant" in vtype:
        return True
    other_ref = str(voucher.get("BASICORDERREF") or voucher.get("OTHERREFERENCE") or "").strip().lower()
    if "instant" in other_ref:
        return True
    return False


def _normalize_name_key(value: str) -> str:
    return str(value or "").replace(" ", "").strip().lower()


def _sync_customer_now(
    *,
    api_base_url: str,
    entity_id: Optional[int],
    company_name: str,
    ledger: Dict[str, Any],
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Try immediate customer sync; returns (ok, response_json, error_msg)."""
    if not api_base_url:
        return False, None, "CATALYTICS_API_BASE_URL not set"
    _base = api_base_url.rstrip('/')
    endpoint = (_base + '/tally-customer-payload/') if _base.endswith('/import') else (_base + '/import/tally-customer-payload/')
    payload = {"entity_id": entity_id, "ledger": ledger}
    if company_name:
        payload["company_name"] = company_name
    try:
        resp = requests.post(endpoint, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
        if resp.status_code not in (200, 201):
            return False, None, f"HTTP {resp.status_code} - {resp.text[:200]}"
        result = resp.json()
        response_json = json.dumps(result)
        if result.get("status") != "success":
            return False, response_json, f"API non-success: {result.get('message')}"
        data = result.get("data", {}) or {}
        if data.get("errors", 0) > 0:
            return False, response_json, "API returned customer errors"
        if data.get("created", 0) == 0 and data.get("updated", 0) == 0:
            return False, response_json, "Customer not created/updated"
        return True, response_json, None
    except Exception as exc:
        return False, None, str(exc)


def build_config(args: argparse.Namespace) -> FetchConfig:
    env_path = getattr(args, "config", None) or DEFAULT_ENV_PATH
    cfg.load_env_file(env_path)
    return FetchConfig(
        db_path=args.db_path or cfg.get_env("TALLY_DB_PATH") or "",
        tally_url=args.tally_url or cfg.get_env("TALLY_URL", "http://localhost:9000/"),
        company=args.company or cfg.get_env("TALLY_COMPANY") or "",
        entity_id=args.entity_id or cfg.get_env_int("CATALYTICS_ENTITY_ID"),
        from_date=args.from_date or cfg.get_env("TALLY_FROM_DATE"),
        to_date=args.to_date or cfg.get_env("TALLY_TO_DATE"),
        days_back=args.days_back if args.days_back is not None else cfg.get_env_int("TALLY_DAYS_BACK"),
        fetch_stock=bool(args.fetch_stock) or cfg.get_env_bool("TALLY_FETCH_STOCK", False),
        dry_run=bool(args.dry_run) or cfg.get_env_bool("TALLY_DRY_RUN", False),
        log_level=args.log_level or cfg.get_env("LOG_LEVEL", "INFO"),
        log_json=bool(args.log_json) or cfg.get_env_bool("LOG_JSON", False),
        log_file=args.log_file or cfg.get_env("LOG_FILE"),
    )


def run_once(config: FetchConfig) -> Dict[str, int]:
    setup_logging(level=config.log_level, json_output=config.log_json, file_path=config.log_file)

    if not config.db_path or not config.company:
        raise ValueError("db_path and company are required")

    conn = db.connect(config.db_path)
    db.init_db(conn)

    # Always use the fixed 3-day window: yesterday / today / tomorrow.
    # Tally Day Book only returns data for the currently open date, so this
    # window ensures whichever date is open in Tally will be covered.
    # config.from_date / config.to_date overrides are intentionally ignored
    # to prevent accidental historical DC fetches.
    from_date, to_date = _default_date_range()

    logger.info("Using fixed 3-day window: %s to %s (yesterday / today / tomorrow)", from_date, to_date)
    companies = tally_api.get_companies(config.tally_url)
    available = [c.get("name") for c in companies]
    company_match = None
    for comp in companies:
        if (comp.get("name") or "").strip().lower() == config.company.strip().lower():
            company_match = comp
            break
    if not company_match:
        logger.error("Company '%s' not found in Tally. Available: %s", config.company, available)
        return {"created": 0, "updated": 0, "skipped": 0}

    company_name = company_match.get("name") or config.company
    company_id = db.ensure_company(
        conn,
        name=company_name,
        tally_name=company_name,
        entity_id=config.entity_id,
        tally_url=config.tally_url,
    )

    vouchers = tally_api.get_delivery_notes(company_name, config.tally_url, from_date, to_date)
    logger.info("Fetched %d delivery notes from Tally", len(vouchers))
    customer_api_url = cfg.get_env("CATALYTICS_API_BASE_URL", "") or ""

    created = 0
    updated = 0
    skipped = 0
    ledgers_fetched = 0
    stock_items_fetched = 0

    for voucher in vouchers:
        tally_guid = _extract_tally_guid(voucher)
        dc_no = _normalize_dc_no(voucher)
        if not dc_no:
            skipped += 1
            continue

        # Only process DCs that have OTHERREFERENCE set.
        # PONUMBER / REFERENCE / VOUCHERREFERENCE are NOT accepted as substitutes.
        ref_value = (voucher.get("OTHERREFERENCE") or voucher.get("BASICORDERREF") or "").strip()
        if not ref_value:
            skipped += 1
            logger.info("Skipping DC %s: OTHERREFERENCE is empty", dc_no)
            continue

        existing_json = None
        if tally_guid:
            existing_by_guid = conn.execute(
                "SELECT id, dc_no, data_json FROM delivery_notes WHERE company_id = ? AND tally_guid = ?",
                (company_id, tally_guid),
            ).fetchone()
            if existing_by_guid:
                existing_json = existing_by_guid["data_json"]
                existing_dc_no = (existing_by_guid["dc_no"] or "").strip()
                if existing_dc_no and existing_dc_no != dc_no:
                    logger.warning(
                        "[GUID MATCH] DC GUID %s has db_no=%s, tally_no=%s - using db_no for update",
                        tally_guid, existing_dc_no, dc_no,
                    )
                    dc_no = existing_dc_no

        if existing_json is None:
            existing = conn.execute(
                "SELECT data_json FROM delivery_notes WHERE company_id = ? AND dc_no = ?",
                (company_id, dc_no),
            ).fetchone()
            existing_json = existing["data_json"] if existing else None

        voucher_date = voucher.get("DATE") or ""
        party_name = voucher.get("PARTYLEDGERNAME") or voucher.get("PARTYNAME") or ""
        reference = ref_value

        # DC-driven customer creation: if party does not exist in local DB, create it from voucher data.
        normalized_party = _normalize_name_key(party_name)
        if normalized_party:
            exists = conn.execute(
                "SELECT id FROM customers WHERE lower(replace(name, ' ', '')) = ?",
                (normalized_party,),
            ).fetchone()
            if not exists:
                cust_gstin = (voucher.get("PARTYGSTIN") or voucher.get("CONSIGNEEGSTIN") or "").strip()
                cust_pan = (voucher.get("BUYERPINNUMBER") or "").strip()
                cust_state = (voucher.get("STATENAME") or voucher.get("CONSIGNEESTATENAME") or "").strip()
                cust_pincode = (voucher.get("PARTYPINCODE") or voucher.get("CONSIGNEEPINCODE") or "").strip()
                cust_country = (voucher.get("COUNTRYOFRESIDENCE") or "").strip()

                cust_phone = ""
                cust_email = ""
                cust_address = ""
                addr_lines = voucher.get("ADDRESSES") or []
                if isinstance(addr_lines, list):
                    addr_parts: List[str] = []
                    for line in addr_lines:
                        text = str(line).strip()
                        low = text.lower()
                        if low.startswith("phone:"):
                            cust_phone = text[6:].strip()
                        elif low.startswith("email:"):
                            cust_email = text[6:].strip()
                        elif text:
                            addr_parts.append(text)
                    cust_address = ", ".join(addr_parts)

                consignee = voucher.get("CONSIGNEE") or {}
                delivery_addresses = []
                if isinstance(consignee, dict) and (consignee.get("ADDRESS") or consignee.get("NAME")):
                    delivery_addresses.append({
                        "name": consignee.get("NAME", ""),
                        "address": consignee.get("ADDRESS", ""),
                        "state": consignee.get("STATE", ""),
                        "country": cust_country or "India",
                        "pincode": consignee.get("PINCODE", ""),
                        "gstin": consignee.get("GSTIN", ""),
                    })

                customer_data_json = json.dumps({
                    "NAME": party_name,
                    "PARTYGSTIN": cust_gstin,
                    "INCOMETAXNUMBER": cust_pan,
                    "STATE": cust_state,
                    "PINCODE": cust_pincode,
                    "MOBILE": cust_phone,
                    "EMAIL": cust_email,
                    "COUNTRY": cust_country,
                    "_source": "dc_voucher",
                    "_dc_no": dc_no,
                })
                try:
                    cur = conn.execute(
                        """
                        INSERT INTO customers (
                            tally_guid, name, tally_company, gstin, pan,
                            address, state, city, pincode, phone, email,
                            delivery_addresses_json, data_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "",
                            party_name,
                            company_name,
                            cust_gstin,
                            cust_pan,
                            cust_address,
                            cust_state,
                            "",
                            cust_pincode,
                            cust_phone,
                            cust_email,
                            json.dumps(delivery_addresses) if delivery_addresses else None,
                            customer_data_json,
                        ),
                    )
                    customer_id = int(cur.lastrowid)
                    logger.info(
                        "[DC-DRIVEN NEW CUSTOMER] '%s' created from DC %s (GSTIN: %s, state: %s, pincode: %s)",
                        party_name,
                        dc_no,
                        cust_gstin or "N/A",
                        cust_state or "N/A",
                        cust_pincode or "N/A",
                    )

                    ledger_for_sync = {
                        "NAME": party_name,
                        "PARTYGSTIN": cust_gstin,
                        "GSTIN": cust_gstin,
                        "INCOMETAXNUMBER": cust_pan,
                        "STATENAME": cust_state,
                        "STATE": cust_state,
                        "PINCODE": cust_pincode,
                        "MOBILE": cust_phone,
                        "EMAIL": cust_email,
                        "PRIMARY_ADDRESS": cust_address,
                        "COUNTRY": cust_country,
                    }
                    if delivery_addresses:
                        ledger_for_sync["DELIVERY_ADDRESSES"] = delivery_addresses
                    ok, response_json, err_msg = _sync_customer_now(
                        api_base_url=customer_api_url,
                        entity_id=config.entity_id,
                        company_name=company_name,
                        ledger=ledger_for_sync,
                    )
                    if ok:
                        conn.execute(
                            """
                            UPDATE customers
                            SET is_synced = 1,
                                last_response_json = ?,
                                last_sync_at = CURRENT_TIMESTAMP,
                                last_sync_error = NULL
                            WHERE id = ?
                            """,
                            (response_json, customer_id),
                        )
                        logger.info("[DC-DRIVEN NEW CUSTOMER] '%s' synced to server", party_name)
                    else:
                        conn.execute(
                            """
                            UPDATE customers
                            SET sync_attempts = sync_attempts + 1,
                                last_sync_error = ?,
                                last_response_json = ?,
                                last_sync_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (err_msg or "sync failed", response_json, customer_id),
                        )
                        logger.warning(
                            "[DC-DRIVEN NEW CUSTOMER] '%s' local only; server sync failed: %s",
                            party_name,
                            err_msg or "unknown error",
                        )
                except Exception as exc:
                    logger.error(
                        "[DC-DRIVEN] Failed to create customer '%s' from DC %s: %s",
                        party_name,
                        dc_no,
                        exc,
                    )

        is_instant = _is_instant_voucher(voucher)
        dn_id = db.upsert_delivery_note(
            conn,
            company_id=company_id,
            dc_no=dc_no,
            voucher_date=voucher_date,
            party_ledger_name=party_name,
            tally_guid=tally_guid,
            reference=reference,
            data=voucher,
            is_instant=is_instant,
        )

        inventory_items = voucher.get("INVENTORY") or []
        db.replace_delivery_note_items(conn, delivery_note_id=dn_id, items=inventory_items)

        ledger_data = None
        if party_name:
            ledger_data = tally_api.get_ledger_by_name(company_name, party_name, config.tally_url)
            if ledger_data:
                db.upsert_json_row(
                    conn,
                    table="ledgers",
                    company_id=company_id,
                    name=party_name,
                    data=ledger_data,
                )
                ledgers_fetched += 1

        stock_items_map: Dict[str, Any] = {}
        if config.fetch_stock:
            for item in inventory_items:
                stock_name = item.get("STOCKITEMNAME") or item.get("ITEMNAME") or ""
                if not stock_name or stock_name in stock_items_map:
                    continue
                stock_data = tally_api.get_stock_item_by_name(company_name, stock_name, config.tally_url)
                if stock_data:
                    stock_items_map[stock_name] = stock_data
                    db.upsert_json_row(
                        conn,
                        table="stock_items",
                        company_id=company_id,
                        name=stock_name,
                        data=stock_data,
                    )
                    stock_items_fetched += 1

        # If we didn't fetch stock items, try to use any existing cached stock data for hashing
        if not config.fetch_stock and inventory_items:
            for item in inventory_items:
                stock_name = item.get("STOCKITEMNAME") or item.get("ITEMNAME") or ""
                if not stock_name:
                    continue
                row = conn.execute(
                    "SELECT data_json FROM stock_items WHERE company_id = ? AND lower(name) = lower(?)",
                    (company_id, stock_name),
                ).fetchone()
                if row:
                    stock_items_map[stock_name] = db.json_loads(row["data_json"])

        payload_hash = _build_payload_hash(voucher, inventory_items, party_name, ledger_data, stock_items_map)
        existing_hash = None
        if dn_id:
            hash_row = conn.execute(
                "SELECT payload_hash FROM sync_status WHERE delivery_note_id = ?",
                (dn_id,),
            ).fetchone()
            existing_hash = hash_row["payload_hash"] if hash_row else None

        is_changed = (existing_json != db.json_dumps(voucher)) or (existing_hash != payload_hash)
        if existing_json is None:
            created += 1
        elif is_changed:
            updated += 1

        if is_changed or existing_hash is None:
            db.ensure_sync_status(
                conn,
                delivery_note_id=dn_id,
                is_synced=0,
                payload_hash=payload_hash,
            )

        if not config.dry_run:
            conn.commit()

    # --- Delete detection DISABLED ---
    # Auto-delete detection is disabled because false positives (partial Tally
    # response, date-range mismatch, Day Book timeout) cause DCs to be
    # incorrectly marked as deleted and then cancelled on the server.
    deleted = 0

    logger.info(
        "Done. created=%d updated=%d skipped=%d deleted=%d ledgers=%d stock_items=%d",
        created,
        updated,
        skipped,
        deleted,
        ledgers_fetched,
        stock_items_fetched,
    )
    return {"created": created, "updated": updated, "skipped": skipped, "deleted": deleted}


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Tally Delivery Notes and store in SQLite.")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--tally-url", help="Tally HTTP URL")
    parser.add_argument("--company", help="Tally company name")
    parser.add_argument("--entity-id", type=int, help="Catalytics entity id (stored for sync)")
    parser.add_argument("--from-date", help="From date YYYYMMDD")
    parser.add_argument("--to-date", help="To date YYYYMMDD")
    parser.add_argument("--days-back", type=int, help="Days back from today (overrides FY default)")
    parser.add_argument("--fetch-stock", action="store_true", help="Fetch stock item details")
    parser.add_argument("--dry-run", action="store_true", help="Do not commit changes")
    parser.add_argument("--log-level", help="Logging level")
    parser.add_argument("--log-json", action="store_true", help="JSON log output")
    parser.add_argument("--log-file", help="Log file path")
    args = parser.parse_args()

    config = build_config(args)
    try:
        run_once(config)
    except Exception:
        logger.exception("Fetch run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
