import unittest
from unittest.mock import Mock

from core.oauth import YOUTUBE_SCOPES
from core.comments.youtube_comments import (
    CommentVerificationUnavailable,
    compute_comment_reply_digest,
    list_channel_comment_threads,
    list_latest_unanswered_comment_threads,
    list_thread_replies,
    publish_approved_comment_reply,
    publish_comment_reply,
)


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeCommentThreads:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return FakeRequest(self.response)


class FakeComments:
    def __init__(self, list_responses=None, insert_response=None):
        self.list_responses = list(list_responses or [])
        self.insert_response = insert_response or {"id": "reply-1"}
        self.list_calls = []
        self.insert_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        response = self.list_responses.pop(0) if self.list_responses else {"items": []}
        return FakeRequest(response)

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)
        return FakeRequest(self.insert_response)


class FakeYoutube:
    def __init__(self, threads=None, comments=None):
        self._threads = threads or FakeCommentThreads({"items": []})
        self._comments = comments or FakeComments()

    def commentThreads(self):
        return self._threads

    def comments(self):
        return self._comments


class YouTubeCommentsAPITests(unittest.TestCase):
    def test_existing_youtube_oauth_scope_covers_comment_read_and_reply(self):
        self.assertIn("https://www.googleapis.com/auth/youtube.force-ssl", YOUTUBE_SCOPES)

    def test_channel_comment_page_uses_channel_wide_filter_and_cursor(self):
        response = {
            "items": [{
                "id": "thread-1",
                "snippet": {
                    "videoId": "abcdefghijk",
                    "topLevelComment": {
                        "id": "comment-1",
                        "snippet": {
                            "authorDisplayName": "Viewer",
                            "textDisplay": "Can I use this tool for free?",
                            "publishedAt": "2026-11-18T10:00:00Z",
                            "likeCount": 3,
                        },
                    },
                    "totalReplyCount": 2,
                    "canReply": True,
                },
            }],
            "nextPageToken": "next-page",
            "pageInfo": {"totalResults": 101},
        }
        threads = FakeCommentThreads(response)
        youtube = FakeYoutube(threads=threads)

        page = list_channel_comment_threads(
            youtube,
            "UC123",
            moderation_status="heldForReview",
            page_token="previous-page",
            max_results=25,
            search_terms="free",
        )

        self.assertEqual(page["items"][0]["thread_id"], "thread-1")
        self.assertEqual(page["items"][0]["top_level_comment_id"], "comment-1")
        self.assertEqual(page["items"][0]["total_reply_count"], 2)
        self.assertTrue(page["items"][0]["can_reply"])
        self.assertEqual(page["next_page_token"], "next-page")
        call = threads.calls[0]
        self.assertEqual(call["allThreadsRelatedToChannelId"], "UC123")
        self.assertEqual(call["moderationStatus"], "heldForReview")
        self.assertEqual(call["pageToken"], "previous-page")
        self.assertEqual(call["maxResults"], 25)
        self.assertEqual(call["searchTerms"], "free")
        self.assertNotIn("videoId", call)


    def test_latest_unanswered_checks_reply_authors_not_total_reply_count(self):
        def thread(thread_id, comment_id, reply_count):
            return {
                "id": thread_id,
                "snippet": {
                    "videoId": "abcdefghijk",
                    "topLevelComment": {"id": comment_id, "snippet": {
                        "authorDisplayName": "Viewer", "textDisplay": "Question?",
                    }},
                    "totalReplyCount": reply_count, "canReply": True,
                },
            }
        threads = FakeCommentThreads({"items": [
            thread("owner-replied", "comment-owner", 1),
            thread("viewer-replied", "comment-viewer", 1),
            thread("no-replies", "comment-none", 0),
        ]})
        comments = FakeComments([
            {"items": [{"id": "reply-owner", "snippet": {
                "authorDisplayName": "AI Simplified Lab", "authorChannelId": {"value": "UC_OWNER"},
            }}]},
            {"items": [{"id": "reply-viewer", "snippet": {
                "authorDisplayName": "Another viewer", "authorChannelId": {"value": "UC_VIEWER"},
            }}]},
        ])
        result = list_latest_unanswered_comment_threads(
            FakeYoutube(threads=threads, comments=comments), "UC_OWNER", target_count=10, scan_limit=10,
        )
        self.assertEqual([item["thread_id"] for item in result["items"]], ["viewer-replied", "no-replies"])
        self.assertEqual(result["scanned_count"], 3)
        self.assertEqual(result["unknown_reply_threads"], 0)
        self.assertTrue(result["items"][0]["owner_unanswered"])
        self.assertEqual(result["items"][0]["replies"][0]["author_channel_id"], "UC_VIEWER")
        self.assertEqual(threads.calls[0]["order"], "time")

    def test_latest_unanswered_treats_missing_reply_author_as_unknown(self):
        threads = FakeCommentThreads({"items": [{
            "id": "thread-unknown", "snippet": {
                "videoId": "abcdefghijk",
                "topLevelComment": {"id": "comment-unknown", "snippet": {"textDisplay": "Question?"}},
                "totalReplyCount": 1, "canReply": True,
            },
        }]})
        comments = FakeComments([{"items": [{
            "id": "reply-unknown", "snippet": {"textDisplay": "No author channel identity"},
        }]}])
        result = list_latest_unanswered_comment_threads(
            FakeYoutube(threads=threads, comments=comments), "UC_OWNER", target_count=1, scan_limit=10,
        )
        self.assertEqual(result["items"], [])
        self.assertEqual(result["unknown_reply_threads"], 1)


    def test_invalid_moderation_status_is_rejected(self):
        with self.assertRaises(ValueError):
            list_channel_comment_threads(FakeYoutube(), "UC123", moderation_status="deleted")

    def test_thread_replies_are_loaded_by_parent_only_on_request(self):
        comments = FakeComments([{
            "items": [{
                "id": "reply-1",
                "snippet": {
                    "parentId": "comment-1",
                    "authorDisplayName": "Creator",
                    "textDisplay": "Thanks for asking!",
                },
            }],
            "nextPageToken": "older",
        }])
        page = list_thread_replies(FakeYoutube(comments=comments), "comment-1", max_results=25)
        self.assertEqual(page["items"][0]["comment_id"], "reply-1")
        self.assertEqual(comments.list_calls[0]["parentId"], "comment-1")
        self.assertEqual(comments.list_calls[0]["maxResults"], 25)

    def test_reply_requires_can_reply_and_does_not_call_youtube(self):
        youtube = FakeYoutube()
        with self.assertRaises(PermissionError):
            publish_comment_reply(youtube, "comment-1", "A reply", can_reply=False)
        self.assertEqual(youtube._comments.insert_calls, [])

    def test_reply_is_inserted_and_verified_in_parent_thread(self):
        comments = FakeComments([{
            "items": [{
                "id": "reply-1",
                "snippet": {"parentId": "comment-1", "textDisplay": "Thanks!"},
            }],
        }], {"id": "reply-1"})
        youtube = FakeYoutube(comments=comments)

        result = publish_comment_reply(youtube, "comment-1", "Thanks!", can_reply=True)

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["comment_id"], "reply-1")
        self.assertEqual(
            comments.insert_calls[0]["body"]["snippet"],
            {"parentId": "comment-1", "textOriginal": "Thanks!"},
        )
        self.assertEqual(comments.list_calls[0]["parentId"], "comment-1")

    def test_uncertain_insert_transport_error_is_not_safe_to_retry(self):
        class TransportFailureComments(FakeComments):
            def insert(self, **kwargs):
                raise TimeoutError("connection lost after request")

        with self.assertRaises(CommentVerificationUnavailable):
            publish_comment_reply(
                FakeYoutube(comments=TransportFailureComments()),
                "comment-1",
                "Thanks!",
                can_reply=True,
            )

    def test_reply_readback_failure_is_not_reported_as_success(self):
        comments = FakeComments([{"items": []}], {"id": "reply-1"})
        with self.assertRaises(CommentVerificationUnavailable):
            publish_comment_reply(FakeYoutube(comments=comments), "comment-1", "Thanks!", can_reply=True)

    def test_reply_publish_requires_persisted_owner_approval_and_exact_digest(self):
        youtube = FakeYoutube(comments=FakeComments([{
            "items": [{
                "id": "reply-1",
                "snippet": {"parentId": "comment-1", "textDisplay": "Thanks!"},
            }],
        }], {"id": "reply-1"}))
        owner_key = "hashed-owner"
        draft = {
            "status": "publishing",
            "approved_by": owner_key,
            "approved_at": "2026-11-18T12:00:00Z",
            "thread_id": "thread-1",
            "parent_comment_id": "comment-1",
            "comment_text_snapshot": "Question?",
            "draft_text": "Thanks!",
            "can_reply": True,
        }
        draft["approval_digest"] = compute_comment_reply_digest(
            owner_key, "thread-1", "comment-1", "Question?", "Thanks!"
        )
        result = publish_approved_comment_reply(youtube, draft, owner_key)
        self.assertEqual(result["status"], "published")

    def test_reply_publish_rejects_unapproved_or_modified_draft(self):
        owner_key = "hashed-owner"
        draft = {
            "status": "pending_review",
            "approved_by": owner_key,
            "approved_at": "2026-11-18T12:00:00Z",
            "thread_id": "thread-1",
            "parent_comment_id": "comment-1",
            "comment_text_snapshot": "Question?",
            "draft_text": "Thanks!",
            "can_reply": True,
        }
        draft["approval_digest"] = compute_comment_reply_digest(
            owner_key, "thread-1", "comment-1", "Question?", "Thanks!"
        )
        with self.assertRaises(PermissionError):
            publish_approved_comment_reply(FakeYoutube(), draft, owner_key)
        draft["status"] = "publishing"
        draft["draft_text"] = "Changed after approval."
        with self.assertRaises(PermissionError):
            publish_approved_comment_reply(FakeYoutube(), draft, owner_key)

    def test_empty_or_oversized_reply_is_rejected_before_write(self):
        youtube = FakeYoutube()
        with self.assertRaises(ValueError):
            publish_comment_reply(youtube, "comment-1", "  ", can_reply=True)
        with self.assertRaises(ValueError):
            publish_comment_reply(youtube, "comment-1", "x" * 10001, can_reply=True)
        self.assertEqual(youtube._comments.insert_calls, [])


if __name__ == "__main__":
    unittest.main()
