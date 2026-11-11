import csv
import io
import unittest
from openpyxl import Workbook

import core.batch_metadata as batch_metadata
import youtube_analytics_connector as connector


VIDEO_ID = "AbCdEfGhIjK"


class FakeRequest:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return self.callback()


class FakeVideos:
    def __init__(self, snippets):
        self.snippets = snippets
        self.calls = []

    def list(self, *, part, id):
        self.calls.append({"part": part, "id": id})

        def response():
            items = []
            for video_id in id.split(","):
                if video_id in self.snippets:
                    items.append({"id": video_id, "snippet": dict(self.snippets[video_id])})
            return {"items": items}

        return FakeRequest(response)


class FakeYoutube:
    def __init__(self, snippets):
        self._videos = FakeVideos(snippets)

    def videos(self):
        return self._videos


def make_snippet(**overrides):
    snippet = {
        "title": "Current title",
        "description": "Current description",
        "tags": ["old tag", "keep me"],
        "categoryId": "28",
        "channelId": connector.CHANNEL_ID,
        "defaultLanguage": "en",
        "defaultAudioLanguage": "en-IN",
    }
    snippet.update(overrides)
    return snippet


class BatchMetadataTests(unittest.TestCase):
    def test_parse_csv_with_video_url_and_semicolon_tags(self):
        payload = (
            "video_url,new_title,description,tags\n"
            f"https://www.youtube.com/watch?v={VIDEO_ID},New title,,tag one;tag two\n"
        ).encode("utf-8")
        parsed = batch_metadata.parse_metadata_file("changes.csv", payload)

        row = parsed["rows"][0]
        self.assertEqual(row["video_id"], VIDEO_ID)
        self.assertEqual(row["title"], "New title")
        self.assertIsNone(row["description"])
        self.assertEqual(row["tags"], ["tag one", "tag two"])
        self.assertEqual(row["status"], "parsed")
        self.assertEqual(parsed["fingerprint"], __import__("hashlib").sha256(payload).hexdigest())

    def test_parse_video_id_url_variants(self):
        self.assertEqual(batch_metadata.parse_video_reference(VIDEO_ID), VIDEO_ID)
        self.assertEqual(
            batch_metadata.parse_video_reference(f"https://youtu.be/{VIDEO_ID}?feature=share"),
            VIDEO_ID,
        )
        self.assertEqual(
            batch_metadata.parse_video_reference(f"https://www.youtube.com/shorts/{VIDEO_ID}"),
            VIDEO_ID,
        )

    def test_rejects_untrusted_non_youtube_urls(self):
        with self.assertRaisesRegex(ValueError, "YouTube video ID"):
            batch_metadata.parse_video_reference("https://example.com/watch?v=abcdefghijk")

    def test_json_tag_array_preserves_commas_inside_tag(self):
        self.assertEqual(
            batch_metadata.parse_tags_cell('["AI, explained", "machine learning"]'),
            ["AI, explained", "machine learning"],
        )
        self.assertEqual(batch_metadata.parse_tags_cell("[]"), [])

    def test_duplicate_video_ids_invalidate_both_rows(self):
        payload = (
            "video_id,title\n"
            f"{VIDEO_ID},First\n"
            f"{VIDEO_ID},Second\n"
        ).encode()
        parsed = batch_metadata.parse_metadata_file("changes.csv", payload)

        self.assertEqual([row["status"] for row in parsed["rows"]], ["invalid", "invalid"])
        self.assertTrue(all("Duplicate video ID" in row["errors"][0] for row in parsed["rows"]))

    def test_excel_import_reads_first_sheet(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Video ID", "New Title", "New Description", "Tags"])
        sheet.append([VIDEO_ID, "Excel title", None, "one | two"])
        stream = io.BytesIO()
        workbook.save(stream)

        parsed = batch_metadata.parse_metadata_file("changes.xlsx", stream.getvalue())
        row = parsed["rows"][0]
        self.assertEqual(row["video_id"], VIDEO_ID)
        self.assertEqual(row["title"], "Excel title")
        self.assertEqual(row["tags"], ["one", "two"])

    def test_25_video_batch_fetches_owner_snippets_in_one_youtube_request(self):
        video_ids = [f"V{index:010d}" for index in range(25)]
        rows = [{
            "row_number": index + 1,
            "video_id": video_id,
            "status": "parsed",
            "title": f"Updated title {index + 1}",
            "exact_title": f"Updated title {index + 1}",
            "title_intent": "exact",
            "description_intent": "unmentioned",
            "tags_intent": "unmentioned",
            "errors": [],
        } for index, video_id in enumerate(video_ids)]
        snippets = {
            video_id: make_snippet(title=f"Current title {index + 1}")
            for index, video_id in enumerate(video_ids)
        }
        youtube = FakeYoutube(snippets)

        staged = batch_metadata.prepare_metadata_batch(youtube, {"rows": rows})

        self.assertEqual(len(staged["rows"]), 25)
        self.assertTrue(all(row["status"] == "ready" for row in staged["rows"]))
        self.assertEqual(len(youtube._videos.calls), 1)
        self.assertEqual(
            youtube._videos.calls[0]["id"].split(","),
            video_ids,
        )

    def test_prepare_batch_reuses_preloaded_owner_verified_snippets(self):
        rows = [{
            "video_id": VIDEO_ID,
            "status": "parsed",
            "title": "Updated title",
            "exact_title": "Updated title",
            "title_intent": "exact",
            "description_intent": "unmentioned",
            "tags_intent": "unmentioned",
            "errors": [],
        }]
        youtube = FakeYoutube({VIDEO_ID: make_snippet()})
        snippets, fetch_errors = batch_metadata.fetch_owner_verified_snippets(youtube, rows)

        staged = batch_metadata.prepare_metadata_batch(
            youtube,
            {"rows": rows},
            preloaded_snippets=snippets,
            preloaded_fetch_errors=fetch_errors,
        )

        self.assertEqual(staged["rows"][0]["status"], "ready")
        self.assertEqual(len(youtube._videos.calls), 1)

    def test_prepare_batch_owner_verifies_and_preserves_blank_fields(self):
        youtube = FakeYoutube({VIDEO_ID: make_snippet()})
        payload = f"video_id,title,description,tags\n{VIDEO_ID},Updated title,,\n".encode()
        parsed = batch_metadata.parse_metadata_file("changes.csv", payload)

        staged = batch_metadata.prepare_metadata_batch(youtube, parsed)
        row = staged["rows"][0]

        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["changed_fields"], ["title"])
        self.assertEqual(row["before"]["description"], "Current description")
        self.assertEqual(row["proposed"]["description"], "Current description")
        self.assertEqual(row["proposed"]["tags"], ["old tag", "keep me"])
        self.assertEqual(len(row["digest"]), 64)
        self.assertEqual(len(youtube._videos.calls), 1)

    def test_prepare_batch_refuses_non_owner_video(self):
        youtube = FakeYoutube({VIDEO_ID: make_snippet(channelId="UCNotTheConfiguredChannel000000")})
        payload = f"video_id,title\n{VIDEO_ID},Updated title\n".encode()
        parsed = batch_metadata.parse_metadata_file("changes.csv", payload)

        staged = batch_metadata.prepare_metadata_batch(youtube, parsed)
        row = staged["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertIn("Ownership check failed", row["errors"][0])

    def test_prepare_batch_validates_youtube_title_limit(self):
        youtube = FakeYoutube({VIDEO_ID: make_snippet()})
        payload = f"video_id,title\n{VIDEO_ID},{'x' * 101}\n".encode()
        parsed = batch_metadata.parse_metadata_file("changes.csv", payload)

        staged = batch_metadata.prepare_metadata_batch(youtube, parsed)
        row = staged["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertIn("100 characters", row["errors"][0])

    def test_prepare_batch_marks_unchanged_proposal_without_write(self):
        youtube = FakeYoutube({VIDEO_ID: make_snippet()})
        payload = f"video_id,title\n{VIDEO_ID},Current title\n".encode()
        parsed = batch_metadata.parse_metadata_file("changes.csv", payload)

        staged = batch_metadata.prepare_metadata_batch(youtube, parsed)
        self.assertEqual(staged["rows"][0]["status"], "no_change")

    def test_row_limit_is_enforced(self):
        data = ["video_id,title"]
        data.extend(f"{VIDEO_ID[:-1]}{i % 10},Title {i}" for i in range(batch_metadata.BATCH_MAX_ROWS + 1))
        with self.assertRaisesRegex(ValueError, "more than 25"):
            batch_metadata.parse_metadata_file("changes.csv", ("\n".join(data) + "\n").encode())

    def test_results_csv_contains_status_and_video_url(self):
        batch = {
            "rows": [{
                "video_id": VIDEO_ID,
                "proposed_title": "Updated title",
                "status": "verified",
                "changed_fields": ["title", "tags"],
                "errors": [],
                "result_message": "YouTube read-back matched.",
            }]
        }
        rows = list(csv.DictReader(io.StringIO(batch_metadata.build_results_csv(batch))))
        self.assertEqual(rows[0]["status"], "verified")
        self.assertEqual(rows[0]["video_url"], f"https://www.youtube.com/watch?v={VIDEO_ID}")
        self.assertEqual(rows[0]["changed_fields"], "title, tags")




class AtomicBatchPublishingTests(unittest.TestCase):
    def _rows(self):
        rows = []
        for video_id in ("Video000001", "Video000002"):
            before = {"title": "Old " + video_id, "description": "Old description", "tags": ["old"], "categoryId": "28"}
            proposed = {**before, "title": "New " + video_id, "description": "New description", "tags": ["new"]}
            row = {"video_id": video_id, "before": before, "proposed": proposed, "status": "ready"}
            row["digest"] = connector.metadata_proposal_digest(video_id, before, proposed)
            rows.append(row)
        return rows


    def test_partial_selection_is_blocked_before_any_youtube_read_or_write(self):
        rows = self._rows()
        fingerprint = batch_metadata.compute_batch_approval_fingerprint([rows[0]], review_revision=1)
        with patch.object(batch_metadata.connector, "fetch_video_snippet") as fetch, patch.object(
            batch_metadata.connector, "apply_approved_dashboard_proposal"
        ) as apply:
            result = batch_metadata.publish_approved_batch_rows_atomic(
                object(), rows, [rows[0]["video_id"]], approved=True,
                approval_fingerprint=fingerprint,
            )
        self.assertTrue(result["blocked"])
        fetch.assert_not_called()
        apply.assert_not_called()

    def test_preflight_failure_means_zero_writes(self):
        rows = self._rows()
        fingerprint = batch_metadata.compute_batch_approval_fingerprint(rows, review_revision=1)
        with patch.object(batch_metadata.connector, "fetch_video_snippet", side_effect=[
            {**rows[0]["before"], "channelId": batch_metadata.connector.CHANNEL_ID},
            {**rows[1]["before"], "title": "Externally changed", "channelId": batch_metadata.connector.CHANNEL_ID},
        ]), patch.object(batch_metadata.connector, "apply_approved_dashboard_proposal") as apply:
            result = batch_metadata.publish_approved_batch_rows_atomic(
                object(), rows, [r["video_id"] for r in rows], approved=True,
                approval_fingerprint=fingerprint,
            )
        apply.assert_not_called()
        self.assertEqual(result["success_count"], 0)
        self.assertTrue(all(r["status"] == "failed" for r in rows))

    def test_second_video_failure_rolls_back_first_and_leaves_no_ready_rows(self):
        rows = self._rows()
        fingerprint = batch_metadata.compute_batch_approval_fingerprint(rows, review_revision=1)
        live = {
            r["video_id"]: {**r["before"], "channelId": batch_metadata.connector.CHANNEL_ID}
            for r in rows
        }
        def fetch(_youtube, video_id):
            return dict(live[video_id])
        def apply(_youtube, video_id, before, proposed, _digest, **_kwargs):
            if video_id == rows[1]["video_id"] and proposed["title"].startswith("New"):
                raise RuntimeError("simulated YouTube write failure")
            live[video_id] = {**proposed, "channelId": batch_metadata.connector.CHANNEL_ID}
            return {"result": "success"}
        with patch.object(batch_metadata.connector, "fetch_video_snippet", side_effect=fetch), patch.object(
            batch_metadata.connector, "apply_approved_dashboard_proposal", side_effect=apply
        ):
            result = batch_metadata.publish_approved_batch_rows_atomic(
                object(), rows, [r["video_id"] for r in rows], approved=True,
                approval_fingerprint=fingerprint,
            )
        self.assertEqual(result["success_count"], 0)
        self.assertTrue(result["rolled_back"])
        self.assertFalse(result["rollback_failed"])
        self.assertTrue(all(r["status"] == "rolled_back" for r in rows))
        self.assertTrue(all(r["status"] not in {"ready", "approved", "publishing"} for r in rows))
        for row in rows:
            self.assertEqual(live[row["video_id"]]["title"], row["before"]["title"])

if __name__ == "__main__":
    unittest.main()
