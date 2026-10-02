#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_notify.py
==========================================================================
Reports a workflow run's lifecycle to Supabase so the module's notification
center can show it. Needs only `requests` (through supabase_client.py), so it
runs in the cheap `plan` / `notify` jobs without installing requirements.txt.

Environment
    RUN_REF            uuid the edge function passed as the `run_ref` input.
                       Blank for a run started by hand from the GitHub UI —
                       then `finish` just posts a plain notification to every
                       module user and nothing else is tracked.
    PHASE              "prepare" | "finalize"             (finish / started)
    GITHUB_RUN_ID      provided by Actions                (started)

CLI (used by the workflows)
    python run_notify.py started
    python run_notify.py finish          # reads OUTCOME, or PLAN_RESULT + PREPARE_RESULT + PLAN_COUNT

Library (used by decree_submission_prepare.py / decree_submission_finalize.py)
    run_notify.report_counts({...})

EVERYTHING HERE IS BEST-EFFORT. A notification problem must never turn a
good submission run red or hide its real result, so every function swallows
its own errors and only logs a warning.
"""

from __future__ import annotations

import logging
import os
import sys

import supabase_client as sb

log = logging.getLogger("run_notify")


def _run_ref() -> str | None:
    return (os.environ.get("RUN_REF") or "").strip() or None


def report_counts(counts: dict) -> None:
    """Adds this process's counters to the run. Numbers are summed, lists are
    concatenated — so the N parallel prepare batches add up to one result."""
    ref = _run_ref()
    if not ref:
        return
    try:
        sb.rpc("add_decree_run_counts", {"p_run_id": ref, "p_counts": counts})
    except Exception as exc:  # noqa: BLE001
        log.warning(f"run_notify.report_counts failed (ignored): {exc}")


def mark_started() -> None:
    ref = _run_ref()
    if not ref:
        return
    try:
        run_id = int(os.environ.get("GITHUB_RUN_ID") or 0) or None
        sb.rpc("mark_decree_run_started", {"p_run_id": ref, "p_github_run_id": run_id})
    except Exception as exc:  # noqa: BLE001
        log.warning(f"run_notify.mark_started failed (ignored): {exc}")


def _outcome_from_env() -> str:
    """success | failure | cancelled."""
    direct = (os.environ.get("OUTCOME") or "").strip().lower()
    if direct:
        return direct if direct in ("success", "failure", "cancelled") else "failure"
    plan = (os.environ.get("PLAN_RESULT") or "").strip().lower()
    prep = (os.environ.get("PREPARE_RESULT") or "").strip().lower()
    if "cancelled" in (plan, prep):
        return "cancelled"
    if plan == "failure":
        return "failure"
    # prepare is skipped on purpose when the planner found nothing to do
    if prep in ("success", "skipped"):
        return "success"
    return "failure"   # prepare 'failure' (a batch exited 1) — counts already reported explain it


def finish() -> None:
    outcome = _outcome_from_env()
    ref = _run_ref()
    try:
        if ref:
            sb.rpc("finish_decree_run", {"p_run_id": ref, "p_outcome": outcome})
            return
        phase = (os.environ.get("PHASE") or "prepare").strip()
        label = "جلسة الإرسال" if phase == "prepare" else "التوقيع والتقديم"
        level = {"success": "success", "cancelled": "warning"}.get(outcome, "error")
        word = {"success": "انتهت", "cancelled": "أُلغيت"}.get(outcome, "انتهت بخطأ")
        sb.insert("decree_notifications", {
            "user_id": None, "level": level, "kind": f"{phase}_manual",
            "title": f"{label} (تشغيل يدوي من GitHub) {word}",
            "body": "لم يبدأ هذا التشغيل من الوحدة، لذلك لا تتوفر تفاصيل الطلبات هنا — راجع سجل GitHub Actions.",
            "phase": phase,
        })
    except Exception as exc:  # noqa: BLE001
        log.warning(f"run_notify.finish failed (ignored): {exc}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "started":
        mark_started()
    elif cmd == "finish":
        finish()
    else:
        print("usage: run_notify.py started|finish", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)   # never fail the job because of a notification
