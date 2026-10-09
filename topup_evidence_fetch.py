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

SIGNING POLICY (v3): the signed attachment on SMC is NEVER used as evidence. An invoice that is not yet
"معتمد" can still be edited after it was signed, so a scanned signed copy may be missing lines that the
current invoice has. For EVERY invoice, whatever its status:
  1. render the CURRENT Details/<id> page to PDF with Chromium (the unsigned invoice, as printed today)
  2. detect the signature-label row and place sig1-4 + stamp with signature_script.sign_pdf
     (signature images: public bucket signature_files = 1b/2b/3b/4b.png + stamp.png)
  3. verify: the rendered PDF really is this receipt, ink was added, the 4 label columns look sane
Result row: signed_source='rendered_signed'; status OK when every check passes, otherwise REVIEW
(file kept, message says which check failed); FAILED only if rendering/signing itself raised.
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
TIME_BUDGET_MIN = int(os.environ.get("EVIDENCE_TIME_BUDGET_MIN") or "90")   # stop STARTING new entries after this; the rest stay PENDING for the next run
# EVERY invoice SMC lists for a previous decree is collected, whatever its status (approved, returned with an
# approval error, pending ...). The status text is only logged and kept in `verification.listing` for reference;
# it never decides whether the invoice is included or whether it needs review.
APPROVED_RE = re.compile(os.environ.get("APPROVED_STATUS_REGEX") or r"معتمد")
NOT_APPROVED_RE = re.compile(r"غير\s*معتمد|مرفوض|ملغ|رفض")
SIGNED_MIN_IMAGES = int(os.environ.get("SIGNED_MIN_IMAGES") or "4")
SIGNED_MIN_BLUE_PX = int(os.environ.get("SIGNED_MIN_BLUE_PX") or "2500")   # blue px the signing must ADD (100 dpi)
LAYOUT_MIN_SPAN_FRAC = float(os.environ.get("LAYOUT_MIN_SPAN_FRAC") or "0.35")  # label columns must span this much of the page width
SIGNATURE_BASE_URL = (os.environ.get("SIGNATURE_BASE_URL") or
                      "https://qayhkvtgkflxvlhstiuz.supabase.co/storage/v1/object/public/signature_files").rstrip("/")
SIGNATURE_FILES = {"sig1": "1b.png", "sig2": "2b.png", "sig3": "3b.png", "sig4": "4b.png", "stamp": "stamp.png"}
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
    oldest first; then ONLY the newest one is kept (unless all_previous_decrees is set)."""
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
    # GENERAL RULE: only the invoices of the single most recent previous decree of the same plan.
    # Extraordinary override: candidate.all_previous_decrees = true -> every previous decree
    # (still capped by protocols.max_previous_decrees when that is > 1).
    if c.get("all_previous_decrees"):
        lim = None
        if c.get("protocol_key"):
            try:
                pr = sb.select("decree_topup_protocols", select="max_previous_decrees",
                               filters={"protocol_key": f"eq.{c['protocol_key']}"}, limit=1)
                lim = (pr[0].get("max_previous_decrees") if pr else None)
            except Exception as e:
                log.warning(f"max_previous_decrees lookup skipped: {e}")
        return decrees[-int(lim):] if lim and int(lim) > 1 else decrees
    return decrees[-1:]


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
                    "approved": bool(APPROVED_RE.search(status_text)) and not NOT_APPROVED_RE.search(status_text),
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
    """Download the 5 signing images from the public signature_files bucket (once per run)."""
    if not _ASSETS:
        from PIL import Image
        d = os.path.join(tempfile.gettempdir(), "topup_signature_assets")
        os.makedirs(d, exist_ok=True)
        got = {}
        for key, fn in SIGNATURE_FILES.items():
            r = requests.get(f"{SIGNATURE_BASE_URL}/{fn}", timeout=60)
            r.raise_for_status()
            path = os.path.join(d, fn)
            with open(path, "wb") as f:
                f.write(r.content)
            Image.open(path).verify()                      # must really be an image, not an error page
            got[key] = path
        _ASSETS.update(got)
    return _ASSETS


def sign_with_script(data: bytes):
    """signature_script.sign_pdf: finds the label row from the rendered page and draws sig1-4 + stamp.
    Returns (signed_bytes, layout) where layout describes where the 4 label columns were found."""
    import signature_script as ss
    from pypdf import PdfReader
    a = _assets()
    with tempfile.TemporaryDirectory() as td:
        src, dst = os.path.join(td, "in.pdf"), os.path.join(td, "out.pdf")
        with open(src, "wb") as f:
            f.write(data)
        total = len(PdfReader(src).pages)
        page = ss._find_last_content_page(src, total) if total > 1 else 0   # same rule sign_pdf uses
        det = ss.detect_label_row(src, page_index=page)
        centers = [round(float(det["columns"][k]["x_center"]), 1) for k in ("stamp", "sig2", "sig1", "sig3") if k in det["columns"]]
        span = float((max(centers) - min(centers)) / det["pdf_w"]) if len(centers) > 1 else 0.0
        layout = {"page_index": page, "pages": total, "centers": centers, "columns": len(centers),
                  "span_frac": round(span, 3), "label_bottom_frac": round(float(det["label_bot_y"]) / float(det["pdf_h"]), 3),
                  "ascending": all(x < y for x, y in zip(centers, centers[1:]))}
        ss.sign_pdf(src, dst, a["sig1"], a["sig2"], a["sig3"], a["sig4"], a["stamp"])
        with open(dst, "rb") as f:
            return f.read(), layout


def pdf_has_number(data: bytes, number: str) -> bool:
    """Chromium (and SMC's own prints) store Arabic-Indic digit runs in visual order, so the receipt number is
    found REVERSED in the text layer (22750956 -> 65905722). Accept either order."""
    t = pdf_text_digits(data)
    n = str(number)
    return bool(n) and (n in t or n[::-1] in t)


def pdf_text_digits(data: bytes) -> str:
    """All text of the PDF, Arabic-Indic digits -> ASCII, whitespace removed (for number lookups)."""
    import pymupdf
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return re.sub(r"\s+", "", ar2en("".join(pg.get_text() for pg in doc)))
    finally:
        doc.close()


_FONT_READY = False
_HAVE_TNR = False          # real Times New Roman installed from the bucket?


def _install_invoice_font() -> None:
    """Fonts for the invoice print. The SMC invoice page is set in Times New Roman (the original
    PDF embeds TimesNewRomanRegular/Bold), NOT Tahoma. On the runner 'Times New Roman' becomes
    Liberation Serif for Latin, but Liberation has no Arabic, so Arabic falls back to the much wider
    DejaVu Sans -> longer lines, wrapped cells, a different layout.
    Best fix: the real files in the private bucket  decree-assets/fonts/times.ttf  and
    fonts/timesbd.ttf  (copy them from C:\\Windows\\Fonts). Optional: without them we fall back to
    an Arabic-capable family (Amiri) appended to the page's own font list."""
    global _FONT_READY, _HAVE_TNR
    if _FONT_READY:
        return
    _FONT_READY = True
    try:
        import subprocess, supabase_storage
        font_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "fonts")
        os.makedirs(font_dir, exist_ok=True)
        got = []
        for obj in ("fonts/times.ttf", "fonts/timesbd.ttf", "fonts/timesi.ttf", "fonts/timesbi.ttf"):
            try:
                if supabase_storage.download(supabase_storage.ASSETS_BUCKET, obj,
                                             os.path.join(font_dir, os.path.basename(obj))):
                    got.append(obj)
            except Exception:
                pass
        _HAVE_TNR = "fonts/times.ttf" in got
        try:
            supabase_storage.fetch_mdt_form_font(font_dir)       # Tahoma (MDT form) - harmless extra
        except Exception:
            pass
        subprocess.run(["fc-cache", "-f", font_dir], check=False, capture_output=True)
        log.info(f"invoice fonts: real Times New Roman installed={_HAVE_TNR} ({got})")
        if not _HAVE_TNR:
            log.warning("fonts/times.ttf is NOT in decree-assets - Arabic text will use a fallback font "
                        "(upload Windows' times.ttf + timesbd.ttf there for an exact match)")
    except Exception as exc:
        log.warning(f"Font install failed ({exc}) - continuing with fallback fonts")


_WIDEN_JS = (
    "(extra) => { let count = 0; "
    "const bump = (w) => { const m = /^([0-9.]+)px$/.exec(w || ''); if (!m) return null; "
    "const val = parseFloat(m[1]); if (val < 300 || val > 900) return null; "
    "return (val + extra) + 'px'; }; "
    "for (const sheet of document.styleSheets) { let rules; "
    "try { rules = sheet.cssRules; } catch (e) { continue; } "
    "for (const rule of rules) { if (rule.style && rule.style.width) { "
    "const nw = bump(rule.style.width); if (nw) { rule.style.width = nw; count++; } } } } "
    "document.querySelectorAll('[style*=\"width\"]').forEach(el => { "
    "const nw = bump(el.style.width); if (nw) { el.style.width = nw; count++; } }); "
    "return count; }"
)
# Real extent of the VISIBLE content (union of element boxes). scrollWidth/scrollHeight are useless
# here: they never go below the browser window, which is how the PDF ended up 924pt wide with the
# invoice floating in the middle and spilling onto a 2nd page.
_BBOX_JS = (
    "() => { let r = 0, b = 0, l = 1e9; "
    "document.querySelectorAll('body *').forEach(el => { "
    "const cs = getComputedStyle(el); if (cs.display === 'none' || cs.visibility === 'hidden') return; "
    "const q = el.getBoundingClientRect(); if (q.width < 1 || q.height < 1) return; "
    "r = Math.max(r, q.right + window.scrollX); b = Math.max(b, q.bottom + window.scrollY); "
    "l = Math.min(l, q.left + window.scrollX); }); "
    "return { r: r, b: b, l: l === 1e9 ? 0 : l }; }"
)
# FULL text of EVERY row, cell by cell (cells joined with " | " so the date column and the hand-written
# notes column stay separate). No length cap and no row cap. Values typed into <input>/<textarea>/<select>
# are NOT part of innerText, so they are appended explicitly - a hand-written note may live in one of those.
_ROWS_JS = (
    "() => Array.from(document.querySelectorAll('table tr')).map(r => "
    "Array.from(r.children).map(c => { "
    "let t = (c.innerText || '').replace(/\\s+/g, ' ').trim(); "
    "c.querySelectorAll('input,textarea,select').forEach(i => { "
    "const v = (i.value || '').trim(); if (v && !t.includes(v)) t += ' ' + v; }); "
    "return t; }).join(' | '))"
)
_LOGIN_MARKERS = ("اسم المستخدم", "كلمة السر")
_FONT_PROBE_JS = (
    "() => { const f = (e) => e ? getComputedStyle(e).fontFamily : ''; "
    "return { body: f(document.body), td: f(document.querySelector('td')) }; }"
)


class SessionExpired(Exception):
    pass


def _cookies_for_browser(smc: SMCSession) -> list:
    from urllib.parse import urlparse
    host = urlparse(BASE_URL).hostname
    out = []
    for c in smc.s.cookies:
        dom = (c.domain or host).lstrip(".") or host
        out.append({"name": c.name, "value": c.value, "domain": dom, "path": c.path or "/",
                    "secure": bool(getattr(c, "secure", False))})
    return out


_PW = None
_BR = None


def _browser():
    """ONE Chromium for the whole run (launching it per invoice cost ~2 s each)."""
    global _PW, _BR
    if _BR is not None and not _BR.is_connected():
        close_browser()
    if _BR is None:
        from playwright.sync_api import sync_playwright
        _PW = sync_playwright().start()
        _BR = _PW.chromium.launch()
    return _BR


def close_browser() -> None:
    global _PW, _BR
    for obj, meth in ((_BR, "close"), (_PW, "stop")):
        try:
            if obj is not None:
                getattr(obj, meth)()
        except Exception:
            pass
    _PW = _BR = None


def _proxy_handler(smc: SMCSession):
    """Every request the page makes to the SMC host is answered through the SAME `requests` session that already
    listed the invoices and read the Details page successfully. The browser never has to log in by itself - that was
    what sent it to the SMC login page again and again although the session was fine."""
    def handle(route):
        req = route.request
        if not req.url.startswith(BASE_URL):
            return route.continue_()
        try:
            hdr = {k: v for k, v in req.headers.items()
                   if k.lower() in ("accept", "content-type", "x-requested-with", "referer", "origin", "accept-language")}
            r = smc.s.request(req.method, req.url, data=req.post_data_buffer, headers=hdr, timeout=60, allow_redirects=True)
            return route.fulfill(status=r.status_code, body=r.content,
                                 headers={"content-type": r.headers.get("content-type", "application/octet-stream")})
        except Exception as exc:
            log.warning(f"proxy request failed for {req.url[:90]}: {exc}")
            return route.abort()
    return handle


def _render_once(smc: SMCSession, receipt: str, extra: int, mode: str = "proxy") -> bytes:
    br = _browser()
    if True:
        ctx = None
        try:
            # small window so layout is driven by the content, not by a 1280px browser window
            ctx = br.new_context(viewport={"width": 700, "height": 800})
            if mode == "proxy":
                ctx.route("**/*", _proxy_handler(smc))
            else:
                ctx.add_cookies(_cookies_for_browser(smc))
            page = ctx.new_page()
            page.goto(DETAILS_URL.format(rid=receipt), wait_until="load", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            html = page.content()
            # a logged-out page must NEVER be printed as if it were the invoice
            if all(m in html for m in _LOGIN_MARKERS) or "OTP-Auth" in html:
                raise SessionExpired(f"browser landed on the SMC login page for receipt {receipt}")
            if receipt not in ar2en(re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" "))):
                raise SessionExpired(f"receipt {receipt} not found on the rendered page (not the invoice)")

            try:
                rows = page.evaluate(_ROWS_JS)
                log.info(f"invoice {receipt}: item rows as loaded ({len(rows)} rows, full text) = {rows}")
                _rd = os.environ.get("TOPUP_ROWS_DIR")          # optional: one JSON file per invoice for parser tests
                if _rd:
                    import json
                    os.makedirs(_rd, exist_ok=True)
                    with open(os.path.join(_rd, f"{receipt}.json"), "w", encoding="utf-8") as _f:
                        json.dump({"receipt": receipt, "rows": rows}, _f, ensure_ascii=False, indent=1)
            except Exception:
                pass
            page.emulate_media(media="print")                      # the site's own print stylesheet FIRST
            page.add_style_tag(content="html,body{background:#fff !important;margin:0}")
            if not _HAVE_TNR:
                # no real Times New Roman: keep the page's own font, just append an Arabic-capable
                # family so Arabic text does not fall back to the wide DejaVu Sans
                page.add_style_tag(content="html,body,table,td,th,div,span,p,b,strong,label"
                                           "{font-family:'Times New Roman','Liberation Serif','Amiri','Noto Naskh Arabic',serif !important}")
            if extra:
                try:
                    log.info(f"invoice {receipt}: widened {page.evaluate(_WIDEN_JS, extra)} px-width rule(s) by {extra}px")
                except Exception as exc:
                    log.warning(f"width widening failed ({exc})")
            try:
                log.info(f"invoice {receipt}: fonts requested by page {page.evaluate(_FONT_PROBE_JS)}")
            except Exception:
                pass

            bb = page.evaluate(_BBOX_JS)
            w = int(bb["r"] + max(bb["l"], 10)) + 4            # mirror the left gap on the right
            w = max(w, 500)
            page.set_viewport_size({"width": w, "height": 800})   # re-flow at exactly that width
            bb = page.evaluate(_BBOX_JS)
            h = int(bb["b"]) + 20
            log.info(f"invoice {receipt}: content box {w}x{h}px")

            dbg = os.environ.get("TOPUP_DEBUG_DIR")
            pdf = b""
            # A4 exactly like SMC's own print: scale 1 (only shrunk if the page is WIDER than A4), content centred,
            # and a long invoice simply continues on page 2, 3 ... as in the original. No cropping, no tall page.
            # signature_script signs the LAST content page (the one with the signature labels).
            A4_W = 793.7                                     # CSS px at 96 dpi
            MT, MB, MSIDE = 40.0, 40.0, 10.0
            scale = min(1.0, (A4_W - 2 * MSIDE) / w)
            side = max((A4_W - w * scale) / 2.0, 0.0)
            page.add_style_tag(content="tr,img{page-break-inside:avoid;break-inside:avoid} thead{display:table-row-group}")
            pdf = page.pdf(width="210mm", height="297mm", scale=round(scale, 4),
                           margin={"top": f"{MT}px", "bottom": f"{MB}px", "left": f"{side}px", "right": f"{side}px"},
                           print_background=True)
            n_pages = len(re.findall(rb"/Type\s*/Page[^s]", pdf))
            log.info(f"invoice {receipt}: A4 render, scale {scale:.3f}, pages={n_pages}")
            if dbg:
                os.makedirs(dbg, exist_ok=True)
                with open(os.path.join(dbg, f"invoice_{receipt}_00_raw_render.pdf"), "wb") as f:
                    f.write(pdf)
            return pdf
        finally:
            try:
                if ctx is not None:
                    ctx.close()
            except Exception:
                pass


def render_details_pdf(smc: SMCSession, receipt: str) -> bytes:
    """Print the CURRENT (unsigned) Details page with Chromium, using the MDT-form method
    (print media first, widen hard-coded px widths, size the canvas to the real content) plus:
      * login-page guard: if the browser lands on the SMC login screen -> re-login and retry,
        never print it as an invoice;
      * content-box sizing (see _BBOX_JS) so the PDF is exactly one invoice page."""
    _install_invoice_font()
    extra = int(os.environ.get("INVOICE_WIDEN_PX", "55"))
    last = None
    for attempt, mode in ((1, "proxy"), (2, "proxy"), (3, "cookies")):
        try:
            return _render_once(smc, receipt, extra, mode)
        except SessionExpired as e:
            last = e
            log.warning(f"{e} [{mode}] - re-login and retry ({attempt}/3)")
            time.sleep(2)
            if not smc.login():
                log.warning("SMC re-login failed")
        except Exception as e:                                   # browser crashed / page timed out: fresh browser, try again
            last = e
            log.warning(f"render error [{mode}] {type(e).__name__}: {str(e)[:120]} - restarting the browser ({attempt}/3)")
            close_browser()
    raise RuntimeError(f"could not render receipt {receipt}: {last}")


# --------------------------------------------------------------------------- one invoice
def friendly_error(err: str) -> str:
    """Short Arabic hint for an invoice that could not be built (the raw error stays in verification.error)."""
    e = (err or "").lower()
    if "login page" in e or "not found on the rendered page" in e:
        return "انتهت جلسة SMC أثناء طباعة الفاتورة — أعد المحاولة"
    if "timeout" in e:
        return "انتهت مهلة تحميل صفحة الفاتورة — أعد المحاولة"
    if "label" in e or "detect" in e or "column" in e:
        return "تعذّر تحديد خانات التوقيع في صفحة الفاتورة — راجع شكل الفاتورة"
    if "signature" in e or "png" in e or "image" in e:
        return "تعذّر تحميل صور التوقيع/الختم — راجع مخزن signature_files"
    return "تعذّر إنشاء/توقيع الفاتورة: " + (err or "")[:90]


def build_invoice_row(smc, cid, decree, info, nid):
    receipt = info["receipt_id"]
    v = {"listing": {k: info[k] for k in ("status_code", "status_text", "amount", "reg_date", "claim_id")},
         "smc_attachment_ignored": bool(info["has_attachment"])}
    v["details"] = check_details(smc, receipt, decree, nid)
    time.sleep(DELAY)

    row = {"candidate_id": cid, "decree_number": decree, "receipt_id": receipt, "signed_source": "rendered_signed"}
    notes, data = [], None
    try:
        raw = render_details_pdf(smc, receipt)                 # the CURRENT, unsigned invoice, ONE A4 page like SMC's own print
        v["rendered"] = inspect_pdf(raw)
        v["rendered_has_receipt"] = pdf_has_number(raw, receipt)
        data, layout = sign_with_script(raw)
        v["layout"] = layout
    except Exception as e:
        v["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        row.update(status="FAILED", verification=v,
                   message=friendly_error(v["error"]))
        return row, None
    time.sleep(DELAY)

    final = inspect_pdf(data)
    v["final"] = final
    added_blue = final["blue_px"] - v["rendered"]["blue_px"]
    v["added_blue_px"] = added_blue
    d = v["details"]
    ties_ok = bool(d.get("details_ok") and d.get("receipt_in_details")
                   and d.get("decree_in_details") is not False and d.get("nid_in_details") is not False)
    listing_ok = info["decree_id"] in ("", decree)
    signed_ok = added_blue >= SIGNED_MIN_BLUE_PX
    lay = v["layout"]
    layout_ok = lay["columns"] >= 4 and lay["ascending"] and lay["span_frac"] >= LAYOUT_MIN_SPAN_FRAC
    page_ok = bool(v["rendered_has_receipt"])

    if not ties_ok:
        notes.append("لم يتطابق رقم الفاتورة/القرار/الرقم القومي مع صفحة التفاصيل — راجع الملف")
    if not listing_ok:
        notes.append(f"الفاتورة مسجلة على قرار آخر ({info['decree_id']})")
    if not page_ok:
        notes.append("نص الـ PDF المُنشأ لا يحتوي رقم الفاتورة (قد تكون صفحة غير صحيحة) — راجع الملف")
    if not signed_ok:
        notes.append("لم تُضَف التوقيعات بشكل كافٍ على الصفحة — راجع الملف")
    if not layout_ok:
        notes.append("مواضع خانات التوقيع غير معتادة (قد تكون التوقيعات في مكان خاطئ) — راجع الملف")
    row.update(bytes=len(data), page_count=final["pages"], sha256=hashlib.sha256(data).hexdigest(),
               verification=v, status="OK" if (ties_ok and listing_ok and page_ok and signed_ok and layout_ok) else "REVIEW",
               message=" | ".join(dict.fromkeys(notes)) or None,
               fetched_at=datetime.now(timezone.utc).isoformat())
    return row, data


_SIGNED = {}      # receipt_id -> (row, pdf bytes) built earlier in THIS run (two entries often share an invoice)


def problem_hint(items) -> str:
    """items: [(decree, receipt_id, row)] -> one short line telling WHY the entry is not READY."""
    bad = [(d, rid, r) for d, rid, r in items if r.get("status") != "OK"]
    if not bad:
        return ""
    parts = [f"{rid}: {(r.get('message') or 'يحتاج مراجعة').split(' | ')[0]}" for _, rid, r in bad[:3]]
    more = f" (+{len(bad) - 3} أخرى)" if len(bad) > 3 else ""
    return f"{len(bad)} من {len(items)} فاتورة لم تكتمل — " + " ؛ ".join(parts) + more


def save_row(cid, receipt, row):
    ex = sb.select(INV, select="id", filters={"candidate_id": f"eq.{cid}", "receipt_id": f"eq.{receipt}"}, limit=1)
    (sb.update(INV, ex[0]["id"], row) if ex else sb.insert(INV, row))



def purge_candidate(cid, s3, bucket):
    """Remove the stored INVOICES of this entry (R2 file + decree_topup_invoices row). Previous-decree REPORT rows
    (doc_kind = PREV_REPORT, owned by topup_report_fetch.py) are never touched."""
    rows = sb.select(INV, select="id,r2_key", limit=500,
                     filters={"candidate_id": f"eq.{cid}", "or": "(doc_kind.is.null,doc_kind.neq.PREV_REPORT)"})
    hdr = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
    for r in rows:
        if r.get("r2_key"):
            try:
                s3.delete_object(Bucket=bucket, Key=r["r2_key"])
            except Exception as e:
                log.warning(f"  candidate {cid}: could not delete {r['r2_key']}: {e}")
        resp = requests.delete(f"{SUPABASE_URL}/rest/v1/{INV}?id=eq.{r['id']}", headers=hdr, timeout=30)
        resp.raise_for_status()
    log.info(f"  candidate {cid}: purged {len(rows)} stored invoice(s)")
    return len(rows)


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
        log.info(f"  candidate {cid}: decree {d}: {len(rows)} invoice(s) listed on SMC (all are collected, whatever their status)")
        for x in rows:
            log.info(f"      invoice {x['receipt_id']}: status_code={x['status_code']!r} status_text={x['status_text']!r} "
                     f"amount={x['amount']} attachment={'yes' if x['has_attachment'] else 'no'}")
        if not rows:
            problems.append(f"{d}: لا توجد فواتير")
        work.extend((d, x) for x in rows)
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
                              "evidence_message": "لا توجد فواتير على SMC للقرارات السابقة — " + " ; ".join(problems)})
        return
    built = []
    for decree, info in work:
        hit = _SIGNED.get(info["receipt_id"])
        if hit:                                              # same invoice already rendered+signed in this run
            row, data = dict(hit[0]), hit[1]
            row.update(candidate_id=cid, decree_number=decree)
            log.info(f"  candidate {cid}: invoice {info['receipt_id']} reused from this run")
        else:
            row, data = build_invoice_row(smc, cid, decree, info, nid)
            if data is not None:
                _SIGNED[info["receipt_id"]] = (dict(row), data)
        if data is not None:
            key = f"topup-invoices/{cid}/{decree}_{info['receipt_id']}.pdf"
            s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/pdf")
            row["r2_key"] = key
        save_row(cid, info["receipt_id"], row)
        built.append((decree, info["receipt_id"], row))
        log.info(f"  candidate {cid}: decree {decree} invoice {info['receipt_id']} -> {row.get('status')} "
                 f"[{row.get('signed_source')}, {row.get('page_count')} page(s), {row.get('bytes')} bytes]"
                 + (f" - {row['message']}" if row.get("message") else ""))
    _rpc("topup_recompute_evidence", {"p_id": cid})
    try:
        end = sb.select(CAND, select="evidence_status,evidence_message", filters={"id": f"eq.{cid}"}, limit=1)
        hint = problem_hint(built)
        if end and end[0]["evidence_status"] != "READY" and hint:       # say WHAT went wrong, in one line
            sb.update(CAND, cid, {"evidence_message": hint[:480]})
            end[0]["evidence_message"] = hint
        if end:
            log.info(f"  candidate {cid}: RESULT evidence_status={end[0]['evidence_status']} "
                     f"{end[0].get('evidence_message') or ''} - open the entry on the page to view the stored PDFs")
    except Exception:
        pass


def write_run_summary(results):
    """The job itself stays green when an ENTRY ends FAILED / NEEDS_REVIEW (that is a data outcome shown
    on the page, not a crash) - so say it loudly here: warning annotations + the run's summary tab."""
    if not results:
        return
    good = [r for r in results if r[1] == "READY"]
    bad = [r for r in results if r[1] != "READY"]
    log.info(f"SUMMARY: {len(good)} READY, {len(bad)} need attention out of {len(results)}")
    for cid, st, msg in bad:
        print(f"::warning title=Top-up entry {cid} - {st}::{msg or 'see the entry on the page'}")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf8") as f:
                f.write(f"### Top-up invoices: {len(good)} READY / {len(bad)} need attention\n\n"
                        "| entry | result | note |\n|---|---|---|\n")
                for cid, st, msg in results:
                    f.write(f"| {cid} | {st} | {str(msg).replace('|', '/')[:300]} |\n")
        except Exception:
            pass


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
    s3, bucket = _r2(), os.environ["R2_BUCKET_NAME"]
    mode = (os.environ.get("EVIDENCE_MODE") or "").strip().lower()      # "" | "refetch" | "clear"  (set by the page's buttons)
    if only and mode in ("refetch", "clear"):
        flt.pop("evidence_status", None)                                # any state: the old files are being replaced/removed
        for cid in only:
            purge_candidate(cid, s3, bucket)
            if mode == "refetch":
                sb.update(CAND, cid, {"evidence_status": "PENDING", "evidence_message": None})
            else:
                sb.update(CAND, cid, {"evidence_status": "NO_PREVIOUS_DECREE",
                                      "evidence_message": "تم حذف الفواتير المخزنة يدوياً — أعد الطلب أو أدخل القرارات/الإيصالات الصحيحة"})
        if mode == "clear":
            return
        flt["evidence_status"] = "eq.PENDING"
    requeue_no_previous(only)
    limit = max(MAX_CANDIDATES, len(only)) if only else MAX_CANDIDATES      # an explicit selection is never cut short
    rows = sb.select(CAND, select="*", filters=flt, order="id.asc", limit=limit)
    log.info(f"{len(rows)} candidate(s) to fetch evidence for (time budget {TIME_BUDGET_MIN} min).")
    results = []
    deadline = time.time() + TIME_BUDGET_MIN * 60
    for i, c in enumerate(rows):
        if time.time() > deadline:
            log.warning(f"time budget reached - {len(rows) - i} entr(ies) stay PENDING for the next run")
            break
        try:
            process(c, smc, s3, bucket)
            end = sb.select(CAND, select="evidence_status,evidence_message", filters={"id": f"eq.{c['id']}"}, limit=1)
            results.append((c["id"], (end[0]["evidence_status"] if end else "?"),
                            (end[0].get("evidence_message") or "") if end else ""))
        except Exception as e:
            log.exception(f"candidate {c['id']} failed")
            sb.update(CAND, c["id"], {"evidence_status": "FAILED",
                                      "evidence_message": f"{type(e).__name__}: {str(e)[:200]}"})
            results.append((c["id"], "FAILED", f"{type(e).__name__}: {str(e)[:200]}"))
    write_run_summary(results)


def requeue_no_previous(only) -> int:
    """Entries marked NO_PREVIOUS_DECREE BEFORE the patient lookup existed (or before it ran) stay stuck even though the
    lookup now lists matching decrees. Put them back in the queue as soon as the lookup is DONE and shows a match."""
    flt = {"evidence_status": "eq.NO_PREVIOUS_DECREE", "lookup_status": "eq.DONE", "protocol_key": "not.is.null",
           "status": "not.in.(SUBMITTED,DISMISSED)"}
    if only:
        flt["id"] = "in.(" + ",".join(map(str, only)) + ")"
    n = 0
    try:
        for c in sb.select(CAND, select="id,source_case_id,kind", filters=flt, limit=500) or []:
            if c.get("kind") == "PREV_REPORT":
                continue
            if _lookup_decrees(c):
                sb.update(CAND, c["id"], {"evidence_status": "PENDING", "evidence_message": None})
                n += 1
    except Exception as e:
        log.warning(f"requeue of NO_PREVIOUS_DECREE entries skipped: {e}")
    if n:
        log.info(f"{n} entr(ies) that had NO_PREVIOUS_DECREE now have matching decrees in the lookup - queued again")
    return n


if __name__ == "__main__":
    try:
        main()
    finally:
        close_browser()
