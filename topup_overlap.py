#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_overlap.py
==========================================================================
Which of a patient's decrees count as "the previous decree of the same plan" for a top-up entry, including the
OVERLAPPING hormonal / Zoladex protocols, and which of them supply the invoices.

THE PROBLEM
  The same patient can be issued
      - a hormonal-only decree            (protocol 'hormonal')
      - a Zoladex-only decree             (protocol 'zoladex')
      - ONE combined hormonal + Zoladex   (protocol 'hormonal_zoladex')
  Requesting the Zoladex alone when the last decree was the combined one (or the hormonal alone when the last was the
  combined one, or the reverse) used to find "no previous decree", because the DB view decree_topup_lookup_view only
  matches the candidate's own plan/protocol.

THE RULE (pairwise, by components - NOT "all hormonal/zoladex are equal")
      hormonal          <-> hormonal, hormonal_zoladex
      zoladex           <-> zoladex,  hormonal_zoladex
      hormonal_zoladex  <-> hormonal_zoladex, hormonal, zoladex
  A hormonal-only decree and a Zoladex-only decree are NOT interchangeable (different drugs); only the combined decree
  bridges the two.

WHICH DECREES SUPPLY THE INVOICES (cover())
  Only as many of the NEWEST compatible decrees as it takes to cover every component the entry asks for:
      hormonal / zoladex request  -> the single newest decree that contains that component (own or combined)
      combined request            -> the newest decree if it is combined; otherwise the newest hormonal-containing
                                     one plus the newest Zoladex-containing one (a combined request needs both bills)
  Decrees older than that are never used. Every other protocol keeps the old behaviour (newest single decree).

Everything here is read-only against the DB (decree_topup_decree_lookup, decree_topup_lookup_view, topup_regimen_map).
The DB view itself is NOT touched: the extra overlap rows are added on the Python side.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Dict, List, Optional, Set

import supabase_client as sb
import topup_dose_count as T

log = logging.getLogger("topup_overlap")
CAND, LOOK, VIEW = "decree_topup_candidates", "decree_topup_decree_lookup", "decree_topup_lookup_view"

# protocol_key (lower case) -> the components it contains. Add a line here to extend the overlap family, e.g.
#   "tamoxifen_zoladex": {"hormonal", "zoladex"},   "tamoxifen": {"hormonal"},
COMPONENTS: Dict[str, Set[str]] = {
    "hormonal": {"hormonal"},
    "zoladex": {"zoladex"},
    "hormonal_zoladex": {"hormonal", "zoladex"},
    # the other hormonal plans of topup_core_meds_map.json. Delete these three lines to keep them as stand-alone protocols.
    "tamoxifen": {"hormonal"},
    "aromasin": {"hormonal"},
    "tamoxifen_zoladex": {"hormonal", "zoladex"},
}


def _norm(p) -> str:
    return (p or "").strip().lower()


def components(protocol) -> Optional[Set[str]]:
    return COMPONENTS.get(_norm(protocol))


def compatible_protocols(protocol) -> Set[str]:
    """The protocol itself plus every protocol that shares a component with it (just itself outside the family)."""
    p = _norm(protocol)
    if not p:
        return set()
    comp = COMPONENTS.get(p)
    if not comp:
        return {p}
    return {k for k, v in COMPONENTS.items() if v & comp}


def continuous_protocols() -> Set[str]:
    """Continuous regimens: no upper limit on doses, so no 'received / yet to be received' wording is ever added to the
    medical report and no cycle counts are required. Override with TOPUP_CONTINUOUS_PROTOCOLS=a,b,c"""
    raw = os.environ.get("TOPUP_CONTINUOUS_PROTOCOLS")
    if raw is None:
        raw = "hormonal,zoladex,hormonal_zoladex,tamoxifen,aromasin,tamoxifen_zoladex"
    return {_norm(x) for x in raw.split(",") if x.strip()}


def is_continuous(protocol) -> bool:
    return _norm(protocol) in continuous_protocols()


# --------------------------------------------------------------------------- decree -> protocol
_PLAN_PROTO: Optional[Dict[str, str]] = None


def _plan_protocols() -> Dict[str, str]:
    """plan text (normalised) -> protocol, from the same table the dose count uses."""
    global _PLAN_PROTO
    if _PLAN_PROTO is None:
        m: Dict[str, str] = {}
        try:
            for r in sb.select("topup_regimen_map", select="plan_text,topup_protocol", limit=2000) or []:
                if r.get("plan_text") and r.get("topup_protocol"):
                    m[T.plan_key(r["plan_text"])] = _norm(r["topup_protocol"])
        except Exception as e:
            log.warning(f"topup_regimen_map not readable ({type(e).__name__}: {str(e)[:100]}) - overlap matching limited to the DB view")
        _PLAN_PROTO = m
    return _PLAN_PROTO


def _keyword_protocol(desc) -> Optional[str]:
    """Fallback for a decree description that is NOT spelled exactly like a row of topup_regimen_map (the site's text
    drifts: extra words, different spelling). Only the hormonal / Zoladex family is recognised; fulvestrant / CDK4/6 /
    chemotherapy texts mention 'العلاج الهرموني' too and are deliberately left alone. TOPUP_OVERLAP_KEYWORDS=0 disables."""
    if (os.environ.get("TOPUP_OVERLAP_KEYWORDS") or "1").strip().lower() in ("0", "false", "off", "no"):
        return None
    k = T.plan_key(desc)
    # other drug families, and SUPPORTIVE plans (bone protection with zoledronic acid etc.) that only MENTION hormonal
    # therapy to describe the patients ("... للمرضي الذين يتلقوا علاج هرموني ...") - never part of the family
    if not k or any(w in k for w in (
            "فلفسترانت", "فولفسترانت", "cdk", "كيماوي", "كيميائي", "كيميايي", "تاكسان", "انثراسيكلين",
            "عظام", "زيلودرونيك", "زوليدرونيك", "zoledron", "دينوسوماب", "denosumab", "بايفوسفونيت", "مساعد", "تدعيمي", "حمض")):
        return None
    z = "زولاديكس" in k or "zoladex" in k or "جوسيرلين" in k
    tam = "تاموكس" in k
    exe = "اكسيميستان" in k or "اروماسين" in k
    # 'hormonal' only counts when it is the TREATMENT NAMED AT THE START of the plan ("علاج هرموني ...", "هرموني ..."),
    # not a word somewhere in the sentence
    h = k.startswith("علاج هرموني") or k.startswith("هرموني")
    if z and (h or tam):
        return "tamoxifen_zoladex" if tam else "hormonal_zoladex"
    if z:
        return "zoladex"
    if tam:
        return "tamoxifen"
    if exe:
        return "aromasin"
    if h:
        return "hormonal"
    return None


def protocol_of_description(desc) -> Optional[str]:
    if not desc:
        return None
    return _plan_protocols().get(T.plan_key(desc)) or _keyword_protocol(desc)


def _app_protocol(decree: str) -> Optional[str]:
    """A decree created by THIS app: attempt -> case -> plan -> topup_protocol."""
    try:
        a = sb.select("decree_request_attempts", select="case_id", filters={"decree_number": f"eq.{decree}"}, limit=1)
        if not a:
            return None
        cs = sb.select("decree_request_cases", select="treatment_plan_id", filters={"id": f"eq.{a[0]['case_id']}"}, limit=1)
        if not cs or not cs[0].get("treatment_plan_id"):
            return None
        p = sb.select("decree_treatment_plans", select="topup_protocol", filters={"id": f"eq.{cs[0]['treatment_plan_id']}"}, limit=1)
        return _norm(p[0].get("topup_protocol")) if p and p[0].get("topup_protocol") else None
    except Exception:
        return None


# --------------------------------------------------------------------------- lookup rows
def _d(s) -> Optional[date]:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def lookup_rows(c: dict) -> List[dict]:
    """Every decree the patient lookup stored for this entry, with parsed dates."""
    rows = sb.select(LOOK, select="decree_number,decree_date,expiry_date,description,is_open,flag,decree_value,"
                                   "value_left_website,pending_unsubmitted_value,real_value_left,fetched_at",
                     filters={"candidate_id": f"eq.{c['id']}"}, limit=500) or []
    for r in rows:
        r["decree_number"] = str(r["decree_number"])
        r["_date"] = _d(r.get("decree_date"))
        r["_expiry"] = _d(r.get("expiry_date"))
    return rows


def matching_rows(c: dict, only_plan: bool = False) -> List[dict]:
    """The patient's decrees that belong to this entry's plan/protocol: the DB view's matches ('PLAN') PLUS decrees of an
    overlapping protocol ('OVERLAP', see the module doc). Oldest first (undated first). Each row also carries
    '_protocol' and '_kind'."""
    cand_proto = _norm(c.get("protocol_key"))
    compat = compatible_protocols(cand_proto)
    rows = lookup_rows(c)
    try:
        view = {str(r["decree_number"]) for r in (sb.select(
            VIEW, select="decree_number,match_kind",
            filters={"candidate_id": f"eq.{c['id']}", "match_kind": ("eq.PLAN" if only_plan else "not.is.null")},
            limit=500) or [])}
    except Exception as e:
        log.warning(f"lookup view not readable ({type(e).__name__}: {str(e)[:100]}) - only overlap rows are used")
        view = set()
    out = []
    for r in rows:
        proto = protocol_of_description(r.get("description"))
        if r["decree_number"] in view:
            r["_kind"] = "PLAN"
            r["_protocol"] = proto if proto in compat else cand_proto
        elif proto and proto in compat and proto != cand_proto:
            r["_kind"] = "OVERLAP"
            r["_protocol"] = proto
        else:
            continue
        out.append(r)
    out.sort(key=lambda r: (r["_date"] is not None, r["_date"] or date.min, r["decree_number"]))
    return out


def decree_protocols(c: dict, decrees: List[str]) -> Dict[str, str]:
    """decree number -> protocol for the given decrees (lookup description first, then the app's own plan, else the
    entry's own protocol - the view already matched it to this plan)."""
    own = _norm(c.get("protocol_key"))
    known = {r["decree_number"]: r["_protocol"] for r in matching_rows(c)}
    out = {}
    for d in decrees:
        d = str(d)
        out[d] = known.get(d) or _app_protocol(d) or own
    return out


def cover(c: dict, decrees: List[str]) -> List[str]:
    """decrees: oldest -> newest (all previous decrees of the compatible plans). Returns the oldest -> newest subset
    that supplies the invoices (see the module doc). Outside the hormonal/Zoladex family: just the newest decree."""
    if not decrees:
        return []
    need = components(c.get("protocol_key"))
    if not need or len(decrees) == 1:
        return decrees[-1:]
    protos = decree_protocols(c, decrees)
    chosen: List[str] = []
    covered: Set[str] = set()
    for d in reversed(decrees):                                   # newest first
        comp = (components(protos.get(str(d))) or set()) & need
        if comp - covered:
            chosen.append(d)
            covered |= comp
        if covered >= need:
            break
    if not chosen:
        return decrees[-1:]
    log.info(f"  candidate {c.get('id')}: overlap cover for '{c.get('protocol_key')}' -> {list(reversed(chosen))} "
             f"({', '.join(f'{d}={protos.get(str(d))}' for d in reversed(chosen))})")
    return list(reversed(chosen))
