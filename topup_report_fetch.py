#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_report_fetch.py
==========================================================================
For administrative letters of kind PREV_REPORT ("يفاد بقرار اللجنه السابقه" and similar):
SMC asks for the PRINTED REPORT of the patient's previously issued decree. This script
produces that paper automatically and stores it as the evidence for the retry request,
exactly like topup_evidence_fetch.py does for signed invoices.

FLOW (taken from your HAR of the manual extraction):
  1. pick the decree whose report is needed
        a. a decree number typed by hand / read from the letter  (manual_decree_numbers), else
        b. the MOST RECENT decree of the SAME PLAN from the patient's decree lookup
           (decree_topup_lookup_view, match_kind = 'PLAN'; filled by topup_decree_lookup.py,
           which the workflow runs first)
  2. GET  {BASE}/smc/Decrees/DecreeRecommendation/<decree>
        -> 302 -> /smc/Decrees/DecreeRecommendations?DecreesReadyToSend=<decree>-,
           the printable "توصية المجلس الطبي المتخصص" page
  3. print that page to PDF with Chromium (print media, backgrounds ON so the watermark shows),
     one A4 page like the manual print
  4. verify against the page text (decree sequence number + the patient's national id)
  5. store in R2 (topup-reports/<candidate>/<decree>.pdf) and in decree_topup_invoices with
     doc_kind = 'PREV_REPORT'; topup_recompute_evidence() then marks the candidate READY
     (all OK) or NEEDS_REVIEW (a check failed -> a person approves it in the page).

Candidates taken: kind = PREV_REPORT, evidence_status = REPORT_PENDING (set by migration 07 and by
the page's retry / manual-decree buttons). Nothing is ever uploaded back to SMC.

TESTED on this machine: the page parser/verification and the Chromium print, fed with the HTML
captured in your HAR (renders one A4 page with the watermark). NOT tested: a run against live
SMC / Supabase / R2.
"""
from __future__ import annotations
import hashlib, io, logging, os, re, sys, time
from datetime import datetime, timezone
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from bs4 import BeautifulSoup

log = logging.getLogger("topup_report_fetch")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
MAX_REPORTS = int(os.environ.get("MAX_REPORTS") or "40")
TIME_BUDGET_MIN = int(os.environ.get("REPORT_TIME_BUDGET_MIN") or "25")
DELAY = 0.4
CAND, INV, LOOKV = "decree_topup_candidates", "decree_topup_invoices", "decree_topup_lookup_view"
SCALES = (1.0, 0.95, 0.92, 0.9)   # first scale that keeps the print on ONE page (same as the manual print)

_AR = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def ar2en(s: str) -> str:
    return (s or "").translate(_AR)


def base_url() -> str:
    import Unified_Decree_Submission_Pipeline as _p
    return _p.BASE_URL


# --------------------------------------------------------------------------- Supabase REST (service role)
def _rest(method, path, **kw):
    hdr = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
    hdr.update(kw.pop("headers", None) or {})
    r = requests.request(method, f"{SUPABASE_URL}/rest/v1/{path}", headers=hdr, timeout=60, **kw)
    if not r.ok:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
    return r.json() if r.text.strip() else None


def _rpc(name, body):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/{name}", json=body, timeout=60,
                      headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"})
    if not r.ok:
        raise RuntimeError(f"{name} {r.status_code}: {r.text[:200]}")


def _set_candidate(cid, **body):
    _rest("PATCH", CAND, params={"id": f"eq.{cid}"}, json=body, headers={"Prefer": "return=minimal"})


def national_id_of(patient_id) -> str:
    if not patient_id:
        return ""
    rows = _rest("GET", "patients", params={"select": "national_id", "id": f"eq.{patient_id}", "limit": 1})
    return str(rows[0]["national_id"]) if rows and rows[0].get("national_id") else ""


def _r2():
    import boto3
    return boto3.client(
        "s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")


# --------------------------------------------------------------------------- which decree
def pick_decree(c: dict) -> tuple[str | None, str]:
    """Returns (decree_number or None, explanation). Manual numbers win; otherwise the most recent
    decree of the same plan found by the patient lookup."""
    cid = c["id"]
    look = _rest("GET", LOOKV, params={"select": "decree_number,decree_date,match_kind",
                                       "candidate_id": f"eq.{cid}",
                                       "order": "decree_date.desc.nullslast"}) or []
    dates = {str(r["decree_number"]): (r.get("decree_date") or "") for r in look}

    manual = [str(x) for x in (c.get("manual_decree_numbers") or []) if str(x).strip()]
    if manual:
        # several typed numbers: the one with the latest known issue date, else the highest number
        best = sorted(manual, key=lambda d: (dates.get(d, ""), d), reverse=True)[0]
        return best, "رقم قرار مُدخل يدوياً" if len(manual) == 1 else f"أحدث قرار من {len(manual)} أرقام مُدخلة"

    status = c.get("lookup_status")
    if status in ("PENDING", "RUNNING"):
        return None, "WAIT_LOOKUP"
    same_plan = [r for r in look if r.get("match_kind") == "PLAN"]
    if same_plan:
        return str(same_plan[0]["decree_number"]), "أحدث قرار لنفس الخطة"
    if status == "ERROR":
        return None, "فشل فحص قرارات المريض — أعد الفحص أو أدخل رقم القرار يدوياً"
    return None, "لا يوجد بين قرارات المريض قرار لنفس الخطة — أدخل رقم القرار يدوياً"


# --------------------------------------------------------------------------- SMC page
def fetch_report_page(smc, decree: str):
    """GET DecreeRecommendation/<decree> (follows the 302). Returns (final_url, html)."""
    base = base_url()
    url = f"{base}/smc/Decrees/DecreeRecommendation/{decree}"
    hdr = {"Referer": f"{base}/smc/Decrees"}
    for attempt in (1, 2):
        r = smc.s.get(url, headers=hdr, timeout=60, allow_redirects=True)
        if smc._session_expired(r) or ("اسم المستخدم" in r.text and "كلمة السر" in r.text):
            log.warning("session expired - re-login")
            if not smc.login():
                raise RuntimeError("SMC re-login failed")
            continue
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} for decree {decree}")
        return r.url, r.text
    raise RuntimeError("SMC session could not be restored")


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style"]):
        s.decompose()
    return ar2en(re.sub(r"\s+", " ", soup.get_text(" ")))


def check_page(html: str, decree: str, nid: str) -> dict:
    """The printable page prints «رقم <sequence> لسنة <year>» and the national id. Checked on the
    page's own text (logical order), so it does not depend on how the PDF stores RTL digits."""
    t = page_text(html)
    year, seq = decree[:4], decree[4:]
    return {
        "is_recommendation_page": "توصية" in t,
        "decree_seq_in_page": bool(seq) and seq in t,
        "decree_year_in_page": bool(year) and year in t,
        "nid_in_page": (nid in t) if nid else None,
    }


def checks_ok(v: dict) -> bool:
    return bool(v["is_recommendation_page"] and v["decree_seq_in_page"] and v["decree_year_in_page"]
                and v["nid_in_page"] is not False)


# --------------------------------------------------------------------------- print to PDF
def _page_count(pdf: bytes) -> int:
    import pymupdf
    d = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        return len(d)
    finally:
        d.close()


def render_pdf(url: str, cookies: list[dict], route_handler=None) -> tuple[bytes, float]:
    """Print the page like the manual print: print media, backgrounds ON (the watermark), CSS page
    size (A4). Tries a slightly smaller scale if the print would spill a line onto page 2."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        try:
            ctx = br.new_context()
            if cookies:
                ctx.add_cookies(cookies)
            if route_handler:
                ctx.route("**/*", route_handler)
            page = ctx.new_page()
            page.goto(url, wait_until="load", timeout=90_000)
            try:
                page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                pass
            page.emulate_media(media="print")
            pdf, used = b"", 1.0
            for s in SCALES:
                pdf = page.pdf(format="A4", print_background=True, prefer_css_page_size=True, scale=s)
                used = s
                if _page_count(pdf) == 1:
                    break
            return pdf, used
        finally:
            br.close()


def cookies_from(smc) -> list[dict]:
    host = urlparse(base_url()).hostname
    return [{"name": c.name, "value": c.value, "domain": host, "path": "/"} for c in smc.s.cookies]


# --------------------------------------------------------------------------- one candidate
def process(c: dict, smc, s3, bucket: str):
    cid = c["id"]
    decree, why = pick_decree(c)
    if decree is None:
        if why == "WAIT_LOOKUP":
            log.info(f"candidate {cid}: patient lookup still running - left for the next run")
            return
        _set_candidate(cid, evidence_status="NO_PREVIOUS_DECREE", evidence_message=why)
        return

    nid = national_id_of(c.get("patient_id"))
    final_url, html = fetch_report_page(smc, decree)
    v = {"decree": decree, "how_chosen": why, "page": check_page(html, decree, nid)}
    time.sleep(DELAY)
    if not v["page"]["is_recommendation_page"]:
        _set_candidate(cid, evidence_status="FAILED",
                       evidence_message=f"صفحة التوصية للقرار {decree} لم تُفتح (أُعيد توجيه الطلب أو لا توجد توصية)")
        return

    pdf, scale = render_pdf(final_url, cookies_from(smc))
    pages = _page_count(pdf)
    v["render"] = {"scale": scale, "pages": pages, "bytes": len(pdf)}
    ok = checks_ok(v["page"]) and pages == 1 and len(pdf) > 20_000

    notes = [why]
    if not v["page"]["decree_seq_in_page"]:
        notes.append("رقم القرار لم يظهر في الصفحة — راجع الملف")
    if v["page"]["nid_in_page"] is False:
        notes.append("الرقم القومي للمريض لم يظهر في الصفحة — راجع الملف")
    if pages != 1:
        notes.append(f"الطباعة جاءت في {pages} صفحات — راجع الملف")

    key = f"topup-reports/{cid}/{decree}.pdf"
    s3.put_object(Bucket=bucket, Key=key, Body=pdf, ContentType="application/pdf")
    row = {"candidate_id": cid, "decree_number": decree, "receipt_id": f"REPORT-{decree}",
           "doc_kind": "PREV_REPORT", "signed_source": "smc_attachment", "r2_key": key,
           "bytes": len(pdf), "page_count": pages, "sha256": hashlib.sha256(pdf).hexdigest(),
           "status": "OK" if ok else "REVIEW", "verification": v,
           "message": None if ok else " | ".join(dict.fromkeys(notes)),
           "fetched_at": datetime.now(timezone.utc).isoformat()}
    _rest("POST", INV, params={"on_conflict": "candidate_id,receipt_id"}, json=[row],
          headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    # only ONE report belongs to a request: drop reports of other decrees from an earlier choice
    _rest("DELETE", INV, params={"candidate_id": f"eq.{cid}", "doc_kind": "eq.PREV_REPORT",
                                 "receipt_id": f"neq.REPORT-{decree}"})
    _set_candidate(cid, evidence_message=None)
    _rpc("topup_recompute_evidence", {"p_id": cid})
    log.info(f"candidate {cid}: report of decree {decree} stored ({'OK' if ok else 'REVIEW'}), {pages} page(s)")


def main():
    u = p = ""
    for un, pw in (("SMC_USERNAME_3", "SMC_PASSWORD_3"), ("SMC_USERNAME_2", "SMC_PASSWORD_2"),
                   ("SMC_USERNAME", "SMC_PASSWORD")):
        if os.environ.get(un) and os.environ.get(pw):
            u, p = os.environ[un], os.environ[pw]
            break
    if not u:
        log.error("No SMC credentials set."); sys.exit(1)

    rows = _rest("GET", CAND, params={
        "select": "id,patient_id,manual_decree_numbers,lookup_status",
        "kind": "eq.PREV_REPORT", "evidence_status": "eq.REPORT_PENDING",
        "status": "not.in.(SUBMITTED,DISMISSED,RESOLVED)", "order": "id.asc", "limit": MAX_REPORTS}) or []
    log.info(f"{len(rows)} previous-decree report(s) to fetch.")
    if not rows:
        return

    import Unified_Decree_Submission_Pipeline as _p
    from Unified_Decree_Submission_Pipeline import SMCSession
    _p.USERNAME, _p.PASSWORD = u, p
    smc = SMCSession()
    if not smc.login():
        log.error("SMC login failed."); sys.exit(1)

    s3, bucket = _r2(), os.environ["R2_BUCKET_NAME"]
    deadline = time.time() + TIME_BUDGET_MIN * 60
    for c in rows:
        if time.time() > deadline:
            log.info("time budget reached - the rest runs next time")
            break
        try:
            process(c, smc, s3, bucket)
        except Exception as e:
            log.exception(f"candidate {c['id']} failed")
            try:
                _set_candidate(c["id"], evidence_status="FAILED",
                               evidence_message=f"{type(e).__name__}: {str(e)[:200]}")
            except Exception:
                pass


if __name__ == "__main__":
    main()
