import unittest
from unittest.mock import MagicMock

from channel_strategy_worker import _inject_targeted_video_evidence, _targeted_strategy_report, _metadata_evidence_is_verbatim


class TargetedVideoEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.video_id = "abcdefghijk"
        self.channel_id = "UC_owner_channel"
        self.youtube = MagicMock()
        self.youtube.videos.return_value.list.return_value.execute.return_value = {
            "items": [{
                "id": self.video_id,
                "snippet": {
                    "channelId": self.channel_id,
                    "title": "A real target video",
                    "description": "Source description for review.",
                    "tags": ["AI", "Tutorial"],
                    "categoryId": "28",
                },
                "contentDetails": {"duration": "PT45S"},
                "statistics": {"viewCount": "1234"},
            }]
        }

    def test_adds_owner_video_with_live_metadata_without_inventing_analytics(self):
        snapshot = {
            "opportunities": [],
            "unscored_short_duration_uploads": [],
            "video_rows": [],
        }

        result = _inject_targeted_video_evidence(
            snapshot, self.youtube, self.channel_id, self.video_id
        )

        self.assertEqual(result["targeted_video_id"], self.video_id)
        self.assertEqual(len(result["opportunities"]), 1)
        target = result["opportunities"][0]
        self.assertEqual(target["video_id"], self.video_id)
        self.assertEqual(target["current_metadata"]["title"], "A real target video")
        self.assertEqual(target["current_metadata"]["tags"], ["AI", "Tutorial"])
        self.assertTrue(target["targeted_test"])
        self.assertNotIn("views", target)
        self.assertNotIn("impressions", target)
        self.youtube.videos.return_value.list.assert_called_once_with(
            part="snippet,contentDetails,statistics", id=self.video_id
        )

    def test_live_target_metadata_replaces_stale_unscored_evidence(self):
        snapshot = {
            "opportunities": [],
            "unscored_short_duration_uploads": [{
                "video_id": self.video_id,
                "title": "Old title",
                "current_metadata": {"title": "Old title", "description": "", "tags": []},
            }],
            "video_rows": [],
        }

        result = _inject_targeted_video_evidence(
            snapshot, self.youtube, self.channel_id, self.video_id
        )

        self.assertEqual(len(result["opportunities"]), 1)
        self.assertEqual(
            result["opportunities"][0]["current_metadata"]["title"],
            "A real target video",
        )
        self.assertEqual(result["unscored_short_duration_uploads"], [])


    def test_targeted_strategy_report_excludes_unrelated_channel_candidates(self):
        report = {
            "executive_summary": "Channel summary remains available.",
            "metadata_candidates": [
                {"video_id": "unrelated001", "decision": "auto_publish"},
                {"video_id": self.video_id, "decision": "review"},
                {"video_id": "unrelated002", "decision": "review"},
            ],
        }

        result = _targeted_strategy_report(report, self.video_id)

        self.assertEqual(
            [item["video_id"] for item in result["metadata_candidates"]],
            [self.video_id],
        )
        self.assertEqual(result["executive_summary"], report["executive_summary"])
        self.assertEqual(len(report["metadata_candidates"]), 3)

    def test_targeted_strategy_report_returns_no_candidate_when_target_missing(self):
        report = {
            "metadata_candidates": [
                {"video_id": "unrelated001", "decision": "review"},
            ],
        }

        result = _targeted_strategy_report(report, self.video_id)

        self.assertEqual(result["metadata_candidates"], [])

    def test_rejects_video_outside_configured_owner_channel(self):
        self.youtube.videos.return_value.list.return_value.execute.return_value["items"][0]["snippet"]["channelId"] = "UC_someone_else"

        with self.assertRaisesRegex(RuntimeError, "does not belong"):
            _inject_targeted_video_evidence(
                {"opportunities": []}, self.youtube, self.channel_id, self.video_id
            )

    def test_rejects_invalid_video_id_before_youtube_lookup(self):
        with self.assertRaisesRegex(ValueError, "11-character"):
            _inject_targeted_video_evidence(
                {"opportunities": []}, self.youtube, self.channel_id, "short-id"
            )
        self.youtube.videos.return_value.list.assert_not_called()



class VerbatimMetadataEvidenceTests(unittest.TestCase):
    def test_exact_source_excerpt_is_accepted(self):
        source = {
            "title": "Why 4 AI Agents Burn 3.7x More Tokens?",
            "description": "A broken JSON tool call can burn 55% of your token budget."
        }
        self.assertTrue(_metadata_evidence_is_verbatim(
            "burn 55% of your token budget", source
        ))

    def test_paraphrased_evidence_is_rejected(self):
        source = {
            "title": "Why 4 AI Agents Burn 3.7x More Tokens?",
            "description": "A broken JSON tool call can burn 55% of your token budget."
        }
        self.assertFalse(_metadata_evidence_is_verbatim(
            "Description mentions burning 55% of the token budget", source
        ))

    def test_evidence_cannot_be_assembled_across_metadata_fields(self):
        source = {
            "title": "Why 4 AI Agents",
            "description": "Burn 3.7x more tokens",
            "tags": ["automation"]
        }
        self.assertFalse(_metadata_evidence_is_verbatim(
            "Why 4 AI Agents Burn 3.7x more tokens", source
        ))

    def test_exact_tag_excerpt_is_accepted(self):
        source = {"title": "AI agents", "description": "", "tags": ["multi-agent routing"]}
        self.assertTrue(_metadata_evidence_is_verbatim("multi-agent routing", source))


if __name__ == "__main__":
    unittest.main()
