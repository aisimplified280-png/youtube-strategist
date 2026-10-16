import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from googleapiclient.errors import HttpError

import youtube_analytics_connector as connector


def make_snippet(**overrides):
    snippet = {
        "title": "Original title",
        "description": "Opening paragraph.\n\nDetails stay here.",
        "tags": ["existing", "keep this tag"],
        "categoryId": "22",
        "channelId": connector.CHANNEL_ID,
        "defaultLanguage": "en",
        "defaultAudioLanguage": "en",
        "localized": {"title": "Localized title"},
    }
    snippet.update(overrides)
    return snippet


class FakeRequest:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return self.callback()


class FakeVideos:
    def __init__(self, snippets):
        self.snippets = snippets
        self.write_calls = []

    def list(self, *, part, id):
        assert "snippet" in part

        def response():
            result = []
            for video_id in id.split(","):
                snippet = self.snippets.get(video_id)
                if snippet is None:
                    continue
                result.append(
                    {
                        "id": video_id,
                        "snippet": snippet.copy(),
                        "contentDetails": {"duration": "PT45S"},
                        "statistics": {"viewCount": "1000"},
                    }
                )
            return {"items": result}

        return FakeRequest(response)

    def update(self, *, part, body):
        self.write_calls.append({"part": part, "body": body})

        def response():
            self.snippets[body["id"]].update(body["snippet"])
            return {"id": body["id"], "snippet": body["snippet"]}

        return FakeRequest(response)


class FakeYoutube:
    def __init__(self, snippets):
        self._videos = FakeVideos(snippets)
        self._snippets = snippets

    def videos(self):
        return self._videos

    def channels(self):
        return self

    def playlistItems(self):
        return self

    def list(self, **kwargs):
        if "id" in kwargs:
            return FakeRequest(
                lambda: {
                    "items": [
                        {
                            "id": connector.CHANNEL_ID,
                            "contentDetails": {
                                "relatedPlaylists": {"uploads": "uploads"}
                            },
                        }
                    ]
                }
            )
        return FakeRequest(
            lambda: {
                "items": [
                    {"contentDetails": {"videoId": video_id}}
                    for video_id in self._snippets
                ]
            }
        )


class FakeAnalytics:
    def __init__(self):
        self.requests = []

    def reports(self):
        return self

    def query(self, **kwargs):
        self.requests.append(kwargs)

        def response():
            if kwargs.get("dimensions") == "insightTrafficSourceType":
                return {
                    "columnHeaders": [
                        {"name": "insightTrafficSourceType"},
                        {"name": "views"},
                        {"name": "estimatedMinutesWatched"},
                    ],
                    "rows": [["SHORTS", 120, 18]],
                }
            if kwargs.get("dimensions") == "video":
                return {
                    "columnHeaders": [
                        {"name": "video"},
                        {"name": "views"},
                        {"name": "estimatedMinutesWatched"},
                        {"name": "averageViewDuration"},
                        {"name": "subscribersGained"},
                    ],
                    "rows": [],
                }
            return {
                "columnHeaders": [
                    {"name": "views"},
                    {"name": "estimatedMinutesWatched"},
                    {"name": "averageViewDuration"},
                    {"name": "likes"},
                    {"name": "comments"},
                    {"name": "shares"},
                    {"name": "subscribersGained"},
                    {"name": "subscribersLost"},
                ],
                "rows": [[120, 18, 9, 4, 2, 1, 1, 0]],
            }

        return FakeRequest(response)


class MetadataConnectorTests(unittest.TestCase):
    def test_prepare_update_preserves_unmodified_snippet_fields(self):
        original = make_snippet()
        youtube = FakeYoutube({"video-1": original})

        plan = connector.prepare_metadata_update(
            youtube,
            {
                "id": "video-1",
                "title": "New title",
                "description_append": "New paragraph.",
                "tags": ["existing", "keep this tag", "new tag"],
            },
        )

        self.assertEqual(plan["proposed_snippet"]["title"], "New title")
        self.assertEqual(
            plan["proposed_snippet"]["description"],
            "Opening paragraph.\n\nDetails stay here.\n\nNew paragraph.",
        )
        self.assertEqual(
            plan["proposed_snippet"]["tags"],
            ["existing", "keep this tag", "new tag"],
        )
        self.assertEqual(plan["proposed_snippet"]["categoryId"], "22")
        self.assertEqual(plan["proposed_snippet"]["defaultLanguage"], "en")
        self.assertEqual(plan["proposed_snippet"]["defaultAudioLanguage"], "en")
        self.assertEqual(
            plan["proposed_snippet"]["localized"], {"title": "Localized title"}
        )
        self.assertEqual(original["title"], "Original title")

    def test_dry_run_never_writes_to_youtube_or_audit_log(self):
        youtube = FakeYoutube({"video-1": make_snippet()})
        with TemporaryDirectory() as temp_dir:
            audit_log = Path(temp_dir) / "audit.jsonl"
            plan = connector.prepare_metadata_update(
                youtube, {"id": "video-1", "title": "A better title"}
            )

            result = connector.execute_metadata_updates(
                youtube,
                [plan],
                dry_run=True,
                confirmed_ids={"video-1"},
                audit_log=audit_log,
            )

            self.assertEqual(result, {"video-1": "dry run"})
            self.assertEqual(youtube._videos.write_calls, [])
            self.assertFalse(audit_log.exists())

    def test_confirmed_update_preserves_other_fields_verifies_and_audits(self):
        youtube = FakeYoutube({"video-1": make_snippet()})
        plan = connector.prepare_metadata_update(
            youtube, {"id": "video-1", "title": "A better title"}
        )

        with TemporaryDirectory() as temp_dir:
            audit_log = Path(temp_dir) / "audit.jsonl"
            result = connector.execute_metadata_updates(
                youtube,
                [plan],
                dry_run=False,
                confirmed_ids={"video-1"},
                audit_log=audit_log,
            )

            self.assertEqual(result, {"video-1": "success"})
            self.assertEqual(len(youtube._videos.write_calls), 1)
            write = youtube._videos.write_calls[0]
            self.assertEqual(write["part"], "snippet")
            self.assertEqual(write["body"]["id"], "video-1")
            self.assertEqual(write["body"]["snippet"]["title"], "A better title")
            self.assertEqual(write["body"]["snippet"]["categoryId"], "22")
            self.assertEqual(
                write["body"]["snippet"]["description"],
                make_snippet()["description"],
            )
            self.assertEqual(
                write["body"]["snippet"]["tags"], make_snippet()["tags"]
            )
            self.assertEqual(write["body"]["snippet"]["defaultLanguage"], "en")
            self.assertNotIn("status", write["body"])

            audit_contents = audit_log.read_text(encoding="utf-8")
            entry = json.loads(audit_contents)
            self.assertEqual(entry["video_id"], "video-1")
            self.assertEqual(entry["before"]["title"], "Original title")
            self.assertEqual(entry["after"]["title"], "A better title")
            self.assertEqual(entry["result"], "success")
            self.assertIsNone(entry["proposal_id"])
            self.assertNotIn("token", audit_contents.lower())

    def test_verification_retries_stale_read_and_ignores_tag_order(self):
        youtube = FakeYoutube({"video-1": make_snippet()})
        plan = connector.prepare_metadata_update(
            youtube, {"id": "video-1", "title": "A better title"}
        )
        delayed_result = make_snippet(
            title="A better title",
            tags=list(reversed(make_snippet()["tags"])),
        )

        with TemporaryDirectory() as temp_dir:
            audit_log = Path(temp_dir) / "audit.jsonl"
            with patch.object(
                connector,
                "fetch_video_snippet",
                side_effect=[make_snippet(), delayed_result],
            ) as fetch:
                with patch.object(connector.time, "sleep") as sleep:
                    result = connector.execute_metadata_updates(
                        youtube,
                        [plan],
                        dry_run=False,
                        confirmed_ids={"video-1"},
                        audit_log=audit_log,
                    )

            self.assertEqual(result, {"video-1": "success"})
            self.assertEqual(fetch.call_count, 2)
            sleep.assert_called_once_with(1)
            entry = json.loads(audit_log.read_text(encoding="utf-8"))
            self.assertEqual(entry["result"], "success")
            self.assertEqual(entry["after"]["title"], "A better title")

    def test_api_write_error_is_audited_and_propagated(self):
        youtube = FakeYoutube({"video-1": make_snippet()})
        plan = connector.prepare_metadata_update(
            youtube, {"id": "video-1", "title": "A better title"}
        )
        error = HttpError(
            SimpleNamespace(status=403, reason="Forbidden"),
            b'{"error": {"message": "forbidden"}}',
        )

        with TemporaryDirectory() as temp_dir:
            audit_log = Path(temp_dir) / "audit.jsonl"
            with patch.object(youtube._videos, "update", side_effect=error):
                with self.assertRaises(HttpError):
                    connector.execute_metadata_updates(
                        youtube,
                        [plan],
                        dry_run=False,
                        confirmed_ids={"video-1"},
                        audit_log=audit_log,
                    )

            entry = json.loads(audit_log.read_text(encoding="utf-8"))
            self.assertEqual(entry["result"], "update failed (HTTP 403)")
            self.assertEqual(entry["before"]["title"], "Original title")
            self.assertIsNone(entry["after"])
            self.assertEqual(entry["approved_changes"]["title"], "A better title")

    def test_transport_write_error_is_audited_without_blind_retry(self):
        youtube = FakeYoutube({"video-1": make_snippet()})
        plan = connector.prepare_metadata_update(
            youtube, {"id": "video-1", "title": "A better title"}
        )

        def fail_update(**_kwargs):
            self.assertEqual(_kwargs["part"], "snippet")
            self.assertEqual(_kwargs["body"]["id"], "video-1")

            def execute():
                raise OSError("transport detail")

            return FakeRequest(execute)

        with TemporaryDirectory() as temp_dir:
            audit_log = Path(temp_dir) / "audit.jsonl"
            with patch.object(
                youtube._videos,
                "update",
                side_effect=fail_update,
            ):
                with self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
                    connector.execute_metadata_updates(
                        youtube,
                        [plan],
                        dry_run=False,
                        confirmed_ids={"video-1"},
                        audit_log=audit_log,
                    )

            entry = json.loads(audit_log.read_text(encoding="utf-8"))
            self.assertEqual(
                entry["result"],
                "update outcome unknown (transport failure)",
            )
            self.assertIsNone(entry["after"])

    def test_verification_transport_error_is_audited_without_retry(self):
        youtube = FakeYoutube({"video-1": make_snippet()})
        plan = connector.prepare_metadata_update(
            youtube, {"id": "video-1", "title": "A better title"}
        )

        with TemporaryDirectory() as temp_dir:
            audit_log = Path(temp_dir) / "audit.jsonl"
            with patch.object(
                connector,
                "fetch_video_snippet",
                side_effect=OSError("verification transport detail"),
            ):
                with self.assertRaisesRegex(RuntimeError, "may have succeeded"):
                    connector.execute_metadata_updates(
                        youtube,
                        [plan],
                        dry_run=False,
                        confirmed_ids={"video-1"},
                        audit_log=audit_log,
                    )

            self.assertEqual(len(youtube._videos.write_calls), 1)
            entry = json.loads(audit_log.read_text(encoding="utf-8"))
            self.assertEqual(
                entry["result"],
                "verification failed (transport failure; update outcome unknown)",
            )
            self.assertIsNone(entry["after"])

    def test_staging_and_wrong_proposal_id_never_write(self):
        youtube = FakeYoutube({"video-1": make_snippet()})
        proposal = {
            "title": "Reviewed title",
            "description": "Opening paragraph.\n\nDetails stay here.\n\n#AI",
            "tags": ["existing", "keep this tag"],
        }
        with TemporaryDirectory() as temp_dir:
            with patch.object(
                connector, "PENDING_PROPOSALS_DIR", Path(temp_dir) / "proposals"
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    proposal_id = connector.stage_metadata_proposal(
                        youtube, "video-1", proposal
                    )
                self.assertIn(f"Proposal ID: {proposal_id}", output.getvalue())
                self.assertEqual(youtube._videos.write_calls, [])
                with self.assertRaises(FileNotFoundError):
                    connector.apply_staged_proposal(
                        youtube, "0" * 20, audit_log=Path(temp_dir) / "audit.jsonl"
                    )
                self.assertEqual(youtube._videos.write_calls, [])

    def test_dashboard_approval_is_digest_bound_and_audits_in_memory(self):
        youtube = FakeYoutube({"video-1": make_snippet()})
        before = make_snippet()
        proposed = dict(before, title="Approved dashboard title")
        digest = connector.metadata_proposal_digest("video-1", before, proposed)
        audit_entries = []

        with self.assertRaisesRegex(
            connector.MetadataValidationError, "does not match"
        ):
            connector.apply_approved_dashboard_proposal(
                youtube,
                "video-1",
                before,
                dict(proposed, title="Different title"),
                digest,
                audit_callback=audit_entries.append,
            )
        self.assertEqual(youtube._videos.write_calls, [])

        result = connector.apply_approved_dashboard_proposal(
            youtube,
            "video-1",
            before,
            proposed,
            digest,
            audit_callback=audit_entries.append,
        )
        self.assertEqual(result["result"], "success")
        self.assertEqual(result["video_id"], "video-1")
        self.assertEqual(len(youtube._videos.write_calls), 1)
        self.assertEqual(youtube._videos.write_calls[0]["body"]["snippet"]["categoryId"], "22")
        self.assertNotIn("channelId", youtube._videos.write_calls[0]["body"]["snippet"])
        self.assertEqual(len(audit_entries), 1)
        self.assertEqual(audit_entries[0]["proposal_id"], digest[:20])
        self.assertEqual(audit_entries[0]["result"], "success")

    def test_revised_proposal_invalidates_old_approval_and_only_latest_applies(self):
        youtube = FakeYoutube({"video-1": make_snippet()})
        with TemporaryDirectory() as temp_dir:
            proposals_dir = Path(temp_dir) / "proposals"
            audit_log = Path(temp_dir) / "audit.jsonl"
            with patch.object(connector, "PENDING_PROPOSALS_DIR", proposals_dir):
                first_id = connector.stage_metadata_proposal(
                    youtube,
                    "video-1",
                    {
                        "title": "First proposal",
                        "description": make_snippet()["description"],
                        "tags": make_snippet()["tags"],
                    },
                )
                latest_id = connector.stage_metadata_proposal(
                    youtube,
                    "video-1",
                    {
                        "title": "Revised proposal",
                        "description": make_snippet()["description"],
                        "tags": make_snippet()["tags"],
                    },
                )
                self.assertNotEqual(first_id, latest_id)
                self.assertFalse((proposals_dir / f"{first_id}.json").exists())
                with self.assertRaises(FileNotFoundError):
                    connector.apply_staged_proposal(
                        youtube, first_id, audit_log=audit_log
                    )
                self.assertEqual(youtube._videos.write_calls, [])

                result = connector.apply_staged_proposal(
                    youtube, latest_id, audit_log=audit_log
                )

            self.assertEqual(result["result"], "success")
            self.assertEqual(result["video_id"], "video-1")
            self.assertEqual(result["title"], "Revised proposal")
            self.assertEqual(len(youtube._videos.write_calls), 1)
            body = youtube._videos.write_calls[0]["body"]
            self.assertEqual(body["snippet"]["title"], "Revised proposal")
            self.assertEqual(body["snippet"]["categoryId"], "22")
            self.assertNotIn("channelId", body["snippet"])
            audit_entry = json.loads(audit_log.read_text(encoding="utf-8"))
            self.assertEqual(audit_entry["proposal_id"], latest_id)

    def test_changed_video_after_proposal_is_not_overwritten(self):
        youtube = FakeYoutube({"video-1": make_snippet()})
        with TemporaryDirectory() as temp_dir:
            with patch.object(
                connector, "PENDING_PROPOSALS_DIR", Path(temp_dir) / "proposals"
            ):
                proposal_id = connector.stage_metadata_proposal(
                    youtube,
                    "video-1",
                    {
                        "title": "Proposed title",
                        "description": make_snippet()["description"],
                        "tags": make_snippet()["tags"],
                    },
                )
                youtube._snippets["video-1"]["description"] = "External edit"
                with self.assertRaisesRegex(RuntimeError, "changed since"):
                    connector.apply_staged_proposal(
                        youtube,
                        proposal_id,
                        audit_log=Path(temp_dir) / "audit.jsonl",
                    )
                self.assertEqual(youtube._videos.write_calls, [])

    def test_read_only_analysis_reports_performance_sources_and_unavailable_ctr(self):
        video_id = "abcdefghijk"
        peer_id = "lmnopqrstuv"
        youtube = FakeYoutube(
            {
                video_id: make_snippet(publishedAt="2026-10-01T00:00:00Z"),
                peer_id: make_snippet(
                    title="Peer video",
                    publishedAt="2026-10-02T00:00:00Z",
                ),
            }
        )
        analytics = FakeAnalytics()

        with TemporaryDirectory() as temp_dir:
            with patch.object(connector, "OUTPUT_DIR", Path(temp_dir)):
                result = connector.analyze_videos(youtube, analytics, video_id)

        analyzed = result["videos"][0]
        self.assertEqual(analyzed["analytics"]["views"], 120)
        self.assertEqual(analyzed["analytics"]["likes"], 4)
        self.assertEqual(
            analyzed["traffic_sources"]["rows"][0]["insightTrafficSourceType"],
            "SHORTS",
        )
        self.assertEqual(analyzed["comparison"]["matched_cohort_rows"], 1)
        self.assertEqual(analyzed["comparison"]["metrics_median"]["views"], 120)
        self.assertIsNone(analyzed["impressions"]["value"])
        self.assertIsNone(analyzed["click_through_rate"]["value"])
        self.assertEqual(analyzed["video"]["duration"]["format"], "undetermined")
        self.assertTrue(all("impressions" not in request["metrics"] for request in analytics.requests))
        self.assertEqual(youtube._videos.write_calls, [])

    def test_resolves_video_title_and_common_youtube_urls(self):
        youtube = FakeYoutube({"video-1": make_snippet()})

        resolved = connector._resolve_video(youtube, "Original title")

        self.assertEqual(resolved["id"], "video-1")
        self.assertEqual(
            connector._parse_video_reference(
                "https://www.youtube.com/shorts/abcdefghijk"
            ),
            "abcdefghijk",
        )
        self.assertEqual(
            connector._parse_video_reference("youtu.be/abcdefghijk"),
            "abcdefghijk",
        )
        self.assertEqual(youtube._videos.write_calls, [])

    def test_batch_shows_all_proposals_and_applies_only_selected_video(self):
        youtube = FakeYoutube(
            {
                "video-1": make_snippet(),
                "video-2": make_snippet(title="Second title"),
            }
        )
        updates = [
            {"id": "video-1", "title": "First improved title"},
            {"id": "video-2", "title": "Second improved title"},
        ]
        with TemporaryDirectory() as temp_dir:
            audit_log = Path(temp_dir) / "audit.jsonl"
            output = io.StringIO()
            with patch.object(connector, "AUDIT_LOG_FILE", audit_log):
                with patch("builtins.input", side_effect=["video-1", "APPLY"]):
                    with redirect_stdout(output):
                        connector._run_metadata_updates(
                            youtube, updates, apply=True
                        )

        self.assertIn("First improved title", output.getvalue())
        self.assertIn("Second improved title", output.getvalue())
        self.assertEqual(len(youtube._videos.write_calls), 1)
        self.assertEqual(youtube._videos.write_calls[0]["body"]["id"], "video-1")

    def test_description_replacement_requires_explicit_opt_in(self):
        youtube = FakeYoutube({"video-1": make_snippet()})

        with self.assertRaisesRegex(
            connector.MetadataValidationError, "allow_description_replacement"
        ):
            connector.prepare_metadata_update(
                youtube,
                {"id": "video-1", "description": "Entirely new description"},
            )

        plan = connector.prepare_metadata_update(
            youtube,
            {
                "id": "video-1",
                "description": "Entirely new description",
                "allow_description_replacement": True,
            },
        )
        self.assertEqual(
            plan["proposed_snippet"]["description"],
            "Entirely new description",
        )

    def test_existing_tags_cannot_be_removed_without_explicit_opt_in(self):
        youtube = FakeYoutube({"video-1": make_snippet()})

        with self.assertRaisesRegex(
            connector.MetadataValidationError, "allow_tag_removal"
        ):
            connector.prepare_metadata_update(
                youtube, {"id": "video-1", "tags": ["new tag"]}
            )

        plan = connector.prepare_metadata_update(
            youtube,
            {
                "id": "video-1",
                "tags": ["new tag"],
                "allow_tag_removal": True,
            },
        )
        self.assertEqual(plan["proposed_snippet"]["tags"], ["new tag"])

    def test_invalid_metadata_and_unsupported_fields_are_rejected(self):
        cases = [
            ({"title": "x" * 101}, "100 characters"),
            ({"description_append": "x" * 5001}, "5000 characters"),
            (
                {"description_append": " ".join(f"#tag{i}" for i in range(16))},
                "15 hashtags",
            ),
            (
                {"description_append": "#invalid-tag"},
                "letters, numbers, or underscores",
            ),
            ({"tags": [""]}, "non-empty strings"),
            ({"privacyStatus": "public"}, "unsupported fields"),
        ]
        for change, message in cases:
            with self.subTest(change=change):
                youtube = FakeYoutube({"video-1": make_snippet()})
                with self.assertRaisesRegex(
                    connector.MetadataValidationError, message
                ):
                    connector.prepare_metadata_update(
                        youtube, {"id": "video-1", **change}
                    )


if __name__ == "__main__":
    unittest.main()
