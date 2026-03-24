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
    tally_guid TEXT,
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

CREATE TABLE IF NOT EXISTS fetch_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    data_type TEXT NOT NULL,
    last_fetch_date TEXT,
    last_fetch_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(company_id, data_type),
    FOREIGN KEY(company_id) REFERENCES companies(id)
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


_SOFT_DELETE_COLUMNS = {
    "ledgers": ["is_deleted", "deleted_at"],
    "stock_items": ["is_deleted", "deleted_at"],
    "delivery_notes": ["is_deleted", "deleted_at"],
}


_soft_delete_ready = False


def migrate_db(conn: sqlite3.Connection) -> None:
    """Add is_deleted / deleted_at columns to tables that lack them."""
    global _soft_delete_ready
    for table, columns in _SOFT_DELETE_COLUMNS.items():
        try:
            existing = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
        except Exception:
            continue
        for col in columns:
            if col not in existing:
                col_type = "INTEGER DEFAULT 0" if col == "is_deleted" else "TEXT"
                try:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                    )
                except Exception:
                    pass  # Column may already exist from concurrent migration
    # Ensure delivery_notes.tally_guid exists for GUID-based updates
    try:
        existing_dn = {row[1] for row in conn.execute("PRAGMA table_info(delivery_notes)").fetchall()}
        if "tally_guid" not in existing_dn:
            conn.execute("ALTER TABLE delivery_notes ADD COLUMN tally_guid TEXT")
    except Exception:
        pass

    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_notes_guid ON delivery_notes(company_id, tally_guid)")
    except Exception:
        pass

    _soft_delete_ready = True


def now_ts() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def json_loads(value: str) -> Any:
    return json.loads(value) if value else None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# Fields that Tally updates automatically on every alteration
# but do NOT represent a meaningful data change for sync purposes.
_VOLATILE_TALLY_FIELDS = {
    "ALTERID",        # increments on every Tally modification
    "REMOTEALTERID",  # same, for remote sync
    "ALTEREDON",      # timestamp of last alteration
    "ALTEREDBY",      # who altered
    "UPDATEDDATETIME",# last update timestamp
    "SORTPOSITION",   # internal ordering, changes unpredictably
}


def stable_hash(data: Any) -> str:
    """
    Compute a hash of Tally data excluding volatile fields that Tally
    changes automatically (ALTERID, ALTEREDON, etc.) without any real
    data change. This prevents unnecessary re-syncs when only those
    internal fields changed.
    """
    if isinstance(data, dict):
        filtered = {k: v for k, v in data.items() if k not in _VOLATILE_TALLY_FIELDS}
        return sha256_text(json_dumps(filtered))
    return sha256_text(json_dumps(data))


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=120.0)  # 120 second timeout for locks (increased for product sync)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # Write-Ahead Logging for better concurrency
    conn.execute("PRAGMA busy_timeout = 120000")  # 120 second busy timeout
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    migrate_db(conn)


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
    restore_clause = ""
    if _soft_delete_ready:
        restore_clause = ",\n            is_deleted = 0,\n            deleted_at = NULL"
    conn.execute(
        f"""
        INSERT INTO {table} (company_id, name, data_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(company_id, name) DO UPDATE SET
            data_json = excluded.data_json,
            updated_at = excluded.updated_at{restore_clause}
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
    restore_clause = ""
    if _soft_delete_ready:
        restore_clause = ",\n            is_deleted = 0,\n            deleted_at = NULL"
    conn.execute(
        f"""
        INSERT INTO delivery_notes
            (company_id, dc_no, voucher_date, party_ledger_name, tally_guid, reference, data_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id, dc_no) DO UPDATE SET
            voucher_date = excluded.voucher_date,
            party_ledger_name = excluded.party_ledger_name,
            tally_guid = excluded.tally_guid,
            reference = excluded.reference,
            data_json = excluded.data_json,
            updated_at = excluded.updated_at{restore_clause}
        """,
        (company_id, dc_no, voucher_date, party_ledger_name, tally_guid, reference, data_json, ts, ts),
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


def mark_records_deleted(
    conn: sqlite3.Connection,
    table: str,
    company_id: int,
    names: Iterable[str],
) -> int:
    """Mark records as deleted by name. Returns count of records marked."""
    ts = now_ts()
    name_list = list(names)
    if not name_list:
        return 0
    count = 0
    # Batch in groups of 500 to stay within SQLite variable limits
    for i in range(0, len(name_list), 500):
        batch = name_list[i : i + 500]
        placeholders = ",".join("?" for _ in batch)
        cursor = conn.execute(
            f"UPDATE {table} SET is_deleted = 1, deleted_at = ? WHERE company_id = ? AND name IN ({placeholders}) AND COALESCE(is_deleted, 0) = 0",
            [ts, company_id] + batch,
        )
        count += cursor.rowcount
    return count


def mark_dc_records_deleted(
    conn: sqlite3.Connection,
    company_id: int,
    dc_nos: Iterable[str],
) -> int:
    """Mark delivery notes as deleted by dc_no. Returns count of records marked."""
    ts = now_ts()
    dc_list = list(dc_nos)
    if not dc_list:
        return 0
    count = 0
    for i in range(0, len(dc_list), 500):
        batch = dc_list[i : i + 500]
        placeholders = ",".join("?" for _ in batch)
        cursor = conn.execute(
            f"UPDATE delivery_notes SET is_deleted = 1, deleted_at = ? WHERE company_id = ? AND dc_no IN ({placeholders}) AND COALESCE(is_deleted, 0) = 0",
            [ts, company_id] + batch,
        )
        count += cursor.rowcount
    return count


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
            is_synced = CASE
                WHEN sync_status.payload_hash != excluded.payload_hash THEN excluded.is_synced
                ELSE sync_status.is_synced
            END,
            attempts = CASE
                WHEN sync_status.payload_hash != excluded.payload_hash THEN 0
                ELSE sync_status.attempts
            END,
            last_attempt_at = CASE
                WHEN sync_status.payload_hash != excluded.payload_hash THEN NULL
                ELSE sync_status.last_attempt_at
            END,
            synced_at = CASE
                WHEN sync_status.payload_hash != excluded.payload_hash THEN NULL
                ELSE sync_status.synced_at
            END,
            last_error = CASE
                WHEN sync_status.payload_hash != excluded.payload_hash THEN NULL
                ELSE sync_status.last_error
            END,
            last_response_json = CASE
                WHEN sync_status.payload_hash != excluded.payload_hash THEN NULL
                ELSE sync_status.last_response_json
            END,
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
            is_synced = CASE
                WHEN ledger_sync_status.payload_hash != excluded.payload_hash THEN excluded.is_synced
                ELSE ledger_sync_status.is_synced
            END,
            attempts = CASE
                WHEN ledger_sync_status.payload_hash != excluded.payload_hash THEN 0
                ELSE ledger_sync_status.attempts
            END,
            last_attempt_at = CASE
                WHEN ledger_sync_status.payload_hash != excluded.payload_hash THEN NULL
                ELSE ledger_sync_status.last_attempt_at
            END,
            synced_at = CASE
                WHEN ledger_sync_status.payload_hash != excluded.payload_hash THEN NULL
                ELSE ledger_sync_status.synced_at
            END,
            last_error = CASE
                WHEN ledger_sync_status.payload_hash != excluded.payload_hash THEN NULL
                ELSE ledger_sync_status.last_error
            END,
            last_response_json = CASE
                WHEN ledger_sync_status.payload_hash != excluded.payload_hash THEN NULL
                ELSE ledger_sync_status.last_response_json
            END,
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
            is_synced = CASE
                WHEN stock_sync_status.payload_hash != excluded.payload_hash THEN excluded.is_synced
                ELSE stock_sync_status.is_synced
            END,
            attempts = CASE
                WHEN stock_sync_status.payload_hash != excluded.payload_hash THEN 0
                ELSE stock_sync_status.attempts
            END,
            last_attempt_at = CASE
                WHEN stock_sync_status.payload_hash != excluded.payload_hash THEN NULL
                ELSE stock_sync_status.last_attempt_at
            END,
            synced_at = CASE
                WHEN stock_sync_status.payload_hash != excluded.payload_hash THEN NULL
                ELSE stock_sync_status.synced_at
            END,
            last_error = CASE
                WHEN stock_sync_status.payload_hash != excluded.payload_hash THEN NULL
                ELSE stock_sync_status.last_error
            END,
            last_response_json = CASE
                WHEN stock_sync_status.payload_hash != excluded.payload_hash THEN NULL
                ELSE stock_sync_status.last_response_json
            END,
            payload_hash = excluded.payload_hash,
            updated_at = excluded.updated_at
        """,
        (stock_item_id, is_synced, payload_hash, ts, ts),
    )


def get_last_fetch_date(conn: sqlite3.Connection, company_id: int, data_type: str) -> Optional[str]:
    """
    Get the last fetch date for a specific data type (e.g., 'delivery_notes')
    
    Returns:
        Date string in YYYYMMDD format, or None if never fetched
    """
    row = conn.execute(
        "SELECT last_fetch_date FROM fetch_metadata WHERE company_id = ? AND data_type = ?",
        (company_id, data_type)
    ).fetchone()
    
    return row["last_fetch_date"] if row else None


def update_last_fetch_date(conn: sqlite3.Connection, company_id: int, data_type: str, fetch_date: str):
    """
    Update the last fetch date for a specific data type

    Args:
        company_id: Company ID
        data_type: Type of data (e.g., 'delivery_notes', 'ledgers', 'stock_items')
        fetch_date: Date in YYYYMMDD format
    """
    now = now_ts()

    existing = conn.execute(
        "SELECT id FROM fetch_metadata WHERE company_id = ? AND data_type = ?",
        (company_id, data_type)
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE fetch_metadata
               SET last_fetch_date = ?, last_fetch_at = ?, updated_at = ?
               WHERE company_id = ? AND data_type = ?""",
            (fetch_date, now, now, company_id, data_type)
        )
    else:
        conn.execute(
            """INSERT INTO fetch_metadata (company_id, data_type, last_fetch_date, last_fetch_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (company_id, data_type, fetch_date, now, now, now)
        )


# =============================================================================
# DATABASE CLASS â€” arasan-style master data (customers / products)
# =============================================================================

import logging as _logging
_db_logger = _logging.getLogger(__name__)


class Database:
    """
    Manages customers and products master-data tables.
    Works alongside the functional DC helpers above in the same SQLite file.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def execute(self, query, params=()):
        """Execute a write query and commit."""
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        self.conn.commit()
        return cursor

    def query(self, query, params=()):
        """Execute a SELECT and return one row."""
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return cursor.fetchone()

    def query_all(self, query, params=()):
        """Execute a SELECT and return all rows."""
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return cursor.fetchall()

    def _create_tables(self):
        cur = self.conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tally_guid TEXT,
                name TEXT UNIQUE NOT NULL,
                tally_company TEXT NOT NULL,
                gstin TEXT,
                pan TEXT,
                address TEXT,
                state TEXT,
                city TEXT,
                pincode TEXT,
                phone TEXT,
                email TEXT,
                data_json TEXT,
                sync_request_json TEXT,
                last_response_json TEXT,
                first_fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_synced INTEGER DEFAULT 0,
                catalytics_id INTEGER,
                sync_attempts INTEGER DEFAULT 0,
                last_sync_error TEXT,
                last_sync_at TIMESTAMP
            )
        """)

        # name_canonical is the UNIQUE key â€” handles spacing variations like "1.5CUM" vs "1.5 CUM"
        cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tally_guid TEXT,
                name TEXT NOT NULL,
                name_canonical TEXT UNIQUE NOT NULL,
                tally_company TEXT NOT NULL,
                hsn_code TEXT,
                unit TEXT,
                rate REAL,
                description TEXT,
                data_json TEXT,
                product_master_name TEXT,
                variant_name TEXT,
                unit_name TEXT,
                product_type_code TEXT,
                product_type_name TEXT,
                gst_applicable TEXT,
                gst_rate REAL,
                igst_rate REAL,
                cgst_rate REAL,
                sgst_rate REAL,
                sync_request_json TEXT,
                last_response_json TEXT,
                first_fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_synced INTEGER DEFAULT 0,
                catalytics_id INTEGER,
                sync_attempts INTEGER DEFAULT 0,
                last_sync_error TEXT,
                last_sync_at TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS duplicate_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_name TEXT NOT NULL,
                tally_company TEXT NOT NULL,
                owned_by_company TEXT NOT NULL,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                details TEXT
            )
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name_canonical)")

        self.conn.commit()
        self._migrate_master_tables()

    def _migrate_master_tables(self):
        """Add missing columns to existing tables when upgrading schema."""
        cur = self.conn.cursor()
        prod_cols = {row[1] for row in cur.execute("PRAGMA table_info(products)").fetchall()}
        if 'name_canonical' not in prod_cols:
            cur.execute("ALTER TABLE products ADD COLUMN name_canonical TEXT")
        cust_cols = {row[1] for row in cur.execute("PRAGMA table_info(customers)").fetchall()}
        if 'delivery_addresses_json' not in cust_cols:
            cur.execute("ALTER TABLE customers ADD COLUMN delivery_addresses_json TEXT")
        self.conn.commit()

    # ========================================================================
    # CUSTOMER OPERATIONS
    # ========================================================================

    def customer_exists_by_guid(self, guid: str):
        """Look up customer by tally_guid. Returns row or None."""
        if not guid:
            return None
        return self.query(
            "SELECT id, tally_company, name FROM customers WHERE tally_guid = ?",
            (guid.strip(),)
        )

    def customer_exists(self, name: str):
        """Check if customer name already exists (ignores whitespace differences)."""
        normalized = ''.join((name or '').split())
        return self.query(
            "SELECT id, tally_company FROM customers WHERE REPLACE(name, ' ', '') = ?",
            (normalized,)
        )

    def insert_customer(self, data: dict):
        self.execute("""
            INSERT INTO customers (
                tally_guid, name, tally_company, gstin, pan,
                address, state, city, pincode, phone, email,
                delivery_addresses_json, data_json, first_fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            data.get('tally_guid'), data.get('name'), data.get('tally_company'),
            data.get('gstin'), data.get('pan'), data.get('address'),
            data.get('state'), data.get('city'), data.get('pincode'),
            data.get('phone'), data.get('email'),
            data.get('delivery_addresses_json'), data.get('data_json'),
        ))

    def update_customer(self, customer_id: int, data: dict):
        """Update existing customer with fresh Tally data (GUID, name, GSTIN, address, etc.)
        and mark for re-sync."""
        self.execute("""
            UPDATE customers
            SET tally_guid = ?, name = ?, gstin = ?, pan = ?,
                address = ?, state = ?, city = ?, pincode = ?,
                phone = ?, email = ?, delivery_addresses_json = ?,
                data_json = ?,
                is_synced = 0, sync_attempts = 0, last_sync_error = NULL,
                last_updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (
            data.get('tally_guid'),
            data.get('name'),
            data.get('gstin'),
            data.get('pan'),
            data.get('address'),
            data.get('state'),
            data.get('city'),
            data.get('pincode'),
            data.get('phone'),
            data.get('email'),
            data.get('delivery_addresses_json'),
            data.get('data_json'),
            customer_id,
        ))

    # ========================================================================
    # PRODUCT OPERATIONS
    # ========================================================================

    def product_exists(self, name: str):
        """Check if product exists by exact name."""
        return self.query(
            "SELECT id, tally_company FROM products WHERE name = ?", (name,)
        )

    def product_exists_by_guid(self, guid: str):
        """Look up product by tally_guid. Returns row or None."""
        if not guid:
            return None
        return self.query(
            "SELECT id, tally_company, name, name_canonical FROM products WHERE tally_guid = ?",
            (guid.strip(),)
        )

    def product_exists_normalized(self, canonical_name: str):
        """Check if product exists by canonical name (handles spacing variations)."""
        return self.query(
            "SELECT id, tally_company, name FROM products WHERE name_canonical = ?",
            (canonical_name,)
        )

    def insert_product(self, data: dict):
        self.execute("""
            INSERT INTO products (
                tally_guid, name, name_canonical, tally_company, hsn_code, unit,
                rate, description, data_json,
                product_master_name, variant_name, unit_name,
                product_type_code, product_type_name,
                gst_applicable, gst_rate, igst_rate, cgst_rate, sgst_rate,
                first_fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            data.get('tally_guid'),
            data.get('name'),
            data.get('name_canonical'),
            data.get('tally_company'),
            data.get('hsn_code'),
            data.get('unit'),
            data.get('rate'),
            data.get('description'),
            data.get('data_json'),
            data.get('product_master_name'),
            data.get('variant_name'),
            data.get('unit_name'),
            data.get('product_type_code'),
            data.get('product_type_name'),
            data.get('gst_applicable'),
            data.get('gst_rate', 0.0),
            data.get('igst_rate', 0.0),
            data.get('cgst_rate', 0.0),
            data.get('sgst_rate', 0.0),
        ))

    def update_product(self, product_id: int, data: dict):
        """Update existing product with fresh Tally data (GUID, name, HSN, GST, etc.)
        and mark for re-sync."""
        self.execute("""
            UPDATE products
            SET tally_guid = ?, name = ?, name_canonical = ?, hsn_code = ?, unit = ?,
                rate = ?, description = ?, data_json = ?,
                product_master_name = ?, variant_name = ?, unit_name = ?,
                product_type_code = ?, product_type_name = ?,
                gst_applicable = ?, gst_rate = ?, igst_rate = ?, cgst_rate = ?, sgst_rate = ?,
                is_synced = 0, sync_attempts = 0, last_sync_error = NULL,
                last_updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (
            data.get('tally_guid'),
            data.get('name'),
            data.get('name_canonical'),
            data.get('hsn_code'),
            data.get('unit'),
            data.get('rate'),
            data.get('description'),
            data.get('data_json'),
            data.get('product_master_name'),
            data.get('variant_name'),
            data.get('unit_name'),
            data.get('product_type_code'),
            data.get('product_type_name'),
            data.get('gst_applicable'),
            data.get('gst_rate', 0.0),
            data.get('igst_rate', 0.0),
            data.get('cgst_rate', 0.0),
            data.get('sgst_rate', 0.0),
            product_id,
        ))

    def get_unsynced_customers(self, limit=None):
        """Get customers that haven't been synced yet."""
        if limit is None:
            return self.query_all("""
                SELECT * FROM customers
                WHERE is_synced = 0
                ORDER BY first_fetched_at
            """)
        return self.query_all("""
            SELECT * FROM customers
            WHERE is_synced = 0
            ORDER BY first_fetched_at
            LIMIT ?
        """, (limit,))

    def mark_customer_synced(self, customer_id, catalytics_id, response_json=None):
        """Mark customer as successfully synced."""
        catalytics_id = None
        self.execute("""
            UPDATE customers
            SET is_synced = 1,
                catalytics_id = ?,
                last_response_json = ?,
                last_sync_at = CURRENT_TIMESTAMP,
                last_sync_error = NULL
            WHERE id = ?
        """, (catalytics_id, response_json, customer_id))

    def mark_customer_sync_failed(self, customer_id, error_msg, response_json=None):
        """Mark customer sync as failed, incrementing attempts."""
        self.execute("""
            UPDATE customers
            SET sync_attempts = sync_attempts + 1,
                last_sync_error = ?,
                last_response_json = ?,
                last_sync_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (error_msg, response_json, customer_id))

    def get_unsynced_products(self, limit=50):
        """Get products that haven't been synced yet."""
        return self.query_all("""
            SELECT * FROM products
            WHERE is_synced = 0
            ORDER BY first_fetched_at
            LIMIT ?
        """, (limit,))

    def mark_product_synced(self, product_id, catalytics_id, response_json=None):
        """Mark product as successfully synced."""
        catalytics_id = None
        self.execute("""
            UPDATE products
            SET is_synced = 1,
                catalytics_id = ?,
                last_response_json = ?,
                last_sync_at = CURRENT_TIMESTAMP,
                last_sync_error = NULL
            WHERE id = ?
        """, (catalytics_id, response_json, product_id))

    def mark_product_sync_failed(self, product_id, error_msg, response_json=None):
        """Mark product sync as failed, incrementing attempts."""
        self.execute("""
            UPDATE products
            SET sync_attempts = sync_attempts + 1,
                last_sync_error = ?,
                last_response_json = ?,
                last_sync_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (error_msg, response_json, product_id))

    def log_duplicate(self, entity_type, entity_name, tally_company, owned_by_company, details=''):
        self.execute("""
            INSERT INTO duplicate_log (entity_type, entity_name, tally_company, owned_by_company, details)
            VALUES (?, ?, ?, ?, ?)
        """, (entity_type, entity_name, tally_company, owned_by_company, details))

    def get_statistics(self):
        total_customers = self.query("SELECT COUNT(*) FROM customers")[0]
        synced_customers = self.query("SELECT COUNT(*) FROM customers WHERE is_synced = 1")[0]
        total_products = self.query("SELECT COUNT(*) FROM products")[0]
        synced_products = self.query("SELECT COUNT(*) FROM products WHERE is_synced = 1")[0]

        customers_by_company = {
            row[0]: row[1]
            for row in self.query_all("SELECT tally_company, COUNT(*) FROM customers GROUP BY tally_company")
        }
        products_by_company = {
            row[0]: row[1]
            for row in self.query_all("SELECT tally_company, COUNT(*) FROM products GROUP BY tally_company")
        }

        return {
            'total_customers': total_customers,
            'synced_customers': synced_customers,
            'total_products': total_products,
            'synced_products': synced_products,
            'customers_by_company': customers_by_company,
            'products_by_company': products_by_company,
        }

    def close(self):
        if self.conn:
            self.conn.close()








