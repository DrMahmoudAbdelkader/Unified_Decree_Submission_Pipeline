#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_resolution.py
==========================================================================
Detects an administrative letter that is ALREADY RESOLVED, so nothing has to be re-requested.

An entry (kind TOPUP / PREV_INCLUSIVE) is resolved automatically when BOTH hold:
  A. the patient has a decree of the SAME plan (or an overlapping hormonal/Zoladex protocol, see topup_overlap.py)
     whose ISSUE date is AFTER the date of the request that received the letter
        -> it was issued because another request for this patient went through after the letter;
  B. that decree is still usable (not stopped / expired) and its REAL value left (value left on the site minus what is
     waiting in Enhanced Monitor) covers at least one dose (TOPUP_RESOLVE_MIN_DOSES, default 1).

Dose value = the average invoiced dose of the entry (decree_topup_dose_calc.avg_dose_value) when it exists, else the
decree's own value / its authorised doses (topup_regimen_map.doses_per_decree). Compared with a small tolerance
(TOPUP_RESOLVE_TOLERANCE, default 1 %), because a decree's left value is often a few piastres below the dose price
(e.g. left 5662.14 vs dose 5662.50).

If A holds but B does not (the later decree is used up / unusable) the entry is NOT resolved: a new request is needed,
and that later decree is the "previous decree" whose bills are attached (topup_evidence_fetch no longer drops decrees
issued after the request - TOPUP_INCLUDE_LATER_DECREES=0 restores the old cut-off).

Resolution = candidate.status 'RESOLVED' (same as the page's «إغلاق (تم الحل)» button) + a message starting with
AUTO_PREFIX. Set TOPUP_AUTO_RESOLVE=0 to switch the whole thing off. Reopen an entry by hand on the page if needed.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Optional

import supabase_client as sb
import topup_dose_count as T
import topup_overlap as ov

log = logging.getLogger("topup_resolution")
CAND = "decree_topup_candidates"
AUTO_PREFIX = "تم الحل تلقائياً"            # also used by topup_gate.py (duplicated there on purpose)
TOPUP_LIKE = (None, "", "TOPUP", "PREV_INCLUSIVE")
DONE_STATUSES = ("SUBMITTED", "DISMISSED", "RESOLVED")


def enabled() -> bool:
    return (os.environ.get("TOPUP_AUTO_RESOLVE") or "1").strip().lower() not in ("0", "false", "off", "no")


def _min_doses() -> float:
    try:
        return float(os.environ.get("TOPUP_RESOLVE_MIN_DOSES") or "1")
    except ValueError:
        return 1.0


def _tolerance() -> float:
    try:
        return float(os.environ.get("TOPUP_RESOLVE_TOLERANCE") or "0.01")
    except ValueError:
        return 0.01


# Columns of decree_request_cases that may hold the date the request really reached the website. The first one that exists
# and is filled wins; otherwise created_at (the day the entry was made in the app, never LATER than the website date).
# Override with TOPUP_REQUEST_DATE_COLUMNS=col1,col2
def _request_date_columns() -> list:
    raw = os.environ.get("TOPUP_REQUEST_DATE_COLUMNS")
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else ["submitted_at", "website_request_date", "request_date"]


def request_date(c: dict) -> Optional[date]:
    """Date of the request that received the letter (the entry's source case)."""
    if not c.get("source_case_id"):
        return None
    flt = {"id": f"eq.{c['source_case_id']}"}
    for col in _request_date_columns():
        try:
            r = sb.select("decree_request_cases", select=col, filters=flt, limit=1)
            d = ov._d((r[0].get(col) or "")[:10]) if r else None
            if d:
                return d
        except Exception:
            continue                                   # column does not exist in this database
    try:
        r = sb.select("decree_request_cases", select="created_at", filters=flt, limit=1)
        return ov._d((r[0].get("created_at") or "")[:10]) if r else None
    except Exception as e:
        log.warning(f"request date of case {c.get('source_case_id')} unreadable: {e}")
        return None


def dose_value(c: dict, row: dict) -> Optional[float]:
    """Value of ONE dose for this decree (see the module doc), or None when it cannot be known.
    A protocol's FIXED limit (decree_topup_protocols.value_cutoff, e.g. 600 for the supportive plan) comes first - it is the
    same number the DB triage view uses (COALESCE(value_cutoff, avg_dose_value)), so both agree."""
    try:
        pk = c.get("protocol_key")
        if pk:
            pr = sb.select("decree_topup_protocols", select="value_cutoff", filters={"protocol_key": f"eq.{pk}"}, limit=1)
            if pr and pr[0].get("value_cutoff"):
                return float(pr[0]["value_cutoff"])
    except Exception:
        pass                                              # table/column missing: continue with the average
    try:
        r = sb.select("decree_topup_dose_calc", select="avg_dose_value", filters={"candidate_id": f"eq.{c['id']}"}, limit=1)
        if r and r[0].get("avg_dose_value"):
            return float(r[0]["avg_dose_value"])
    except Exception:
        pass                                              # table/column missing: fall back to the estimate
    try:
        import topup_dose_calc as dc
        mp, idx, _mt = dc.load_map()
        _status, plan = T.regimen_for_plan(idx, row.get("description") or "")
        if plan and plan.get("doses_per_decree") and row.get("decree_value"):
            return float(row["decree_value"]) / float(plan["doses_per_decree"])
    except Exception as e:
        log.debug(f"dose value estimate failed: {e}")
    # plan text not in the regimen map (spelling differs): "... 4 جرعات ..." in the description gives the authorised doses
    m = re.search(r"(\d+)\s*جرع", (row.get("description") or "").translate(T.ARABIC_DIGITS))
    if m and int(m.group(1)) > 0 and row.get("decree_value"):
        return float(row["decree_value"]) / int(m.group(1))
    return None


def evaluate(c: dict) -> dict:
    """-> dict(resolved, message, reason, decree). Never raises."""
    res = dict(resolved=False, message="", reason="", decree=None)
    try:
        if c.get("kind") not in TOPUP_LIKE:
            res["reason"] = "kind not eligible"; return res
        if c.get("manual_decree_numbers") or c.get("manual_receipt_ids"):
            res["reason"] = "operator typed decrees/receipts by hand - left alone"; return res
        try:
            sk = sb.select("decree_topup_protocols", select="skip_value_check", filters={"protocol_key": f"eq.{c.get('protocol_key')}"}, limit=1) if c.get("protocol_key") else []
            if sk and sk[0].get("skip_value_check"):
                res["reason"] = "protocol skips the value check"; return res
        except Exception:
            pass
        req = request_date(c)
        if not req:
            res["reason"] = "request date unknown"; return res
        matched = ov.matching_rows(c)
        later = [m for m in matched if m["_date"] and m["_date"] > req]
        if not later:
            res["reason"] = "no same-plan decree issued after the request"; return res
        today = date.today()
        notes = []
        for m in sorted(later, key=lambda r: (r["_date"], r["decree_number"]), reverse=True):    # newest first
            num = m["decree_number"]
            if m.get("flag") in ("expired", "stopped"):
                notes.append(f"{num}: {m['flag']}"); continue
            if m["_expiry"] and m["_expiry"] < today:
                notes.append(f"{num}: expired on {m['_expiry']}"); continue
            left = m.get("real_value_left")
            if left is None:
                left = m.get("value_left_website")
            if left is None:
                notes.append(f"{num}: value left unknown"); continue
            dv = dose_value(c, m)
            if not dv:
                # dose price unknown: only an UNTOUCHED decree (left = its whole value) is safe to call resolved
                dec_val = m.get("decree_value")
                if dec_val and float(left) >= float(dec_val) * (1 - _tolerance()) and float(dec_val) > 0:
                    res.update(resolved=True, decree=num, message=(
                        f"{AUTO_PREFIX}: صدر للمريض القرار {num} بنفس الخطة بتاريخ {m['_date']:%d/%m/%Y} بعد تاريخ هذا الطلب "
                        f"({req:%d/%m/%Y}) ولم يُصرف منه شيء بعد (المتبقي {float(left):,.2f} = كامل القيمة) — لا حاجة لطلب جديد."))
                    return res
                notes.append(f"{num}: dose value unknown"); continue
            need = dv * _min_doses() * (1 - _tolerance())
            if float(left) >= need:
                res.update(resolved=True, decree=num, message=(
                    f"{AUTO_PREFIX}: صدر للمريض القرار {num} بنفس الخطة بتاريخ {m['_date']:%d/%m/%Y} بعد تاريخ هذا الطلب "
                    f"({req:%d/%m/%Y}) والمتبقي الحقيقي {float(left):,.2f} يغطي جرعة ({dv:,.2f}) — لا حاجة لطلب جديد."))
                return res
            notes.append(f"{num}: left {float(left):,.2f} < dose {dv:,.2f}")
        res["reason"] = "later decree(s) exist but none covers a dose: " + "; ".join(notes)
        return res
    except Exception as e:
        res["reason"] = f"check skipped ({type(e).__name__}: {str(e)[:120]})"
        log.warning(f"  candidate {c.get('id')}: resolution check skipped: {res['reason']}")
        return res


def auto_resolve(c: dict) -> Optional[dict]:
    """Evaluate and, when resolved, close the entry. Returns the evaluation when it was closed, else None."""
    if not enabled():
        return None
    try:
        fresh = sb.select(CAND, select="*", filters={"id": f"eq.{c['id']}"}, limit=1)
        fresh = fresh[0] if fresh else c
        if fresh.get("status") in DONE_STATUSES:
            return None
        ev = evaluate(fresh)
        if not ev["resolved"]:
            log.info(f"  candidate {c['id']}: not auto-resolved - {ev['reason']}")
            return None
        sb.update(CAND, c["id"], {"status": "RESOLVED", "evidence_status": "NO_PREVIOUS_DECREE",
                                  "evidence_message": ev["message"][:480]})
        log.info(f"  candidate {c['id']}: AUTO-RESOLVED by decree {ev['decree']}")
        return ev
    except Exception as e:
        log.warning(f"  candidate {c.get('id')}: auto-resolve skipped ({type(e).__name__}: {str(e)[:140]})")
        return None


def auto_resolve_by_id(cid: int) -> Optional[dict]:
    rows = sb.select(CAND, select="*", filters={"id": f"eq.{cid}"}, limit=1)
    return auto_resolve(rows[0]) if rows else None
