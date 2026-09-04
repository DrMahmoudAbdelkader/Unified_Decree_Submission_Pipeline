"""
decree_common.py
==========================================================================
Shared between decree_submission_prepare.py and decree_submission_finalize.py:
  - table lookups / requirement+event helpers (same as before)
  - fetch_and_configure_assets(): pulls signatures + medical report
    template from Supabase Storage, points the pipeline's Arabic font
    paths at the apt-installed Amiri font (confirmed to cover both
    Arabic AND Latin glyphs — see the workflow's apt-get step)
  - run_finalize_stages(): Stages 2-6 (sign, build report, locate-doc-
    dependent merge, upload) — the part that's IDENTICAL whether the
    document was a cache hit (prepare continues straight through) or a
    freshly-approved review (finalize resumes here). Kept in one place
    so there's exactly one implementation to trust.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional

import Unified_Decree_Submission_Pipeline as _pipeline_module
from Unified_Decree_Submission_Pipeline import (
    SMCSession,
    stage_render_and_sign,
    stage_upload_merged_pdf,
    merge_final_pdf,
    call_with_reconnect,
)
import medical_report_overlay as _report_module
from medical_report_overlay import build_medical_report_pdf

import supabase_client as sb
import supabase_storage
import r2_client

log = logging.getLogger("decree_common")

CASES_TABLE = "decree_request_cases"
ATTEMPTS_TABLE = "decree_request_attempts"
REQUIREMENTS_TABLE = "decree_request_requirements"
EVENTS_TABLE = "decree_request_events"
ALIASES_TABLE = "cancer_type_aliases"
PATIENTS_TABLE = "patients"
PLANS_TABLE = "decree_treatment_plans"
CUSTOM_PLANS_TABLE = "decree_custom_treatment_plans"

MERGED_PDF_DIR = "/tmp/decree_merged"
PATIENT_DOC_CACHE_DIR = "/tmp/patient_docs"
ASSETS_DIR = "/tmp/decree_assets"

# Confirmed via `fc-list` + fontTools cmap inspection against the exact
# fonts-hosny-amiri package installed in the workflow: Amiri-Regular/Bold
# cover BOTH Arabic and Latin glyphs in one file — the same requirement
# the original code called out Tahoma for. Nothing to download; these
# paths exist once `apt-get install fonts-hosny-amiri` has run.
LINUX_ARABIC_FONT_PATH = "/usr/share/fonts/opentype/fonts-hosny-amiri/Amiri-Regular.ttf"
LINUX_ARABIC_FONT_PATH_BOLD = "/usr/share/fonts/opentype/fonts-hosny-amiri/Amiri-Bold.ttf"


# =====================================================================
# Credentials / environment wiring (same monkeypatch pattern used
# throughout this project — never edits the pipeline files themselves)
# =====================================================================

def configure_smc_credentials(username_env="SMC_USERNAME", password_env="SMC_PASSWORD"):
    username = os.environ.get(username_env)
    password = os.environ.get(password_env)
    if username:
        _pipeline_module.USERNAME = username
    if password:
        _pipeline_module.PASSWORD = password
    _pipeline_module.WKHTMLTOPDF_PATH = os.environ.get("WKHTMLTOPDF_PATH", "/usr/bin/wkhtmltopdf")
    _pipeline_module.PATIENT_DOCS_ROOT = os.environ.get("PATIENT_DOCS_ROOT", PATIENT_DOC_CACHE_DIR)
    _pipeline_module.PATIENT_DOCS_UNDER_PROCESSED_DIR = os.path.join(_pipeline_module.PATIENT_DOCS_ROOT, "UNDER_PROCESSED")
    os.makedirs(_pipeline_module.PATIENT_DOCS_UNDER_PROCESSED_DIR, exist_ok=True)


def fetch_and_configure_assets() -> Dict[str, str]:
    """Downloads signatures + medical report template from Supabase
    Storage, and points every hardcoded local-PC path in the pipeline
    (SIGNATURE_FILES, medical_report_overlay's template/font paths) at
    the downloaded/apt-installed equivalents. Raises RuntimeError with a
    specific message if anything required is missing — never lets a
    downstream FileNotFoundError surface unexplained."""
    assets = supabase_storage.fetch_signing_and_template_assets(ASSETS_DIR)

    _pipeline_module.SIGNATURE_FILES = {
        "sig1": assets["sig1"], "sig2": assets["sig2"], "sig3": assets["sig3"],
        "sig4": assets["sig4"], "stamp": assets["stamp"],
    }

    _report_module.MEDICAL_REPORT_TEMPLATE_PDF = assets["template"]
    _report_module.ARABIC_FONT_PATH = LINUX_ARABIC_FONT_PATH
    _report_module.ARABIC_FONT_PATH_BOLD = LINUX_ARABIC_FONT_PATH_BOLD
    if not os.path.exists(LINUX_ARABIC_FONT_PATH) or not os.path.exists(LINUX_ARABIC_FONT_PATH_BOLD):
        raise RuntimeError(
            f"Amiri font not found at {LINUX_ARABIC_FONT_PATH} / {LINUX_ARABIC_FONT_PATH_BOLD} — "
            f"make sure the workflow's apt-get step installs 'fonts-hosny-amiri'."
        )
    return assets


# =====================================================================
# Lookups (unchanged from the single-phase version)
# =====================================================================

def load_cancer_type_aliases() -> Dict[str, str]:
    """Optional override table: case tumor_type text -> pipeline_tumor_key.
    No longer required for a case to process — resolve_tumor_type() in the
    pipeline script now recognizes the module's raw diagnosis text (e.g.
    "Breast Cancer") directly. If this table has been emptied or dropped,
    that's fine: fall back to no overrides instead of crashing the whole
    prepare run (same "one row's problem should not kill the batch"
    principle as everything else in this module)."""
    try:
        rows = sb.select(ALIASES_TABLE, select="module_cancer_code,pipeline_tumor_key", filters={"is_active": "is.true"})
    except Exception as e:
        log.warning(f"cancer_type_aliases lookup skipped ({e}) — resolving tumor types from raw text only.")
        return {}
    return {r["module_cancer_code"]: r["pipeline_tumor_key"] for r in rows}


def open_requirement(case_id: int, attempt_id: Optional[int], message: str):
    sb.insert(REQUIREMENTS_TABLE, {
        "case_id": case_id, "attempt_id": attempt_id,
        "requirement_text": message, "status": "OPEN",
    })
    log_event(case_id, attempt_id, "submission_failed", {"message": message})


def log_event(case_id: int, attempt_id: Optional[int], event_type: str, details: dict):
    sb.insert(EVENTS_TABLE, {
        "case_id": case_id, "attempt_id": attempt_id,
        "event_type": event_type, "details": details,
    })


def resolve_plan(case: dict) -> Optional[dict]:
    if case.get("plan_source") == "OFFICIAL" and case.get("treatment_plan_id"):
        rows = sb.select(PLANS_TABLE, select="*", filters={"id": f"eq.{case['treatment_plan_id']}"})
    elif case.get("custom_treatment_plan_id"):
        rows = sb.select(CUSTOM_PLANS_TABLE, select="*", filters={"id": f"eq.{case['custom_treatment_plan_id']}"})
    else:
        return None
    return rows[0] if rows else None


def resolve_plan_texts(plan: dict) -> Dict[str, str]:
    base = plan.get("website_submission_treatment_plan") or ""
    return {
        "mdt_text": plan.get("mdt_treatment_plan_text") or base,
        "medical_report_text": plan.get("medical_report_treatment_plan_text") or base,
    }


# decree_treatment_plans.default_request_category (module-facing, used for
# the module's own cascading-dropdown catalog browsing) now allows a much
# broader set than the pipeline's resolve_request_category() understands:
# 'scan','medication','surgery','radiotherapy','intervention','pathology',
# 'other',''. The pipeline itself only ever recognizes FOUR values —
# 'ordinary','surgery','scan','pet_ct' — each tied to a real proc_id
# override (or none, for 'ordinary'). Passing the module's raw value
# straight into resolve_request_category() returns None for anything
# outside that set of four, which previously meant every 'medication' /
# 'radiotherapy' / 'intervention' / 'pathology' / 'other' plan would hard-
# fail at submission. This map is the translation layer: every module
# category that doesn't correspond to a known SMC proc_id override falls
# through to 'ordinary' (the tumor type's own proc_id is used, which is
# correct for anything that isn't specifically a scan/surgery/PET-CT
# override) rather than being rejected.
PLAN_CATEGORY_TO_PIPELINE_CATEGORY = {
    "scan": "scan",
    "surgery": "surgery",
    "pet_ct": "pet_ct",  # only reachable if you add 'pet_ct' to
                          # decree_treatment_plans_default_request_category_check —
                          # not there today, so this is forward-compatible, not yet live.
    "medication": "ordinary",
    "radiotherapy": "ordinary",
    "intervention": "ordinary",
    "pathology": "ordinary",
    "other": "ordinary",
    "": "ordinary",
    None: "ordinary",
}


def resolve_request_category_value(case: dict, plan: dict) -> str:
    """Returns a value resolve_request_category() in the pipeline is
    guaranteed to recognize — never the module's raw, broader category
    vocabulary. case.request_category (when a human has explicitly set
    it) takes priority; it's schema-constrained to the same four values
    already, so it's used as-is. Otherwise the plan's default_request_category
    is translated down via PLAN_CATEGORY_TO_PIPELINE_CATEGORY."""
    case_value = case.get("request_category")
    if case_value:
        return case_value
    plan_value = plan.get("default_request_category")
    return PLAN_CATEGORY_TO_PIPELINE_CATEGORY.get(plan_value, "ordinary")


def get_national_id(patient_id: int) -> Optional[str]:
    rows = sb.select(PATIENTS_TABLE, select="national_id", filters={"id": f"eq.{patient_id}"})
    return rows[0]["national_id"] if rows else None


# =====================================================================
# Shared Stages 2-6 — sign, report, merge, upload — identical regardless
# of whether the document was a cache hit or a freshly-approved review.
# =====================================================================

def run_finalize_stages(session: SMCSession, case_id: int, attempt_id: int, national_id: str,
                         pipeline_state: dict, patient_pdf_path: str) -> dict:
    """pipeline_state must contain: pre_request_id, full_name, tumor_cfg
    (dict), medical_report_text. Returns a result dict shaped like
    process_row()'s: status SUCCESS/FAILED, final_request_no, error."""
    pre_request_id = pipeline_state["pre_request_id"]
    full_name = pipeline_state["full_name"]
    tumor_cfg = pipeline_state["tumor_cfg"]
    medical_report_text = pipeline_state["medical_report_text"]

    try:
        log.info(f"  [Stage 2] Rendering + signing MDT form for pre_request_id={pre_request_id} …")
        mdt_signed_bytes = call_with_reconnect(session, "MDT render/sign", stage_render_and_sign,
                                                session, pre_request_id, broad=True)

        log.info("  [Stage 3] Building medical report …")
        report_pdf_bytes = build_medical_report_pdf(full_name, national_id, medical_report_text, tumor_cfg)

        log.info("  [Stage 5] Merging MDT + report + patient document …")
        os.makedirs(MERGED_PDF_DIR, exist_ok=True)
        merged_pdf_path = os.path.join(MERGED_PDF_DIR, f"{national_id}_{pre_request_id}.pdf")
        merge_final_pdf(mdt_signed_bytes, report_pdf_bytes, patient_pdf_path, merged_pdf_path)

        if not os.path.exists(merged_pdf_path) or os.path.getsize(merged_pdf_path) < 20_000:
            return {"status": "FAILED", "error": f"Merged PDF write failed or suspiciously small: {merged_pdf_path}"}

        log.info("  [Stage 6] Uploading merged PDF to the website …")
        final_request_no = call_with_reconnect(session, "Final upload", stage_upload_merged_pdf,
                                                session, national_id, pre_request_id, merged_pdf_path, tumor_cfg)

        # NOT re-uploaded to R2 here, on purpose — per your instruction,
        # the script never needs to keep a copy of the PDF it just used.
        # R2 population is entirely the labeling step's job now (see
        # decree_submission_prepare.py's stage_and_flag_for_review): a
        # human reviews/cleans a freshly-extracted document and saves the
        # result to R2 themselves, which is what makes the NEXT request
        # for that same patient a cache hit. This script only ever reads
        # from R2, never writes to it.
        return {"status": "SUCCESS", "final_request_no": final_request_no, "pre_request_id": pre_request_id}

    except Exception as exc:
        log.exception(f"case {case_id}: finalize stages failed")
        return {"status": "FAILED", "error": str(exc)}


def write_submission_result(case_id: int, attempt_id: int, result: dict, request_category: str):
    if result["status"] == "SUCCESS":
        sb.update(ATTEMPTS_TABLE, attempt_id, {
            "attempt_status": "SUBMITTED",
            "website_request_id": result["final_request_no"],
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "document_review_status": "not_required",
        })
        sb.update(CASES_TABLE, case_id, {"case_status": "SUBMITTED", "request_category": request_category})
        log_event(case_id, attempt_id, "submitted", {"final_request_no": result["final_request_no"],
                                                       "pre_request_id": result.get("pre_request_id")})
    else:
        msg = result.get("error") or "Submission failed at the sign/report/merge/upload stage."
        open_requirement(case_id, attempt_id, msg)
