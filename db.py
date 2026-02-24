import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tally_name TEXT,
    entity_id INTEGER,
    tally_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS ledgers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(company_id, name),
    FOREIGN KEY(company_id) REFERENCES companies(id)
);

CREATE TABLE IF NOT EXISTS stock_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(company_id, name),
    FOREIGN KEY(company_id) REFERENCES companies(id)
);

CREATE TABLE IF NOT EXISTS delivery_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    dc_no TEXT NOT NULL,
    voucher_date TEXT,
    party_ledger_name TEXT,
    reference TEXT,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(company_id, dc_no),
    FOREIGN KEY(company_id) REFERENCES companies(id)
);

CREATE TABLE IF NOT EXISTS delivery_note_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_note_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    stock_name TEXT,
    qty REAL,
    rate REAL,
    amount REAL,
    godown_name TEXT,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(delivery_note_id, line_no),
    FOREIGN KEY(delivery_note_id) REFERENCES delivery_notes(id)
);

CREATE TABLE IF NOT EXISTS sync_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_note_id INTEGER NOT NULL UNIQUE,
    is_synced INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    synced_at TEXT,
    last_error TEXT,
    last_response_json TEXT,
    payload_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(delivery_note_id) REFERENCES delivery_notes(id)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,
    stats_json TEXT,
    error_text TEXT
);

CREATE TABLE IF NOT EXISTS ledger_sync_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_id INTEGER NOT NULL UNIQUE,
    is_synced INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    synced_at TEXT,
    last_error TEXT,
    last_response_json TEXT,
    payload_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(ledger_id) REFERENCES ledgers(id)
);

CREATE TABLE IF NOT EXISTS stock_sync_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_item_id INTEGER NOT NULL UNIQUE,
    is_synced INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    synced_at TEXT,
    last_error TEXT,
    last_response_json TEXT,
    payload_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(stock_item_id) REFERENCES stock_items(id)
);
"""


def now_ts() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def json_loads(value: str) -> Any:
    return json.loads(value) if value else None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def ensure_company(
    conn: sqlite3.Connection,
    *,
    name: str,
    tally_name: Optional[str],
    entity_id: Optional[int],
    tally_url: Optional[str],
) -> int:
    ts = now_ts()
    conn.execute(
        """
        INSERT INTO companies (name, tally_name, entity_id, tally_url, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            tally_name = excluded.tally_name,
            entity_id = excluded.entity_id,
            tally_url = excluded.tally_url,
            updated_at = excluded.updated_at
        """,
        (name, tally_name, entity_id, tally_url, ts, ts),
    )
    row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
    return int(row["id"]) if row else 0


def upsert_json_row(
    conn: sqlite3.Connection,
    *,
    table: str,
    company_id: int,
    name: str,
    data: Dict[str, Any],
) -> None:
    ts = now_ts()
    data_json = json_dumps(data)
    conn.execute(
        f"""
        INSERT INTO {table} (company_id, name, data_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(company_id, name) DO UPDATE SET
            data_json = excluded.data_json,
            updated_at = excluded.updated_at
        """,
        (company_id, name, data_json, ts, ts),
    )


def upsert_ledger(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    name: str,
    data: Dict[str, Any],
) -> int:
    upsert_json_row(conn, table="ledgers", company_id=company_id, name=name, data=data)
    row = conn.execute(
        "SELECT id FROM ledgers WHERE company_id = ? AND name = ?",
        (company_id, name),
    ).fetchone()
    return int(row["id"]) if row else 0


def upsert_stock_item(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    name: str,
    data: Dict[str, Any],
) -> int:
    upsert_json_row(conn, table="stock_items", company_id=company_id, name=name, data=data)
    row = conn.execute(
        "SELECT id FROM stock_items WHERE company_id = ? AND name = ?",
        (company_id, name),
    ).fetchone()
    return int(row["id"]) if row else 0


def upsert_delivery_note(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    dc_no: str,
    voucher_date: Optional[str],
    party_ledger_name: Optional[str],
    reference: Optional[str],
    data: Dict[str, Any],
) -> int:
    ts = now_ts()
    data_json = json_dumps(data)
    conn.execute(
        """
        INSERT INTO delivery_notes
            (company_id, dc_no, voucher_date, party_ledger_name, reference, data_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id, dc_no) DO UPDATE SET
            voucher_date = excluded.voucher_date,
            party_ledger_name = excluded.party_ledger_name,
            reference = excluded.reference,
            data_json = excluded.data_json,
            updated_at = excluded.updated_at
        """,
        (company_id, dc_no, voucher_date, party_ledger_name, reference, data_json, ts, ts),
    )
    row = conn.execute(
        "SELECT id FROM delivery_notes WHERE company_id = ? AND dc_no = ?",
        (company_id, dc_no),
    ).fetchone()
    return int(row["id"]) if row else 0


def replace_delivery_note_items(
    conn: sqlite3.Connection,
    *,
    delivery_note_id: int,
    items: Iterable[Dict[str, Any]],
) -> None:
    ts = now_ts()
    conn.execute(
        "DELETE FROM delivery_note_items WHERE delivery_note_id = ?",
        (delivery_note_id,),
    )
    for idx, item in enumerate(items or []):
        stock_name = item.get("STOCKITEMNAME") or item.get("ITEMNAME") or ""
        qty = item.get("BILLEDQTY") or item.get("ACTUALQTY") or ""
        rate = item.get("RATE") or ""
        amount = item.get("AMOUNT") or ""
        godown = item.get("GODOWNNAME") or ""
        conn.execute(
            """
            INSERT INTO delivery_note_items
                (delivery_note_id, line_no, stock_name, qty, rate, amount, godown_name, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_note_id,
                idx,
                stock_name,
                _safe_float(qty),
                _safe_float(rate),
                _safe_float(amount),
                godown,
                json_dumps(item),
                ts,
            ),
        )


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ensure_sync_status(
    conn: sqlite3.Connection,
    *,
    delivery_note_id: int,
    is_synced: int,
    payload_hash: Optional[str],
) -> None:
    ts = now_ts()
    conn.execute(
        """
        INSERT INTO sync_status
            (delivery_note_id, is_synced, attempts, last_attempt_at, synced_at,
             last_error, last_response_json, payload_hash, created_at, updated_at)
        VALUES (?, ?, 0, NULL, NULL, NULL, NULL, ?, ?, ?)
        ON CONFLICT(delivery_note_id) DO UPDATE SET
            is_synced = excluded.is_synced,
            attempts = 0,
            last_attempt_at = NULL,
            synced_at = NULL,
            last_error = NULL,
            last_response_json = NULL,
            payload_hash = excluded.payload_hash,
            updated_at = excluded.updated_at
        """,
        (delivery_note_id, is_synced, payload_hash, ts, ts),
    )


def ensure_ledger_sync_status(
    conn: sqlite3.Connection,
    *,
    ledger_id: int,
    is_synced: int,
    payload_hash: Optional[str],
) -> None:
    ts = now_ts()
    conn.execute(
        """
        INSERT INTO ledger_sync_status
            (ledger_id, is_synced, attempts, last_attempt_at, synced_at,
             last_error, last_response_json, payload_hash, created_at, updated_at)
        VALUES (?, ?, 0, NULL, NULL, NULL, NULL, ?, ?, ?)
        ON CONFLICT(ledger_id) DO UPDATE SET
            is_synced = excluded.is_synced,
            attempts = 0,
            last_attempt_at = NULL,
            synced_at = NULL,
            last_error = NULL,
            last_response_json = NULL,
            payload_hash = excluded.payload_hash,
            updated_at = excluded.updated_at
        """,
        (ledger_id, is_synced, payload_hash, ts, ts),
    )


def ensure_stock_sync_status(
    conn: sqlite3.Connection,
    *,
    stock_item_id: int,
    is_synced: int,
    payload_hash: Optional[str],
) -> None:
    ts = now_ts()
    conn.execute(
        """
        INSERT INTO stock_sync_status
            (stock_item_id, is_synced, attempts, last_attempt_at, synced_at,
             last_error, last_response_json, payload_hash, created_at, updated_at)
        VALUES (?, ?, 0, NULL, NULL, NULL, NULL, ?, ?, ?)
        ON CONFLICT(stock_item_id) DO UPDATE SET
            is_synced = excluded.is_synced,
            attempts = 0,
            last_attempt_at = NULL,
            synced_at = NULL,
            last_error = NULL,
            last_response_json = NULL,
            payload_hash = excluded.payload_hash,
            updated_at = excluded.updated_at
        """,
        (stock_item_id, is_synced, payload_hash, ts, ts),
    )
