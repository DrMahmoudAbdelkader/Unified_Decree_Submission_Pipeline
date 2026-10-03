#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decree_status_and_letters_sync.py
==========================================================================
Daily status sweep — REBUILT to use the confirmed-working status
mechanism from request_status_sync.py (a sibling repo's script, supplied
2026-09-15), instead of the SearchRecommendationRequest + _PrintLetters
popup-scraping this file used before.

CHANGES IN THIS REVISION (2026-10-03)
--------------------------------------------------------------------------
Reviewed against the real smc_status_map: no pre-final stage is marked
is_final (only Approved / Admin_Letter / Cancelled* are), so the "pre-final
treated as final" theory is ruled out. What WAS weak, and is fixed here:

 1. SEED ROWS HAD NO FALLBACK. A decree_request_month_seed row that did not
    come back in the bulk SendRequestStatusJson window was skipped silently
    and retried the same way every night — if the bulk call missed it
    (truncated/failed chunk, site quirk) it stayed at its stale status
    forever (this is how requests whose decree had already been issued kept
    reading "لجنة طبية" / "محول إلى طبيب آخر" in the seed table). Seed rows
    missing from the bulk result now get the same targeted single lookup
    in-app attempts already get, capped per run by MAX_SEED_SINGLE_LOOKUPS
    (default 200), newest requests first.
 2. fetch_status_window() RETRIES. A transient HTTP 5xx / network error on
    one 10-day chunk used to return {} straight away, which made every row
    in that chunk look "not found". It now retries (STATUS_WINDOW_RETRIES,
    default 2) with a short back-off before giving up.
 3. request_date TOLERANCE. The seed refresh passed request_date straight to
    strptime("%Y-%m-%d"); a timestamp-typed column ("2026-09-23T00:00:00")
    would raise and silently disable the whole seed refresh (it is caught
    and logged as "Seed table refresh failed"). Only the first 10 characters
    are used now.
 4. SPELLING-VARIANT LOOKUP. smc_status_map matching was exact-string only
    (that is why the table carries both "قرار نهائى" and "قرار نهائي").
    A variant not in the table fell to "Unknown". resolve_status_bucket()
    now falls back to a normalized match (أ/إ/آ->ا, ى->ي, tatweel and extra
    whitespace removed) when — and only when — it maps to exactly one bucket.
    Unknown is still never final.
 5. One failing seed row can no longer abort the rest of the seed refresh,
    and unmapped statuses seen on seed rows are logged like attempt ones.
 6. The run summary now reports seed single lookups / not-found counts.

Nothing else changed: same tables, same columns, same schedule.

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
    MAX_SEED_SINGLE_LOOKUPS           — default 200 (NEW).
    STATUS_WINDOW_RETRIES             — default 2 (NEW).

BEFORE TRUSTING THIS FOR REAL: smc_status_map now needs a row for every
raw status SendRequestStatusJson can return — including the early ones
(تم التسجيل, لجنة طبية, تحويل الي طبيب اخر, توصية مبدئية, ...) that the
old popup-based version never saw at all. Any status text that comes back
without a matching row falls into the "Unknown" bucket (never guessed as
final) and is logged — check the run summary's "Unmapped statuses" line
after the first real run and add rows for whatever shows up there.
(Known gap in the current table: 'تأجيل الطلب لإرفاق ملف الأشعة'.)

FULL-MONTH SEED REFRESH (decree_request_month_seed)
--------------------------------------------------------------------------
After the open-attempt sweep above finishes, sync_seed_table() reuses the
SAME logged-in session and the SAME SendRequestStatusJson bulk endpoint to
refresh decree_request_month_seed (see decree_request_month_seed_schema.sql
and decree-status-tracking.js) — the full month's SMC export used to cover
requests that have no in-app case/attempt at all. No second login: that
endpoint already returns any request's status for a date window regardless
of whether this app tracked it, chunked into ~10-day windows since seed rows
can span the whole month. Rows the bulk windows miss now get a capped
targeted single lookup (see CHANGES above). This replaces the standalone
decree_request_seed_status_refresh.py stub — delete that file, nothing runs
it anymore. A seed-refresh failure is caught and logged separately; it never
fails the run or affects the attempt sweep above.

DECREE NUMBER EXTRACTION (decree_number)
--------------------------------------------------------------------------
After the sweep and the seed refresh, sync_decree_numbers() finds every row
that is at the Approved bucket (قرار نهائى) but has no decree_number yet —
in decree_request_attempts AND decree_request_month_seed — opens
GET /smc/Requests/Details/<request id> with the same logged-in session,
and reads the issued decree number from the "القرارات" column of the
request-history table (logic ported from the Excel script that was
confirmed working). Because the target list is simply "approved and still
no number", the very first run is also the one-time backfill of everything
already approved, and a request whose page had no number yet is retried on
later nights automatically. Capped per run at MAX_DECREE_LOOKUPS (default
400); the rest is picked up the next night. The decree's total VALUE is NOT
fetched here (different account) — see decree_value_sync.py.
Needs decree_number_value_migration.sql applied once; until then this step
logs a hint and skips, and the rest of the run is unaffected.
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
# NEW: per-run cap on targeted single lookups for seed rows the bulk windows
# did not return (see sync_seed_table()).
MAX_SEED_SINGLE_LOOKUPS = int(os.environ.get("MAX_SEED_SINGLE_LOOKUPS", "200"))
# NEW: retries for a failed SendRequestStatusJson bulk-window call.
STATUS_WINDOW_RETRIES = int(os.environ.get("STATUS_WINDOW_RETRIES", "2"))

ATTEMPTS_TABLE = "decree_request_attempts"
CASES_TABLE = "decree_request_cases"
EVENTS_TABLE = "decree_request_events"
STATUS_MAP_TABLE = "smc_status_map"
# Full-month backfill table (see decree_request_month_seed_schema.sql /
# decree-status-tracking.js) — most of its rows have no in-app case/attempt
# at all, so they can't go through load_open_attempts()/process_one_attempt()
# above. sync_seed_table() below refreshes them instead, reusing this
# same script's session and the same SendRequestStatusJson bulk endpoint,
# since that endpoint already returns any request's status for a date
# window regardless of whether this app tracked it. This replaces the old
# decree_request_seed_status_refresh.py stub — delete that file, it's no
# longer needed as a separate script/login.
SEED_TABLE = "decree_request_month_seed"

# Decree-number extraction (see sync_decree_numbers()). Decree numbers start
# with the year they were issued; anything else found on the page (older
# decrees quoted in the history table) is ignored.
DECREE_YEAR_PREFIXES = tuple(
    p.strip() for p in os.environ.get("DECREE_YEAR_PREFIXES", "2026,2027").split(",") if p.strip()
)
MAX_DECREE_LOOKUPS = int(os.environ.get("MAX_DECREE_LOOKUPS", "400"))

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


def _date_part(value) -> str:
    """'2026-09-23' / '2026-09-23T00:00:00+00:00' / a date object -> '2026-09-23'.
    (NEW) Used wherever a stored date/timestamp is fed to strptime("%Y-%m-%d")."""
    return str(value)[:10]


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


def fetch_status_window(session, start_date_iso: str, end_date_iso: str,
                        retries: Optional[int] = None) -> Dict[str, dict]:
    """ONE call covering every request submitted in [start, end]. Returns a
    dict keyed by request_number so process_one_attempt() can look an
    attempt's status up with no further network call for anything inside
    the window.

    (NEW) Retries on a network error or a non-200 answer (the site has
    answered HTTP 500 for an over-wide window before) instead of returning
    {} on the first failure — an empty result makes every row in the window
    look "not found"."""
    if retries is None:
        retries = STATUS_WINDOW_RETRIES
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

    resp = None
    for attempt in range(1, retries + 2):
        try:
            candidate = session.post(url, data=payload, timeout=30)
            if candidate.status_code == 200:
                resp = candidate
                break
            log.warning(f"SendRequestStatusJson (bulk window) HTTP {candidate.status_code} "
                        f"(attempt {attempt}/{retries + 1})")
        except Exception as e:
            log.warning(f"SendRequestStatusJson (bulk window) error: {e} (attempt {attempt}/{retries + 1})")
        if attempt <= retries:
            time.sleep(REQUEST_DELAY * attempt * 3)
    if resp is None:
        log.error(f"SendRequestStatusJson (bulk window {start_date_iso}..{end_date_iso}) failed "
                  f"after {retries + 1} attempt(s).")
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
        # When RequestNumber is set the site ignores the date frame, so
        # From = To = the same day is enough (confirmed by the owner).
        "StartDate": _smc_datetime_str(today_iso),
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
# Status normalization — same mechanism, now also tolerant of Arabic
# spelling variants (NEW) that aren't literally in smc_status_map.
# =====================================================================

_AR_NORMALIZE = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ـ": None})


def _norm_status(text) -> str:
    """Collapses the spelling variants the site is known to mix (أ/إ/آ ->
    ا, ى -> ي, tatweel removed, runs of whitespace -> one space)."""
    return re.sub(r"\s+", " ", str(text or "")).strip().translate(_AR_NORMALIZE)


# normalized spelling -> smc_status_map row, or None when two DIFFERENT
# buckets collide on the same normalized spelling (never guess then).
_NORMALIZED_STATUS_INDEX: Dict[str, Optional[dict]] = {}


def _build_normalized_index(status_map: Dict[str, dict]) -> None:
    _NORMALIZED_STATUS_INDEX.clear()
    for arabic, row in status_map.items():
        key = _norm_status(arabic)
        if not key:
            continue
        if key not in _NORMALIZED_STATUS_INDEX:
            _NORMALIZED_STATUS_INDEX[key] = row
        else:
            existing = _NORMALIZED_STATUS_INDEX[key]
            if existing is None or existing.get("english_bucket") != row.get("english_bucket"):
                _NORMALIZED_STATUS_INDEX[key] = None


def resolve_status_bucket(status_map: Dict[str, dict], arabic_status: str) -> dict:
    """Exact match first (unchanged behavior). If that misses, try the
    normalized spelling — only when it maps to exactly one bucket. Anything
    else is "Unknown", which is never final."""
    row = status_map.get(arabic_status)
    if row is not None:
        return row
    if arabic_status:
        key = _norm_status(arabic_status)
        # Rebuild lazily if a caller passes a map the index wasn't built from.
        if not _NORMALIZED_STATUS_INDEX and status_map:
            _build_normalized_index(status_map)
        hit = _NORMALIZED_STATUS_INDEX.get(key)
        if hit is not None:
            return hit
    return {"arabic_status": arabic_status, "english_bucket": "Unknown", "is_final": False, "requires_action": False}


def load_status_map() -> Dict[str, dict]:
    rows = sb.select(STATUS_MAP_TABLE, select="arabic_status,english_bucket,is_final,requires_action")
    status_map = {r["arabic_status"]: r for r in rows}
    _build_normalized_index(status_map)
    return status_map


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

    Ordered by last_status_checked_at ascending, nulls first, rather than
    by id. This matters now that a per-run cap (MAX_SINGLE_STATUS_LOOKUPS)
    can mean not every stale attempt gets its single-lookup budget spent on
    it every night — ordering by staleness means the budget always goes to
    whichever attempts have gone longest without a check, instead of the
    same id-ordered prefix winning (and the same tail starving) every
    single run."""
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
# Full-month seed table (decree_request_month_seed) refresh — reuses this
# script's already-logged-in session and the same SendRequestStatusJson
# bulk endpoint the open-attempt sweep uses above. No second login, no
# separate script: that endpoint already returns EVERY request's status
# for a date window, whether or not this app has a case/attempt for it,
# which is exactly what a seed row is.
# =====================================================================

def load_open_seed_rows(status_map: Dict[str, dict]) -> List[dict]:
    """Every decree_request_month_seed row not already at a final status —
    reusing the exact same status_map (the exact same Approved/Admin_Letter/
    Cancelled* definition of "final") the open-attempt sweep above already
    uses, so a status this app doesn't treat as final for an in-app attempt
    isn't treated as final here either. Paged the same way
    load_open_attempts() is, for the same reason (PostgREST's unpaged
    response cap)."""
    page_size = 500
    offset = 0
    rows: List[dict] = []
    while True:
        page = sb.select(
            SEED_TABLE,
            select="id,request_number,last_known_status,request_date",
            filters={"offset": str(offset)},
            order="request_date.asc",
            limit=page_size,
        )
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return [
        r for r in rows
        if not resolve_status_bucket(status_map, r.get("last_known_status") or "")["is_final"]
    ]


def _chunk_date_ranges(start_iso: str, end_iso: str, chunk_days: int = 10):
    """Splits [start, end] into <=chunk_days-wide inclusive slices. A full
    month is well outside the ~15-day window this endpoint has actually
    been exercised at (LOOKBACK_DAYS's default) — chunking at the same
    proven scale avoids finding out the hard way whether one huge
    single-call window gets silently truncated by the site."""
    cur = datetime.strptime(_date_part(start_iso), "%Y-%m-%d")
    end = datetime.strptime(_date_part(end_iso), "%Y-%m-%d")
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        yield cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")
        cur = chunk_end + timedelta(days=1)


def sync_seed_table(session, status_map: Dict[str, dict], today_iso: str) -> Dict[str, object]:
    """Refreshes decree_request_month_seed the same way process_one_attempt()
    refreshes decree_request_attempts above — same session, same bulk
    endpoint, same status_map — just against a wider, chunked date window
    since seed rows can be a full month old. Only ever writes
    last_known_status/imported_at on decree_request_month_seed itself; never
    touches decree_request_cases/decree_request_attempts/decree_request_events,
    same as the schema/JS side already assume about this table.

    (NEW) A seed row the bulk windows did not return is no longer skipped
    forever: up to MAX_SEED_SINGLE_LOOKUPS of them (newest requests first —
    the ones most likely to have changed) get the same targeted single
    lookup in-app attempts already get."""
    counters: Dict[str, object] = {
        "checked": 0, "updated": 0, "reached_final": 0,
        "used_single_lookup": 0, "not_found": 0, "deferred": 0, "crashed": 0,
        "unmapped_statuses": [],
    }
    open_rows = load_open_seed_rows(status_map)
    if not open_rows:
        log.info("Seed refresh: no open decree_request_month_seed rows to check.")
        return counters

    dated_rows = [r for r in open_rows if r.get("request_date")]
    earliest = min((_date_part(r["request_date"]) for r in dated_rows), default=today_iso)
    log.info(f"Seed refresh: {len(open_rows)} open seed row(s), window {earliest}..{today_iso}.")

    bulk: Dict[str, dict] = {}
    for chunk_start, chunk_end in _chunk_date_ranges(earliest, today_iso):
        bulk.update(fetch_status_window(session, chunk_start, chunk_end))
        time.sleep(REQUEST_DELAY)

    unmapped = set()

    def _apply(row: dict, hit: dict) -> None:
        new_status = hit["request_status"]
        bucket = resolve_status_bucket(status_map, new_status)
        if bucket["english_bucket"] == "Unknown":
            unmapped.add(new_status)
        if new_status == row.get("last_known_status"):
            return
        sb.update(SEED_TABLE, row["id"], {
            "last_known_status": new_status,
            "imported_at": datetime.now().astimezone().isoformat(),
        })
        counters["updated"] += 1
        if bucket["is_final"]:
            counters["reached_final"] += 1

    missing: List[dict] = []
    for row in open_rows:
        counters["checked"] += 1
        hit = bulk.get(str(row["request_number"]))
        if hit is None or not hit.get("request_status"):
            missing.append(row)   # not in any bulk window -> single-lookup fallback below
            continue
        try:
            _apply(row, hit)
        except Exception as exc:
            counters["crashed"] += 1
            log.error(f"  seed row {row.get('id')} (request {row.get('request_number')}) "
                      f"raised {type(exc).__name__}: {exc}")

    # Newest first: recent requests are the ones whose status is still moving,
    # and old rows SMC no longer returns would otherwise eat the whole budget.
    missing.sort(key=lambda r: _date_part(r.get("request_date") or ""), reverse=True)
    budget = MAX_SEED_SINGLE_LOOKUPS
    if missing:
        log.info(f"Seed refresh: {len(missing)} open seed row(s) were not in the bulk windows; "
                 f"single-looking up to {budget}.")
    for row in missing:
        if budget <= 0:
            counters["deferred"] += 1
            continue
        budget -= 1
        request_number = str(row["request_number"])
        try:
            hit = fetch_single_status(session, request_number, today_iso)
            time.sleep(REQUEST_DELAY)
            counters["used_single_lookup"] += 1
            if hit is None or not hit.get("request_status"):
                counters["not_found"] += 1
                continue   # leave last_known_status as-is, retried next run
            _apply(row, hit)
        except Exception as exc:
            counters["crashed"] += 1
            log.error(f"  seed row {row.get('id')} (request {request_number}) "
                      f"raised {type(exc).__name__}: {exc}")

    counters["unmapped_statuses"] = sorted(unmapped)
    log.info(f"Seed refresh done. Checked {counters['checked']}, updated {counters['updated']}, "
             f"reached a final status {counters['reached_final']}, single lookups "
             f"{counters['used_single_lookup']} (not found {counters['not_found']}, deferred "
             f"{counters['deferred']}), crashed {counters['crashed']}.")
    if unmapped:
        log.warning(f"Unmapped statuses seen on seed rows — add these to smc_status_map: {sorted(unmapped)}")
    return counters


# =====================================================================
# Decree number (رقم القرار) for finally-approved requests
# =====================================================================

def extract_decree_numbers(html: str) -> List[str]:
    """Decree numbers found in the 'القرارات' column of the request-history
    table on /smc/Requests/Details/<id>. Ignores the recommendations column
    (التوصيات) and any numbers hard-coded inside <script> blocks (the page's
    PrevDec() JS contains an unrelated ParamDecreeId)."""
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    for th in soup.find_all("th"):
        if th.get_text(strip=True) != "القرارات":
            continue
        table = th.find_parent("table")
        if table is None:
            continue
        for a in table.find_all("a"):
            txt = a.get_text(strip=True)
            if re.fullmatch(r"\d{10,}", txt):
                found.append(txt)
                continue
            m = re.search(r"DecreesReadyToSend=(\d+)", a.get("onclick", ""))
            if m:
                found.append(m.group(1))
    return list(dict.fromkeys(found))


def fetch_decree_number(session_wrapper, request_number: str):
    """Returns (ok, decree_number_or_None).
    ok=False                  -> details page could not be fetched/validated
    ok=True,  decree is None  -> page fine, no decree on it (yet)
    If several year-matching decrees are listed, the LAST one is used (the
    most recently issued) and the full list is logged."""
    url = f"{BASE_URL}/smc/Requests/Details/{request_number}"
    for attempt in range(1, MAX_POPUP_ATTEMPTS + 1):
        try:
            r = session_wrapper.s.get(url, timeout=30)
            if r.status_code == 200 and "Home/Index" in r.url and "username" in r.text.lower():
                log.warning("  session expired during decree lookup — re-logging in …")
                if not session_wrapper.login():
                    return False, None
                r = session_wrapper.s.get(url, timeout=30)
        except Exception as e:
            log.warning(f"  {request_number}: details attempt {attempt}/{MAX_POPUP_ATTEMPTS} error: {e}")
            time.sleep(REQUEST_DELAY * attempt)
            continue
        if r.status_code == 200 and 'id="requestID"' in r.text:
            wanted = [d for d in extract_decree_numbers(r.text) if d.startswith(DECREE_YEAR_PREFIXES)]
            if len(wanted) > 1:
                log.warning(f"  {request_number}: {len(wanted)} decree numbers on page {wanted} — using the last.")
            return True, (wanted[-1] if wanted else None)
        log.warning(f"  {request_number}: details attempt {attempt}/{MAX_POPUP_ATTEMPTS} — "
                    f"HTTP {r.status_code} / not the details page")
        time.sleep(REQUEST_DELAY * attempt)
    return False, None


def sync_decree_numbers(session_wrapper, status_map: Dict[str, dict]) -> Dict[str, int]:
    """Fills decree_number for every Approved row that doesn't have one yet,
    in decree_request_attempts and decree_request_month_seed. Oldest-checked
    first (never-checked first), capped at MAX_DECREE_LOOKUPS per run. Only
    ever writes decree_number / decree_number_checked_at."""
    counters = {"checked": 0, "filled": 0, "no_decree": 0, "failed": 0, "crashed": 0, "capped": 0}
    order = "decree_number_checked_at.asc.nullsfirst,id.asc"
    targets: List[tuple] = []   # (table, row id, request number)

    try:
        for r in sb.select(
            ATTEMPTS_TABLE, select="id,website_request_id",
            filters={"website_request_id": "not.is.null",
                     "smc_status_normalized": "eq.Approved",
                     "decree_number": "is.null"},
            order=order, limit=MAX_DECREE_LOOKUPS,
        ):
            targets.append((ATTEMPTS_TABLE, r["id"], str(r["website_request_id"])))

        approved_statuses = sorted(a for a, v in status_map.items() if v.get("english_bucket") == "Approved")
        for status in approved_statuses:
            room = MAX_DECREE_LOOKUPS - len(targets)
            if room <= 0:
                break
            for r in sb.select(
                SEED_TABLE, select="id,request_number",
                filters={"last_known_status": f"eq.{status}", "decree_number": "is.null"},
                order=order, limit=room,
            ):
                targets.append((SEED_TABLE, r["id"], str(r["request_number"])))
    except RuntimeError as e:
        if "decree_number" in str(e):
            log.error("decree_number column missing — run decree_number_value_migration.sql once. "
                      "Skipping decree extraction this run.")
            return counters
        raise

    if not targets:
        log.info("Decree numbers: nothing to fill.")
        return counters
    if len(targets) >= MAX_DECREE_LOOKUPS:
        counters["capped"] = 1
        log.info(f"Decree numbers: hit the per-run cap ({MAX_DECREE_LOOKUPS}); the rest continues next run.")

    cache: Dict[str, Optional[tuple]] = {}   # one fetch per request number per run
    for table, row_id, request_number in targets:
        counters["checked"] += 1
        try:
            if request_number not in cache:
                cache[request_number] = fetch_decree_number(session_wrapper, request_number)
                time.sleep(REQUEST_DELAY)
            ok, decree = cache[request_number]
            fields: Dict[str, object] = {"decree_number_checked_at": datetime.now().astimezone().isoformat()}
            if ok and decree:
                fields["decree_number"] = decree
                counters["filled"] += 1
                log.info(f"  {request_number} -> decree {decree}")
            elif ok:
                counters["no_decree"] += 1
                log.info(f"  {request_number}: approved but no decree number on the page yet")
            else:
                counters["failed"] += 1
            sb.update(table, row_id, fields)
        except Exception as exc:
            counters["crashed"] += 1
            log.error(f"  decree lookup for {request_number} raised {type(exc).__name__}: {exc}")

    log.info(f"Decree numbers done. Checked {counters['checked']}, filled {counters['filled']}, "
             f"no decree yet {counters['no_decree']}, fetch failed {counters['failed']}, "
             f"crashed {counters['crashed']}.")
    return counters


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
    # Chunked like the seed refresh: one call over a large LOOKBACK_DAYS
    # (e.g. 33) made the site answer HTTP 500, which silently forced every
    # attempt onto a slow single lookup.
    bulk_window: Dict[str, dict] = {}
    for chunk_start, chunk_end in _chunk_date_ranges(start_iso, today_iso):
        bulk_window.update(fetch_status_window(session, chunk_start, chunk_end))
        time.sleep(REQUEST_DELAY)

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

    try:
        seed_counters = sync_seed_table(session, status_map, today_iso)
    except Exception as exc:
        # Best-effort, same spirit as the fallback query in
        # decree-status-tracking.js: a seed-refresh failure never fails the
        # whole run or blocks the open-attempt sweep above, which already
        # completed successfully by this point.
        seed_counters = {"checked": 0, "updated": 0, "reached_final": 0,
                         "used_single_lookup": 0, "not_found": 0, "deferred": 0,
                         "crashed": 0, "unmapped_statuses": []}
        log.error(f"Seed table refresh failed (open-attempt sweep above still succeeded): {exc}")

    try:
        decree_counters = sync_decree_numbers(session_wrapper, status_map)
    except Exception as exc:
        decree_counters = {"checked": 0, "filled": 0, "no_decree": 0, "failed": 0, "crashed": 0, "capped": 0}
        log.error(f"Decree number extraction failed (the sweep above still succeeded): {exc}")

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
            f.write(
                f"\n## Full-month seed refresh (decree_request_month_seed)\n"
                f"- Open seed rows checked: **{seed_counters['checked']}**\n"
                f"- Updated: **{seed_counters['updated']}**\n"
                f"- Reached a final status this run: **{seed_counters['reached_final']}**\n"
                f"- Not in the bulk windows → targeted single lookups: **{seed_counters.get('used_single_lookup', 0)}** "
                f"(not found on SMC: {seed_counters.get('not_found', 0)}, "
                f"deferred to next run: {seed_counters.get('deferred', 0)})\n"
            )
            if seed_counters.get("crashed"):
                f.write(f"- Crashed on an individual seed row (see log): **{seed_counters['crashed']}**\n")
            if seed_counters.get("unmapped_statuses"):
                f.write(f"- ⚠️ Unmapped statuses on seed rows — add to `smc_status_map`: "
                        f"{', '.join(seed_counters['unmapped_statuses'])}\n")
            if seed_counters.get("deferred"):
                f.write(f"\n> {seed_counters['deferred']} seed row(s) were deferred (single-lookup cap). "
                        f"Raise MAX_SEED_SINGLE_LOOKUPS if this stays large night after night.\n")

    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(
                f"\n## Decree numbers (approved requests)\n"
                f"- Looked up: **{decree_counters['checked']}**\n"
                f"- Decree numbers filled: **{decree_counters['filled']}**\n"
                f"- Approved but no number on the page yet: **{decree_counters['no_decree']}**\n"
                f"- Fetch failed: **{decree_counters['failed']}**\n"
            )
            if decree_counters["capped"]:
                f.write(f"\n> Hit the per-run cap ({MAX_DECREE_LOOKUPS}); remaining approved requests "
                        f"continue on the next run. Raise `max_decree_lookups` for a bigger backfill.\n")

    if attempts and crashed > len(attempts) / 2:
        log.error(f"More than half of tonight's attempts crashed ({crashed}/{len(attempts)}) — "
                  f"treating this run as failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
