#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patient_file_gate.py — patient-file HOLD notes. A hold NEVER blocks a decree request.

A HOLD (table patient_file_holds) is opened
  * automatically by topup_live_checks.py when an administrative letter says the national ID card is invalid
    (source ID_CARD_LETTER) - it only drives the top-up tab «بانتظار بطاقة سارية جديدة» and the note on the «ملف المريض» page;
  * by hand, if a caller uses the edge function's hold_create (the screen no longer offers a button for it).
It is cleared automatically by the patient-file edge function when a scan carrying one of the needed paper types
(ID_FRONT / ID_BACK / ID_FRONT_BACK for the ID letter) is saved into the patient's file.

decree_submission_prepare.py no longer calls open_holds(): requests are decided by the people sending them, not by this table.
open_holds() / hold_message() stay for reporting and for anything that wants to show the note.
Fail-OPEN: if the table does not exist yet / Supabase hiccups, nothing breaks.
"""
from __future__ import annotations

import logging
import os
from typing import List

import supabase_client as sb

log = logging.getLogger("patient_file_gate")
ID_CODES = ["ID_FRONT", "ID_BACK", "ID_FRONT_BACK"]


def enabled() -> bool:
    return (os.environ.get("PATIENT_FILE_HOLDS") or "1").strip().lower() not in ("0", "false", "off", "no")


def open_holds(national_id: str) -> List[dict]:
    if not enabled():
        return []
    try:
        return sb.select("patient_file_holds", select="id,reason,needed_codes,source,created_at",
                         filters={"national_id": f"eq.{national_id}", "cleared_at": "is.null"},
                         order="created_at.asc") or []
    except Exception as e:
        if (os.environ.get("PATIENT_FILE_HOLD_STRICT") or "").strip() in ("1", "true"):
            raise
        log.warning(f"hold check skipped for {national_id}: {type(e).__name__}: {str(e)[:120]}")
        return []


def hold_message(holds: List[dict]) -> str:
    parts = []
    for h in holds:
        need = "، ".join(h.get("needed_codes") or []) or "أي إضافة"
        parts.append(f"{h.get('reason')} (المطلوب: {need})")
    return ("تنبيه: ملف المريض ينتظر أوراقاً جديدة — " + " | ".join(parts) +
            " (لا يمنع إرسال الطلب؛ أضف الأوراق من شاشة «ملف المريض» حين يلزم).")


def open_id_card_hold(national_id: str, source_case_id=None, reason: str = "") -> None:
    """Called when an ID-card admin letter is seen. Idempotent (one open hold per patient+source)."""
    try:
        have = sb.select("patient_file_holds", select="id",
                         filters={"national_id": f"eq.{national_id}", "source": "eq.ID_CARD_LETTER", "cleared_at": "is.null"},
                         limit=1)
        if have:
            return
        sb.insert("patient_file_holds", {
            "national_id": national_id, "source": "ID_CARD_LETTER", "needed_codes": ID_CODES,
            "reason": reason or "خطاب إداري: بطاقة الرقم القومي غير سارية — مطلوب مسح بطاقة سارية",
            "source_case_id": source_case_id})
    except Exception as e:
        log.warning(f"could not open ID hold for {national_id}: {e}")
