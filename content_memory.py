"""Private, owner-scoped memory for weekly channel reviews and metadata experiments.

The app stores snapshots in a spreadsheet created by this OAuth client. The
Drive scope is limited to files created/opened by this app; it does not grant
access to the owner's entire Drive.
"""

import json
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SPREADSHEET_TITLE = "AI Simplified Lab Strategist Memory"
SCAN_SHEET = "Weekly Scans"
EXPERIMENT_SHEET = "Approved Experiments"

SCAN_HEADERS = (
    "saved_at_utc",
    "period_start",
    "period_end",
    "uploads_in_window",
    "measured_views_count",
    "usable_ctr_count",
    "candidate_count",
    "candidates_json",
    "action_brief_json",
)
EXPERIMENT_HEADERS = (
    "applied_at_utc",
    "video_id",
    "video_url",
    "before_title",
    "after_title",
    "before_description",
    "after_description",
    "before_tags_json",
    "after_tags_json",
    "baseline_metrics_json",
    "proposal_digest",
    "result",
)

CANDIDATE_FIELDS = (
    "video_id",
    "title",
    "url",
    "published_at",
    "format",
    "views",
    "impressions",
    "ctr",
    "age_days",
    "selection_basis",
    "opportunity_type",
    "opportunity_evidence",
    "peer_count",
    "peer_ctr_count",
    "peer_ctr_median",
    "peer_views_median",
    "relative_ctr_gap",
    "relative_views_gap",
    "averageViewDuration",
)


class MemoryStoreError(RuntimeError):
    """A safe, user-facing failure to load or save private content memory."""


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _candidate_snapshot(candidate):
    return {key: candidate.get(key) for key in CANDIDATE_FIELDS}


def build_weekly_action_brief(candidates, *, limit=3):
    """Turn measured opportunity signals into restrained, actionable next steps."""
    brief = []
    for candidate in list(candidates or [])[:limit]:
        basis = candidate.get("selection_basis")
        evidence = candidate.get("opportunity_evidence") or (
            "No comparative evidence was returned for this upload."
        )
        if basis == "low_ctr_vs_peers":
            action = (
                "Review the title and thumbnail as one promise. Make the subject and "
                "specific payoff clearer, then compare the packaging with the opening "
                "of the video. Treat this as a hypothesis, not a CTR guarantee."
            )
            confidence = "Measured CTR signal"
            success_metric = "Recheck reported CTR after enough new impressions accrue."
        elif basis == "low_views_vs_peers":
            action = (
                "Check topic demand and distribution first, then review whether the "
                "title accurately communicates the video's payoff. Do not assume "
                "metadata caused the view gap."
            )
            confidence = "Matched-peer views signal"
            success_metric = (
                "Compare views and watch-time measures over a comparable period."
            )
        else:
            action = (
                "Use this as a manual review only. Check the title, thumbnail, opening "
                "and traffic sources before deciding what to change."
            )
            confidence = "Limited evidence"
            success_metric = (
                "Collect more comparable data before judging whether the change helped."
            )
        brief.append({
            "video_id": candidate.get("video_id"),
            "title": candidate.get("title") or "(untitled video)",
            "url": candidate.get("url"),
            "signal": candidate.get("opportunity_type") or "Review candidate",
            "confidence": confidence,
            "evidence": evidence,
            "first_action": action,
            "success_metric": success_metric,
        })
    return brief


def build_scan_snapshot(
    *,
    start_date,
    end_date,
    upload_count,
    measured_views_count,
    usable_ctr_count,
    candidates,
):
    """Build a small, JSON-safe snapshot without saving full Analytics payloads."""
    brief = build_weekly_action_brief(candidates)
    compact_candidates = [_candidate_snapshot(item) for item in candidates or []]
    return {
        "saved_at_utc": _utc_now(),
        "period_start": str(start_date),
        "period_end": str(end_date),
        "uploads_in_window": int(upload_count or 0),
        "measured_views_count": int(measured_views_count or 0),
        "usable_ctr_count": int(usable_ctr_count or 0),
        "candidate_count": len(compact_candidates),
        "candidates": compact_candidates,
        "action_brief": brief,
    }


def metrics_snapshot_from_session(session_state, video_id):
    """Return measured values only when a cached row exists; never substitute zero."""
    for key in ("opportunity_data", "dashboard_data"):
        data = session_state.get(key)
        if not isinstance(data, dict):
            continue
        for row in data.get("video_rows", []):
            if row.get("video_id") == video_id:
                return {
                    "period": data.get("period"),
                    "views": row.get("views"),
                    "averageViewDuration": row.get("averageViewDuration"),
                    "estimatedMinutesWatched": row.get("estimatedMinutesWatched"),
                    "impressions": row.get("impressions"),
                    "ctr": row.get("ctr"),
                    "analytics_status": row.get("analytics_status"),
                    "impressions_status": row.get("impressions_status"),
                    "ctr_status": row.get("ctr_status"),
                }
    return {
        "availability": (
            "No matching cached measurement was available at approval time; "
            "metrics were not inferred."
        )
    }


class GoogleSheetsMemoryStore:
    """Append-only private spreadsheet store backed by the owner's Google OAuth."""

    def __init__(self, drive_service, sheets_service):
        self.drive = drive_service
        self.sheets = sheets_service
        self.spreadsheet_id = self._find_or_create_spreadsheet()
        self.spreadsheet_url = (
            "https://docs.google.com/spreadsheets/d/"
            + self.spreadsheet_id
            + "/edit"
        )
        self._ensure_sheets_and_headers()

    @classmethod
    def from_credentials(cls, credentials):
        try:
            drive = build(
                "drive", "v3", credentials=credentials, cache_discovery=False
            )
            sheets = build(
                "sheets", "v4", credentials=credentials, cache_discovery=False
            )
            return cls(drive, sheets)
        except MemoryStoreError:
            raise
        except HttpError as error:
            raise MemoryStoreError(_http_error_message(error)) from None
        except Exception:
            raise MemoryStoreError(
                "Google could not initialize private Strategist Memory. "
                "Check that the Google Drive API and Google Sheets API are enabled, "
                "then reconnect Google from the app."
            ) from None

    def _find_or_create_spreadsheet(self):
        query = (
            "name = 'AI Simplified Lab Strategist Memory' and "
            "mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
        )
        try:
            response = self.drive.files().list(
                q=query,
                spaces="drive",
                pageSize=100,
                orderBy="createdTime",
                fields="files(id,name,mimeType,createdTime)",
            ).execute()
            files = response.get("files", [])
            if files:
                return files[0]["id"]
            created = self.sheets.spreadsheets().create(
                body={
                    "properties": {"title": SPREADSHEET_TITLE},
                    "sheets": [
                        {"properties": {"title": SCAN_SHEET}},
                        {"properties": {"title": EXPERIMENT_SHEET}},
                    ],
                },
                fields="spreadsheetId",
            ).execute()
            spreadsheet_id = created.get("spreadsheetId")
            if not spreadsheet_id:
                raise MemoryStoreError(
                    "Google created no identifiable Strategist Memory spreadsheet."
                )
            return spreadsheet_id
        except MemoryStoreError:
            raise
        except HttpError as error:
            raise MemoryStoreError(_http_error_message(error)) from None

    def _ensure_sheets_and_headers(self):
        expected = {
            SCAN_SHEET: SCAN_HEADERS,
            EXPERIMENT_SHEET: EXPERIMENT_HEADERS,
        }
        try:
            metadata = self.sheets.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                fields="sheets.properties",
            ).execute()
            existing = {
                item.get("properties", {}).get("title")
                for item in metadata.get("sheets", [])
            }
            missing = [name for name in expected if name not in existing]
            if missing:
                self.sheets.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={
                        "requests": [
                            {"addSheet": {"properties": {"title": name}}}
                            for name in missing
                        ]
                    },
                ).execute()
            for name, headers in expected.items():
                cell_range = _sheet_range(name, "A1:Z1")
                existing_header = self.sheets.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id,
                    range=cell_range,
                ).execute().get("values", [])
                if not existing_header or not any(str(v).strip() for v in existing_header[0]):
                    self.sheets.spreadsheets().values().update(
                        spreadsheetId=self.spreadsheet_id,
                        range=_sheet_range(name, "A1"),
                        valueInputOption="RAW",
                        body={"values": [list(headers)]},
                    ).execute()
        except HttpError as error:
            raise MemoryStoreError(_http_error_message(error)) from None

    def append_scan(self, snapshot):
        if not isinstance(snapshot, dict):
            raise ValueError("Scan snapshot must be a dictionary.")
        row = [
            snapshot.get("saved_at_utc", _utc_now()),
            str(snapshot.get("period_start", "")),
            str(snapshot.get("period_end", "")),
            int(snapshot.get("uploads_in_window", 0)),
            int(snapshot.get("measured_views_count", 0)),
            int(snapshot.get("usable_ctr_count", 0)),
            int(snapshot.get("candidate_count", 0)),
            _json(snapshot.get("candidates", [])),
            _json(snapshot.get("action_brief", [])),
        ]
        self._append(SCAN_SHEET, row)

    def append_experiment(
        self,
        *,
        video_id,
        video_url,
        before,
        proposed,
        proposal_digest,
        baseline_metrics,
    ):
        """Persist an approved and verified change, not a draft or rejected proposal."""
        row = [
            _utc_now(),
            str(video_id),
            str(video_url or ""),
            str(before.get("title", "")),
            str(proposed.get("title", "")),
            str(before.get("description", "")),
            str(proposed.get("description", "")),
            _json(before.get("tags", [])),
            _json(proposed.get("tags", [])),
            _json(baseline_metrics or {}),
            str(proposal_digest),
            "verified",
        ]
        self._append(EXPERIMENT_SHEET, row)

    def _append(self, sheet_name, row):
        try:
            self.sheets.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=_sheet_range(sheet_name, "A1"),
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
        except HttpError as error:
            raise MemoryStoreError(_http_error_message(error)) from None

    def list_recent_scans(self, *, limit=8):
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("History limit must be positive.")
        try:
            response = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=_sheet_range(SCAN_SHEET, "A1:I500"),
            ).execute()
            rows = response.get("values", [])
        except HttpError as error:
            raise MemoryStoreError(_http_error_message(error)) from None
        history = []
        for row in reversed(rows[1:]):
            if not row:
                continue
            row = list(row) + [""] * max(0, len(SCAN_HEADERS) - len(row))
            try:
                candidates = json.loads(row[7]) if row[7] else []
                brief = json.loads(row[8]) if row[8] else []
            except (json.JSONDecodeError, TypeError):
                candidates, brief = [], []
            history.append({
                "saved_at_utc": row[0],
                "period_start": row[1],
                "period_end": row[2],
                "uploads_in_window": _as_int(row[3]),
                "measured_views_count": _as_int(row[4]),
                "usable_ctr_count": _as_int(row[5]),
                "candidate_count": _as_int(row[6]),
                "candidates": candidates,
                "action_brief": brief,
            })
            if len(history) >= limit:
                break
        return history


def _sheet_range(sheet_name, cell_range):
    escaped_name = sheet_name.replace("'", "''")
    return "'" + escaped_name + "'!" + cell_range


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _http_error_message(error):
    status = getattr(getattr(error, "resp", None), "status", None)
    if status == 403:
        return (
            "Google denied access to private Strategist Memory. Enable the Google "
            "Drive API and Google Sheets API in the OAuth client's Google Cloud project, "
            "then reconnect Google from the app so it can grant the new limited scopes."
        )
    if status == 404:
        return (
            "The private Strategist Memory spreadsheet could not be found. "
            "Reconnect Google to create or rediscover it."
        )
    if isinstance(status, int):
        return (
            f"Google returned HTTP {status} while accessing Strategist Memory. "
            "Check API enablement and reconnect Google if permissions changed."
        )
    return (
        "Google could not read or write private Strategist Memory. "
        "Check Drive/Sheets API access, then reconnect Google."
    )


def describe_memory_error(error):
    """Produce a safe, actionable message without showing response bodies or tokens."""
    if isinstance(error, MemoryStoreError):
        return str(error)
    if isinstance(error, HttpError):
        return _http_error_message(error)
    return (
        "The latest review is available in this session, but durable memory could not "
        "be saved. Check the Google Drive API and Google Sheets API, then reconnect."
    )
