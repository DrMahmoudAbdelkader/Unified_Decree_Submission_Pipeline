#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_dose_calc.py - connects the pure counting engine (topup_dose_count.py) to the pipeline.

  run(c, smc, h)         called at the end of topup_evidence_fetch.process(). Reads ALL the patient's previous decrees of the
                         same plan chain, the item rows of their invoices (kept in decree_topup_item_rows) and their unbilled
                         orders, counts the doses and stores the result in decree_topup_dose_calc. The numbers are also written
                         to the entry's received/remaining boxes (topup_set_counts) UNLESS a person typed/edited them.
  refresh_unbilled(cid)  DB only: recount with stored invoice rows + CURRENT unbilled orders (for topup_gate before release).
  hold_message(...)      text of the review hold (blocking flags nobody reviewed), or None.
  supposed_for(...)      "doses supposed to have been received" for the medical-report sentence.

The map is read from the DB tables of migration 11 (a plan added with topup_dose_set_plan() works without any deploy);
topup_core_meds_map.json in the repo is only a fallback. Set TOPUP_DOSE_CALC=0 to switch the whole thing off.
"""
from __future__ import annotations
import hashlib
import logging
import os
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

import supabase_client as sb
import topup_dose_count as T

log = logging.getLogger("topup_dose_calc")
CAND, CALC, ROWS, LOOK = "decree_topup_candidates", "decree_topup_dose_calc", "decree_topup_item_rows", "decree_topup_decree_lookup"
TOPUP_LIKE = (None, "", "TOPUP", "PREV_INCLUSIVE")
MAP_JSON = os.environ.get("TOPUP_MAP_JSON") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "topup_core_meds_map.json")
_MAP = None


def enabled() -> bool:
    return (os.environ.get("TOPUP_DOSE_CALC") or "1").strip() not in ("0", "false", "off", "no")


def load_map(force: bool = False):
    """-> (map dict, plan index, matcher)"""
    global _MAP
    if _MAP and not force:
        return _MAP
    try:
        keys = sb.select("topup_med_keys", select="med_key,unified_names,alias_regex", limit=1000)
        dic = sb.select("topup_med_dictionary", select="website_name,med_key", limit=2000)
        plans = sb.select("topup_regimen_map", select="plan_text,excel_index,topup_protocol,counts_doses,no_calc_reason,doses_per_decree,review_note", limit=2000)
        core = sb.select("topup_regimen_core_meds", select="plan_text,med_key,excel_name,qty_per_dose,role", limit=5000)
        if not keys or not plans:
            raise RuntimeError("map tables are empty (run migration 11)")
        by_plan = {}
        for r in core:
            by_plan.setdefault(r["plan_text"], []).append(dict(excel_name=r.get("excel_name") or r["med_key"], med_key=r["med_key"],
                                                                 qty_per_dose=r.get("qty_per_dose"), role=r["role"]))
        mp = dict(keys={k["med_key"]: dict(regex=k.get("alias_regex") or "", unified=k.get("unified_names") or "") for k in keys},
                  exact={T._norm(d["website_name"]): d["med_key"] for d in dic},
                  plans=[dict(plan_text=p["plan_text"], excel_index=p.get("excel_index"), protocol=p.get("topup_protocol"),
                              counts_doses=bool(p.get("counts_doses")),
                              doses_per_decree=float(p["doses_per_decree"]) if p.get("doses_per_decree") is not None else 0,
                              core=by_plan.get(p["plan_text"], [])) for p in plans])
        log.info(f"dose map loaded from the database: {len(mp['plans'])} plan(s), {len(mp['keys'])} medication key(s)")
    except Exception as e:
        log.warning(f"dose map tables not readable ({type(e).__name__}: {str(e)[:120]}) - using {os.path.basename(MAP_JSON)}")
        mp = T.load_map_json(MAP_JSON)
    _MAP = (mp, T.index_plans(mp), T.Matcher(mp))
    return _MAP


def _to_date(s) -> Optional[date]:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _pending(decree: str) -> List[dict]:
    import topup_manual_bill as mb
    return [dict(date=_to_date(o.get("date")), value=o.get("value")) for o in mb._pending_orders(decree)]


def _lookup_info(cid: int, decrees: List[str]) -> dict:
    if not decrees:
        return {}
    try:
        rows = sb.select(LOOK, select="decree_number,decree_date,description",
                         filters={"candidate_id": f"eq.{cid}", "decree_number": "in.(" + ",".join(decrees) + ")"}, limit=1000)
        return {str(r["decree_number"]): r for r in rows}
    except Exception as e:
        log.warning(f"  decree lookup rows not readable: {e}")
        return {}


def _app_plan_text(decree: str) -> str:
    """Decree created by THIS app: attempt -> case -> plan text."""
    try:
        a = sb.select("decree_request_attempts", select="case_id", filters={"decree_number": f"eq.{decree}"}, limit=1)
        if not a:
            return ""
        c = sb.select("decree_request_cases", select="treatment_plan_id", filters={"id": f"eq.{a[0]['case_id']}"}, limit=1)
        if not c or not c[0].get("treatment_plan_id"):
            return ""
        p = sb.select("decree_treatment_plans", select="website_submission_treatment_plan", filters={"id": f"eq.{c[0]['treatment_plan_id']}"}, limit=1)
        return (p[0].get("website_submission_treatment_plan") or "") if p else ""
    except Exception:
        return ""


def _stored_rows(decree: str) -> Tuple[List[str], int]:
    got = sb.select(ROWS, select="receipt_id,rows", filters={"decree_number": f"eq.{decree}"}, limit=1000)
    return [r for g in got for r in (g.get("rows") or [])], len(got)


def _rows_for_decree(decree: str, smc, h) -> Tuple[List[str], int]:
    """With an SMC session: list the decree's invoices and read each one's item rows (stored in decree_topup_item_rows).
    Every invoice counts whatever its status (the dispensing happened)."""
    if smc is None or h is None:
        return _stored_rows(decree)
    rows, n = [], 0
    for x in h.list_receipts(smc, decree):
        rid = x["receipt_id"]
        try:
            rr = h.rows_for_receipt(smc, rid)
            sb.upsert(ROWS, [dict(receipt_id=rid, decree_number=decree, rows=rr, fetched_at=datetime.now(timezone.utc).isoformat())], on_conflict="receipt_id")
        except Exception as e:
            log.warning(f"  invoice {rid}: item rows could not be read ({type(e).__name__}: {str(e)[:100]}) - using the stored copy")
            old = sb.select(ROWS, select="rows", filters={"receipt_id": f"eq.{rid}"}, limit=1)
            rr = (old[0].get("rows") if old else None) or []
        rows.extend(rr); n += 1
    return rows, n


def _sig(blocking: List[str]) -> str:
    return hashlib.md5("|".join(blocking).encode("utf-8")).hexdigest()[:12]


def _current_counts(cid: int) -> Tuple[Optional[int], Optional[int]]:
    r = sb.select("decree_topup_overview", select="id,cycles_received,cycles_remaining", filters={"id": f"eq.{cid}"}, limit=1)
    return (r[0].get("cycles_received"), r[0].get("cycles_remaining")) if r else (None, None)


def _store_and_apply(c: dict, res: dict) -> dict:
    cid = c["id"]
    if not res.get("counting"):                       # supportive / not-enabled plans only: nothing to calculate or write
        return dict(applied=False, manual=False, skipped=True)
    prev = sb.select(CALC, select="auto_received,auto_remaining,blocking_sig,reviewed_at", filters={"candidate_id": f"eq.{cid}"}, limit=1)
    prev = prev[0] if prev else None
    cur_r, cur_m = _current_counts(cid)
    new_r, new_m = int(round(res["received"])), int(round(res["remaining"]))
    # a person edited the boxes when they differ from what we last wrote (or were typed before this feature existed)
    manual = cur_r is not None and (prev is None or (cur_r, cur_m) != (prev.get("auto_received"), prev.get("auto_remaining")))
    sig = _sig(res["blocking"])
    row = dict(candidate_id=cid, protocol_key=c.get("protocol_key"), received=res["received"], supposed=res["supposed"],
               remaining=res["remaining"], supposed_known=res["supposed_known"], auto_received=new_r, auto_remaining=new_m,
               blocking=res["blocking"], info=res["info"], blocking_sig=sig, chain=T.to_jsonable(res["per_decree"]),
               calc_at=datetime.now(timezone.utc).isoformat(),
               reviewed_at=(prev.get("reviewed_at") if prev and prev.get("blocking_sig") == sig else None))
    sb.upsert(CALC, [row], on_conflict="candidate_id")
    applied = False
    if res["supposed_known"] and not manual and (cur_r, cur_m) != (new_r, new_m):
        try:
            sb.rpc("topup_set_counts", {"p_id": cid, "p_received": new_r, "p_remaining": new_m}); applied = True
        except Exception as e:
            log.warning(f"  candidate {cid}: could not write the counts ({str(e)[:120]})")
    return dict(applied=applied, manual=manual)


def run(c: dict, smc, h) -> Optional[dict]:
    """Called by topup_evidence_fetch.process(). Never raises: a dose-count problem must not break the evidence run."""
    try:
        if not enabled() or not c.get("protocol_key") or c.get("kind") not in TOPUP_LIKE:
            return None
        mp, idx, mt = load_map()
        decrees = h.merged_previous(c, smc)
        if not decrees:
            return None
        look = _lookup_info(c["id"], decrees)
        chain = []
        for d in decrees:
            plan_text = (look.get(d) or {}).get("description") or _app_plan_text(d)
            rows, n = _rows_for_decree(d, smc, h)
            chain.append(dict(decree_number=d, decree_date=(look.get(d) or {}).get("decree_date"), plan_text=plan_text,
                              rows=rows, pending=_pending(d), invoices=n))
        res = T.compute_chain(chain, idx, mt)
        out = _store_and_apply(c, res)
        log.info(f"  [dose-calc] candidate {c['id']}: received {res['received']:g} of {res['supposed']:g} authorised, "
                 f"remaining {res['remaining']:g} over {len(chain)} decree(s)"
                 f"{' (no counting plan - nothing written)' if out.get('skipped') else ' (counts written)' if out['applied'] else ' (manual counts kept)' if out['manual'] else ''}")
        for d in res["per_decree"]:
            log.info(f"      decree {d['decree_number']}: authorised {d['authorised']} | doses {d['doses']} | "
                     f"invoice dates {[x.strftime('%d/%m/%Y') for x in d['invoice_dates']]} | unbilled {len(d['unbilled'])}")
        for b in res["blocking"]:
            log.warning(f"      [dose-calc] REVIEW NEEDED: {b}")
        return res
    except Exception as e:
        log.warning(f"  [dose-calc] candidate {c.get('id')}: skipped ({type(e).__name__}: {str(e)[:160]})")
        return None


def refresh_unbilled(cid: int) -> bool:
    """DB only. True when the numbers written to the entry changed."""
    try:
        if not enabled():
            return False
        old = sb.select(CALC, select="chain", filters={"candidate_id": f"eq.{cid}"}, limit=1)
        if not old:
            return False
        c = sb.select(CAND, select="id,protocol_key,kind", filters={"id": f"eq.{cid}"}, limit=1)
        if not c:
            return False
        mp, idx, mt = load_map()
        chain = []
        for d in old[0].get("chain") or []:
            rows, n = _stored_rows(d["decree_number"])
            chain.append(dict(decree_number=d["decree_number"], decree_date=d.get("decree_date"), plan_text=d.get("plan_text"),
                              rows=rows, pending=_pending(d["decree_number"]), invoices=n))
        before = _current_counts(cid)
        _store_and_apply(c[0], T.compute_chain(chain, idx, mt))
        return _current_counts(cid) != before
    except Exception as e:
        log.warning(f"[dose-calc] refresh skipped for candidate {cid}: {type(e).__name__}: {str(e)[:140]}")
        return False


def _calc_row(cid: int) -> Optional[dict]:
    try:
        r = sb.select(CALC, select="auto_received,auto_remaining,supposed,blocking,reviewed_at", filters={"candidate_id": f"eq.{cid}"}, limit=1)
        return r[0] if r else None
    except Exception:
        return None


def hold_message(cid: int, cur_received, cur_remaining) -> Optional[str]:
    """Blocking flags that nobody has reviewed. Editing the numbers or pressing «راجعت العدد» on the page releases it."""
    dc = _calc_row(cid)
    if not dc or not dc.get("blocking") or dc.get("reviewed_at"):
        return None
    if (cur_received, cur_remaining) != (dc.get("auto_received"), dc.get("auto_remaining")):
        return None                                           # a person changed the numbers = reviewed
    return ("عدد الجرعات المحسوب تلقائياً يحتاج مراجعة: " + " ؛ ".join(dc["blocking"]) +
            " — افتح الإدخال في صفحة «متابعة الخطابات الإداريه» وعدّل الأرقام أو اضغط «راجعت العدد».")


def supposed_for(cid: int, cur_received, cur_remaining) -> Optional[float]:
    """Doses the patient is supposed to have received (for the report sentence)."""
    dc = _calc_row(cid)
    if dc and dc.get("supposed") is not None and (cur_received, cur_remaining) == (dc.get("auto_received"), dc.get("auto_remaining")):
        return float(dc["supposed"])
    if cur_received is not None and cur_remaining is not None:
        return float(cur_received) + float(cur_remaining)
    return None
