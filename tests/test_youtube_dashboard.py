import unittest
from datetime import date, timedelta

import httplib2
from googleapiclient.errors import HttpError

import youtube_dashboard
from dashboard_security import (
    consume_oauth_state,
    create_oauth_state,
    owner_is_authorized,
    reuse_or_create_oauth_state,
    verify_oauth_state,
)
from youtube_analytics_connector import CHANNEL_ID
from test_youtube_analytics_connector import FakeRequest


class FakeDashboardVideos:
    def __init__(self, snippets):
        self.snippets = snippets
        self.write_calls = []

    def list(self, *, part, id):
        assert "snippet" in part

        def response():
            items = []
            for video_id in id.split(","):
                snippet = self.snippets.get(video_id)
                if snippet:
                    items.append({
                        "id": video_id,
                        "snippet": snippet.copy(),
                        "contentDetails": {"duration": "PT90S"},
                        "statistics": {"viewCount": "200"},
                    })
            return {"items": items}

        return FakeRequest(response)

    def update(self, **kwargs):
        self.write_calls.append(kwargs)
        return FakeRequest(lambda: {})


class FakeDashboardYoutube:
    def __init__(self, snippets):
        self.snippets = snippets
        self._videos = FakeDashboardVideos(snippets)

    def channels(self):
        return self

    def playlistItems(self):
        return self

    def videos(self):
        return self._videos

    def list(self, **kwargs):
        if "playlistId" in kwargs:
            return FakeRequest(lambda: {
                "items": [
                    {"contentDetails": {"videoId": video_id}}
                    for video_id in self.snippets
                ]
            })
        return FakeRequest(lambda: {
            "items": [{
                "id": CHANNEL_ID,
                "contentDetails": {
                    "relatedPlaylists": {"uploads": "uploads"}
                },
            }]
        })


class FakeDashboardAnalytics:
    def __init__(self, metrics_by_video):
        self.metrics_by_video = metrics_by_video
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
                    "rows": [["SHORTS", 9, 2]],
                }
            if kwargs.get("dimensions") == "ageGroup,gender":
                return {
                    "columnHeaders": [
                        {"name": "ageGroup"},
                        {"name": "gender"},
                        {"name": "viewerPercentage"},
                    ],
                    "rows": [["18-24", "female", 42.5]],
                }
            if kwargs.get("dimensions") == "day":
                return {
                    "columnHeaders": [
                        {"name": "day"},
                        {"name": "views"},
                        {"name": "estimatedMinutesWatched"},
                        {"name": "averageViewDuration"},
                        {"name": "subscribersGained"},
                        {"name": "subscribersLost"},
                        {"name": "likes"},
                        {"name": "comments"},
                        {"name": "shares"},
                    ],
                    "rows": [[kwargs["startDate"], 9, 2, 12, 1, 0, 2, 0, 1]],
                }
            video_id = kwargs.get("filters", "").removeprefix("video==")
            if video_id not in self.metrics_by_video:
                return {"columnHeaders": [], "rows": []}
            metrics = self.metrics_by_video[video_id]
            headers = list(metrics)
            return {
                "columnHeaders": [{"name": name} for name in headers],
                "rows": [[metrics[name] for name in headers]],
            }

        return FakeRequest(response)


def video_snippet(title, published_at, description="", tags=None):
    return {
        "title": title,
        "description": description,
        "tags": tags or [],
        "categoryId": "28",
        "channelId": CHANNEL_ID,
        "publishedAt": published_at,
    }


class FakeReportCollection:
    def __init__(self, response):
        self.response = response
        self.list_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return FakeRequest(
            lambda: self.response()
            if callable(self.response)
            else self.response
        )


class FakeReachJob:
    def __init__(self, existing_job=True, reports=None):
        self.existing_job = existing_job
        self.reports_resource = FakeReportCollection(
            {"reports": reports or []}
        )
        self.created_jobs = []
        self.list_resource = FakeReportCollection(
            {
                "jobs": (
                    [{
                        "id": "reach-job",
                        "reportTypeId": youtube_dashboard.REACH_REPORT_TYPE_ID,
                    }]
                    if existing_job
                    else []
                )
            }
        )

    def list(self, **kwargs):
        return self.list_resource.list(**kwargs)

    def reports(self):
        return self.reports_resource

    def create(self, *, body):
        self.created_jobs.append(body)
        return FakeRequest(lambda: {
            "id": "reach-job",
            "reportTypeId": body["reportTypeId"],
        })


class FakeReachReporting:
    def __init__(self, *, existing_job=True, reports=None):
        self.job_resource = FakeReachJob(existing_job, reports)
        self.report_types = FakeReportCollection({
            "reportTypes": [{"id": youtube_dashboard.REACH_REPORT_TYPE_ID}]
        })

    def jobs(self):
        return self.job_resource

    def reportTypes(self):
        return self.report_types


class FakeDownloadResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


class FakeDownloadSession:
    def __init__(self, reports):
        self.reports = reports
        self.urls = []

    def get(self, url, *, timeout):
        self.urls.append((url, timeout))
        return FakeDownloadResponse(self.reports[url])


class YoutubeDashboardTests(unittest.TestCase):
    def test_analytics_mapping_and_unavailable_impressions_ctr(self):
        yesterday = date.today() - timedelta(days=1)
        youtube = FakeDashboardYoutube({
            "video-1": video_snippet(
                "Prompt caching",
                f"{yesterday.isoformat()}T10:00:00Z",
            ),
        })
        analytics = FakeDashboardAnalytics({
            "video-1": {
                "views": 12,
                "estimatedMinutesWatched": 4,
                "averageViewDuration": 20,
                "subscribersGained": 1,
                "subscribersLost": 0,
                "likes": 2,
                "comments": 3,
                "shares": 4,
            },
        })

        result = youtube_dashboard.load_dashboard_data(
            youtube,
            analytics,
            yesterday - timedelta(days=6),
            yesterday,
            video_limit=10,
        )

        row = result["video_rows"][0]
        self.assertEqual(row["views"], 12)
        self.assertEqual(row["estimatedMinutesWatched"], 4)
        self.assertEqual(row["averageViewDuration"], 20)
        self.assertEqual(row["subscribersGained"], 1)
        self.assertEqual(row["subscribersLost"], 0)
        self.assertEqual(row["likes"], 2)
        self.assertEqual(row["comments"], 3)
        self.assertEqual(row["shares"], 4)
        self.assertIsNone(row["impressions"])
        self.assertIsNone(row["ctr"])
        self.assertIn("not configured", row["impressions_status"])
        self.assertIn("not configured", row["ctr_status"])
        self.assertTrue(result["daily_rows"])
        self.assertTrue(result["traffic_sources"])
        self.assertEqual(
            result["previous_period"],
            {
                "start": (yesterday - timedelta(days=13)).isoformat(),
                "end": (yesterday - timedelta(days=7)).isoformat(),
            },
        )
        self.assertTrue(
            all("impressions" not in query["metrics"] for query in analytics.requests)
        )
        self.assertEqual(youtube._videos.write_calls, [])
        demographics, demographics_status = (
            youtube_dashboard.fetch_video_audience_demographics(
                analytics,
                "video-1",
                yesterday - timedelta(days=6),
                yesterday,
            )
        )
        self.assertEqual(demographics[0]["viewerPercentage"], 42.5)
        self.assertEqual(demographics_status, "Available")

    def test_reporting_api_reach_metrics_aggregate_daily_rows(self):
        start_date = date(2025, 1, 2)
        end_date = date(2025, 1, 3)
        csv_rows = (
            "date,video_id,video_thumbnail_impressions,"
            "video_thumbnail_impressions_ctr\n"
            "2025-01-02,video-1,100,4\n"
            "2025-01-03,video-1,300,8\n"
            "2025-01-03,other-video,999,20\n"
        ).encode()
        reports = [
            {
                "downloadUrl": "https://reports.example/reach.csv",
                "startTime": "2025-01-02T00:00:00Z",
                "endTime": "2025-01-03T00:00:00Z",
            },
        ]
        reporting = FakeReachReporting(reports=reports)
        download = FakeDownloadSession({
            "https://reports.example/reach.csv": csv_rows,
        })

        metrics, status = youtube_dashboard.fetch_reach_metrics(
            reporting,
            object(),
            ["video-1"],
            start_date,
            end_date,
            download_session=download,
        )

        self.assertEqual(metrics["video-1"]["impressions"], 400)
        self.assertEqual(metrics["video-1"]["ctr"], 7)
        self.assertEqual(len(reporting.job_resource.created_jobs), 0)
        self.assertEqual(
            reporting.job_resource.reports_resource.list_calls[0],
            {
                "jobId": "reach-job",
                "startTimeAtOrAfter": "2025-01-02T00:00:00Z",
                "startTimeBefore": "2025-01-04T00:00:00Z",
            },
        )
        self.assertIn("1 of 2 selected days", status)
        self.assertIn("incomplete", status)

    def test_reporting_api_missing_ctr_is_metric_specific(self):
        reporting = FakeReachReporting(reports=[{
            "downloadUrl": "https://reports.example/no-ctr.csv",
            "startTime": "2025-01-02T00:00:00Z",
            "endTime": "2025-01-03T00:00:00Z",
        }])
        download = FakeDownloadSession({
            "https://reports.example/no-ctr.csv": (
                "date,video_id,video_thumbnail_impressions\n"
                "2025-01-02,video-1,17\n"
            ).encode(),
        })

        metrics, _ = youtube_dashboard.fetch_reach_metrics(
            reporting,
            object(),
            ["video-1"],
            date(2025, 1, 2),
            date(2025, 1, 2),
            download_session=download,
        )

        self.assertEqual(metrics["video-1"]["impressions"], 17)
        self.assertIsNone(metrics["video-1"]["ctr"])
        self.assertIn(
            "does not contain video_thumbnail_impressions_ctr",
            metrics["video-1"]["ctr_status"],
        )

    def test_reporting_api_new_job_reports_delayed_data_without_fabricating(self):
        reporting = FakeReachReporting(existing_job=False)

        metrics, status = youtube_dashboard.fetch_reach_metrics(
            reporting,
            object(),
            ["video-1"],
            date(2025, 1, 2),
            date(2025, 1, 2),
            download_session=FakeDownloadSession({}),
        )

        self.assertEqual(len(reporting.job_resource.created_jobs), 1)
        self.assertIsNone(metrics["video-1"]["impressions"])
        self.assertIsNone(metrics["video-1"]["ctr"])
        self.assertIn("48 hours", status)
        self.assertIn("before the job was created", status)

    def test_reporting_api_http_failure_is_reported_as_unavailable(self):
        reporting = FakeReachReporting()
        response = httplib2.Response({"status": "403"})

        def deny_report_job_list():
            raise HttpError(response, b"forbidden")

        reporting.job_resource.list_resource.response = deny_report_job_list

        metrics, status = youtube_dashboard.fetch_reach_metrics(
            reporting,
            object(),
            ["video-1"],
            date(2025, 1, 2),
            date(2025, 1, 2),
            download_session=FakeDownloadSession({}),
        )

        self.assertIn("HTTP 403", status)
        self.assertIsNone(metrics["video-1"]["impressions"])
        self.assertIsNone(metrics["video-1"]["ctr"])
        self.assertIn("HTTP 403", metrics["video-1"]["impressions_status"])

    def test_empty_analytics_report_maps_metrics_to_missing_not_zero(self):
        yesterday = date.today() - timedelta(days=1)
        youtube = FakeDashboardYoutube({
            "video-1": video_snippet(
                "Recent video",
                f"{yesterday.isoformat()}T10:00:00Z",
            ),
        })
        analytics = FakeDashboardAnalytics({})
        result = youtube_dashboard.load_dashboard_data(
            youtube,
            analytics,
            yesterday,
            yesterday,
            video_limit=1,
        )

        self.assertIsNone(result["video_rows"][0]["views"])
        self.assertIn("No rows", result["video_rows"][0]["analytics_status"])

    def test_specific_video_url_loads_snippet_and_analytics(self):
        yesterday = date.today() - timedelta(days=1)
        youtube = FakeDashboardYoutube({
            "video-1": video_snippet(
                "Requested video",
                f"{yesterday.isoformat()}T10:00:00Z",
            ),
        })
        analytics = FakeDashboardAnalytics({
            "video-1": {
                "views": 12,
                "estimatedMinutesWatched": 4,
                "averageViewDuration": 20,
                "subscribersGained": 1,
                "subscribersLost": 0,
                "likes": 2,
                "comments": 3,
                "shares": 4,
            },
        })

        row = youtube_dashboard.fetch_video_dashboard_row(
            youtube,
            analytics,
            "https://www.youtube.com/shorts/video-1",
            yesterday - timedelta(days=6),
            yesterday,
        )

        self.assertEqual(row["video_id"], "video-1")
        self.assertEqual(row["title"], "Requested video")
        self.assertEqual(row["views"], 12)
        self.assertEqual(youtube._videos.write_calls, [])

    def test_recommendations_use_only_same_format_age_matched_actual_metrics(self):
        rows = [
            {
                "video_id": "target",
                "title": "Target",
                "url": "https://youtube.com/watch?v=target",
                "format": "Short-duration candidate (<=180 sec)",
                "age_days": 20,
                "views": 20,
                "averageViewDuration": 40,
            },
            *[
                {
                    "video_id": f"peer-{index}",
                    "title": f"Peer {index}",
                    "url": f"https://youtube.com/watch?v=peer-{index}",
                    "format": "Short-duration candidate (<=180 sec)",
                    "age_days": 21 + index,
                    "views": 40 + index * 10,
                    "averageViewDuration": 10,
                }
                for index in range(3)
            ],
            {
                "video_id": "long",
                "title": "Long",
                "url": "https://youtube.com/watch?v=long",
                "format": "Long-form (>180 sec)",
                "age_days": 20,
                "views": 1000,
                "averageViewDuration": 100,
            },
        ]

        ranked = youtube_dashboard.rank_optimization_candidates(rows)

        self.assertEqual(ranked[0]["video_id"], "target")
        self.assertEqual(ranked[0]["peer_count"], 3)
        self.assertEqual(ranked[0]["peer_views_median"], 50)
        self.assertIn(
            "average view duration is 40s",
            ranked[0]["recommendation_evidence"],
        )
        self.assertIn(
            "vs. their 10s median",
            ranked[0]["recommendation_evidence"],
        )
        self.assertNotIn("long", [row["video_id"] for row in ranked])

    def test_rank_optimization_candidates_excludes_zero_views(self):
        rows = [
            {
                "video_id": "zero-view-video",
                "title": "Zero views dormant video",
                "url": "https://youtube.com/watch?v=zero-view-video",
                "format": "Short-duration candidate (<=180 sec)",
                "age_days": 20,
                "views": 0,
                "averageViewDuration": 0,
            },
            {
                "video_id": "target",
                "title": "Target video with measured traffic",
                "url": "https://youtube.com/watch?v=target",
                "format": "Short-duration candidate (<=180 sec)",
                "age_days": 20,
                "views": 25,
                "averageViewDuration": 15,
            },
            *[
                {
                    "video_id": f"peer-{index}",
                    "title": f"Peer {index}",
                    "url": f"https://youtube.com/watch?v=peer-{index}",
                    "format": "Short-duration candidate (<=180 sec)",
                    "age_days": 21 + index,
                    "views": 50 + index * 10,
                    "averageViewDuration": 20,
                }
                for index in range(3)
            ],
        ]
        ranked = youtube_dashboard.rank_optimization_candidates(rows)
        # zero-view video must NOT be ranked as a candidate
        self.assertNotIn("zero-view-video", [row["video_id"] for row in ranked])
        # the measured video with views > 0 should be ranked
        self.assertEqual(ranked[0]["video_id"], "target")

    def test_recent_video_opportunities_prioritize_measured_low_ctr(self):
        rows = [
            {
                "video_id": "recent-low-ctr",
                "title": "Recent upload with low CTR",
                "url": "https://youtube.com/watch?v=recent-low-ctr",
                "published_at": "2026-10-10",
                "format": "Short-duration candidate (<=180 sec)",
                "age_days": 7,
                "views": 90,
                "impressions": 800,
                "ctr": 1.2,
            },
            *[
                {
                    "video_id": f"peer-{index}",
                    "title": f"Peer {index}",
                    "url": f"https://youtube.com/watch?v=peer-{index}",
                    "published_at": f"2026-10-{index + 2:02d}",
                    "format": "Short-duration candidate (<=180 sec)",
                    "age_days": 7 + index,
                    "views": 150 + index * 20,
                    "impressions": 500,
                    "ctr": 5.0 + index,
                }
                for index in range(3)
            ],
        ]

        ranked = youtube_dashboard.rank_recent_video_opportunities(
            rows, ["recent-low-ctr"]
        )

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["selection_basis"], "low_ctr_vs_peers")
        self.assertEqual(ranked[0]["opportunity_priority"], 0)
        self.assertEqual(ranked[0]["peer_ctr_median"], 6.0)
        self.assertIn("Measured thumbnail CTR is 1.20%", ranked[0]["opportunity_evidence"])
        self.assertNotIn("estimate", ranked[0]["opportunity_evidence"].lower())

    def test_recent_opportunities_never_invent_ctr_when_unavailable(self):
        rows = [
            {
                "video_id": "recent-low-views",
                "title": "Recent upload",
                "url": "https://youtube.com/watch?v=recent-low-views",
                "format": "Short-duration candidate (<=180 sec)",
                "age_days": 5,
                "views": 20,
                "impressions": None,
                "ctr": None,
            },
            *[
                {
                    "video_id": f"peer-{index}",
                    "title": f"Peer {index}",
                    "url": f"https://youtube.com/watch?v=peer-{index}",
                    "format": "Short-duration candidate (<=180 sec)",
                    "age_days": 5 + index,
                    "views": 100 + index * 20,
                    "impressions": None,
                    "ctr": None,
                }
                for index in range(3)
            ],
        ]

        ranked = youtube_dashboard.rank_recent_video_opportunities(
            rows, ["recent-low-views"]
        )

        self.assertEqual(ranked[0]["selection_basis"], "low_views_vs_peers")
        self.assertEqual(ranked[0]["opportunity_type"], "Views below similar uploads")
        self.assertIsNone(ranked[0]["ctr"])
        self.assertIsNone(ranked[0].get("peer_ctr_median"))

    def test_recent_opportunities_fall_back_to_explicit_limited_evidence_watchlist(self):
        rows = [
            {
                "video_id": "recent-low-views",
                "title": "Recent upload",
                "url": "https://youtube.com/watch?v=recent-low-views",
                "format": "Short-duration candidate (<=180 sec)",
                "age_days": 5,
                "views": 20,
                "impressions": None,
                "ctr": None,
            },
            {
                "video_id": "other",
                "title": "Other upload",
                "url": "https://youtube.com/watch?v=other",
                "format": "Long-form (>180 sec)",
                "age_days": 30,
                "views": 500,
                "impressions": None,
                "ctr": None,
            },
        ]

        ranked = youtube_dashboard.rank_recent_video_opportunities(
            rows, ["recent-low-views"]
        )

        self.assertEqual(ranked[0]["selection_basis"], "lowest_observed_views")
        self.assertIn("review shortlist only", ranked[0]["opportunity_evidence"])
        self.assertIn("not a confirmed underperformer", ranked[0]["opportunity_evidence"])

    def test_low_ctr_with_measured_reach_is_a_packaging_review(self):
        rows = [
            {
                "video_id": "target",
                "format": "Long-form (>180 sec)",
                "age_days": 20,
                "views": 20,
                "impressions": 120,
                "ctr": 2.0,
            },
            *[
                {
                    "video_id": f"peer-{index}",
                    "format": "Long-form (>180 sec)",
                    "age_days": 21 + index,
                    "views": 100 + index,
                    "impressions": 1000,
                    "ctr": 5.0,
                }
                for index in range(3)
            ],
        ]

        ranked = youtube_dashboard.rank_optimization_candidates(rows)

        self.assertEqual(ranked[0]["diagnosis"], "Packaging review")
        self.assertIn("CTR is 2% vs. a 5% peer median", ranked[0]["next_step"])
        self.assertIn("title and thumbnail", ranked[0]["next_step"])

    def test_missing_reach_data_does_not_claim_metadata_is_the_cause(self):
        rows = [
            {
                "video_id": "target",
                "format": "Long-form (>180 sec)",
                "age_days": 20,
                "views": 20,
                "impressions": None,
                "ctr": None,
            },
            *[
                {
                    "video_id": f"peer-{index}",
                    "format": "Long-form (>180 sec)",
                    "age_days": 21 + index,
                    "views": 100 + index,
                    "impressions": None,
                    "ctr": None,
                }
                for index in range(3)
            ],
        ]

        ranked = youtube_dashboard.rank_optimization_candidates(rows)

        self.assertEqual(ranked[0]["diagnosis"], "No specific bottleneck isolated")
        self.assertIn("do not identify a cause", ranked[0]["next_step"])
        self.assertNotIn("title", ranked[0]["next_step"])

    def test_metadata_suggestion_preserves_description_links_and_existing_tags(self):
        snippet = {
            "title": "Why Your LLM Cache Fails",
            "description": (
                "Learn about LLM prompt caching and semantic caching. "
                "See https://example.com/resource\n\n#AI"
            ),
            "tags": ["existing approved tag"],
        }

        proposal = youtube_dashboard.generate_metadata_proposal(snippet)

        self.assertEqual(
            proposal["title"],
            "Why Your LLM Prompt Cache Fails",
        )
        self.assertIn("https://example.com/resource", proposal["description"])
        self.assertIn("#AI", proposal["description"])
        self.assertIn("#PromptCaching", proposal["description"])
        self.assertIn("#SemanticCaching", proposal["description"])
        self.assertEqual(proposal["tags"][0], "existing approved tag")
        self.assertIn("LLM prompt caching", proposal["tags"])
        self.assertEqual(snippet["description"].count("https://example.com/resource"), 1)

    def test_unknown_topic_does_not_invent_title_or_tags(self):
        snippet = {
            "title": "A video title",
            "description": "A topic with no recognized metadata phrase.",
            "tags": ["keep"],
        }

        proposal = youtube_dashboard.generate_metadata_proposal(snippet)

        self.assertEqual(proposal["title"], snippet["title"])
        self.assertEqual(proposal["description"], snippet["description"])
        self.assertEqual(proposal["tags"], ["keep"])
        self.assertIn("No recognized topic phrase", proposal["explanation"])

    def test_multiple_detected_topics_do_not_duplicate_tags(self):
        proposal = youtube_dashboard.generate_metadata_proposal({
            "title": "Cross-validation and regularization",
            "description": "Explore cross validation and regularization.",
            "tags": [],
        })

        self.assertEqual(proposal["tags"].count("machine learning"), 1)
        self.assertIn("#CrossValidation", proposal["description"])
        self.assertIn("#Regularization", proposal["description"])

    def test_suggestion_respects_existing_description_size_limit(self):
        description = "Prompt caching. " + ("x" * 4984)
        proposal = youtube_dashboard.generate_metadata_proposal({
            "title": "Prompt caching",
            "description": description,
            "tags": [],
        })

        self.assertEqual(proposal["description"], description)
        self.assertLessEqual(len(proposal["description"]), 5000)

    def test_google_login_access_is_owner_only_and_verified(self):
        self.assertTrue(
            owner_is_authorized("Owner@example.com", True, "owner@example.com")
        )
        self.assertFalse(
            owner_is_authorized("other@example.com", True, "owner@example.com")
        )
        self.assertFalse(
            owner_is_authorized("owner@example.com", False, "owner@example.com")
        )

    def test_youtube_oauth_state_is_bound_to_owner_and_expires(self):
        state = create_oauth_state(
            "google-subject",
            "test-signing-secret",
            now=1000,
            nonce="test-nonce",
        )

        self.assertTrue(
            verify_oauth_state(
                state,
                "google-subject",
                "test-signing-secret",
                now=1001,
            )
        )
        self.assertFalse(
            verify_oauth_state(
                state,
                "other-subject",
                "test-signing-secret",
                now=1001,
            )
        )
        self.assertFalse(
            verify_oauth_state(
                state,
                "google-subject",
                "wrong-secret",
                now=1001,
            )
        )
        self.assertFalse(
            verify_oauth_state(
                state,
                "google-subject",
                "test-signing-secret",
                now=1000 + 601,
            )
        )

    def test_youtube_oauth_state_matches_session_or_validates_signed_fallback(self):
        state = create_oauth_state(
            "google-subject",
            "test-signing-secret",
            now=1000,
            nonce="pending-state",
        )

        self.assertTrue(
            consume_oauth_state(
                state,
                state,
                "google-subject",
                "test-signing-secret",
                now=1001,
            )
        )
        self.assertTrue(
            consume_oauth_state(
                state,
                None,
                "google-subject",
                "test-signing-secret",
                now=1001,
            )
        )
        self.assertFalse(
            consume_oauth_state(
                state,
                "different-state",
                "google-subject",
                "test-signing-secret",
                now=1001,
            )
        )
        self.assertEqual(
            reuse_or_create_oauth_state(
                state,
                "google-subject",
                "test-signing-secret",
                now=1001,
            ),
            state,
        )
        self.assertNotEqual(
            reuse_or_create_oauth_state(
                state,
                "google-subject",
                "test-signing-secret",
                now=2000,
            ),
            state,
        )


if __name__ == "__main__":
    unittest.main()
