"""
Sync products to new Catalytics API format (TallyProductNamePayloadAPIView)
This script transforms Tally stock items into the new format with:
- product_master_name, unit_master_name, variant_name
- product_type_code, product_type_name
- hsn_code, rate, gst_rate, etc.
"""
import argparse
from dataclasses import dataclass
import logging
from typing import Any, Dict, List, Optional
import os
import sys
import re

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import requests
import config as cfg
import db
from logging_utils import setup_logging

DEFAULT_ENV_PATH = cfg.resolve_env_path(os.path.dirname(__file__))
logger = logging.getLogger("tally_sync_products_new")


@dataclass
class SyncConfig:
    db_path: str
    api_base_url: str
    api_key: Optional[str]
    entity_id: Optional[int]
    company: Optional[str]
    batch_size: int
    limit: int
    max_attempts: int
    dry_run: bool
    log_level: str
    log_json: bool
    log_file: Optional[str]


def _parse_product_name(stock_name: str) -> Dict[str, str]:
    """
    Parse Tally stock item name into components.
    Example: "INDUSTRIAL OXYGEN 4 CUM (CYL)" ->
        product_master_name: "INDUSTRIAL OXYGEN"
        variant_name: "4 CUM"
        product_type_code: "CYL"
    """
    stock_name = stock_name.strip()
    
    # Extract product_type_code from parentheses at end
    product_type_code = ""
    product_type_name = ""
    type_match = re.search(r'\(([^)]+)\)\s*$', stock_name)
    if type_match:
        product_type_code = type_match.group(1).strip()
        product_type_name = product_type_code
        stock_name = stock_name[:type_match.start()].strip()
    
    # Try to extract variant (number + unit pattern)
    variant_name = ""
    variant_match = re.search(r'(\d+(?:\.\d+)?\s*[A-Z]+)\s*$', stock_name, re.IGNORECASE)
    if variant_match:
        variant_name = variant_match.group(1).strip()
        product_master_name = stock_name[:variant_match.start()].strip()
    else:
        product_master_name = stock_name
    
    return {
        "product_master_name": product_master_name,
        "variant_name": variant_name,
        "product_type_code": product_type_code,
        "product_type_name": product_type_name,
    }


def _extract_unit(stock_data: Dict[str, Any]) -> str:
    """Extract unit from Tally stock item data"""
    return (
        stock_data.get("BASEUNITS")
        or stock_data.get("UNIT")
        or stock_data.get("UOM")
        or "Nos"
    )


def _extract_rates(stock_data: Dict[str, Any]) -> Dict[str, float]:
    """Extract rate and GST rates from Tally data"""
    rate = 0.0
    try:
        rate_str = stock_data.get("OPENINGRATE") or stock_data.get("RATE") or "0"
        rate = float(str(rate_str).replace(",", ""))
    except:
        pass
    
    gst_rate = 0.0
    igst_rate = 0.0
    cgst_rate = 0.0
    sgst_rate = 0.0
    
    # Try to extract GST from tax classifications
    gst_details = stock_data.get("GSTDETAILS") or {}
    if isinstance(gst_details, dict):
        try:
            gst_rate = float(gst_details.get("GSTRATE") or gst_details.get("TAXRATE") or "0")
            igst_rate = gst_rate
            cgst_rate = gst_rate / 2
            sgst_rate = gst_rate / 2
        except:
            pass
    
    return {
        "rate": rate,
        "gst_rate": gst_rate,
        "igst_rate": igst_rate,
        "cgst_rate": cgst_rate,
        "sgst_rate": sgst_rate,
    }


def _build_product_payload(
    item_row: Dict[str, Any],
    *,
    entity_id: Optional[int],
    company_name: Optional[str],
) -> Dict[str, Any]:
    """Transform Tally stock item into new API format"""
    stock_data = db.json_loads(item_row["data_json"]) or {}
    stock_name = item_row["name"] or stock_data.get("NAME") or ""
    
    # Parse product name components
    name_parts = _parse_product_name(stock_name)
    
    # Extract unit
    unit = _extract_unit(stock_data)
    
    # Extract rates
    rates = _extract_rates(stock_data)
    
    # Extract HSN code
    hsn_code = (
        stock_data.get("HSNCODE")
        or stock_data.get("HSN")
        or stock_data.get("HSNNO")
        or ""
    )
    
    # Extract GUID/UUID (unique identifier from Tally)
    guid = (
        stock_data.get("GUID")
        or stock_data.get("MASTERID")
        or stock_data.get("ALTERID")
        or stock_data.get("REMOTEALTGUID")
        or stock_data.get("REMOTEID")
        or ""
    )
    
    # Build payload
    payload = {
        "stock_item_name": stock_name,
        "product_master_name": name_parts["product_master_name"],
        "unit_master_name": unit,
        "variant_name": name_parts["variant_name"],
        "product_type_code": name_parts["product_type_code"],
        "product_type_name": name_parts["product_type_name"],
        "hsn_code": str(hsn_code).strip(),
        "guid": str(guid).strip(),  # Add GUID/UUID for unique identification
        "rate": rates["rate"],
        "gst_rate": rates["gst_rate"],
        "igst_rate": rates["igst_rate"],
        "cgst_rate": rates["cgst_rate"],
        "sgst_rate": rates["sgst_rate"],
    }
    
    if entity_id:
        payload["entity_id"] = entity_id
    if company_name:
        payload["tally_company"] = company_name
    
    return payload


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
        where_company = "AND s.company_id = ?"
        params.append(company_id)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT s.*, ss.is_synced, ss.attempts, ss.payload_hash
        FROM stock_items s
        LEFT JOIN stock_sync_status ss ON ss.stock_item_id = s.id
        WHERE COALESCE(ss.is_synced, 0) = 0
          AND COALESCE(ss.attempts, 0) < ?
          AND COALESCE(s.is_deleted, 0) = 0
          {where_company}
        ORDER BY s.updated_at ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _update_sync_status(
    conn,
    *,
    stock_item_id: int,
    success: bool,
    payload_hash: str,
    response_json: Optional[Dict[str, Any]],
    error_text: Optional[str],
) -> None:
    ts = db.now_ts()
    conn.execute(
        """
        INSERT INTO stock_sync_status
            (stock_item_id, is_synced, attempts, last_attempt_at, synced_at,
             last_error, last_response_json, payload_hash, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_item_id) DO UPDATE SET
            is_synced = excluded.is_synced,
            attempts = stock_sync_status.attempts + 1,
            last_attempt_at = excluded.last_attempt_at,
            synced_at = excluded.synced_at,
            last_error = excluded.last_error,
            last_response_json = excluded.last_response_json,
            payload_hash = excluded.payload_hash,
            updated_at = excluded.updated_at
        """,
        (
            stock_item_id,
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


def build_config(args: argparse.Namespace) -> SyncConfig:
    env_path = getattr(args, "config", None) or DEFAULT_ENV_PATH
    cfg.load_env_file(env_path)
    return SyncConfig(
        db_path=args.db_path or cfg.get_env("TALLY_DB_PATH") or "",
        api_base_url=args.api_base_url or cfg.get_env("CATALYTICS_API_BASE_URL") or "",
        api_key=args.api_key or cfg.get_env("CATALYTICS_API_KEY"),
        entity_id=args.entity_id or cfg.get_env_int("CATALYTICS_ENTITY_ID"),
        company=args.company or cfg.get_env("TALLY_COMPANY"),
        batch_size=args.batch_size or cfg.get_env_int("SYNC_BATCH_SIZE", 10) or 10,
        limit=args.limit or cfg.get_env_int("SYNC_LIMIT", 200) or 200,
        max_attempts=args.max_attempts or cfg.get_env_int("SYNC_MAX_ATTEMPTS", 5) or 5,
        dry_run=bool(args.dry_run) or cfg.get_env_bool("SYNC_DRY_RUN", False),
        log_level=args.log_level or cfg.get_env("LOG_LEVEL", "INFO"),
        log_json=bool(args.log_json) or cfg.get_env_bool("LOG_JSON", False),
        log_file=args.log_file or cfg.get_env("LOG_FILE"),
    )


def run_once(config: SyncConfig) -> Dict[str, int]:
    setup_logging(level=config.log_level, json_output=config.log_json, file_path=config.log_file)

    if not config.db_path or not config.api_base_url:
        raise ValueError("db_path and api_base_url are required")

    conn = db.connect(config.db_path)
    db.init_db(conn)

    company_id = None
    company_name = config.company
    if config.company:
        row = conn.execute(
            "SELECT id, name, entity_id FROM companies WHERE name = ?",
            (config.company,),
        ).fetchone()
        if row:
            company_id = int(row["id"])
            if not config.entity_id and row["entity_id"]:
                config.entity_id = int(row["entity_id"])
            company_name = row["name"]

    items = _fetch_unsynced(conn, company_id=company_id, limit=config.limit, max_attempts=config.max_attempts)
    if not items:
        logger.info("No unsynced stock items found")
        return {"sent": 0, "ok": 0, "failed": 0}

    logger.info("Found %d unsynced products to sync", len(items))

    # New API endpoint
    endpoint = config.api_base_url.rstrip("/") + "/tally-product_name-payload/"
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["X-API-Key"] = config.api_key

    total_sent = 0
    total_ok = 0
    total_fail = 0

    # Send products ONE AT A TIME (new API expects single product per request)
    for item_row in items:
        try:
            payload = _build_product_payload(
                item_row,
                entity_id=config.entity_id,
                company_name=company_name,
            )
            payload_hash = db.sha256_text(db.json_dumps(payload))
        except Exception as exc:
            logger.exception("Failed to build payload for product id=%s name=%s", item_row.get("id"), item_row.get("name"))
            _update_sync_status(
                conn,
                stock_item_id=item_row["id"],
                success=False,
                payload_hash="",
                response_json=None,
                error_text=f"payload_build_error: {exc}",
            )
            conn.commit()
            total_fail += 1
            continue

        if config.dry_run:
            logger.info("Dry-run: would send product %s", payload.get("stock_item_name"))
            _update_sync_status(
                conn,
                stock_item_id=item_row["id"],
                success=False,
                payload_hash=payload_hash,
                response_json={"dry_run": True},
                error_text="dry_run",
            )
            conn.commit()
            continue

        # Send request
        try:
            logger.info("Sending product: %s", payload.get("stock_item_name"))
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)
            total_sent += 1
        except Exception as exc:
            logger.exception("API request failed for product %s", payload.get("stock_item_name"))
            _update_sync_status(
                conn,
                stock_item_id=item_row["id"],
                success=False,
                payload_hash=payload_hash,
                response_json=None,
                error_text=str(exc),
            )
            conn.commit()
            total_fail += 1
            continue

        # Parse response
        response_json = None
        try:
            response_json = resp.json()
        except Exception:
            message = f"HTTP {resp.status_code} non-JSON response"
            response_json = {"status": "error", "message": message, "raw_preview": (resp.text or "")[:500]}

        # Check success
        status_val = response_json.get("status")
        success = (status_val == "success" and resp.status_code == 200)
        
        error_text = None
        if not success:
            error_text = response_json.get("message") or f"HTTP {resp.status_code}"
        
        logger.info("Product %s: %s", payload.get("stock_item_name"), "OK" if success else f"FAILED - {error_text}")
        
        _update_sync_status(
            conn,
            stock_item_id=item_row["id"],
            success=success,
            payload_hash=payload_hash,
            response_json=response_json,
            error_text=error_text,
        )
        conn.commit()
        
        if success:
            total_ok += 1
        else:
            total_fail += 1

    logger.info("Product sync complete. sent=%d ok=%d failed=%d", total_sent, total_ok, total_fail)
    return {"sent": total_sent, "ok": total_ok, "failed": total_fail}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync products to new Catalytics API format")
    parser.add_argument("--config", help="Path to .env file")
    parser.add_argument("--db-path", help="SQLite database path")
    parser.add_argument("--api-base-url", help="Catalytics base URL")
    parser.add_argument("--api-key", help="API key (X-API-Key)")
    parser.add_argument("--entity-id", type=int, help="Entity ID")
    parser.add_argument("--company", help="Company name")
    parser.add_argument("--batch-size", type=int, default=1, help="Batch size (1 for new API)")
    parser.add_argument("--limit", type=int, default=200, help="Max products per run")
    parser.add_argument("--max-attempts", type=int, default=5, help="Max retry attempts")
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    parser.add_argument("--log-level", help="Log level")
    parser.add_argument("--log-json", action="store_true", help="JSON logs")
    parser.add_argument("--log-file", help="Log file")
    args = parser.parse_args()

    config = build_config(args)
    try:
        run_once(config)
    except Exception:
        logger.exception("Product sync failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
