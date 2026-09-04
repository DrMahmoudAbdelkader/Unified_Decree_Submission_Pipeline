"""
supabase_storage.py
==========================================================================
Supabase Storage REST helper, for the things that actually fit there:
  - "decree-assets" bucket (private): signature images, stamp image, the
    medical report template PDF — small, static, rarely change.
  - "decree-pending-review" bucket (private): freshly-extracted patient
    documents awaiting human approval — small and TRANSIENT (deleted once
    approved and promoted to the permanent R2 archive, or once rejected).
    This is deliberately NOT where the 10 GB approved archive lives —
    that's r2_client.py. Keeping the pending set small and short-lived
    means Supabase Storage's free/cheap tier is never under real pressure.

Reads from environment:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   - same ones supabase_client.py uses

ONE-TIME SETUP (you do this once, manually, in the Supabase dashboard or CLI):
    1. Create a private bucket named "decree-assets".
       Upload: signatures/sig1.png, sig2.png, sig3.png, sig4.png, stamp.png
               medical_report_template.pdf
    2. Create a private bucket named "decree-pending-review" (starts empty —
       decree_submission_prepare.py populates it).
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("supabase_storage")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""

ASSETS_BUCKET = "decree-assets"
PENDING_REVIEW_BUCKET = "decree-pending-review"

_TIMEOUT = 60


def _headers():
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }


def download(bucket: str, object_path: str, local_path: str) -> bool:
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{object_path}"
    resp = requests.get(url, headers=_headers(), timeout=_TIMEOUT)
    if resp.status_code != 200:
        log.error(f"Storage download failed ({resp.status_code}) for {bucket}/{object_path}: {resp.text[:200]}")
        return False
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(resp.content)
    return True


def upload(bucket: str, object_path: str, local_path: str, content_type: str = "application/octet-stream") -> bool:
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{object_path}"
    with open(local_path, "rb") as f:
        resp = requests.post(
            url,
            headers={**_headers(), "Content-Type": content_type},
            data=f.read(),
            params={"upsert": "true"},
            timeout=_TIMEOUT,
        )
    if resp.status_code not in (200, 201):
        log.error(f"Storage upload failed ({resp.status_code}) for {bucket}/{object_path}: {resp.text[:200]}")
        return False
    return True


def delete(bucket: str, object_path: str) -> bool:
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{object_path}"
    resp = requests.delete(url, headers=_headers(), timeout=_TIMEOUT)
    return resp.status_code in (200, 204)


def fetch_signing_and_template_assets(local_dir: str) -> dict:
    """Downloads the fixed set of small assets needed by
    Unified_Decree_Submission_Pipeline.SIGNATURE_FILES and
    medical_report_overlay.MEDICAL_REPORT_TEMPLATE_PDF. Returns a dict of
    local paths keyed the same way SIGNATURE_FILES is, plus 'template'.
    Raises RuntimeError with a specific missing-file message rather than
    letting a downstream FileNotFoundError surface unexplained deep inside
    pipeline code — same "fail with an actionable message" pattern used
    everywhere else in this project."""
    os.makedirs(local_dir, exist_ok=True)
    paths = {}
    required = {
        "sig1": "signatures/sig1.png", "sig2": "signatures/sig2.png",
        "sig3": "signatures/sig3.png", "sig4": "signatures/sig4.png",
        "stamp": "signatures/stamp.png",
        "template": "medical_report_template.pdf",
    }
    missing = []
    for key, object_path in required.items():
        local_path = os.path.join(local_dir, os.path.basename(object_path))
        if download(ASSETS_BUCKET, object_path, local_path):
            paths[key] = local_path
        else:
            missing.append(object_path)
    if missing:
        raise RuntimeError(
            f"Missing required asset(s) in Supabase Storage bucket '{ASSETS_BUCKET}': {missing}. "
            f"Upload them once via the Supabase dashboard before running this workflow."
        )
    return paths
