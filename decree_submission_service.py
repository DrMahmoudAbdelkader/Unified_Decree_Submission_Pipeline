#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decree_submission_service.py
==========================================================================
FINAL ARCHITECTURE: GitHub Actions + Supabase only — nothing else.

This script runs as a GitHub Actions job (workflow_dispatch — see
.github/workflows/submit-decree-requests.yml), started on demand by the
Supabase Edge Function `submit-decree-requests` when a user clicks
"إرسال" in the module's "الإرسال إلى SMC" tab. All the actual work (the
fragile, already-debugged Arabic-PDF/SMC-scraping logic) stays exactly
as it is in Unified_Decree_Submission_Pipeline.py — this file does NOT
reimplement any of that, it only wires process_row() up to the real
Supabase schema.

CONFIRMED NOT the Flask/always-on-service design — that version was
resent by mistake in a later upload and is intentionally NOT used. This
is the version confirmed against the real schema and process_row()
signature.

WHY THIS ISN'T A LIVE, INSTANT RESULT: GitHub Actions runs are started
asynchronously — the edge function tells GitHub "go", GitHub queues the
job, and neither the edge function nor the module waits for it to finish
(a full run doing live SMC scraping + PDF generation can take minutes
per row). So the module reflects progress by refreshing from
decree_request_cases / decree_request_requirements / decree_request_events
— the same tables this script writes to — a few seconds after firing,
not from a synchronous HTTP response. See loadSmcSubmissionData() /
submitToSmc() in decree-request-entry.js for exactly how that refresh
loop works.

SCHEMA NOTES (matched against the real schema dump, not guessed):
  - decree_request_cases.case_status has a fixed CHECK constraint
    (DRAFT / READY_TO_SUBMIT / SUBMITTED / PENDING / ADMIN_LETTER /
    RESUBMISSION / FINAL_APPROVED / FINAL_DECLINED / CANCELLED / CLOSED)
    — there is no 'needs_attention' value. Failures do NOT change
    case_status; they open a row in decree_request_requirements (which
    already exists for exactly this) with a specific, human-readable,
    actionable message, plus a decree_request_events entry. The case
    stays READY_TO_SUBMIT — visible, retryable, never silently skipped.
  - request_category resolves from decree_request_cases.request_category
    if set, else from the plan's default_request_category, else
    'ordinary' — the pipeline's own resolve_request_category() /
    resolve_effective_proc_id() do the rest.
  - cancer_type_aliases.pipeline_tumor_key stores the diagnosis code
    (lower-cased), not a canonical_id. Passed straight into
    resolve_tumor_type() unchanged.

SECURITY NOTE — still not actioned, flagging again:
  Unified_Decree_Submission_Pipeline.py currently hardcodes the SMC
  login (USERNAME / PASSWORD) as plain module-level strings rather than
  reading them from the environment. This script overrides those two
  module attributes at import time (same pattern already used below for
  WKHTMLTOPDF_PATH / PATIENT_DOCS_ROOT) so the real credentials can live
  only in GitHub Actions secrets — but that only helps if the hardcoded
  values are also SCRUBBED from the pipeline file before it's committed
  to the repo (git history keeps plaintext secrets forever otherwise,
  even if you edit them out in a later commit). Do that scrub first, and
  rotate the password on the SMC site itself once this is stable — it
  has now passed through plaintext chat uploads multiple times.

WHAT THIS SCRIPT DOES, per run:
    1. SELECT decree_request_cases WHERE case_status = 'READY_TO_SUBMIT'
       (optionally narrowed to CASE_IDS — see below), joined to
       patients, and to decree_treatment_plans OR
       decree_custom_treatment_plans depending on plan_source.
    2. For each case, re-runs the SAME pre-flight checks the module's
       "الإرسال إلى SMC" tab already ran client-side before allowing the
       click (cancer type alias exists, plan linked and has text,
       patient exists) — server-side, because the module's checks are a
       courtesy to the user, not a security boundary; data can change
       between the click and the run actually starting.
    3. Calls process_row(...) — the pipeline's own, already-tested,
       6-stage function. Nothing about its internals is modified.
    4. SUCCESS -> updates decree_request_attempts / decree_request_cases,
       inserts an event. FAILURE -> opens a decree_request_requirements
       row with process_row()'s own specific error text verbatim,
       inserts an event, case stays READY_TO_SUBMIT untouched.
    5. Prints a JSON summary to stdout (visible in the Action's log) and
       writes it to $GITHUB_STEP_SUMMARY as a table.

RUN LOCALLY (for testing before wiring into Actions):
    pip install -r requirements.txt
    export SUPABASE_URL=...
    export SUPABASE_SERVICE_ROLE_KEY=...
    export SMC_USERNAME=...  SMC_PASSWORD=...
    export CASE_IDS=123,456        # optional — omit to run every READY_TO_SUBMIT case
    python decree_submission_service.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(__file__))

# Reused verbatim — not reimplemented. This script imports the existing,
# already-tested pipeline instead of duplicating any of its logic.
import Unified_Decree_Submission_Pipeline as _pipeline_module

# The pipeline hardcodes Windows/on-prem paths and credentials that don't
# exist / shouldn't be trusted on a GitHub-hosted Linux runner. Overriding
# the MODULE ATTRIBUTES here — never editing the pipeline file itself —
# keeps the fragile, already-debugged pipeline logic untouched while
# letting it run safely in CI. SMCSession.login() reads USERNAME/PASSWORD
# from this module's globals at call time, so overriding them before
# login() is called is enough; no pipeline edit needed.
if os.environ.get("SMC_USERNAME"):
    _pipeline_module.USERNAME = os.environ["SMC_USERNAME"]
if os.environ.get("SMC_PASSWORD"):
    _pipeline_module.PASSWORD = os.environ["SMC_PASSWORD"]

# WKHTMLTOPDF_PATH removed - MDT print rendering now uses Playwright/
# Chromium, not a wkhtmltopdf binary.
_pipeline_module.PATIENT_DOCS_ROOT = os.environ.get("PATIENT_DOCS_ROOT", "/tmp/patient_docs")
_pipeline_module.PATIENT_DOCS_UNDER_PROCESSED_DIR = os.path.join(_pipeline_module.PATIENT_DOCS_ROOT, "UNDER_PROCESSED")
os.makedirs(_pipeline_module.PATIENT_DOCS_UNDER_PROCESSED_DIR, exist_ok=True)

from Unified_Decree_Submission_Pipeline import (
    SMCSession,
    process_row,
    load_checkpoints,
)
import supabase_client as sb

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("decree_submission_service")

CASES_TABLE = "decree_request_cases"
ATTEMPTS_TABLE = "decree_request_attempts"
REQUIREMENTS_TABLE = "decree_request_requirements"
EVENTS_TABLE = "decree_request_events"
ALIASES_TABLE = "cancer_type_aliases"
PATIENTS_TABLE = "patients"
PLANS_TABLE = "decree_treatment_plans"
CUSTOM_PLANS_TABLE = "decree_custom_treatment_plans"


# =====================================================================
# Lookups
# =====================================================================

def load_cancer_type_aliases() -> Dict[str, str]:
    rows = sb.select(ALIASES_TABLE, select="module_cancer_code,pipeline_tumor_key", filters={"is_active": "is.true"})
    return {r["module_cancer_code"]: r["pipeline_tumor_key"] for r in rows}


def _open_requirement(case_id: int, attempt_id: Optional[int], message: str):
    """Failures land here, not on case_status — see module docstring for why."""
    sb.insert(REQUIREMENTS_TABLE, {
        "case_id": case_id,
        "attempt_id": attempt_id,
        "requirement_text": message,
        "status": "OPEN",
    })
    _log_event(case_id, attempt_id, "submission_failed", {"message": message})


def _log_event(case_id: int, attempt_id: Optional[int], event_type: str, details: dict):
    sb.insert(EVENTS_TABLE, {
        "case_id": case_id, "attempt_id": attempt_id,
        "event_type": event_type, "details": details,
    })


def _resolve_plan(case: dict) -> Optional[dict]:
    if case.get("plan_source") == "OFFICIAL" and case.get("treatment_plan_id"):
        rows = sb.select(PLANS_TABLE, select="*", filters={"id": f"eq.{case['treatment_plan_id']}"})
    elif case.get("custom_treatment_plan_id"):
        rows = sb.select(CUSTOM_PLANS_TABLE, select="*", filters={"id": f"eq.{case['custom_treatment_plan_id']}"})
    else:
        return None
    return rows[0] if rows else None


def _resolve_plan_texts(plan: dict) -> Dict[str, str]:
    base = plan.get("website_submission_treatment_plan") or ""
    return {
        "mdt_text": plan.get("mdt_treatment_plan_text") or base,
        "medical_report_text": plan.get("medical_report_treatment_plan_text") or base,
    }


def _resolve_request_category(case: dict, plan: dict) -> str:
    return case.get("request_category") or plan.get("default_request_category") or "ordinary"


# =====================================================================
# Per-case submission
# =====================================================================

def submit_one_case(session: SMCSession, case: dict, checkpoints: dict, aliases: Dict[str, str]) -> dict:
    case_id = case["id"]

    pipeline_key = aliases.get(case["tumor_type"])
    if not pipeline_key:
        msg = (f"نوع الورم \"{case['tumor_type']}\" غير مربوط بعد بأنواع الأورام في السكربت. "
               f"اطلب من أحد المسؤولين إضافته من إعدادات أنواع الأورام (جدول cancer_type_aliases).")
        _open_requirement(case_id, None, msg)
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}

    plan = _resolve_plan(case)
    if not plan:
        msg = "لا توجد خطة علاجية مرتبطة بهذا الطلب — أضف خطة قبل الإرسال."
        _open_requirement(case_id, None, msg)
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}

    texts = _resolve_plan_texts(plan)
    if not texts["mdt_text"].strip():
        msg = "الخطة العلاجية المرتبطة بهذا الطلب لا تحتوي على نص — أكمل نص الخطة قبل الإرسال."
        _open_requirement(case_id, None, msg)
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}

    request_category = _resolve_request_category(case, plan)

    patient_rows = sb.select(PATIENTS_TABLE, select="national_id", filters={"id": f"eq.{case['patient_id']}"})
    if not patient_rows:
        msg = f"لم يتم العثور على بيانات المريض (id={case['patient_id']}) — لا يمكن تحديد الرقم القومي."
        _open_requirement(case_id, None, msg)
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}
    national_id = patient_rows[0]["national_id"]

    existing_attempts = sb.select(ATTEMPTS_TABLE, select="id,attempt_number", filters={"case_id": f"eq.{case_id}"})
    attempt_number = max((a["attempt_number"] for a in existing_attempts), default=0) + 1
    attempt = sb.insert(ATTEMPTS_TABLE, {
        "case_id": case_id,
        "attempt_number": attempt_number,
        "website_submission_treatment_plan": texts["mdt_text"],
        "attempt_status": "READY_TO_SUBMIT",
    })
    attempt_id = attempt.get("id")

    try:
        result = process_row(
            session=session,
            patient_id=national_id,
            description=texts["mdt_text"],
            tumor_type_raw=pipeline_key,
            request_category_raw=request_category,
            row_num=case_id,
            checkpoints=checkpoints,
        )
    except Exception as exc:  # never let one row's crash kill the batch
        log.exception(f"case {case_id}: unexpected exception")
        msg = f"حدث خطأ غير متوقع أثناء الإرسال — حاول مرة أخرى بعد قليل. ({exc})"
        _open_requirement(case_id, attempt_id, msg)
        sb.update(ATTEMPTS_TABLE, attempt_id, {"attempt_status": "READY_TO_SUBMIT"})
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}

    if result["status"] == "SUCCESS":
        sb.update(ATTEMPTS_TABLE, attempt_id, {
            "attempt_status": "SUBMITTED",
            "website_request_id": result["final_request_no"],
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        })
        sb.update(CASES_TABLE, case_id, {
            "case_status": "SUBMITTED",
            "request_category": request_category,
        })
        _log_event(case_id, attempt_id, "submitted", {
            "final_request_no": result["final_request_no"],
            "pre_request_id": result["pre_request_id"],
            "warnings": result["warnings"],
        })
        return {"case_id": case_id, "status": "submitted", "request_number": result["final_request_no"]}

    msg = result.get("error") or "; ".join(result.get("warnings", [])) or f"انتهى الطلب بحالة {result['status']}."
    _open_requirement(case_id, attempt_id, msg)
    return {"case_id": case_id, "status": "requirement_opened", "message": msg}


# =====================================================================
# Batch runner
# =====================================================================

def smc_creds_present() -> bool:
    return bool(getattr(_pipeline_module, "USERNAME", None) and getattr(_pipeline_module, "PASSWORD", None))


def parse_case_ids(raw: str) -> List[int]:
    raw = (raw or "").strip()
    if not raw:
        return []
    return [int(p.strip()) for p in raw.split(",") if p.strip().isdigit()]


def write_step_summary(summary: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Decree submission run",
        f"- Total cases attempted: **{summary['total']}**",
        f"- Submitted successfully: **{summary['submitted']}**",
        f"- Needs review (requirement opened): **{summary['requirement_opened']}**",
        "",
    ]
    if summary["results"]:
        lines.append("| case_id | status | detail |")
        lines.append("|---|---|---|")
        for r in summary["results"]:
            detail = r.get("request_number") or r.get("message") or ""
            detail = str(detail).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r['case_id']} | {r['status']} | {detail} |")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def run_batch(case_ids: List[int]) -> dict:
    if not smc_creds_present():
        raise SystemExit("SMC credentials not configured (SMC_USERNAME/SMC_PASSWORD secrets, or hardcoded in the pipeline file).")

    session = SMCSession()
    if not session.login():
        raise SystemExit("SMC login failed — check SMC_USERNAME/SMC_PASSWORD secrets.")

    checkpoints = load_checkpoints(os.environ.get("DECREE_CHECKPOINT_PATH", "./decree_checkpoints.json"))
    aliases = load_cancer_type_aliases()

    filters = {"case_status": "eq.READY_TO_SUBMIT"}
    if case_ids:
        filters["id"] = f"in.({','.join(str(i) for i in case_ids)})"

    cases = sb.select(CASES_TABLE, select="*", filters=filters)
    log.info(f"{len(cases)} case(s) with case_status=READY_TO_SUBMIT" + (f" matching case_ids={case_ids}" if case_ids else ""))

    results: List[dict] = [submit_one_case(session, case, checkpoints, aliases) for case in cases]

    return {
        "total": len(results),
        "submitted": sum(1 for r in results if r["status"] == "submitted"),
        "requirement_opened": sum(1 for r in results if r["status"] == "requirement_opened"),
        "results": results,
    }


def main():
    case_ids = parse_case_ids(os.environ.get("CASE_IDS", ""))
    summary = run_batch(case_ids)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    write_step_summary(summary)
    if summary["total"] > 0 and summary["submitted"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
