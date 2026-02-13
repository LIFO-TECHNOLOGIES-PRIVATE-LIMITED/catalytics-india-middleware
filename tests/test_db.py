import sqlite3
import unittest

import db


class TestDBHelpers(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        db.init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_company_and_ledger_upsert(self) -> None:
        company_id = db.ensure_company(
            self.conn,
            name="TestCo",
            tally_name="TestCo",
            entity_id=1,
            tally_url="http://tally/",
        )
        self.assertTrue(company_id)

        db.upsert_json_row(
            self.conn,
            table="ledgers",
            company_id=company_id,
            name="LedgerA",
            data={"NAME": "LedgerA", "GSTIN": "123"},
        )
        row = self.conn.execute(
            "SELECT data_json FROM ledgers WHERE company_id = ? AND name = ?",
            (company_id, "LedgerA"),
        ).fetchone()
        self.assertIsNotNone(row)

    def test_delivery_note_and_items(self) -> None:
        company_id = db.ensure_company(
            self.conn,
            name="TestCo",
            tally_name="TestCo",
            entity_id=1,
            tally_url="http://tally/",
        )
        dn_id = db.upsert_delivery_note(
            self.conn,
            company_id=company_id,
            dc_no="DC-1",
            voucher_date="20240101",
            party_ledger_name="LedgerA",
            reference="PO-1",
            data={"VOUCHERNUMBER": "DC-1"},
        )
        self.assertTrue(dn_id)

        db.replace_delivery_note_items(
            self.conn,
            delivery_note_id=dn_id,
            items=[{"STOCKITEMNAME": "ItemA", "BILLEDQTY": "2", "RATE": "10"}],
        )
        row = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM delivery_note_items WHERE delivery_note_id = ?",
            (dn_id,),
        ).fetchone()
        self.assertEqual(row["cnt"], 1)

        db.ensure_sync_status(
            self.conn,
            delivery_note_id=dn_id,
            is_synced=0,
            payload_hash="hash1",
        )
        status_row = self.conn.execute(
            "SELECT is_synced, payload_hash FROM sync_status WHERE delivery_note_id = ?",
            (dn_id,),
        ).fetchone()
        self.assertEqual(status_row["is_synced"], 0)
        self.assertEqual(status_row["payload_hash"], "hash1")


if __name__ == "__main__":
    unittest.main()
