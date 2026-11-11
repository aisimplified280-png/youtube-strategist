import unittest
from unittest.mock import MagicMock

from fastapi import HTTPException

import assistant_publish_api as api


class AssistantPublishApiTests(unittest.TestCase):
    def test_publish_confirmation_defaults_to_false(self):
        body = api.PublishBody(proposed_metadata={"title": "A new title"})
        self.assertFalse(body.publish_confirmed)

    def test_nonexistent_eleven_character_word_is_not_accepted_as_video(self):
        youtube = MagicMock()
        youtube.videos.return_value.list.return_value.execute.return_value = {"items": []}
        with self.assertRaises(HTTPException) as error:
            api._video_before(youtube, "recommended")
        self.assertEqual(error.exception.status_code, 404)

    def test_external_video_is_read_only(self):
        youtube = MagicMock()
        youtube.videos.return_value.list.return_value.execute.return_value = {
            "items": [{
                "id": "AbCdEfGhIjK",
                "snippet": {
                    "title": "External", "description": "", "tags": [],
                    "categoryId": "28", "channelId": "UC_EXTERNAL",
                },
            }]
        }
        with self.assertRaises(HTTPException) as error:
            api._video_before(youtube, "AbCdEfGhIjK")
        self.assertEqual(error.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
