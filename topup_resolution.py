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

GENERAL RULE (v9, same in decree-topup.js):
  * ONE DOSE =  supportive plans -> SUPPORTIVE_DOSE_VALUE (500 EGP, env TOPUP_SUPPORTIVE_DOSE_VALUE)
                protocol with a fixed limit -> decree_topup_protocols.value_cutoff
                otherwise -> average price of a dose dispensed so far: decree_topup_dose_calc.avg_dose_value, else the stored
                             decree_topup_dose_price.avg_dose_value (written by refresh_dose_price(); it exists for kinds without a
                             dose count such as «القرار السابق شامل» and for supportive plans), else decree value / authorised doses
  * It applies to «التجديد في نهاية المدة» AND «القرار السابق شامل», for entries that are still open AND for entries already
    SUBMITTED (the tracking tab) - TOPUP_AUTO_RESOLVE_SUBMITTED=0 leaves the tracked ones alone.
  * The decree may have been issued from the app OR directly on the SMC website: it is read from the patient's page by
    topup_decree_lookup.py, so both count the same.
  * sweep() applies the rule to every such entry (the nightly live-checks run calls it).

Resolution = candidate.status 'RESOLVED' (same as the page's «إغلاق (تم الحل)» button) + a message starting with
AUTO_PREFIX. Set TOPUP_AUTO_RESOLVE=0 to switch the whole thing off. Reopen an entry by hand on the page if needed.

v10 (see topup_family.py)
  * SAME-PLAN MATCH BY FAMILY: a decree whose description differs from the entry's plan only by the dose count / "(شامل ...)"
    (4 vs 6 doses ...) is the same plan. Before, such a decree was never matched, so the entry was never resolved.
  * ONE PASS, FEW QUERIES: sweep() loads protocols, dose prices, request dates and the patient decrees ONCE for all entries
    (Ctx) instead of 8-12 queries per entry, and never re-reads a row it just read.
  * FAMILY FIRST: sweep() starts with topup_family.sweep() (duplicates / already-served families are closed before any
    decree is evaluated) and a closed entry hands over to topup_family.propagate() so its siblings follow at once.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Optional

import supabase_client as sb
import topup_dose_count as T
import topup_family as fam
import topup_overlap as ov

log = logging.getLogger("topup_resolution")
CAND = "decree_topup_candidates"
AUTO_PREFIX = "تم الحل تلقائياً"            # also used by topup_gate.py (duplicated there on purpose)
TOPUP_LIKE = (None, "", "TOPUP", "PREV_INCLUSIVE")
SUPPORTIVE_DOSE_VALUE = float(os.environ.get("TOPUP_SUPPORTIVE_DOSE_VALUE") or "500")
PRICE = "decree_topup_dose_price"
# SUPPORTIVE plans (bone protection / adjuvant supportive treatment): same words as SUPPORTIVE_PLAN_RE in decree-topup.js
_SUPPORTIVE_WORDS = ("وقايه العظام", "معالجه العظام", "زيلودرونيك", "زوليدرونيك", "zoledron", "دينوزوماب", "دينوسوماب",
                     "denosumab", "العلاج المساعد", "تدعيمي")


def _submitted_too() -> bool:
    return (os.environ.get("TOPUP_AUTO_RESOLVE_SUBMITTED") or "1").strip().lower() not in ("0", "false", "off", "no")


def _done_statuses() -> tuple:
    """Entries that are never touched. SUBMITTED (= being tracked after a resubmission) is open to the rule unless switched off."""
    return ("DISMISSED", "RESOLVED") + (() if _submitted_too() else ("SUBMITTED",))


def is_supportive(c: dict, row: Optional[dict] = None) -> bool:
    pk = str(c.get("protocol_key") or "").lower()
    if "support" in pk:
        return True
    txt = T.plan_key(" ".join(str(x or "") for x in (c.get("plan_text"), (row or {}).get("description"))))
    return bool(txt) and any(w in txt for w in _SUPPORTIVE_WORDS)


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


_REQ_DATE_COLS = None
_REQ_CACHE: dict = {}                                 # source_case_id -> date (None = unknown), per process


def _resolved_request_date_columns() -> list:
    """Probe the candidate columns ONCE per run (not once per candidate) and keep only the ones that exist."""
    global _REQ_DATE_COLS
    if _REQ_DATE_COLS is None:
        ok = []
        for col in _request_date_columns():
            try:
                sb.select("decree_request_cases", select=col, limit=1)
                ok.append(col)
            except Exception:
                pass                                   # column does not exist in this database
        _REQ_DATE_COLS = ok
    return _REQ_DATE_COLS


def request_dates(case_ids) -> dict:
    """{case_id: date} for MANY requests in ONE query per 150 (was 1-4 queries per entry). The first filled column of
    _request_date_columns() wins, otherwise created_at."""
    want = sorted({int(x) for x in case_ids if x} - set(_REQ_CACHE))
    if want:
        cols = _resolved_request_date_columns()
        sel = ",".join(["id", "created_at"] + cols)
        for i in range(0, len(want), 150):
            ch = want[i:i + 150]
            try:
                rows = sb.select("decree_request_cases", select=sel, filters={"id": "in.(" + ",".join(map(str, ch)) + ")"}, limit=1000) or []
            except Exception as e:
                log.warning(f"request dates unreadable: {e}")
                rows = []
            got = set()
            for r in rows:
                d = None
                for col in cols:
                    d = ov._d((r.get(col) or "")[:10])
                    if d:
                        break
                _REQ_CACHE[int(r["id"])] = d or ov._d((r.get("created_at") or "")[:10])
                got.add(int(r["id"]))
            for x in ch:
                _REQ_CACHE.setdefault(x, None)
    return {int(x): _REQ_CACHE.get(int(x)) for x in case_ids if x}


def request_date(c: dict) -> Optional[date]:
    """Date of the request that received the letter (the entry's source case)."""
    sid = c.get("source_case_id")
    return request_dates([sid]).get(int(sid)) if sid else None


# --------------------------------------------------------------------------- bulk context for a sweep
class Ctx:
    """Everything evaluate() needs that is the same for many entries, read ONCE: protocols, stored dose prices and the
    patient decrees (view decree_topup_lookup_view). evaluate(c) without a Ctx still works (it queries per entry)."""

    def __init__(self):
        self.protocols: dict = {}
        self.dose_calc: dict = {}
        self.price: dict = {}
        self.lookup: dict = {}

    @classmethod
    def load(cls, cands: list) -> "Ctx":
        x = cls()
        try:
            try:
                rows = sb.select("decree_topup_protocols", select="protocol_key,value_cutoff,skip_value_check", limit=500) or []
            except Exception:
                rows = sb.select("decree_topup_protocols", select="protocol_key,value_cutoff", limit=500) or []
            x.protocols = {r["protocol_key"]: r for r in rows}
        except Exception as e:
            log.debug(f"protocols not preloaded: {e}")
        ids = [int(c["id"]) for c in cands]
        for i in range(0, len(ids), 150):
            flt = {"candidate_id": "in.(" + ",".join(map(str, ids[i:i + 150])) + ")"}
            for table, dest in (("decree_topup_dose_calc", x.dose_calc), (PRICE, x.price)):
                try:
                    for r in sb.select(table, select="candidate_id,avg_dose_value", filters=flt, limit=1000) or []:
                        dest[int(r["candidate_id"])] = r.get("avg_dose_value")
                except Exception:
                    pass                               # table missing (migration 12 / 16 not applied)
        for i in range(0, len(ids), 100):
            try:
                for r in sb.select("decree_topup_lookup_view",
                                   select="candidate_id,decree_number,description,decree_date,expiry_date,flag,match_kind,"
                                          "real_value_left,value_left_website,decree_value",
                                   filters={"candidate_id": "in.(" + ",".join(map(str, ids[i:i + 100])) + ")"}, limit=1000) or []:
                    x.lookup.setdefault(int(r["candidate_id"]), []).append(r)
            except Exception as e:
                log.debug(f"patient decrees not preloaded: {e}")
                break
        return x


def family_extra_rows(c: dict, ctx: Optional[Ctx] = None, exclude=()) -> list:
    """Decrees of the patient whose description is the same plan FAMILY as the entry's plan text (dose count / "(شامل ...)"
    ignored), shaped like topup_overlap rows (_date / _expiry / _kind='FAMILY'). `exclude` = decree numbers already matched.
    TOPUP_FAMILY_MATCH=0 returns nothing."""
    if not fam._flag("TOPUP_FAMILY_MATCH"):
        return []
    mine = fam.plan_family(c.get("plan_text"))
    if not mine:
        return []
    seen = {str(x) for x in exclude}
    if ctx is not None:
        src = ctx.lookup.get(int(c["id"]), [])
    else:
        try:
            src = sb.select("decree_topup_lookup_view",
                            select="decree_number,description,decree_date,expiry_date,flag,match_kind,real_value_left,value_left_website,decree_value",
                            filters={"candidate_id": f"eq.{c['id']}"}, limit=500) or []
        except Exception:
            src = []
    out = []
    for r in src:
        num = str(r.get("decree_number"))
        if num in seen or fam.plan_family(r.get("description")) != mine:
            continue
        seen.add(num)
        out.append({**r, "decree_number": num, "_date": ov._d((r.get("decree_date") or "")[:10]),
                    "_expiry": ov._d((r.get("expiry_date") or "")[:10]), "_kind": "FAMILY"})
    return out


def family_rows(c: dict, ctx: Optional[Ctx] = None) -> list:
    """The patient's decrees that count as 'same plan' for this entry: the matches of topup_overlap (plan / protocol /
    overlapping hormonal-Zoladex) PLUS every decree of the same plan FAMILY (see family_extra_rows)."""
    matched = list(ov.matching_rows(c) or [])
    return matched + family_extra_rows(c, ctx, {str(m.get("decree_number")) for m in matched})


def dose_value(c: dict, row: dict, ctx: Optional[Ctx] = None) -> Optional[float]:
    """Value of ONE dose for this decree (see the module doc), or None when it cannot be known.
    A protocol's FIXED limit (decree_topup_protocols.value_cutoff, e.g. 600 for the supportive plan) comes first - it is the
    same number the DB triage view uses (COALESCE(value_cutoff, avg_dose_value)), so both agree."""
    if is_supportive(c, row):
        return SUPPORTIVE_DOSE_VALUE                       # supportive decrees: 500 EGP or more left = a dose can still be dispensed
    if ctx is not None:                                   # bulk-loaded: no query at all
        pk = c.get("protocol_key")
        if pk and (ctx.protocols.get(pk) or {}).get("value_cutoff"):
            return float(ctx.protocols[pk]["value_cutoff"])
        for src in (ctx.dose_calc, ctx.price):
            v = src.get(int(c["id"]))
            if v:
                return float(v)
    else:
        try:
            pk = c.get("protocol_key")
            if pk:
                pr = sb.select("decree_topup_protocols", select="value_cutoff", filters={"protocol_key": f"eq.{pk}"}, limit=1)
                if pr and pr[0].get("value_cutoff"):
                    return float(pr[0]["value_cutoff"])
        except Exception:
            pass                                          # table/column missing: continue with the average
        try:
            r = sb.select("decree_topup_dose_calc", select="avg_dose_value", filters={"candidate_id": f"eq.{c['id']}"}, limit=1)
            if r and r[0].get("avg_dose_value"):
                return float(r[0]["avg_dose_value"])
        except Exception:
            pass                                          # table/column missing: fall back to the estimate
        try:
            r = sb.select(PRICE, select="avg_dose_value", filters={"candidate_id": f"eq.{c['id']}"}, limit=1)
            if r and r[0].get("avg_dose_value"):
                return float(r[0]["avg_dose_value"])       # stored average price of the dispensed doses (kinds without a dose count)
        except Exception:
            pass                                          # migration 16 not applied yet
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


def evaluate(c: dict, ctx: Optional[Ctx] = None) -> dict:
    """-> dict(resolved, message, reason, decree). Never raises."""
    res = dict(resolved=False, message="", reason="", decree=None)
    try:
        if c.get("kind") not in TOPUP_LIKE:
            res["reason"] = "kind not eligible"; return res
        if c.get("manual_decree_numbers") or c.get("manual_receipt_ids"):
            res["reason"] = "operator typed decrees/receipts by hand - left alone"; return res
        try:
            if ctx is not None:
                sk = [ctx.protocols[c["protocol_key"]]] if c.get("protocol_key") in ctx.protocols else []
            else:
                sk = sb.select("decree_topup_protocols", select="skip_value_check", filters={"protocol_key": f"eq.{c.get('protocol_key')}"}, limit=1) if c.get("protocol_key") else []
            if sk and sk[0].get("skip_value_check") and not is_supportive(c):      # supportive plans are judged by the 500 EGP limit instead
                res["reason"] = "protocol skips the value check"; return res
        except Exception:
            pass
        req = request_date(c)
        if not req:
            res["reason"] = "request date unknown"; return res
        matched = family_rows(c, ctx)
        later = [m for m in matched if m["_date"] and m["_date"] > req]
        if not later:
            res["reason"] = "no same-plan decree issued after the request"; return res
        today = date.today()
        notes = []
        for m in sorted(later, key=lambda r: (r["_date"], str(r["decree_number"])), reverse=True):    # newest first
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
            dv = dose_value(c, m, ctx)
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


def auto_resolve(c: dict, ctx: Optional[Ctx] = None, trust: bool = False) -> Optional[dict]:
    """Evaluate and, when resolved, close the entry. Returns the evaluation when it was closed, else None.
    trust=True: `c` was read a moment ago with select=* (sweep) - no second read. A closed entry hands over to
    topup_family.propagate() so the siblings of the same patient + plan family are closed with it."""
    if not enabled():
        return None
    try:
        if trust:
            fresh = c
        else:
            fresh = sb.select(CAND, select="*", filters={"id": f"eq.{c['id']}"}, limit=1)
            fresh = fresh[0] if fresh else c
        if fresh.get("status") in _done_statuses():
            return None
        ev = evaluate(fresh, ctx)
        if not ev["resolved"]:
            log.info(f"  candidate {c['id']}: not auto-resolved - {ev['reason']}")
            return None
        sb.update(CAND, c["id"], {"status": "RESOLVED", "evidence_status": "NO_PREVIOUS_DECREE",
                                  "evidence_message": ev["message"][:480]})
        log.info(f"  candidate {c['id']}: AUTO-RESOLVED by decree {ev['decree']}")
        try:
            fam.propagate({**fresh, "status": "RESOLVED", "evidence_message": ev["message"][:480]})
        except Exception as e:
            log.debug(f"family propagate skipped: {e}")
        return ev
    except Exception as e:
        log.warning(f"  candidate {c.get('id')}: auto-resolve skipped ({type(e).__name__}: {str(e)[:140]})")
        return None


def auto_resolve_by_id(cid: int) -> Optional[dict]:
    rows = sb.select(CAND, select="*", filters={"id": f"eq.{cid}"}, limit=1)
    return auto_resolve(rows[0]) if rows else None


# --------------------------------------------------------------------------- stored price of one dispensed dose
def save_dose_price(cid: int, avg, doses=None, source: str = "dose_calc") -> bool:
    """Keep the average price of ONE dispensed dose for this entry (decree_topup_dose_price, migration 16). The page and
    evaluate() both read it, so they use the same number. A price that comes from the dose count ('dose_calc') is never
    overwritten by the weaker invoice-average estimate ('invoices'). Never raises."""
    try:
        if not avg or float(avg) <= 0:
            return False
        if source != "dose_calc":
            old = sb.select(PRICE, select="source", filters={"candidate_id": f"eq.{cid}"}, limit=1)
            if old and old[0].get("source") == "dose_calc":
                return False
        sb.upsert(PRICE, [{"candidate_id": cid, "avg_dose_value": round(float(avg), 2),
                           "doses_counted": int(doses) if doses else None, "source": source,
                           "updated_at": date.today().isoformat()}], on_conflict="candidate_id")
        return True
    except Exception as e:
        log.warning(f"  candidate {cid}: dose price not stored ({type(e).__name__}: {str(e)[:100]}) - run migration 16")
        return False


def refresh_dose_price(cid: int) -> Optional[float]:
    """Average value of the stored invoices of this entry (one invoice = one dispensed dose cycle) -> saved as the dose price.
    This is what gives «القرار السابق شامل» entries and supportive plans a price although they have no dose count."""
    try:
        inv = sb.select("decree_topup_invoices", select="doc_kind,verification",
                        filters={"candidate_id": f"eq.{cid}", "status": "eq.OK"}, limit=500) or []
        vals = []
        for i in inv:
            if (i.get("doc_kind") or "INVOICE") != "INVOICE":
                continue
            amt = ((i.get("verification") or {}).get("listing") or {}).get("amount")
            try:
                if amt not in (None, "") and float(amt) > 0:
                    vals.append(float(amt))
            except (TypeError, ValueError):
                continue
        if not vals:
            return None
        avg = sum(vals) / len(vals)
        save_dose_price(cid, avg, len(vals), source="invoices")
        return avg
    except Exception as e:
        log.warning(f"  candidate {cid}: invoice-average price skipped ({type(e).__name__}: {str(e)[:100]})")
        return None


# --------------------------------------------------------------------------- the rule for EVERY entry
def sweep(limit: int = 3000, ids=None, family_first: bool = True) -> int:
    """Applies auto_resolve() to every TOPUP / PREV_INCLUSIVE entry that is still open or (unless switched off) SUBMITTED, using
    the patient decrees already stored by the lookups. Entries without a finished lookup are skipped (nothing to compare).
    v10: duplicates / already-served families are closed FIRST (topup_family.sweep), then the remaining entries are evaluated
    with ONE bulk read of everything shared (Ctx). Returns how many were closed. Never raises."""
    if not enabled():
        return 0
    closed = 0
    try:
        if family_first:
            st = fam.sweep(ids=ids)
            closed += st.get("resolved", 0)
        sts = ["DETECTED", "NEEDS_COUNTS", "READY"] + (["SUBMITTED"] if _submitted_too() else [])
        flt = {"status": "in.(" + ",".join(sts) + ")", "lookup_status": "eq.DONE"}
        if ids:                                              # a parallel runner only sweeps its own group
            flt["id"] = "in.(" + ",".join(str(int(i)) for i in ids) + ")"
            rows = sb.select(CAND, select="*", filters=flt, order="id.asc", limit=1000) or []
        else:
            rows = fam._select_all(CAND, "*", flt)           # keyset pages: a response never exceeds the server's row cap
        rows = [c for c in rows if c.get("kind") in TOPUP_LIKE]
        ctx = Ctx.load(rows) if rows else None
        n_ok = 0
        for c in rows:
            if auto_resolve(c, ctx=ctx, trust=True):
                n_ok += 1
        closed += n_ok
        log.info(f"[resolution sweep] {len(rows)} entr(ies) checked, {n_ok} closed - sufficient value already exists after the letter")
    except Exception as e:
        log.warning(f"[resolution sweep] skipped ({type(e).__name__}: {str(e)[:140]})")
    return closed
