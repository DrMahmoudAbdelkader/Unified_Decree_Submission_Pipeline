#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_plantext.py
decree_topup_candidates has NO plan_text column in this database: the text is only exposed by the views
decree_topup_overview / decree_topup_overview_inclusive (that is what decree-topup.js reads). Selecting plan_text from the
table gives HTTP 400 / 42703, which stopped the family sweep and CHECK 3 of topup_live_checks.py.

    fill(rows) -> rows      adds 'plan_text' (from the views) to every candidate dict that lacks it. Never raises;
                            an entry whose text cannot be read keeps plan_text = None (the family code skips those).
"""
from __future__ import annotations
import logging
import supabase_client as sb

log = logging.getLogger("topup_plantext")
_VIEWS = ("decree_topup_overview", "decree_topup_overview_inclusive")


def fill(rows):
    try:
        need = sorted({int(r["id"]) for r in (rows or []) if r.get("id") is not None and not r.get("plan_text")})
        got = {}
        for view in _VIEWS:
            todo = [i for i in need if i not in got]
            for k in range(0, len(todo), 100):
                ch = todo[k:k + 100]
                try:
                    for x in sb.select(view, select="id,plan_text", filters={"id": "in.(" + ",".join(map(str, ch)) + ")"},
                                       limit=1000) or []:
                        if x.get("plan_text"):
                            got[int(x["id"])] = x["plan_text"]
                except Exception as e:
                    log.warning(f"plan_text not readable from {view} ({type(e).__name__}: {str(e)[:100]})")
                    break
            if len(got) >= len(need):
                break
        for r in rows or []:
            if not r.get("plan_text") and r.get("id") is not None:
                r["plan_text"] = got.get(int(r["id"]))
    except Exception as e:
        log.warning(f"plan_text fill skipped ({type(e).__name__}: {str(e)[:100]})")
    return rows
