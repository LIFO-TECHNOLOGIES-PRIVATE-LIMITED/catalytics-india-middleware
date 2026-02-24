import sqlite3
import unittest

from tally_middleware import db
from tally_middleware import sync_catalytics as syncer


class TestSyncHelpers(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        db.init_db(self.conn)
        self.company_id = db.ensure_company(
            self.conn,
            name="TestCo",
            tally_name="TestCo",
            entity_id=1,
            tally_url="http://tally/",
        )

        self.dn_id = db.upsert_delivery_note(
            self.conn,
            company_id=self.company_id,
            dc_no="DC-1",
            voucher_date="20240101",
            party_ledger_name="LedgerA",
            reference="PO-1",
            data={"VOUCHERNUMBER": "DC-1", "PARTYLEDGERNAME": "LedgerA"},
        )
        db.replace_delivery_note_items(
            self.conn,
            delivery_note_id=self.dn_id,
            items=[{"STOCKITEMNAME": "ItemA", "BILLEDQTY": "2", "RATE": "10"}],
        )
        db.upsert_json_row(
            self.conn,
            table="ledgers",
            company_id=self.company_id,
            name="LedgerA",
            data={"NAME": "LedgerA", "GSTIN": "123"},
        )
        db.upsert_json_row(
            self.conn,
            table="stock_items",
            company_id=self.company_id,
            name="ItemA",
            data={"NAME": "ItemA", "HSNCODE": "HSN1"},
        )
        db.ensure_sync_status(
            self.conn,
            delivery_note_id=self.dn_id,
            is_synced=0,
            payload_hash="hash1",
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_fetch_unsynced_and_build_payload(self) -> None:
        notes = syncer._fetch_unsynced(self.conn, company_id=self.company_id, limit=10, max_attempts=5)
        self.assertEqual(len(notes), 1)
        payload, payload_hash = syncer._build_payload_for_note(
            self.conn,
            notes[0],
            entity_id=1,
            company_name="TestCo",
            allow_tally_fetch=False,
        )
        self.assertIn("voucher", payload)
        self.assertIn("ledgers", payload)
        self.assertIn("stock_items", payload)
        self.assertTrue(payload_hash)

    def test_update_sync_status(self) -> None:
        syncer._update_sync_status(
            self.conn,
            delivery_note_id=self.dn_id,
            success=True,
            payload_hash="hash2",
            response_json={"status": "success"},
            error_text=None,
        )
        row = self.conn.execute(
            "SELECT is_synced, attempts, payload_hash FROM sync_status WHERE delivery_note_id = ?",
            (self.dn_id,),
        ).fetchone()
        self.assertEqual(row["is_synced"], 1)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["payload_hash"], "hash2")


if __name__ == "__main__":
    unittest.main()
