import unittest
from unittest.mock import Mock

import content_memory


class ContentMemoryTests(unittest.TestCase):
    def test_weekly_brief_uses_measured_ctr_as_a_review_signal_not_a_promise(self):
        brief = content_memory.build_weekly_action_brief([{
            "video_id": "v1",
            "title": "Low CTR candidate",
            "url": "https://youtube.com/watch?v=v1",
            "selection_basis": "low_ctr_vs_peers",
            "opportunity_type": "Low CTR vs similar uploads",
            "opportunity_evidence": "CTR 2% vs peer median 5%.",
        }])

        self.assertEqual(len(brief), 1)
        self.assertEqual(brief[0]["confidence"], "Measured CTR signal")
        self.assertIn("title and thumbnail", brief[0]["first_action"])
        self.assertIn("not a CTR guarantee", brief[0]["first_action"])
        self.assertIn("CTR 2%", brief[0]["evidence"])

    def test_weekly_brief_is_conservative_when_peer_evidence_is_limited(self):
        brief = content_memory.build_weekly_action_brief([{
            "video_id": "v2",
            "title": "Sparse data",
            "selection_basis": "lowest_observed_views",
        }])

        self.assertEqual(brief[0]["confidence"], "Limited evidence")
        self.assertIn("manual review only", brief[0]["first_action"])
        self.assertIn("No comparative evidence", brief[0]["evidence"])

    def test_weekly_brief_caps_items_to_three_by_default(self):
        candidates = [
            {"video_id": f"v{i}", "title": f"Video {i}",
             "selection_basis": "low_views_vs_peers"}
            for i in range(7)
        ]
        brief = content_memory.build_weekly_action_brief(candidates)
        self.assertEqual(len(brief), 3)

    def test_scan_snapshot_keeps_missing_metrics_missing(self):
        snapshot = content_memory.build_scan_snapshot(
            start_date="2026-10-05",
            end_date="2026-10-16",
            upload_count=4,
            measured_views_count=3,
            usable_ctr_count=0,
            candidates=[{
                "video_id": "v1",
                "title": "Unknown CTR",
                "views": 20,
                "impressions": None,
                "ctr": None,
                "selection_basis": "lowest_observed_views",
            }],
        )

        self.assertEqual(snapshot["candidate_count"], 1)
        self.assertIsNone(snapshot["candidates"][0]["ctr"])
        self.assertIsNone(snapshot["candidates"][0]["impressions"])
        self.assertEqual(snapshot["usable_ctr_count"], 0)
        self.assertEqual(len(snapshot["action_brief"]), 1)

    def test_metrics_snapshot_does_not_invent_baselines(self):
        snapshot = content_memory.metrics_snapshot_from_session({}, "missing")
        self.assertIn("not inferred", snapshot["availability"])

    def test_metrics_snapshot_uses_matching_cached_row_and_period(self):
        state = {
            "opportunity_data": {
                "period": {"start": "2026-10-05", "end": "2026-10-11"},
                "video_rows": [{
                    "video_id": "v1",
                    "views": 21,
                    "ctr": None,
                    "impressions": None,
                    "averageViewDuration": 10,
                }],
            }
        }
        snapshot = content_memory.metrics_snapshot_from_session(state, "v1")
        self.assertEqual(snapshot["period"]["start"], "2026-10-05")
        self.assertEqual(snapshot["views"], 21)
        self.assertIsNone(snapshot["ctr"])
        self.assertIsNone(snapshot["impressions"])

    def test_initialization_creates_private_workbook_and_headers_before_reporting_ready(self):
        drive = Mock()
        drive.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }

        sheets = Mock()
        spreadsheet_resource = Mock()
        sheets.spreadsheets.return_value = spreadsheet_resource
        spreadsheet_resource.create.return_value.execute.return_value = {
            "spreadsheetId": "memory-spreadsheet-123"
        }
        spreadsheet_resource.get.return_value.execute.return_value = {
            "sheets": [
                {"properties": {"title": content_memory.SCAN_SHEET}},
                {"properties": {"title": content_memory.EXPERIMENT_SHEET}},
            ]
        }
        values_resource = Mock()
        spreadsheet_resource.values.return_value = values_resource
        values_resource.get.return_value.execute.return_value = {}
        values_resource.update.return_value.execute.return_value = {}

        store = content_memory.GoogleSheetsMemoryStore(drive, sheets)

        self.assertEqual(
            store.spreadsheet_url,
            "https://docs.google.com/spreadsheets/d/memory-spreadsheet-123/edit",
        )
        spreadsheet_resource.create.assert_called_once()
        self.assertEqual(values_resource.update.call_count, 2)
        header_rows = [
            call.kwargs["body"]["values"][0]
            for call in values_resource.update.call_args_list
        ]
        self.assertIn(list(content_memory.SCAN_HEADERS), header_rows)
        self.assertIn(list(content_memory.EXPERIMENT_HEADERS), header_rows)
        self.assertEqual(
            drive.files.return_value.list.call_args.kwargs["q"],
            "name = 'AI Simplified Lab Strategist Memory' and "
            "mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false",
        )

    def test_append_scan_writes_one_append_only_sheet_row(self):
        store = content_memory.GoogleSheetsMemoryStore.__new__(
            content_memory.GoogleSheetsMemoryStore
        )
        store.spreadsheet_id = "spreadsheet-123"
        values = Mock()
        values.append.return_value.execute.return_value = {}
        spreadsheets = Mock()
        spreadsheets.values.return_value = values
        store.sheets = Mock()
        store.sheets.spreadsheets.return_value = spreadsheets

        store.append_scan({
            "saved_at_utc": "2026-10-17T00:00:00+00:00",
            "period_start": "2026-10-05",
            "period_end": "2026-10-16",
            "uploads_in_window": 3,
            "measured_views_count": 2,
            "usable_ctr_count": 1,
            "candidate_count": 1,
            "candidates": [{"video_id": "v1", "ctr": None}],
            "action_brief": [],
        })

        values.append.assert_called_once()
        kwargs = values.append.call_args.kwargs
        self.assertEqual(kwargs["spreadsheetId"], "spreadsheet-123")
        self.assertEqual(kwargs["range"], "'Weekly Scans'!A1")
        self.assertIsNone(
            __import__("json").loads(kwargs["body"]["values"][0][7])[0]["ctr"]
        )

    def test_append_experiment_records_only_verified_status(self):
        store = content_memory.GoogleSheetsMemoryStore.__new__(
            content_memory.GoogleSheetsMemoryStore
        )
        store.spreadsheet_id = "spreadsheet-123"
        values = Mock()
        values.append.return_value.execute.return_value = {}
        spreadsheets = Mock()
        spreadsheets.values.return_value = values
        store.sheets = Mock()
        store.sheets.spreadsheets.return_value = spreadsheets

        store.append_experiment(
            video_id="v1",
            video_url="https://youtube.com/watch?v=v1",
            before={"title": "Old", "description": "Old description", "tags": ["old"]},
            proposed={"title": "New", "description": "New description", "tags": ["new"]},
            proposal_digest="digest",
            baseline_metrics={"views": 10, "ctr": None},
        )

        row = values.append.call_args.kwargs["body"]["values"][0]
        self.assertEqual(row[1], "v1")
        self.assertEqual(row[-1], "verified")
        self.assertEqual(row[3], "Old")
        self.assertEqual(row[4], "New")
