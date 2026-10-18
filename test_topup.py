import json, sys, types, re, os
from datetime import date, timedelta

# ---------- fake supabase_client ----------
T = {}
def _match(row, k, v):
    val = row.get(k)
    if v.startswith("eq."): return str(val) == v[3:]
    if v.startswith("in.("): return str(val) in v[4:-1].split(",")
    if v == "not.is.null": return val is not None
    if v.startswith("neq."): return str(val) != v[4:]
    if v.startswith("not.in.("): return str(val) not in v[8:-1].split(",")
    return True
def select(table, select="*", filters=None, order=None, limit=None):
    if table not in T: raise RuntimeError("no table " + table)
    rows = [dict(r) for r in T[table] if all(_match(r, k, v) for k, v in (filters or {}).items() if k != "or")]
    return rows[:limit] if limit else rows
UPD = []
def update(table, id_, body): UPD.append((table, id_, body)); [r.update(body) for r in T[table] if r["id"] == id_]
sb = types.ModuleType("supabase_client"); sb.select = select; sb.update = update
sb.upsert = lambda *a, **k: None; sb.rpc = lambda *a, **k: None; sb.insert = lambda *a, **k: None
sys.modules["supabase_client"] = sb
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import topup_overlap as ov, topup_resolution as rs

mp = json.load(open("topup_core_meds_map.json", encoding="utf-8"))
T["topup_regimen_map"] = [dict(plan_text=p["plan_text"], topup_protocol=p["protocol"]) for p in mp["plans"]]
ZOL6 = "زولاديكس تحت الجلد لمده 6 شهور (شامل المتابعة والتحاليل)"
COMB = "علاج هرموني خط ثاني + زولاديكس ودوائي وابحاث"
HORM = "علاج هرموني ودوائي وابحاث"
TAX = "علاج كيماوي تاكسان (دوستاكل) 4 جرعات (شامل المتابعة والتحاليل)"   # spelled as on the screenshot (NOT in the map)

def setup(cand, rows, view=None, req="2026-09-27T10:00:00"):
    T["decree_topup_candidates"] = [cand]
    T["decree_request_cases"] = [dict(id=10, created_at=req)]
    T["decree_topup_decree_lookup"] = rows
    T["decree_topup_lookup_view"] = [dict(candidate_id=cand["id"], decree_number=r["decree_number"], match_kind="PLAN")
                                    for r in rows if (view is None or r["decree_number"] in view)]
    T.pop("decree_topup_dose_calc", None)
    UPD.clear()
def row(cid, num, d, desc, val, left, exp=None, flag=None):
    return dict(candidate_id=cid, decree_number=num, decree_date=d, expiry_date=exp, description=desc, is_open=True,
                flag=flag, decree_value=val, value_left_website=left, pending_unsubmitted_value=0, real_value_left=left,
                fetched_at="2026-10-18T05:00:00+00:00")
cand = lambda proto, **k: dict(id=1, patient_id=5, protocol_key=proto, source_case_id=10, kind="TOPUP", status="OPEN",
                               lookup_status="DONE", manual_decree_numbers=None, manual_receipt_ids=None, **k)
ok = []
def check(name, cond):
    ok.append(cond); print(("PASS " if cond else "FAIL ") + name)

# 1) screenshot 2: zoladex, request 27/09, later decree 11/10 with left 5662.14 (dose 5662.50) -> resolved
setup(cand("zoladex"), [row(1, "202641505070", "2026-10-11", ZOL6, 11325, 5662.14, "2027-04-09"),
                        row(1, "202639071881", "2026-04-02", ZOL6, 11325, 0.0, "2026-09-29")], req="2026-09-27T10:00:00")
r = rs.auto_resolve(cand("zoladex"))
check("zoladex later decree with 5662.14 left (dose 5662.50) -> RESOLVED", bool(r) and r["decree"] == "202641505070")
check("  status written RESOLVED + auto message", any(u[2].get("status") == "RESOLVED" and u[2]["evidence_message"].startswith(rs.AUTO_PREFIX) for u in UPD))

# 2) same but left too small -> not resolved
setup(cand("zoladex"), [row(1, "202641505070", "2026-10-11", ZOL6, 11325, 3000.0, "2027-04-09")])
check("later decree but left 3000 < one dose -> NOT resolved", rs.auto_resolve(cand("zoladex")) is None and not UPD)

# 3) screenshot 1: taxane, request 13/10, decree 15/10, left == full value (plan spelling not in map)
setup(cand("taxanes"), [row(1, "202641553482", "2026-10-15", TAX, 16211, 16211, "2027-01-13")], req="2026-10-13T09:00:00")
r = rs.auto_resolve(cand("taxanes"))
check("taxane later decree 15/10 (request 13/10), value untouched -> RESOLVED", bool(r))

# 4) decree issued BEFORE the request date -> never resolves
setup(cand("zoladex"), [row(1, "1", "2026-09-01", ZOL6, 11325, 11325, "2027-03-01")], req="2026-09-27T10:00:00")
check("decree issued before the request -> NOT resolved", rs.auto_resolve(cand("zoladex")) is None)

# 5) later decree stopped/expired -> not resolved
setup(cand("zoladex"), [row(1, "1", "2026-10-11", ZOL6, 11325, 11325, "2027-04-09", flag="stopped")])
check("later decree flagged stopped -> NOT resolved", rs.auto_resolve(cand("zoladex")) is None)

# 6) manual decrees typed by operator -> left alone
c = cand("zoladex"); c["manual_decree_numbers"] = ["123"]
setup(c, [row(1, "1", "2026-10-11", ZOL6, 11325, 11325, "2027-04-09")])
check("operator typed decrees -> NOT auto-resolved", rs.auto_resolve(c) is None)

# 7) overlap matching
rows = [row(1, "H", "2026-09-10", HORM, 6000, 1000), row(1, "C", "2026-08-01", COMB, 9000, 0), row(1, "Z", "2026-02-01", ZOL6, 11325, 0)]
setup(cand("zoladex"), rows, view={"Z"})
m = ov.matching_rows(cand("zoladex"))
check("zoladex request: matches own Z + combined C, NOT hormonal-only H", [x["decree_number"] for x in m] == ["Z", "C"])
check("  C is flagged OVERLAP", [x["_kind"] for x in m] == ["PLAN", "OVERLAP"])
check("  cover = newest compatible only (C)", ov.cover(cand("zoladex"), ["Z", "C"]) == ["C"])

setup(cand("hormonal"), rows, view={"H"})
m = ov.matching_rows(cand("hormonal"))
check("hormonal request: matches H + combined C, NOT zoladex-only Z", sorted(x["decree_number"] for x in m) == ["C", "H"])
check("  cover = newest hormonal-containing (H)", ov.cover(cand("hormonal"), ["C", "H"]) == ["H"])

setup(cand("hormonal_zoladex"), rows, view={"C"})
m = ov.matching_rows(cand("hormonal_zoladex"))
check("combined request: matches C + H + Z", sorted(x["decree_number"] for x in m) == ["C", "H", "Z"])
check("  newest H (hormonal) + next Zoladex-containing C -> both", ov.cover(cand("hormonal_zoladex"), ["Z", "C", "H"]) == ["C", "H"])
check("  newest is combined -> only it", ov.cover(cand("hormonal_zoladex"), ["Z", "H", "C"]) == ["C"])
check("non-family protocol keeps newest only", ov.cover(cand("taxanes"), ["a", "b", "c"]) == ["c"])
check("compat sets", ov.compatible_protocols("zoladex") == {"zoladex", "hormonal_zoladex", "tamoxifen_zoladex"} and ov.compatible_protocols("taxanes") == {"taxanes"})

# 8) overlap decree as the 'later resolver'
setup(cand("zoladex"), [row(1, "C", "2026-10-11", COMB, 9000, 9000, "2027-04-09")])
check("zoladex request resolved by a LATER COMBINED decree with value left", bool(rs.auto_resolve(cand("zoladex"))))

# ---------- gate ----------
sys.modules.setdefault("requests", types.ModuleType("requests"))
import importlib
try:
    import requests  # noqa
except Exception:
    pass
import topup_gate as G
def gate_with(g, candrow, proto):
    G._candidate = lambda cid: candrow
    return G._adjust(dict(g))
base = dict(is_topup=True, kind="TOPUP", candidate_id=1, protocol_key="zoladex", ready=False, evidence_status="NO_PREVIOUS_DECREE",
            cycles_received=None, cycles_remaining=None, invoices_ok=0, invoices_bad=0)
nop = dict(status="OPEN", evidence_status="NO_PREVIOUS_DECREE", evidence_message="لا يوجد قرار سابق بنفس الخطة/البروتوكول ...", lookup_status="DONE",
           manual_decree_numbers=None, manual_receipt_ids=None, protocol_key="zoladex")
g = gate_with(base, nop, "zoladex")
check("gate: NO_PREVIOUS_DECREE + lookup DONE -> ready, no_previous", g["ready"] and g.get("no_previous"))
g = gate_with(base, {**nop, "lookup_status": "ERROR"}, "zoladex")
check("gate: lookup not DONE -> still blocked", not g["ready"] and not g.get("no_previous"))
g = gate_with(base, {**nop, "evidence_message": "تم حذف الفواتير المخزنة يدوياً"}, "zoladex")
check("gate: operator-cleared invoices -> still blocked", not g["ready"])
g = gate_with(base, {**nop, "manual_decree_numbers": ["1"]}, "zoladex")
check("gate: manual decrees typed -> still blocked", not g["ready"])
res = dict(nop, status="RESOLVED", evidence_message=rs.AUTO_PREFIX + ": ...")
g = gate_with(base, res, "zoladex")
check("gate: auto-resolved -> blocked with message", (not g["ready"]) and g.get("auto_resolved") and G.not_ready_message(g).startswith(rs.AUTO_PREFIX))
cont = dict(base, evidence_status="READY", invoices_ok=2, kind="TOPUP")
g = gate_with(cont, dict(nop, evidence_status="READY", evidence_message=""), "zoladex")
check("gate: continuous plan, READY invoices, counts missing -> ready", g["ready"] and g["continuous"])
tax = dict(cont, protocol_key="taxanes")
g = gate_with(tax, dict(nop, evidence_status="READY", evidence_message="", protocol_key="taxanes"), "taxanes")
check("gate: non-continuous plan, counts missing -> still NOT ready", (not g["ready"]) and not g["continuous"])
# cycles statement / invoices / hold
G._gate = lambda case_id: dict(is_topup=True, kind="TOPUP", candidate_id=1, protocol_key="hormonal", continuous=True, ready=True,
                               cycles_received=3, cycles_remaining=3)
check("cycles_statement: hormonal -> None (no dose wording)", G.cycles_statement(1) is None)
G._gate = lambda case_id: dict(is_topup=True, kind="TOPUP", candidate_id=1, protocol_key="taxanes", continuous=False, ready=True,
                               cycles_received=2, cycles_remaining=2)
check("cycles_statement: taxane keeps its sentence", (G.cycles_statement(1) or "").startswith("Patient had received 2"))
G._gate = lambda case_id: dict(is_topup=True, kind="TOPUP", candidate_id=1, protocol_key="zoladex", no_previous=True, ready=True)
check("with_invoices: no_previous -> original path untouched", G.with_invoices(1, "/tmp/x.pdf") == "/tmp/x.pdf")
check("hold: continuous -> none", G._dose_hold(1, dict(is_topup=True, kind="TOPUP", candidate_id=1, continuous=True)) is None)

# ---------- v2 additions: keyword fallback, tamoxifen family, gate-time resolve, continuous message ----------
check("keyword: combined text spelled differently -> hormonal_zoladex", ov.protocol_of_description("علاج هرموني + زولاديكس (خط ثالث)") == "hormonal_zoladex")
check("keyword: zoladex only (extra words)", ov.protocol_of_description("زولاديكس تحت الجلد لمدة 12 شهر شامل المتابعة") == "zoladex")
check("keyword: fulvestrant 'العلاج الهرموني' text is NOT hormonal", ov.protocol_of_description("فلفسترانت كخط ثاني لمستجيبي العلاج الهرموني (قرار اول)") is None)
check("keyword: taxane text -> None", ov.protocol_of_description(TAX) is None)
check("tamoxifen_zoladex bridges to zoladex and hormonal", ov.compatible_protocols("zoladex") >= {"zoladex", "hormonal_zoladex", "tamoxifen_zoladex"}
      and "tamoxifen" in ov.compatible_protocols("hormonal"))
check("tamoxifen/aromasin are continuous", ov.is_continuous("tamoxifen") and ov.is_continuous("aromasin") and not ov.is_continuous("taxanes"))
TAMZ = "علاج هرموني تاموكسفين المصاحب للزولاديكس تحت الجلد ( 6 اشهر) ودوائي وابحاث"
rows2 = [row(1, "T", "2026-08-01", TAMZ, 9000, 0), row(1, "Z", "2026-02-01", ZOL6, 11325, 0)]
setup(cand("zoladex"), rows2, view={"Z"})
check("zoladex request: later tamoxifen+zoladex decree is the cover", ov.cover(cand("zoladex"), [x["decree_number"] for x in ov.matching_rows(cand("zoladex"))]) == ["T"])
# unknown spelling (not in the map) still overlaps
ODD = "علاج هرموني + زولاديكس خط ثالث ودوائي"
setup(cand("zoladex"), [row(1, "Z", "2026-02-01", ZOL6, 11325, 0), row(1, "O", "2026-08-01", ODD, 9000, 0)], view={"Z"})
check("description not in the map still matched as OVERLAP", [x["decree_number"] for x in ov.matching_rows(cand("zoladex"))] == ["Z", "O"])
# request date: created_at fallback when optional columns do not exist
setup(cand("zoladex"), [row(1, "202641505070", "2026-10-11", ZOL6, 11325, 5662.14, "2027-04-09")], req="2026-09-27T10:00:00")
check("request_date falls back to created_at", str(rs.request_date(cand("zoladex"))) == "2026-09-27")
# continuous: not-ready message never asks for counts
msg = G.not_ready_message(dict(kind="TOPUP", continuous=True, cycles_received=None, cycles_remaining=None, evidence_status="PENDING", invoices_ok=0, invoices_bad=0))
check("continuous not-ready message does not ask for cycle counts", "عدد الدورات" not in msg)
msg = G.not_ready_message(dict(kind="TOPUP", continuous=False, cycles_received=None, cycles_remaining=None, evidence_status="READY", invoices_ok=2, invoices_bad=0))
check("non-continuous still asks for cycle counts", "عدد الدورات" in msg)
# gate-time resolve: a READY zoladex entry is closed at submit time when a later decree covers a dose
setup(cand("zoladex"), [row(1, "202641505070", "2026-10-11", ZOL6, 11325, 5662.14, "2027-04-09")], req="2026-09-27T10:00:00")
T["decree_topup_candidates"][0].update(evidence_status="READY", evidence_message="", protocol_key="zoladex")
gate_real = G._candidate
G._candidate = lambda cid: select("decree_topup_candidates", filters={"id": "eq.%s" % cid})[0]
g = G._adjust(dict(base, evidence_status="READY", invoices_ok=2))
check("gate-time auto-resolve closes the entry and blocks the send", g.get("auto_resolved") and not g["ready"])
G._candidate = gate_real

BONE = "العلاج المساعد لوقاية العظام للمرضي الذين يتلقوا علاج هرموني حمض الزيلودرونيك ( جرعة واحدة ) (شامل المتابعه والتحاليل)"
check("bone-protection zoledronic decree is NOT in the hormonal/zoladex family", ov.protocol_of_description(BONE) is None)
setup(cand("zoladex"), [row(1, "Z", "2026-02-01", ZOL6, 11325, 0), row(1, "B", "2026-09-01", BONE, 500, 500)], view={"Z"})
check("bone decree never matched as OVERLAP for zoladex", [x["decree_number"] for x in ov.matching_rows(cand("zoladex"))] == ["Z"])
setup(cand("hormonal"), [row(1, "H", "2026-02-01", HORM, 6000, 0), row(1, "B", "2026-09-01", BONE, 500, 500)], view={"H"})
check("bone decree never matched as OVERLAP for hormonal", [x["decree_number"] for x in ov.matching_rows(cand("hormonal"))] == ["H"])

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
