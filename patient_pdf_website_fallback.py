#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patient_pdf_website_fallback.py
==========================================================================
FALLBACK #1 for a missing patient document PDF.

Ported from your standalone script
"extract_only_recent_submitted_pdf_file_for_each_patient_-_Copy.py" into a
single callable function, so the main pipeline can call it in-process
instead of running it separately. The logic (search requests -> filter to
Gustave-facility rows -> try each until one has a downloadable PDF -> save
it) is unchanged from that script.

KEY DIFFERENCE FROM THE STANDALONE SCRIPT: this module does NOT log in on
its own. It reuses the SMCSession object the main pipeline already logged
in with for MDT creation - same site (smc.smcegy.com), same account, so
there is no reason to open a second session. Pass in the pipeline's
`session` object (an instance of Unified_Decree_Submission_Pipeline.SMCSession)
and this module drives it with `session.s` (the underlying requests.Session)
exactly the way the standalone script drove its own SMCSession.session.

If session.s ever comes back logged-out mid-way (site redirected to the
login page), call session.login() again before retrying - this module
does not attempt that itself, to keep it a thin, predictable fallback
step rather than a second copy of retry/reconnect logic (the pipeline's
own call_with_reconnect() wrapper already covers that when this function
is invoked through it - see the wiring in the main pipeline file).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Dict, List, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

log = logging.getLogger("patient_pdf_website_fallback")

# Only requests whose sending/treatment facility contains this keyword are
# eligible - same filter as the standalone script.
FACILITY_KEYWORD = "جوستاف"

SEARCH_FROM_DATE = "01/01/2000"
REQUEST_TIMEOUT = 30
RETRIES = 2
REQUEST_SLEEP = 0.3


def _parse_requests_table(html: str):
    """Parses one page of GetRequests's response: a table of request rows
    plus pager info. Ported verbatim (same table id, same column order,
    same pager format) from the standalone script's parse_requests_table()."""
    soup = BeautifulSoup(html, "html.parser")
    rows_data = []

    table = soup.find("table", id="requestTable")
    if table:
        trs = table.find_all("tr")[1:]  # skip header row
        for tr in trs:
            tds = tr.find_all("td")
            if len(tds) < 7:
                continue

            request_id_link = tds[0].find("a")
            request_id = (request_id_link.get_text(strip=True)
                          if request_id_link else tds[0].get_text(strip=True))

            sending_site = tds[4].get_text(strip=True)
            treatment_site = tds[5].get_text(strip=True)

            rows_data.append({
                "request_id": request_id,
                "sending_site": sending_site,
                "treatment_site": treatment_site,
            })

    total_pages = 1
    pager = soup.find("div", id="myPager")
    if pager:
        m = re.search(r"Page\s+\d+\s+of\s+(\d+)", pager.get_text())
        if m:
            total_pages = int(m.group(1))

    return rows_data, total_pages


def _search_requests_by_national_id(sess, base_url: str, national_id: str) -> List[Dict]:
    all_rows = []
    page = 1
    total_pages = 1

    while True:
        params = {
            "nationalId": national_id,
            "fromDate": SEARCH_FROM_DATE,
            "toDate": time.strftime("%m/%d/%Y"),
            "cancerCase": "False",
            "page": page,
        }
        url = f"{base_url}/smc/Requests/GetRequests"

        html = None
        for attempt in range(RETRIES):
            try:
                resp = sess.post(url, params=params, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    html = resp.text
                    break
                log.warning(f"  GetRequests page {page} returned {resp.status_code} "
                            f"(attempt {attempt + 1})")
            except Exception as e:
                log.error(f"  Error calling GetRequests page {page}: {e}")
            time.sleep(1)

        if html is None:
            break

        rows, found_total_pages = _parse_requests_table(html)
        if found_total_pages:
            total_pages = found_total_pages
        if not rows:
            break

        all_rows.extend(rows)
        if page >= total_pages:
            break
        page += 1
        time.sleep(REQUEST_SLEEP)

    return all_rows


def _filter_gustave_rows(rows: List[Dict]) -> List[Dict]:
    return [
        r for r in rows
        if FACILITY_KEYWORD in r.get("sending_site", "") or FACILITY_KEYWORD in r.get("treatment_site", "")
    ]


def _parse_details_page(html: str, request_id: str) -> Dict:
    soup = BeautifulSoup(html, "html.parser")

    pdf_url = None
    pdf_filename = None
    for a in soup.find_all("a", onclick=True):
        onclick = a["onclick"]
        if "REQUESTCONTENTNAME" not in onclick:
            continue
        m = re.search(
            r"openFile\(\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'REQUESTCONTENTNAME'\s*\)",
            onclick,
        )
        if m:
            ssn_m, reqid_m, fname_m, reqdate_m = m.groups()
            pdf_filename = fname_m
            pdf_url = (
                "{base}/smc/Requests/GetFiles"
                "?SSN={ssn}&ReqID={reqid}&fn={fn}&ReqDate={rd}&ReqContentName=REQUESTCONTENTNAME"
            ).format(base="{base}", ssn=ssn_m, reqid=reqid_m, fn=fname_m, rd=quote(reqdate_m))
            break

    return {"request_id": request_id, "pdf_url": pdf_url, "pdf_filename": pdf_filename}


def _get_request_details(sess, base_url: str, request_id: str) -> Optional[Dict]:
    url = f"{base_url}/smc/Requests/Details/{request_id}"
    for attempt in range(RETRIES):
        try:
            resp = sess.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                details = _parse_details_page(resp.text, request_id)
                if details.get("pdf_url"):
                    details["pdf_url"] = details["pdf_url"].format(base=base_url)
                return details
            log.warning(f"  Details page returned {resp.status_code} for request {request_id} "
                        f"(attempt {attempt + 1})")
        except Exception as e:
            log.error(f"  Error fetching details for request {request_id}: {e}")
        time.sleep(1)
    return None


def _download_pdf(sess, pdf_url: str, national_id: str, request_id: str, output_dir: str) -> Optional[str]:
    if not pdf_url:
        return None

    clean_id = re.sub(r"[^0-9]", "", national_id) or f"ID_{request_id}"
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"{clean_id}.pdf")

    for attempt in range(RETRIES):
        try:
            resp = sess.get(pdf_url, timeout=60)
            if resp.status_code == 200 and resp.content:
                with open(path, "wb") as f:
                    f.write(resp.content)
                return path
            log.warning(f"  PDF download returned {resp.status_code} for request {request_id} "
                        f"(attempt {attempt + 1})")
        except Exception as e:
            log.error(f"  Error downloading PDF for request {request_id}: {e}")
        time.sleep(1)
    return None


def download_patient_pdf_from_website(pipeline_session, base_url: str, national_id: str,
                                       output_dir: str) -> Optional[str]:
    """
    FALLBACK #1 entry point.

    Args:
        pipeline_session: the main pipeline's already-logged-in SMCSession
                           instance (Unified_Decree_Submission_Pipeline.SMCSession).
                           Its `.s` attribute (a requests.Session) is used
                           directly - no separate login happens here.
        base_url:          the SMC site base URL (same BASE_URL the pipeline
                            uses, e.g. "https://smc.smcegy.com").
        national_id:       patient's national ID / SSN, as used elsewhere
                            in the pipeline.
        output_dir:        folder to save the downloaded PDF into. Saved
                            as "<national_id>.pdf" inside it.

    Returns:
        Full path to the downloaded PDF on success, or None if no
        Gustave-facility request for this patient had a working PDF
        (or the patient has no requests on the site at all). A None
        return is not itself an error - it means "try the next
        fallback" (the DMS archive fallback) or give up on this row.
    """
    log.info(f"  [website fallback] Searching SMC site requests for patient {national_id} …")
    all_rows = _search_requests_by_national_id(pipeline_session.s, base_url, national_id)
    gustave_rows = _filter_gustave_rows(all_rows)
    log.info(f"  [website fallback] {len(all_rows)} total request(s), "
             f"{len(gustave_rows)} matching facility keyword '{FACILITY_KEYWORD}'")

    if not gustave_rows:
        log.info("  [website fallback] No matching requests found for this patient.")
        return None

    for row in gustave_rows:
        request_id = row["request_id"]
        details = _get_request_details(pipeline_session.s, base_url, request_id)
        if not details:
            continue
        if not details.get("pdf_url"):
            continue
        log.info(f"  [website fallback] Found PDF link in request {request_id}, downloading …")
        saved_path = _download_pdf(pipeline_session.s, details["pdf_url"], national_id,
                                    request_id, output_dir)
        if saved_path:
            log.info(f"  [website fallback] ✅ Downloaded to {saved_path}")
            return saved_path
        time.sleep(REQUEST_SLEEP)

    log.info("  [website fallback] All matching requests checked - none had a working PDF.")
    return None
