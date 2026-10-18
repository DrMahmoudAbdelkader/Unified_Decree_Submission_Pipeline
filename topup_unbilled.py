#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_unbilled.py
==========================================================================
CHECKPOINT that runs BEFORE the invoices of any previous decree are read.

THE PROBLEM
  On SMC a dispensing is first ENTERED on the decree (DecreeTreatmentProcedure page = the items), and only becomes
  an invoice when somebody opens the decree's «فاتورة» page and presses save (HospDecreeReceipts/Create).
  If the user forgot that second step the decree has consumed value (value left <> decree value) but
      - no invoice at all  -> the entry fails with "لا توجد فواتير على SMC للقرارات السابقة", or
      - an older invoice only -> only that one is extracted and the newer unbilled items are silently missing.

THE FIX (exactly the sequence recorded in saving_the_bill_then_extracting_it.har)
  1. GET  /smc/HospDecreeReceipts/Create?decreeId=<decree>              the page itself (title box = patient name)
  2. GET  /smc//HospDecreeReceipts/CheckDecreeTreatmentProcedures?decreeId=<d>&title=<t>
          {"success":"success"}                         -> there ARE items not yet in a bill, title is free
          {"fail":"no data in decreeTreatmentProcedureList"} -> nothing waiting: nothing to do
          {"fail":"HasExceedValue"}                     -> the decree carries an exceed value: SMC refuses a new bill
          anything else                                 -> the title is already used on this decree
  3. POST /smc//HospDecreeReceipts/CheckPendingReceipts  decreeId=<d>   true -> an unsent pending bill blocks a new one
  4. POST /smc//HospDecreeReceipts/Create   {"TITLE": t, "DECREEID": <d>}
  5. re-list the decree's bills and confirm the new title is really there.

  TITLE FORMAT:  "<patient name> - <patient national id>"   e.g.  "جيهان حسين ابراهيم  خليل - 27511192301382"
  The name is read from the Create page's own title box (exactly what the site pre-fills), the national id from the
  decree page (#CitizenSSN). A title is unique per decree on SMC, so if a previous run already used this title the next
  ones become  "<title> (2)", "<title> (3)" ...

THEN the ordinary extraction (topup_evidence_fetch.list_receipts) lists ALL the decree's bills, the new one included.

SWITCHES
  TOPUP_BILL_UNBILLED = 1 (default) create the bill | dry = only detect and log, never write | 0 = checkpoint off

RESULT STATUS (ensure_billed(...)["status"])
  NOTHING     no unbilled items                     -> carry on
  CREATED     bill created and seen in the listing  -> carry on
  DRY_RUN     unbilled items exist, nothing written
  BLOCKED     unbilled items exist but SMC refuses a new bill (pending unsent bill / exceed value / title clash)
  FAILED      network / parse / the bill did not appear
  SKIPPED     checkpoint off
  Callers must NOT treat BLOCKED / FAILED / DRY_RUN as "evidence complete": see blocking().
"""
from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional

log = logging.getLogger("topup_unbilled")

BLOCKING = ("BLOCKED", "FAILED", "DRY_RUN")
MAX_TITLE_TRIES = 5
DELAY = 0.4
LAST: Dict[str, dict] = {}                      # decree number -> last result in this process (for the caller's message)


def mode() -> str:
    v = (os.environ.get("TOPUP_BILL_UNBILLED") or "1").strip().lower()
    if v in ("0", "false", "off", "no"):
        return "off"
    if v in ("dry", "dry-run", "dryrun", "log"):
        return "dry"
    return "on"


# --------------------------------------------------------------------------- tiny SMC helpers
def _send(smc, method: str, url: str, **kw):
    """One request; if the session died, log in again and repeat once (same pattern as list_receipts)."""
    r = smc.s.request(method, url, timeout=60, **kw)
    if smc._session_expired(r):
        log.warning("session expired during the unbilled-items check - re-login")
        if not smc.login():
            raise RuntimeError("SMC re-login failed")
        r = smc.s.request(method, url, timeout=60, **kw)
    return r


def _xhr(referer: str, base: str, extra: Optional[dict] = None) -> dict:
    h = {"X-Requested-With": "XMLHttpRequest", "Referer": referer, "Origin": base}
    h.update(extra or {})
    return h


def parse_title_default(page_html: str) -> str:
    """Patient name as the site pre-fills it in the «عنوان الفاتورة» box (kept byte for byte, only the ends trimmed)."""
    m = re.search(r'<input[^>]*\bid="TITLE"[^>]*\bvalue="([^"]*)"', page_html or "", re.S) or \
        re.search(r'<input[^>]*\bvalue="([^"]*)"[^>]*\bid="TITLE"', page_html or "", re.S)
    return _html.unescape(m.group(1)).strip() if m else ""


def parse_citizen_ssn(page_html: str) -> str:
    m = re.search(r'id="CitizenSSN"[^>]*\bvalue="(\d{10,})"', page_html or "") or \
        re.search(r'\bvalue="(\d{10,})"[^>]*id="CitizenSSN"', page_html or "")
    return m.group(1) if m else ""


def parse_check(text: str) -> dict:
    """CheckDecreeTreatmentProcedures answer -> {'ok': bool, 'fail': str|None}. 'fail' is the site's own code."""
    t = (text or "").strip()
    try:
        j = json.loads(t)
        if isinstance(j, dict):
            if j.get("success"):
                return {"ok": True, "fail": None}
            return {"ok": False, "fail": str(j.get("fail") or "")}
    except ValueError:
        pass
    if '"success"' in t:
        return {"ok": True, "fail": None}
    return {"ok": False, "fail": t[:80]}


def _ws(s) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _res(status: str, decree: str, **kw) -> dict:
    out = dict(status=status, decree=str(decree), title=None, receipt_id=None, message="")
    out.update(kw)
    LAST[str(decree)] = out
    return out


# --------------------------------------------------------------------------- the checkpoint
def ensure_billed(smc, decree: str, base_url: str, list_fn) -> dict:
    """Make sure every item entered on `decree` is inside a bill. Never raises.
    list_fn(smc, decree, ...) must be the plain receipt lister (it is used to confirm the new bill)."""
    decree = str(decree)
    m = mode()
    if m == "off":
        return _res("SKIPPED", decree)
    try:
        create_url = f"{base_url}/smc/HospDecreeReceipts/Create?decreeId={decree}"
        ref = create_url
        page = _send(smc, "GET", create_url, headers={"Referer": f"{base_url}/smc/DecreeTreatmentProcedure/Create/{decree}"})
        if page.status_code != 200:
            return _res("FAILED", decree, message=f"صفحة الفواتير HTTP {page.status_code}")
        name = parse_title_default(page.text)
        time.sleep(DELAY)

        chk_url = f"{base_url}/smc//HospDecreeReceipts/CheckDecreeTreatmentProcedures"
        # first probe with a throw-away-free title: the real one needs the national id, which costs one more page, so
        # probe with the name alone ONLY to learn whether anything is waiting (title clash cannot happen for a bare name
        # unless an old bill carries it - in that case the answer is a clash, which still proves nothing is decided yet).
        probe = parse_check(_send(smc, "GET", chk_url, params={"decreeId": decree, "title": name or "x"},
                                  headers=_xhr(ref, base_url)).text)
        if not probe["ok"] and "no data in decreeTreatmentProcedureList" in (probe["fail"] or ""):
            log.info(f"  [unbilled] decree {decree}: every entered item is already inside a bill")
            return _res("NOTHING", decree)
        if not probe["ok"] and probe["fail"] == "HasExceedValue":
            msg = "على القرار قيمة تجاوز مسجلة — SMC يمنع إنشاء فاتورة جديدة لبنود غير مفوترة"
            log.warning(f"  [unbilled] decree {decree}: {msg}")
            return _res("BLOCKED", decree, message=msg)
        time.sleep(DELAY)

        # something is waiting (or the bare name clashes): the national id is needed for the real title
        dpage = _send(smc, "GET", f"{base_url}/smc/DecreeTreatmentProcedure/Create/{decree}")
        nid = parse_citizen_ssn(dpage.text) if dpage.status_code == 200 else ""
        if not (name and nid):
            return _res("FAILED", decree, message=f"تعذّر قراءة اسم المريض/رقمه القومي من SMC (name={bool(name)}, nid={bool(nid)})")
        base_title = f"{name} - {nid}"

        if _send(smc, "POST", f"{base_url}/smc//HospDecreeReceipts/CheckPendingReceipts", data={"decreeId": decree},
                 headers=_xhr(ref, base_url, {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
                 ).text.strip().lower() == "true":
            msg = "يوجد بنود غير مفوترة لكن توجد فاتورة معلّقة لم تُرسل بعد على SMC — أرسلها أولاً ثم أعد الطلب"
            log.warning(f"  [unbilled] decree {decree}: {msg}")
            return _res("BLOCKED", decree, message=msg)

        title = None
        for n in range(1, MAX_TITLE_TRIES + 1):
            cand = base_title if n == 1 else f"{base_title} ({n})"
            r = parse_check(_send(smc, "GET", chk_url, params={"decreeId": decree, "title": cand},
                                  headers=_xhr(ref, base_url)).text)
            if r["ok"]:
                title = cand
                break
            if "no data in decreeTreatmentProcedureList" in (r["fail"] or ""):
                return _res("NOTHING", decree)
            if r["fail"] == "HasExceedValue":
                return _res("BLOCKED", decree, message="على القرار قيمة تجاوز مسجلة — SMC يمنع إنشاء فاتورة جديدة")
            time.sleep(DELAY)                                  # title already used on this decree -> next variant
        if not title:
            return _res("BLOCKED", decree, message=f"عنوان الفاتورة «{base_title}» مستخدم من قبل على هذا القرار ({MAX_TITLE_TRIES} محاولات)")

        if m == "dry":
            log.warning(f"  [unbilled] decree {decree}: DRY RUN - items are waiting, would create the bill «{title}»")
            return _res("DRY_RUN", decree, title=title, message="بنود غير مفوترة — وضع التجربة لم ينشئ الفاتورة")

        log.info(f"  [unbilled] decree {decree}: items entered but not billed -> creating the bill «{title}»")
        cr = _send(smc, "POST", f"{base_url}/smc//HospDecreeReceipts/Create",
                   data=json.dumps({"TITLE": title, "DECREEID": int(decree)}, ensure_ascii=False).encode("utf-8"),
                   headers=_xhr(ref, base_url, {"Content-Type": "application/json"}))
        if cr.status_code != 200:
            return _res("FAILED", decree, title=title, message=f"إنشاء الفاتورة HTTP {cr.status_code}")
        time.sleep(DELAY)
        for x in list_fn(smc, decree, bill_unbilled=False):          # trust nothing: the bill must appear in the listing
            if _ws(x.get("title")) == _ws(title):          # the listing parser collapses runs of spaces (the name has a double one)
                log.info(f"  [unbilled] decree {decree}: bill {x['receipt_id']} created ({x.get('amount')}) status={x.get('status_text')!r}")
                return _res("CREATED", decree, title=title, receipt_id=x["receipt_id"],
                            message=f"تم إنشاء الفاتورة {x['receipt_id']} ({x.get('amount')}) للبنود غير المفوترة")
        return _res("FAILED", decree, title=title, message="أُرسل طلب إنشاء الفاتورة لكنها لم تظهر في قائمة فواتير القرار")
    except Exception as e:
        log.warning(f"  [unbilled] decree {decree}: check failed ({type(e).__name__}: {str(e)[:120]})")
        return _res("FAILED", decree, message=f"{type(e).__name__}: {str(e)[:120]}")


def blocking(decrees: List[str]) -> List[dict]:
    """Results of this process for the given decrees that mean 'the bills of this decree are NOT complete'."""
    return [LAST[str(d)] for d in decrees if str(d) in LAST and LAST[str(d)]["status"] in BLOCKING]
