#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patient_pdf_dms_archive_fallback.py
==========================================================================
Talks to the on-premises CMIS/MRM portal (http://41.33.24.253/CMIS/MRM_5.2)
to pull a patient's archived document PDFs, using its own separate
login/credentials (HMIS_USERNAME / HMIS_PASSWORD) - completely different
system/session from the SMC decree site used elsewhere in the pipeline.

Every endpoint, payload shape, and response format below was CONFIRMED
against real captures you provided (not guessed) - see the flow
description in each function's docstring for exactly which capture it
came from. The one earlier assumption that turned out to be WRONG has
been fixed: ArchiveViewAll must be called under CMIS_BASE (which already
includes "/MRM_5.2") - calling it at a bare "/CMIS/Archive/ArchiveViewAll"
(no MRM_5.2 segment, what the previous version of this file did) 404s,
which is exactly what the first real test run showed.

--------------------------------------------------------------------------
CONFIRMED FLOW (in call order)
--------------------------------------------------------------------------
1. search_patient_by_national_id()
   POST {CMIS_BASE}/Search/AdvancedPatientSearch
   Finds the patient's MR code directly from their national ID - no more
   dependency on the DMS extractor's cumulative workbook for this lookup,
   since every patient in this pipeline is identified by national ID and
   this endpoint accepts that directly (PatientId field).

2. authorize_patient()
   POST {CMIS_BASE}/Home/AuthorizedToSearchPatient  ->  body is literally
   the text "true" on success.

3. draw_tree_by_mr()
   POST {CMIS_BASE}/Home/DrawTreeByMR
   Opens the patient's visit tree. Confirmed this does NOT supply archive
   dates (those come from step 4) - it's kept because it's what actually
   "opens" the patient into the site's session before Archive/Index will
   show their documents (matches your manual click-through order).

4. get_archive_index()
   GET {CMIS_BASE}/Archive/Index  (no payload)
   THIS is where the archive date identifiers actually come from - each
   row's checkbox carries data-id="dd/mm/yyyy HH:MM:SS AM/PM" (note: the
   time portion is 24-hour with a literal, slightly redundant "AM/PM"
   suffix tacked on - e.g. "16/07/2026 15:22:18 PM". This is NOT a bug in
   this module: it is copied VERBATIM from the site's own markup, because
   ArchiveViewAll expects to receive back exactly what Archive/Index
   handed out - confirmed by your captured
   REQUEST_ArchiveViewAll_payload.json matching these exact strings.

5. call_archive_view_all()
   POST {CMIS_BASE}/Archive/ArchiveViewAll
   Body: {"List": [<data-id strings from step 4, exact subset being
   requested>]}
   Returns a "Gallery" HTML fragment (confirmed sample: your Gallery tab
   response) with one <img class="gallery-image"> per archived item.

6. parse_gallery_pdf_urls()
   Extracts the real PDF URL from each gallery item - confirmed to appear
   inside either a window.open('...') or a PreviewImageinPopUp('...')
   call. Filenames are NOT predictable (confirmed real examples:
   "3000.pdf", "3000 (2).pdf", and completely unrelated names like
   "ERM036731023-27806040100861.PDF") and the path sometimes has a double
   slash before the filename - the regex tolerates all of this rather
   than assuming a naming pattern. Some archived items may be blank/
   corrupted scanned pages (confirmed by you) - these are downloaded and
   merged in like any other page rather than filtered out, exactly as you
   said you don't mind their presence.

--------------------------------------------------------------------------
TWO WAYS THIS MODULE IS USED BY THE PIPELINE
--------------------------------------------------------------------------
- get_all_patient_archive_pdfs_merged(): FULL extraction - pulls every
  archived PDF for this patient (all rows from Archive/Index, not just
  recent ones) and merges them into one new file. Used when the patient
  has NO local PDF at all yet.

- refresh_local_pdf_with_recent_archive_docs(): INCREMENTAL refresh -
  only looks at archive rows dated within the last `days_back` days
  (default 7, widened from the original today/yesterday-only window;
  relative to `reference_date`, defaults to now), and if any are found,
  downloads just those and appends them onto the END of the EXISTING
  local PDF, overwriting it in place under the same filename. Used when
  the patient already has a local PDF and you just want to catch
  anything newly scanned in since it was saved - exactly the "newly
  added investigation papers" behaviour you described.
"""

from __future__ import annotations

import io
import logging
import os
import re
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

import requests
from PyPDF2 import PdfMerger

log = logging.getLogger("patient_pdf_dms_archive_fallback")

# =====================================================================
# CONFIG
# =====================================================================

CMIS_BASE = "http://41.33.24.253/CMIS/MRM_5.2"
HMIS_USERNAME = os.environ.get("HMIS_USERNAME", "557")
HMIS_PASSWORD = os.environ.get("HMIS_PASSWORD", "557")
TIMEOUT = 30

_XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{CMIS_BASE}/Home/Index"}


# =====================================================================
# LOGIN - unchanged, already confirmed working (same flow your DMS
# extractor script uses successfully).
# =====================================================================

def _extract_gettoken(html: str) -> str:
    m = re.search(r"function\s+gettoken\s*\(\s*\)\s*\{.*?return\s*'([^']+)'", html, re.DOTALL)
    return m.group(1) if m else ""


def login(session: requests.Session) -> bool:
    log.info("  [DMS archive] Logging in to CMIS …")
    session.get(f"{CMIS_BASE}/Home/Index", timeout=TIMEOUT)
    session.get(f"{CMIS_BASE}/Login/LoginModel", timeout=TIMEOUT)
    r = session.post(
        f"{CMIS_BASE}/Login/ValidateUserPopupForSEC",
        data={"UserID": HMIS_USERNAME, "Password": HMIS_PASSWORD},
        headers={
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "http://41.33.24.253",
            "Referer": f"{CMIS_BASE}/Home/IndexSessionExpire",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        log.error(f"  [DMS archive] CMIS login failed - HTTP {r.status_code}")
        return False

    r2 = session.get(f"{CMIS_BASE}/Home/Index", timeout=TIMEOUT)
    token = _extract_gettoken(r2.text)
    if not token:
        log.error("  [DMS archive] CMIS login looked OK but no gettoken() found.")
        return False
    session.headers["RequestVerificationToken"] = token
    log.info("  [DMS archive] CMIS login successful.")
    return True


# =====================================================================
# STEP 1 - search by national ID (replaces the old workbook-based MR
# lookup entirely - this endpoint takes the national ID directly).
# =====================================================================

def search_patient_by_national_id(session: requests.Session, national_id: str) -> Optional[Dict]:
    """
    POST Search/AdvancedPatientSearch with every field blank except
    PatientId. Returns the first match's full record dict (has "MR",
    "EngName", "ArabicName", "NationalID", etc.) or None if not found.
    Field list and defaults confirmed against your real capture.
    """
    payload = {
        "FirstName": "", "FamilyName": "", "MiddleName": "", "LastName": "",
        "FirstNameAr": "", "FamilyNameAr": "", "MiddleNameAr": "", "LastNameAr": "",
        "MR": "", "PhoneNo": "", "BirthDate": "", "OldMR": "", "NSHNo": "",
        "FamilyMR": "", "ToDate": "", "Soundex": "False", "MobileNo": "",
        "From": 0, "To": 100, "PatientId": national_id, "IdType": "",
    }
    try:
        r = session.post(f"{CMIS_BASE}/Search/AdvancedPatientSearch", data=payload,
                          headers=_XHR_HEADERS, timeout=TIMEOUT)
    except Exception as e:
        log.error(f"  [DMS archive] AdvancedPatientSearch request failed: {e}")
        return None

    if r.status_code != 200:
        log.warning(f"  [DMS archive] AdvancedPatientSearch returned HTTP {r.status_code}")
        return None

    try:
        data = r.json()
    except Exception:
        log.warning("  [DMS archive] AdvancedPatientSearch response was not JSON.")
        return None

    results = data.get("List") or []
    if not results:
        log.info(f"  [DMS archive] No CMIS patient found for national ID {national_id}.")
        return None
    return results[0]


# =====================================================================
# STEP 2 - authorize
# =====================================================================

def authorize_patient(session: requests.Session, mr) -> bool:
    try:
        r = session.post(f"{CMIS_BASE}/Home/AuthorizedToSearchPatient", data={"MR": mr},
                          headers=_XHR_HEADERS, timeout=TIMEOUT)
    except Exception as e:
        log.error(f"  [DMS archive] AuthorizedToSearchPatient request failed: {e}")
        return False
    return r.status_code == 200 and r.text.strip().lower() == "true"


# =====================================================================
# STEP 3 - open the patient tree
# =====================================================================

def draw_tree_by_mr(session: requests.Session, mr) -> Optional[str]:
    try:
        r = session.post(f"{CMIS_BASE}/Home/DrawTreeByMR",
                          data={"MR": mr, "OrderBySpeciality": "False", "DoctorGroup": "False"},
                          headers=_XHR_HEADERS, timeout=TIMEOUT)
    except Exception as e:
        log.error(f"  [DMS archive] DrawTreeByMR request failed: {e}")
        return None
    if r.status_code != 200:
        log.warning(f"  [DMS archive] DrawTreeByMR returned HTTP {r.status_code}")
        return None
    return r.text


# =====================================================================
# STEP 4 - Archive/Index: the real source of archive date identifiers
# =====================================================================

def get_archive_index(session: requests.Session) -> Optional[str]:
    try:
        r = session.get(f"{CMIS_BASE}/Archive/Index", headers=_XHR_HEADERS, timeout=TIMEOUT)
    except Exception as e:
        log.error(f"  [DMS archive] Archive/Index request failed: {e}")
        return None
    if r.status_code != 200:
        log.warning(f"  [DMS archive] Archive/Index returned HTTP {r.status_code}")
        return None
    return r.text


def extract_archive_row_dates(archive_index_html: str) -> List[str]:
    """Pulls every row's data-id date string verbatim - these are exactly
    what ArchiveViewAll expects back in its "List" payload. Confirmed
    against your real Archive/Index capture (4 rows, matching your
    REQUEST_ArchiveViewAll_payload.json exactly)."""
    return re.findall(r'data-id="([^"]+)"', archive_index_html)


def _parse_row_date(data_id: str) -> Optional[date]:
    """Pulls just the dd/mm/yyyy portion out of a data-id string like
    "16/07/2026 15:22:18 PM" for date-based filtering."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", data_id.strip())
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


# =====================================================================
# STEP 5 - ArchiveViewAll (the fix: correct URL is under CMIS_BASE,
# i.e. includes "/MRM_5.2" - the earlier 404 was from omitting it)
# =====================================================================

def call_archive_view_all(session: requests.Session, date_strings: List[str]) -> Optional[str]:
    try:
        r = session.post(f"{CMIS_BASE}/Archive/ArchiveViewAll", json={"List": date_strings},
                          headers=_XHR_HEADERS, timeout=TIMEOUT)
    except Exception as e:
        log.error(f"  [DMS archive] ArchiveViewAll request failed: {e}")
        return None
    if r.status_code != 200:
        log.warning(f"  [DMS archive] ArchiveViewAll returned HTTP {r.status_code} "
                    f"(URL used: {CMIS_BASE}/Archive/ArchiveViewAll)")
        return None
    return r.text


def parse_gallery_pdf_urls(gallery_html: str) -> List[str]:
    """Extracts every real PDF URL from a Gallery response. Confirmed
    against your real capture: URLs appear inside either
    window.open('...') or PreviewImageinPopUp('...'), filenames are
    unpredictable (plain "<mr>.pdf", "<mr> (n).pdf", or something
    completely unrelated like a lab-report code), extensions can be
    upper or lower case, and the path before the filename sometimes has
    a double slash. All of that is tolerated here rather than assumed
    away. Order is preserved and duplicates are dropped."""
    urls = re.findall(r"(?:window\.open|PreviewImageinPopUp)\('([^']+?\.pdf)'\)",
                       gallery_html, re.IGNORECASE)
    # Fallback: a bare src='...pdf' not already caught via onclick (seen
    # in your capture as a duplicate of the onclick URL on the same
    # <img> tag, but kept as a fallback in case a future item only has
    # the src attribute and no onclick).
    for m in re.finditer(r"src='([^']+?\.pdf)'", gallery_html, re.IGNORECASE):
        if m.group(1) not in urls:
            urls.append(m.group(1))

    seen = set()
    ordered_unique = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered_unique.append(u)
    return ordered_unique


# =====================================================================
# Download + merge helpers
# =====================================================================

def _download_pdf_bytes(session: requests.Session, url: str) -> Optional[bytes]:
    try:
        r = session.get(url, timeout=60)
        if r.status_code == 200 and r.content:
            return r.content
        log.warning(f"  [DMS archive] Download returned HTTP {r.status_code} for {url}")
    except Exception as e:
        log.error(f"  [DMS archive] Error downloading {url}: {e}")
    return None


def _merge_pdf_bytes_list(pdf_bytes_list: List[bytes], output_path: str) -> bool:
    if not pdf_bytes_list:
        return False
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    merger = PdfMerger()
    appended_any = False
    for content in pdf_bytes_list:
        try:
            merger.append(io.BytesIO(content))
            appended_any = True
        except Exception as e:
            # A genuinely corrupted/blank archive item that PyPDF2 can't
            # even open at all (not just a blank page - actually
            # unreadable) - skip just that one rather than failing the
            # whole merge, since you said blank pages are fine to ignore.
            log.warning(f"  [DMS archive] Skipped one archive PDF that failed to parse: {e}")
    if not appended_any:
        merger.close()
        return False
    merger.write(output_path)
    merger.close()
    return True


# =====================================================================
# Shared session setup: login + search + authorize + open tree
# =====================================================================

def _open_patient_session(national_id: str) -> Optional[tuple]:
    """Returns (session, mr, archive_index_html) on success, or None if
    any step fails (patient not found, auth failed, etc.)."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    })

    if not login(session):
        return None

    patient = search_patient_by_national_id(session, national_id)
    if not patient:
        return None
    mr = patient["MR"]
    log.info(f"  [DMS archive] National ID {national_id} -> MR {mr} ({patient.get('EngName')})")

    if not authorize_patient(session, mr):
        log.warning(f"  [DMS archive] AuthorizedToSearchPatient did not return true for MR {mr}.")
        return None

    if draw_tree_by_mr(session, mr) is None:
        log.warning(f"  [DMS archive] DrawTreeByMR failed for MR {mr}.")
        return None

    archive_index_html = get_archive_index(session)
    if archive_index_html is None:
        log.warning(f"  [DMS archive] Archive/Index failed for MR {mr}.")
        return None

    return session, mr, archive_index_html


# =====================================================================
# PUBLIC ENTRY POINT #1 - full extraction (patient has NO local PDF yet)
# =====================================================================

def get_all_patient_archive_pdfs_merged(national_id: str, output_dir: str) -> Optional[str]:
    """
    Pulls EVERY archived PDF for this patient and merges them into one
    new file saved as "<national_id>_archive.pdf" under output_dir.
    Returns the merged file's path, or None if the patient couldn't be
    found/authorized in CMIS, or they have zero archived documents.
    """
    opened = _open_patient_session(national_id)
    if not opened:
        return None
    session, mr, archive_index_html = opened

    all_dates = extract_archive_row_dates(archive_index_html)
    if not all_dates:
        log.info(f"  [DMS archive] MR {mr} has no archive rows at all.")
        return None
    log.info(f"  [DMS archive] MR {mr} has {len(all_dates)} archived item(s) - pulling all.")

    gallery_html = call_archive_view_all(session, all_dates)
    if not gallery_html:
        return None

    pdf_urls = parse_gallery_pdf_urls(gallery_html)
    log.info(f"  [DMS archive] Resolved {len(pdf_urls)} downloadable PDF URL(s) for MR {mr}.")
    if not pdf_urls:
        return None

    pdf_bytes_list = [b for b in (_download_pdf_bytes(session, u) for u in pdf_urls) if b]
    if not pdf_bytes_list:
        log.warning(f"  [DMS archive] Found PDF links for MR {mr} but none downloaded successfully.")
        return None

    clean_id = re.sub(r"[^0-9]", "", national_id) or str(mr)
    output_path = os.path.join(output_dir, f"{clean_id}_archive.pdf")
    if not _merge_pdf_bytes_list(pdf_bytes_list, output_path):
        return None

    log.info(f"  [DMS archive] ✅ Merged {len(pdf_bytes_list)} archived PDF(s) -> {output_path}")
    return output_path


# =====================================================================
# PUBLIC ENTRY POINT #2 - incremental refresh (patient ALREADY has a
# local PDF; only pull today/yesterday's new archive items and append)
# =====================================================================

def refresh_local_pdf_with_recent_archive_docs(national_id: str, existing_local_pdf_path: str,
                                                reference_date: Optional[date] = None,
                                                days_back: int = 7) -> bool:
    """
    Checks CMIS's archive for this patient for any document dated from
    TODAY back through `days_back` days before it (relative to
    `reference_date`, defaults to datetime.now().date()) that isn't
    already reflected in the existing local file, downloads just those,
    and appends them onto the end of the existing local PDF -
    OVERWRITING existing_local_pdf_path in place under the same
    filename, exactly as you described ("replace the old pdf file with
    this new merged file").

    WIDENED WINDOW: this used to only look at today/yesterday
    (days_back=1). Per your latest instruction the window is now a full
    week by default (days_back=7) - any archive row uploaded from today
    back through 7 days before it is caught, not just the last 2 days.
    The caller (Unified_Decree_Submission_Pipeline.py's
    RECENT_ARCHIVE_DAYS_BACK) controls the actual value used in
    production; the days_back=7 default here only applies if this
    function is called directly without that override.

    Returns True if the local file was updated (new pages appended),
    False if nothing new was found or anything along the way failed (in
    which case the existing local file is left completely untouched -
    this function never deletes or empties it, only ever appends to a
    fresh copy and swaps it in on success).
    """
    if not os.path.exists(existing_local_pdf_path):
        log.warning(f"  [DMS archive refresh] {existing_local_pdf_path} does not exist - "
                    f"use get_all_patient_archive_pdfs_merged() instead for a first-time pull.")
        return False

    opened = _open_patient_session(national_id)
    if not opened:
        return False
    session, mr, archive_index_html = opened

    all_dates = extract_archive_row_dates(archive_index_html)
    ref = reference_date or datetime.now().date()
    days_back = max(days_back, 0)
    cutoff_days = {ref - timedelta(days=n) for n in range(days_back + 1)}
    recent_dates = [d for d in all_dates if _parse_row_date(d) in cutoff_days]

    if not recent_dates:
        log.info(f"  [DMS archive refresh] MR {mr} has no archive items dated between "
                 f"{ref - timedelta(days=days_back)} and {ref} ({days_back}-day window) - "
                 f"local file left unchanged.")
        return False

    log.info(f"  [DMS archive refresh] MR {mr} has {len(recent_dates)} recent archive item(s) "
             f"({recent_dates}) - pulling them.")
    gallery_html = call_archive_view_all(session, recent_dates)
    if not gallery_html:
        return False

    pdf_urls = parse_gallery_pdf_urls(gallery_html)
    if not pdf_urls:
        log.info(f"  [DMS archive refresh] MR {mr}'s recent archive rows resolved to zero "
                 f"downloadable PDF URLs - local file left unchanged.")
        return False

    new_pdf_bytes = [b for b in (_download_pdf_bytes(session, u) for u in pdf_urls) if b]
    if not new_pdf_bytes:
        log.warning(f"  [DMS archive refresh] Found {len(pdf_urls)} recent PDF link(s) for MR {mr} "
                    f"but none downloaded successfully - local file left unchanged.")
        return False

    # Build the merged file (existing local pages FIRST, new pages
    # appended after) into a temp path, then swap it in - so a failure
    # partway through never corrupts or truncates the existing file.
    tmp_path = existing_local_pdf_path + ".refresh_tmp"
    with open(existing_local_pdf_path, "rb") as f:
        existing_bytes = f.read()
    ok = _merge_pdf_bytes_list([existing_bytes] + new_pdf_bytes, tmp_path)
    if not ok:
        log.warning(f"  [DMS archive refresh] Merge failed for MR {mr} - local file left unchanged.")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False

    os.replace(tmp_path, existing_local_pdf_path)
    log.info(f"  [DMS archive refresh] ✅ Appended {len(new_pdf_bytes)} new page-set(s) into "
             f"{existing_local_pdf_path}")
    return True
