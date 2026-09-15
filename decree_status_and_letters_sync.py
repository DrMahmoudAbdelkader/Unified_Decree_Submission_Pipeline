#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decree_status_and_letters_sync.py
==========================================================================
Daily status sweep — REBUILT to use the confirmed-working status
mechanism from request_status_sync.py (a sibling repo's script, supplied
2026-09-15), instead of the SearchRecommendationRequest + _PrintLetters
popup-scraping this file used before.

WHY THE REBUILD
--------------------------------------------------------------------------
The previous version's status source was find_recommendation_id_for_request()
— it asked "does a recommendation/letter exist yet for this request?" and,
if not, gave up (that was the explicit "_check_pre_recommendation_status
is a stub" gap documented in every earlier version of this file). That
meant every request still at تم التسجيل / لجنة طبية / تحويل الي طبيب اخر
— i.e. most open requests, most of the time — was invisible to this
script no matter how many nights it ran.

request_status_sync.py hits a different, better endpoint:
POST /smc/Reports/SendRequestStatusJson, which is the JSON backing call
for the site's own "SendRequestStatus" report page. It returns EVERY
request's current status directly (STATUSARABICNAME), regardless of
whether a recommendation exists — because it's driven by the report
page, not the recommendation/letters workflow. That closes the gap
entirely; there is no more pre-recommendation stub in this file.

WHAT DID NOT CHANGE
--------------------------------------------------------------------------
This script still writes to THIS app's own schema —
decree_request_attempts.smc_status_raw / smc_status_normalized /
status_is_final / last_status_checked_at, and
decree_request_cases.case_status via BUCKET_TO_CASE_STATUS — the exact
tables this app's "decree status tracker" module and request-entry module
read. request_status_sync.py writes to a DIFFERENT pair of tables
(decree_request_status_daily_export / decree_admin_letter_details) that
belong to a separate reporting pipeline in the other repo; those are not
touched here and are not what feeds this app.

THE TWO-TIER LOOKUP (ported from request_status_sync.py's own design)
--------------------------------------------------------------------------
1. BULK WINDOW (cheap): one POST to SendRequestStatusJson for a rolling
   LOOKBACK_DAYS-day window (default 15) returns the current status of
   every request submitted in that window in ONE call — covers the large
   majority of open attempts without a single per-attempt round trip.
2. STALE FALLBACK (targeted): any open attempt whose website_request_id
   did NOT come back in the bulk window (submitted longer ago than
   LOOKBACK_DAYS but still open) gets ONE targeted SendRequestStatusJson
   call, filtered by RequestNumber alone with a wide start date — same
   technique request_status_sync.refresh_stale_open_requests() uses.
   Capped per run at MAX_SINGLE_STATUS_LOOKUPS (default 300) to bound
   run time; anything past the cap is picked up on a later run, and
   load_open_attempts() orders by staleness (oldest-checked first) so the
   cap never starves the same tail of the queue night after night.

LETTER TEXT (نص الخطاب) — narrowed, not removed
--------------------------------------------------------------------------
The old SearchRecommendationRequest + _PrintLetters popup call is KEPT,
but only as a second step, fired only for attempts whose status this run
resolved to the Admin_Letter bucket and which don't already have
response_text saved. Previously this round trip ran for every single open
attempt regardless of status; now it only runs for the ones that actually
need a letter body, which is both faster and lighter on the SMC site.

CONFIG NEEDED FROM YOU
--------------------------------------------------------------------------
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
    SMC_USERNAME_2 / SMC_PASSWORD_2   — dedicated secondary account,
        falls back to SMC_USERNAME / SMC_PASSWORD if unset.
    SMC_SENDING_SITE                  — only used for the admin-letter
        text lookup (SearchRecommendationRequest still needs it);
        SendRequestStatusJson itself does not take a sending-site param.
    LOOKBACK_DAYS                     — default 15.
    MAX_SINGLE_STATUS_LOOKUPS         — default 300.

BEFORE TRUSTING THIS FOR REAL: smc_status_map now needs a row for every
raw status SendRequestStatusJson can return — including the early ones
(تم التسجيل, لجنة طبية, تحويل الي طبيب اخر, توصية مبدئية, ...) that the
old popup-based version never saw at all. Any status text that comes back
without a matching row falls into the "Unknown" bucket (never guessed as
final) and is logged — check the run summary's "Unmapped statuses" line
after the first real run and add rows for whatever shows up there.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))

import Unified_Decree_Submission_Pipeline as _pipeline_module
from Unified_Decree_Submission_Pipeline import SMCSession
import supabase_client as sb

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("decree_status_and_letters_sync")

BASE_URL = _pipeline_module.BASE_URL
REQUEST_DELAY = 0.3
MAX_POPUP_ATTEMPTS = 3

# Only used by the admin-letter text lookup (SearchRecommendationRequest) —
# SendRequestStatusJson itself takes no sending-site parameter.
SENDING_SITE = os.environ.get("SMC_SENDING_SITE", "102233")

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "15"))
MAX_SINGLE_STATUS_LOOKUPS = int(os.environ.get("MAX_SINGLE_STATUS_LOOKUPS", "300"))

ATTEMPTS_TABLE = "decree_request_attempts"
CASES_TABLE = "decree_request_cases"
EVENTS_TABLE = "decree_request_events"
STATUS_MAP_TABLE = "smc_status_map"

# decree_request_events.created_by is NOT NULL with no default. Every OTHER
# writer in this pipeline goes through decree_common.log_event(), which
# already sets this fixed "system" UUID; this script writes to
# decree_request_events directly and had simply never set it — that
# omission crashed every run of the previous version on its very first
# insert. Reusing the identical UUID keeps every automated row
# attributable to the same account regardless of which script wrote it.
SYSTEM_CREATED_BY = "9b8fa9a6-0567-4a93-8e21-d0f8bc098394"

# english_bucket (from smc_status_map) -> decree_request_cases.case_status.
# Unmapped/non-final buckets are deliberately absent — case_status is left
# as whatever it already is rather than guessed at.
#
# Admin_Letter (خطاب ادارى) -> FINAL_DECLINED, not "ADMIN_LETTER": this is
# a terminal SMC response (a refusal), same as an approval or a
# cancellation, so it belongs on a terminal case_status. Stamping it
# "ADMIN_LETTER" left it reading as an in-progress status downstream. The
# raw response is not lost by this — smc_status_raw/smc_status_normalized
# on the attempt row still record "خطاب ادارى"/"Admin_Letter" exactly as
# before; only the denormalized case_status changes.
BUCKET_TO_CASE_STATUS = {
    "Approved": "FINAL_APPROVED",
    "Admin_Letter": "FINAL_DECLINED",
    "Cancelled": "CANCELLED",
    "Cancelled_For_Edit": "CANCELLED",
    "Cancelled_By_Request": "CANCELLED",
}


# =====================================================================
# Cairo local time — no extra dependency: zoneinfo is stdlib (3.9+) and
# Linux runners carry the system tz database, so Africa/Cairo (including
# Egypt's 2023-reinstated DST) resolves correctly with no pip install.
# Falls back to a fixed UTC+2 offset (logging once) only if the platform
# genuinely has no tz database at all — that fallback will be off by an
# hour during Egypt's Apr-Oct DST window, which only affects which
# calendar day a request near local midnight is filed under, never
# whether it's found at all.
# =====================================================================
try:
    from zoneinfo import ZoneInfo
    CAIRO_TZ = ZoneInfo("Africa/Cairo")
except Exception:
    from datetime import timezone
    CAIRO_TZ = timezone(timedelta(hours=2))
    log.warning("Africa/Cairo tz data not available on this runner — falling back to a fixed "
                "UTC+2 offset (will be off by 1 hour during Egypt's Apr-Oct DST window).")


def cairo_today_iso() -> str:
    return datetime.now(CAIRO_TZ).strftime("%Y-%m-%d")


# =====================================================================
# STEP 1 — SendRequestStatusJson (ported from request_status_sync.py,
# confirmed working against the real site). Pure parsing helpers first.
# =====================================================================

def _smc_datetime_str(date_iso: str, end_of_day: bool = False) -> str:
    """'2026-08-08' -> '8/8/2026 12:00:00 AM' — the exact format the site's
    own JS sends (no zero-padding). end_of_day=True gives '... 11:59:59 PM'
    so a same-day request isn't excluded by an exact-midnight boundary."""
    d = datetime.strptime(date_iso, "%Y-%m-%d")
    if end_of_day:
        return f"{d.month}/{d.day}/{d.year} 11:59:59 PM"
    return f"{d.month}/{d.day}/{d.year} 12:00:00 AM"


_DOTNET_DATE_RE = re.compile(r"/Date\((-?\d+)\)/")


def _parse_dotnet_date(value, fallback_iso: Optional[str] = None) -> Optional[str]:
    """ASP.NET serializes DateTime as '/Date(1699999999000)/' (epoch ms).
    Converts to 'YYYY-MM-DD' in Cairo local time. Never raises."""
    if not value:
        return fallback_iso
    m = _DOTNET_DATE_RE.search(str(value))
    if not m:
        return fallback_iso
    try:
        epoch_ms = int(m.group(1))
        dt = datetime.fromtimestamp(epoch_ms / 1000, tz=CAIRO_TZ)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return fallback_iso


def _clean_id(value) -> str:
    """SendRequestStatusJson's serializer emits numeric IDs as JSON floats
    (e.g. 67227949.0). Strips a trailing '.0' off any whole-number
    float/string before it's used anywhere — confirmed via the sibling
    script that the site's own pages 404 on the '.0'-suffixed form."""
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        return text[:-2]
    return text


def _parse_send_request_status_json(raw):
    """Unwraps the response regardless of whether the server sent a real
    JSON array or a JSON-encoded string containing one — the report page's
    own JS unconditionally does a second JSON.parse, so this mirrors that."""
    data = raw
    for _ in range(2):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                return []
        else:
            break
    return data if isinstance(data, list) else []


def _row_from_record(rec: dict, fallback_date_iso: Optional[str] = None) -> Optional[dict]:
    request_number = _clean_id(rec.get("REQUESTID"))
    if not request_number:
        return None
    return {
        "request_number": request_number,
        "patient_name": (rec.get("CITIZENFULLNAMEARABIC") or "").strip() or None,
        "patient_id": _clean_id(rec.get("CITIZENSSN")) or None,
        "request_status": (rec.get("STATUSARABICNAME") or "").strip() or None,
        "request_date": _parse_dotnet_date(rec.get("REQUESTDATE"), fallback_iso=fallback_date_iso),
    }


def fetch_status_window(session, start_date_iso: str, end_date_iso: str) -> Dict[str, dict]:
    """ONE call covering every request submitted in [start, end]. Returns a
    dict keyed by request_number so process_one_attempt() can look an
    attempt's status up with no further network call for anything inside
    the window."""
    url = f"{BASE_URL}/smc/Reports/SendRequestStatusJson"
    payload = {
        "CitizenName": "",
        "StartDate": _smc_datetime_str(start_date_iso),
        "EndDate": _smc_datetime_str(end_date_iso, end_of_day=True),
        "SsnNumber": "",
        "RequestNumber": "",
        "RequestStatusId": "",
        "SystemUserId": "",
    }
    log.info(f"Fetching SendRequestStatusJson window {start_date_iso}..{end_date_iso} …")
    try:
        resp = session.post(url, data=payload, timeout=30)
    except Exception as e:
        log.error(f"SendRequestStatusJson (bulk window) failed: {e}")
        return {}
    if resp.status_code != 200:
        log.error(f"SendRequestStatusJson (bulk window) returned HTTP {resp.status_code}")
        return {}

    try:
        raw = resp.json()
    except ValueError:
        raw = resp.text

    out: Dict[str, dict] = {}
    for rec in _parse_send_request_status_json(raw):
        row = _row_from_record(rec, fallback_date_iso=end_date_iso)
        if row:
            out[row["request_number"]] = row
    log.info(f"  {len(out)} request(s) returned in the window.")
    return out


def fetch_single_status(session, request_number: str, today_iso: str) -> Optional[dict]:
    """Targeted re-check for ONE request_number, bypassing the rolling
    window entirely (StartDate pinned far in the past) — for attempts
    submitted longer ago than LOOKBACK_DAYS that are still open. Returns
    None if the site no longer returns anything for this number (never
    raises)."""
    url = f"{BASE_URL}/smc/Reports/SendRequestStatusJson"
    payload = {
        "CitizenName": "",
        "StartDate": _smc_datetime_str("2015-01-01"),
        "EndDate": _smc_datetime_str(today_iso, end_of_day=True),
        "SsnNumber": "",
        "RequestNumber": request_number,
        "RequestStatusId": "",
        "SystemUserId": "",
    }
    try:
        resp = session.post(url, data=payload, timeout=30)
    except Exception as e:
        log.error(f"  [stale] {request_number}: request failed ({e})")
        return None
    if resp.status_code != 200:
        log.warning(f"  [stale] {request_number}: HTTP {resp.status_code}")
        return None
    try:
        raw = resp.json()
    except ValueError:
        raw = resp.text
    for rec in _parse_send_request_status_json(raw):
        if _clean_id(rec.get("REQUESTID")) == request_number:
            return _row_from_record(rec)
    return None


# =====================================================================
# Letter text (نص الخطاب) — narrowed to Admin_Letter-bucket attempts only.
# Endpoints/parsing kept from the previous version of this file (already
# confirmed working for response_text extraction).
# =====================================================================

def pipe_field(pipe_text: str, label: str) -> str:
    parts = pipe_text.split("|")
    for i, p in enumerate(parts):
        if label in p and i + 1 < len(parts):
            return parts[i + 1].strip()
    return ""


def popup_looks_valid(pipe_text: str) -> bool:
    return "رقم الطلب" in pipe_text or "الرقم القومي" in pipe_text


def _extract_labeled_field(html: str, label: str) -> Optional[str]:
    pattern = rf"{re.escape(label)}\s*</b>\s*<br\s*/?>\s*(.*?)(?:<b>|</td>|</tr>|$)"
    m = re.search(pattern, html, re.DOTALL)
    if m:
        candidate = BeautifulSoup(m.group(1), "html.parser").get_text(" ", strip=True)
        if candidate:
            return candidate
    return None


def extract_response_text(html: str) -> Optional[str]:
    """Pulls نص الخطاب from the _PrintLetters popup HTML. The status label
    this function used to also try to extract is gone — status now comes
    from SendRequestStatusJson, which is both more complete and doesn't
    depend on guessing which Arabic label a popup uses."""
    text = _extract_labeled_field(html, "نص الخطاب")
    if text is not None:
        return text
    pipe = BeautifulSoup(html, "html.parser").get_text(separator="|", strip=True)
    return pipe_field(pipe, "نص الخطاب") or None


def find_recommendation_id_for_request(session, request_number: str) -> Optional[str]:
    """POST SearchRecommendationRequest with requestID=<request_number> —
    only called now for attempts already known (via SendRequestStatusJson)
    to be at an Admin_Letter status, purely to locate the recommendation id
    needed to fetch the letter's body text."""
    url = f"{BASE_URL}/smc/Decrees/SearchRecommendationRequest"
    today = datetime.now()
    data = {
        "requestID": request_number,
        "SSN": "",
        "SendingSite": SENDING_SITE,
        "RequestCreator": "",
        "DoctorRecommendationCreator": "",
        "AdminActionContentType": "0",
        "dateFrom": (today - timedelta(days=730)).strftime("%Y-%m-%d"),
        "dateTo": today.strftime("%Y-%m-%d"),
        "OrderingBy": "0",
        "Print": "All",
        "actionUrl": "norecommendation",
        "isHospitalNotExternalSite": "true",
        "page": "1",
    }
    try:
        resp = session.post(url, data=data, timeout=30)
    except Exception as e:
        log.error(f"  SearchRecommendationRequest failed for {request_number}: {e}")
        return None
    if resp.status_code != 200:
        log.warning(f"  SearchRecommendationRequest HTTP {resp.status_code} for {request_number}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", id="RecommendationTable")
    if not table:
        return None
    ar2en_map = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    for tbody in table.find_all("tbody"):
        row = tbody.find("tr")
        if not row:
            continue
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        span = cells[1].find("span", {"id": "RecommendationId"})
        cell = span if span else cells[1]
        rec_id = cell.get_text(strip=True).translate(ar2en_map).strip()
        if rec_id:
            return rec_id
    return None


def get_letter_popup_html(session, rec_id: str) -> Optional[str]:
    url = f"{BASE_URL}/smc/Requests/_PrintLetters"
    for attempt in range(1, MAX_POPUP_ATTEMPTS + 1):
        try:
            r = session.get(url, params={"RecommIDs": f"{rec_id},"}, timeout=30)
        except Exception as e:
            log.warning(f"    _PrintLetters attempt {attempt} error: {e}")
            time.sleep(REQUEST_DELAY * attempt)
            continue
        if r.status_code == 200:
            pipe = BeautifulSoup(r.text, "html.parser").get_text(separator="|", strip=True)
            if popup_looks_valid(pipe):
                return r.text
        time.sleep(REQUEST_DELAY * attempt)
    return None


def fetch_admin_letter_text(session, request_number: str) -> Optional[str]:
    """The two-call letter-text fetch, only ever invoked for an attempt
    already confirmed (via SendRequestStatusJson) to be at Admin_Letter."""
    rec_id = find_recommendation_id_for_request(session, request_number)
    time.sleep(REQUEST_DELAY)
    if not rec_id:
        log.warning(f"  {request_number}: status is Admin_Letter but no recommendation id found — "
                    f"letter text not fetched this run.")
        return None
    html = get_letter_popup_html(session, rec_id)
    time.sleep(REQUEST_DELAY)
    if not html:
        return None
    return extract_response_text(html)


# =====================================================================
# Status normalization — unchanged mechanism, now fed a far more complete
# and reliable set of raw status strings.
# =====================================================================

def resolve_status_bucket(status_map: Dict[str, dict], arabic_status: str) -> dict:
    return status_map.get(
        arabic_status,
        {"arabic_status": arabic_status, "english_bucket": "Unknown", "is_final": False, "requires_action": False},
    )


def load_status_map() -> Dict[str, dict]:
    rows = sb.select(STATUS_MAP_TABLE, select="arabic_status,english_bucket,is_final,requires_action")
    return {r["arabic_status"]: r for r in rows}


def load_open_attempts() -> List[dict]:
    """Every attempt with a request number that hasn't reached a final
    status yet, oldest-checked first.

    Two fixes carried over from the previous version, both still load-
    bearing:
      - `or=(status_is_final.is.false,status_is_final.is.null)` — PostgREST's
        `is.false` matches ONLY literal false; a NULL (every attempt from
        before status_is_final existed) is neither true nor false and was
        silently excluded by the old `is.false`-only filter.
      - Paging (500/page) — PostgREST caps unpaged responses at db-max-rows
        with no error, so past ~1000 open attempts the old unpaged query
        would silently only ever see the first page forever.

    NEW: ordered by last_status_checked_at ascending, nulls first, rather
    than by id. This matters now that a per-run cap
    (MAX_SINGLE_STATUS_LOOKUPS) can mean not every stale attempt gets its
    single-lookup budget spent on it every night — ordering by staleness
    means the budget always goes to whichever attempts have gone longest
    without a check, instead of the same id-ordered prefix winning (and
    the same tail starving) every single run."""
    page_size = 500
    offset = 0
    rows: List[dict] = []
    while True:
        page = sb.select(
            ATTEMPTS_TABLE,
            select="id,case_id,website_request_id,attempt_status,response_text,status_is_final,last_status_checked_at",
            filters={
                "website_request_id": "not.is.null",
                "or": "(status_is_final.is.false,status_is_final.is.null)",
                "offset": str(offset),
            },
            order="last_status_checked_at.asc.nullsfirst,id.asc",
            limit=page_size,
        )
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += page_size


_LAST_CHECKED_COLUMN_AVAILABLE = True


def _mark_checked(attempt_id, extra_fields: Optional[Dict[str, object]] = None):
    """Writes update_fields PLUS a last_status_checked_at stamp, so "SMC was
    asked and genuinely had nothing new" is distinguishable from "never
    asked" — and now also drives load_open_attempts()'s staleness order.
    Degrades gracefully (logs once, keeps going without the stamp) if the
    column doesn't exist yet in this database."""
    global _LAST_CHECKED_COLUMN_AVAILABLE
    fields = dict(extra_fields or {})
    if _LAST_CHECKED_COLUMN_AVAILABLE:
        attempt_fields = dict(fields)
        attempt_fields["last_status_checked_at"] = datetime.now().astimezone().isoformat()
        try:
            return sb.update(ATTEMPTS_TABLE, attempt_id, attempt_fields)
        except RuntimeError as e:
            if "last_status_checked_at" not in str(e):
                raise
            _LAST_CHECKED_COLUMN_AVAILABLE = False
            log.warning(
                "decree_request_attempts.last_status_checked_at does not exist — continuing without "
                "the last-checked stamp (and without staleness-based ordering). Run the status-sync "
                "migration to enable both."
            )
    if not fields:
        return None
    return sb.update(ATTEMPTS_TABLE, attempt_id, fields)


def _get_smc_credentials():
    username = os.environ.get("SMC_USERNAME_2") or os.environ.get("SMC_USERNAME")
    password = os.environ.get("SMC_PASSWORD_2") or os.environ.get("SMC_PASSWORD")
    return username, password


# =====================================================================
# Per-attempt processing
# =====================================================================

def process_one_attempt(session, status_map: Dict[str, dict], attempt: dict,
                         bulk_window: Dict[str, dict], lookup_budget: Dict[str, int],
                         today_iso: str) -> Dict[str, object]:
    """Everything that happens for ONE open attempt. Isolated in its own
    function (and wrapped in try/except by the caller) so an unexpected
    error on one attempt can never again take down the whole run — that is
    exactly what happened on 2026-09-15 when a missing created_by field
    propagated an unhandled exception out of main() and silently dropped
    every attempt after the failing one for the night.

    Never raises for a predictable failure — every expected path returns
    cleanly; the caller's except should only ever catch a genuinely
    unexpected error."""
    counters = {"checked": 1, "updated": 0, "letters_fetched": 0,
                "reached_final": 0, "used_bulk": 0, "used_single_lookup": 0,
                "budget_exhausted": 0, "not_found": 0, "unmapped_status": None}
    request_number = attempt["website_request_id"]

    row = bulk_window.get(request_number)
    if row is not None:
        counters["used_bulk"] = 1
    elif lookup_budget["remaining"] > 0:
        lookup_budget["remaining"] -= 1
        row = fetch_single_status(session, request_number, today_iso)
        time.sleep(REQUEST_DELAY)
        counters["used_single_lookup"] = 1
    else:
        # Budget exhausted this run — leave the attempt untouched (no
        # _mark_checked call) so it's neither stamped as freshly checked
        # nor loses its place at the front of tomorrow's staleness order.
        counters["budget_exhausted"] = 1
        return counters

    if row is None or not row.get("request_status"):
        # SendRequestStatusJson returned nothing for this request number at
        # all (removed request, or a transient site issue) — stamp it as
        # checked (this WAS a real attempt, not a skip) but don't touch the
        # stored status.
        counters["not_found"] = 1
        _mark_checked(attempt["id"])
        return counters

    status_raw = row["request_status"]
    bucket_info = resolve_status_bucket(status_map, status_raw)

    update_fields: Dict[str, object] = {
        "smc_status_raw": status_raw,
        "smc_status_normalized": bucket_info["english_bucket"],
        "status_is_final": bool(bucket_info["is_final"]),
    }
    if bucket_info["is_final"]:
        counters["reached_final"] = 1
    if bucket_info["english_bucket"] == "Unknown":
        counters["unmapped_status"] = status_raw

    if (bucket_info["english_bucket"] == "Admin_Letter" and not attempt.get("response_text")):
        letter_text = fetch_admin_letter_text(session, request_number)
        if letter_text:
            update_fields["response_text"] = letter_text
            counters["letters_fetched"] = 1

    _mark_checked(attempt["id"], update_fields)
    sb.insert(EVENTS_TABLE, {
        "case_id": attempt["case_id"],
        "attempt_id": attempt["id"],
        "event_type": "status_sync_checked",
        "created_by": SYSTEM_CREATED_BY,
        "details": {
            "request_number": request_number,
            "status_raw": status_raw,
            "resolved_bucket": bucket_info["english_bucket"],
            "source": "bulk_window" if counters["used_bulk"] else "single_lookup",
            "response_text_captured": bool(update_fields.get("response_text")),
        },
    })
    counters["updated"] = 1

    if bucket_info["english_bucket"] in BUCKET_TO_CASE_STATUS:
        case_rows = sb.select(CASES_TABLE, select="case_status", filters={"id": f"eq.{attempt['case_id']}"})
        current_case_status = case_rows[0]["case_status"] if case_rows else None
        if current_case_status in ("SUBMITTED", "PENDING", "ADMIN_LETTER", "RESUBMISSION"):
            new_case_status = BUCKET_TO_CASE_STATUS[bucket_info["english_bucket"]]
            sb.update(CASES_TABLE, attempt["case_id"], {"case_status": new_case_status})
            sb.insert(EVENTS_TABLE, {
                "case_id": attempt["case_id"],
                "attempt_id": attempt["id"],
                "event_type": "case_status_advanced",
                "created_by": SYSTEM_CREATED_BY,
                "details": {"from": current_case_status, "to": new_case_status,
                            "via_bucket": bucket_info["english_bucket"]},
            })

    return counters


# =====================================================================
# MAIN
# =====================================================================

def main():
    username, password = _get_smc_credentials()
    if not username or not password:
        log.error("Neither SMC_USERNAME_2/SMC_PASSWORD_2 nor SMC_USERNAME/SMC_PASSWORD are set — aborting.")
        sys.exit(1)

    _pipeline_module.USERNAME = username
    _pipeline_module.PASSWORD = password

    session_wrapper = SMCSession()
    if not session_wrapper.login():
        log.error("SMC login failed — aborting.")
        sys.exit(1)
    session = session_wrapper.s

    status_map = load_status_map()
    if not status_map:
        log.error("smc_status_map is empty — run schema_additions_phase1.sql first. Aborting.")
        sys.exit(1)

    today_iso = cairo_today_iso()
    start_iso = (datetime.strptime(today_iso, "%Y-%m-%d") - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    bulk_window = fetch_status_window(session, start_iso, today_iso)

    attempts = load_open_attempts()
    log.info(f"{len(attempts)} open attempt(s) to check "
             f"(bulk window covers submissions {start_iso}..{today_iso}).")

    lookup_budget = {"remaining": MAX_SINGLE_STATUS_LOOKUPS}

    checked = updated = letters_fetched = reached_final = 0
    used_bulk = used_single = budget_exhausted = not_found = crashed = 0
    unmapped_statuses = set()

    for attempt in attempts:
        try:
            result = process_one_attempt(session, status_map, attempt, bulk_window, lookup_budget, today_iso)
        except Exception as exc:
            # Second layer of defense against exactly what happened on
            # 2026-09-15: one attempt's unexpected error is recorded and the
            # sweep continues instead of the whole process dying.
            crashed += 1
            checked += 1
            log.error(f"  attempt {attempt.get('id')} (request {attempt.get('website_request_id')}) "
                      f"raised {type(exc).__name__}: {exc}")
            try:
                sb.insert(EVENTS_TABLE, {
                    "case_id": attempt.get("case_id"),
                    "attempt_id": attempt.get("id"),
                    "event_type": "status_sync_error",
                    "created_by": SYSTEM_CREATED_BY,
                    "details": {"error": f"{type(exc).__name__}: {exc}"},
                })
            except Exception:
                pass
            continue

        if result["budget_exhausted"]:
            budget_exhausted += 1
            continue

        checked += result["checked"]
        updated += result["updated"]
        letters_fetched += result["letters_fetched"]
        reached_final += result["reached_final"]
        used_bulk += result["used_bulk"]
        used_single += result["used_single_lookup"]
        not_found += result["not_found"]
        if result.get("unmapped_status"):
            unmapped_statuses.add(result["unmapped_status"])

    log.info(f"Done. Checked {checked} (bulk {used_bulk}, single-lookup {used_single}), "
             f"updated {updated}, reached a final status {reached_final}, letters fetched "
             f"{letters_fetched}, not found on site {not_found}, budget-exhausted "
             f"(deferred to next run) {budget_exhausted}, crashed {crashed}.")
    if unmapped_statuses:
        log.warning(f"Unmapped statuses seen — add these to smc_status_map: {sorted(unmapped_statuses)}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(
                f"## Decree status sync\n"
                f"- Bulk window: **{start_iso} → {today_iso}** ({len(bulk_window)} request(s) returned)\n"
                f"- Open attempts checked: **{checked}** "
                f"(from bulk window: {used_bulk}, via targeted single lookup: {used_single})\n"
                f"- Rows updated: **{updated}**\n"
                f"- Reached a final status this run (now excluded from future sweeps): **{reached_final}**\n"
                f"- Letter texts newly captured: **{letters_fetched}**\n"
                f"- Not found on SMC at all this run: **{not_found}**\n"
                f"- Deferred to next run (single-lookup budget exhausted): **{budget_exhausted}**\n"
                f"- Crashed on an individual attempt (see decree_request_events, "
                f"event_type=status_sync_error): **{crashed}**\n"
            )
            if unmapped_statuses:
                f.write(f"- ⚠️ Unmapped statuses — add to `smc_status_map`: "
                        f"{', '.join(sorted(unmapped_statuses))}\n")
            if budget_exhausted:
                f.write(f"\n> {budget_exhausted} stale attempt(s) were deferred rather than skipped silently — "
                        f"they'll be prioritized first on tomorrow's run (oldest-checked-first ordering). "
                        f"Raise MAX_SINGLE_STATUS_LOOKUPS if this number stays large night after night.\n")

    if attempts and crashed > len(attempts) / 2:
        log.error(f"More than half of tonight's attempts crashed ({crashed}/{len(attempts)}) — "
                  f"treating this run as failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
