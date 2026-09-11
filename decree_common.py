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
import re
from datetime import datetime, timezone
from typing import Dict, Optional

import Unified_Decree_Submission_Pipeline as _pipeline_module
from Unified_Decree_Submission_Pipeline import (
    SMCSession,
    stage_render_and_sign,
    stage_upload_merged_pdf,
    stage_fix_uhi_exclusion_and_reprint_mdt,
    is_deep_uhi_upload_error,
    is_max_requests_reached_error,
    RowProcessingError,
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

# DEBUG_RENDER_MDT_AND_STOP=1 output lives here (see
# decree_submission_prepare.debug_dump_mdt_and_stop) — a SEPARATE
# directory from MERGED_PDF_DIR on purpose, so the debug artifact upload
# step in the workflow can't accidentally pick up a real merged
# submission PDF, and vice versa.
DEBUG_MDT_DIR = "/tmp/decree_mdt_debug"

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
    # WKHTMLTOPDF_PATH removed - MDT print rendering now uses Playwright/
    # Chromium (installed by the workflow's "playwright install" step),
    # not a wkhtmltopdf binary.
    _pipeline_module.PATIENT_DOCS_ROOT = os.environ.get("PATIENT_DOCS_ROOT", PATIENT_DOC_CACHE_DIR)
    _pipeline_module.PATIENT_DOCS_UNDER_PROCESSED_DIR = os.path.join(_pipeline_module.PATIENT_DOCS_ROOT, "UNDER_PROCESSED")
    # FIXED BUG: Unified_Decree_Submission_Pipeline.FALLBACK_PATIENT_DOCS_DIR
    # is a plain module-level string, computed ONCE at import time from the
    # hardcoded D:\SMS\... Windows path (before this function ever runs).
    # Re-pointing PATIENT_DOCS_UNDER_PROCESSED_DIR above does NOT change it,
    # because it's a separate variable, not a live reference — so every
    # website/CMIS-fallback download was still landing in that stale
    # Windows-literal folder name on the Linux runner instead of anywhere
    # this script (or R2) could find it. This is the actual cause of the
    # "Downloaded to D:\SMS\..." log lines. Must be repointed explicitly.
    _pipeline_module.FALLBACK_PATIENT_DOCS_DIR = _pipeline_module.PATIENT_DOCS_UNDER_PROCESSED_DIR
    os.makedirs(_pipeline_module.PATIENT_DOCS_UNDER_PROCESSED_DIR, exist_ok=True)
    os.makedirs(_pipeline_module.PATIENT_DOCS_ROOT, exist_ok=True)


def _install_mdt_form_font() -> None:
    """Downloads the MDT-form font (see supabase_storage.fetch_mdt_form_font)
    into a USER-level font directory and refreshes fontconfig's cache -
    deliberately ~/.local/share/fonts, not /usr/share/fonts, so this needs
    no sudo on the GitHub-hosted runner. fontconfig searches user font
    dirs by default, so Chromium (via Playwright) picks it up the same as
    a system-wide install would. Non-fatal on failure - see
    fetch_mdt_form_font()'s own docstring for why."""
    font_dir = os.path.expanduser("~/.local/share/fonts")
    try:
        paths = supabase_storage.fetch_mdt_form_font(font_dir)
        if paths:
            import subprocess
            subprocess.run(["fc-cache", "-f", font_dir], check=False,
                            capture_output=True)
            log.info(f"Installed MDT-form font(s) into {font_dir}: {list(paths.values())}")
            # Hand the real local paths straight to render_print_page_to_pdf()
            # so it can inject an explicit @font-face itself (see that
            # function's comment) instead of relying on fontconfig to have
            # matched the file's internal name-table family against
            # whatever family the SMC page's CSS actually asks for.
            _pipeline_module.MDT_FORM_FONT_PATHS = paths
            # One-shot diagnostic so a future run's log says definitively
            # whether the font that landed on disk is actually named
            # "Tahoma" (or whatever family the page wants) as far as
            # fontconfig is concerned - if it isn't, that's exactly why a
            # plain fc-cache install (no @font-face override) would still
            # silently fall back, with nothing in the log to show why.
            fc_check = subprocess.run(
                ["fc-list", ":", "family"], check=False,
                capture_output=True, text=True,
            )
            log.info(f"fc-list after install (fontconfig's view of available families):\n"
                     f"{fc_check.stdout}")
        else:
            log.warning(
                "fetch_mdt_form_font() returned no paths - MDT_FORM_FONT_PATHS stays "
                "empty, render_print_page_to_pdf() will fall back to fontconfig "
                "substitution and log a warning when it does."
            )
    except Exception as exc:
        # Never let a font-install hiccup take down the whole run - the
        # MDT form still renders, just with a fallback font.
        log.warning(f"Could not install MDT-form font ({exc}) - continuing with fallback font.")


def fetch_and_configure_assets() -> Dict[str, str]:
    """Downloads signatures + medical report template from Supabase
    Storage, and points every hardcoded local-PC path in the pipeline
    (SIGNATURE_FILES, medical_report_overlay's template/font paths) at
    the downloaded/apt-installed equivalents. Raises RuntimeError with a
    specific message if anything required is missing — never lets a
    downstream FileNotFoundError surface unexplained.

    Also installs the MDT-form font (separate from the medical-report's
    Amiri font below - see _install_mdt_form_font()) so the Chromium
    render in render_print_page_to_pdf() has the same font the SMC page's
    CSS actually asks for, instead of falling back to a wider generic
    sans and reflowing the form's fixed-width fields."""
    assets = supabase_storage.fetch_signing_and_template_assets(ASSETS_DIR)
    _install_mdt_form_font()

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
        "created_by": "9b8fa9a6-0567-4a93-8e21-d0f8bc098394"
    })


def _normalize_plan_name(name: Optional[str]) -> str:
    """MUST match smc-submissions.js's normalizePlanName() exactly (trim,
    lowercase, collapse whitespace) — this is what lets a plan name typed
    once in the module compare equal here, in the JS readiness check, and
    in decree-request-entry.js's own catalogPlanForCase(), regardless of
    which of the three actually resolves a given case first."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


_active_official_plans_cache: Optional[list] = None


def _active_official_plans() -> list:
    """Fetched ONCE per process (module-level cache), not once per case —
    prepare.py calls resolve_plan() in a loop over every case in the
    batch, and this catalog is the same ~2000+-row active table on every
    call. Same reasoning as smc-submissions.js's loadSmcSubmissionData(),
    which fetches its copy once per page load rather than per row."""
    global _active_official_plans_cache
    if _active_official_plans_cache is None:
        _active_official_plans_cache = sb.select(PLANS_TABLE, select="*", filters={"is_active": "is.true"})
    return _active_official_plans_cache


def _best_official_match(target_name: str) -> Optional[dict]:
    """Same tie-break scoring as resolvePlanForCase()/catalogPlanForCase()
    in the JS side: prefer whichever same-named official plan has the most
    complete data (protocol code, website wording, MDT text)."""
    if not target_name:
        return None
    rows = _active_official_plans()
    matches = [r for r in rows if _normalize_plan_name(r.get("reception_display_name")) == target_name]
    if not matches:
        return None

    def _score(p: dict) -> int:
        return (
            int(bool(p.get("website_protocol_code")))
            + int(bool(p.get("website_submission_treatment_plan")))
            + int(bool(p.get("mdt_treatment_plan_text")))
        )

    matches.sort(key=_score, reverse=True)
    return matches[0]


def resolve_plan(case: dict) -> Optional[dict]:
    """Resolves the ACTUAL plan that should govern this case — NOT
    necessarily the plan case['plan_source']/treatment_plan_id/
    custom_treatment_plan_id still points to.

    FIXED: this used to be a blind FK-only lookup. "ربط الخطة المخصصة
    بخطة موجودة" (mapCustomPlanToExisting() in decree-request-entry.js)
    clones the matched official plan's data into a brand-new
    decree_treatment_plans row and sets decree_custom_treatment_plans.
    is_active = false on the original custom row — but it deliberately
    does NOT repoint the case's own custom_treatment_plan_id FK to that
    new row (see decree-request-entry.js's NOTE ON
    map_custom_treatment_plan_to_existing). So a case created against a
    custom plan that was LATER tied to an official one still has
    plan_source = 'CUSTOM' and an FK pointing at the now-retired
    (is_active=false, but NOT deleted) custom row.

    A naive FK lookup here still finds that retired custom row fine (it's
    fetched by id, not filtered on is_active) — so this never crashed or
    returned None outright. But it hands back the OLD custom row, whose
    mdt_treatment_plan_text nobody keeps updated once a plan is tied to an
    official one — the real, current MDT text lives on the NEW official
    row. That's what was silently reaching Stage 2 with stale/blank plan
    text, and exactly why the SAME case reads correctly in
    decree-request-entry.js (whose own catalogPlanForCase() already
    re-resolves by name, never trusting this FK) but failed here / showed
    "لا توجد خطة علاجية مرتبطة" or "لا تحتوي على نص خطة MDT" wherever this
    function's result was used to judge readiness.

    This mirrors resolvePlanForCase() in smc-submissions.js exactly: for
    a CUSTOM-sourced case, re-resolve fresh by NAME against the live
    active official catalog every time, rather than trusting the
    plan_source/FK snapshot. Only if no official plan shares the custom
    row's name does the (still genuinely custom) row get used as-is.
    """
    if case.get("plan_source") == "OFFICIAL" and case.get("treatment_plan_id"):
        rows = sb.select(PLANS_TABLE, select="*", filters={"id": f"eq.{case['treatment_plan_id']}"})
        return rows[0] if rows else None

    if not case.get("custom_treatment_plan_id"):
        return None

    custom_rows = sb.select(CUSTOM_PLANS_TABLE, select="*",
                             filters={"id": f"eq.{case['custom_treatment_plan_id']}"})
    if not custom_rows:
        return None
    custom_row = custom_rows[0]

    target_name = _normalize_plan_name(custom_row.get("reception_display_name"))
    if not target_name:
        return custom_row

    official_match = _best_official_match(target_name)
    return official_match or custom_row  # never tied to an official plan — still genuinely custom


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
        try:
            final_request_no = call_with_reconnect(session, "Final upload", stage_upload_merged_pdf,
                                                    session, national_id, pre_request_id, merged_pdf_path, tumor_cfg)
        except RowProcessingError as upload_exc:
            if is_max_requests_reached_error(upload_exc):
                # Site-side per-patient concurrent-open-request cap, NOT a
                # UHI/insurance issue - the UHI-exclusion edit below never
                # clears it on retry (same string comes back every time)
                # and would wrongly flag a possibly-insured patient as
                # UHI-excluded. The MDT itself was created fine; only this
                # final upload is blocked. Fail distinctly so a human
                # knows to retry later, not to re-run a UHI fix.
                raise RowProcessingError(
                    f"Patient {national_id} hit the site's max-open-requests limit for "
                    f"MDT #{pre_request_id} (Requests/SearchSSN returned "
                    "'MaxNumberOfRequestsReached'). This is a site-side per-patient "
                    "concurrent-request cap, NOT a UHI/insurance issue - the MDT itself "
                    "was created fine and was left untouched. Retry this case on its own "
                    "later, once this patient's other open request(s) have cleared."
                ) from upload_exc

            if not is_deep_uhi_upload_error(upload_exc):
                raise

            # The quick HASINSURANCE=N/UHIEXCLUDED=Y resend inside
            # stage_upload_merged_pdf already tried and failed for a
            # genuine UHI-insurance block. Fall back to editing the
            # pre-request + re-printing the MDT form, then rebuild the
            # merged PDF with the CORRECTED form and retry the upload
            # once. The previous merged PDF (built from the faulty/stuck
            # first print) is overwritten/discarded here.
            log.warning(f"  case {case_id}: UHI-blocked after standard resend — editing "
                        f"pre-request + re-printing MDT form.")
            mdt_signed_bytes = call_with_reconnect(
                session, "UHI edit + MDT reprint", stage_fix_uhi_exclusion_and_reprint_mdt,
                session, national_id, pre_request_id, broad=True,
            )
            log.info("  Re-merging with the corrected MDT form …")
            try:
                os.remove(merged_pdf_path)
            except OSError:
                pass
            merge_final_pdf(mdt_signed_bytes, report_pdf_bytes, patient_pdf_path, merged_pdf_path)
            if not os.path.exists(merged_pdf_path) or os.path.getsize(merged_pdf_path) < 20_000:
                return {"status": "FAILED",
                        "error": f"Re-merged PDF (post-UHI-fix) write failed or suspiciously small: {merged_pdf_path}"}
            log.info("  Retrying upload with the corrected merged PDF …")
            final_request_no = call_with_reconnect(
                session, "Final upload (retry after UHI fix)", stage_upload_merged_pdf,
                session, national_id, pre_request_id, merged_pdf_path, tumor_cfg,
            )

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
