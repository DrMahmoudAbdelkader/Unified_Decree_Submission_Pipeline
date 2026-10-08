#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_gate.py - plugs the top-up (التجديد في نهاية المده) evidence into the submission pipeline.

Used from decree_common.run_finalize_stages(), which BOTH decree_submission_prepare.py
(straight-through path) and decree_submission_finalize.py (incl. linked siblings) go through,
so one patch covers every way a request gets submitted, bulk or single.

  assert_ready(case_id)        raises if the case is a top-up retry whose evidence is not READY
                               (cycle counts missing, invoices missing / need review).
                               NOT a top-up case  -> returns immediately, nothing changes.
  with_invoices(case_id, pdf)  returns the path to use as the patient document: the original
                               file + the approved signed invoices appended after it.
                               NOT a top-up case  -> returns the original path untouched.
                               The original file (the R2 permanent cache copy) is never modified.

Fails OPEN on infrastructure errors (migration 04 not applied, Supabase unreachable): a normal
request must never be blocked by this feature. A real top-up that is genuinely not ready DOES block.
"""
from __future__ import annotations
import logging, os, tempfile
import requests

log = logging.getLogger("topup_gate")

# Wording of the sentence added to the medical report. {x} cycles received, {y} still to be
# received, {unit} e.g. "دورة", {protocol} the protocol's Arabic label (decree_topup_protocols).
TOPUP_STATEMENT_TEMPLATE = (
    "وقد تلقى المريض عدد ({x}) {unit} علاجية من بروتوكول ({protocol})، "
    "ويتبقى للمريض عدد ({y}) {unit} علاجية لاستكمال البروتوكول، "
    "علماً بأن قيمة القرار السابق قد استُنفدت بالكامل."
)
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
_H = lambda: {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}


def get_gate(case_id: int) -> dict:
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/topup_evidence_gate", json={"p_case_id": int(case_id)},
                          headers=_H(), timeout=30)
        if not r.ok:
            raise RuntimeError(f"{r.status_code}: {r.text[:160]}")
        return r.json() or {"is_topup": False}
    except Exception as e:                                  # fail open
        log.warning(f"top-up gate skipped for case {case_id}: {e}")
        return {"is_topup": False}


def _not_ready_reason(g: dict) -> str:
    why = []
    if g.get("cycles_received") is None or g.get("cycles_remaining") is None:
        why.append("لم تُدخل عدد الدورات المستلمة/المتبقية")
    if not g.get("invoices_ok"):
        why.append("لا توجد فواتير موقّعة معتمدة")
    if g.get("invoices_bad"):
        why.append(f"{g['invoices_bad']} فاتورة تحتاج مراجعة/اعتماد")
    if g.get("evidence_status") not in (None, "READY") and not why:
        why.append(f"حالة الأدلة: {g['evidence_status']}")
    return ("طلب تجديد في نهاية المدة غير جاهز للإرسال — " + " ، ".join(why)
            + " — أكمل ذلك من صفحة «تجديد نهاية المدة» ثم أعد الإرسال.")


def assert_ready(case_id: int) -> dict:
    g = get_gate(case_id)
    if not g.get("is_topup") or g.get("ready"):
        return g
    raise RuntimeError(_not_ready_reason(g))


def split_ready(cases: list) -> tuple:
    """For the bulk prepare run: (cases that may proceed, [{case_id, message}] that must wait).
    Non top-up cases always proceed (and get_gate fails open, so an outage never blocks them)."""
    ok, blocked = [], []
    for c in cases:
        g = get_gate(c["id"])
        if g.get("is_topup") and not g.get("ready"):
            blocked.append({"case_id": c["id"], "message": _not_ready_reason(g)})
        else:
            ok.append(c)
    return ok, blocked


def cycles_statement(case_id: int):
    """Arabic sentence for the medical report (cycles received / still to receive), or None when
    this case is not a top-up. Wording lives in TOPUP_STATEMENT_TEMPLATE - edit it there."""
    g = get_gate(case_id)
    if not g.get("is_topup"):
        return None
    x, y = g.get("cycles_received"), g.get("cycles_remaining")
    if x is None or y is None:
        raise RuntimeError("طلب تجديد: عدد الدورات المستلمة/المتبقية غير مُدخل")
    label, unit = g.get("protocol_key") or "", "دورة"
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/decree_topup_protocols", headers=_H(), timeout=30,
                         params={"select": "label_ar,unit_word_ar", "protocol_key": f"eq.{g.get('protocol_key')}"})
        if r.ok and r.json():
            label, unit = r.json()[0]["label_ar"], (r.json()[0].get("unit_word_ar") or "دورة")
    except Exception as e:
        log.warning(f"protocol label lookup failed ({e}) - using the key")
    return TOPUP_STATEMENT_TEMPLATE.format(x=x, y=y, unit=unit, protocol=label)


def _r2():
    import boto3
    return boto3.client(
        "s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")


def _approved_invoices(candidate_id: int) -> list[dict]:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/decree_topup_invoices", headers=_H(), timeout=30, params={
        "select": "decree_number,receipt_id,r2_key", "candidate_id": f"eq.{candidate_id}",
        "status": "eq.OK", "r2_key": "not.is.null", "order": "decree_number.asc,receipt_id.asc"})
    r.raise_for_status()
    return r.json()


def with_invoices(case_id: int, patient_pdf_path: str) -> str:
    g = get_gate(case_id)
    if not g.get("is_topup"):
        return patient_pdf_path
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:                                    # repo may still be on PyPDF2
        from PyPDF2 import PdfReader, PdfWriter
    invs = _approved_invoices(g["candidate_id"])
    if not invs:
        raise RuntimeError("طلب تجديد: لا توجد فواتير معتمدة لإرفاقها بالمستند")
    s3, bucket = _r2(), os.environ["R2_BUCKET_NAME"]
    w = PdfWriter()
    for pg in PdfReader(patient_pdf_path).pages:
        w.add_page(pg)
    import io
    for inv in invs:
        body = s3.get_object(Bucket=bucket, Key=inv["r2_key"])["Body"].read()
        for pg in PdfReader(io.BytesIO(body)).pages:
            w.add_page(pg)
    out = os.path.join(tempfile.mkdtemp(prefix="topup_doc_"), f"case{case_id}_with_invoices.pdf")
    with open(out, "wb") as f:
        w.write(f)
    log.info(f"  [top-up] case {case_id}: appended {len(invs)} signed invoice(s) to the patient document")
    return out
