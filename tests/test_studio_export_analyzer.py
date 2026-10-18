import unittest
from io import BytesIO

from openpyxl import Workbook

import studio_export_analyzer as studio


class StudioExportAnalyzerTests(unittest.TestCase):
    def test_parses_preamble_and_normalizes_video_performance_metrics(self):
        payload = (
            "YouTube Studio export\n"
            "Date range: Last 28 days\n"
            "Video title,Video ID,Views,Watch time (hours),Average view duration,"
            "Average percentage viewed,Impressions,Impressions click-through rate,"
            "Subscribers gained\n"
            "AI Basics,abcdefghijk,1000,5,0:32,35%,5000,2.5%,12\n"
            "Prompt Tips,zyxwvutsrqp,500,3,0:27,29%,2500,0.04,4\n"
        ).encode("utf-8")
        parsed = studio.parse_studio_export("content.csv", payload)
        report = studio.analyze_studio_export(parsed)

        self.assertEqual(parsed["report_type"], "video_performance")
        self.assertEqual(parsed["header_row"], 3)
        self.assertEqual(report["record_count"], 2)
        self.assertEqual(report["video_count"], 2)
        first = next(row for row in report["video_rows"] if row["video_id"] == "abcdefghijk")
        self.assertEqual(first["views"], 1000)
        self.assertEqual(first["watch_time_minutes"], 300)
        self.assertEqual(first["average_view_duration_seconds"], 32)
        self.assertEqual(first["average_view_percentage"], 35)
        self.assertEqual(first["ctr_percent"], 2.5)
        self.assertEqual(first["subscribers_gained"], 12)
        self.assertTrue(report["recommendations"])

    def test_parses_xlsx_workbook_and_normalizes_percent_cells(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Reach"
        worksheet.append(["YouTube Studio report"])
        worksheet.append([
            "Video title", "Video ID", "Views", "Impressions",
            "Impressions click-through rate", "Average percentage viewed",
        ])
        worksheet.append(["AI Basics", "abcdefghijk", 1000, 5000, 0.025, 0.35])
        worksheet["E3"].number_format = "0.00%"
        worksheet["F3"].number_format = "0.0%"
        payload = BytesIO()
        workbook.save(payload)

        parsed = studio.parse_studio_export("reach.xlsx", payload.getvalue())
        report = studio.analyze_studio_export(parsed)
        self.assertEqual(parsed["delimiter"], "excel")
        self.assertEqual(parsed["source_sheet"], "Reach")
        self.assertEqual(parsed["report_type"], "video_performance")
        self.assertEqual(report["video_count"], 1)
        video = report["video_rows"][0]
        self.assertEqual(video["video_id"], "abcdefghijk")
        self.assertEqual(video["ctr_percent"], 2.5)
        self.assertEqual(video["average_view_percentage"], 35.0)

    def test_parses_tsv_export_and_does_not_invent_missing_metrics(self):
        payload = (
            "Video title\tViews\tImpressions\tImpressions click-through rate\n"
            "One video\t250\t1000\t4.5%\n"
            "Two video\t200\t800\t3.2%\n"
            "Three video\t200\t900\t2.8%\n"
        ).encode("utf-8")
        parsed = studio.parse_studio_export("reach.tsv", payload)
        report = studio.analyze_studio_export(parsed)
        self.assertEqual(parsed["delimiter"], "tab")
        self.assertEqual(report["summary"]["views"], 650)
        self.assertNotIn("watch_time_minutes", report["summary"])
        self.assertTrue(any("CTR" in item["evidence"] for item in report["recommendations"]))

    def test_retention_report_finds_large_observed_adjacent_drops(self):
        payload = (
            "Audience retention export\n"
            "Elapsed video time,Audience retention,Relative audience retention\n"
            "0:00,100%,1.0\n"
            "0:10,88%,1.1\n"
            "0:20,70%,0.9\n"
            "0:30,61%,0.8\n"
            "0:40,58%,0.7\n"
        ).encode("utf-8")
        parsed = studio.parse_studio_export("retention.csv", payload)
        report = studio.analyze_studio_export(parsed)
        self.assertEqual(parsed["report_type"], "retention")
        self.assertEqual(len(report["retention_rows"]), 5)
        self.assertIn("18.0 percentage points", report["recommendations"][0]["evidence"])
        self.assertTrue(any("Relative retention" in item["title"]
                            for item in report["recommendations"]))

    def test_accepts_duration_as_hh_mm_ss(self):
        payload = (
            "Video title,Views,Average view duration\n"
            "A video,3,00:01:05\n"
        ).encode()
        parsed = studio.parse_studio_export("content.csv", payload)
        report = studio.analyze_studio_export(parsed)
        self.assertEqual(report["video_rows"][0]["average_view_duration_seconds"], 65)

    def test_rejects_unknown_or_unrecognized_exports(self):
        with self.assertRaises(studio.StudioExportError):
            studio.parse_studio_export("report.xlsx", b"not supported")
        with self.assertRaises(studio.StudioExportError):
            studio.parse_studio_export("report.csv", b"hello,world\nfoo,bar\n")

    def test_csv_download_cells_are_safe_for_spreadsheet_opening(self):
        self.assertEqual(studio.safe_csv_export_value("=HYPERLINK(\"bad\")"),
                         "'=HYPERLINK(\"bad\")")
        self.assertEqual(studio.safe_csv_export_value("@handle"), "'@handle")
        self.assertEqual(studio.safe_csv_export_value("Normal title"), "Normal title")
        self.assertEqual(studio.safe_csv_export_value(1234), 1234)

    def test_empty_file_returns_actionable_error(self):
        with self.assertRaisesRegex(studio.StudioExportError, "empty"):
            studio.parse_studio_export("report.csv", b"")

    def test_builds_session_evidence_index_without_summing_overlapping_files(self):
        base_video = {
            "video_id": "abcdefghijk",
            "video_title": "AI Basics",
            "views": 400,
            "watch_time_minutes": 200,
            "average_view_duration_seconds": 30,
            "average_view_percentage": 40,
            "impressions": 5000,
            "ctr_percent": 3.2,
            "subscribers_gained": 6,
            "subscribers_lost": 1,
            "date_start": "2026-01-01",
            "date_end": "2026-01-28",
            "row_count": 1,
        }
        reports = [
            {"filename": "first.csv", "report_type": "video_performance", "video_rows": [base_video]},
            {"filename": "second.csv", "report_type": "video_performance", "video_rows": [base_video]},
        ]
        index = studio.build_evidence_index(reports)
        by_id = studio.find_video_evidence(index, "abcdefghijk", "AI Basics")
        self.assertEqual(len(by_id), 2)
        self.assertEqual({item["source_file"] for item in by_id}, {"first.csv", "second.csv"})
        by_title = studio.find_video_evidence(index, None, "AI Basics")
        self.assertEqual(len(by_title), 2)

    def test_traffic_source_breakdown_surfaces_the_leading_reported_source(self):
        payload = (
            "Traffic source,Views,Watch time (hours)\n"
            "Shorts feed,500,20\n"
            "YouTube Search,200,15\n"
            "External,100,5\n"
        ).encode("utf-8")
        parsed = studio.parse_studio_export("traffic.csv", payload)
        report = studio.analyze_studio_export(parsed)
        self.assertEqual(report["report_type"], "traffic_sources")
        self.assertIn("Shorts feed", report["recommendations"][0]["title"])
        self.assertIn("62.5%", report["recommendations"][0]["evidence"])

    def test_total_row_is_not_double_counted_with_video_detail(self):
        payload = (
            "Video title,Views\n"
            "Video One,100\n"
            "Video Two,200\n"
            "Total,300\n"
        ).encode("utf-8")
        report = studio.analyze_studio_export(
            studio.parse_studio_export("content.csv", payload)
        )
        self.assertEqual(report["summary"]["views"], 300)
        self.assertEqual(report["video_count"], 2)

    def test_retention_curve_is_indexed_when_export_includes_video_title(self):
        payload = (
            "Video title,Elapsed video time,Audience retention\n"
            "AI Basics,0:00,100%\n"
            "AI Basics,0:10,82%\n"
            "AI Basics,0:20,65%\n"
        ).encode("utf-8")
        report = studio.analyze_studio_export(
            studio.parse_studio_export("retention.csv", payload)
        )
        index = studio.build_evidence_index([report])
        evidence = studio.find_video_evidence(index, None, "AI Basics")
        self.assertEqual(len(evidence), 1)
        self.assertEqual(len(evidence[0]["retention_curve"]), 3)
        self.assertEqual(evidence[0]["retention_curve"][1]["retention_percent"], 82)

    def test_manual_retention_attachment_indexes_curve_by_video_id(self):
        payload = (
            "Elapsed video time,Audience retention\n"
            "0:00,100%\n"
            "0:10,85%\n"
            "0:20,70%\n"
        ).encode("utf-8")
        report = studio.analyze_studio_export(
            studio.parse_studio_export("retention.csv", payload)
        )
        index = studio.build_evidence_index([report])
        studio.add_manual_retention_evidence(index, report, "https://youtu.be/abcdefghijk")
        evidence = studio.find_video_evidence(index, "abcdefghijk", None)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(len(evidence[0]["retention_curve"]), 3)
        self.assertEqual(evidence[0]["source_file"], "retention.csv")

    def test_opportunity_recommendation_keeps_measured_reach_for_ranking(self):
        videos = [
            {"video_title": "One", "video_id": "aaaaaaaaaaa", "views": 100,
             "average_view_percentage": 50.0, "ctr_percent": None, "impressions": None},
            {"video_title": "Two", "video_id": "bbbbbbbbbbb", "views": 200,
             "average_view_percentage": 60.0, "ctr_percent": None, "impressions": None},
            {"video_title": "Three", "video_id": "ccccccccccc", "views": 300,
             "average_view_percentage": 70.0, "ctr_percent": None, "impressions": None},
            {"video_title": "Four", "video_id": "ddddddddddd", "views": 400,
             "average_view_percentage": 80.0, "ctr_percent": None, "impressions": None},
        ]
        recommendations = studio._performance_recommendations(videos)
        opportunities = [
            item for item in recommendations if item["priority"] == "Opportunity"
        ]
        self.assertEqual(len(opportunities), 2)
        self.assertEqual(
            max(
                opportunities,
                key=lambda item: item["measured_metrics"]["views"],
            )["measured_metrics"]["views"],
            400,
        )

    def test_low_ctr_recommendation_uses_only_measured_impressions(self):
        videos = [
            {"video_title": "One", "video_id": "aaaaaaaaaaa", "ctr_percent": 2.0, "impressions": 10000,
             "views": 200, "average_view_percentage": None},
            {"video_title": "Two", "video_id": "bbbbbbbbbbb", "ctr_percent": 3.2, "impressions": 5000,
             "views": 160, "average_view_percentage": None},
            {"video_title": "Three", "video_id": "ccccccccccc", "ctr_percent": 5.0, "impressions": 3000,
             "views": 150, "average_view_percentage": None},
            {"video_title": "Not enough impressions", "video_id": "ddddddddddd", "ctr_percent": 0.2,
             "impressions": 30, "views": 1, "average_view_percentage": None},
        ]
        recommendations = studio._performance_recommendations(videos)
        self.assertTrue(any(item.get("video_id") == "aaaaaaaaaaa" for item in recommendations))
        self.assertFalse(any(item.get("video_id") == "ddddddddddd" for item in recommendations))

    def test_studio_table_data_csv_with_content_and_video_title(self):
        payload = (
            "Content,Video title,Video publish time,Views,Watch time (hours),Subscribers,Impressions,Impressions click-through rate (%)\n"
            "WCJF5TmXpuU,LLM Prompt Caching Explained,2026-01-10,1200,45.5,24,8000,4.2\n"
            "AbCdEfGhIjK,Semantic Search in Practice,2026-01-12,300,12.0,5,3000,1.8\n"
            "XyZ12345678,Vector DB Fundamentals,2026-01-14,900,30.0,15,6000,3.5\n"
        ).encode("utf-8")
        parsed = studio.parse_studio_export("Table data.csv", payload)
        report = studio.analyze_studio_export(parsed)

        self.assertEqual(parsed["report_type"], "video_performance")
        self.assertEqual(report["video_count"], 3)
        first = next(v for v in report["video_rows"] if v["video_id"] == "WCJF5TmXpuU")
        self.assertEqual(first["video_title"], "LLM Prompt Caching Explained")
        self.assertEqual(first["ctr_percent"], 4.2)
        self.assertEqual(first["views"], 1200)

        # Check that CTR underperformance recommendation was ranked
        rec = next(r for r in report["recommendations"] if r.get("video_id") == "AbCdEfGhIjK")
        self.assertIn("Semantic Search in Practice", rec["title"])
        self.assertEqual(rec["priority"], "High")

        # Check diagnostics
        diag = report["diagnostics"]
        self.assertTrue(diag["qualifications"]["ctr"]["met"])
        self.assertEqual(diag["qualifications"]["ctr"]["status"], "Qualified")

    def test_low_ctr_under_one_percent_is_not_scaled_when_header_has_percent(self):
        payload = (
            "Video title,Impressions,Impressions click-through rate (%)\n"
            "Video Low,2000,0.8\n"
            "Video Med,3000,3.5\n"
        ).encode("utf-8")
        parsed = studio.parse_studio_export("reach.csv", payload)
        report = studio.analyze_studio_export(parsed)
        low_vid = next(v for v in report["video_rows"] if v["video_title"] == "Video Low")
        # 0.8% CTR should remain 0.8, not get scaled to 80.0
        self.assertEqual(low_vid["ctr_percent"], 0.8)

    def test_table_data_views_only_generates_actionable_recommendations(self):
        payload = (
            "Content,Video title,Views,Watch time (hours)\n"
            "vid11111111,Top Upload,1500,60\n"
            "vid22222222,Medium Upload,800,30\n"
            "vid33333333,Low Upload,100,4\n"
        ).encode("utf-8")
        parsed = studio.parse_studio_export("Table data.csv", payload)
        report = studio.analyze_studio_export(parsed)

        # Views-only ranking should generate both opportunity and review actions
        self.assertTrue(any(r["priority"] == "Opportunity" for r in report["recommendations"]))
        self.assertTrue(any(r["priority"] == "Review" for r in report["recommendations"]))
        low_rec = next(r for r in report["recommendations"] if r.get("video_id") == "vid33333333")
        self.assertIn("Low Upload", low_rec["title"])

        diag = report["diagnostics"]
        self.assertTrue(diag["qualifications"]["views"]["met"])
        self.assertFalse(diag["qualifications"]["ctr"]["met"])


if __name__ == "__main__":
    unittest.main()
