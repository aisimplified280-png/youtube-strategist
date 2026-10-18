"""Parse and analyse YouTube Studio CSV/TSV exports without persisting uploaded files.

The parser intentionally recognizes only known metric families. Unknown columns are reported
as ignored rather than guessed into a metric, and all analysis stays in the current session.
"""
from __future__ import annotations

import csv
import io
import math
import re
from collections import defaultdict
from datetime import datetime
from statistics import median


HEADER_ALIASES = {
    "video_title": {
        "video title", "content title", "title", "video name", "content",
    },
    "video_id": {"video id", "content id", "video identifier"},
    "date": {"date", "day", "report date", "date published"},
    "views": {"views", "engaged views", "organic views"},
    "watch_time": {
        "watch time hours", "watch time (hours)", "watch time minutes",
        "watch time (minutes)", "estimated minutes watched",
        "estimated minutes watched (minutes)",
    },
    "average_view_duration": {
        "average view duration", "avg view duration", "average view duration seconds",
        "average view duration (seconds)",
    },
    "average_view_percentage": {
        "average percentage viewed", "average view percentage",
        "average percentage viewed (%)", "average view duration percentage",
    },
    "impressions": {"impressions", "thumbnail impressions", "video thumbnail impressions"},
    "ctr": {
        "impressions click through rate", "impressions click-through rate",
        "click through rate", "click-through rate", "video thumbnail impressions ctr",
        "thumbnail click through rate",
    },
    "subscribers_gained": {"subscribers gained", "subscribers"},
    "subscribers_lost": {"subscribers lost"},
    "likes": {"likes", "likes (vs dislikes)"},
    "comments": {"comments", "comment count"},
    "shares": {"shares", "share count"},
    "traffic_source": {
        "traffic source", "traffic source type", "youtube traffic source",
        "insight traffic source type",
    },
    "geography": {"geography", "country", "country code", "region"},
    "age_group": {"age group", "viewer age", "age"},
    "gender": {"gender", "viewer gender"},
    "device_type": {"device type", "device"},
    "elapsed_time": {
        "elapsed video time", "elapsed time", "video time", "elapsed video time (seconds)",
    },
    "retention_percent": {
        "audience retention", "audience retention percentage", "retention rate",
        "audience watch ratio", "absolute audience retention",
    },
    "relative_retention": {"relative audience retention", "relative retention"},
}

NUMERIC_FIELDS = {
    "views", "watch_time", "average_view_duration", "average_view_percentage",
    "impressions", "ctr", "subscribers_gained", "subscribers_lost",
    "likes", "comments", "shares", "retention_percent", "relative_retention",
}


class StudioExportError(ValueError):
    """Raised when an upload is unsupported or has no recognizable report header."""


def _norm(value: str) -> str:
    value = (value or "").replace("\ufeff", "").strip().casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[_:/()%-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


_NORMALIZED_ALIASES = {
    field: {_norm(alias) for alias in aliases}
    for field, aliases in HEADER_ALIASES.items()
}


def _map_header(value: str):
    normalized = _norm(value)
    if not normalized:
        return None
    for field, aliases in _NORMALIZED_ALIASES.items():
        if normalized in aliases:
            return field
    # Common export variants where the label includes a unit or explanatory suffix.
    if normalized.startswith("average view duration"):
        return "average_view_duration"
    if normalized.startswith("average percentage viewed"):
        return "average_view_percentage"
    if normalized.startswith("impressions click through rate"):
        return "ctr"
    if normalized.startswith("video thumbnail impressions ctr"):
        return "ctr"
    if normalized.startswith("video thumbnail impressions"):
        return "impressions"
    if normalized.startswith("estimated minutes watched"):
        return "watch_time"
    if normalized.startswith("watch time") and "hour" in normalized:
        return "watch_time"
    if normalized.startswith("watch time") and "minute" in normalized:
        return "watch_time"
    if "relative audience retention" in normalized:
        return "relative_retention"
    if "audience retention" in normalized:
        return "retention_percent"
    if normalized.startswith("elapsed video time"):
        return "elapsed_time"
    return None


def _parse_number(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.casefold() in {"—", "-", "n/a", "na", "not available", "(not set)"}:
        return None
    text = text.replace(",", "").replace("\u2212", "-")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    text = text.replace("%", "").strip()
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", text)
    if not match:
        return None
    try:
        result = float(match.group(0))
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _parse_duration_seconds(value):
    text = str(value or "").strip()
    if not text:
        return None
    # Studio may display average duration as M:SS or H:MM:SS.
    if re.fullmatch(r"\d{1,3}:\d{2}(?::\d{2})?", text):
        chunks = [int(part) for part in text.split(":")]
        if len(chunks) == 2:
            return chunks[0] * 60 + chunks[1]
        return chunks[0] * 3600 + chunks[1] * 60 + chunks[2]
    return _parse_number(text)


def _parse_elapsed(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("%"):
        number = _parse_number(text)
        return {"seconds": None, "percent": number}
    return {"seconds": _parse_duration_seconds(text), "percent": None}


def _is_percent_label(header):
    return "%" in (header or "") or "percent" in (header or "").casefold() or "rate" in (header or "").casefold()


def _to_percent(value, raw, header):
    if value is None:
        return None
    if "%" in str(raw) or _is_percent_label(header):
        return value
    # CSVs can represent percentages as decimal ratios even when the displayed label omits %.
    if 0 <= value <= 1:
        return value * 100
    return value


def _detect_delimiter(text):
    sample = "\n".join(text.splitlines()[:80])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        return dialect.delimiter
    except csv.Error:
        candidates = [",", "\t", ";"]
        lines = text.splitlines()[:80]
        scores = {}
        for delimiter in candidates:
            counts = [line.count(delimiter) for line in lines if line.strip()]
            scores[delimiter] = (sum(count > 0 for count in counts), sum(counts))
        return max(candidates, key=lambda item: scores[item])


def _decode_upload(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise StudioExportError("Could not decode this file. Export it as CSV or TSV and upload again.")


def parse_studio_export(filename: str, data: bytes) -> dict:
    """Parse one Studio CSV/TSV export and return normalized in-memory records."""
    suffix = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    if suffix not in {"csv", "tsv", "txt"}:
        raise StudioExportError("Upload a YouTube Studio CSV or TSV export. Excel workbooks are not supported yet.")
    text = _decode_upload(data)
    if not text.strip():
        raise StudioExportError("This file is empty.")

    delimiter = "\t" if suffix == "tsv" else _detect_delimiter(text)
    try:
        matrix = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error as error:
        raise StudioExportError("The file could not be parsed as a CSV/TSV report.") from error
    if not matrix:
        raise StudioExportError("This file contains no rows.")

    header_candidates = []
    for index, row in enumerate(matrix[:60]):
        mapped = {}
        for column_index, label in enumerate(row):
            field = _map_header(label)
            if field and field not in mapped:
                mapped[field] = column_index
        if len(mapped) >= 2:
            # Prefer a complete report header and break ties by reaching one later in preamble.
            important = sum(field in mapped for field in (
                "video_title", "views", "impressions", "ctr",
                "elapsed_time", "retention_percent", "relative_retention",
            ))
            header_candidates.append((len(mapped) * 10 + important, index, mapped, row))
    if not header_candidates:
        raise StudioExportError(
            "I couldn't identify a YouTube Studio report header. Upload an export from "
            "Analytics → Advanced mode / See more → Export current view."
        )
    _, header_index, columns, header_row = max(header_candidates, key=lambda item: (item[0], item[1]))
    header_labels = {field: header_row[index].strip() for field, index in columns.items()}
    records = []
    for row in matrix[header_index + 1:]:
        if not any(str(cell).strip() for cell in row):
            continue
        if len(row) <= max(columns.values(), default=-1):
            row = row + [""] * (max(columns.values()) + 1 - len(row))
        # Ignore repeated header lines and completely unmeasured table footers.
        if any(_map_header(cell) == field and str(cell).strip()
               for field, index in columns.items() for cell in [row[index]]):
            continue
        record = {}
        for field, index in columns.items():
            raw = str(row[index]).strip() if index < len(row) else ""
            label = header_labels.get(field, "")
            if field in {"video_title", "video_id", "date", "traffic_source", "geography",
                         "age_group", "gender", "device_type"}:
                record[field] = raw or None
            elif field == "elapsed_time":
                elapsed = _parse_elapsed(raw)
                record["elapsed_seconds"] = elapsed["seconds"]
                record["elapsed_percent"] = elapsed["percent"]
                record["elapsed_label"] = raw or None
            elif field == "watch_time":
                number = _parse_number(raw)
                normalized_label = _norm(label)
                if number is not None and "hour" in normalized_label:
                    number *= 60
                record["watch_time_minutes"] = number
            elif field == "average_view_duration":
                record["average_view_duration_seconds"] = _parse_duration_seconds(raw)
            elif field == "average_view_percentage":
                number = _parse_number(raw)
                record["average_view_percentage"] = _to_percent(number, raw, label)
            elif field == "ctr":
                number = _parse_number(raw)
                record["ctr_percent"] = _to_percent(number, raw, label)
            elif field == "retention_percent":
                number = _parse_number(raw)
                record["retention_percent"] = _to_percent(number, raw, label)
            elif field == "relative_retention":
                record["relative_retention_index"] = _parse_number(raw)
            else:
                number = _parse_number(raw)
                if field in {"views", "impressions", "subscribers_gained", "subscribers_lost",
                             "likes", "comments", "shares"} and number is not None:
                    number = int(number) if number.is_integer() else number
                record[field] = number
        if any(value is not None for key, value in record.items() if key not in {"video_title", "date", "traffic_source"}):
            records.append(record)

    if not records:
        raise StudioExportError("A report header was found, but no rows with usable measurements followed it.")

    fields = set(columns)
    if "elapsed_time" in fields and ({"retention_percent", "relative_retention"} & fields):
        report_type = "retention"
    elif "video_title" in fields and ({"views", "impressions", "ctr", "watch_time"} & fields):
        report_type = "video_performance"
    elif "traffic_source" in fields:
        report_type = "traffic_sources"
    elif "date" in fields and ({"views", "watch_time"} & fields):
        report_type = "daily_trend"
    else:
        report_type = "analytics_table"

    return {
        "filename": filename,
        "report_type": report_type,
        "delimiter": "tab" if delimiter == "\t" else ("semicolon" if delimiter == ";" else "comma"),
        "header_row": header_index + 1,
        "header_labels": header_labels,
        "fields": sorted(fields),
        "ignored_headers": [
            str(label).strip() for label in header_row
            if str(label).strip() and _map_header(label) is None
        ],
        "records": records,
    }


def _weighted_average(rows, value_key, weight_key):
    values = [(row.get(value_key), row.get(weight_key)) for row in rows]
    values = [(value, weight if isinstance(weight, (int, float)) and weight > 0 else 1)
              for value, weight in values if isinstance(value, (int, float))]
    if not values:
        return None
    total_weight = sum(weight for _, weight in values)
    return sum(value * weight for value, weight in values) / total_weight if total_weight else None


def _video_summaries(records):
    groups = defaultdict(list)
    for row in records:
        title = row.get("video_title")
        video_id = row.get("video_id")
        if not title and not video_id:
            continue
        if title and title.strip().casefold() in {"total", "overall", "all videos", "grand total"}:
            continue
        key = ("id", video_id) if video_id else ("title", _norm(title))
        groups[key].append(row)

    result = []
    for (key_type, key_value), rows in groups.items():
        first = rows[0]
        item = {
            "video_id": first.get("video_id"),
            "video_title": first.get("video_title") or first.get("video_id"),
            "row_count": len(rows),
        }
        for field in ("views", "watch_time_minutes", "impressions", "subscribers_gained",
                      "subscribers_lost", "likes", "comments", "shares"):
            available = [row.get(field) for row in rows if isinstance(row.get(field), (int, float))]
            item[field] = sum(available) if available else None
        item["ctr_percent"] = _weighted_average(rows, "ctr_percent", "impressions")
        item["average_view_duration_seconds"] = _weighted_average(
            rows, "average_view_duration_seconds", "views"
        )
        item["average_view_percentage"] = _weighted_average(
            rows, "average_view_percentage", "views"
        )
        date_values = [row.get("date") for row in rows if row.get("date")]
        item["date_start"] = min(date_values) if date_values else None
        item["date_end"] = max(date_values) if date_values else None
        result.append(item)
    return result


def _elapsed_sort_key(row):
    if isinstance(row.get("elapsed_seconds"), (int, float)):
        return row["elapsed_seconds"]
    if isinstance(row.get("elapsed_percent"), (int, float)):
        return row["elapsed_percent"] * 10000
    return float("inf")


def _retention_recommendations(records):
    recommendations = []
    absolute = sorted(
        [row for row in records if isinstance(row.get("retention_percent"), (int, float))],
        key=_elapsed_sort_key,
    )
    if len(absolute) >= 3:
        changes = []
        for previous, current in zip(absolute, absolute[1:]):
            drop = previous["retention_percent"] - current["retention_percent"]
            if drop > 0:
                changes.append((drop, previous, current))
        for drop, previous, current in sorted(changes, key=lambda item: item[0], reverse=True)[:3]:
            if drop < 3:
                continue
            start = previous.get("elapsed_label") or "previous point"
            end = current.get("elapsed_label") or "next point"
            recommendations.append({
                "priority": "Review",
                "title": f"Inspect the retention drop between {start} and {end}",
                "evidence": f"Reported audience retention decreased by {drop:.1f} percentage points between adjacent exported points.",
                "action": "Review the visual/narration transition and whether it matches the promise of the opening. The curve shows where to inspect, not why viewers left.",
            })
        if not recommendations:
            recommendations.append({
                "priority": "Monitor",
                "title": "No large adjacent retention drop surfaced in this export",
                "evidence": "The parsed absolute-retention points do not contain an adjacent drop of 3 percentage points or more.",
                "action": "Compare the curve against the opening promise and inspect the raw report for smaller dips or spikes.",
            })
    relative = [row for row in records if isinstance(row.get("relative_retention_index"), (int, float))]
    if relative:
        recommendations.append({
            "priority": "Context",
            "title": "Relative retention is a comparison index, not audience-retained percentage",
            "evidence": "This export contains relative audience-retention values.",
            "action": "Use the relative line to compare interest at equivalent moments, but do not interpret its values as the exact percent of viewers still watching.",
        })
    return recommendations


def _performance_recommendations(videos):
    recommendations = []
    ctr_rows = [
        row for row in videos
        if isinstance(row.get("ctr_percent"), (int, float))
        and isinstance(row.get("impressions"), (int, float))
        and row["impressions"] >= 100
    ]
    if len(ctr_rows) >= 3:
        midpoint = median(row["ctr_percent"] for row in ctr_rows)
        low_ctr = sorted(
            [row for row in ctr_rows if row["ctr_percent"] < midpoint],
            key=lambda row: (row["ctr_percent"], -row["impressions"]),
        )[:5]
        for row in low_ctr[:3]:
            recommendations.append({
                "priority": "High" if row["impressions"] >= 1000 else "Review",
                "title": f"Review title/thumbnail alignment: {row['video_title']}",
                "evidence": (
                    f"Reported CTR {row['ctr_percent']:.2f}% is below this upload set's "
                    f"median ({midpoint:.2f}%) across {len(ctr_rows)} videos with at least 100 impressions."
                ),
                "action": "Check whether the title and thumbnail communicate one clear, accurate reason to click. Treat this as a review candidate, not proof the packaging caused the difference.",
                "video_id": row.get("video_id"),
                "video_title": row.get("video_title"),
            })
    retention_rows = [
        row for row in videos
        if isinstance(row.get("average_view_percentage"), (int, float))
    ]
    if len(retention_rows) >= 3:
        midpoint = median(row["average_view_percentage"] for row in retention_rows)
        lowest = sorted(retention_rows, key=lambda row: row["average_view_percentage"])[:3]
        for row in lowest:
            if row["average_view_percentage"] >= midpoint:
                continue
            recommendations.append({
                "priority": "Review",
                "title": f"Review opening and pacing: {row['video_title']}",
                "evidence": (
                    f"Average percentage viewed is {row['average_view_percentage']:.1f}%, "
                    f"below this export's median ({midpoint:.1f}%) across {len(retention_rows)} videos."
                ),
                "action": "Compare the opening promise with how quickly the video delivers value. Account for differences in video length and format before drawing conclusions.",
                "video_id": row.get("video_id"),
                "video_title": row.get("video_title"),
            })
    if not recommendations:
        recommendations.append({
            "priority": "Context",
            "title": "Not enough comparable video evidence for a ranked packaging review",
            "evidence": "This report has fewer than three videos with measured comparison metrics or lacks impression/retention measurements.",
            "action": "Upload a Content/Reach report with Views, Impressions, CTR, Average view duration and Average percentage viewed where available. Do not treat missing metrics as zero.",
        })
    return recommendations


def _daily_recommendations(records):
    by_day = defaultdict(lambda: {"views": 0.0, "watch_time_minutes": 0.0})
    has_views = False
    has_watch = False
    for row in records:
        raw_date = row.get("date")
        if not raw_date:
            continue
        day = raw_date.strip()
        if not re.search(r"\d{4}", day):
            continue
        for field in ("views", "watch_time_minutes"):
            value = row.get(field)
            if isinstance(value, (int, float)):
                by_day[day][field] += value
                if field == "views":
                    has_views = True
                if field == "watch_time_minutes":
                    has_watch = True
    days = sorted(by_day)
    recommendations = []
    if len(days) >= 6 and has_views:
        split = max(1, len(days) // 2)
        earlier = days[:split]
        later = days[split:]
        left = sum(by_day[day]["views"] for day in earlier)
        right = sum(by_day[day]["views"] for day in later)
        if left > 0 and later:
            delta = (right - left) / left * 100
            direction = "increased" if delta > 0 else "decreased"
            recommendations.append({
                "priority": "Context",
                "title": f"Views {direction} {abs(delta):.1f}% between the two halves of this export",
                "evidence": f"Views sum to {left:,.0f} in the earlier half and {right:,.0f} in the later half across {len(days)} reported dates.",
                "action": "Compare uploads and traffic sources across those periods before attributing the change to any title, thumbnail, or publishing decision.",
            })
    if has_watch and len(days) >= 2 and not recommendations:
        recommendations.append({
            "priority": "Context",
            "title": "Daily watch-time data is available",
            "evidence": f"The export contains watch time across {len(days)} dates.",
            "action": "Compare watch time alongside views; more views do not necessarily mean more total watch time.",
        })
    return recommendations


def analyze_studio_export(parsed: dict) -> dict:
    """Return a compact report and grounded actions from one parsed export."""
    records = parsed["records"]
    videos = _video_summaries(records)
    fields = set(parsed["fields"])
    recommendations = []
    if parsed["report_type"] == "retention":
        recommendations = _retention_recommendations(records)
    elif parsed["report_type"] == "video_performance" or videos:
        recommendations = _performance_recommendations(videos)
    elif "date" in fields:
        recommendations = _daily_recommendations(records)

    numeric_summary = {}
    for display_name, key in (
        ("views", "views"),
        ("watch_time_minutes", "watch_time_minutes"),
        ("impressions", "impressions"),
        ("likes", "likes"),
        ("comments", "comments"),
        ("shares", "shares"),
        ("subscribers_gained", "subscribers_gained"),
        ("subscribers_lost", "subscribers_lost"),
    ):
        values = [row.get(key) for row in records if isinstance(row.get(key), (int, float))]
        if values:
            numeric_summary[display_name] = sum(values)

    return {
        "filename": parsed["filename"],
        "report_type": parsed["report_type"],
        "header_row": parsed["header_row"],
        "headers_detected": parsed["header_labels"],
        "ignored_headers": parsed["ignored_headers"],
        "fields": parsed["fields"],
        "record_count": len(records),
        "video_count": len(videos),
        "summary": numeric_summary,
        "video_rows": videos,
        "retention_rows": [
            row for row in records
            if isinstance(row.get("elapsed_seconds"), (int, float))
            or isinstance(row.get("elapsed_percent"), (int, float))
        ],
        "recommendations": recommendations,
        "rows": records,
    }


def build_evidence_index(reports: list[dict]) -> dict:
    """Build a small session-only performance evidence index, keyed by ID and normalized title."""
    index = {}
    for report in reports:
        filename = report.get("filename")
        for row in report.get("video_rows", []):
            evidence = {
                "source_file": filename,
                "report_type": report.get("report_type"),
                "views": row.get("views"),
                "watch_time_minutes": row.get("watch_time_minutes"),
                "average_view_duration_seconds": row.get("average_view_duration_seconds"),
                "average_view_percentage": row.get("average_view_percentage"),
                "impressions": row.get("impressions"),
                "ctr_percent": row.get("ctr_percent"),
                "subscribers_gained": row.get("subscribers_gained"),
                "subscribers_lost": row.get("subscribers_lost"),
                "date_start": row.get("date_start"),
                "date_end": row.get("date_end"),
            }
            if row.get("video_id"):
                index[f"id:{row['video_id']}"] = evidence
            if row.get("video_title"):
                index[f"title:{_norm(row['video_title'])}"] = evidence
    return index
