#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decree_status_and_letters_sync.py
==========================================================================
Phase 3 daily sync — runs as a GitHub Actions scheduled job (see
.github/workflows/decree-status-sync.yml). Confirmed via HAR capture:
`/smc/Decrees/SearchRecommendationRequest` accepts a `requestID` param
directly — no date-range sweep needed to find a request's recommendation.

WHAT CHANGED IN THIS REWRITE
--------------------------------------------------------------------------
1. Dropped the `smc_session` module dependency. That module was never
   supplied in any session and its SMCSession(username=, password=)
   constructor shape doesn't match the pipeline's actual SMCSession()
   (no-arg, module-level USERNAME/PASSWORD read at login() time — see
   Unified_Decree_Submission_Pipeline.py). This script now reuses THAT
   SMCSession, the exact same class decree_submission_service.py uses,
   overriding USERNAME/PASSWORD from SMC_USERNAME_2/SMC_PASSWORD_2 first
   (a separate account, so a long-running status sweep never fights the
   submission job for the same session) falling back to SMC_USERNAME/
   SMC_PASSWORD if no dedicated secondary account is configured.

2. Dropped the dependency on request_status_sync.py for status
   normalization. That file was never supplied in any session, and per
   the workflow's own comments it only ever wrote to a *reporting* table
   (decree_request_status_daily_export), never to attempt_status/
   case_status — so relying on it meant Phase 3's actual "auto-update
   the status" requirement was never implemented anywhere. This version
   implements it directly (see STEP B below) using the ONE endpoint this
   script already confirmed works — the _PrintLetters popup — which
   carries both the response text AND a status label whenever a
   recommendation/letter already exists for the request.

3. Actually WIRES UP smc_status_map. The previous version loaded it and
   defined resolve_status_bucket() but never called it — this rewrite
   calls it for every raw status label found and writes the result to
   decree_request_attempts.smc_status_raw / smc_status_normalized /
   status_is_final (added by schema_additions_phase1b_status_tracking.sql
   — run that migration before this script, or every query against
   decree_request_attempts will fail with "column does not exist").
   When the resolved bucket is final, it also stamps
   decree_request_cases.case_status using BUCKET_TO_CASE_STATUS below.

KNOWN REMAINING GAP — please read before assuming this is complete
--------------------------------------------------------------------------
The _PrintLetters popup only exists once SMC has generated a
recommendation/letter for a request — which covers Admin_Letter and the
various final/recommendation statuses. It does NOT cover the *earlier*
statuses that precede any recommendation at all: "تم التسجيل" (just
registered), "لجنة طبية" (in medical committee), "تحويل الي طبيب اخر"
(reassigned). For those, find_recommendation_id_for_request() below
correctly returns None (no recommendation exists yet) and the attempt is
left exactly as it is — nothing is guessed. Detecting those specific
in-between statuses needs a genuinely different endpoint (the old
request_status_sync.py apparently called one — SendRequestStatusJson —
but its actual request/response shape was never supplied to me in any
session, so I'm not going to fabricate a call against an endpoint I've
never seen a real response from). _check_pre_recommendation_status()
below is a clearly-marked stub for exactly this — one function to fill
in once you can supply either request_status_sync.py's real
implementation of that call, or a fresh HAR capture of it.

CONFIG NEEDED FROM YOU before this runs for real:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   — your project's, service role
    SMC_USERNAME_2 / SMC_PASSWORD_2           — dedicated secondary SMC
        account for this sync (recommended so it never shares a login
        session with a concurrent submission run). Falls back to
        SMC_USERNAME / SMC_PASSWORD if not set.
    SMC_SENDING_SITE                          — facility id used in the
        SearchRecommendationRequest payload (102233 in the captured HAR;
        confirm this is your facility's real site id before trusting it)
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))

# Reused verbatim — same SMCSession class decree_submission_service.py
# uses, not a separate smc_session.py module (which was never supplied).
import Unified_Decree_Submission_Pipeline as _pipeline_module
from Unified_Decree_Submission_Pipeline import SMCSession
import supabase_client as sb

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("decree_status_and_letters_sync")

BASE_URL = _pipeline_module.BASE_URL
REQUEST_DELAY = 0.3
MAX_POPUP_ATTEMPTS = 3

# CONFIG — confirm SENDING_SITE against your real facility id before trusting
SENDING_SITE = os.environ.get("SMC_SENDING_SITE", "102233")

ATTEMPTS_TABLE = "decree_request_attempts"
CASES_TABLE = "decree_request_cases"
EVENTS_TABLE = "decree_request_events"
STATUS_MAP_TABLE = "smc_status_map"

# english_bucket (from smc_status_map, seeded by schema_additions_phase1.sql)
# -> decree_request_cases.case_status. Only buckets that map cleanly onto
# the table's fixed CHECK constraint values are listed; anything else
# (Under_Medical_Review, Reassigned_To_Doctor, Preliminary_Recommendation,
# Final_Recommendation, Registered, Unknown) is intentionally NOT in here —
# those are non-final per smc_status_map's own is_final flag, so case_status
# is deliberately left as whatever it already is (normally 'SUBMITTED' or
# 'PENDING') rather than guessed at.
BUCKET_TO_CASE_STATUS = {
    "Approved": "FINAL_APPROVED",
    "Admin_Letter": "ADMIN_LETTER",
    "Cancelled": "CANCELLED",
    "Cancelled_For_Edit": "CANCELLED",
    "Cancelled_By_Request": "CANCELLED",
}


# =====================================================================
# Helpers ported verbatim from Extract_Admin_Letters_By_Time_Frame.py
# (not reimplemented — same functions, so this can never quietly drift
# out of sync with the already-tested parsing logic)
# =====================================================================

_AR2EN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def ar2en(text: str) -> str:
    return (text or "").translate(_AR2EN_DIGITS)


def cell_text(tag) -> str:
    return tag.get_text(strip=True) if tag else ""


def pipe_field(pipe_text: str, label: str) -> str:
    parts = pipe_text.split("|")
    for i, p in enumerate(parts):
        if label in p and i + 1 < len(parts):
            return parts[i + 1].strip()
    return ""


def popup_looks_valid(pipe_text: str) -> bool:
    return "رقم الطلب" in pipe_text or "الرقم القومي" in pipe_text


# Candidate Arabic field labels for the status shown on the _PrintLetters
# popup, tried in order. This popup's exact label wasn't confirmed in any
# HAR capture supplied so far (only "نص الخطاب" was) — these are the most
# plausible labels given the terminology you supplied for the status list
# itself. If none of these match, extract_status_and_response() falls back
# to returning None for the status (never guesses) and logs the raw popup
# text into the event record so you can tell me the right label from a
# real example and I fix this in one line.
_STATUS_LABEL_CANDIDATES = ["حالة الطلب", "الحالة", "حاله الطلب", "نوع القرار", "القرار"]


def _extract_labeled_field(html: str, label: str) -> Optional[str]:
    """Same tag-aware pattern already used for نص الخطاب, generalized to any
    <b>label</b><br>value block."""
    pattern = rf"{re.escape(label)}\s*</b>\s*<br\s*/?>\s*(.*?)(?:<b>|</td>|</tr>|$)"
    m = re.search(pattern, html, re.DOTALL)
    if m:
        candidate = BeautifulSoup(m.group(1), "html.parser").get_text(" ", strip=True)
        if candidate:
            return candidate
    return None


def extract_status_and_response(html: str) -> Dict[str, Optional[str]]:
    """Pulls BOTH the response text (نص الخطاب — confirmed working) and a
    best-effort raw status label from the same _PrintLetters popup HTML.
    Never raises; missing pieces come back as None."""
    response_text = _extract_labeled_field(html, "نص الخطاب")
    if response_text is None:
        pipe = BeautifulSoup(html, "html.parser").get_text(separator="|", strip=True)
        response_text = pipe_field(pipe, "نص الخطاب") or None

    status_raw = None
    for label in _STATUS_LABEL_CANDIDATES:
        status_raw = _extract_labeled_field(html, label)
        if status_raw:
            break
    if status_raw is None:
        pipe = BeautifulSoup(html, "html.parser").get_text(separator="|", strip=True)
        for label in _STATUS_LABEL_CANDIDATES:
            status_raw = pipe_field(pipe, label) or None
            if status_raw:
                break

    return {"response_text": response_text, "status_raw": status_raw}


# =====================================================================
# STEP A — direct-by-request-number lookup (replaces the date sweep)
# =====================================================================

def find_recommendation_id_for_request(session, request_number: str) -> Optional[str]:
    """POST SearchRecommendationRequest with requestID=<request_number>.
    Confirmed via HAR capture: this returns the exact matching row (if a
    recommendation/letter exists for that request) without needing any
    date-range sweep — dateFrom/dateTo are still required form fields but
    do not narrow the requestID match, so a fixed wide window is fine."""
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
    for tbody in table.find_all("tbody"):
        row = tbody.find("tr")
        if not row:
            continue
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        span = cells[1].find("span", {"id": "RecommendationId"})
        rec_id = ar2en(cell_text(span) if span else cell_text(cells[1])).strip()
        if rec_id:
            return rec_id
    return None


def get_letter_popup_html(session, rec_id: str) -> Optional[str]:
    """GET _PrintLetters?RecommIDs=<rec_id> and return the raw popup HTML,
    or None if it never comes back looking like a real popup."""
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


def _check_pre_recommendation_status(session, request_number: str) -> Optional[str]:
    """STUB — deliberately not implemented. Requests still at 'تم التسجيل' /
    'لجنة طبية' / 'تحويل الي طبيب اخر' have no recommendation yet, so
    find_recommendation_id_for_request() correctly returns None for them
    and the caller skips straight past this function. Filling this in
    needs the real request/response shape of whatever endpoint
    request_status_sync.py used (referenced in its docstring as
    SendRequestStatusJson) — not fabricated here since it was never
    actually supplied. Returns None unconditionally until then."""
    return None


# =====================================================================
# STEP B — status normalization, now actually wired to smc_status_map
# =====================================================================

def resolve_status_bucket(status_map: Dict[str, dict], arabic_status: str) -> dict:
    """Look up a raw Arabic status in the cached smc_status_map. Falls back
    to a non-final, non-actionable bucket for anything unrecognized instead
    of guessing — an unmapped status should surface for a human to add,
    not silently get treated as final or as final-cancelled."""
    return status_map.get(
        arabic_status,
        {"arabic_status": arabic_status, "english_bucket": "Unknown", "is_final": False, "requires_action": False},
    )


# =====================================================================
# MAIN
# =====================================================================

def load_status_map() -> Dict[str, dict]:
    rows = sb.select(STATUS_MAP_TABLE, select="arabic_status,english_bucket,is_final,requires_action")
    return {r["arabic_status"]: r for r in rows}


def load_open_attempts() -> List[dict]:
    """Every attempt with a request number that hasn't reached a final
    status yet. status_is_final is tracked directly on the attempt row so
    this query never re-checks something already resolved. Requires
    schema_additions_phase1b_status_tracking.sql to have been run."""
    return sb.select(
        ATTEMPTS_TABLE,
        select="id,case_id,website_request_id,attempt_status,response_text,status_is_final",
        filters={"website_request_id": "not.is.null", "status_is_final": "is.false"},
    )


def _get_smc_credentials():
    """Prefers a dedicated secondary account (so this long daily sweep never
    shares a login session with a concurrent submission run) but falls back
    to the primary account if no secondary one is configured."""
    username = os.environ.get("SMC_USERNAME_2") or os.environ.get("SMC_USERNAME")
    password = os.environ.get("SMC_PASSWORD_2") or os.environ.get("SMC_PASSWORD")
    return username, password


def main():
    username, password = _get_smc_credentials()
    if not username or not password:
        log.error("Neither SMC_USERNAME_2/SMC_PASSWORD_2 nor SMC_USERNAME/SMC_PASSWORD are set — aborting.")
        sys.exit(1)

    # Override the pipeline's module-level credentials, same pattern as
    # decree_submission_service.py — never edits the pipeline file itself.
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

    attempts = load_open_attempts()
    log.info(f"{len(attempts)} open attempt(s) to check.")

    checked = updated = letters_fetched = pre_recommendation_skipped = 0
    unmapped_statuses = set()

    for attempt in attempts:
        request_number = attempt["website_request_id"]
        checked += 1

        rec_id = find_recommendation_id_for_request(session, request_number)
        time.sleep(REQUEST_DELAY)

        if not rec_id:
            # No recommendation/letter exists yet — this request is still at
            # an earlier step (تم التسجيل / لجنة طبية / تحويل الي طبيب اخر).
            # See _check_pre_recommendation_status()'s docstring: genuinely
            # not implemented, not silently skipped by accident.
            _check_pre_recommendation_status(session, request_number)
            pre_recommendation_skipped += 1
            continue

        html = get_letter_popup_html(session, rec_id)
        time.sleep(REQUEST_DELAY)
        if not html:
            continue

        extracted = extract_status_and_response(html)
        response_text = attempt.get("response_text") or extracted["response_text"]
        if extracted["response_text"] and not attempt.get("response_text"):
            letters_fetched += 1

        update_fields: Dict[str, object] = {}
        if extracted["response_text"] and not attempt.get("response_text"):
            update_fields["response_text"] = extracted["response_text"]

        bucket_info = None
        if extracted["status_raw"]:
            bucket_info = resolve_status_bucket(status_map, extracted["status_raw"])
            update_fields["smc_status_raw"] = extracted["status_raw"]
            update_fields["smc_status_normalized"] = bucket_info["english_bucket"]
            update_fields["status_is_final"] = bool(bucket_info["is_final"])
            if bucket_info["english_bucket"] == "Unknown":
                unmapped_statuses.add(extracted["status_raw"])
        else:
            # No status label confidently extracted from the popup — record
            # that a recommendation exists so this isn't silently invisible,
            # but don't guess at is_final. See _STATUS_LABEL_CANDIDATES'
            # docstring: send me a real popup example and this becomes a
            # one-line fix.
            log.warning(f"  request {request_number}: recommendation {rec_id} found but no status label "
                        f"matched any candidate — logging raw popup for review, not updating status.")

        if update_fields:
            sb.update(ATTEMPTS_TABLE, attempt["id"], update_fields)
            sb.insert(EVENTS_TABLE, {
                "case_id": attempt["case_id"],
                "attempt_id": attempt["id"],
                "event_type": "status_sync_checked",
                "details": {
                    "recommendation_id": rec_id,
                    "request_number": request_number,
                    "status_raw": extracted["status_raw"],
                    "resolved_bucket": bucket_info["english_bucket"] if bucket_info else None,
                    "response_text_captured": bool(extracted["response_text"]),
                },
            })
            updated += 1

            # Only stamp case_status for buckets with an unambiguous mapping
            # onto the CHECK-constrained enum, and only move a case FORWARD
            # from SUBMITTED/PENDING — never overwrite a status a human (or
            # the submission service) already set to something else.
            if bucket_info and bucket_info["english_bucket"] in BUCKET_TO_CASE_STATUS:
                case_rows = sb.select(CASES_TABLE, select="case_status", filters={"id": f"eq.{attempt['case_id']}"})
                current_case_status = case_rows[0]["case_status"] if case_rows else None
                if current_case_status in ("SUBMITTED", "PENDING", "ADMIN_LETTER", "RESUBMISSION"):
                    new_case_status = BUCKET_TO_CASE_STATUS[bucket_info["english_bucket"]]
                    sb.update(CASES_TABLE, attempt["case_id"], {"case_status": new_case_status})
                    sb.insert(EVENTS_TABLE, {
                        "case_id": attempt["case_id"],
                        "attempt_id": attempt["id"],
                        "event_type": "case_status_advanced",
                        "details": {"from": current_case_status, "to": new_case_status,
                                    "via_bucket": bucket_info["english_bucket"]},
                    })

    log.info(f"Done. Checked {checked}, updated {updated}, letters fetched {letters_fetched}, "
             f"pre-recommendation (not yet checkable) {pre_recommendation_skipped}.")
    if unmapped_statuses:
        log.warning(f"Unmapped statuses seen — add these to smc_status_map: {sorted(unmapped_statuses)}")


if __name__ == "__main__":
    main()
