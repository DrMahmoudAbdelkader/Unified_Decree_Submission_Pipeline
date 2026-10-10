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
 BLOCKING flags (request held until a person reviews): received > authorised, a decree whose plan is not in the map,
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
        if not base and not re.search(r"\d", c[4]):
            continue                                   # header / total rows
        items.append(dict(name=c[1], qty=qty, unit=c[3], date_field=c[4], date=base, note=c[5],
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


def count_plan(raw_rows: Iterable[str], plan: dict, matcher: Matcher) -> dict:
    anchors = [c["med_key"] for c in plan["core"] if c["role"] == "anchor"]
    allowed = {c["med_key"] for c in plan["core"]}
    qpd = {c["med_key"]: c.get("qty_per_dose") for c in plan["core"]}
    per: Dict[str, dict] = {}
    for it in parse_rows(raw_rows):
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
        if (b - a).days < MIN_GAP_DAYS and not qty_ok:
            info.append(f"تاريخا جرعتين بينهما {(b - a).days} يوم فقط: {a:%d/%m/%Y} و {b:%d/%m/%Y}")
    return dict(doses=len(dose_dates), dose_dates=dose_dates, meds=meds, info=info, anchors=anchors)


def compute_chain(decrees: List[dict], idx: Dict[str, dict], matcher: Matcher) -> dict:
    """decrees: oldest -> newest, each {'decree_number','decree_date','plan_text','rows':[...],'pending':[{'date','value'}],'invoices':int}"""
    per, blocking, info = [], [], []
    seen: Dict[dt.date, str] = {}
    received = supposed = 0.0
    supposed_known = True
    counting = 0
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
        res = count_plan(d.get("rows") or [], plan, matcher)
        row.update(authorised=plan["doses_per_decree"], anchors=res["anchors"])
        supposed += float(plan["doses_per_decree"] or 0)
        mine = []
        for day in res["dose_dates"]:
            if day in seen:
                info.append(f"التاريخ {day:%d/%m/%Y} ظاهر في القرارين {seen[day]} و {d['decree_number']} — احتُسب مرة واحدة")
                continue
            seen[day] = d["decree_number"]; mine.append(day)
        row["invoice_dates"] = mine
        row["info"] = res["info"]
        info.extend(f"القرار {d['decree_number']}: {x}" for x in res["info"])
        if not d.get("invoices"):
            info.append(f"القرار {d['decree_number']}: لا توجد فواتير على SMC")
        for o in sorted(d.get("pending") or [], key=lambda x: (x["date"] is None, x["date"] or dt.date.min)):
            row["unbilled"].append(dict(date=o["date"], value=o.get("value")))
            if o["date"] and o["date"] in seen:
                info.append(f"صرف غير مفوتر بتاريخ {o['date']:%d/%m/%Y} يطابق تاريخ جرعة مفوترة")
        row["doses"] = len(mine) + len(row["unbilled"])
        received += row["doses"]
        per.append(row)
    if counting and received == 0 and supposed_known:
        blocking.append("لم يُحتسب أي جرعة: لا يوجد دواء أساسي في الفواتير ولا صرف غير مفوتر — راجع الأسماء/الفواتير")
    if supposed_known and received > supposed:
        blocking.append(f"الجرعات المستلمة ({received:g}) أكثر من المعتمدة ({supposed:g}) — راجع الحالة يدويًا")
    return dict(received=received, supposed=supposed, remaining=max(supposed - received, 0), supposed_known=supposed_known, counting=counting,
                per_decree=per, blocking=blocking, info=info)


def to_jsonable(x):
    if isinstance(x, dict):
        return {k: to_jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [to_jsonable(v) for v in x]
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    return x
