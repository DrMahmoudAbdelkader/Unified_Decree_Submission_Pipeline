#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decree_submission_finalize.py
==========================================================================
Phase 2 entrypoint — triggered by the module's "موافقة ومتابعة" (Approve &
Continue) click after a human has reviewed a freshly-extracted patient
document (same edge function as prepare, phase="finalize", case_ids
required — this never runs in bulk, only for cases a human just approved).

Requires the case's latest attempt to have document_review_status =
'approved' (the module sets this when the button is clicked — see the
module wiring notes) and a non-null pipeline_state (written by
decree_submission_prepare.py). Anything else is treated as a
configuration error, not silently skipped, since finalize should only
ever be invoked for a specific, already-approved case.

RUN LOCALLY:
    export SUPABASE_URL=...  SUPABASE_SERVICE_ROLE_KEY=...
    export SMC_USERNAME=...  SMC_PASSWORD=...
    export R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=...
    export CASE_IDS=6
    python decree_submission_finalize.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import List

sys.path.insert(0, os.path.dirname(__file__))

import decree_common as common
from Unified_Decree_Submission_Pipeline import SMCSession
import supabase_client as sb
import r2_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("decree_submission_finalize")


def parse_case_ids(raw: str) -> List[int]:
    raw = (raw or "").strip()
    if not raw:
        return []
    return [int(p.strip()) for p in raw.split(",") if p.strip().isdigit()]


def _download_approved_doc(national_id: str) -> str:
    """The labeling step is expected to have already moved the cleaned
    document from R2's pending/<id>.pdf to the permanent <id>.pdf (root)
    key before setting document_review_status='approved' — so this is
    just an ordinary permanent-cache read, same call prepare.py's
    find_local_fn uses. Raises clearly if that hasn't actually happened
    yet, rather than silently treating a missing file as "not needed"."""
    local_path = r2_client.download_if_exists(national_id, common.PATIENT_DOC_CACHE_DIR)
    if not local_path:
        raise RuntimeError(
            f"No document found at R2's permanent {national_id}.pdf — the labeling step may not have "
            f"saved it there yet. Approve only after the cleaned PDF is actually in R2."
        )
    return local_path


def finalize_one_case(session: SMCSession, case: dict) -> dict:
    case_id = case["id"]

    attempts = sb.select(common.ATTEMPTS_TABLE, select="*", filters={"case_id": f"eq.{case_id}"}, order="attempt_number.desc", limit=1)
    if not attempts:
        return {"case_id": case_id, "status": "error", "message": "No attempt found for this case."}
    attempt = attempts[0]
    attempt_id = attempt["id"]

    if attempt.get("document_review_status") != "approved":
        msg = (f"لا يمكن إكمال هذا الطلب — حالة مراجعة المستند الحالية هي "
               f"'{attempt.get('document_review_status')}' وليست 'approved'. "
               "يجب الموافقة على المستند من الوحدة أولاً.")
        common.open_requirement(case_id, attempt_id, msg)
        return {"case_id": case_id, "status": "error", "message": msg}

    pipeline_state = attempt.get("pipeline_state")
    if not pipeline_state:
        msg = "لا توجد بيانات محفوظة لإكمال هذا الطلب (pipeline_state فارغ) — أعد الإرسال من البداية."
        common.open_requirement(case_id, attempt_id, msg)
        return {"case_id": case_id, "status": "error", "message": msg}
    if isinstance(pipeline_state, str):
        pipeline_state = json.loads(pipeline_state)

    national_id = common.get_national_id(case["patient_id"])
    if not national_id:
        msg = f"لم يتم العثور على بيانات المريض (id={case['patient_id']})."
        common.open_requirement(case_id, attempt_id, msg)
        return {"case_id": case_id, "status": "error", "message": msg}

    try:
        patient_pdf_path = _download_approved_doc(national_id)
    except Exception as exc:
        msg = f"تعذر تنزيل المستند المعتمد — {exc}"
        common.open_requirement(case_id, attempt_id, msg)
        return {"case_id": case_id, "status": "error", "message": msg}

    result = common.run_finalize_stages(session, case_id, attempt_id, national_id, pipeline_state, patient_pdf_path)
    common.write_submission_result(case_id, attempt_id, result, pipeline_state.get("request_category", "ordinary"))

    if result["status"] == "SUCCESS":
        return {"case_id": case_id, "status": "submitted", "request_number": result["final_request_no"]}
    return {"case_id": case_id, "status": "error", "message": result.get("error")}


def main():
    case_ids = parse_case_ids(os.environ.get("CASE_IDS", ""))
    if not case_ids:
        raise SystemExit("decree_submission_finalize.py requires CASE_IDS — it never runs in bulk.")

    common.configure_smc_credentials()
    common.fetch_and_configure_assets()

    session = SMCSession()
    if not session.login():
        raise SystemExit("SMC login failed — check SMC_USERNAME/SMC_PASSWORD secrets.")

    cases = sb.select(common.CASES_TABLE, select="*", filters={"id": f"in.({','.join(str(i) for i in case_ids)})"})
    results = [finalize_one_case(session, case) for case in cases]

    summary = {
        "total": len(results),
        "submitted": sum(1 for r in results if r["status"] == "submitted"),
        "error": sum(1 for r in results if r["status"] == "error"),
        "results": results,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"## Decree finalize run\n- Submitted: **{summary['submitted']}**\n- Errors: **{summary['error']}**\n")

    if summary["error"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
