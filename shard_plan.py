#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shard_plan.py
==========================================================================
ONE planner for every workflow that fans out into parallel shards (the
generalisation of chunk_cases.py, which keeps serving submit-decree-prepare).
It runs as the cheap first job (`plan`, ~20 s, no Playwright / PDF libs) and
writes a GitHub Actions matrix; the real job then runs one runner per entry,
all at once.

MODE (env) decides what is being split:

  topup     decree_topup_candidates that need ANY of the three top-up steps
            (patient lookup / previous-decree report / signed invoices), or the
            explicit CANDIDATE_IDS the page's buttons pass in.
            -> entries carry `ids`   (becomes CANDIDATE_IDS of that shard)
  finalize  the CASE_IDS handed over by the edge function.
            -> entries carry `ids`   (becomes CASE_IDS of that shard)
  status    open attempts + seed rows of the daily status sweep.
            -> entries carry `shard_index` / `shard_count`
  value     decrees that have a number but no value yet.
            -> entries carry `shard_index` / `shard_count`
  open      decrees with pending Enhanced-Monitor entries (decree open check).
            -> same, and also creates the decree_check_runs row (run_id output)

Common env:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  CHUNK_SIZE         items per shard ("about this many per runner")
  MAX_SHARDS         hard ceiling on parallel runners, default 15. If the queue
                     needs more, shards GROW instead of more runners starting.
  SMC_ACCOUNT_COUNT  how many SMC logins the shards may rotate over (the `slot`
                     field); 1 = every shard uses the workflow's usual account.

THE TWO INVARIANTS (same as chunk_cases.py)
  1. A multi-shard job NEVER gets a blank id list (blank = "the whole queue",
     which every sibling shard would then also process). The one exception is
     the single-shard FALLBACK when the planner's own query fails: one runner,
     whole queue = exactly yesterday's behaviour.
  2. Rows that must be handled together land in the same shard: a patient's
     candidates / cases are never split (see chunk_cases.build_chunks).

Outputs (GITHUB_OUTPUT): matrix (JSON list), count, run_id (open mode only).
count == 0 means "nothing to do": the caller skips the shard job, because an
empty matrix is a hard error in Actions.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import supabase_client as sb
from chunk_cases import build_chunks

CAND = "decree_topup_candidates"


def _int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


MODE = (os.environ.get("MODE") or "").strip().lower()
CHUNK = max(1, _int("CHUNK_SIZE", 10))
MAX_SHARDS = max(1, _int("MAX_SHARDS", 15))
ACCOUNTS = max(1, _int("SMC_ACCOUNT_COUNT", 1))


def parse_ids(raw):
    return [int(x) for x in re.findall(r"\d+", raw or "")]


def paged(table, select, filters=None, order=None):
    """PostgREST silently caps unpaged answers; page like the other scripts do."""
    rows, offset, page = [], 0, 1000
    while True:
        f = dict(filters or {})
        f["offset"] = str(offset)
        chunk = sb.select(table, select=select, filters=f, order=order, limit=page)
        rows += chunk
        if len(chunk) < page:
            return rows
        offset += page


# --------------------------------------------------------------------------- id-list modes
def id_matrix(rows):
    cases = [{"id": r["id"], "patient_id": r.get("patient_id")} for r in rows]
    chunks = build_chunks(cases, CHUNK, MAX_SHARDS)
    return [{"name": f"shard-{i + 1}", "n": i + 1, "slot": (i % ACCOUNTS) + 1,
             "ids": ",".join(str(c) for c in chunk)} for i, chunk in enumerate(chunks)]


def plan_topup():
    given = parse_ids(os.environ.get("CANDIDATE_IDS"))
    try:
        if given:
            rows = paged(CAND, "id,patient_id", {"id": f"in.({','.join(map(str, given))})"})
            known = {r["id"] for r in rows}
            rows += [{"id": i, "patient_id": None} for i in given if i not in known]
        else:
            seen = {}
            queues = [
                # 1) «فحص قرارات المريض» lookups
                {"lookup_status": "eq.PENDING"},
                # 2) signed invoices (topup_evidence_fetch.py's own filter)
                {"evidence_status": "eq.PENDING", "protocol_key": "not.is.null",
                 "status": "not.in.(SUBMITTED,DISMISSED)"},
                # 3) previous-decree reports (topup_report_fetch.py's own filter)
                {"kind": "eq.PREV_REPORT", "evidence_status": "eq.REPORT_PENDING",
                 "status": "not.in.(SUBMITTED,DISMISSED,RESOLVED)"},
            ]
            for f in queues:
                for r in paged(CAND, "id,patient_id", f):
                    seen.setdefault(r["id"], r)
            rows = sorted(seen.values(), key=lambda r: r["id"])
    except Exception as exc:
        if given:                      # we know the ids: just don't group by patient
            rows = [{"id": i, "patient_id": None} for i in given]
        else:
            print(f"::warning::planner query failed ({exc}) - falling back to ONE runner on the whole queue")
            return [{"name": "shard-1", "n": 1, "slot": 1, "ids": ""}], {}
    return id_matrix(rows), {}


def plan_finalize():
    ids = parse_ids(os.environ.get("CASE_IDS"))
    if not ids:
        return [], {}
    try:
        rows = paged("decree_request_cases", "id,patient_id", {"id": f"in.({','.join(map(str, ids))})"})
    except Exception as exc:
        print(f"::warning::patient grouping skipped ({exc})")
        rows = []
    known = {r["id"] for r in rows}
    rows += [{"id": i, "patient_id": None} for i in ids if i not in known]
    return id_matrix(rows), {}


# --------------------------------------------------------------------------- numeric-shard modes
def numeric_matrix(work, minimum):
    n = min(MAX_SHARDS, max(minimum, math.ceil(work / CHUNK)))
    if work == 0 and minimum == 0:
        n = 0
    return [{"name": f"shard-{i + 1}", "n": i + 1, "slot": (i % ACCOUNTS) + 1,
             "shard_index": i, "shard_count": n} for i in range(n)]


def plan_status():
    # never 0: the seed refresh + decree-number extraction run even with no open attempts
    try:
        attempts = paged("decree_request_attempts", "id", {
            "website_request_id": "not.is.null",
            "or": "(status_is_final.is.false,status_is_final.is.null)"})
        seed = paged("decree_request_month_seed", "id")
        work = len(attempts) + len(seed)
    except Exception as exc:
        print(f"::warning::planner count failed ({exc}) - ONE runner")
        return numeric_matrix(0, 1), {}
    return numeric_matrix(work, 1), {}


def plan_value():
    cap = _int("MAX_VALUE_LOOKUPS", 1000)
    try:
        decrees = set()
        for table in ("decree_request_attempts", "decree_request_month_seed"):
            for r in paged(table, "decree_number", {"decree_number": "not.is.null", "decree_value": "is.null"}):
                decrees.add(str(r["decree_number"]).strip())
        work = min(len(decrees), cap)
    except Exception as exc:
        print(f"::warning::planner count failed ({exc}) - ONE runner")
        return numeric_matrix(0, 1), {}
    return numeric_matrix(work, 0), {}


def plan_open():
    only = (os.environ.get("ONLY_DECREE") or "").strip()
    dry = (os.environ.get("DRY_RUN") or "").strip().lower() == "true"
    try:
        rows = paged("pending_decree_orders", "decree_number",
                     {"status": "neq.completed", "decree_number": "not.is.null"})
        decrees = {str(r["decree_number"]).strip() for r in rows}
        if only:
            decrees = {d for d in decrees if d == only}
    except Exception as exc:
        print(f"::warning::planner count failed ({exc}) - ONE runner")
        return numeric_matrix(0, 1), {"run_id": ""}
    matrix = numeric_matrix(len(decrees), 1)      # >=1: orphan cycles are closed even with nothing pending
    run_id = ""
    if not dry:
        # the checker used to create this row itself; with shards the planner does it ONCE and
        # open_check_finish.py fills in the combined counters when every shard is done
        row = sb.insert("decree_check_runs", {"trigger_type": os.environ.get("TRIGGER_TYPE") or "local",
                                              "decrees_total": len(decrees)})
        run_id = row.get("id", "")
    return matrix, {"run_id": run_id}


PLANNERS = {"topup": plan_topup, "finalize": plan_finalize, "status": plan_status,
            "value": plan_value, "open": plan_open}


def emit(matrix, extra):
    payload = json.dumps(matrix, ensure_ascii=False)
    lines = {"count": len(matrix), "matrix": payload, **{k: v for k, v in extra.items()}}
    for k, v in lines.items():
        print(f"{k}={v}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            for k, v in lines.items():
                f.write(f"{k}={v}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## Shard plan ({MODE}) - {len(matrix)} runner(s) in parallel, max {MAX_SHARDS}\n")
            for m in matrix:
                what = (f"{len(m['ids'].split(',')) if m['ids'] else 'ALL'} item(s): {m['ids']}" if "ids" in m
                        else f"slice {m['shard_index'] + 1}/{m['shard_count']}")
                f.write(f"- `{m['name']}` (SMC slot {m['slot']}): {what}\n")


def main():
    if MODE not in PLANNERS:
        raise SystemExit(f"MODE must be one of {sorted(PLANNERS)}, got {MODE!r}")
    matrix, extra = PLANNERS[MODE]()
    emit(matrix, extra)


if __name__ == "__main__":
    main()
