import unittest
from unittest.mock import MagicMock

from channel_strategy_worker import _inject_targeted_video_evidence


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


if __name__ == "__main__":
    unittest.main()
