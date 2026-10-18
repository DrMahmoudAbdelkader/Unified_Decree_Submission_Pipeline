#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_gate.py   (kind-aware: TOPUP / PREV_REPORT / ID_CARD)
==========================================================================
The ONE place the submission pipeline asks "is this request an answer to an administrative
letter, and is everything it needs in place?". Same four entry points the pipeline already
calls — decree_common.run_finalize_stages() and decree_submission_prepare.py need no change:

    assert_ready(case_id)                 raise (-> FAILED + operator requirement) when not ready
    with_invoices(case_id, patient_pdf)   path of the PDF to merge after the MDT + report
    cycles_statement(case_id)             English, dated dose sentence for the medical report (TOPUP only; None when nothing is
                                          left to receive -> invoices only, like a supportive decree)
    split_ready(cases)                    (ready_cases, blocked) for prepare's bulk run

What each kind needs before its retry request may be sent (answer of the DB function
topup_evidence_gate(case_id), migration 07):

    TOPUP         cycle counts typed  +  signed invoices READY      -> invoices are appended
    PREV_REPORT   the previous decree's printed report READY        -> the report is appended
    ID_CARD       «تم التصحيح» pressed on the page                  -> nothing appended (the
                  corrected card is already inside the patient's own documents)

ADJUSTMENTS applied on top of the DB gate (see _adjust()):
    * NO PREVIOUS DECREE  - the patient has no earlier decree of the same plan (and the patient lookup finished): the
                            letter cannot be answered with bills, so the request is sent NORMALLY (no invoices appended,
                            no dose sentence) instead of being blocked.
    * AUTO-RESOLVED       - topup_resolution.py closed the entry (a later same-plan decree already has enough value
                            left): the request is NOT sent; the message tells the operator why.
    * CONTINUOUS PLANS    - hormonal / Zoladex / hormonal+Zoladex (no upper dose limit): no cycle counts required, no dose
                            review hold, and NO "received / yet to be received" sentence in the medical report - only the
                            previous decree's bills are attached.

A request that is not linked to any candidate is not a top-up: every function is then a no-op
and the pipeline behaves exactly as before. If the DB function itself cannot be called (migration
not applied) the gate FAILS OPEN with a warning, so normal requests are never blocked by it.
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import List, Optional, Tuple

import requests

log = logging.getLogger("topup_gate")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""

# Placeholder wording for the report sentence. If your previous topup_gate.py used a different
# sentence, copy it here — nothing else depends on it.
CYCLES_TEMPLATE = "المستلم من القرار السابق: {received} ({unit})، والمتبقي المطلوب: {remaining} ({unit})."
# Used when the automatic dose count knows how many doses the previous decrees authorised in total.
CYCLES_TEMPLATE_TOTALS = ("إجمالي الجرعات المعتمدة بالقرارات السابقة: {supposed} ({unit})، المستلم منها: {received}، "
                          "والمتبقي: {remaining}.")

KIND_LABEL = {"TOPUP": "تجديد في نهاية المدة", "PREV_INCLUSIVE": "القرار السابق شامل", "PREV_REPORT": "طلب تقرير القرار السابق",
              "ID_CARD": "بطاقة الرقم القومي"}


# message prefixes written by the pipeline into decree_topup_candidates.evidence_message
AUTO_RESOLVED_PREFIX = "تم الحل تلقائياً"          # == topup_resolution.AUTO_PREFIX
NO_PREVIOUS_PREFIX = "لا يوجد قرار سابق"            # == the text topup_evidence_fetch.process() writes when nothing matched
_CONTINUOUS_DEFAULT = ("hormonal", "zoladex", "hormonal_zoladex")


# kinds that run the same workflow: cycle counts + signed invoices + report sentence
TOPUP_LIKE = ("TOPUP", "PREV_INCLUSIVE")


class TopupNotReady(Exception):
    """Raised by assert_ready(); the pipeline turns any exception into FAILED + a requirement."""


# --------------------------------------------------------------------------- Supabase
def _hdr():
    return {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}


def _rpc(name: str, body: dict):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/{name}", json=body, headers=_hdr(), timeout=30)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
    return r.json()


def _gate_raw(case_id: int) -> dict:
    """{'is_topup': False} for a normal request. Fails open (with a warning) if the RPC is missing.

    A READY_TO_SUBMIT request that nobody linked to a letter (it was created in the normal entry module, not from the top-up
    page) but belongs to the same patient + plan of an open letter is linked here (topup_autolink_case, migration 14), so it is
    submitted AFTER the top-up logic: counts, signed invoices, manual bill and report sentence. TOPUP_AUTOLINK_READY=0 disables."""
    try:
        g = _rpc("topup_evidence_gate", {"p_case_id": int(case_id)})
        g = g if isinstance(g, dict) else {"is_topup": False}
    except Exception as exc:
        log.warning(f"topup gate skipped for case {case_id}: {exc}")
        return {"is_topup": False}
    if not g.get("is_topup") and (os.environ.get("TOPUP_AUTOLINK_READY") or "1").strip() not in ("0", "false", "off", "no"):
        try:
            if _rpc("topup_autolink_case", {"p_case_id": int(case_id)}):
                log.info(f"  case {case_id}: linked automatically to an open administrative letter of the same patient + plan")
                g2 = _rpc("topup_evidence_gate", {"p_case_id": int(case_id)})
                if isinstance(g2, dict):
                    g = g2
        except Exception as exc:                                   # migration 14 not applied: behave as before
            log.debug(f"topup autolink skipped for case {case_id}: {exc}")
    return g


def _env_on(name: str, default: bool = True) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return default if not v else v not in ("0", "false", "off", "no")


def _is_continuous(protocol: Optional[str]) -> bool:
    try:
        import topup_overlap as ov
        return ov.is_continuous(protocol)
    except Exception:
        raw = os.environ.get("TOPUP_CONTINUOUS_PROTOCOLS")
        names = [x.strip().lower() for x in raw.split(",") if x.strip()] if raw is not None else list(_CONTINUOUS_DEFAULT)
        return (protocol or "").strip().lower() in names


def _candidate(candidate_id) -> dict:
    """The few candidate columns the adjustments need; {} on any problem (= no adjustment, old behaviour)."""
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/decree_topup_candidates", headers=_hdr(), timeout=30, params={
            "select": "status,evidence_status,evidence_message,lookup_status,manual_decree_numbers,manual_receipt_ids,protocol_key",
            "id": f"eq.{int(candidate_id)}", "limit": 1})
        rows = r.json() if r.ok else []
        return rows[0] if rows else {}
    except Exception as exc:
        log.warning(f"topup candidate {candidate_id} not readable ({exc}) - gate adjustments skipped")
        return {}


def _adjust(g: dict) -> dict:
    """See the module doc (ADJUSTMENTS). Only touches TOPUP / PREV_INCLUSIVE entries."""
    if not g.get("is_topup") or not g.get("candidate_id"):
        return g
    if (g.get("kind") or "TOPUP") not in TOPUP_LIKE:
        return g
    cand = _candidate(g["candidate_id"])
    if not cand:
        return g
    msg = (cand.get("evidence_message") or "").strip()
    protocol = g.get("protocol_key") or cand.get("protocol_key")
    g["continuous"] = _is_continuous(protocol)

    if cand.get("status") == "RESOLVED" and msg.startswith(AUTO_RESOLVED_PREFIX):
        g["auto_resolved"] = True
        g["auto_resolved_message"] = msg
        g["ready"] = False
        return g

    if (not g.get("ready") and _env_on("TOPUP_NO_PREVIOUS_PASS")
            and cand.get("evidence_status") == "NO_PREVIOUS_DECREE" and msg.startswith(NO_PREVIOUS_PREFIX)
            and cand.get("lookup_status") == "DONE"
            and not cand.get("manual_decree_numbers") and not cand.get("manual_receipt_ids")):
        g["no_previous"] = True
        g["ready"] = True
        return g

    if (g["continuous"] and not g.get("ready") and g.get("evidence_status") == "READY"
            and g.get("invoices_ok") and not g.get("invoices_bad")):
        g["ready"] = True                      # continuous plan: bills are enough, no cycle counts needed
    return g


def _gate(case_id: int) -> dict:
    return _adjust(_gate_raw(case_id))


def _documents(candidate_id: int) -> List[dict]:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/decree_topup_invoices", headers=_hdr(), timeout=60, params={
        "select": "id,decree_number,receipt_id,r2_key,doc_kind", "candidate_id": f"eq.{candidate_id}",
        "status": "eq.OK", "r2_key": "not.is.null", "order": "decree_number.asc,id.asc"})
    r.raise_for_status()
    return r.json() or []


def _unit_word(protocol_key: Optional[str]) -> str:
    if not protocol_key:
        return "جرعة"
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/decree_topup_protocols", headers=_hdr(), timeout=30,
                         params={"select": "unit_word_ar", "protocol_key": f"eq.{protocol_key}", "limit": 1})
        rows = r.json() if r.ok else []
        return (rows[0].get("unit_word_ar") if rows else None) or "جرعة"
    except Exception:
        return "جرعة"


# --------------------------------------------------------------------------- messages
def not_ready_message(g: dict) -> str:
    if g.get("auto_resolved"):
        return g.get("auto_resolved_message") or "تم الحل تلقائياً — لا حاجة لطلب جديد."
    kind = g.get("kind") or "TOPUP"
    head = f"طلب «{KIND_LABEL.get(kind, kind)}» لا يمكن إرساله بعد: "
    if kind == "ID_CARD":
        return head + "لم يُضغط «تم التصحيح» بعد استبدال صورة بطاقة الرقم القومي في مستندات المريض."
    if kind == "PREV_REPORT":
        return head + (f"تقرير القرار السابق غير جاهز (الحالة: {g.get('evidence_status')}). "
                       f"افتح صفحة «متابعة الخطابات الإداريه» وتأكد من جلب التقرير أو اعتماده.")
    missing = []
    if g.get("cycles_received") is None or g.get("cycles_remaining") is None:
        missing.append("عدد الدورات (المستلمة / المتبقية)")
    if g.get("evidence_status") != "READY" or not g.get("invoices_ok") or g.get("invoices_bad"):
        missing.append(f"الفواتير الموقّعة (الحالة: {g.get('evidence_status')}، سليمة: {g.get('invoices_ok')}، "
                       f"تحتاج مراجعة: {g.get('invoices_bad')})")
    return head + " و".join(missing or ["بيانات غير مكتملة"]) + ". أكملها من صفحة «متابعة الخطابات الإداريه»."


# --------------------------------------------------------------------------- automatic dose count
def _dose_hold(case_id: int, g: dict) -> Optional[str]:
    """Review hold of the automatic dose count (topup_dose_calc.py), or None. First recounts with the CURRENT unbilled
    orders so a dispensing saved after the evidence run is included. Never raises: any problem = no hold."""
    try:
        if not g.get("is_topup") or g.get("kind") not in TOPUP_LIKE or not g.get("candidate_id"):
            return None
        if g.get("continuous") or g.get("no_previous"):
            return None                                     # no dose counts matter for these: nothing to review
        import topup_dose_calc as dc
        if not dc.enabled():
            return None
        cid = int(g["candidate_id"])
        if dc.refresh_unbilled(cid):
            g = _gate(case_id)                              # the numbers written to the entry changed
        return dc.hold_message(cid, g.get("cycles_received"), g.get("cycles_remaining"))
    except Exception as exc:
        log.warning(f"dose-count hold skipped for case {case_id}: {type(exc).__name__}: {exc}")
        return None


# --------------------------------------------------------------------------- entry points
def assert_ready(case_id: int) -> None:
    g = _gate(case_id)
    if g.get("is_topup") and not g.get("ready"):
        raise TopupNotReady(not_ready_message(g))
    hold = _dose_hold(case_id, g)
    if hold:
        raise TopupNotReady(hold)


_UNIT_EN = {"جرعه": "dose", "جرعة": "dose", "دورة": "cycle", "دوره": "cycle"}


def _plural(n, word: str) -> str:
    return f"{word}" if float(n) == 1 else f"{word}s"


def cycles_statement(case_id: int) -> Optional[str]:
    """English sentence for the medical report:
         Patient had received X doses on the following dates: a, b, c. Y doses are yet to be received by the patient.
    X / Y are the numbers on the entry (typed or calculated); the dates are the counted dose dates (invoices + unbilled
    orders) and are listed only when they agree with X, so the sentence never contradicts itself."""
    g = _gate(case_id)
    if not g.get("is_topup") or g.get("kind") not in TOPUP_LIKE:
        return None
    if g.get("continuous") or g.get("no_previous"):
        return None                  # hormonal / Zoladex: continuous regimen - never state received / yet-to-be-received doses
    if g.get("cycles_received") is None or g.get("cycles_remaining") is None:
        return None
    rec, rem = int(g["cycles_received"]), int(g["cycles_remaining"])
    if rem <= 0:
        # nothing is left to receive (all authorised doses given, or more): like a supportive decree the request carries the
        # previous decrees' invoices only - NO received / yet-to-be-received sentence in the medical report.
        return None
    word = _UNIT_EN.get(_unit_word(g.get("protocol_key")), "dose")
    dates = []
    try:
        import topup_dose_calc as dc
        if dc.enabled():
            dates = dc.dose_dates(int(g["candidate_id"]))
    except Exception as exc:
        log.warning(f"dose dates unavailable for case {case_id}: {exc}")
    if dates and len(dates) == rec:
        first = f"Patient had received {rec} {_plural(rec, word)} on the following {'date' if rec == 1 else 'dates'}: " + ", ".join(d.strftime("%d/%m/%Y") for d in dates) + "."
    else:
        first = f"Patient had received {rec} {_plural(rec, word)}."
    return f"{first} {rem} {_plural(rem, word)} {'is' if rem == 1 else 'are'} yet to be received by the patient."


def with_invoices(case_id: int, patient_pdf_path: str) -> str:
    """patient document + the stored evidence (signed invoices, or the previous decree's report),
    written to a NEW temp file — the patient's permanent cached file is never modified. For a
    normal request or an ID_CARD letter the original path is returned untouched."""
    g = _gate(case_id)
    if not g.get("is_topup") or g.get("kind") not in ("TOPUP", "PREV_INCLUSIVE", "PREV_REPORT"):
        return patient_pdf_path
    if g.get("no_previous"):
        log.info(f"  [top-up] case {case_id}: no previous decree of the same plan - request sent normally (no invoices appended).")
        return patient_pdf_path
    docs = _documents(int(g["candidate_id"]))
    if not docs:
        raise TopupNotReady(not_ready_message({**g, "ready": False}))

    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:                       # the repo's requirements may carry PyPDF2 instead
        from PyPDF2 import PdfReader, PdfWriter
    import boto3
    s3 = boto3.client(
        "s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
    bucket = os.environ["R2_BUCKET_NAME"]

    import io
    writer = PdfWriter()
    added = 0
    # evidence FIRST: the pipeline puts MDT form + medical report in front of whatever this returns,
    # so the final order is  MDT -> report -> invoices (-> manual bill) -> patient document.
    for d in docs:
        body = s3.get_object(Bucket=bucket, Key=d["r2_key"])["Body"].read()
        for page in PdfReader(io.BytesIO(body)).pages:
            writer.add_page(page)
            added += 1
    # manual "كشف حساب" page — only when the previous decree still has dispensings waiting to be billed
    # in Enhanced Monitor. Placed right after the invoices. A failure here must never block the request.
    bill_pages = 0
    if g.get("kind") in TOPUP_LIKE:
        try:
            import topup_manual_bill
            bill = topup_manual_bill.bill_pdf_for_candidate(int(g["candidate_id"]))
            if bill:
                for page in PdfReader(io.BytesIO(bill)).pages:
                    writer.add_page(page)
                    bill_pages += 1
        except Exception as exc:
            log.warning(f"  [top-up] manual bill skipped for case {case_id}: {exc}")
    for page in PdfReader(patient_pdf_path).pages:
        writer.add_page(page)
    out_dir = tempfile.mkdtemp(prefix="topup_merge_")
    out_path = os.path.join(out_dir, os.path.basename(patient_pdf_path))
    with open(out_path, "wb") as f:
        writer.write(f)
    log.info(f"  [top-up/{g.get('kind')}] case {case_id}: placed {len(docs)} document(s) / {added} page(s) "
             f"before the patient document (right after MDT + report)"
             f"{' + ' + str(bill_pages) + ' manual-bill page(s)' if bill_pages else ''}.")
    return out_path


def split_ready(cases: List[dict]) -> Tuple[List[dict], List[dict]]:
    """For prepare's bulk run: cases that are top-up retries and NOT ready are held back (and an
    OPEN requirement tells the operator why) instead of consuming an MDT; everything else passes."""
    ready, blocked = [], []
    for c in cases:
        g = _gate(c["id"])
        msg = None
        if g.get("is_topup") and not g.get("ready"):
            msg = not_ready_message(g)
        elif g.get("is_topup"):
            msg = _dose_hold(c["id"], g)
        if msg:
            blocked.append({"case_id": c["id"], "message": msg, "kind": g.get("kind")})
            try:
                import decree_common as common
                common.open_requirement(c["id"], None, msg)
            except Exception as exc:
                log.warning(f"could not open a requirement for held-back case {c['id']}: {exc}")
            log.info(f"  case {c['id']}: held back - {msg}")
        else:
            ready.append(c)
    return ready, blocked
