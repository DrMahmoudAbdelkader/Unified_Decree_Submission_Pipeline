#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recompress_r2_backlog.py
==========================================================================
ONE-OFF maintenance script for the backlog problem: patient PDFs that were
already promoted into R2's PERMANENT cache (root-level <national_id>.pdf
keys, written by the labeling step / r2_client.upload()) before the ~1MB
SMC upload-cap compression step existed, and are therefore too big for a
future finalize run to upload as-is once merged with the MDT form/report.

This does NOT touch decree_request_cases/attempts, Supabase, or the SMC
site at all — it only walks R2's permanent cache, and for every file over
the target size: downloads it, compresses it with the SAME logic
decree_common.py now runs automatically on every future merged PDF
(compress_pdf_under_limit.compress_pdf_to_size), and re-uploads it to the
SAME key, overwriting the oversized original in place.

Run this ONCE to clear today's backlog. You do not need to run it again
routinely — decree_common.run_finalize_stages now compresses the merged
PDF on every future submission automatically, so new oversized files
should not accumulate. (If you ever bulk-import a batch of pre-cleaned
documents straight into R2 without going through the module's normal
labeling step, re-run this afterwards.)

RUN LOCALLY:
    export R2_ACCOUNT_ID=...  R2_ACCESS_KEY_ID=...  R2_SECRET_ACCESS_KEY=...
    export R2_BUCKET_NAME=...
    python recompress_r2_backlog.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile

import r2_client
from compress_pdf_under_limit import compress_pdf_to_size, DEFAULT_TARGET_BYTES

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("recompress_r2_backlog")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Only report which keys are over target; don't download/compress/upload anything.")
    args = parser.parse_args()

    if not r2_client._configured():
        raise SystemExit("R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY not set.")

    keys = r2_client.list_permanent_doc_keys()
    log.info(f"Found {len(keys)} document(s) in the permanent R2 cache.")

    client = r2_client._get_client()
    bucket = r2_client.R2_BUCKET_NAME

    over_target, compressed, failed = 0, 0, 0

    with tempfile.TemporaryDirectory(prefix="r2_backlog_") as tmp_dir:
        for key in keys:
            head = client.head_object(Bucket=bucket, Key=key)
            size = head["ContentLength"]
            if size <= DEFAULT_TARGET_BYTES:
                continue

            over_target += 1
            log.info(f"[{key}] {size / 1e6:.2f} MB — over the {DEFAULT_TARGET_BYTES / 1e6:.2f} MB target.")
            if args.dry_run:
                continue

            local_src = os.path.join(tmp_dir, key.replace("/", "_"))
            client.download_file(bucket, key, local_src)

            local_dst = local_src  # compress in place, same convention as the pipeline
            if compress_pdf_to_size(local_src, local_dst, DEFAULT_TARGET_BYTES):
                new_size = os.path.getsize(local_dst)
                client.upload_file(local_dst, bucket, key)
                compressed += 1
                log.info(f"  ✅ [{key}] re-uploaded: {size / 1e6:.2f} MB -> {new_size / 1e6:.2f} MB")
            else:
                failed += 1
                log.error(f"  ⚠️  [{key}] could not be compressed under target — left unchanged in R2.")

            os.remove(local_src)

    log.info(f"\nDone. {over_target} over target, {compressed} compressed+re-uploaded, {failed} failed.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
