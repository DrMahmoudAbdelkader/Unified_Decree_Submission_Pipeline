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
    locate_patient_document_pdf, resolve_tumor_type, resolve_request_category, resolve_effective_proc_id
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

    pipeline_key = aliases.get(case["tumor_type"])
    if not pipeline_key:
        msg = (f"نوع الورم \"{case['tumor_type']}\" غير مربوط بعد بأنواع الأورام في السكربت. "
               f"اطلب من أحد المسؤولين إضافته من إعدادات أنواع الأورام (جدول cancer_type_aliases).")
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

    canonical, tumor_cfg_base = resolve_tumor_type(pipeline_key)
    if canonical is None:
        msg = f"لم يتم التعرف على كود الورم \"{pipeline_key}\" في السكربت — راجع جدول cancer_type_aliases."
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
        "results": results,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"## Decree prepare run\n- Submitted: **{summary['submitted']}**\n"
                    f"- Pending review: **{summary['pending_review']}**\n"
                    f"- Needs attention: **{summary['requirement_opened']}**\n")

    if summary["total"] > 0 and summary["submitted"] == 0 and summary["pending_review"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
