"""
r2_client.py
==========================================================================
Minimal Cloudflare R2 helper. R2 is S3-API-compatible, so this is just
boto3's S3 client pointed at R2's endpoint — no custom HTTP/signing code
needed. Used for the PERMANENT, approved patient-document cache (the
10 GB corpus). Never used for the transient pending-review holding area
— that's Supabase Storage (see supabase_storage.py), a separate, smaller
bucket, since a rejected/re-extracted review item shouldn't need to touch
the big archive at all.

Reads from environment (set as GitHub Actions secrets):
    R2_ACCOUNT_ID       - the account-id portion of your R2 endpoint
                           (https://<account-id>.r2.cloudflarestorage.com)
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    R2_BUCKET_NAME      - e.g. "decree-patient-docs"

Usage:
    import r2_client as r2
    path = r2.download_if_exists("27806040100861", "/tmp/patient_docs")
    # -> "/tmp/patient_docs/27806040100861.pdf" or None

    r2.upload("27806040100861", "/tmp/patient_docs/27806040100861.pdf")
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError
from botocore.config import Config

log = logging.getLogger("r2_client")

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
# FIXED BUG: this was hardcoded to "cleaned-pdfs-files", silently ignoring
# the R2_BUCKET_NAME secret set in the GitHub Actions workflow. Every R2
# call was therefore targeting whatever this literal said, regardless of
# what bucket was actually configured — a mismatch here fails HEAD/GET/PUT
# silently (exists() just returns False, upload_pending() just returns
# False) rather than raising, so nothing ever looked broken until you
# checked R2 itself. Reads from the environment again, with this bucket
# name kept only as the fallback default if the secret isn't set.
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "cleaned-pdfs-files")

_client = None


def _configured() -> bool:
    return bool(R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY)


def _get_client():
    global _client
    if _client is None:
        if not _configured():
            raise RuntimeError("R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY not set.")
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _client


def _key_for(national_id: str) -> str:
    clean_id = "".join(c for c in national_id if c.isalnum())
    return f"{clean_id}.pdf"


def exists(national_id: str) -> bool:
    if not _configured():
        return False
    client = _get_client()
    try:
        client.head_object(Bucket=R2_BUCKET_NAME, Key=_key_for(national_id))
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return False
        log.warning(f"R2 head_object error for {national_id}: {e}")
        return False


def download_if_exists(national_id: str, output_dir: str) -> Optional[str]:
    """Returns the local path if this patient's document is already in the
    permanent R2 cache (meaning it was reviewed and approved once before),
    or None if not — a None return means 'proceed to live extraction +
    review', not an error."""
    if not _configured():
        return None
    if not exists(national_id):
        return None
    client = _get_client()
    os.makedirs(output_dir, exist_ok=True)
    local_path = os.path.join(output_dir, _key_for(national_id))
    try:
        client.download_file(R2_BUCKET_NAME, _key_for(national_id), local_path)
        log.info(f"  [R2 cache hit] {national_id} -> {local_path}")
        return local_path
    except ClientError as e:
        log.warning(f"R2 download failed for {national_id}: {e}")
        return None


def _pending_key_for(national_id: str) -> str:
    """Freshly-extracted-but-not-yet-labeled candidates live under pending/
    in the SAME bucket as the permanent, labeled archive — one storage
    system, not two. The labeling step is expected to read pending/<id>.pdf,
    let a human clean it, then write the result to <id>.pdf (root) and
    delete pending/<id>.pdf once done."""
    return f"pending/{_key_for(national_id)}"


def upload_pending(national_id: str, local_path: str) -> bool:
    """Stages a freshly-extracted, not-yet-reviewed document for a human
    to label. Never confused with the permanent cache (upload()) because
    it lives under a different key prefix in the same bucket."""
    if not _configured():
        log.warning("R2 not configured — cannot stage document for review.")
        return False
    client = _get_client()
    try:
        client.upload_file(local_path, R2_BUCKET_NAME, _pending_key_for(national_id))
        return True
    except ClientError as e:
        log.error(f"R2 pending upload failed for {national_id}: {e}")
        return False


def pending_review_url(national_id: str, expires_in: int = 60 * 60 * 24 * 7) -> Optional[str]:
    """A temporary signed link a human can open directly to view the
    pending (unlabeled) document while deciding how to clean it."""
    if not _configured():
        return None
    client = _get_client()
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET_NAME, "Key": _pending_key_for(national_id)},
            ExpiresIn=expires_in,
        )
    except ClientError as e:
        log.warning(f"Could not create pending review URL for {national_id}: {e}")
        return None


def list_permanent_doc_keys() -> list:
    """Lists every key in the PERMANENT cache (root-level <id>.pdf files),
    explicitly excluding the pending/ prefix — used by one-off maintenance
    scripts (e.g. recompress_r2_backlog.py) that need to walk the whole
    approved archive rather than look up one national_id at a time.
    Paginates since the bucket can hold well over 1000 objects (the boto3
    default page size)."""
    if not _configured():
        return []
    client = _get_client()
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=R2_BUCKET_NAME):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.startswith("pending/") or key.startswith("delta/"):
                    continue
                if key.endswith(".pdf"):
                    keys.append(key)
    except ClientError as e:
        log.error(f"R2 list_objects_v2 failed: {e}")
    return keys


# =====================================================================
# LABELED-DELTA STAGING — used ONLY when an already-labeled (R2 cache-hit)
# patient has NEW DMS/CMIS archive papers and the operator is asked to
# label just those new pages, not the whole file again.
#
#   pending/<id>.pdf   the UNLABELED new pages only (what the labeler opens;
#                      same key a full review uses — pipeline_state.review_mode
#                      tells the two apart)
#   delta/<id>.pdf     the operator's LABELED new pages (the document-review
#                      edge function hands out a PUT URL for this key instead
#                      of the permanent key when review_mode == "delta")
#   <id>.pdf           the permanent, already-labeled file. NEVER overwritten
#                      by the delta upload; decree_submission_finalize.py
#                      appends delta/<id>.pdf onto it, uploads the result,
#                      and only then deletes the delta object.
# =====================================================================

def _delta_key_for(national_id: str) -> str:
    return f"delta/{_key_for(national_id)}"


def download_labeled_delta(national_id: str, output_dir: str) -> Optional[str]:
    """Local path of the operator's labeled new pages, or None if the
    operator hasn't uploaded them (or they were already merged + deleted)."""
    if not _configured():
        return None
    client = _get_client()
    os.makedirs(output_dir, exist_ok=True)
    local_path = os.path.join(output_dir, f"{_key_for(national_id)[:-4]}_labeled_delta.pdf")
    try:
        client.download_file(R2_BUCKET_NAME, _delta_key_for(national_id), local_path)
        return local_path
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        log.warning(f"R2 labeled-delta download failed for {national_id}: {e}")
        return None


def delete_labeled_delta(national_id: str) -> None:
    """Best-effort cleanup once the delta is safely merged into the
    permanent file. Never raises."""
    if not _configured():
        return
    try:
        _get_client().delete_object(Bucket=R2_BUCKET_NAME, Key=_delta_key_for(national_id))
    except ClientError as e:
        log.warning(f"R2 labeled-delta delete failed for {national_id}: {e}")


def upload(national_id: str, local_path: str) -> bool:
    """Promotes a now-approved document into the permanent cache, so the
    NEXT decree request for this same patient is a cache hit and skips
    human review entirely — same benefit the local PATIENT_DOCS_ROOT
    folder gave before."""
    if not _configured():
        log.warning("R2 not configured — skipping cache promotion (not fatal, just no future cache hit).")
        return False
    client = _get_client()
    try:
        client.upload_file(local_path, R2_BUCKET_NAME, _key_for(national_id))
        log.info(f"  [R2 cache promoted] {national_id} <- {local_path}")
        return True
    except ClientError as e:
        log.error(f"R2 upload failed for {national_id}: {e}")
        return False


# =====================================================================
# DMS/CMIS ARCHIVE MERGE LOG — one small JSON object per patient,
# recording exactly which archive item IDs (the verbatim
# "dd/mm/yyyy HH:MM:SS AM/PM" data-id strings — see
# patient_pdf_dms_archive_fallback.py) are already baked into that
# patient's current document.
#
# WHY THIS EXISTS: the DMS archive merge used to be driven purely by a
# rolling "last N days" window computed relative to *today*, re-run on
# every submission. That meant the SAME archive items a patient's file
# was already merged and reviewed with yesterday fell right back inside
# today's window and got downloaded and appended AGAIN — duplicating
# pages on every subsequent run for as long as they stayed inside the
# window, which regularly re-triggered a human review for a patient who
# had already been reviewed and submitted. This log is the fix: prepare.py
# now takes the archive items dated inside the window (ARCHIVE_WINDOW_DAYS,
# default 7) and subtracts merged_ids here (see patient_pdf_dms_archive_
# fallback.collect_recent_archive_delta). What is left is the genuinely
# new papers; if nothing is left the patient goes straight to submission.
#
# Lives in the SAME bucket as the permanent document cache, under its
# own key prefix — never confused with either the permanent <id>.pdf
# key or the pending/<id>.pdf staging key.
# =====================================================================

def _merge_log_key_for(national_id: str) -> str:
    clean_id = "".join(c for c in national_id if c.isalnum())
    return f"merge_log/{clean_id}.json"


def get_merged_archive_ids(national_id: str) -> List[str]:
    """Returns the list of DMS/CMIS archive item IDs already merged into
    this patient's document on some earlier run, or [] if none recorded
    yet (a genuinely first-time patient, or R2 not configured) — an
    empty list is not an error, it just means "nothing to diff against,
    treat every current archive item as new"."""
    if not _configured():
        return []
    client = _get_client()
    try:
        resp = client.get_object(Bucket=R2_BUCKET_NAME, Key=_merge_log_key_for(national_id))
        data = json.loads(resp["Body"].read().decode("utf-8"))
        return list(data.get("merged_ids") or [])
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return []
        log.warning(f"R2 merge-log read failed for {national_id}: {e}")
        return []
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as e:
        log.warning(f"R2 merge-log for {national_id} was unreadable ({e}) — treating as empty.")
        return []


def save_merged_archive_ids(national_id: str, merged_ids: List[str],
                             window_start: Optional[str] = None,
                             window_end: Optional[str] = None) -> bool:
    """Persists the FULL current set of archive item IDs now baked into
    this patient's document. Call this every time a DMS/CMIS merge
    actually appends something — not just when the file also gets
    promoted to the permanent cache — since the log's job is to record
    what's been merged, not to gate on review status.

    window_start / window_end (ISO dates, optional) record WHICH PERIOD
    that merge covered, purely so a human opening merge_log/<id>.json can
    see it ("last collected 2026-09-24 -> 2026-10-01"). The diff itself
    only ever uses merged_ids."""
    if not _configured():
        return False
    client = _get_client()
    try:
        payload = {"merged_ids": list(merged_ids),
                   "updated_at": datetime.now(timezone.utc).isoformat()}
        if window_start or window_end:
            payload["last_window"] = {"from": window_start, "to": window_end}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        client.put_object(Bucket=R2_BUCKET_NAME, Key=_merge_log_key_for(national_id),
                           Body=body, ContentType="application/json")
        return True
    except ClientError as e:
        log.error(f"R2 merge-log write failed for {national_id}: {e}")
        return False
