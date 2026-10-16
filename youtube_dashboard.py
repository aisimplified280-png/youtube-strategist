"""Read-only channel analytics and conservative, metadata-grounded suggestions."""

import csv
import gzip
import io
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from statistics import median

import httplib2
import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession

import youtube_analytics_connector as connector

CORE_METRICS = (
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "subscribersGained",
    "subscribersLost",
    "likes",
    "comments",
    "shares",
)
METRIC_LABELS = {
    "views": "Views",
    "impressions": "Impressions",
    "impressionsClickThroughRate": "CTR",
    "estimatedMinutesWatched": "Watch time (minutes)",
    "averageViewDuration": "Avg. view duration (seconds)",
    "subscribersGained": "Subscribers gained",
    "subscribersLost": "Subscribers lost",
    "likes": "Likes",
    "comments": "Comments",
    "shares": "Shares",
}
UNAVAILABLE_IMPRESSIONS_REASON = (
    "Unavailable: YouTube Analytics reports.query does not support thumbnail "
    "impressions; the YouTube Reporting API channel_reach_basic_a1 report is required."
)
UNAVAILABLE_CTR_REASON = (
    "Unavailable: YouTube Analytics reports.query does not support thumbnail CTR; "
    "the YouTube Reporting API channel_reach_basic_a1 report is required."
)
REACH_REPORT_TYPE_ID = "channel_reach_basic_a1"
REACH_REPORT_JOB_NAME = "AI Simplified Lab dashboard video reach"
REACH_IMPRESSIONS_COLUMN = "video_thumbnail_impressions"
REACH_CTR_COLUMN = "video_thumbnail_impressions_ctr"
REACH_DIMENSION_COLUMNS = ("date", "video_id")
TOPIC_TERMS = (
    (
        "prompt caching",
        re.compile(r"\bprompt\s+cach(?:e|ing)\b", re.IGNORECASE),
        "#PromptCaching",
        ("LLM prompt caching", "prompt caching"),
    ),
    (
        "semantic caching",
        re.compile(r"\bsemantic\s+cach(?:e|ing)\b", re.IGNORECASE),
        "#SemanticCaching",
        ("semantic caching",),
    ),
    (
        "RAG",
        re.compile(r"\bRAG\b|\bretrieval[- ]augmented generation\b", re.IGNORECASE),
        "#RAG",
        ("RAG", "retrieval augmented generation"),
    ),
    (
        "cross-validation",
        re.compile(r"\bcross[- ]validation\b", re.IGNORECASE),
        "#CrossValidation",
        ("cross validation", "machine learning"),
    ),
    (
        "dropout",
        re.compile(r"\bdropout\b", re.IGNORECASE),
        "#Dropout",
        ("dropout", "deep learning"),
    ),
    (
        "regularization",
        re.compile(r"\bregulari[sz]ation\b", re.IGNORECASE),
        "#Regularization",
        ("regularization", "machine learning"),
    ),
    (
        "non-deterministic AI",
        re.compile(r"\bnon[- ]deterministic\b", re.IGNORECASE),
        "#AIEvaluation",
        ("AI evaluation", "LLM testing"),
    ),
)


def _query_report(analytics, start_date, end_date, metrics, *, dimensions=None,
                  video_id=None, max_results=200):
    return connector._query_analytics(
        analytics,
        start_date.isoformat(),
        end_date.isoformat(),
        metrics,
        dimensions=dimensions,
        video_id=video_id,
        max_results=max_results,
    )


def _report_or_unavailable(analytics, start_date, end_date, metrics, *,
                           dimensions=None, video_id=None):
    try:
        rows = _query_report(
            analytics,
            start_date,
            end_date,
            metrics,
            dimensions=dimensions,
            video_id=video_id,
        )
    except connector.HttpError as error:
        status = getattr(error.resp, "status", "unknown")
        return [], f"Unavailable: Analytics API returned HTTP {status}."
    return rows, "Available" if rows else "No rows returned for this range."


def _metric_values(rows):
    if not rows:
        return {metric: None for metric in CORE_METRICS}
    return {
        metric: rows[0].get(metric)
        for metric in CORE_METRICS
    }


def _reporting_list_pages(resource, item_key, **parameters):
    items = []
    page_token = None
    while True:
        page_parameters = dict(parameters)
        if page_token:
            page_parameters["pageToken"] = page_token
        response = resource.list(**page_parameters).execute()
        items.extend(response.get(item_key, []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return items


def _get_reach_report_job(reporting):
    jobs = _reporting_list_pages(reporting.jobs(), "jobs")
    current_time = datetime.now(timezone.utc)
    for job in jobs:
        if job.get("reportTypeId") != REACH_REPORT_TYPE_ID:
            continue
        expire_time = job.get("expireTime")
        if expire_time:
            try:
                expires_at = datetime.fromisoformat(
                    expire_time.replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if expires_at <= current_time:
                continue
        return job, False

    report_types = _reporting_list_pages(
        reporting.reportTypes(),
        "reportTypes",
    )
    if REACH_REPORT_TYPE_ID not in {
        item.get("id") for item in report_types
    }:
        return None, False

    job = reporting.jobs().create(
        body={
            "reportTypeId": REACH_REPORT_TYPE_ID,
            "name": REACH_REPORT_JOB_NAME,
        }
    ).execute()
    return job, True


def _utc_midnight(value):
    return datetime.combine(value, time.min, tzinfo=timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )


def _reach_report_coverage(reports, start_date, end_date):
    covered_days = set()
    for report in reports:
        start_time = report.get("startTime")
        if not isinstance(start_time, str):
            return None
        try:
            report_day = datetime.fromisoformat(
                start_time.replace("Z", "+00:00")
            ).date()
        except ValueError:
            return None
        if start_date <= report_day <= end_date:
            covered_days.add(report_day)
    return len(covered_days)


def _download_report_csv(session, download_url):
    response = session.get(download_url, timeout=60)
    response.raise_for_status()
    content = response.content
    if content.startswith(b"\x1f\x8b"):
        content = gzip.decompress(content)
    return content.decode("utf-8-sig")


def _parse_reach_rows(content, start_date, end_date, video_ids):
    rows = csv.DictReader(io.StringIO(content))
    required_columns = set(REACH_DIMENSION_COLUMNS)
    if not rows.fieldnames or not required_columns.issubset(rows.fieldnames):
        raise ValueError("The reach report is missing required columns.")
    metric_columns = {
        "impressions": REACH_IMPRESSIONS_COLUMN in rows.fieldnames,
        "ctr": REACH_CTR_COLUMN in rows.fieldnames,
    }

    result = {}
    for row in rows:
        video_id = row.get("video_id", "")
        report_date = row.get("date", "")
        if video_id not in video_ids:
            continue
        if not report_date:
            raise ValueError("The reach report contains a row without a date.")
        try:
            day = date.fromisoformat(report_date)
        except ValueError as error:
            raise ValueError("The reach report contains an invalid date.") from error
        if not start_date <= day <= end_date:
            continue

        impressions_text = (
            row.get(REACH_IMPRESSIONS_COLUMN, "").strip()
            if metric_columns["impressions"]
            else ""
        )
        ctr_text = (
            row.get(REACH_CTR_COLUMN, "").strip()
            if metric_columns["ctr"]
            else ""
        )
        try:
            impressions = float(impressions_text) if impressions_text else None
            ctr = float(ctr_text) if ctr_text else None
        except ValueError as error:
            raise ValueError("The reach report contains nonnumeric metrics.") from error
        if impressions is not None and (
            not math.isfinite(impressions)
            or not impressions.is_integer()
            or impressions < 0
        ):
            raise ValueError("The reach report contains invalid impressions.")
        if ctr is not None and (not math.isfinite(ctr) or ctr < 0):
            raise ValueError("The reach report contains an invalid click-through rate.")
        aggregate = result.setdefault(
            video_id,
            {
                "impressions": 0,
                "impressions_rows": 0,
                "ctr_weighted_sum": 0.0,
                "ctr_impressions": 0.0,
                "ctr_incomplete": False,
            },
        )
        if impressions is not None:
            aggregate["impressions"] += int(impressions)
            aggregate["impressions_rows"] += 1
        if ctr is not None and impressions is not None and impressions > 0:
            aggregate["ctr_weighted_sum"] += ctr * impressions
            aggregate["ctr_impressions"] += impressions
        elif impressions is not None and impressions > 0:
            aggregate["ctr_incomplete"] = True

    return {
        "metrics": {
            video_id: {
                "impressions": (
                    values["impressions"] if values["impressions_rows"] else None
                ),
                "ctr": (
                    values["ctr_weighted_sum"] / values["ctr_impressions"]
                    if values["ctr_impressions"]
                    else None
                ),
                "ctr_incomplete": values["ctr_incomplete"],
            }
            for video_id, values in result.items()
        },
        "metric_columns": metric_columns,
    }


def _unavailable_reach_metrics(video_ids, reason):
    return {
        video_id: {
            "impressions": None,
            "impressions_status": reason,
            "ctr": None,
            "ctr_status": reason,
        }
        for video_id in video_ids
    }


def fetch_reach_metrics(
    reporting,
    credentials,
    video_ids,
    start_date,
    end_date,
    *,
    download_session=None,
):
    """Read thumbnail reach metrics from the asynchronous Reporting API report."""
    unavailable = _unavailable_reach_metrics(
        video_ids,
        "Unavailable: YouTube Analytics reports.query does not provide thumbnail "
        "reach metrics; the YouTube Reporting API has not returned values.",
    )
    if reporting is None or credentials is None:
        reason = "Unavailable: the YouTube Reporting API client is not configured."
        return _unavailable_reach_metrics(video_ids, reason), reason
    if not video_ids:
        return unavailable, "No eligible videos were returned for this date range."

    try:
        job, job_created = _get_reach_report_job(reporting)
    except connector.HttpError as error:
        status = getattr(error.resp, "status", "unknown")
        reason = (
            f"Unavailable: YouTube Reporting API returned HTTP {status}. "
            "Enable the YouTube Reporting API and reconnect if required."
        )
        return _unavailable_reach_metrics(video_ids, reason), reason
    except (
        GoogleAuthError,
        httplib2.HttpLib2Error,
        OSError,
        requests.RequestException,
    ):
        reason = (
            "Unavailable: the YouTube Reporting API request failed. Reconnect "
            "YouTube authorization and check network/API availability."
        )
        return _unavailable_reach_metrics(video_ids, reason), reason
    if job is None:
        reason = (
            f"Unavailable: the YouTube Reporting API did not offer "
            f"{REACH_REPORT_TYPE_ID} for this channel."
        )
        return _unavailable_reach_metrics(video_ids, reason), reason

    end_exclusive = end_date + timedelta(days=1)
    try:
        reports = _reporting_list_pages(
            reporting.jobs().reports(),
            "reports",
            jobId=job["id"],
            startTimeAtOrAfter=_utc_midnight(start_date),
            startTimeBefore=_utc_midnight(end_exclusive),
        )
    except connector.HttpError as error:
        status = getattr(error.resp, "status", "unknown")
        reason = f"Unavailable: reach report listing failed (HTTP {status})."
        return _unavailable_reach_metrics(video_ids, reason), reason
    except (
        GoogleAuthError,
        httplib2.HttpLib2Error,
        OSError,
        requests.RequestException,
    ):
        reason = "Unavailable: the YouTube reach report listing request failed."
        return _unavailable_reach_metrics(video_ids, reason), reason
    if not reports:
        if job_created:
            reason = (
                "Reach reporting was just enabled. The first daily report may take up "
                "to 48 hours, and data before the job was created may not be available."
            )
            return _unavailable_reach_metrics(video_ids, reason), reason
        reason = (
            "No reach report files are available for the selected dates. "
            "YouTube generates reach reports asynchronously; recent dates may not "
            "be ready yet."
        )
        return _unavailable_reach_metrics(video_ids, reason), reason

    session = download_session or AuthorizedSession(credentials)
    collected = []
    supported_columns = {"impressions": True, "ctr": True}
    try:
        for report in reports:
            csv_content = _download_report_csv(session, report["downloadUrl"])
            parsed = _parse_reach_rows(
                csv_content,
                start_date,
                end_date,
                set(video_ids),
            )
            collected.extend(parsed["metrics"].items())
            for metric, supported in parsed["metric_columns"].items():
                supported_columns[metric] = (
                    supported_columns[metric] and supported
                )
    except (
        requests.RequestException,
        OSError,
        UnicodeDecodeError,
        ValueError,
        KeyError,
        TypeError,
    ):
        reason = (
            "Unavailable: a reach report could not be downloaded or its schema "
            "did not match the documented reach report."
        )
        return _unavailable_reach_metrics(video_ids, reason), reason

    combined = {}
    for video_id, metrics in collected:
        target = combined.setdefault(
            video_id,
            {
                "impressions": 0,
                "has_impressions": False,
                "ctr_weighted_sum": 0.0,
                "ctr_impressions": 0.0,
                "ctr_incomplete": False,
            },
        )
        if metrics["impressions"] is not None:
            target["impressions"] += metrics["impressions"]
            target["has_impressions"] = True
        if metrics["ctr"] is not None and metrics["impressions"]:
            target["ctr_weighted_sum"] += (
                metrics["ctr"] * metrics["impressions"]
            )
            target["ctr_impressions"] += metrics["impressions"]
        if metrics["ctr_incomplete"]:
            target["ctr_incomplete"] = True

    for video_id in video_ids:
        metrics = combined.get(
            video_id,
            {
                "impressions": 0,
                "has_impressions": False,
                "ctr_weighted_sum": 0.0,
                "ctr_impressions": 0.0,
                "ctr_incomplete": False,
            },
        )
        unavailable[video_id] = {
            "impressions": (
                metrics["impressions"]
                if metrics["has_impressions"] and supported_columns["impressions"]
                else None
            ),
            "impressions_status": (
                "Available from YouTube Reporting API reach report."
                if supported_columns["impressions"] and metrics["has_impressions"]
                else "Unavailable: the returned reach report does not contain "
                "video_thumbnail_impressions."
                if not supported_columns["impressions"]
                else "No impressions row for this video/date range; not treated as zero."
            ),
            "ctr": (
                metrics["ctr_weighted_sum"] / metrics["ctr_impressions"]
                if (
                    metrics["ctr_impressions"]
                    and supported_columns["ctr"]
                    and not metrics["ctr_incomplete"]
                )
                else None
            ),
            "ctr_status": (
                "Available from YouTube Reporting API reach report; CTR is "
                "impression-weighted across returned daily rows."
                if (
                    metrics["ctr_impressions"]
                    and supported_columns["ctr"]
                    and not metrics["ctr_incomplete"]
                )
                else "Unavailable: the returned reach report does not contain "
                "video_thumbnail_impressions_ctr."
                if not supported_columns["ctr"]
                else "Unavailable: CTR is incomplete for the selected date range."
                if metrics["ctr_incomplete"]
                else "Unavailable: CTR is missing from returned reach report rows."
            ),
        }

    expected_days = (end_date - start_date).days + 1
    covered_days = _reach_report_coverage(reports, start_date, end_date)
    if covered_days is None:
        coverage_status = (
            f"Loaded {len(reports)} daily reach report file(s), but their coverage "
            "could not be confirmed; displayed reach values may be partial."
        )
    elif covered_days < expected_days:
        coverage_status = (
            f"Partial reach coverage: reports are available for {covered_days} of "
            f"{expected_days} selected days. Displayed reach totals and CTR use only "
            "the returned daily reports and are incomplete for this date range."
        )
    else:
        coverage_status = (
            f"Loaded daily reach reports for all {expected_days} selected days. "
            "CTR is impression-weighted across those reports."
        )
    return unavailable, coverage_status


def _list_dashboard_videos(youtube, limit, end_date):
    playlist_id = connector._channel_uploads_playlist(youtube)
    video_ids = []
    page_token = None
    while len(video_ids) < limit:
        response = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=min(50, limit - len(video_ids)),
            pageToken=page_token,
        ).execute()
        batch = [
            item["contentDetails"]["videoId"]
            for item in response.get("items", [])
            if item.get("contentDetails", {}).get("videoId")
        ]
        if not batch:
            break
        details = connector._fetch_video_details(youtube, batch)
        for video_id in batch:
            video = details.get(video_id)
            if not video:
                continue
            published = video.get("snippet", {}).get("publishedAt", "")
            if published and date.fromisoformat(published[:10]) <= end_date:
                video_ids.append(video_id)
                if len(video_ids) >= limit:
                    break
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return connector._fetch_video_details(youtube, video_ids)


def _format_group(video):
    duration = connector._format_assessment(video)
    if duration["duration_seconds"] > connector.YOUTUBE_MAX_SHORT_DURATION_SECONDS:
        return "Long-form (>180 sec)"
    return "Short-duration candidate (<=180 sec)"


def _dashboard_video_row(
    video,
    metrics_rows,
    metrics_status,
    end_date,
    reach_metrics=None,
):
    video_id = video["id"]
    snippet = video.get("snippet", {})
    published = snippet.get("publishedAt", "")
    age_days = (
        max(1, (end_date - date.fromisoformat(published[:10])).days)
        if published else None
    )
    duration = connector._format_assessment(video)
    reach = (reach_metrics or {}).get(video_id, {})
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": snippet.get("title", ""),
        "published_at": published,
        "age_days": age_days,
        "duration_seconds": duration["duration_seconds"],
        "format": _format_group(video),
        "shorts_classification": duration["short_classification"],
        "category_id": snippet.get("categoryId"),
        "snippet": snippet,
        **_metric_values(metrics_rows),
        "analytics_status": (
            "Available" if metrics_rows else metrics_status
        ),
        "impressions": reach.get("impressions"),
        "impressions_status": reach.get(
            "impressions_status",
            UNAVAILABLE_IMPRESSIONS_REASON,
        ),
        "ctr": reach.get("ctr"),
        "ctr_status": reach.get("ctr_status", UNAVAILABLE_CTR_REASON),
    }


def fetch_video_dashboard_row(
    youtube,
    analytics,
    reference,
    start_date,
    end_date,
    *,
    reporting=None,
    credentials=None,
):
    video = connector._resolve_video(youtube, reference)
    metrics_rows, metrics_status = _report_or_unavailable(
        analytics,
        start_date,
        end_date,
        ",".join(CORE_METRICS),
        video_id=video["id"],
    )
    reach_metrics, _ = fetch_reach_metrics(
        reporting,
        credentials,
        [video["id"]],
        start_date,
        end_date,
    )
    return _dashboard_video_row(
        video,
        metrics_rows,
        metrics_status,
        end_date,
        reach_metrics,
    )


def load_dashboard_data(youtube, analytics, start_date, end_date, *,
                        video_limit=100, reporting=None, credentials=None):
    if not isinstance(start_date, date) or not isinstance(end_date, date):
        raise ValueError("Start and end dates must be calendar dates.")
    if start_date > end_date:
        raise ValueError("Start date must be on or before end date.")
    if end_date >= date.today():
        raise ValueError("The Analytics end date must be yesterday or earlier.")
    if not 1 <= video_limit <= 250:
        raise ValueError("The video catalogue limit must be between 1 and 250.")

    summary_rows, summary_status = _report_or_unavailable(
        analytics,
        start_date,
        end_date,
        ",".join(CORE_METRICS),
    )
    daily_rows, daily_status = _report_or_unavailable(
        analytics,
        start_date,
        end_date,
        ",".join(CORE_METRICS),
        dimensions="day",
    )
    channel_traffic, channel_traffic_status = _report_or_unavailable(
        analytics,
        start_date,
        end_date,
        "views,estimatedMinutesWatched",
        dimensions="insightTrafficSourceType",
    )

    details_by_id = _list_dashboard_videos(youtube, video_limit, end_date)
    reach_metrics, reach_status = fetch_reach_metrics(
        reporting,
        credentials,
        list(details_by_id),
        start_date,
        end_date,
    )
    video_rows = []
    api_warnings = []
    if reach_status.startswith("Unavailable:"):
        api_warnings.append(reach_status)
    for video in details_by_id.values():
        video_id = video["id"]
        metrics_rows, metrics_status = _report_or_unavailable(
            analytics,
            start_date,
            end_date,
            ",".join(CORE_METRICS),
            video_id=video_id,
        )
        if metrics_status.startswith("Unavailable:"):
            api_warnings.append(metrics_status)
        video_rows.append(
            _dashboard_video_row(
                video,
                metrics_rows,
                metrics_status,
                end_date,
                reach_metrics,
            )
        )

    return {
        "channel_id": connector.CHANNEL_ID,
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "summary": summary_rows[0] if summary_rows else None,
        "summary_status": summary_status,
        "daily_rows": daily_rows,
        "daily_status": daily_status,
        "traffic_sources": channel_traffic,
        "traffic_status": channel_traffic_status,
        "reach_status": reach_status,
        "video_rows": video_rows,
        "catalogue_limit": video_limit,
        "catalogue_note": (
            f"Analytics evaluated for the {len(video_rows)} most recent eligible "
            f"uploads in a catalogue capped at {video_limit}; older uploads are not included."
        ),
        "api_warnings": sorted(set(api_warnings)),
        "unavailable_metrics": {
            "impressions": UNAVAILABLE_IMPRESSIONS_REASON,
            "ctr": UNAVAILABLE_CTR_REASON,
        },
    }


def fetch_video_traffic_sources(analytics, video_id, start_date, end_date):
    return _report_or_unavailable(
        analytics,
        start_date,
        end_date,
        "views,estimatedMinutesWatched",
        dimensions="insightTrafficSourceType",
        video_id=video_id,
    )


def fetch_video_audience_demographics(analytics, video_id, start_date, end_date):
    return _report_or_unavailable(
        analytics,
        start_date,
        end_date,
        "viewerPercentage",
        dimensions="ageGroup,gender",
        video_id=video_id,
    )


def rank_optimization_candidates(video_rows, *, limit=5):
    """Rank only measured videos against same-format, age-matched peers."""
    recommendations = []
    for video in video_rows:
        views = video.get("views")
        if not isinstance(views, (int, float)):
            continue
        peers = [
            peer for peer in video_rows
            if peer["video_id"] != video["video_id"]
            and peer["format"] == video["format"]
            and peer.get("age_days") is not None
            and video.get("age_days") is not None
            and abs(peer["age_days"] - video["age_days"])
            <= max(14, int(video["age_days"] * 0.5))
            and isinstance(peer.get("views"), (int, float))
        ]
        if len(peers) < 3:
            continue
        peer_views_median = median(peer["views"] for peer in peers)
        if peer_views_median <= 0:
            continue
        relative_views = (views - peer_views_median) / peer_views_median
        if relative_views >= 0:
            continue

        peer_duration_values = [
            peer["averageViewDuration"]
            for peer in peers
            if isinstance(peer.get("averageViewDuration"), (int, float))
        ]
        peer_duration_median = (
            median(peer_duration_values) if peer_duration_values else None
        )
        average_duration = video.get("averageViewDuration")
        if (
            isinstance(average_duration, (int, float))
            and peer_duration_median is not None
            and average_duration > peer_duration_median
        ):
            evidence = (
                f"{views:g} views vs. {peer_views_median:g} median views across "
                f"{len(peers)} age- and format-matched channel videos, while average "
                f"view duration is {average_duration:g}s vs. their "
                f"{peer_duration_median:g}s median. Review the title/description "
                "for a clearer match to the video's existing topic."
            )
        else:
            evidence = (
                f"{views:g} views vs. {peer_views_median:g} median views across "
                f"{len(peers)} age- and format-matched channel videos. This is a "
                "metadata review candidate, not evidence that metadata caused the gap."
            )
        recommendations.append({
            **video,
            "peer_count": len(peers),
            "peer_views_median": peer_views_median,
            "relative_views_gap": relative_views,
            "peer_average_view_duration_median": peer_duration_median,
            "recommendation_evidence": evidence,
        })

    recommendations.sort(
        key=lambda item: (item["relative_views_gap"], -item["peer_count"])
    )
    return recommendations[:limit]


def generate_metadata_proposal(snippet):
    """Suggest only metadata terms directly evidenced by the existing snippet."""
    title = snippet.get("title", "")
    description = snippet.get("description", "")
    combined = f"{title}\n{description}"
    proposed_title = title
    proposed_description = description
    proposed_tags = list(snippet.get("tags", []))
    detected_topics = []
    new_hashtags = []
    new_tags = []
    existing_tag_keys = {tag.casefold() for tag in proposed_tags}
    hashtag_count = len(re.findall(
        r"(?<![\w#])#([^\s#]+)",
        f"{title}\n{description}",
    ))

    for label, pattern, hashtag, tags in TOPIC_TERMS:
        if not pattern.search(combined):
            continue
        detected_topics.append(label)
        if hashtag.casefold() not in combined.casefold() and all(
            hashtag.casefold() != existing.casefold()
            for existing in new_hashtags
        ):
            candidate_hashtags = [*new_hashtags, hashtag]
            candidate_description = (
                f"{description.rstrip()}\n\n{' '.join(candidate_hashtags)}"
                if description.strip()
                else " ".join(candidate_hashtags)
            )
            if hashtag_count < 15 and len(candidate_description) <= 5000:
                new_hashtags.append(hashtag)
                hashtag_count += 1
        for tag in tags:
            if tag.casefold() not in existing_tag_keys:
                if len(",".join([*proposed_tags, *new_tags, tag])) <= 500:
                    new_tags.append(tag)
                    existing_tag_keys.add(tag.casefold())

    if (
        re.search(r"\bprompt\s+caching\b", description, re.IGNORECASE)
        and re.search(r"\bLLM\s+cache\b", proposed_title, re.IGNORECASE)
        and not re.search(r"\bLLM\s+prompt\s+cache", proposed_title, re.IGNORECASE)
    ):
        candidate_title = re.sub(
            r"\bLLM\s+cache\b",
            "LLM Prompt Cache",
            proposed_title,
            count=1,
            flags=re.IGNORECASE,
        )
        if len(candidate_title) <= 100:
            proposed_title = candidate_title

    proposed_tags.extend(new_tags)
    if new_hashtags:
        proposed_description = (
            f"{description.rstrip()}\n\n{' '.join(new_hashtags)}"
            if description.strip()
            else " ".join(new_hashtags)
        )
    proposal_snippet = {
        **snippet,
        "title": proposed_title,
        "description": proposed_description,
        "tags": proposed_tags,
    }
    connector._validate_metadata(proposal_snippet)
    explanation = (
        "Suggested search terms are derived from words already present in the "
        "current title and description; existing description text, links, and tags "
        "are retained. No video content was transcribed."
    )
    if not detected_topics:
        explanation += (
            " No recognized topic phrase was found, so the title and tags are left "
            "unchanged rather than inventing a claim."
        )
    else:
        explanation += " Detected topics: " + ", ".join(detected_topics) + "."

    return {
        "title": proposed_title,
        "description": proposed_description,
        "tags": proposed_tags,
        "explanation": explanation,
        "detected_topics": detected_topics,
    }


def default_date_range(today=None):
    today = today or date.today()
    end_date = today - timedelta(days=1)
    return end_date - timedelta(days=27), end_date
