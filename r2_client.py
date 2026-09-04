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

import logging
import os
from typing import Optional

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
