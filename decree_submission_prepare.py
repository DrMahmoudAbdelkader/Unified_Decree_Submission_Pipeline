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
         same as before. If the SMC-website fallback finds it, this ALSO
         merges in the last 30 days of DMS/CMIS archive pages onto that
         freshly-downloaded file (see resolve_patient_document() /
         PREPARE_NEWLY_EXTRACTED_ARCHIVE_MERGE_DAYS_BACK below) — so the
         human review that follows sees the fully merged document, not
         the bare extraction. Because this is freshly extracted, per the
         two-phase decision, this run STOPS here — uploads the (already
         merged) PDF to R2's pending/<national_id>.pdf key (see
         r2_client.py; same bucket as the permanent cache, different
         prefix — NOT Supabase Storage), saves everything decree_
         submission_finalize.py will need to resume, and opens a
         requirement asking a human to review it in the module before the
         actual signing/merging/upload happens. decree_submission_
         finalize.py never re-merges archive pages — that already
         happened here, before the human ever saw the file.
       - SAME-PATIENT SIBLINGS: if another case for this same national_id
         already has an attempt sitting at pending_review earlier in THIS
         run, this case is never uploaded/reviewed a second time — see
         link_sibling_to_pending_review() and pending_review_by_patient
         below. It's parked at document_review_status='awaiting_linked_review'
         and finalize_linked_siblings() (decree_submission_finalize.py)
         finishes it automatically the moment the primary case's document
         is approved, reusing that one already-downloaded PDF.

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
    render_print_page_to_pdf, apply_signatures_and_stamp, shutdown_shared_browser, \
    find_and_verify_reusable_mdt, RECENT_ARCHIVE_DAYS_BACK
from patient_pdf_website_fallback import download_patient_pdf_from_website
from patient_pdf_dms_archive_fallback import refresh_local_pdf_with_recent_archive_docs
import supabase_client as sb
import r2_client

# How far back to look in the DMS/CMIS archive when a patient's document
# had to be freshly EXTRACTED this run (not an R2 cache hit) — widened
# from the pipeline's normal RECENT_ARCHIVE_DAYS_BACK (7, unchanged, still
# used for a cache-hit patient exactly as before) per your instruction,
# so the human reviewing the freshly-extracted file sees it WITH the last
# month of archive pages already merged in, not just the bare extraction.
# This merge now happens here, during prepare, BEFORE the file is staged
# to pending/ for review — not during finalize — so decree_common.
# run_finalize_stages() never has to re-merge (see its own docstring).
PREPARE_NEWLY_EXTRACTED_ARCHIVE_MERGE_DAYS_BACK = 30

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


def link_sibling_to_pending_review(case: dict, attempt_id: int, national_id: str, pre_request_id: str,
                                    full_name: str, tumor_cfg: dict, medical_report_text: str,
                                    request_category: str, doc_source: str, primary_case_id: int):
    """A SIBLING case for a patient that already has another case in this
    same run sitting at pending_review (see resolve_patient_document() /
    pending_review_by_patient in main()).

    Deliberately does NOT call r2_client.upload_pending() again (the
    primary case's stage_and_flag_for_review() already put the ONE copy
    of this patient's extracted document there) and does NOT set
    document_review_status='pending_review' or open a requirement — doing
    either would be exactly the bug being fixed: a second, independent
    "needs review" card for a document a human is already about to review
    once. Instead this attempt is parked at 'awaiting_linked_review' with
    enough pipeline_state to finish on its own, and
    decree_submission_finalize.py's cascade (see finalize_linked_siblings())
    picks it up automatically the moment the primary case is approved and
    successfully finalized — no second review, ever, for the same file.
    """
    case_id = case["id"]
    pipeline_state = {
        "pre_request_id": pre_request_id,
        "full_name": full_name,
        "tumor_cfg": tumor_cfg,
        "medical_report_text": medical_report_text,
        "request_category": request_category,
        "doc_source": doc_source,
        "linked_primary_case_id": primary_case_id,
    }
    sb.update(common.ATTEMPTS_TABLE, attempt_id, {
        "document_review_status": "awaiting_linked_review",
        "document_storage_key": f"r2:pending/{national_id}.pdf",
        "pipeline_state": pipeline_state,
    })
    sb.update(common.CASES_TABLE, case_id, {"case_status": "PENDING"})
    common.log_event(case_id, attempt_id, "linked_to_sibling_pending_review",
                      {"pre_request_id": pre_request_id, "primary_case_id": primary_case_id,
                       "national_id": national_id})


def resolve_patient_document(session: SMCSession, national_id: str, doc_cache: Dict[str, tuple]):
    """locate_patient_document_pdf() is the potentially-slow step (SMC
    website search + CMIS archive fallback when nothing's cached) — and
    when the same patient has more than one case in a single run, calling
    it once per CASE instead of once per PATIENT was both the root cause
    of the duplicate-review bug (two independent 'newly extracted'
    results for the exact same document) and pure wasted time (the same
    slow fallback search running twice for nothing). doc_cache is a plain
    dict scoped to this run, keyed by national_id, so every case for the
    same patient after the first one reuses the already-resolved result
    instead of hitting the network again.

    ARCHIVE MERGE WINDOW, per your latest instruction:
      - R2 CACHE HIT ("local" — this patient's document was already
        reviewed/approved on some earlier run): UNCHANGED. find_local_fn
        below is the only override on this path; locate_patient_document_
        pdf() calls the real refresh_local_pdf_with_recent_archive_docs()
        with its own default RECENT_ARCHIVE_DAYS_BACK (7) exactly as it
        always has, and finishes straight through to submission in this
        same run — no human review needed.
      - NEWLY EXTRACTED (not cached — had to be pulled from the SMC
        website, or failing that the full CMIS archive): the website-
        fallback branch now ALSO merges in the last
        PREPARE_NEWLY_EXTRACTED_ARCHIVE_MERGE_DAYS_BACK (30) days of DMS/
        CMIS archive pages, so the human review that happens next (via
        the app's document-review edge function) sees the fully merged
        document, not just the bare extraction. This is the ONLY thing
        that changed on this path — everything else (staging to
        pending/, opening the review requirement, moving to the next
        case) is untouched.

      _extraction_state is a plain closure flag, not a global: it's
      created fresh for every call, so it can only ever reflect THIS
      patient's resolution, never leak across patients or runs. It gets
      set to True only if _website_fn below actually returns a path (a
      genuine extraction) — never for a cache hit, and never for the
      full-CMIS-archive fallback (which locate_patient_document_pdf never
      calls refresh_fn for at all — that fallback already pulls every
      archived page there is, so a 'last N days' merge on top of it would
      be a no-op by construction, same as before this change).
    """
    if national_id in doc_cache:
        return doc_cache[national_id]

    extraction_state = {"newly_extracted": False}

    def _website_fn(session_, base_url, patient_id, output_dir):
        path = download_patient_pdf_from_website(session_, base_url, patient_id, output_dir)
        if path:
            extraction_state["newly_extracted"] = True
        return path

    def _refresh_fn(patient_id, pdf_path, days_back=RECENT_ARCHIVE_DAYS_BACK):
        if extraction_state["newly_extracted"]:
            days_back = PREPARE_NEWLY_EXTRACTED_ARCHIVE_MERGE_DAYS_BACK
        return refresh_local_pdf_with_recent_archive_docs(patient_id, pdf_path, days_back=days_back)

    result = locate_patient_document_pdf(
        session, national_id,
        find_local_fn=make_r2_aware_finder(national_id),
        website_fn=_website_fn,
        refresh_fn=_refresh_fn,
    )
    doc_cache[national_id] = result
    return result


def prepare_one_case(session: SMCSession, case: dict, aliases: Dict[str, str],
                      doc_cache: Dict[str, tuple], pending_review_by_patient: Dict[str, int],
                      existing_attempt_counts: Optional[Dict[int, int]] = None) -> dict:
    case_id = case["id"]

    # Resolution order:
    #   1. Supabase cancer_type_aliases table, if this case's exact
    #      tumor_type CODE (e.g. "OTHER_ONCOLOGY") has a hand-added
    #      override row there (kept as an escape hatch — but note it's a
    #      single GLOBAL override per code, so it can't distinguish one
    #      OTHER_CUSTOM case's real diagnosis from another's).
    #   2. FIXED — was previously never consulted here: case["tumor_type_custom"],
    #      the raw cancer_type_group text. Since decree-request-entry.js's
    #      deriveTumorTypeFromCancerGroup() rewrite, this field ALWAYS
    #      carries the real, verbatim, submission-synced cancer-type text
    #      (Arabic diagnosis name) for EVERY case — not just OTHER_CUSTOM
    #      ones — and is kept byte-for-byte in sync with the Arabic names
    #      registered in this script via _add_generic_tumor_type() (see
    #      that module's CANCER_TYPE_GROUP_TO_SUBMISSION_LABEL comment). A
    #      case filed under the generic "OTHER_ONCOLOGY" bucket is, by
    #      that module's own design, still a REAL, already-catalogued
    #      cancer type (e.g. "سرطان المبيض" -> ovarian_cancer, C56) — it
    #      just isn't one of the ~42 fixed TUMOR_TYPES dropdown codes.
    #      Passing only the coarse "OTHER_ONCOLOGY" string (as before)
    #      could never resolve to anything; the actual diagnosis text
    #      resolves it correctly, with no human judgment call needed.
    #   3. Falls back to the coarse tumor_type code itself if #2 didn't
    #      resolve (covers rows where tumor_type_custom is blank/stale) —
    #      the original primary path, still correct for the ~11
    #      hand-curated TUMOR_TYPES codes that match module-key aliases
    #      directly (e.g. BREAST_CANCER).
    # Only a case that fails ALL THREE — a genuinely unrecognized
    # cancer_type_group, i.e. real OTHER_CUSTOM — legitimately needs a
    # human to add a mapping. That's the only kind of "custom" left after
    # this fix.
    alias_override = aliases.get(case["tumor_type"])
    tumor_type_custom = (case.get("tumor_type_custom") or "").strip()

    pipeline_key = alias_override or tumor_type_custom or case["tumor_type"]
    canonical, tumor_cfg_base = resolve_tumor_type(pipeline_key)

    if canonical is None and not alias_override and tumor_type_custom:
        # tumor_type_custom didn't resolve either — last resort, try the
        # coarse code itself.
        canonical, tumor_cfg_base = resolve_tumor_type(case["tumor_type"])

    if canonical is None:
        msg = (f"لم يتم التعرف على نوع الورم \"{tumor_type_custom or case['tumor_type']}\" "
               f"(نوع الحالة: {case['tumor_type']}) لا في جدول cancer_type_aliases ولا في "
               f"TUMOR_TYPE_ALIASES بالسكربت. أضف نوع الورم هذا في السكربت "
               f"(Unified_Decree_Submission_Pipeline.py) أو أضف تحويلاً له في جدول "
               f"cancer_type_aliases.")
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

    # Use pre-fetched attempt count if available (batch optimization), else query individually.
    if existing_attempt_counts is not None and case_id in existing_attempt_counts:
        attempt_number = existing_attempt_counts[case_id] + 1
    else:
        existing_attempts = sb.select(common.ATTEMPTS_TABLE, select="id,attempt_number", filters={"case_id": f"eq.{case_id}"})
        attempt_number = max((a["attempt_number"] for a in existing_attempts), default=0) + 1
    attempt = sb.insert(common.ATTEMPTS_TABLE, {
        "case_id": case_id,
        "attempt_number": attempt_number,
        "website_submission_treatment_plan": texts["mdt_text"],
        "attempt_status": "READY_TO_SUBMIT",
    })
    attempt_id = attempt.get("id")

    # REUSE CHECK: if an EARLIER attempt at this same case already created
    # an MDT on SMC (recorded in that attempt's pipeline_state) and this
    # case only came back to READY_TO_SUBMIT because something AFTER MDT
    # creation failed (signing, the medical report, the merge, or the
    # final upload — see write_submission_result()'s revert-to-
    # READY_TO_SUBMIT logic), reuse that same MDT instead of creating a
    # brand-new, redundant one for the same patient. Confirmed safe via
    # a live GetPreRequests lookup (find_and_verify_reusable_mdt) right
    # before trusting it — an MDT already converted/submitted, or one
    # SMC no longer has, is never reused; this always falls back to a
    # normal fresh creation in that case, exactly as before this existed.
    mdt_out = None
    prior_state = common.find_prior_pre_request_id(case_id, exclude_attempt_id=attempt_id)
    if prior_state:
        try:
            mdt_out = call_with_reconnect(
                session, "MDT reuse check", find_and_verify_reusable_mdt,
                session, national_id, prior_state["pre_request_id"],
            )
        except Exception:
            log.exception(f"case {case_id}: MDT reuse check failed — falling back to a fresh MDT creation")
            mdt_out = None
        if mdt_out:
            common.log_event(case_id, attempt_id, "reused_existing_mdt",
                              {"pre_request_id": mdt_out["pre_request_id"]})

    if not mdt_out:
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

    # Persisted immediately — independent of whatever happens in the rest
    # of THIS run — so that if this attempt also fails somewhere further
    # down (report/merge/upload), the NEXT run's reuse check above always
    # has something to find. Previously this was only ever saved for the
    # two human-review paths below; a case that hit the cache (no review
    # needed) and then failed at the finalize stage left its attempt row
    # with pipeline_state still empty, which is exactly what was causing
    # every retry to recreate the MDT from scratch.
    sb.update(common.ATTEMPTS_TABLE, attempt_id, {
        "pipeline_state": {"pre_request_id": pre_request_id, "full_name": full_name, "tumor_cfg": tumor_cfg},
    })

    if debug_mode_on():
        return debug_dump_mdt_and_stop(session, case_id, attempt_id, pre_request_id, national_id)

    # Resolved ONCE per patient per run (see resolve_patient_document's
    # docstring) — a second case for the same patient reuses this instead
    # of re-searching the SMC website / CMIS archive from scratch.
    id_pdf_path, doc_source, newly_extracted = resolve_patient_document(session, national_id, doc_cache)

    if not id_pdf_path:
        msg = (f"لم يتم العثور على مستند المريض لا في الأرشيف الدائم ولا على موقع SMC ولا في أرشيف CMIS. "
               f"تم إنشاء طلب MDT رقم {pre_request_id} بالفعل على الموقع — يحتاج هذا الطلب مستند مريض قبل المتابعة.")
        common.open_requirement(case_id, attempt_id, msg)
        return {"case_id": case_id, "status": "requirement_opened", "message": msg}

    if newly_extracted:
        primary_case_id = pending_review_by_patient.get(national_id)
        if primary_case_id is None:
            # First case for this patient in this run to need review —
            # this is the ONE card the human will see and label.
            stage_and_flag_for_review(case, attempt_id, national_id, pre_request_id, full_name, tumor_cfg,
                                       texts["medical_report_text"], request_category, id_pdf_path, doc_source)
            pending_review_by_patient[national_id] = case_id
            return {"case_id": case_id, "status": "pending_review", "pre_request_id": pre_request_id}
        # A sibling case for a patient that's already queued for review
        # above — link it instead of opening a second review for the same
        # physical document.
        link_sibling_to_pending_review(case, attempt_id, national_id, pre_request_id, full_name, tumor_cfg,
                                        texts["medical_report_text"], request_category, doc_source, primary_case_id)
        return {"case_id": case_id, "status": "pending_review_linked",
                "pre_request_id": pre_request_id, "linked_primary_case_id": primary_case_id}

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
    # Ordered by created_at so that, when a patient has more than one
    # READY_TO_SUBMIT case, the OLDEST one is always the "primary" that
    # gets the human-reviewable card (see pending_review_by_patient
    # above) — deterministic and predictable, rather than depending on
    # whatever order Postgres happens to return rows in.
    cases = sb.select(common.CASES_TABLE, select="*", filters=filters, order="created_at.asc")
    log.info(f"{len(cases)} case(s) with case_status=READY_TO_SUBMIT" + (f" matching case_ids={case_ids}" if case_ids else ""))

    results = []
    # Performance: batch-fetch all national_ids for this run's cases in
    # one DB round-trip instead of one per case (see decree_common.py's
    # prefetch_national_ids / get_national_id cache).
    patient_ids = list({c["patient_id"] for c in cases if c.get("patient_id")})
    if patient_ids:
        common.prefetch_national_ids(patient_ids)

    # Performance: batch-fetch existing attempt counts for all cases in one
    # round-trip so prepare_one_case() doesn't need a separate query per case.
    existing_attempt_counts: Dict[int, int] = {}
    if cases:
        case_id_list = [c["id"] for c in cases]
        try:
            attempt_rows = sb.select(
                common.ATTEMPTS_TABLE, select="case_id,attempt_number",
                filters={"case_id": f"in.({','.join(str(i) for i in case_id_list)})"},
            )
            for row in attempt_rows:
                cid = row["case_id"]
                existing_attempt_counts[cid] = max(existing_attempt_counts.get(cid, 0), row["attempt_number"])
        except Exception as e:
            log.warning(f"Could not batch-fetch attempt counts ({e}) — will query per-case instead.")

    # Scoped to this one run: reused across every case below so that (a)
    # a patient with more than one case only has their document searched
    # for once (see resolve_patient_document()), and (b) only their FIRST
    # such case opens a human-reviewable card — see
    # link_sibling_to_pending_review() for why the rest link to it instead
    # of duplicating it.
    doc_cache: Dict[str, tuple] = {}
    pending_review_by_patient: Dict[str, int] = {}
    try:
        for case in cases:
            # !! CRASH-ISOLATION FIX !!
            # This used to be a bare `results.append(prepare_one_case(...))`
            # with NO per-case try/except — an unhandled exception ANYWHERE
            # inside prepare_one_case() (e.g. link_sibling_to_pending_review()'s
            # Supabase write hitting a DB check-constraint that hadn't been
            # migrated yet) propagated straight out of this loop and killed
            # the entire run, silently skipping every remaining case in the
            # batch (they were never even attempted, not "processed with an
            # error" — just never reached). That's what made a single bad
            # case look like "the whole batch crashes under load": it had
            # nothing to do with how many cases were queued, only with
            # WHERE in the list the first unexpected exception happened to
            # land. Every OTHER failure mode in prepare_one_case() already
            # catches its own exceptions and opens a human-reviewable
            # requirement instead of raising (see the try/except around
            # stage_create_mdt above, for example) — this makes that the
            # rule for the whole loop, not just the paths someone thought
            # to wrap already.
            case_id = case.get("id")
            try:
                results.append(prepare_one_case(session, case, aliases, doc_cache, pending_review_by_patient, existing_attempt_counts))
            except Exception as exc:
                log.exception(f"case {case_id}: unexpected error — flagging and continuing with the rest of the batch")
                msg = f"خطأ غير متوقع أثناء تجهيز هذا الطلب — راجعه يدويًا. ({exc})"
                try:
                    common.open_requirement(case_id, None, msg)
                except Exception:
                    log.exception(f"case {case_id}: also failed to open a requirement for the above error")
                results.append({"case_id": case_id, "status": "unexpected_error", "message": str(exc)})
    finally:
        # Tears down the shared Chromium/Playwright process started lazily
        # by render_print_page_to_pdf() (see Unified_Decree_Submission_
        # Pipeline.py's shared-browser block) — always, even on an
        # unhandled error partway through the batch, so nothing is left
        # running after this script exits.
        shutdown_shared_browser()
    summary = {
        "total": len(results),
        "submitted": sum(1 for r in results if r["status"] == "submitted"),
        "pending_review": sum(1 for r in results if r["status"] == "pending_review"),
        "pending_review_linked": sum(1 for r in results if r["status"] == "pending_review_linked"),
        "requirement_opened": sum(1 for r in results if r["status"] == "requirement_opened"),
        "unexpected_error": sum(1 for r in results if r["status"] == "unexpected_error"),
        "debug_stopped": sum(1 for r in results if r["status"] == "debug_stopped"),
        "results": results,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"## Decree prepare run\n- Submitted: **{summary['submitted']}**\n"
                    f"- Pending review: **{summary['pending_review']}**\n"
                    f"- Linked to another case's pending review (same patient, no extra review needed): "
                    f"**{summary['pending_review_linked']}**\n"
                    f"- Needs attention: **{summary['requirement_opened']}**\n"
                    f"- Unexpected errors (flagged, batch continued): **{summary['unexpected_error']}**\n"
                    f"- Debug-stopped (render only): **{summary['debug_stopped']}**\n")

    # A run that STOPPED to hand a case to a human for document review is
    # not a failure — that's exactly what a "prepare" run is supposed to
    # do for a freshly-extracted document (see this file's module
    # docstring). "submitted" (finished straight through on a cache hit),
    # "pending_review" (a new review card opened) and "pending_review_linked"
    # (the same document, already covered by another case's review card)
    # are all correct, successful outcomes of THIS run — only mark the
    # whole run red when every single case in it ended in a genuine error
    # (requirement_opened / unexpected_error) with nothing else to show
    # for it.
    handled_without_error = (
        summary["submitted"] + summary["pending_review"] + summary["pending_review_linked"]
        + summary["debug_stopped"]
    )
    if summary["total"] > 0 and handled_without_error == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
