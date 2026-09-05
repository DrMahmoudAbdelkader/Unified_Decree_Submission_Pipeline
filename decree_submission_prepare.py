#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decree_submission_prepare.py
==========================================================================
Phase 1/2 combined entrypoint — triggered by the module's "إرسال" click
(same edge function as before, phase="prepare").

For each READY_TO_SUBMIT case:
  1. Same pre-flight checks as the single-phase version (cancer type
     alias, plan linked + has text, patient found) — failures open a
     decree_request_requirements row exactly as before.
  2. Stage 1: creates the MDT request on SMC (stage_create_mdt) —
     unchanged pipeline logic.
  3. Locates the patient document via locate_patient_document_pdf(),
     with a custom find_local_fn that checks the PERMANENT R2 cache
     first (see r2_client.py) instead of a local folder that doesn't
     exist on this runner.
       - R2 CACHE HIT (already reviewed and approved for this patient
         before): treated as a "local" find by locate_patient_document_pdf
         (newly_extracted=False) — no review needed. This run continues
         straight through Stages 2-6 (decree_common.run_finalize_stages)
         and finishes the submission in the SAME run.
       - NOT CACHED (genuinely new, or R2 not configured): falls through
         to the live SMC-website / CMIS-archive fallback extraction,
         same as before. Because this is freshly extracted, per the
         two-phase decision, this run STOPS here — uploads the extracted
         PDF to the Supabase Storage pending-review bucket, saves
         everything decree_submission_finalize.py will need to resume,
         and opens a requirement asking a human to review it in the
         module before the actual signing/merging/upload happens.

RUN LOCALLY (test one case before trusting the cron/click):
    export SUPABASE_URL=...  SUPABASE_SERVICE_ROLE_KEY=...
    export SMC_USERNAME=...  SMC_PASSWORD=...
    export R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=...
    export CASE_IDS=6
    python decree_submission_prepare.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(__file__))

import decree_common as common
from Unified_Decree_Submission_Pipeline import SMCSession, call_with_reconnect, stage_create_mdt, \
    locate_patient_document_pdf, resolve_tumor_type, resolve_request_category, resolve_effective_proc_id, \
    render_print_page_to_pdf, apply_signatures_and_stamp
import supabase_client as sb
import r2_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("decree_submission_prepare")


def parse_case_ids(raw: str) -> List[int]:
    raw = (raw or "").strip()
    if not raw:
        return []
    return [int(p.strip()) for p in raw.split(",") if p.strip().isdigit()]


def make_r2_aware_finder(national_id: str):
    """Passed as locate_patient_document_pdf's find_local_fn. Signature
    must match find_patient_id_pdf(patient_id) -> Optional[str] exactly,
    since it's a drop-in replacement."""
    def _find(patient_id: str) -> Optional[str]:
        return r2_client.download_if_exists(patient_id, common.PATIENT_DOC_CACHE_DIR)
    return _find


def debug_mode_on() -> bool:
    return os.environ.get("DEBUG_RENDER_MDT_AND_STOP", "").strip() in ("1", "true", "True")


def debug_dump_mdt_and_stop(session: SMCSession, case_id: int, attempt_id: int,
                             pre_request_id: str, national_id: str) -> dict:
    """DEBUG_RENDER_MDT_AND_STOP=1 path.

    Renders the print page for the MDT that stage_create_mdt() just
    created and STOPS — no locate_patient_document_pdf, no medical
    report, no merge, no upload. The point is to isolate exactly the
    piece that's been unreliable (the HTML->PDF render on the
    GitHub-hosted runner) from every other stage, so you can inspect it
    on its own before trusting the pipeline to go on and actually submit.

    Writes TWO files so a bad render can be told apart from a bad
    signature overlay:
      *_00_raw_render.pdf    - straight out of render_print_page_to_pdf(),
                                 no signatures/stamp - this is the one
                                 that shows whether the font/layout fix
                                 actually worked.
      *_01_signed.pdf        - the same render with apply_signatures_and_stamp()
                                 applied, i.e. exactly what would have been
                                 merged with the medical report + patient
                                 document and uploaded, had this not been
                                 a debug run.

    Both land in decree_common.DEBUG_MDT_DIR, which the workflow uploads
    as its OWN separate run artifact - download it from the Actions run
    page, open both PDFs, compare against a known-good MDT PDF, then
    delete the artifact from the run page once you're done with it.

    Deliberately does NOT call open_requirement() or update
    case_status/attempt_status - the case is left exactly as
    READY_TO_SUBMIT, so a normal (non-debug) run afterwards reprocesses
    it as if this debug run never happened. This DOES still create a
    real, new MDT on the SMC server (stage_create_mdt already ran for
    real before this function is even called) - that's unavoidable,
    since the print page this function renders only exists right after
    a genuine creation.
    """
    os.makedirs(common.DEBUG_MDT_DIR, exist_ok=True)

    log.info(f"[DEBUG_RENDER_MDT_AND_STOP] Rendering MDT print page for pre_request_id={pre_request_id} …")
    raw_bytes = call_with_reconnect(session, "MDT render (debug)", render_print_page_to_pdf,
                                     session, pre_request_id, broad=True)
    raw_path = os.path.join(common.DEBUG_MDT_DIR, f"{national_id}_{pre_request_id}_00_raw_render.pdf")
    with open(raw_path, "wb") as f:
        f.write(raw_bytes)

    signed_bytes = apply_signatures_and_stamp(raw_bytes)
    signed_path = os.path.join(common.DEBUG_MDT_DIR, f"{national_id}_{pre_request_id}_01_signed.pdf")
    with open(signed_path, "wb") as f:
        f.write(signed_bytes)

    log.info(f"[DEBUG_RENDER_MDT_AND_STOP] Wrote {raw_path}")
    log.info(f"[DEBUG_RENDER_MDT_AND_STOP] Wrote {signed_path}")

    common.log_event(case_id, attempt_id, "debug_mdt_render_stop",
                      {"pre_request_id": pre_request_id, "raw_path": raw_path, "signed_path": signed_path})

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(
                "## DEBUG_RENDER_MDT_AND_STOP\n"
                f"- pre_request_id: **{pre_request_id}**\n"
                "- Stopped after Stage 1 (MDT creation) + Stage 2 (render only) — "
                "no report, merge, or upload happened.\n"
                "- The two PDFs are in this run's **decree-mdt-debug-<run id>** artifact below — "
                "download it, open both, and compare against a known-good MDT PDF.\n"
                "- This case's status was left as READY_TO_SUBMIT — run the workflow again "
                "without the debug flag to submit it for real once the render looks right.\n"
            )

    return {"case_id": case_id, "status": "debug_stopped", "pre_request_id": pre_request_id,
            "raw_path": raw_path, "signed_path": signed_path}


def stage_and_flag_for_review(case: dict, attempt_id: int, national_id: str, pre_request_id: str, full_name: str,
                               tumor_cfg: dict, medical_report_text: str, request_category: str,
                               extracted_pdf_path: str, doc_source: str):
    case_id = case["id"]

    if not r2_client.upload_pending(national_id, extracted_pdf_path):
        common.open_requirement(case_id, attempt_id,
                                 "تم استخراج مستند المريض لكن تعذر رفعه للمراجعة على Cloudflare R2 — حاول مرة أخرى.")
        return

    review_url = r2_client.pending_review_url(national_id)

    pipeline_state = {
        "pre_request_id": pre_request_id,
        "full_name": full_name,
        "tumor_cfg": tumor_cfg,
        "medical_report_text": medical_report_text,
        "request_category": request_category,
        "doc_source": doc_source,
        "review_url": review_url,
    }

    sb.update(common.ATTEMPTS_TABLE, attempt_id, {
        "document_review_status": "pending_review",
        "document_storage_key": f"r2:pending/{national_id}.pdf",
        "pipeline_state": pipeline_state,
    })
    sb.update(common.CASES_TABLE, case_id, {"case_status": "PENDING"})

    msg = ("تم إنشاء طلب MDT بنجاح (رقم مبدئي: {pre}) وتم استخراج مستند مريض جديد يحتاج مراجعة "
           "قبل المتابعة. راجع المستند ثم اضغط \"موافقة ومتابعة\" لإكمال التوقيع وإنشاء التقرير الطبي "
           "ورفع الطلب.{link}").format(
        pre=pre_request_id,
        link=f"\nرابط المراجعة: {review_url}" if review_url else "",
    )
    common.log_event(case_id, attempt_id, "extraction_pending_review",
                      {"pre_request_id": pre_request_id, "doc_source": doc_source, "review_url": review_url})
    sb.insert(common.REQUIREMENTS_TABLE, {"case_id": case_id, "attempt_id": attempt_id,
                                           "requirement_text": msg, "status": "OPEN"})


def prepare_one_case(session: SMCSession, case: dict, aliases: Dict[str, str]) -> dict:
    case_id = case["id"]

    # Resolution order:
    #   1. Supabase cancer_type_aliases table, if this case's exact
    #      tumor_type text has a hand-added override row there (kept as an
    #      escape hatch for one-off text your module produces that you'd
    #      rather remap from the dashboard than by editing this script).
    #   2. Otherwise, the raw tumor_type text itself, straight into
    #      resolve_tumor_type() below — this is now the primary path, since
    #      the decree-request module's Excel export writes plain diagnosis
    #      text (e.g. "Breast Cancer") and TUMOR_TYPE_ALIASES in the
    #      pipeline script already recognizes that text directly. The
    #      Supabase table is no longer required for a case to go through.
    pipeline_key = aliases.get(case["tumor_type"]) or case["tumor_type"]

    canonical, tumor_cfg_base = resolve_tumor_type(pipeline_key)
    if canonical is None:
        msg = (f"لم يتم التعرف على نوع الورم \"{case['tumor_type']}\" لا في جدول "
               f"cancer_type_aliases ولا في TUMOR_TYPE_ALIASES بالسكربت. "
               f"أضف نوع الورم هذا في السكربت (Unified_Decree_Submission_Pipeline.py) "
               f"أو أضف تحويلاً له في جدول cancer_type_aliases.")
        common.open_requirement(case_id, None, msg)
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}

    plan = common.resolve_plan(case)
    if not plan:
        msg = "لا توجد خطة علاجية مرتبطة بهذا الطلب — أضف خطة قبل الإرسال."
        common.open_requirement(case_id, None, msg)
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}

    texts = common.resolve_plan_texts(plan)
    if not texts["mdt_text"].strip():
        msg = "الخطة العلاجية المرتبطة بهذا الطلب لا تحتوي على نص — أكمل نص الخطة قبل الإرسال."
        common.open_requirement(case_id, None, msg)
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}

    request_category_raw = common.resolve_request_category_value(case, plan)

    national_id = common.get_national_id(case["patient_id"])
    if not national_id:
        msg = f"لم يتم العثور على بيانات المريض (id={case['patient_id']}) — لا يمكن تحديد الرقم القومي."
        common.open_requirement(case_id, None, msg)
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}

    request_category = resolve_request_category(request_category_raw)
    if request_category is None:
        msg = f"فئة الطلب \"{request_category_raw}\" غير معروفة — يجب أن تكون scan أو surgery أو فارغة (عادي)."
        common.open_requirement(case_id, None, msg)
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}

    tumor_cfg = dict(tumor_cfg_base)
    tumor_cfg["proc_id"] = resolve_effective_proc_id(tumor_cfg_base, request_category)

    existing_attempts = sb.select(common.ATTEMPTS_TABLE, select="id,attempt_number", filters={"case_id": f"eq.{case_id}"})
    attempt_number = max((a["attempt_number"] for a in existing_attempts), default=0) + 1
    attempt = sb.insert(common.ATTEMPTS_TABLE, {
        "case_id": case_id,
        "attempt_number": attempt_number,
        "website_submission_treatment_plan": texts["mdt_text"],
        "attempt_status": "READY_TO_SUBMIT",
    })
    attempt_id = attempt.get("id")

    try:
        mdt_out = call_with_reconnect(session, "MDT creation", stage_create_mdt,
                                       session, national_id, texts["mdt_text"], tumor_cfg)
    except Exception as exc:
        log.exception(f"case {case_id}: MDT creation failed")
        msg = f"فشل إنشاء طلب MDT — حاول مرة أخرى بعد قليل. ({exc})"
        common.open_requirement(case_id, attempt_id, msg)
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}

    pre_request_id = mdt_out["pre_request_id"]
    full_name = mdt_out["full_name"]

    if debug_mode_on():
        return debug_dump_mdt_and_stop(session, case_id, attempt_id, pre_request_id, national_id)

    id_pdf_path, doc_source, newly_extracted = locate_patient_document_pdf(
        session, national_id, find_local_fn=make_r2_aware_finder(national_id),
    )

    if not id_pdf_path:
        msg = (f"لم يتم العثور على مستند المريض لا في الأرشيف الدائم ولا على موقع SMC ولا في أرشيف CMIS. "
               f"تم إنشاء طلب MDT رقم {pre_request_id} بالفعل على الموقع — يحتاج هذا الطلب مستند مريض قبل المتابعة.")
        common.open_requirement(case_id, attempt_id, msg)
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}

    if newly_extracted:
        stage_and_flag_for_review(case, attempt_id, national_id, pre_request_id, full_name, tumor_cfg,
                                   texts["medical_report_text"], request_category, id_pdf_path, doc_source)
        return {"case_id": case_id, "status": "pending_review", "pre_request_id": pre_request_id}

    # Cache hit — already reviewed and approved for this patient before.
    # Continue straight through to submission in this same run.
    pipeline_state = {
        "pre_request_id": pre_request_id, "full_name": full_name, "tumor_cfg": tumor_cfg,
        "medical_report_text": texts["medical_report_text"],
    }
    result = common.run_finalize_stages(session, case_id, attempt_id, national_id, pipeline_state, id_pdf_path)
    common.write_submission_result(case_id, attempt_id, result, request_category)
    if result["status"] == "SUCCESS":
        return {"case_id": case_id, "status": "submitted", "request_number": result["final_request_no"]}
    return {"case_id": case_id, "status": "requirement_opened", "message": result.get("error")}


def main():
    common.configure_smc_credentials()
    common.fetch_and_configure_assets()  # fail fast if signatures/template aren't uploaded yet

    session = SMCSession()
    if not session.login():
        raise SystemExit("SMC login failed — check SMC_USERNAME/SMC_PASSWORD secrets.")

    aliases = common.load_cancer_type_aliases()
    case_ids = parse_case_ids(os.environ.get("CASE_IDS", ""))

    if debug_mode_on():
        # Safety guard: DEBUG_RENDER_MDT_AND_STOP still creates a REAL new
        # MDT on the SMC server per case it touches (see
        # debug_dump_mdt_and_stop's docstring). Refuse to run it across a
        # whole batch of READY_TO_SUBMIT cases by accident — this flag is
        # for isolating the render step on ONE known case, not a normal
        # run. Require the caller to pass exactly one CASE_IDS value.
        if len(case_ids) != 1:
            raise SystemExit(
                "DEBUG_RENDER_MDT_AND_STOP=1 requires exactly one case id in CASE_IDS "
                f"(got {case_ids or 'none'}) — it still creates a real MDT on the SMC server "
                "for every case it touches, so don't run it against a whole batch."
            )
        log.warning("DEBUG_RENDER_MDT_AND_STOP is ON — will create a real MDT for case "
                    f"{case_ids[0]}, render it, write both PDFs to {common.DEBUG_MDT_DIR}, "
                    "and stop there (no report/merge/upload).")

    filters = {"case_status": "eq.READY_TO_SUBMIT"}
    if case_ids:
        filters["id"] = f"in.({','.join(str(i) for i in case_ids)})"
    cases = sb.select(common.CASES_TABLE, select="*", filters=filters)
    log.info(f"{len(cases)} case(s) with case_status=READY_TO_SUBMIT" + (f" matching case_ids={case_ids}" if case_ids else ""))

    results = [prepare_one_case(session, case, aliases) for case in cases]
    summary = {
        "total": len(results),
        "submitted": sum(1 for r in results if r["status"] == "submitted"),
        "pending_review": sum(1 for r in results if r["status"] == "pending_review"),
        "requirement_opened": sum(1 for r in results if r["status"] == "requirement_opened"),
        "debug_stopped": sum(1 for r in results if r["status"] == "debug_stopped"),
        "results": results,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"## Decree prepare run\n- Submitted: **{summary['submitted']}**\n"
                    f"- Pending review: **{summary['pending_review']}**\n"
                    f"- Needs attention: **{summary['requirement_opened']}**\n"
                    f"- Debug-stopped (render only): **{summary['debug_stopped']}**\n")

    if summary["total"] > 0 and summary["submitted"] == 0 and summary["pending_review"] == 0 \
            and summary["debug_stopped"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
