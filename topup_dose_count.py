#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_dose_count.py (v3) - pure functions (no DB, no network) that count the DOSES of a regimen's core medication.

 * One decree = one PLAN (its website description). The map says, per plan, how many doses it authorises and which core
   medications DRIVE the dose count ("anchors"). A dose = one distinct date on which any anchor was dispensed.
 * Dates come from the invoice item rows  `value | item | qty | unit | date | notes` : the date field is the first dispensing
   date and every date written in the notes (d/m, dd-mm, d-m-yy, dd/mm/yyyy ...) is another.
 * Unbilled orders saved after a decree (pending_decree_orders) are doses too: one order = one dose on its date.
 * Chain: received = all doses under ALL decrees of the regimen, supposed = sum of the doses those decrees authorised,
         remaining = max(supposed - received, 0).
 * Quantity never creates a dose; qty / qty_per_dose is only a cross-check (INFO flag).
 BLOCKING flags (request held until a person reviews): an unbilled dispensing on the same date as an invoiced dose, received > authorised, a decree whose plan is not in the map,
 no dose found at all. INFO flags are only displayed.
"""
from __future__ import annotations
import datetime as dt
import json
import re
from typing import Dict, Iterable, List, Optional, Tuple

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫", "01234567890123456789.")
_DATE = re.compile(r"(?<![\d.])(\d{1,2})([/\-])(\d{1,2})(?:\2(\d{2,4}))?(?!\d)")
MIN_GAP_DAYS = 14

# ---- Dosing-pattern rules (e.g. taxane: 3-weekly x4  OR  weekly x9) ---------------------------------------------------------
# Rules are keyed by the plan's protocol. They can be overridden from the DB table topup_dose_patterns (migration 13) or from the
# "patterns" key of the JSON map. A pattern with doses=None keeps the plan's own doses_per_decree.
#  gap_min/gap_max : an ordinary gap (days) between two consecutive doses of that pattern
#  max_multiple    : a held-off dose makes a gap of 2 x interval (1 missed dose => multiple 2). 1 = no tolerance
#  default         : pattern used when the dates cannot decide (a single dose, or a tie)
#  undecided       : "default" (use default + INFO) | "block" (hold the request for review)
DEFAULT_PATTERN_RULES = {
    "taxane": dict(
        patterns=[dict(name="3-weekly", label_ar="كل 3 أسابيع", doses=None, gap_min=18, gap_max=25),
                  dict(name="weekly", label_ar="أسبوعي", doses=9, gap_min=4, gap_max=10)],
        max_multiple=2, default="3-weekly", undecided="default"),
}


def classify_pattern(dates: List[dt.date], rule: dict) -> dict:
    """Vote on the cadence of a decree's dose dates. Each gap votes for the pattern it fits at multiple 1 (ordinary gap); a gap that
    fits no pattern as-is may still vote at a multiple 2..max_multiple (a dose was held off). Gaps that fit several patterns equally
    well, or none, don't vote. -> dict(pattern, votes, gaps, decided, reason)"""
    pats = rule["patterns"]
    ds = sorted(set(d for d in dates if d))
    gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
    votes = {p["name"]: 0 for p in pats}
    for g in gaps:
        for m in range(1, int(rule.get("max_multiple") or 1) + 1):
            hit = [p["name"] for p in pats if p["gap_min"] * m <= g <= p["gap_max"] * m]
            if len(hit) == 1:
                votes[hit[0]] += 1; break
            if len(hit) > 1:
                break
    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    top, second = ranked[0], (ranked[1] if len(ranked) > 1 else (None, 0))
    if top[1] > second[1]:
        return dict(pattern=top[0], votes=votes, gaps=gaps, decided=True, reason="")
    why = "جرعة واحدة فقط أو لا فجوات" if not gaps else "الفجوات بين الجرعات لا ترجّح نمطًا واحدًا"
    return dict(pattern=rule.get("default"), votes=votes, gaps=gaps, decided=False, reason=why)


def widen_for_count(plan: dict, rule: dict, pat: dict, n_doses: int) -> dict:
    """Undecided cadence (e.g. dates unreadable) but more doses were given than the default pattern authorises: the default
    cannot be the right one, so take the smallest pattern whose authorised doses can hold them."""
    if pat["decided"]:
        return pat
    cur = pattern_authorised(plan, rule, pat["pattern"])
    if n_doses <= cur:
        return pat
    fits = sorted(((pattern_authorised(plan, rule, p["name"]), p["name"]) for p in rule["patterns"]
                   if pattern_authorised(plan, rule, p["name"]) >= n_doses))
    if fits:
        return dict(pat, pattern=fits[0][1], reason=f"{n_doses} جرعات أكثر من النمط الافتراضي")
    return pat


def pattern_authorised(plan: dict, rule: dict, pattern: str) -> float:
    for p in rule["patterns"]:
        if p["name"] == pattern:
            return float(p["doses"]) if p.get("doses") is not None else float(plan["doses_per_decree"] or 0)
    return float(plan["doses_per_decree"] or 0)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9\u0600-\u06FF]+", "", (s or "").lower())


def plan_key(s: str) -> str:
    """Plan descriptions compared without tatweel/diacritics, alef/ya/ta-marbuta variants and spacing."""
    s = (s or "").lower().replace("\u0640", "")
    s = re.sub(r"[\u064B-\u065F]", "", s)
    s = s.translate(str.maketrans("أإآٱىةؤئ", "اااايهوي"))
    s = re.sub(r"[^a-z0-9\u0621-\u064A]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _mk(d, m, y) -> Optional[dt.date]:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def parse_date_field(s: str) -> Optional[dt.date]:
    m = re.fullmatch(r"\s*(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\s*", (s or "").translate(ARABIC_DIGITS))
    if not m:
        return None
    y = int(m[3]); y += 2000 if y < 100 else 0
    return _mk(int(m[1]), int(m[2]), y)


def note_dates(note: str, base: dt.date) -> List[dt.date]:
    out = []
    for m in _DATE.finditer((note or "").translate(ARABIC_DIGITS)):
        y = int(m[4]) if m[4] else base.year
        y += 2000 if y < 100 else 0
        d = _mk(int(m[1]), int(m[3]), y)
        if d:
            out.append(d)
    return out


def parse_rows(raw_rows: Iterable[str]) -> List[dict]:
    items = []
    for r in raw_rows:
        c = [x.strip() for x in str(r).split(" | ")]
        if len(c) < 6:
            continue
        base = parse_date_field(c[4])
        try:
            qty = float(c[2].translate(ARABIC_DIGITS))
        except ValueError:
            qty = None
        try:
            value = float(c[0].translate(ARABIC_DIGITS).replace(",", "").replace("٬", ""))
        except ValueError:
            value = None
        if not base and not re.search(r"\d", c[4]):
            continue                                   # header / total rows
        items.append(dict(name=c[1], qty=qty, value=value, unit=c[3], date_field=c[4], date=base, note=c[5],
                          note_dates=note_dates(c[5], base) if base else []))
    return items


class Matcher:
    """Invoice item name -> medication key: 1) exact website name (dictionary), 2) spelling patterns, longest first."""
    def __init__(self, mp: dict):
        self.exact = dict(mp.get("exact", {}))
        self.pats = sorted(((k, re.compile(v["regex"], re.I)) for k, v in mp["keys"].items() if v.get("regex")),
                           key=lambda kv: -len(kv[1].pattern))

    def key_of(self, name: str) -> Optional[str]:
        k = self.exact.get(_norm(name))
        if k:
            return k
        for k, p in self.pats:
            if p.search(name or ""):
                return k
        return None


def load_map_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def index_plans(mp: dict) -> Dict[str, dict]:
    return {plan_key(p["plan_text"]): p for p in mp["plans"]}


def regimen_for_plan(idx: Dict[str, dict], plan_text: str) -> Tuple[str, Optional[dict]]:
    """('ok', plan) | ('not_counting', plan) | ('unknown', None)"""
    p = idx.get(plan_key(plan_text))
    if not p:
        return "unknown", None
    return ("ok" if p["counts_doses"] else "not_counting"), p


def count_plan(raw_rows: Iterable[str], plan: dict, matcher: Matcher, min_gap: int = MIN_GAP_DAYS) -> dict:
    anchors = [c["med_key"] for c in plan["core"] if c["role"] == "anchor"]
    # Hormonal + Zoladex plans: the hormonal tablet (monthly) drives the doses; Zoladex (every 3 months) is a companion and must
    # NEVER add dates of its own, even if a map row marks it as an anchor.
    if str(plan.get("protocol") or "").lower().endswith("_zoladex"):
        anchors = [a for a in anchors if a != "goserelin"] or anchors
    allowed = {c["med_key"] for c in plan["core"]}
    total_value, value_rows = 0.0, 0
    qpd = {c["med_key"]: c.get("qty_per_dose") for c in plan["core"]}
    per: Dict[str, dict] = {}
    for it in parse_rows(raw_rows):
        if it["date"] and it.get("value") is not None:        # every dispensed line of the invoice, core or not
            total_value += it["value"]; value_rows += 1
        k = matcher.key_of(it["name"])
        if not k or k not in allowed:                  # non-core medications are ignored completely
            continue
        d = per.setdefault(k, dict(dates=set(), qty=0.0, rows=0, unread=[], names=set()))
        d["names"].add(it["name"])
        if it["date"]:
            d["dates"].add(it["date"]); d["dates"].update(it["note_dates"])
        d["qty"] += it["qty"] or 0
        d["rows"] += 1
        if it["note"] and re.search(r"\d", it["note"].translate(ARABIC_DIGITS)) and not it["note_dates"]:
            d["unread"].append(it["note"])
    meds, info = {}, []
    for k, d in per.items():
        dates = sorted(d["dates"])
        o = dict(doses=len(dates), dates=dates, qty=d["qty"], qty_per_dose=qpd.get(k), check="", names=sorted(d["names"]))
        q = qpd.get(k)
        if q and float(q).is_integer() and k in anchors:
            exp = d["qty"] / q
            o["check"] = "ok" if abs(exp - len(dates)) < 1e-9 else f"الكمية/{q:g} = {exp:g} لكن عدد التواريخ {len(dates)}"
            if o["check"] != "ok":
                info.append(f"{k}: {o['check']}")
        if d["unread"]:
            info.append(f"{k}: ملاحظة بها أرقام بلا تاريخ مقروء: {d['unread']}")
        meds[k] = o
    dose_dates = sorted(set().union(*[set(meds[k]["dates"]) for k in anchors if k in meds])) if any(k in meds for k in anchors) else []
    qty_ok = any(meds[k]["check"] == "ok" for k in anchors if k in meds) and all(meds[k]["check"] in ("", "ok") for k in anchors if k in meds)
    for a, b in zip(dose_dates, dose_dates[1:]):
        if (b - a).days < min_gap and not qty_ok:
            info.append(f"تاريخا جرعتين بينهما {(b - a).days} يوم فقط: {a:%d/%m/%Y} و {b:%d/%m/%Y}")
    return dict(doses=len(dose_dates), dose_dates=dose_dates, meds=meds, info=info, anchors=anchors, total_value=total_value, value_rows=value_rows)


_PROTO_ALIAS = {"taxanes": "taxane", "taxan": "taxane", "taxanes_": "taxane"}
_TAXANE_MEDS = {"docetaxel", "paclitaxel"}


def pattern_rule_for(plan: dict, mp_rules: Optional[dict]) -> Optional[dict]:
    """Rule of a plan: by its protocol key (plural/alias tolerant: 'taxanes' == 'taxane'), else - when the protocol key is
    missing or different - by the medications that drive it (docetaxel / paclitaxel => taxane rule)."""
    rules = dict(DEFAULT_PATTERN_RULES)
    if mp_rules:
        rules.update(mp_rules)
    k = (plan.get("protocol") or "").strip().lower()
    k = _PROTO_ALIAS.get(k, k)
    if k in rules:
        return rules[k]
    if k.endswith("s") and k[:-1] in rules:
        return rules[k[:-1]]
    anchors = {c["med_key"] for c in plan.get("core", []) if c.get("role") == "anchor"}
    if anchors & _TAXANE_MEDS and "taxane" in rules:
        return rules["taxane"]
    return None


def compute_chain(decrees: List[dict], idx: Dict[str, dict], matcher: Matcher, pattern_rules: Optional[dict] = None) -> dict:
    """decrees: oldest -> newest, each {'decree_number','decree_date','plan_text','rows':[...],'pending':[{'date','value'}],'invoices':int}"""
    per, blocking, info = [], [], []
    seen: Dict[dt.date, str] = {}
    received = supposed = 0.0
    supposed_known = True
    counting = 0
    inv_value, inv_doses = 0.0, 0
    patterns_used = []
    for d in decrees:
        status, plan = regimen_for_plan(idx, d.get("plan_text") or "")
        row = dict(decree_number=d["decree_number"], decree_date=d.get("decree_date"), plan_text=d.get("plan_text"),
                   invoices=d.get("invoices", 0), authorised=None, anchors=[], invoice_dates=[], unbilled=[], doses=0, info=[])
        if status == "unknown":
            supposed_known = False
            blocking.append(f"القرار {d['decree_number']}: نص الخطة غير موجود في خريطة الجرعات"
                            f"{' (' + d['plan_text'][:60] + ')' if d.get('plan_text') else ' (لم يُقرأ نص الخطة)'} — لا يمكن معرفة عدد الجرعات المعتمدة")
            per.append(row); continue
        if status == "not_counting":
            info.append(f"القرار {d['decree_number']}: الخطة غير مفعّلة لحساب الجرعات — تم تجاهله")
            per.append(row); continue
        counting += 1
        rule = pattern_rule_for(plan, pattern_rules)
        # a protocol with several valid cadences: the 2-doses-apart INFO flag must not fire for the shortest ordinary gap
        min_gap = min([p["gap_min"] for p in rule["patterns"]] + [MIN_GAP_DAYS]) if rule else MIN_GAP_DAYS
        res = count_plan(d.get("rows") or [], plan, matcher, min_gap=min_gap)
        authorised = float(plan["doses_per_decree"] or 0)
        pat = None
        if rule:
            # cadence is read from ALL dose dates of this decree (invoiced + unbilled); held-off doses are tolerated
            pat_dates = list(res["dose_dates"]) + [o["date"] for o in (d.get("pending") or []) if o.get("date")]
            pat = widen_for_count(plan, rule, classify_pattern(pat_dates, rule), len(set(pat_dates)))
            if not pat["decided"] and rule.get("undecided") == "block":
                blocking.append(f"القرار {d['decree_number']}: تعذّر تحديد نمط الجرعات (أسبوعي/كل 3 أسابيع): {pat['reason']} — راجع العدد المعتمد")
            authorised = pattern_authorised(plan, rule, pat["pattern"])
            lbl = next((p.get("label_ar") or p["name"] for p in rule["patterns"] if p["name"] == pat["pattern"]), pat["pattern"])
            msg = (f"القرار {d['decree_number']}: نمط الجرعات {lbl} ({pat['pattern']}) — المعتمد {authorised:g} جرعة"
                   + ("" if pat["decided"] else f" (افتراضي: {pat['reason']})"))
            info.append(msg)
        row.update(authorised=authorised, anchors=res["anchors"], pattern=(pat or {}).get("pattern"),
                   pattern_decided=(pat or {}).get("decided"), gaps=(pat or {}).get("gaps"))
        supposed += authorised
        mine = []
        for day in res["dose_dates"]:
            if day in seen:
                info.append(f"التاريخ {day:%d/%m/%Y} ظاهر في القرارين {seen[day]} و {d['decree_number']} — احتُسب مرة واحدة")
                continue
            seen[day] = d["decree_number"]; mine.append(day)
        row["invoice_dates"] = mine
        row["invoice_value"] = round(res["total_value"], 2)
        inv_value += res["total_value"]; inv_doses += len(mine)
        if pat:
            patterns_used.append(pat["pattern"])
        row["info"] = res["info"]
        info.extend(f"القرار {d['decree_number']}: {x}" for x in res["info"])
        if not d.get("invoices"):
            info.append(f"القرار {d['decree_number']}: لا توجد فواتير على SMC")
        for o in sorted(d.get("pending") or [], key=lambda x: (x["date"] is None, x["date"] or dt.date.min)):
            row["unbilled"].append(dict(date=o["date"], value=o.get("value")))
        row["doses"] = len(mine) + len(row["unbilled"])
        received += row["doses"]
        per.append(row)
    # An unbilled dispensing dated the SAME day as a dose that is already on an invoice (of any decree in the chain) is most
    # likely the same dose entered twice -> BLOCKING: the request is held until a person reviews it. It is still counted.
    invoiced_days = set(seen)
    for row in per:
        for u in row["unbilled"]:
            if u["date"] and u["date"] in invoiced_days:
                blocking.append(f"القرار {row['decree_number']}: صرف غير مفوتر بتاريخ {u['date']:%d/%m/%Y} يطابق جرعة موجودة في فاتورة بنفس التاريخ "
                                f"(احتمال تكرار نفس الجرعة) — راجع قبل الإرسال")
    if counting and received == 0 and supposed_known:
        blocking.append("لم يُحتسب أي جرعة: لا يوجد دواء أساسي في الفواتير ولا صرف غير مفوتر — راجع الأسماء/الفواتير")
    remaining = max(supposed - received, 0)
    # Nothing left to receive (received == authorised, or MORE than authorised - e.g. a hormonal decree that really dispensed
    # more than its 6 months): this is NOT a review case any more. The request carries the previous decrees' invoices only,
    # exactly like a supportive decree: no received / yet-to-be-received sentence in the medical report.
    invoices_only = bool(supposed_known and counting and received > 0 and remaining == 0)
    if invoices_only:
        info.append(f"الجرعات المستلمة ({received:g}) " + ("تساوي" if received == supposed else "تزيد عن") +
                    f" المعتمدة ({supposed:g}) — لا توجد جرعات متبقية: يُرفق الطلب بفواتير القرارات السابقة فقط بلا جملة جرعات في التقرير الطبي")
    avg = round(inv_value / inv_doses, 2) if inv_doses and inv_value > 0 else None
    return dict(received=received, supposed=supposed, remaining=remaining, supposed_known=supposed_known, counting=counting,
                per_decree=per, blocking=blocking, info=info, invoices_only=invoices_only,
                invoiced_value=round(inv_value, 2), invoiced_doses=inv_doses, avg_dose_value=avg,
                pattern=(patterns_used[-1] if patterns_used else None))


def to_jsonable(x):
    if isinstance(x, dict):
        return {k: to_jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [to_jsonable(v) for v in x]
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    return x
