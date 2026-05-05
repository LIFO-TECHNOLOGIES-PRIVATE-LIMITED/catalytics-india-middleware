import sqlite3
import unittest
import os
from unittest.mock import Mock, patch

import db
import sync_catalytics as syncer


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
            tally_guid=None,
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
            api_base_url="http://localhost:8000",
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

    def test_find_matching_instant_dc(self) -> None:
        note = {
            "voucher_date": "20260420",
            "party_ledger_name": "ABC Industries",
        }
        voucher = {
            "DATE": "20260420",
            "PARTYLEDGERNAME": "ABC Industries",
        }
        items = [
            {"STOCKITEMNAME": "Oxygen 7M3", "BILLEDQTY": "2 Nos"},
            {"STOCKITEMNAME": "Nitrogen 10M3", "BILLEDQTY": "1"},
        ]
        instant_dcs = [
            {
                "id": 101,
                "dc_no": "INS-101",
                "is_instant_dc": True,
                "dc_synced": False,
                "dc_date": "2026-04-19",
                "customer": {"name": "ABC Industries"},
                "order_details": [
                    {"product": {"name": "Oxygen 7M3"}, "quantity": 2},
                    {"product": {"name": "Nitrogen 10M3"}, "quantity": 1},
                ],
            }
        ]

        match = syncer._find_matching_instant_dc(note, voucher, items, instant_dcs)
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], 101)

    @patch("sync_catalytics.requests.get")
    def test_fetch_unsynced_instant_dcs_with_detail_fallback(self, mock_get: Mock) -> None:
        first_resp = Mock()
        first_resp.status_code = 200
        first_resp.json.return_value = {"results": [{"id": 55, "dc_no": "INS-55"}]}

        detail_resp = Mock()
        detail_resp.status_code = 200
        detail_resp.json.return_value = {
            "data": {
                "id": 55,
                "dc_no": "INS-55",
                "dc_date": "2026-04-20",
                "customer": {"name": "ABC Industries"},
                "order_details": [{"product": {"name": "Oxygen 7M3"}, "quantity": 2}],
            }
        }

        mock_get.side_effect = [first_resp, detail_resp]
        rows = syncer._fetch_unsynced_instant_dcs("http://localhost:8000", 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 55)
        self.assertEqual(rows[0]["dc_date"], "2026-04-20")

    def test_enrich_voucher_sets_admin_and_created_fields(self) -> None:
        voucher = {}
        note = {
            "dc_no": "DC-42",
            "voucher_date": "20260421",
            "party_ledger_name": "ABC Industries",
        }
        with patch.dict(os.environ, {"DEFAULT_ADMIN_USER_ID": "55"}, clear=False):
            syncer._enrich_voucher(voucher, note, [])

        self.assertEqual(voucher.get("created_by"), 55)
        self.assertEqual(voucher.get("modified_by"), 55)
        self.assertEqual(voucher.get("created_at"), "2026-04-21")
        self.assertEqual(voucher.get("created_on"), "2026-04-21")

    @patch("sync_catalytics.requests.post")
    def test_mark_instant_dc_synced_on_portal_post(self, mock_post: Mock) -> None:
        resp = Mock()
        resp.status_code = 200
        mock_post.return_value = resp

        ok = syncer._mark_instant_dc_synced_on_portal(
            "http://localhost:8000/import",
            123,
            tally_voucher_no="DC-123",
        )
        self.assertTrue(ok)
        mock_post.assert_called_once()
        called_url = mock_post.call_args[0][0]
        called_payload = mock_post.call_args[1]["json"]
        self.assertTrue(called_url.endswith("/transaction/delivery_challan/instant/123/mark-synced"))
        self.assertEqual(called_payload.get("tally_voucher_no"), "DC-123")


if __name__ == "__main__":
    unittest.main()
