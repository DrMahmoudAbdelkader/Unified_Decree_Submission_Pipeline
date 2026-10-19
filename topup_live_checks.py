#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_live_checks.py
==========================================================================
Two LIVE checks for the administrative-letters (top-up) module. Run as a step of topup-evidence-fetch.yml
(for the entries of that runner) and nightly by topup-live-checks.yml (every open entry).

CHECK 1 - MANUAL / OUTSIDE-THE-APP REQUESTS   (every kind: TOPUP, PREV_INCLUSIVE, PREV_REPORT, ID_CARD)
  Somebody may already have filed a request for this patient by hand on the SMC website, outside this app's workflow.
  The patient's requests are read live from SMC (Reports/SendRequestStatusJson filtered by the national id, newest first,
  in 15-day windows going back to the date of the declined request) and walked from the newest one:
      * status «قرار نهائي» (Approved) or «خطاب ادارى» (Admin_Letter)  -> STOP: nothing older can still be pending
      * cancelled statuses                                              -> ignored
      * request number already known to the app (decree_request_attempts.website_request_id) -> ignored
      * anything else (تم التسجيل / لجنة طبية / محول إلى طبيب آخر / توصية ... )     -> a PENDING MANUAL REQUEST
  Each one is stored in decree_topup_manual_requests with its date, live status and the plan text printed on the request
  page (whatever it says: a manual request does not use the app's standard plans). While one exists the entry is NOT a
  work item (the page moves it to «سبق التعامل معها») and topup_gate refuses to send a new request for it.

CHECK 2 - NEW ID CARD IN THE DMS ARCHIVE   (kind ID_CARD only)
  Opens the patient in CMIS like the submission workflow does and looks for archive rows dated ON/AFTER the day the letter
  arrived (and not rejected before). Found  -> the new paper(s) are merged into ONE pdf, stored in R2 and
  decree_topup_invoices (doc_kind ID_CARD_NEW) for the user to look at, and the entry is marked READY through
  topup_mark_id_fixed (so it shows in «تم التصحيح - جاهز للطلب»). Not found -> idcard_state = WAITING_NEW (tab «بانتظار بطاقة
  سارية جديدة»); it is checked again every 24 h. If the user finds the paper is NOT a valid card he presses «البطاقة غير سارية»
  (topup_idcard_reject, migration 15): those archive rows are remembered as rejected and the entry goes back to waiting.
  When the request is later filed through the normal workflow, its patient PDF already contains the new archive pages.

WIRING (see the delivery notes): migration 15 creates decree_topup_manual_requests + the columns/RPCs used here; topup_gate.py
blocks the send while a manual request is stored; decree-topup.js moves such entries to «سبق التعامل معها» and shows the request
(number, date, status, plan text) on the card; ID_CARD tabs: «بانتظار بطاقة سارية جديدة» / «تم التصحيح — جاهز للطلب».

NOT TESTED against live SMC / CMIS. The plan-text reader for Requests/Details is label-based (see _plan_text) and has to be
checked once on a real manual request.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from bs4 import BeautifulSoup

log = logging.getLogger("topup_live_checks")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
CAND, MANUAL, INV = "decree_topup_candidates", "decree_topup_manual_requests", "decree_topup_invoices"
DELAY = 0.4
MANUAL_MAX_AGE_H = float(os.environ.get("TOPUP_MANUAL_MAX_AGE_H") or "20")   # re-check an entry at most this often (unless ids were pressed)
IDCARD_MAX_AGE_H = float(os.environ.get("TOPUP_IDCARD_MAX_AGE_H") or "20")   # the 24 h archive re-check
ONLY_IDS = [int(x) for x in re.findall(r"\d+", os.environ.get("CANDIDATE_IDS") or "")]
FORCE = bool(ONLY_IDS)                                                         # an explicit selection is always re-checked
TIME_BUDGET_MIN = int(os.environ.get("LIVE_CHECK_BUDGET_MIN") or "40")
IDCARD_SAME_DAY = (os.environ.get("TOPUP_IDCARD_SAME_DAY") or "1").strip().lower() not in ("0", "false", "off", "no")
OPEN_EXCLUDED = "not.in.(SUBMITTED,DISMISSED,RESOLVED)"
STOP_BUCKETS = ("Approved", "Admin_Letter")


# --------------------------------------------------------------------------- Supabase REST
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


def _set(cid, **body):
    _rest("PATCH", CAND, params={"id": f"eq.{cid}"}, json=body, headers={"Prefer": "return=minimal"})


def _now():
    return datetime.now(timezone.utc)


def _age_h(ts):
    if not ts:
        return None
    try:
        return (_now() - datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).total_seconds() / 3600
    except ValueError:
        return None


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def national_id_of(patient_id) -> str:
    if not patient_id:
        return ""
    rows = _rest("GET", "patients", params={"select": "national_id", "id": f"eq.{patient_id}", "limit": 1})
    return str(rows[0]["national_id"]) if rows and rows[0].get("national_id") else ""


def _candidates():
    prm = {"select": "*", "status": OPEN_EXCLUDED, "order": "id.asc", "limit": 2000}
    if ONLY_IDS:
        prm["id"] = "in.(" + ",".join(map(str, ONLY_IDS)) + ")"
    return _rest("GET", CAND, params=prm) or []


# =========================================================================== CHECK 1 - manual requests
def _known_request_numbers(patient_id) -> set:
    """Request numbers this app filed for the patient (any case, any attempt)."""
    cases = _rest("GET", "decree_request_cases", params={"select": "id", "patient_id": f"eq.{patient_id}", "limit": 1000}) or []
    ids = [str(c["id"]) for c in cases]
    out = set()
    for i in range(0, len(ids), 100):
        rows = _rest("GET", "decree_request_attempts", params={
            "select": "website_request_id", "case_id": "in.(" + ",".join(ids[i:i + 100]) + ")",
            "website_request_id": "not.is.null", "limit": 1000}) or []
        out.update(str(r["website_request_id"]) for r in rows)
    return out


def _patient_requests(smc, nid: str, since: date) -> list:
    """Every request of the patient filed from `since` until today, NEWEST FIRST. 15-day windows (a wide single window made the
    site answer HTTP 500 before), newest window first."""
    import decree_status_and_letters_sync as st
    url = f"{st.BASE_URL}/smc/Reports/SendRequestStatusJson"
    today = date.today()
    out, seen = [], set()
    end = today
    while end >= since:
        start = max(since, end - timedelta(days=14))
        payload = {"CitizenName": "", "StartDate": st._smc_datetime_str(start.isoformat()),
                   "EndDate": st._smc_datetime_str(end.isoformat(), end_of_day=True), "SsnNumber": nid,
                   "RequestNumber": "", "RequestStatusId": "", "SystemUserId": ""}
        resp = None
        for attempt in (1, 2, 3):
            try:
                r = smc.s.post(url, data=payload, timeout=40)
                if r.status_code == 200:
                    resp = r
                    break
                log.warning(f"  SendRequestStatusJson {start}..{end} HTTP {r.status_code} ({attempt}/3)")
            except Exception as e:
                log.warning(f"  SendRequestStatusJson {start}..{end}: {e} ({attempt}/3)")
            time.sleep(1.5 * attempt)
        if resp is None:
            raise RuntimeError(f"طلبات المريض على SMC لم تُقرأ ({start}..{end})")
        try:
            raw = resp.json()
        except ValueError:
            raw = resp.text
        for rec in st._parse_send_request_status_json(raw):
            row = st._row_from_record(rec, fallback_date_iso=end.isoformat())
            if row and row["request_number"] not in seen and (not row.get("patient_id") or row["patient_id"] == nid):
                seen.add(row["request_number"])
                out.append(row)
        time.sleep(DELAY)
        end = start - timedelta(days=1)
    out.sort(key=lambda r: ((r.get("request_date") or ""), int(r["request_number"]) if r["request_number"].isdigit() else 0), reverse=True)
    return out


_PLAN_LABELS = ("الخطة العلاجية", "خطة العلاج", "الخطه العلاجيه", "التوصية", "التوصيه", "الإجراء", "الاجراء", "خطة")


def _plan_text(smc, request_number: str) -> str:
    """Whatever plan wording the request page carries. Label-based and NOT verified on a live manual request: tries the
    th/td pairs, then the TREATMENTPLAN field, and returns '' when nothing is found (the row is still stored)."""
    import decree_status_and_letters_sync as st
    try:
        r = smc.s.get(f"{st.BASE_URL}/smc/Requests/Details/{request_number}", timeout=40)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all(["textarea", "input"]):
            if str(tag.get("name") or tag.get("id") or "").upper() == "TREATMENTPLAN":
                v = tag.text if tag.name == "textarea" else tag.get("value", "")
                if v and v.strip():
                    return re.sub(r"[ \t]+", " ", v).strip()[:1000]
        for s in soup(["script", "style"]):
            s.decompose()
        for th in soup.find_all("th"):
            label = re.sub(r"\s+", " ", th.get_text(" ", strip=True)).rstrip(":").strip()
            if any(l in label for l in _PLAN_LABELS):
                td = th.find_next_sibling("td")
                if td is not None:
                    v = re.sub(r"\s+", " ", td.get_text(" ", strip=True))
                    if v:
                        return v[:1000]
    except Exception as e:
        log.warning(f"  plan text of request {request_number} not read: {e}")
    return ""


def _source_request(c: dict):
    """(date of the declined request, its SMC request number)."""
    import topup_resolution as tr
    d = tr.request_date(c)
    num = ""
    if c.get("source_case_id"):
        a = _rest("GET", "decree_request_attempts", params={"select": "website_request_id", "case_id": f"eq.{c['source_case_id']}",
                                                            "website_request_id": "not.is.null", "limit": 1}) or []
        num = str(a[0]["website_request_id"]) if a else ""
    return d or _d(c.get("detected_at")), num


def check_manual(c: dict, smc, status_map, cache: dict) -> int:
    """Stores the patient's pending manual requests for this entry. Returns how many were found."""
    import decree_status_and_letters_sync as st
    cid = c["id"]
    nid = national_id_of(c.get("patient_id"))
    if not nid:
        return 0
    since, src_num = _source_request(c)
    if not since:
        raise RuntimeError("تاريخ الطلب المرفوض غير معروف")
    key = (nid, since)
    if key not in cache:
        cache[key] = _patient_requests(smc, nid, since)
    known = _known_request_numbers(c["patient_id"]) | ({src_num} if src_num else set())
    ignored = {str(x) for x in (c.get("manual_ignored_numbers") or [])}      # the operator pressed «تجاهل» (topup_manual_ignore)
    pending, unmapped = [], set()
    for r in cache[key]:                                   # newest first
        status = r.get("request_status") or ""
        bucket = st.resolve_status_bucket(status_map, status)
        b = bucket["english_bucket"]
        if b in STOP_BUCKETS:
            break                                          # a final answer: nothing older can still be pending
        if b.startswith("Cancelled"):
            continue
        if b == "Unknown":
            unmapped.add(status)
        rd = _d(r.get("request_date"))
        if r["request_number"] in known or r["request_number"] in ignored or (rd and rd < since):
            continue
        pending.append({**r, "bucket": b})
    if unmapped:
        log.warning(f"  unmapped statuses treated as pending: {sorted(unmapped)}")
    rows = []
    for r in pending:
        rows.append({"candidate_id": cid, "request_number": r["request_number"], "request_date": r.get("request_date"),
                     "status_raw": r.get("request_status"), "status_bucket": r["bucket"],
                     "plan_text": _plan_text(smc, r["request_number"]), "fetched_at": _now().isoformat()})
        time.sleep(DELAY)
    _rest("DELETE", MANUAL, params={"candidate_id": f"eq.{cid}"})
    if rows:
        _rest("POST", MANUAL, json=rows, headers={"Prefer": "return=minimal"})
    _set(cid, manual_check_at=_now().isoformat(), manual_check_message=None)
    return len(rows)


# =========================================================================== CHECK 2 - new ID card in DMS
def _letter_date(c: dict):
    if c.get("source_case_id"):
        try:
            r = _rest("GET", "decree_smc_status_tracking", params={"select": "admin_letter_committee_date",
                                                                   "case_id": f"eq.{c['source_case_id']}", "limit": 1}) or []
            d = _d(r[0].get("admin_letter_committee_date")) if r else None
            if d:
                return d
        except Exception:
            pass
    return _d(c.get("detected_at"))


def _r2():
    import boto3
    return boto3.client("s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")


def check_idcard(c: dict) -> str:
    """-> 'FOUND' | 'WAITING_NEW' | 'ERROR'"""
    import patient_pdf_dms_archive_fallback as dms
    cid = c["id"]
    nid = national_id_of(c.get("patient_id"))
    letter = _letter_date(c)
    if not nid or not letter:
        _set(cid, idcard_check_at=_now().isoformat(), idcard_message="الرقم القومي أو تاريخ الخطاب غير معروف")
        return "ERROR"
    rejected = set(c.get("idcard_rejected_ids") or [])
    opened = dms._open_patient_session(nid)
    if not opened:
        _set(cid, idcard_check_at=_now().isoformat(), idcard_message="تعذّر فتح أرشيف DMS للمريض (تسجيل الدخول/البحث) — يُعاد بعد 24 ساعة")
        return "ERROR"
    session, mr, index_html = opened
    ids = dms.extract_archive_row_dates(index_html)
    floor = letter if IDCARD_SAME_DAY else letter + timedelta(days=1)
    new = [i for i in ids if (dms._parse_row_date(i) or date.min) >= floor and i not in rejected]
    if not new:
        _set(cid, idcard_state="WAITING_NEW", idcard_check_at=_now().isoformat(),
             idcard_message=f"لا توجد أوراق أرشيف بتاريخ {floor:%d/%m/%Y} أو بعده — فُحص {len(ids)} صف")
        return "WAITING_NEW"
    gallery = dms.call_archive_view_all(session, new)
    urls = dms.parse_gallery_pdf_urls(gallery) if gallery else []
    blobs = [b for b in (dms._download_pdf_bytes(session, u) for u in urls) if b]
    if not blobs:
        _set(cid, idcard_check_at=_now().isoformat(), idcard_message="وُجدت أوراق أرشيف جديدة لكن تعذّر تنزيلها — يُعاد بعد 24 ساعة")
        return "ERROR"
    tmp = os.path.join(tempfile.mkdtemp(prefix="idcard_"), f"{nid}_idcard.pdf")
    if not dms._merge_pdf_bytes_list(blobs, tmp):
        _set(cid, idcard_check_at=_now().isoformat(), idcard_message="تعذّر دمج أوراق الأرشيف الجديدة")
        return "ERROR"
    data = open(tmp, "rb").read()
    import pymupdf
    d = pymupdf.open(stream=data, filetype="pdf")
    pages = len(d)
    d.close()
    key = f"topup-idcards/{cid}/{_now():%Y%m%d%H%M%S}.pdf"
    bucket = os.environ["R2_BUCKET_NAME"]
    s3 = _r2()
    old = _rest("GET", INV, params={"select": "id,r2_key", "candidate_id": f"eq.{cid}", "doc_kind": "eq.ID_CARD_NEW"}) or []
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/pdf")
    for o in old:
        if o.get("r2_key"):
            try:
                s3.delete_object(Bucket=bucket, Key=o["r2_key"])
            except Exception:
                pass
        _rest("DELETE", INV, params={"id": f"eq.{o['id']}"})
    dates = sorted({str(dms._parse_row_date(i)) for i in new})
    _rest("POST", INV, json=[{
        "candidate_id": cid, "decree_number": "IDCARD", "receipt_id": f"IDCARD-{cid}", "doc_kind": "ID_CARD_NEW",
        "signed_source": "dms_archive", "r2_key": key, "bytes": len(data), "page_count": pages,
        "sha256": hashlib.sha256(data).hexdigest(), "status": "REVIEW",
        "verification": {"archive_ids": new, "letter_date": str(letter)},
        "message": f"أوراق أرشيف DMS جديدة بتاريخ {', '.join(dates)} — راجع أنها تحمل البطاقة السارية",
        "fetched_at": _now().isoformat()}], headers={"Prefer": "return=minimal"})
    _set(cid, idcard_state="FOUND", idcard_found_ids=new, idcard_found_at=_now().isoformat(),
         idcard_check_at=_now().isoformat(), idcard_message=f"{len(new)} ورقة أرشيف جديدة ({', '.join(dates)}) — {pages} صفحة")
    _rpc("topup_mark_id_fixed", {"p_id": cid, "p_note": f"وُجدت أوراق أرشيف جديدة بتاريخ {', '.join(dates)} — راجع الملف المستخرج"})
    log.info(f"  candidate {cid}: ID-card check FOUND {len(new)} new archive row(s) -> marked ready for review")
    return "FOUND"


# =========================================================================== main
def main():
    rows = _candidates()
    manual_todo = [c for c in rows if FORCE or (_age_h(c.get("manual_check_at")) or 1e9) >= MANUAL_MAX_AGE_H]
    id_todo = [c for c in rows if (c.get("kind") == "ID_CARD") and not c.get("id_fixed_at") and c.get("idcard_state") != "FOUND"
               and (FORCE or (_age_h(c.get("idcard_check_at")) or 1e9) >= IDCARD_MAX_AGE_H)]
    log.info(f"{len(rows)} open entr(ies): {len(manual_todo)} for the manual-request check, {len(id_todo)} for the ID-card archive check")
    deadline = time.time() + TIME_BUDGET_MIN * 60
    found_total = 0

    if manual_todo:
        u = p = ""
        for un, pw in (("SMC_USERNAME_3", "SMC_PASSWORD_3"), ("SMC_USERNAME_2", "SMC_PASSWORD_2"), ("SMC_USERNAME", "SMC_PASSWORD")):
            if os.environ.get(un) and os.environ.get(pw):
                u, p = os.environ[un], os.environ[pw]
                break
        if not u:
            log.error("No SMC credentials set - manual-request check skipped")
        else:
            import Unified_Decree_Submission_Pipeline as _p
            from Unified_Decree_Submission_Pipeline import SMCSession
            import decree_status_and_letters_sync as st
            _p.USERNAME, _p.PASSWORD = u, p
            smc = SMCSession()
            if not smc.login():
                log.error("SMC login failed - manual-request check skipped")
            else:
                status_map = st.load_status_map()
                cache = {}
                for c in manual_todo:
                    if time.time() > deadline:
                        log.warning("time budget reached - the rest is checked next run")
                        break
                    try:
                        n = check_manual(c, smc, status_map, cache)
                        found_total += n
                        log.info(f"  candidate {c['id']}: {n} pending manual request(s)")
                    except Exception as e:
                        log.warning(f"  candidate {c['id']}: manual-request check failed ({type(e).__name__}: {str(e)[:150]})")
                        try:
                            _set(c["id"], manual_check_message=f"{type(e).__name__}: {str(e)[:200]}")
                        except Exception:
                            pass
    for c in id_todo:
        if time.time() > deadline:
            break
        try:
            check_idcard(c)
        except Exception as e:
            log.warning(f"  candidate {c['id']}: ID-card check failed ({type(e).__name__}: {str(e)[:150]})")
            try:
                _set(c["id"], idcard_check_at=_now().isoformat(), idcard_message=f"{type(e).__name__}: {str(e)[:200]}")
            except Exception:
                pass
    log.info(f"done: {found_total} pending manual request(s) found in this run")


if __name__ == "__main__":
    main()
