#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compress_pdf_under_limit.py
==========================================================================
Shrinks a PDF under a target byte size (default: just under 2 MB, to fit
the SMC portal's upload cap), for both:

  1. One-off / bulk cleanup of PDFs you already have (already on
     Cloudflare, already merged, whatever) that are over the limit.
  2. Dropping into the main pipeline as a step right before upload, so a
     merged patient PDF that comes out over 1 MB gets shrunk automatically
     instead of failing the request.

STRATEGY (tries progressively more aggressive settings, stops as soon as
one gets under the target - never compresses harder than it has to):

  A. Ghostscript pass (preferred - much better quality/size ratio, keeps
     any real vector text/signature overlays crisp). Tries, in order:
     /prepress -> /printer -> /ebook -> /screen, then /screen with
     manually forced lower image DPI/quality if /screen alone isn't
     enough. Requires `gs` on PATH (Ubuntu/Debian: `apt-get install
     ghostscript`; most servers already have it or can).

  B. PyMuPDF fallback (pure Python, no external binary) if Ghostscript
     isn't available or somehow didn't get under target: re-renders each
     page to a JPEG at decreasing DPI/quality and rebuilds the PDF from
     those images. This WILL make embedded text non-selectable (turns it
     into a picture of the page) - fine for scanned/signed medical PDFs
     that are already just images, but mention this trade-off if the
     source PDF has real selectable text you care about.

Both paths are all-or-nothing per attempt: each attempt writes to a temp
file, only the first one landing under the target byte size is kept.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional

log = logging.getLogger("compress_pdf_under_limit")

DEFAULT_TARGET_BYTES = int(1.95 * 1024 * 1024)  # a hair under 2 MB for safety margin


def _gs_available() -> bool:
    return shutil.which("gs") is not None


def _run_ghostscript(src_path: str, dst_path: str, pdf_setting: str,
                      image_dpi: Optional[int] = None) -> bool:
    """Runs one Ghostscript compression attempt. pdf_setting is one of
    Ghostscript's built-in /PDFSETTINGS presets; image_dpi, if given,
    additionally forces color/gray/mono image downsampling to that
    resolution on top of the preset (used for the final, most aggressive
    attempts once the presets alone aren't enough)."""
    cmd = [
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS=/{pdf_setting}",
        "-dNOPAUSE", "-dQUIET", "-dBATCH",
        "-dDetectDuplicateImages=true",
    ]
    if image_dpi:
        cmd += [
            "-dDownsampleColorImages=true", f"-dColorImageResolution={image_dpi}",
            "-dDownsampleGrayImages=true", f"-dGrayImageResolution={image_dpi}",
            "-dDownsampleMonoImages=true", f"-dMonoImageResolution={image_dpi}",
            "-dColorImageDownsampleType=/Bicubic",
            "-dGrayImageDownsampleType=/Bicubic",
        ]
    cmd += [f"-sOutputFile={dst_path}", src_path]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=180)
        return result.returncode == 0 and os.path.exists(dst_path) and os.path.getsize(dst_path) > 0
    except Exception as e:
        log.warning(f"  Ghostscript attempt ({pdf_setting}, dpi={image_dpi}) failed: {e}")
        return False


def _compress_with_ghostscript(src_path: str, target_bytes: int, tmp_dir: str) -> Optional[str]:
    # (preset, forced_dpi) attempts, roughly increasing aggressiveness.
    attempts = [
        ("prepress", None), ("printer", None), ("ebook", None), ("screen", None),
        ("screen", 150), ("screen", 120), ("screen", 100), ("screen", 85), ("screen", 70),
    ]
    for i, (preset, dpi) in enumerate(attempts):
        out_path = os.path.join(tmp_dir, f"gs_attempt_{i}.pdf")
        if not _run_ghostscript(src_path, out_path, preset, dpi):
            continue
        size = os.path.getsize(out_path)
        log.info(f"  [gs {preset}{f'/{dpi}dpi' if dpi else ''}] -> {size / 1e6:.2f} MB")
        if size <= target_bytes:
            return out_path
    return None


def _compress_with_pymupdf(src_path: str, target_bytes: int, tmp_dir: str) -> Optional[str]:
    import fitz  # PyMuPDF

    for i, (dpi, quality) in enumerate([
        (150, 70), (130, 60), (110, 55), (100, 45), (90, 40), (80, 35), (70, 30),
    ]):
        src = fitz.open(src_path)
        out = fitz.open()
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        for page in src:
            pix = page.get_pixmap(matrix=mat, alpha=False)
            jpeg_bytes = pix.tobytes("jpeg", jpg_quality=quality)
            img_page = out.new_page(width=page.rect.width, height=page.rect.height)
            img_page.insert_image(img_page.rect, stream=jpeg_bytes)
        out_path = os.path.join(tmp_dir, f"pymupdf_attempt_{i}.pdf")
        out.save(out_path, deflate=True, garbage=4)
        out.close()
        src.close()

        size = os.path.getsize(out_path)
        log.info(f"  [pymupdf {dpi}dpi/q{quality}] -> {size / 1e6:.2f} MB")
        if size <= target_bytes:
            return out_path
    return None


def compress_pdf_to_size(src_path: str, dst_path: str,
                          target_bytes: int = DEFAULT_TARGET_BYTES) -> bool:
    """
    Compresses src_path so the result is <= target_bytes and writes it to
    dst_path (dst_path may be the same as src_path - a temp file is used
    internally either way, so the source is never touched/corrupted mid-
    attempt). If the source is already under target, it's just copied
    across unchanged (no pointless recompression).

    Returns True if dst_path now exists and is <= target_bytes (i.e. the
    upload can proceed), False if nothing got small enough (dst_path is
    left untouched in that case - caller should treat this the same as
    any other pre-upload failure and NOT attempt the upload).
    """
    original_size = os.path.getsize(src_path)
    if original_size <= target_bytes:
        if os.path.abspath(src_path) != os.path.abspath(dst_path):
            shutil.copyfile(src_path, dst_path)
        log.info(f"  Already under target ({original_size / 1e6:.2f} MB) - no compression needed.")
        return True

    log.info(f"  {src_path}: {original_size / 1e6:.2f} MB, target <= {target_bytes / 1e6:.2f} MB")

    with tempfile.TemporaryDirectory(prefix="pdf_compress_") as tmp_dir:
        best_path = None

        if _gs_available():
            best_path = _compress_with_ghostscript(src_path, target_bytes, tmp_dir)
        else:
            log.info("  Ghostscript not found on PATH - skipping straight to the PyMuPDF fallback.")

        if best_path is None:
            log.info("  Falling back to PyMuPDF page-rasterization "
                     "(this makes the PDF's text non-selectable - it becomes an image "
                     "of each page, same as a scan).")
            best_path = _compress_with_pymupdf(src_path, target_bytes, tmp_dir)

        if best_path is None:
            log.error(f"  Could not get under {target_bytes / 1e6:.2f} MB with any attempt.")
            return False

        shutil.copyfile(best_path, dst_path)
        final_size = os.path.getsize(dst_path)
        log.info(f"  ✅ Compressed {original_size / 1e6:.2f} MB -> {final_size / 1e6:.2f} MB")
        return True


def _bulk_cli():
    """Command-line bulk mode: compress every .pdf in a folder (in place,
    or into an output folder) that's over the target size. Use this to
    clean up a backlog of already-uploaded-to-Cloudflare PDFs after
    pulling them down locally - point --input-dir at wherever you've
    synced/downloaded them to.

    Usage:
        python3 compress_pdf_under_limit.py --input-dir ./patient_pdfs \\
            [--output-dir ./patient_pdfs_compressed] [--target-mb 1.95]
    """
    import argparse

    parser = argparse.ArgumentParser(description="Bulk-compress PDFs under a target size.")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", default=None,
                         help="If omitted, compresses in place (overwrites originals).")
    parser.add_argument("--target-mb", type=float, default=1.95)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    target_bytes = int(args.target_mb * 1024 * 1024)
    out_dir = args.output_dir or args.input_dir
    os.makedirs(out_dir, exist_ok=True)

    pdfs = [f for f in os.listdir(args.input_dir) if f.lower().endswith(".pdf")]
    print(f"Found {len(pdfs)} PDF(s) in {args.input_dir}")

    ok, failed, skipped = 0, 0, 0
    for fname in pdfs:
        src = os.path.join(args.input_dir, fname)
        dst = os.path.join(out_dir, fname)
        size = os.path.getsize(src)
        if size <= target_bytes:
            if src != dst:
                shutil.copyfile(src, dst)
            skipped += 1
            continue
        print(f"\n{fname} ({size / 1e6:.2f} MB) …")
        if compress_pdf_to_size(src, dst, target_bytes):
            ok += 1
        else:
            failed += 1
            print(f"  ⚠️  {fname} could not be brought under {args.target_mb} MB.")

    print(f"\nDone. {ok} compressed, {skipped} already under target, {failed} still over target.")


if __name__ == "__main__":
    _bulk_cli()
