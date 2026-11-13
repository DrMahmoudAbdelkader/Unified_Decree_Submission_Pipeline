#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_family.py   (NEW - v10)
==========================================================================
ONE letter per patient + treatment plan, and no letter left open when the plan was already served.

THE PROBLEM
  A patient can get several administrative letters for the SAME plan (the original request, its resubmission, a second
  request filed by somebody else ...). The plan text of those requests often differs only by the dose count or the
  "(شامل المتابعه والتحاليل)" suffix, e.g.
        «علاج كيماوي تاكسان (دوستاكل) 4 جرعات (شامل المتابعه والتحاليل)»   and   «... 6 جرعات (شامل ...)»
  Each letter became its own line: the work was duplicated, a second request could be created for a plan that was already
  requested / already answered, and every report counted the same patient+plan several times.

THE RULES (all in sweep(); the page applies the very same rules on screen, see decree-topup.js v10)
  FAMILY   = patient + plan_family(plan_text).  plan_family() drops the dose count, any "(شامل ...)" part, digits, diacritics
             and spelling variants, so the 4-dose and the 6-dose wording are ONE family.  Kinds TOPUP / PREV_INCLUSIVE /
             PREV_REPORT are one family together (ID_CARD letters are never touched).
  LEAD     = the member with the NEWEST letter date (committee date of the letter, else the day the system detected it).
  A. SUCCESS  the family was already served by a successful decree:
        - a later request of the same plan (case id after the lead's request) reached «قرار نهائي» (FINAL_APPROVED), or
        - another member of the family was already closed as «sufficient value / resolved» by a decree issued after the lead's
          request date (its closing message carries the decree date).
        ->  the lead AND every open / duplicate sibling are closed as RESOLVED (message FAMILY_OK_PREFIX ...).
        The third success source - a decree on the SMC patient page (also one filed directly on the website) whose left value
        covers a dose - is topup_resolution.py; after it closes the lead it calls propagate() so the siblings follow.
  B. SUPERSEDED  otherwise every OPEN older sibling (DETECTED / NEEDS_COUNTS / READY) is closed as a duplicate:
        status DISMISSED + message DUP_PREFIX ...  ->  it disappears from every work queue, from the pipeline selection
        (the runners skip DISMISSED) and from the reports.  SUBMITTED / RESOLVED / DISMISSED / WAIT_FOR_EXPIRY members are
        never changed (they are history or being tracked).
  C. IN FLIGHT  a request of the same plan that is still pending exists after the lead's request -> NOT closed (it may still
        be rejected) but reported: send_block() tells topup_gate.py / the page that a new request must not be created.

  Reopen override: an entry closed here and re-opened by the operator is stored in decree_topup_dedupe_lock
  (RPC topup_dedupe_unlock, migration 17) and is never grouped or closed again.

LOAD ON THE DATABASE (a hard requirement): the whole sweep is ~6 + (patients / 40) read requests and one PATCH per row that
actually changes - no per-entry queries, no rewriting of unchanged rows. Patients without any OPEN entry are never read.

SWITCHES (env):  TOPUP_FAMILY=0 turns everything off | TOPUP_FAMILY_APPLY=0 = dry run (logs what it would do, writes nothing)
                 TOPUP_FAMILY_IGNORE_DOSE_COUNT=0 = the dose count stays part of the family (4 and 6 doses are then different)
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Dict, Iterable, List, Optional

import supabase_client as sb

log = logging.getLogger("topup_family")

CAND = "decree_topup_candidates"
LOCK = "decree_topup_dedupe_lock"
OPEN_STATUSES = ("DETECTED", "NEEDS_COUNTS", "READY")
FAMILY_KINDS = (None, "", "TOPUP", "PREV_INCLUSIVE", "PREV_REPORT")
AUTO_PREFIX = "تم الحل تلقائياً"                               # topup_resolution.AUTO_PREFIX (duplicated on purpose: no import cycle)
DUP_PREFIX = "مكرر — خطاب أحدث لنفس المريض والخطة"
FAMILY_OK_PREFIX = "مُغلق تلقائياً — حُلّ بقرار/طلب آخر لنفس الخطة"
_COLS = "id,patient_id,source_case_id,kind,status,plan_text,detected_at,evidence_message"


def _flag(name: str, default: str = "1") -> bool:
    return (os.environ.get(name) or default).strip().lower() not in ("0", "false", "off", "no")


def enabled() -> bool:
    return _flag("TOPUP_FAMILY")


def apply_enabled() -> bool:
    return _flag("TOPUP_FAMILY_APPLY")


# --------------------------------------------------------------------------- plan family (MIRRORED in decree-topup.js planFamily())
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def norm_ar(t) -> str:
    s = str(t or "").translate(_AR_DIGITS).lower()
    s = s.replace("\u0640", "")
    s = re.sub("[\u064B-\u065F]", "", s)
    s = re.sub("[أإآٱ]", "ا", s).replace("ى", "ي").replace("ة", "ه")
    return re.sub("[^a-z0-9\u0621-\u064A]+", " ", s).strip()


def plan_family(text) -> str:
    """'علاج كيماوي تاكسان (دوستاكل) 4 جرعات (شامل المتابعه والتحاليل)' -> 'علاج كيماوي تاكسان دوستاكل' (same for 6 doses)."""
    s = str(text or "").translate(_AR_DIGITS)
    s = re.sub(r"\([^)]*شامل[^)]*\)", " ", s)                # "(شامل المتابعه والتحاليل)"
    s = norm_ar(s)
    if _flag("TOPUP_FAMILY_IGNORE_DOSE_COUNT"):
        s = re.sub(r"(?:^|\s)\d+\s*(?:جرع|دور|كورس)[\u0621-\u064A]*", " ", s)          # "4 جرعات" / "6 دورات"
        s = re.sub(r"(?:^|\s)\d+(?=\s|$)", " ", s)                                      # any bare number left
    s = re.sub(r"(?:^|\s)شامل(?:\s.*)?$", " ", s)                                       # "... شامل المتابعه" without brackets
    return re.sub(r"\s+", " ", s).strip()


def same_family(a, b) -> bool:
    fa, fb = plan_family(a), plan_family(b)
    return bool(fa) and fa == fb


def _d(s) -> Optional[date]:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def is_dup_closed(c: dict) -> bool:
    return c.get("status") == "DISMISSED" and str(c.get("evidence_message") or "").startswith(DUP_PREFIX)


def is_family_closed(c: dict) -> bool:
    return c.get("status") == "RESOLVED" and str(c.get("evidence_message") or "").startswith(FAMILY_OK_PREFIX)


def _is_auto_success(c: dict) -> bool:
    return c.get("status") == "RESOLVED" and str(c.get("evidence_message") or "").startswith((AUTO_PREFIX, FAMILY_OK_PREFIX))


def _decree_date_in_message(msg: str) -> Optional[date]:
    """topup_resolution writes 'صدر للمريض القرار N بنفس الخطة بتاريخ dd/mm/yyyy ...' - the decree's issue date."""
    m = re.search(r"بتاريخ\s+(\d{2})/(\d{2})/(\d{4})", str(msg or ""))
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- data (bulk reads only)
def _select_all(table: str, select: str, filters: dict, page: int = 1000) -> list:
    """Keyset pagination on id (PostgREST caps a response at ~1000 rows)."""
    out, last = [], 0
    while True:
        flt = dict(filters)
        flt["id"] = f"gt.{last}"
        rows = sb.select(table, select=select, filters=flt, order="id.asc", limit=page) or []
        out.extend(rows)
        if len(rows) < page:
            return out
        last = int(rows[-1]["id"])


def _kind_ok(c: dict) -> bool:
    return c.get("kind") in FAMILY_KINDS


def _load_members(patient_ids: Optional[Iterable] = None, ids: Optional[Iterable] = None):
    """-> (members_by_patient, open_patient_ids). Only patients that have at least one OPEN family entry are read."""
    flt = {"status": "in.(" + ",".join(OPEN_STATUSES) + ")"}
    if ids:
        flt["id"] = "in.(" + ",".join(str(int(i)) for i in ids) + ")"
        open_rows = sb.select(CAND, select=_COLS + ",kind", filters=flt, limit=1000) or []
    elif patient_ids:
        pids = [int(p) for p in patient_ids if p is not None]
        open_rows = []
        for ch in _chunks(pids, 100):
            open_rows += sb.select(CAND, select=_COLS + ",kind",
                                   filters={**flt, "patient_id": "in.(" + ",".join(map(str, ch)) + ")"}, limit=1000) or []
    else:
        open_rows = _select_all(CAND, _COLS + ",kind", flt)
    open_rows = [c for c in open_rows if _kind_ok(c) and c.get("patient_id") is not None]
    pids = sorted({int(c["patient_id"]) for c in open_rows})
    members: Dict[int, List[dict]] = {}
    for ch in _chunks(pids, 100):
        rows = sb.select(CAND, select=_COLS + ",kind",
                         filters={"patient_id": "in.(" + ",".join(map(str, ch)) + ")"}, limit=1000) or []
        for c in rows:
            if _kind_ok(c):
                members.setdefault(int(c["patient_id"]), []).append(c)
    return members, pids


def _load_locks() -> set:
    try:
        return {int(r["candidate_id"]) for r in (sb.select(LOCK, select="candidate_id", limit=5000) or [])}
    except Exception:
        return set()                                       # migration 17 not applied yet: no locks


def _letter_dates(case_ids: Iterable) -> Dict[int, str]:
    out: Dict[int, str] = {}
    ids = sorted({int(x) for x in case_ids if x})
    for ch in _chunks(ids, 150):
        try:
            for r in sb.select("decree_smc_status_tracking", select="case_id,admin_letter_committee_date",
                               filters={"case_id": "in.(" + ",".join(map(str, ch)) + ")"}, limit=1000) or []:
                if r.get("admin_letter_committee_date"):
                    out[int(r["case_id"])] = str(r["admin_letter_committee_date"])[:10]
        except Exception as e:
            log.debug(f"letter dates not read: {e}")
            break
    return out


def case_kind(row: dict) -> str:
    """APPROVED | INERT (letter / declined / cancelled / closed) | PENDING (waiting to be sent, or sent and under review)."""
    st, att, smc = row.get("case_status"), row.get("attempt_status"), row.get("smc_status_normalized")
    if st == "FINAL_APPROVED" or smc == "Approved":
        return "APPROVED"
    if st in ("FINAL_DECLINED", "ADMIN_LETTER", "CANCELLED", "CLOSED") or att == "ADMIN_LETTER" or smc == "Admin_Letter":
        return "INERT"
    return "PENDING"


def _family_cases(patient_ids: List[int], min_case_id: int) -> Dict[int, List[dict]]:
    """Later requests of the patients that are APPROVED or still PENDING, with their plan wording.
    ONE query per 40 patients, only rows that matter (letters / declined / cancelled are filtered out by the server)."""
    out: Dict[int, List[dict]] = {}
    for ch in _chunks(patient_ids, 40):
        try:
            rows = sb.select("decree_request_case_export",
                             select="case_id,patient_id,created_at,case_status,attempt_status,smc_status_normalized,reception_display_name",
                             filters={"patient_id": "in.(" + ",".join(map(str, ch)) + ")",
                                      "case_status": "not.in.(FINAL_DECLINED,ADMIN_LETTER,CANCELLED,CLOSED)",
                                      "case_id": f"gt.{int(min_case_id)}"},
                             order="created_at.desc", limit=1000) or []
        except Exception as e:
            log.warning(f"later requests not readable ({type(e).__name__}: {str(e)[:100]}) - success / in-flight checks by request skipped")
            return {}
        seen = set()
        for r in rows:
            cid = int(r["case_id"])
            if cid in seen:
                continue                                    # one row per case (latest attempt first)
            seen.add(cid)
            if case_kind(r) != "INERT":
                out.setdefault(int(r["patient_id"]), []).append({**r, "case_id": cid})
    # plan wording of the cases that have none in the export
    need = [c["case_id"] for lst in out.values() for c in lst if not str(c.get("reception_display_name") or "").strip()]
    if need:
        names: Dict[int, str] = {}
        for ch in _chunks(sorted(set(need)), 100):
            try:
                for t in sb.select("decree_smc_status_tracking", select="case_id,treatment_plan_text",
                                   filters={"case_id": "in.(" + ",".join(map(str, ch)) + ")"}, limit=1000) or []:
                    if t.get("treatment_plan_text"):
                        names[int(t["case_id"])] = t["treatment_plan_text"]
            except Exception:
                break
        for lst in out.values():
            for c in lst:
                if not str(c.get("reception_display_name") or "").strip():
                    c["reception_display_name"] = names.get(c["case_id"], "")
    return out


# --------------------------------------------------------------------------- the evaluation of one family
def _sort_key(m: dict, ldates: Dict[int, str]):
    sc = m.get("source_case_id")
    return ((ldates.get(int(sc)) if sc else None) or str(m.get("detected_at") or "")[:10] or "", int(m["id"]))


def evaluate_family(members: List[dict], ldates: Dict[int, str], cases: List[dict], req_dates: Dict[int, date]) -> dict:
    """members: the family (already without locked entries). -> dict(lead, siblings, success, inflight)
    success = None | dict(kind 'CASE'|'SIBLING', ref, text)   inflight = None | case row"""
    ordered = sorted(members, key=lambda m: _sort_key(m, ldates), reverse=True)
    lead, siblings = ordered[0], ordered[1:]
    fam = plan_family(lead.get("plan_text"))
    lead_src = int(lead.get("source_case_id") or 0)
    res = dict(lead=lead, siblings=siblings, success=None, inflight=None)
    own_sources = {int(m["source_case_id"]) for m in members if m.get("source_case_id")}
    for c in sorted(cases, key=lambda x: x["case_id"]):
        if c["case_id"] <= lead_src or c["case_id"] in own_sources:
            continue
        if not same_family(c.get("reception_display_name"), fam):
            continue
        k = case_kind(c)
        if k == "APPROVED" and not res["success"]:
            res["success"] = dict(kind="CASE", ref=c["case_id"],
                                  text=f"الطلب #{c['case_id']} لنفس الخطة وصل إلى «قرار نهائي» بعد هذا الطلب")
        elif k == "PENDING" and not res["inflight"]:
            res["inflight"] = c
    if not res["success"]:
        lead_req = req_dates.get(lead_src)
        for m in siblings:
            if _is_auto_success(m):
                dd = _decree_date_in_message(m.get("evidence_message"))
                if dd and lead_req and dd > lead_req:
                    res["success"] = dict(kind="SIBLING", ref=m["id"],
                                          text=f"الإدخال #{m['id']} لنفس الخطة حُلّ بقرار صدر بتاريخ {dd:%d/%m/%Y} بعد تاريخ هذا الطلب")
                    break
    return res


def _fmt(d) -> str:
    dd = _d(d)
    return dd.strftime("%d/%m/%Y") if dd else "—"


def _write(c: dict, status: str, msg: str, apply: bool) -> bool:
    log.info(f"  [family] entry {c['id']} (patient {c.get('patient_id')}) {c.get('status')} -> {status}: {msg[:110]}")
    if not apply:
        return True
    try:
        body = {"status": status, "evidence_message": msg[:480]}
        if status == "RESOLVED":
            body["evidence_status"] = "NO_PREVIOUS_DECREE"      # same as topup_resolution.auto_resolve
        sb.update(CAND, c["id"], body)
        c["status"], c["evidence_message"] = status, msg[:480]
        return True
    except Exception as e:
        log.warning(f"  [family] entry {c['id']}: not written ({type(e).__name__}: {str(e)[:120]})")
        return False


def sweep(ids: Optional[Iterable] = None, patient_ids: Optional[Iterable] = None) -> dict:
    """Applies rules A / B / C to every family that has an open entry. Returns counters. Never raises."""
    stats = dict(families=0, superseded=0, resolved=0, inflight=0)
    if not enabled():
        return stats
    apply = apply_enabled()
    try:
        members, pids = _load_members(patient_ids=patient_ids, ids=ids)
        if not pids:
            log.info("[family] no open entries")
            return stats
        locks = _load_locks()
        ldates = _letter_dates(m.get("source_case_id") for lst in members.values() for m in lst)
        # group (patient, family) - locked entries and entries without plan wording stand alone
        groups: Dict[tuple, List[dict]] = {}
        for pid, lst in members.items():
            for m in lst:
                fam = plan_family(m.get("plan_text"))
                if not fam or int(m["id"]) in locks:
                    continue
                groups.setdefault((pid, fam), []).append(m)
        work = {k: g for k, g in groups.items() if any(m["status"] in OPEN_STATUSES for m in g)}
        if not work:
            return stats
        stats["families"] = len(work)
        # later requests: only patients that own an open family; the oldest lead's request bounds the query
        src_ids = [int(m["source_case_id"]) for g in work.values() for m in g if m.get("source_case_id")]
        cases_by_patient = _family_cases(sorted({k[0] for k in work}), min(src_ids) if src_ids else 0) if src_ids else {}
        # request dates only for families that have an auto-resolved sibling (needed to compare the decree date)
        need_req = {int(g_lead["source_case_id"]) for g in work.values()
                    for g_lead in [max(g, key=lambda m: _sort_key(m, ldates))]
                    if g_lead.get("source_case_id") and any(_is_auto_success(m) for m in g)}
        req_dates: Dict[int, date] = {}
        if need_req:
            import topup_resolution as tr
            req_dates = tr.request_dates(sorted(need_req))
        for (pid, fam), g in work.items():
            ev = evaluate_family(g, ldates, cases_by_patient.get(pid, []), req_dates)
            lead, sibs, ok = ev["lead"], ev["siblings"], ev["success"]
            if ev["inflight"] and not ok:
                stats["inflight"] += 1
                log.info(f"  [family] patient {pid}: request #{ev['inflight']['case_id']} of the same plan is still pending - "
                         f"no new request must be created for entry {lead['id']}")
            if ok:
                msg = f"{FAMILY_OK_PREFIX}: {ok['text']} — لا حاجة لطلب جديد."
                for m in [lead] + sibs:
                    if m["status"] in OPEN_STATUSES or is_dup_closed(m):
                        if _write(m, "RESOLVED", msg, apply):
                            stats["resolved"] += 1
                continue
            for m in sibs:                                  # B. only OPEN older siblings are closed
                if m["status"] in OPEN_STATUSES:
                    msg = (f"{DUP_PREFIX} — الإدخال #{lead['id']} (خطاب بتاريخ {_fmt(_sort_key(lead, ldates)[0])}) هو الذي يُعمل عليه؛ "
                           f"هذا الخطاب أقدم لنفس الخطة ولا يحتاج طلباً مستقلاً.")
                    if _write(m, "DISMISSED", msg, apply):
                        stats["superseded"] += 1
        log.info(f"[family] {stats['families']} family(ies) with open entries: {stats['superseded']} duplicate(s) closed, "
                 f"{stats['resolved']} closed as already served, {stats['inflight']} with a request still in flight"
                 f"{'' if apply else '  (DRY RUN - nothing written)'}")
    except Exception as e:
        log.warning(f"[family] sweep skipped ({type(e).__name__}: {str(e)[:160]})")
    return stats


# --------------------------------------------------------------------------- hooks used by the other modules
def propagate(c: dict) -> int:
    """Called right after topup_resolution closed `c` as RESOLVED by a decree: the siblings of the same patient + plan family
    (open ones and the ones closed as duplicates) are closed as RESOLVED too. Returns how many changed. Never raises."""
    if not enabled() or not c.get("patient_id") or not _kind_ok(c):
        return 0
    try:
        fam = plan_family(c.get("plan_text"))
        if not fam:
            return 0
        if int(c["id"]) in _load_locks():
            return 0
        rows = sb.select(CAND, select=_COLS + ",kind", filters={"patient_id": f"eq.{int(c['patient_id'])}"}, limit=1000) or []
        locks = _load_locks()
        done = 0
        msg = (f"{FAMILY_OK_PREFIX}: الإدخال #{c['id']} لنفس المريض والخطة حُلّ بقرار صدر بعد الطلب"
               f" — لا حاجة لطلب جديد.")
        for m in rows:
            if int(m["id"]) == int(c["id"]) or not _kind_ok(m) or int(m["id"]) in locks:
                continue
            if plan_family(m.get("plan_text")) != fam:
                continue
            if m["status"] in OPEN_STATUSES or is_dup_closed(m):
                if _write(m, "RESOLVED", msg, apply_enabled()):
                    done += 1
        return done
    except Exception as e:
        log.warning(f"[family] propagate skipped ({type(e).__name__}: {str(e)[:120]})")
        return 0


def guard(rows: List[dict]) -> List[dict]:
    """For a batch of entries about to be processed (evidence run): applies the family rules to THEIR patients only (one bulk
    pass) and returns the entries that are still open. Entries closed as duplicates / already served are dropped."""
    if not enabled() or not rows:
        return rows
    pids = sorted({int(c["patient_id"]) for c in rows if c.get("patient_id") is not None and _kind_ok(c)})
    if pids:
        sweep(patient_ids=pids)
    ids = [int(c["id"]) for c in rows]
    keep = []
    try:
        fresh = {}
        for ch in _chunks(ids, 150):
            for r in sb.select(CAND, select="id,status", filters={"id": "in.(" + ",".join(map(str, ch)) + ")"}, limit=1000) or []:
                fresh[int(r["id"])] = r["status"]
        for c in rows:
            if fresh.get(int(c["id"]), c.get("status")) in ("DISMISSED", "RESOLVED") and c.get("status") not in ("DISMISSED", "RESOLVED"):
                log.info(f"  [family] entry {c['id']} was closed by the family rules - skipped")
                continue
            keep.append(c)
    except Exception:
        return rows
    return keep


def send_block(c: dict) -> Optional[str]:
    """Message when a NEW request for this entry must not be created / sent (None = free to go). For topup_gate.py:
        from topup_family import send_block
        msg = send_block(candidate_row);  if msg: refuse(msg)
    Blocks when a request of the same patient + plan family, created after the entry's own request, is still pending or was
    already approved. One cheap read per call."""
    try:
        if not enabled() or not c.get("patient_id") or not c.get("source_case_id"):
            return None
        fam = plan_family(c.get("plan_text"))
        if not fam:
            return None
        src = int(c["source_case_id"])
        cases = _family_cases([int(c["patient_id"])], src).get(int(c["patient_id"]), [])
        own = {src}
        for x in sorted(cases, key=lambda z: z["case_id"]):
            if x["case_id"] in own or not same_family(x.get("reception_display_name"), fam):
                continue
            if case_kind(x) == "APPROVED":
                return f"طلب #{x['case_id']} لنفس المريض والخطة وصل إلى «قرار نهائي» بعد هذا الطلب — لا حاجة لطلب جديد"
            if case_kind(x) == "PENDING":
                return f"يوجد طلب #{x['case_id']} لنفس المريض والخطة ما زال قيد الإرسال/المراجعة — لا يُنشأ طلب مكرر"
    except Exception as e:
        log.debug(f"send_block skipped: {e}")
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    only = [int(x) for x in re.findall(r"\d+", os.environ.get("CANDIDATE_IDS") or "")]
    print(sweep(ids=only or None))
