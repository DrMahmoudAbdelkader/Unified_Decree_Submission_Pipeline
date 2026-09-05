"""
supabase_storage.py
==========================================================================
Supabase Storage REST helper — now scoped to ONLY the small, static,
rarely-changing assets: signature images and the medical report template
PDF, in a private "decree-assets" bucket. Patient documents (both the
permanent archive and the pending-review staging area) live entirely in
Cloudflare R2 now (see r2_client.py) — a single storage system for
everything patient-document-related, under a `pending/` key prefix for
not-yet-labeled files and no prefix for the permanent, labeled archive.

Reads from environment:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   - same ones supabase_client.py uses

ONE-TIME SETUP (you do this once, manually, in the Supabase dashboard or CLI):
    Create a private bucket named "decree-assets" and upload:
        signatures/sig1.png, sig2.png, sig3.png, sig4.png, stamp.png
        medical_report_template.pdf
        fonts/mdt_form_font.ttf, fonts/mdt_form_font_bold.ttf
            (your own local C:\Windows\Fonts\tahoma.ttf / tahomabd.ttf,
            or whatever the SMC print page's CSS actually specifies —
            confirm via DevTools' computed font-family on that page.
            NOT redistributed by this repo: GitHub Actions secrets cap
            out at 64 KB, far smaller than a real font file, and a public
            repo isn't an appropriate place for it anyway - Supabase
            Storage's private bucket has neither problem.)
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("supabase_storage")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""

ASSETS_BUCKET = "decree-assets"

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


def fetch_mdt_form_font(local_dir: str) -> dict:
    """Downloads the font the SMC print page's own CSS actually asks for
    (almost certainly Tahoma - see medical_report_overlay.py's note),
    installed into a private Storage bucket rather than committed to this
    public repo or stuffed into a GitHub Actions secret (both of which
    either republish a font of unclear redistribution rights, or simply
    don't fit - Actions secrets cap out at 64 KB, well under a real font
    file's size).

    Deliberately non-fatal if these objects aren't uploaded yet: the MDT
    form still renders without this (falling back to whatever generic
    sans-serif fontconfig picks on the runner), it just won't match the
    local layout as closely. Returns {} with a logged warning in that
    case rather than raising, so a first deploy before you've uploaded
    the font doesn't hard-fail every run.
    """
    os.makedirs(local_dir, exist_ok=True)
    wanted = {
        "regular": "fonts/mdt_form_font.ttf",
        "bold": "fonts/mdt_form_font_bold.ttf",
    }
    paths = {}
    for key, object_path in wanted.items():
        local_path = os.path.join(local_dir, os.path.basename(object_path))
        if download(ASSETS_BUCKET, object_path, local_path):
            paths[key] = local_path
    if "regular" not in paths:
        log.warning(
            f"No MDT-form font found at '{ASSETS_BUCKET}/fonts/mdt_form_font.ttf' - "
            f"the rendered MDT form will use a fallback font and may not match the "
            f"local layout. Upload your local Tahoma (or whatever the print page's "
            f"CSS specifies) there once to fix this."
        )
    return paths
