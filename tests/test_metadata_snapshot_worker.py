import unittest
from unittest.mock import patch

import metadata_snapshot_worker


class _Request:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class _Videos:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _Request(self.response)


class _YouTube:
    def __init__(self, response):
        self._videos = _Videos(response)

    def videos(self):
        return self._videos


class MetadataSnapshotWorkerTests(unittest.TestCase):
    def test_only_public_videos_from_exact_channel_are_snapshotted(self):
        youtube = _YouTube({"items": [
            {"id": "PUBLIC00001", "snippet": {
                "channelId": "UC" + "A" * 22, "title": "Public",
                "description": "Description", "tags": ["ai"],
            }, "status": {"privacyStatus": "public"}},
            {"id": "UNLISTED001", "snippet": {
                "channelId": "UC" + "A" * 22, "title": "Unlisted",
                "description": "", "tags": [],
            }, "status": {"privacyStatus": "unlisted"}},
            {"id": "OTHER000001", "snippet": {
                "channelId": "UC" + "B" * 22, "title": "Other",
                "description": "", "tags": [],
            }, "status": {"privacyStatus": "public"}},
        ]})
        with (
            patch.object(metadata_snapshot_worker, "list_upload_video_ids",
                        return_value=["PUBLIC00001", "UNLISTED001", "OTHER000001", "MISSING0001"]),
            patch.object(metadata_snapshot_worker, "_record_live_metadata_snapshot",
                        return_value={"video_id": "PUBLIC00001"}) as record,
        ):
            summary = metadata_snapshot_worker.snapshot_public_uploads(
                youtube, object(), "owner-key", "UC" + "A" * 22
            )

        self.assertEqual(summary["catalogue_ids"], 4)
        self.assertEqual(summary["snapshotted_public"], 1)
        self.assertEqual(summary["non_public"], 1)
        self.assertEqual(summary["wrong_channel"], 1)
        self.assertEqual(summary["missing_or_unavailable"], 1)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[2], "PUBLIC00001")

    def test_snapshot_scan_fails_if_a_ledger_write_fails(self):
        youtube = _YouTube({"items": [{
            "id": "PUBLIC00001", "snippet": {
                "channelId": "UC" + "A" * 22, "title": "Public",
                "description": "Description", "tags": [],
            }, "status": {"privacyStatus": "public"},
        }]})
        with (
            patch.object(metadata_snapshot_worker, "list_upload_video_ids",
                        return_value=["PUBLIC00001"]),
            patch.object(metadata_snapshot_worker, "_record_live_metadata_snapshot",
                        side_effect=RuntimeError("ledger unavailable")),
        ):
            with self.assertRaisesRegex(RuntimeError, "snapshot scan incomplete"):
                metadata_snapshot_worker.snapshot_public_uploads(
                    youtube, object(), "owner-key", "UC" + "A" * 22
                )


if __name__ == "__main__":
    unittest.main()
