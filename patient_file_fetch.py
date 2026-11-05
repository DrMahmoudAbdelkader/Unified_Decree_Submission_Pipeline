#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patient_file_fetch.py — fills the gap "patient has NO file in R2".

MODE=fetch (default)  NATIONAL_IDS=a,b,c
    For each id with no <nid>.pdf in R2: download the patient's PDF from the SMC website (same function the
    submission pipeline uses) and stage it at R2 pending/<nid>.pdf. NOTHING uses a pending file: a person must open it
    on the «ملف المريض» screen, label it and press save (that promotes it to <nid>.pdf).
    Not on SMC either -> patient_file_fetch_requests.status = NOT_FOUND and the screen offers «ابدأ الملف من الأوراق الممسوحة».
MODE=register
    One-time: list every <nid>.pdf already in R2 and register it in patient_documents (READY, source LEGACY).
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import requests
import supabase_client as sb
import r2_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("patient_file_fetch")


def _upsert(table: str, rows, on_conflict: str):
    """PostgREST upsert done directly, so this script does not depend on supabase_client having an upsert()."""
    url = os.environ["SUPABASE_URL"].rstrip("/") + f"/rest/v1/{table}?on_conflict={on_conflict}"
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    r = requests.post(url, json=rows, timeout=60, headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal"})
    if r.status_code >= 300:
        raise RuntimeError(f"upsert {table}: HTTP {r.status_code} {r.text[:200]}")


OUT_DIR = os.environ.get("PATIENT_DOC_CACHE_DIR", "/tmp/patient_fetch")


def _finish(nid: str, status: str, message: str):
    try:
        rows = sb.select("patient_file_fetch_requests", select="id",
                         filters={"national_id": f"eq.{nid}", "status": "eq.QUEUED"})
        for r in rows:
            sb.update("patient_file_fetch_requests", r["id"],
                      {"status": status, "message": message[:400], "finished_at": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        log.warning(f"could not record fetch result for {nid}: {e}")


def register():
    keys = r2_client.list_permanent_doc_keys()
    rows = [{"national_id": k[:-4], "status": "READY", "source": "LEGACY", "version": 0} for k in keys]
    for i in range(0, len(rows), 500):
        _upsert("patient_documents", rows[i:i + 500], "national_id")
    log.info(f"registered {len(rows)} existing patient files")


def fetch(ids):
    from Unified_Decree_Submission_Pipeline import SMCSession, BASE_URL
    import decree_common as common
    from patient_pdf_website_fallback import download_patient_pdf_from_website
    # Same account choice as topup_live_checks.py: a spare login first, so this on-demand run does not kick the session of a prepare /
    # finalize runner that uses SMC_USERNAME at the same moment (SMC keeps one live session per login).
    for un, pw in (("SMC_USERNAME_3", "SMC_PASSWORD_3"), ("SMC_USERNAME_2", "SMC_PASSWORD_2"), ("SMC_USERNAME", "SMC_PASSWORD")):
        if os.environ.get(un) and os.environ.get(pw):
            common.configure_smc_credentials(un, pw)
            log.info(f"SMC login: {un}")
            break
    else:
        common.configure_smc_credentials()
    session = SMCSession()
    if not session.login():
        raise SystemExit("SMC login failed.")
    os.makedirs(OUT_DIR, exist_ok=True)
    for nid in ids:
        try:
            if r2_client.exists(nid):
                _finish(nid, "DONE", "الملف موجود بالفعل في R2."); continue
            if r2_client.pending_exists(nid):
                _finish(nid, "DONE", "ملف SMC مستخرج سابقاً وبانتظار المراجعة."); continue
            path = download_patient_pdf_from_website(session, BASE_URL, nid, OUT_DIR)
            if not path:
                _upsert("patient_documents", [{"national_id": nid, "status": "NEEDS_SCAN",
                                           "note": "لا يوجد ملف على موقع SMC"}], "national_id")
                _finish(nid, "NOT_FOUND", "لا يوجد ملف للمريض على موقع SMC — ابدأ الملف من الأوراق الممسوحة."); continue
            if not r2_client.upload_pending(nid, path):
                _finish(nid, "FAILED", "تعذر رفع الملف المستخرج إلى R2."); continue
            _upsert("patient_documents", [{"national_id": nid, "status": "NEEDS_REVIEW", "source": "SMC",
                                       "note": "مستخرج من SMC - بانتظار المراجعة"}], "national_id")
            _finish(nid, "DONE", "تم استخراج الملف من SMC — راجعه ثم احفظه.")
        except Exception as e:
            log.exception(f"{nid}: fetch failed")
            _finish(nid, "FAILED", f"{type(e).__name__}: {str(e)[:200]}")


if __name__ == "__main__":
    if (os.environ.get("MODE") or "fetch").lower() == "register":
        register()
    else:
        fetch([x.strip() for x in (os.environ.get("NATIONAL_IDS") or "").split(",") if x.strip()])
