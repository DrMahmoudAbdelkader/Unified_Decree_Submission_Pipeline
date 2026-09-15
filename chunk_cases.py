#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chunk_cases.py
==========================================================================
Planner step for submit-decree-prepare.yml's PARALLEL fan-out.

Resolves exactly the same case list decree_submission_prepare.main() would
resolve (case_status=READY_TO_SUBMIT, optionally narrowed by CASE_IDS,
ordered by created_at.asc — identical filter, identical order), splits it
into batches of CHUNK_SIZE, and emits them as a GitHub Actions matrix that
runs ALL AT ONCE. 100 cases at chunk size 10 = 10 runners working
simultaneously, not 10 runners taking turns.

Imports supabase_client ONLY (requests is its whole dependency tree) —
never decree_common / Unified_Decree_Submission_Pipeline — so the planner
job doesn't install PyMuPDF/reportlab/boto3/Playwright just to run one
SELECT. It finishes in ~20s.

THREE INVARIANTS THIS FILE EXISTS TO PROTECT
--------------------------------------------
1. A batch job NEVER runs with a blank CASE_IDS. If it did, prepare.py
   would fall back to "every READY_TO_SUBMIT case" and all 10 parallel
   batches would each process the entire queue — 10 duplicate MDTs per
   patient on SMC. This is the single most dangerous failure mode of
   parallelising this workflow.

2. All cases belonging to the SAME PATIENT always land in the SAME batch.
   prepare.py's doc_cache and pending_review_by_patient dicts are scoped
   to one process (see resolve_patient_document() and
   link_sibling_to_pending_review()). Split one patient across two
   SIMULTANEOUS runs and you get: the slow SMC/CMIS document search run
   twice concurrently, two racing uploads to the same
   r2 pending/<id>.pdf key, and two independent human review cards for
   one physical document — exactly the bug the sibling-linking logic
   exists to prevent, except now it's a race instead of a sequence.

   Consequence, on purpose: a patient with more than CHUNK_SIZE cases
   produces one over-sized batch. Correctness beats an even split.

3. Every batch gets a SLOT NUMBER (1..N). The workflow uses it to hand
   each parallel runner a different SMC login where you have one
   available (SMC_USERNAME_<slot>), falling back to the primary account.
   See the workflow's comments — your own decree_status_and_letters_sync.
   _get_smc_credentials() already documents why a concurrent run should
   not share a login session.

Reads from environment:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   (same secrets as everything else)
    CASE_IDS      - workflow input, comma-separated or blank (= bulk)
    CHUNK_SIZE    - cases per batch, default 10
    MAX_BATCHES   - hard ceiling on how many runners are allowed to start
                    at once, default 10. If the queue needs more batches
                    than this, CHUNK_SIZE is grown until it fits, rather
                    than silently spawning 40 concurrent logins against
                    the SMC portal. This is the throttle — raise it only
                    as far as your SMC account situation actually allows.
    SMC_ACCOUNT_COUNT - how many distinct SMC logins you have configured
                    as secrets (SMC_USERNAME / SMC_USERNAME_2 / ...).
                    Default 1. Slots are assigned round-robin over this
                    many accounts; it does NOT limit how many batches
                    run, it only decides which login each one uses.

Writes to GITHUB_OUTPUT:
    matrix - JSON list of {"name","case_ids","slot"}
    count  - number of batches (0 = nothing to do; the caller skips the
             batch job, since an empty matrix is a hard error in Actions)
"""

from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import supabase_client as sb

CASES_TABLE = "decree_request_cases"


def parse_case_ids(raw: str):
    return [int(p.strip()) for p in (raw or "").split(",") if p.strip().isdigit()]


def debug_mode_on() -> bool:
    return os.environ.get("DEBUG_RENDER_MDT_AND_STOP", "").strip() in ("1", "true", "True")


def group_by_patient(cases):
    """Returns a list of id-lists, one per patient, preserving the
    created_at.asc order of first appearance so the oldest case for a
    patient stays the 'primary' that owns the review card."""
    by_patient = {}
    order = []
    for c in cases:
        # A case with no patient_id can't collide with anyone — give it its
        # own group so it packs freely.
        key = c.get("patient_id") or f"__case_{c['id']}"
        if key not in by_patient:
            by_patient[key] = []
            order.append(key)
        by_patient[key].append(c["id"])
    return [by_patient[k] for k in order]


def build_chunks(cases, chunk_size: int, max_batches: int):
    """Decides how many batches to run, then spreads the patient-groups
    across them as EVENLY as possible.

    Even sizes matter here in a way they would not for sequential batches:
    with every batch running at once, the run finishes when the SLOWEST
    batch finishes. A naive left-to-right fill produces a tail batch with
    one or two cases in it — a whole runner spun up, and 2 minutes of
    setup paid, to do 30 seconds of work, while another runner carries a
    full load. Assigning each group to the currently-least-loaded batch
    keeps every runner doing roughly the same amount of work.

    Groups are assigned largest-first (classic LPT scheduling) because a
    big patient-group arriving last can otherwise only be dumped on top of
    an already-full batch.

    Batch COUNT is ceil(total / chunk_size), capped at max_batches — so
    chunk_size behaves as 'about this many per runner' and max_batches as
    the hard ceiling on concurrent SMC logins."""
    groups = group_by_patient(cases)
    if not groups:
        return []

    total = sum(len(g) for g in groups)
    n_batches = min(max_batches, max(1, math.ceil(total / max(1, chunk_size))))
    # Never spin up more runners than there are patient-groups to put in
    # them; an empty matrix entry would start a runner with a blank
    # CASE_IDS, which is the one thing that must never happen.
    n_batches = min(n_batches, len(groups))

    batches = [[] for _ in range(n_batches)]
    # Largest group first, ties broken by the group's first (oldest) case id
    # so the plan is deterministic and reproducible for the same queue.
    for group in sorted(groups, key=lambda g: (-len(g), g[0])):
        target = min(batches, key=len)
        target.extend(group)

    # Keep each batch's ids in ascending id order for a readable plan; the
    # oldest-case-is-primary rule is unaffected, since prepare.py re-selects
    # with order=created_at.asc itself and a patient's cases are all here.
    return [sorted(b) for b in batches if b]


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
            f.write(f"## Batch plan — {count} runner(s) starting in parallel\n")
            for item in matrix:
                n = len(item["case_ids"].split(","))
                f.write(f"- `{item['name']}` (SMC account slot {item['slot']}): "
                        f"{n} case(s) — {item['case_ids']}\n")


def main():
    requested = parse_case_ids(os.environ.get("CASE_IDS", ""))
    chunk_size = max(1, int(os.environ.get("CHUNK_SIZE", "10") or 10))
    max_batches = max(1, int(os.environ.get("MAX_BATCHES", "10") or 10))
    account_count = max(1, int(os.environ.get("SMC_ACCOUNT_COUNT", "1") or 1))

    if debug_mode_on():
        # No chunking at all — prepare.py's own "exactly one case id" guard
        # must stay the thing that decides, since a debug run creates a real
        # MDT on SMC for every case it touches.
        if not requested:
            emit([], 0)
            return
        emit([{"name": "debug", "slot": 1,
               "case_ids": ",".join(str(i) for i in requested)}], 1)
        return

    filters = {"case_status": "eq.READY_TO_SUBMIT"}
    if requested:
        filters["id"] = f"in.({','.join(str(i) for i in requested)})"

    cases = sb.select(CASES_TABLE, select="id,patient_id", filters=filters,
                      order="created_at.asc")

    chunks = build_chunks(cases, chunk_size, max_batches)
    matrix = [
        {
            "name": f"batch-{i + 1}",
            "case_ids": ",".join(str(cid) for cid in chunk),
            # Round-robin over however many SMC logins exist. With
            # SMC_ACCOUNT_COUNT=1 every batch gets slot 1 and they all share
            # the primary account — allowed, but read the workflow's
            # max-parallel comment before trusting it at 10 runners.
            "slot": (i % account_count) + 1,
        }
        for i, chunk in enumerate(chunks)
    ]
    emit(matrix, len(matrix))


if __name__ == "__main__":
    main()
