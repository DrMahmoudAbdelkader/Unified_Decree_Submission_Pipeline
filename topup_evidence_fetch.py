#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_evidence_fetch.py   (v2 - rebuilt from the HAR of a manual SMC session)
==========================================================================
For every top-up candidate with evidence_status = PENDING: find the patient's
PREVIOUS decrees of the same protocol, list each decree's invoices on SMC,
and keep a SIGNED copy of every approved invoice in R2 + decree_topup_invoices.

CONFIRMED SMC FLOW (all from the HAR):
  1. decree -> invoices   POST {BASE}/smc//HospDecreeReceipts/GetHospitalDecreeReceipts
                          form body  decreeId=<decree>      (XHR, returns an HTML table
                          #hospDecreeReceiptTblId, one tr.item per invoice)
  2. invoice page         GET  {BASE}/smc/HospDecreeReceipts/Details/<receiptId>
                          = the PRINTED, UNSIGNED invoice (text, Arabic-Indic digits).
                          Ends with the 4 signature labels signature_script.py anchors on.
  3. signed attachment    GET  {BASE}/smc/HospDecreeReceipts/ReadFiles?ReceiptId=<receiptId>
                          = the hospital's own upload of the signed invoice.

WHAT THE REAL ATTACHMENTS LOOK LIKE: a one-page SCAN (producer "Toshiba e-STUDIO",
one full-page image, NO text layer). So "signed?" cannot be decided from text or from
the number of images. Rules used here:
  - scan (no text layer): signed  <=>  enough BLUE-INK pixels on the page (wet-ink
    signatures + stamp are blue, the printed text is grey).  Measured on 3 real signed
    invoices: ~7,700 blue px each vs ~600 on the same page with the signature block
    blanked.  Threshold SIGNED_MIN_BLUE_PX (default 2500).
  - born-digital PDF (has text): signed <=> >= SIGNED_MIN_IMAGES (default 4) images on
    the last text page (what signature_script.py draws).
Because a scan has no text, the invoice is tied to the decree / patient through the
Details page (digits normalised) and the listing's own DECREEID field instead.

FALLBACK LADDER (every rung below the first ends in status REVIEW, never OK):
  A  attachment present and looks signed ............ keep it                 (smc_attachment)
  B  attachment present but NOT signed .............. sign it with
                                                      signature_script.sign_pdf (rendered_fallback)
  C  no attachment / download failed / not a PDF .... render Details/<id> with
                                                      Chromium, then sign it    (rendered_fallback)
Nothing is ever uploaded back to SMC.
"""
from __future__ import annotations
import hashlib, io, logging, os, re, sys, tempfile, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from bs4 import BeautifulSoup
import Unified_Decree_Submission_Pipeline as _p
from Unified_Decree_Submission_Pipeline import SMCSession
import supabase_client as sb

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("topup_evidence_fetch")

BASE_URL = _p.BASE_URL
LIST_URL = BASE_URL + "/smc//HospDecreeReceipts/GetHospitalDecreeReceipts"   # double slash exactly as the site's own JS
DETAILS_URL = BASE_URL + "/smc/HospDecreeReceipts/Details/{rid}"
READ_URL = BASE_URL + "/smc/HospDecreeReceipts/ReadFiles"
CREATE_URL = BASE_URL + "/smc/HospDecreeReceipts/Create?decreeId={decree}"   # the page that fires the listing XHR

MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES") or "25")
SIGNED_MIN_IMAGES = int(os.environ.get("SIGNED_MIN_IMAGES") or "4")
SIGNED_MIN_BLUE_PX = int(os.environ.get("SIGNED_MIN_BLUE_PX") or "2500")
DELAY = 0.4
CAND, INV = "decree_topup_candidates", "decree_topup_invoices"
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def ar2en(s: str) -> str:
    return (s or "").translate(_AR_DIGITS)


# --------------------------------------------------------------------------- infra
def _creds():
    for u, p in (("SMC_USERNAME_3", "SMC_PASSWORD_3"), ("SMC_USERNAME_2", "SMC_PASSWORD_2"),
                 ("SMC_USERNAME", "SMC_PASSWORD")):
        if os.environ.get(u) and os.environ.get(p):
            return os.environ[u], os.environ[p]
    return "", ""


def _rpc(name, body):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/{name}", timeout=60, json=body,
                      headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"})
    if not r.ok:
        raise RuntimeError(f"{name} {r.status_code}: {r.text[:200]}")


def _r2():
    import boto3
    return boto3.client(
        "s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")


def national_id_of(patient_id) -> str:
    if not patient_id:
        return ""
    rows = sb.select("patients", select="national_id", filters={"id": f"eq.{patient_id}"}, limit=1)
    return str(rows[0]["national_id"]) if rows and rows[0].get("national_id") else ""


# --------------------------------------------------------------------------- which decrees
def _db_decrees(c: dict) -> list[str]:
    """Decrees stored by THIS app: attempts of the patient's earlier cases whose plan carries the
    same top-up protocol (excluding the declined request itself). Oldest first."""
    if not (c.get("patient_id") and c.get("protocol_key")):
        return []
    cases = sb.select("decree_request_cases", select="id,treatment_plan_id",
                      filters={"patient_id": f"eq.{c['patient_id']}"}, limit=1000)
    plan_ids = {x["treatment_plan_id"] for x in cases if x.get("treatment_plan_id")}
    if not plan_ids:
        return []
    plans = sb.select("decree_treatment_plans", select="id",
                      filters={"topup_protocol": f"eq.{c['protocol_key']}",
                               "id": "in.(" + ",".join(map(str, plan_ids)) + ")"}, limit=1000)
    ok_plans = {p["id"] for p in plans}
    case_ids = [x["id"] for x in cases if x["treatment_plan_id"] in ok_plans and x["id"] != c["source_case_id"]]
    if not case_ids:
        return []
    atts = sb.select("decree_request_attempts", select="id,decree_number",
                     filters={"case_id": "in.(" + ",".join(map(str, case_ids)) + ")",
                              "decree_number": "not.is.null"}, order="id.asc", limit=1000)
    return list(dict.fromkeys(str(a["decree_number"]) for a in atts))


def _ensure_lookup(c: dict, smc) -> None:
    """The patient's decrees as SMC itself lists them (the page's «فحص قرارات المريض»). Done here
    when it has not been done for this entry yet, so one press fetches everything."""
    if smc is None or c.get("lookup_status") == "DONE":
        return
    try:
        import topup_decree_lookup as lk
        n = lk.lookup_one(smc, {"id": c["id"], "patient_id": c.get("patient_id")})
        sb.update(CAND, c["id"], {"lookup_status": "DONE", "lookup_message": f"{n} قرار",
                                  "lookup_finished_at": datetime.now(timezone.utc).isoformat()})
        c["lookup_status"] = "DONE"
        log.info(f"  candidate {c['id']}: patient lookup done inline ({n} decree(s))")
    except Exception as e:
        log.warning(f"  candidate {c['id']}: inline patient lookup failed: {type(e).__name__}: {e}")


def _lookup_decrees(c: dict) -> list[tuple[str, str]]:
    """[(issue_date 'YYYY-MM-DD' or '', decree_number)] of the patient's decrees on SMC that belong to
    the same PLAN / PROTOCOL (match computed in the DB view), issued on or before the declined
    request. Oldest first."""
    try:
        rows = sb.select("decree_topup_lookup_view", select="decree_number,decree_date,match_kind",
                         filters={"candidate_id": f"eq.{c['id']}", "match_kind": "not.is.null"},
                         order="decree_date.asc.nullsfirst", limit=200)
    except Exception as e:
        log.warning(f"  lookup view not readable (migration 06?): {e}")
        return []
    cutoff = ""
    if c.get("source_case_id"):
        try:
            src = sb.select("decree_request_cases", select="created_at",
                            filters={"id": f"eq.{c['source_case_id']}"}, limit=1)
            cutoff = (src[0].get("created_at") or "")[:10] if src else ""
        except Exception:
            cutoff = ""
    out = []
    for r in rows or []:
        d = (r.get("decree_date") or "")[:10]
        if cutoff and d and d > cutoff:
            continue                       # issued AFTER the declined request: not a "previous" decree
        out.append((d, str(r["decree_number"])))
    return out


def previous_decrees(c: dict, smc=None) -> list[str]:
    """Manual numbers win. Otherwise the union of
         (a) decrees this app stored for the patient's earlier cases on the same protocol, and
         (b) decrees SMC lists for the patient on the same plan/protocol (the patient lookup),
    oldest first; protocols.max_previous_decrees then keeps only the NEWEST N."""
    if c.get("manual_decree_numbers"):
        return list(dict.fromkeys(str(x) for x in c["manual_decree_numbers"]))
    _ensure_lookup(c, smc)
    from_db = _db_decrees(c)
    from_smc = _lookup_decrees(c)
    dates = {n: d for d, n in from_smc}
    merged = list(dict.fromkeys(from_db + [n for _, n in from_smc]))
    # oldest first by issue date (unknown dates count as oldest); stable for ties
    decrees = sorted(merged, key=lambda n: dates.get(n, ""))
    if not decrees:
        return []
    log.info(f"  candidate {c['id']}: previous decrees {len(from_db)} from the app + {len(from_smc)} from SMC "
             f"-> {len(decrees)} distinct")
    # protocols.max_previous_decrees (e.g. 'supportive' = 1): only the NEWEST previous decrees' invoices
    lim = None
    if c.get("protocol_key"):
        try:
            pr = sb.select("decree_topup_protocols", select="max_previous_decrees",
                           filters={"protocol_key": f"eq.{c['protocol_key']}"}, limit=1)
            lim = (pr[0].get("max_previous_decrees") if pr else None)
        except Exception as e:
            log.warning(f"max_previous_decrees lookup skipped: {e}")
    return decrees[-int(lim):] if lim else decrees


# --------------------------------------------------------------------------- SMC calls
def list_receipts(smc: SMCSession, decree: str) -> list[dict]:
    """POST decreeId=<decree> exactly like the Create page's own XHR. Returns one dict per invoice:
    receipt_id, decree_id, claim_id, status_code ('M1'...), status_text, approved, amount,
    reg_date, has_attachment."""
    hdr = {"X-Requested-With": "XMLHttpRequest",
           "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
           "Referer": CREATE_URL.format(decree=decree), "Origin": BASE_URL}
    r = smc.s.post(LIST_URL, data={"decreeId": decree}, headers=hdr, timeout=60)
    if smc._session_expired(r):
        log.warning("session expired during listing - re-login")
        if not smc.login():
            raise RuntimeError("SMC re-login failed")
        r = smc.s.post(LIST_URL, data={"decreeId": decree}, headers=hdr, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"receipt listing HTTP {r.status_code} for decree {decree}")
    return parse_receipt_table(r.text)


def parse_receipt_table(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("tr.item"):
        def hid(cls):
            t = tr.select_one(f"input.{cls}")
            return (t.get("value") or "").strip() if t else ""
        rid = hid("ID")
        if not rid:
            continue
        tds = tr.find_all("td", recursive=False)
        txt = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tds]
        # column order from the table header: كود | عنوان | تاريخ التسجيل | حالة | ملاحظات | تعديل | قيمة | رقم المطالبة | المرفقات ...
        status_text = txt[3] if len(txt) > 3 else ""
        att = tr.select_one(f'td[id="receiptAttachment_{rid}"] a[href*="ReadFiles"]') or \
              tr.select_one('a[href*="ReadFiles"]')
        amount = None
        if len(txt) > 6:
            m = re.search(r"[\d,]+(?:\.\d+)?", txt[6])
            amount = float(m.group(0).replace(",", "")) if m else None
        out.append({"receipt_id": rid, "decree_id": hid("DECREEID"), "claim_id": hid("CLAIMID"),
                    "status_code": hid("STATUSID"), "status_text": status_text,
                    "approved": "معتمد" in status_text,
                    "amount": amount, "reg_date": txt[2] if len(txt) > 2 else "",
                    "has_attachment": att is not None})
    return out


def check_details(smc: SMCSession, receipt: str, decree: str, nid: str) -> dict:
    """The Details page is printed text with Arabic-Indic digits. Normalise and cross-check
    receipt number, decree number and patient national id (scans have no text to check)."""
    r = smc._get(DETAILS_URL.format(rid=receipt))
    if r is None:
        return {"details_ok": False, "details_error": "HTTP/session problem"}
    t = ar2en(re.sub(r"\s+", " ", BeautifulSoup(r.text, "html.parser").get_text(" ")))
    d = {"details_ok": True,
         "receipt_in_details": f"{receipt}" in t,
         "decree_in_details": (decree in t) if decree else None,
         "nid_in_details": (nid in t) if nid else None}
    m = re.search(r"قرار وزاري رقم\s*(\d{10,})", t)
    d["decree_on_page"] = m.group(1) if m else None
    return d


def download_attachment(smc: SMCSession, receipt: str):
    r = smc._get(f"{READ_URL}?ReceiptId={receipt}")
    if r is None:
        return None, "HTTP error / session problem"
    if not r.content.startswith(b"%PDF"):
        return None, f"response is not a PDF (content-type {r.headers.get('content-type')})"
    return r.content, None


# --------------------------------------------------------------------------- is it signed?
def inspect_pdf(data: bytes) -> dict:
    """Facts only: pages, text length, images on last page, blue-ink pixels on last page."""
    import pymupdf
    import numpy as np
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        v = {"bytes": len(data), "pages": len(doc),
             "text_len": sum(len(pg.get_text().strip()) for pg in doc)}
        idx = next((i for i in range(len(doc) - 1, -1, -1) if doc[i].get_text().strip()), len(doc) - 1)
        pg = doc[idx]
        v["images_last_page"] = len(pg.get_images(full=True))
        pix = pg.get_pixmap(dpi=100, alpha=False)
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).astype(int)
        v["blue_px"] = int(((a[..., 2] - a[..., 0] > 25) & (a[..., 2] > 70)).sum())
        v["is_scan"] = v["text_len"] < 50
        v["looks_signed"] = (v["blue_px"] >= SIGNED_MIN_BLUE_PX) if v["is_scan"] \
            else (v["images_last_page"] >= SIGNED_MIN_IMAGES)
        return v
    finally:
        doc.close()


# --------------------------------------------------------------------------- signing / rendering
_ASSETS = {}


def _assets() -> dict:
    if not _ASSETS:
        import supabase_storage
        _ASSETS.update(supabase_storage.fetch_signing_and_template_assets(
            os.path.join(tempfile.gettempdir(), "topup_assets")))
    return _ASSETS


def sign_with_script(data: bytes) -> bytes:
    """signature_script.sign_pdf: detects the label row from rendered pixels (works on text
    PDFs AND on scans) and draws sig1-4 + stamp from the Supabase 'decree-assets' bucket."""
    import signature_script as ss
    a = _assets()
    with tempfile.TemporaryDirectory() as td:
        src, dst = os.path.join(td, "in.pdf"), os.path.join(td, "out.pdf")
        with open(src, "wb") as f:
            f.write(data)
        ss.sign_pdf(src, dst, a["sig1"], a["sig2"], a["sig3"], a["sig4"], a["stamp"])
        with open(dst, "rb") as f:
            return f.read()


def render_details_pdf(smc: SMCSession, receipt: str) -> bytes:
    """Rung C: print the (unsigned) Details page with Chromium, like the manual print."""
    from urllib.parse import urlparse
    from playwright.sync_api import sync_playwright
    host = urlparse(BASE_URL).hostname
    cookies = [{"name": c.name, "value": c.value, "domain": host, "path": "/"} for c in smc.s.cookies]
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        try:
            ctx = br.new_context()
            ctx.add_cookies(cookies)
            page = ctx.new_page()
            page.goto(DETAILS_URL.format(rid=receipt), wait_until="load", timeout=60_000)
            page.emulate_media(media="print")
            # the page body is grey on screen; keep the printed sheet white
            page.add_style_tag(content="body{background:#fff !important}")
            return page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            br.close()


# --------------------------------------------------------------------------- one invoice
def build_invoice_row(smc, cid, decree, info, nid):
    receipt = info["receipt_id"]
    v = {"listing": {k: info[k] for k in ("status_code", "status_text", "amount", "reg_date", "claim_id")}}
    v["details"] = check_details(smc, receipt, decree, nid)
    time.sleep(DELAY)

    data, source, notes = None, "smc_attachment", []
    if info["has_attachment"]:
        raw, err = download_attachment(smc, receipt)
        time.sleep(DELAY)
        if raw is None:
            notes.append(f"تعذر تحميل المرفق: {err}")
        else:
            v["attachment"] = inspect_pdf(raw)
            if v["attachment"]["looks_signed"]:
                data = raw                                        # rung A
            else:
                try:                                              # rung B
                    data, source = sign_with_script(raw), "rendered_fallback"
                    notes.append("مرفق SMC غير موقّع — تم التوقيع تلقائياً")
                except Exception as e:
                    v["sign_error"] = f"{type(e).__name__}: {str(e)[:160]}"
                    notes.append("مرفق SMC غير موقّع وفشل التوقيع التلقائي")
    else:
        notes.append("لا يوجد مرفق للفاتورة على SMC")
    if data is None:                                              # rung C
        try:
            data, source = sign_with_script(render_details_pdf(smc, receipt)), "rendered_fallback"
            notes.append("تم إنشاء الفاتورة من صفحة التفاصيل وتوقيعها تلقائياً")
        except Exception as e:
            v["render_error"] = f"{type(e).__name__}: {str(e)[:160]}"

    row = {"candidate_id": cid, "decree_number": decree, "receipt_id": receipt, "signed_source": source}
    if data is None:
        row.update(status="FAILED", message=" | ".join(notes) or "تعذر الحصول على الفاتورة", verification=v)
        return row, None
    final = inspect_pdf(data)
    v["final"] = final
    d = v["details"]
    ties_ok = bool(d.get("details_ok") and d.get("receipt_in_details")
                   and d.get("decree_in_details") is not False and d.get("nid_in_details") is not False)
    listing_ok = info["decree_id"] in ("", decree)
    good = (source == "smc_attachment" and final["looks_signed"] and ties_ok and listing_ok)
    if not ties_ok:
        notes.append("لم يتطابق رقم الفاتورة/القرار/الرقم القومي مع صفحة التفاصيل — راجع الملف")
    if not listing_ok:
        notes.append(f"الفاتورة مسجلة على قرار آخر ({info['decree_id']})")
    if source == "rendered_fallback":
        notes.append("راجع الملف قبل الاعتماد")
    elif not final["looks_signed"]:
        notes.append("لم يتم التحقق من وجود التوقيعات — راجع الملف")
    row.update(bytes=len(data), page_count=final["pages"], sha256=hashlib.sha256(data).hexdigest(),
               verification=v, status="OK" if good else "REVIEW",
               message=" | ".join(dict.fromkeys(notes)) or None,
               fetched_at=datetime.now(timezone.utc).isoformat())
    return row, data


def save_row(cid, receipt, row):
    ex = sb.select(INV, select="id", filters={"candidate_id": f"eq.{cid}", "receipt_id": f"eq.{receipt}"}, limit=1)
    (sb.update(INV, ex[0]["id"], row) if ex else sb.insert(INV, row))


# --------------------------------------------------------------------------- one candidate
def process(c: dict, smc: SMCSession, s3, bucket: str):
    cid = c["id"]
    sb.update(CAND, cid, {"evidence_status": "FETCHING"})
    nid = national_id_of(c.get("patient_id"))
    decrees = previous_decrees(c, smc)
    manual_receipts = [str(x) for x in (c.get("manual_receipt_ids") or [])]
    if not decrees and not manual_receipts:
        sb.update(CAND, cid, {"evidence_status": "NO_PREVIOUS_DECREE",
                              "evidence_message": "لا يوجد قرار سابق بنفس الخطة/البروتوكول لا في سجلات النظام ولا في قرارات المريض على SMC — أدخل أرقام القرارات يدوياً"})
        return

    work, problems = [], []          # work: (decree, listing_info)
    for d in decrees:
        try:
            rows = list_receipts(smc, d)
        except Exception as e:
            problems.append(f"{d}: {type(e).__name__}: {str(e)[:80]}"); continue
        time.sleep(DELAY)
        approved = [x for x in rows if x["approved"]]
        if not rows:
            problems.append(f"{d}: لا توجد فواتير")
        elif not approved:
            problems.append(f"{d}: لا توجد فواتير معتمدة")
        work.extend((d, x) for x in approved)
    # manually typed receipt ids: decree is read off the invoice page itself
    for rid in manual_receipts:
        if any(x["receipt_id"] == rid for _, x in work):
            continue
        det = check_details(smc, rid, "", nid)
        d = det.get("decree_on_page") or (decrees[0] if len(decrees) == 1 else "manual")
        work.append((d, {"receipt_id": rid, "decree_id": "", "claim_id": "", "status_code": "",
                         "status_text": "manual", "approved": True, "amount": None, "reg_date": "",
                         "has_attachment": True}))

    if not work:
        sb.update(CAND, cid, {"evidence_status": "FAILED",
                              "evidence_message": "لا توجد فواتير معتمدة للقرارات السابقة — " + " ; ".join(problems)})
        return
    for decree, info in work:
        row, data = build_invoice_row(smc, cid, decree, info, nid)
        if data is not None:
            key = f"topup-invoices/{cid}/{decree}_{info['receipt_id']}.pdf"
            s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/pdf")
            row["r2_key"] = key
        save_row(cid, info["receipt_id"], row)
    _rpc("topup_recompute_evidence", {"p_id": cid})


def main():
    u, p = _creds()
    if not u:
        log.error("No SMC credentials set."); sys.exit(1)
    _p.USERNAME, _p.PASSWORD = u, p
    smc = SMCSession()
    if not smc.login():
        log.error("SMC login failed."); sys.exit(1)
    try:
        _rpc("topup_release_stale_fetching", {"p_minutes": 45})
    except Exception as e:
        log.warning(f"stale release skipped: {e}")
    flt = {"evidence_status": "eq.PENDING", "protocol_key": "not.is.null",
           "status": "not.in.(SUBMITTED,DISMISSED)"}
    only = [int(x) for x in re.findall(r"\d+", os.environ.get("CANDIDATE_IDS") or "")]
    if only:                       # the page asked for these entries only
        flt["id"] = "in.(" + ",".join(map(str, only)) + ")"
    rows = sb.select(CAND, select="*", filters=flt, order="id.asc", limit=MAX_CANDIDATES)
    log.info(f"{len(rows)} candidate(s) to fetch evidence for.")
    s3, bucket = _r2(), os.environ["R2_BUCKET_NAME"]
    for c in rows:
        try:
            process(c, smc, s3, bucket)
        except Exception as e:
            log.exception(f"candidate {c['id']} failed")
            sb.update(CAND, c["id"], {"evidence_status": "FAILED",
                                      "evidence_message": f"{type(e).__name__}: {str(e)[:200]}"})


if __name__ == "__main__":
    main()
