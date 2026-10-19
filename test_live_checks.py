"""Tests for topup_live_checks.py (manual SMC requests + ID card in DMS archive) and the gate's manual-request block.
Network / DB are faked; the DMS module's own date parsing and PDF merge are the REAL ones."""
import os, sys, types, io
from datetime import date, datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["R2_BUCKET_NAME"] = "bkt"

# ---- fake status module (the real one needs the whole SMC pipeline) ----
BUCKETS = {"قرار نهائي": "Approved", "خطاب ادارى": "Admin_Letter", "ملغي": "Cancelled",
           "تم التسجيل": "Registered", "لجنة طبية": "Medical_Committee", "محول إلى طبيب آخر": "Referred",
           "توصية مبدئية": "Preliminary", "توصية نهائية": "Final_Recommendation"}
st = types.ModuleType("decree_status_and_letters_sync")
st.BASE_URL = "https://smc.example"
st.load_status_map = lambda: BUCKETS
st.resolve_status_bucket = lambda m, s: {"english_bucket": m.get(s, "Unknown")}
sys.modules["decree_status_and_letters_sync"] = st

import topup_live_checks as tl
import patient_pdf_dms_archive_fallback as dms
import topup_gate as G

ok = []
def check(n, c): ok.append(bool(c)); print(("PASS " if c else "FAIL ") + n)

# =========================================================================== CHECK 1: the walk
def req(num, d, status):
    return {"request_number": str(num), "request_date": d, "request_status": status, "patient_id": "N"}

NEWEST_FIRST = [
    req(9, "2026-10-17", "تم التسجيل"),            # manual, pending
    req(8, "2026-10-16", "ملغي"),                  # cancelled -> ignored
    req(7, "2026-10-15", "لجنة طبية"),             # filed by the app (known) -> ignored
    req(6, "2026-10-14", "محول إلى طبيب آخر"),     # manual, pending
    req(5, "2026-10-13", "توصية نهائية"),          # manual, pending
    req(4, "2026-10-12", "قرار نهائي"),            # STOP
    req(3, "2026-10-01", "تم التسجيل"),            # older than the stop -> must never be listed
]
WROTE = {"delete": [], "post": []}
SET = []
def fake_rest(method, path, **kw):
    if method == "DELETE": WROTE["delete"].append((path, kw.get("params")))
    if method == "POST": WROTE["post"].append((path, kw.get("json")))
    return None
tl._rest = fake_rest
tl._set = lambda cid, **b: SET.append((cid, b))
tl.national_id_of = lambda pid: "N"
tl._source_request = lambda c: (date(2026, 9, 27), "2")
tl._known_request_numbers = lambda pid: {"7"}
tl._plan_text = lambda smc, num: f"خطة يدوية {num}"
tl.time.sleep = lambda s: None

def run_manual(listing, cand=None):
    WROTE["post"].clear(); WROTE["delete"].clear(); SET.clear()
    tl._patient_requests = lambda smc, nid, since: listing
    c = dict(id=1, patient_id=5, kind="TOPUP"); c.update(cand or {})
    n = tl.check_manual(c, None, BUCKETS, {})
    rows = WROTE["post"][0][1] if WROTE["post"] else []
    return n, rows

n, rows = run_manual(NEWEST_FIRST)
check("walk stops at the first «قرار نهائي»; cancelled and app-filed requests ignored -> 9, 6, 5",
      [r["request_number"] for r in rows] == ["9", "6", "5"] and n == 3)
check("  each stored with its date, live status and the plan text found on the request page",
      rows[0]["request_date"] == "2026-10-17" and rows[0]["status_raw"] == "تم التسجيل" and rows[0]["plan_text"] == "خطة يدوية 9")
check("  rows replaced (DELETE then POST) and the check time stamped", WROTE["delete"] and SET and "manual_check_at" in SET[-1][1])
n, rows = run_manual(NEWEST_FIRST, {"manual_ignored_numbers": ["6"]})
check("operator pressed «تجاهل» on request 6 -> not stored again", [r["request_number"] for r in rows] == ["9", "5"])
n, rows = run_manual([req(4, "2026-10-12", "قرار نهائي")] + NEWEST_FIRST[:2])
check("newest request already final -> nothing can be pending after it", n == 0 and not rows)
n, rows = run_manual([req(9, "2026-10-17", "خطاب ادارى")])
check("newest request is an admin letter -> stop, none pending", n == 0)
n, rows = run_manual([req(9, "2026-10-17", "حالة جديدة غير معروفة")])
check("an UNMAPPED status is treated as pending (never silently ignored)", n == 1 and rows[0]["status_bucket"] == "Unknown")
n, rows = run_manual([req(9, "2026-09-20", "تم التسجيل")])
check("request older than the declined request is not a pending manual request", n == 0)
n, rows = run_manual([req(2, "2026-09-27", "تم التسجيل")])
check("the declined request itself (source number) is never reported", n == 0)

# =========================================================================== CHECK 2: ID card in the DMS archive
import pymupdf
def pdf_bytes(txt):
    d = pymupdf.open(); d.new_page().insert_text((72, 72), txt); b = d.tobytes(); d.close(); return b

INDEX = "".join(f'<tr data-id="{x}"></tr>' for x in
                ["01/10/2026 10:00:00 AM", "10/10/2026 09:00:00 AM", "20/10/2026 15:22:18 PM"])
CALLS = {}
def fake_open(nid): return (object(), "MR1", INDEX) if CALLS.get("open", True) else None
dms._open_patient_session = fake_open
def fake_view_all(session, ids): CALLS["viewed"] = list(ids); return "<gallery/>"
dms.call_archive_view_all = fake_view_all
dms.parse_gallery_pdf_urls = lambda html: [f"u{i}" for i in range(len(CALLS["viewed"]))]
dms._download_pdf_bytes = lambda session, u: pdf_bytes("card " + u)

class S3:
    def __init__(s): s.put, s.deleted = [], []
    def put_object(s, **k): s.put.append(k)
    def delete_object(s, **k): s.deleted.append(k)
s3 = S3(); tl._r2 = lambda: s3
RPC = []
tl._rpc = lambda name, body: RPC.append((name, body))
INVS = {"old": [], "posted": []}
def rest2(method, path, **kw):
    if method == "GET" and path == tl.INV: return INVS["old"]
    if method == "POST": INVS["posted"].append(kw.get("json"))
    return None
tl._rest = rest2

def run_id(letter, rejected=None, opened=True):
    CALLS.clear(); CALLS["open"] = opened; RPC.clear(); SET.clear(); INVS["posted"].clear(); s3.put.clear(); s3.deleted.clear()
    tl._letter_date = lambda c: letter
    c = dict(id=7, patient_id=5, kind="ID_CARD", idcard_rejected_ids=rejected or [])
    return tl.check_idcard(c)

r = run_id(date(2026, 10, 10))
check("archive rows dated ON/AFTER the letter are picked (10/10 and 20/10, not 01/10)",
      r == "FOUND" and CALLS["viewed"] == ["10/10/2026 09:00:00 AM", "20/10/2026 15:22:18 PM"])
check("  extracted paper merged into ONE pdf, stored in R2 and recorded as ID_CARD_NEW for review",
      len(s3.put) == 1 and INVS["posted"][0][0]["doc_kind"] == "ID_CARD_NEW" and INVS["posted"][0][0]["page_count"] == 2
      and INVS["posted"][0][0]["status"] == "REVIEW")
check("  entry moved to «جاهز للطلب» through topup_mark_id_fixed", RPC and RPC[0][0] == "topup_mark_id_fixed" and RPC[0][1]["p_id"] == 7)
check("  state FOUND + the archive ids remembered", any(b.get("idcard_state") == "FOUND" and len(b["idcard_found_ids"]) == 2 for _, b in SET))

r = run_id(date(2026, 10, 10), rejected=["20/10/2026 15:22:18 PM"])
check("a paper the user rejected is never picked again", r == "FOUND" and CALLS["viewed"] == ["10/10/2026 09:00:00 AM"])
r = run_id(date(2026, 10, 10), rejected=["10/10/2026 09:00:00 AM", "20/10/2026 15:22:18 PM"])
check("everything new was rejected -> back to WAITING_NEW, nothing marked fixed", r == "WAITING_NEW" and not RPC and not INVS["posted"])
r = run_id(date(2026, 10, 25))
check("no archive paper after the letter -> WAITING_NEW (tab «بانتظار بطاقة سارية جديدة»), nothing extracted",
      r == "WAITING_NEW" and not RPC and not s3.put and any(b.get("idcard_state") == "WAITING_NEW" for _, b in SET))
r = run_id(date(2026, 10, 10), opened=False)
check("DMS unreachable -> ERROR, entry NOT marked ready and NOT forced to waiting", r == "ERROR" and not RPC and not any(b.get("idcard_state") for _, b in SET))
INVS["old"] = [{"id": 1, "r2_key": "old.pdf"}]
r = run_id(date(2026, 10, 10))
check("a newer finding replaces the previously stored ID_CARD_NEW file", r == "FOUND" and s3.deleted and s3.deleted[0]["Key"] == "old.pdf")
INVS["old"] = []

# due-ness: 24 h re-check
now = datetime.now(timezone.utc)
check("24 h rule: checked 2 h ago -> not due, 30 h ago -> due", (tl._age_h((now - timedelta(hours=2)).isoformat()) or 0) < tl.IDCARD_MAX_AGE_H
      and (tl._age_h((now - timedelta(hours=30)).isoformat()) or 0) >= tl.IDCARD_MAX_AGE_H)

# =========================================================================== gate: manual request blocks ALL kinds
G._rpc_orig = G._rpc
for kind in ("TOPUP", "PREV_INCLUSIVE", "PREV_REPORT", "ID_CARD"):
    G._manual_requests = lambda cid: [{"request_number": "777", "request_date": "2026-10-15", "status_raw": "لجنة طبية", "plan_text": "خطة يدوية"}]
    g = G._manual_block(dict(is_topup=True, kind=kind, candidate_id=1, ready=True))
    check(f"gate [{kind}]: ready entry becomes NOT ready while a manual request is pending", g["ready"] is False and g.get("manual_pending"))
msg = G.not_ready_message(g)
check("gate message names the request number, date, status and plan", all(x in msg for x in ("777", "2026-10-15", "لجنة طبية", "خطة يدوية")))
G._manual_requests = lambda cid: []
g = G._manual_block(dict(is_topup=True, kind="TOPUP", candidate_id=1, ready=True))
check("gate: no manual request -> untouched", g["ready"] is True and "manual_pending" not in g)
g = G._manual_block(dict(is_topup=False, candidate_id=None, ready=True))
check("gate: a normal (non top-up) request is never touched", g["ready"] is True)
def boom(cid): raise RuntimeError("relation does not exist")
G._manual_requests = boom
g = G._manual_block(dict(is_topup=True, kind="TOPUP", candidate_id=1, ready=True))
check("gate: migration 15 missing -> fails OPEN (old behaviour), never blocks", g["ready"] is True)
G._manual_requests = lambda cid: [{"request_number": "1"}]
os.environ["TOPUP_MANUAL_BLOCK"] = "0"
g = G._manual_block(dict(is_topup=True, kind="TOPUP", candidate_id=1, ready=True))
check("gate: TOPUP_MANUAL_BLOCK=0 switches it off", g["ready"] is True)
del os.environ["TOPUP_MANUAL_BLOCK"]
# the chain used by assert_ready / split_ready
G._gate_raw = lambda cid: dict(is_topup=True, kind="PREV_REPORT", candidate_id=1, ready=True, protocol_key=None)
G._adjust = lambda g: g
try:
    G.assert_ready(10); raised = False
except G.TopupNotReady as e:
    raised = "777" in str(e) or "يدوياً" in str(e)
G._manual_requests = lambda cid: [{"request_number": "777", "request_date": "2026-10-15", "status_raw": "لجنة طبية", "plan_text": ""}]
try:
    G.assert_ready(10); raised = False
except G.TopupNotReady as e:
    raised = "777" in str(e)
check("assert_ready() raises with the manual-request message (this is what stops the submission)", raised)
G._dose_hold = lambda cid, g: None
import types as _t
sys.modules["decree_common"] = _t.SimpleNamespace(open_requirement=lambda *a, **k: None)
ready, blocked = G.split_ready([{"id": 10}])
check("split_ready() holds the case back instead of consuming an MDT", not ready and blocked and blocked[0]["case_id"] == 10)

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
