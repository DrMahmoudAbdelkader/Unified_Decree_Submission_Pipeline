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
    cycles_statement(case_id)             Arabic sentence for the medical report (TOPUP only)
    split_ready(cases)                    (ready_cases, blocked) for prepare's bulk run

What each kind needs before its retry request may be sent (answer of the DB function
topup_evidence_gate(case_id), migration 07):

    TOPUP         cycle counts typed  +  signed invoices READY      -> invoices are appended
    PREV_REPORT   the previous decree's printed report READY        -> the report is appended
    ID_CARD       «تم التصحيح» pressed on the page                  -> nothing appended (the
                  corrected card is already inside the patient's own documents)

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


# kinds that run the same workflow: cycle counts + signed invoices + report sentence
TOPUP_LIKE = ("TOPUP", "PREV_INCLUSIVE")


class TopupNotReady(Exception):
    """Raised by assert_ready(); the pipeline turns any exception into FAILED + a requirement."""


# --------------------------------------------------------------------------- Supabase
def _hdr():
    return {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}


def _gate(case_id: int) -> dict:
    """{'is_topup': False} for a normal request. Fails open (with a warning) if the RPC is missing."""
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/topup_evidence_gate", json={"p_case_id": int(case_id)},
                          headers=_hdr(), timeout=30)
        if not r.ok:
            log.warning(f"topup gate skipped for case {case_id}: HTTP {r.status_code} {r.text[:120]}")
            return {"is_topup": False}
        g = r.json()
        return g if isinstance(g, dict) else {"is_topup": False}
    except Exception as exc:
        log.warning(f"topup gate skipped for case {case_id}: {exc}")
        return {"is_topup": False}


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


def cycles_statement(case_id: int) -> Optional[str]:
    g = _gate(case_id)
    if not g.get("is_topup") or g.get("kind") not in TOPUP_LIKE:
        return None
    if g.get("cycles_received") is None or g.get("cycles_remaining") is None:
        return None
    unit = _unit_word(g.get("protocol_key"))
    try:
        import topup_dose_calc as dc
        sup = dc.supposed_for(int(g["candidate_id"]), g["cycles_received"], g["cycles_remaining"]) if dc.enabled() else None
    except Exception as exc:
        log.warning(f"dose totals unavailable for case {case_id}: {exc}")
        sup = None
    if sup is not None:
        return CYCLES_TEMPLATE_TOTALS.format(supposed=f"{sup:g}", received=g["cycles_received"],
                                             remaining=g["cycles_remaining"], unit=unit)
    return CYCLES_TEMPLATE.format(received=g["cycles_received"], remaining=g["cycles_remaining"], unit=unit)


def with_invoices(case_id: int, patient_pdf_path: str) -> str:
    """patient document + the stored evidence (signed invoices, or the previous decree's report),
    written to a NEW temp file — the patient's permanent cached file is never modified. For a
    normal request or an ID_CARD letter the original path is returned untouched."""
    g = _gate(case_id)
    if not g.get("is_topup") or g.get("kind") not in ("TOPUP", "PREV_INCLUSIVE", "PREV_REPORT"):
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
