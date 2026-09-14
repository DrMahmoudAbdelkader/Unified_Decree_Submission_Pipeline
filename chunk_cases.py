#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chunk_cases.py
==========================================================================
Planner step for submit-decree-prepare.yml's fan-out.

Resolves EXACTLY the same case list decree_submission_prepare.main() would
resolve (case_status=READY_TO_SUBMIT, optionally narrowed by CASE_IDS,
ordered by created_at.asc — identical filter, identical order), then splits
it into batches of CHUNK_SIZE and emits them as a GitHub Actions matrix.

Deliberately imports ONLY supabase_client (requests-only) — never
decree_common / Unified_Decree_Submission_Pipeline — so the planner job
doesn't have to install PyMuPDF, reportlab, Playwright, etc. just to run a
single SELECT.

TWO INVARIANTS THIS FILE EXISTS TO PROTECT
------------------------------------------
1. A batch job NEVER runs with a blank CASE_IDS. If it did, prepare.py
   would fall back to "every READY_TO_SUBMIT case" and every parallel
   batch would process the whole queue — duplicate MDTs on SMC.

2. All cases belonging to the SAME PATIENT always land in the SAME batch.
   prepare.py's doc_cache and pending_review_by_patient dicts are scoped
   to one process (see resolve_patient_document() /
   link_sibling_to_pending_review()). Split one patient's cases across two
   runs and you get: the slow SMC/CMIS document search run twice, TWO
   r2 pending/<id>.pdf uploads to the same key, and TWO independent human
   review cards for one physical document — i.e. exactly the bug the
   sibling-linking logic was written to kill. Grouping by patient keeps
   that logic correct without touching it.

   Consequence, on purpose: a patient with more than CHUNK_SIZE cases
   produces one over-sized batch. Correctness wins over an even split.

Reads from environment:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   (same secrets as everything else)
    CASE_IDS      - the workflow input, comma-separated or blank (= bulk)
    CHUNK_SIZE    - default 10
    DEBUG_RENDER_MDT_AND_STOP - if truthy, no chunking at all: the given
                    CASE_IDS pass through as a single batch, so
                    prepare.py's own "exactly one case id" guard still
                    fires the way it does today.

Writes to GITHUB_OUTPUT:
    matrix - JSON list of {"name": "batch-1", "case_ids": "1,2,3"}
    count  - number of batches (0 = nothing to do; the caller skips the
             batch job entirely, since an empty matrix is an error in
             Actions)
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import supabase_client as sb

CASES_TABLE = "decree_request_cases"


def parse_case_ids(raw: str):
    return [int(p.strip()) for p in (raw or "").split(",") if p.strip().isdigit()]


def debug_mode_on() -> bool:
    return os.environ.get("DEBUG_RENDER_MDT_AND_STOP", "").strip() in ("1", "true", "True")


def build_chunks(cases, chunk_size: int):
    """cases: list of {"id", "patient_id"} already in created_at.asc order.

    Groups consecutive cases by patient, then packs whole patient-groups
    into batches without ever splitting a group across two batches."""
    by_patient = {}
    order = []
    for c in cases:
        # Cases with no patient_id can't collide with anyone — give each
        # its own group key so they pack freely.
        key = c.get("patient_id") or f"__case_{c['id']}"
        if key not in by_patient:
            by_patient[key] = []
            order.append(key)
        by_patient[key].append(c["id"])

    chunks = []
    current = []
    for key in order:
        group = by_patient[key]
        if current and len(current) + len(group) > chunk_size:
            chunks.append(current)
            current = []
        current.extend(group)
        if len(current) >= chunk_size:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def emit(matrix, count):
    payload = json.dumps(matrix, ensure_ascii=False)
    print(f"count={count}")
    print(f"matrix={payload}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"count={count}\n")
            f.write(f"matrix={payload}\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## Batch plan\n- Batches: **{count}**\n")
            for item in matrix:
                ids = item["case_ids"].split(",")
                f.write(f"  - `{item['name']}`: {len(ids)} case(s) — {item['case_ids']}\n")


def main():
    requested = parse_case_ids(os.environ.get("CASE_IDS", ""))
    chunk_size = max(1, int(os.environ.get("CHUNK_SIZE", "10")))

    if debug_mode_on():
        # One batch, untouched — prepare.py rejects anything but exactly
        # one id itself, and that guard must stay the thing that decides.
        if not requested:
            emit([], 0)
            return
        emit([{"name": "debug", "case_ids": ",".join(str(i) for i in requested)}], 1)
        return

    filters = {"case_status": "eq.READY_TO_SUBMIT"}
    if requested:
        filters["id"] = f"in.({','.join(str(i) for i in requested)})"

    # Same select/order prepare.main() uses, so the oldest case for a
    # patient stays the "primary" that owns the review card.
    cases = sb.select(CASES_TABLE, select="id,patient_id", filters=filters,
                      order="created_at.asc")

    chunks = build_chunks(cases, chunk_size)
    matrix = [
        {"name": f"batch-{i + 1}", "case_ids": ",".join(str(cid) for cid in chunk)}
        for i, chunk in enumerate(chunks)
    ]
    emit(matrix, len(matrix))


if __name__ == "__main__":
    main()
