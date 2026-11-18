import copy
import unittest

import youtube_analytics_connector as connector
from core.metadata.resource import (
    apply_approved_resource_proposal,
    fetch_video_resource,
    normalize_video_resource,
)


class FakeRequest:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return self.callback()


class FakeVideos:
    def __init__(self, resource):
        self.resource = copy.deepcopy(resource)
        self.write_calls = []

    def list(self, *, part, id):
        return FakeRequest(lambda: {"items": [{
            "id": id,
            **copy.deepcopy(self.resource),
        }]})

    def update(self, *, part, body):
        self.write_calls.append({"part": part, "body": copy.deepcopy(body)})

        def apply():
            for resource_part in part.split(","):
                self.resource[resource_part] = copy.deepcopy(body[resource_part])
            return {"id": body["id"]}

        return FakeRequest(apply)


class FakeYoutube:
    def __init__(self, resource):
        self._videos = FakeVideos(resource)

    def videos(self):
        return self._videos


def make_resource():
    return {
        "snippet": {
            "channelId": connector.CHANNEL_ID,
            "title": "Original title",
            "description": "Original description",
            "tags": ["ai", "tutorial"],
            "categoryId": "28",
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
            "publishedAt": "2026-01-01T00:00:00Z",
        },
        "status": {
            "privacyStatus": "public",
            "license": "youtube",
            "embeddable": True,
            "publicStatsViewable": True,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": False,
            "uploadStatus": "processed",
        },
        "recordingDetails": {"recordingDate": "2026-01-01T00:00:00Z"},
        "localizations": {
            "te": {"title": "పాత శీర్షిక", "description": "పాత వివరణ"},
            "hi": {"title": "पुराना शीर्षक", "description": "पुराना विवरण"},
        },
    }


class FullMetadataResourceTests(unittest.TestCase):
    def test_normalize_excludes_read_only_api_fields(self):
        snapshot = normalize_video_resource(make_resource())
        self.assertNotIn("channelId", snapshot["snippet"])
        self.assertNotIn("publishedAt", snapshot["snippet"])
        self.assertNotIn("uploadStatus", snapshot["status"])
        self.assertEqual(snapshot["status"]["privacyStatus"], "public")

    def test_snippet_update_preserves_supported_fields_and_readback(self):
        youtube = FakeYoutube(make_resource())
        before = fetch_video_resource(youtube, "video-123")
        proposed = copy.deepcopy(before)
        proposed["snippet"]["title"] = "Improved title"
        digest = connector.metadata_proposal_digest("video-123", before, proposed)
        audit = []

        result = apply_approved_resource_proposal(
            youtube, "video-123", before, proposed, digest, audit_callback=audit.append
        )

        self.assertEqual(result["result"], "success")
        self.assertEqual(result["updated_parts"], ["snippet"])
        write = youtube._videos.write_calls[0]
        self.assertEqual(write["part"], "snippet")
        self.assertEqual(write["body"]["snippet"]["categoryId"], "28")
        self.assertEqual(write["body"]["snippet"]["defaultLanguage"], "en")
        self.assertEqual(write["body"]["snippet"]["defaultAudioLanguage"], "en")
        self.assertNotIn("channelId", write["body"]["snippet"])
        self.assertEqual(audit[0]["result"], "success")

    def test_status_update_is_separate_and_preserves_other_supported_status(self):
        youtube = FakeYoutube(make_resource())
        before = fetch_video_resource(youtube, "video-123")
        proposed = copy.deepcopy(before)
        proposed["status"]["embeddable"] = False
        digest = connector.metadata_proposal_digest("video-123", before, proposed)

        result = apply_approved_resource_proposal(
            youtube, "video-123", before, proposed, digest
        )

        write = youtube._videos.write_calls[0]
        self.assertEqual(result["updated_parts"], ["status"])
        self.assertEqual(write["part"], "status")
        self.assertEqual(write["body"]["status"]["privacyStatus"], "public")
        self.assertEqual(write["body"]["status"]["license"], "youtube")
        self.assertNotIn("uploadStatus", write["body"]["status"])

    def test_localization_update_preserves_other_locales(self):
        youtube = FakeYoutube(make_resource())
        before = fetch_video_resource(youtube, "video-123")
        proposed = copy.deepcopy(before)
        proposed["localizations"]["te"]["title"] = "కొత్త శీర్షిక"
        digest = connector.metadata_proposal_digest("video-123", before, proposed)

        result = apply_approved_resource_proposal(
            youtube, "video-123", before, proposed, digest
        )

        self.assertEqual(result["updated_parts"], ["localizations"])
        self.assertIn("hi", youtube._videos.resource["localizations"])
        self.assertEqual(
            youtube._videos.resource["localizations"]["te"]["title"],
            "కొత్త శీర్షిక",
        )

    def test_stale_resource_fails_before_write(self):
        youtube = FakeYoutube(make_resource())
        before = fetch_video_resource(youtube, "video-123")
        proposed = copy.deepcopy(before)
        proposed["snippet"]["title"] = "Improved title"
        digest = connector.metadata_proposal_digest("video-123", before, proposed)
        youtube._videos.resource["snippet"]["description"] = "Changed elsewhere"

        with self.assertRaisesRegex(RuntimeError, "changed after review"):
            apply_approved_resource_proposal(
                youtube, "video-123", before, proposed, digest
            )
        self.assertEqual(youtube._videos.write_calls, [])

    def test_invalid_or_unsupported_fields_fail_closed(self):
        youtube = FakeYoutube(make_resource())
        before = fetch_video_resource(youtube, "video-123")
        proposed = copy.deepcopy(before)
        proposed["status"]["uploadStatus"] = "deleted"
        digest = connector.metadata_proposal_digest("video-123", before, proposed)

        with self.assertRaisesRegex(connector.MetadataValidationError, "Unsupported status fields"):
            apply_approved_resource_proposal(
                youtube, "video-123", before, proposed, digest
            )
        self.assertEqual(youtube._videos.write_calls, [])

    def test_digest_mismatch_fails_before_write(self):
        youtube = FakeYoutube(make_resource())
        before = fetch_video_resource(youtube, "video-123")
        proposed = copy.deepcopy(before)
        proposed["snippet"]["title"] = "Improved title"

        with self.assertRaisesRegex(connector.MetadataValidationError, "does not match"):
            apply_approved_resource_proposal(
                youtube, "video-123", before, proposed, "0" * 64
            )
        self.assertEqual(youtube._videos.write_calls, [])


if __name__ == "__main__":
    unittest.main()
