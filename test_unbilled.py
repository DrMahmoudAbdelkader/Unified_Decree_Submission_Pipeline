import json, sys, types, re
from urllib.parse import urlparse, parse_qs
sys.path.insert(0, ".")
import topup_unbilled as ub

BASE = "https://smc.smcegy.com"
NAME, NID = "جيهان حسين ابراهيم  خليل", "27511192301382"
CREATE_PAGE = f'<input class="form-control" id="TITLE" name="TITLE" type="text" value="{NAME} " />'
DECREE_PAGE = f'<input type="hidden" id="CitizenSSN" value="{NID}">'

class R:
    def __init__(s, text="", code=200): s.text, s.status_code = text, code

class Fake:
    """Minimal SMC: items waiting -> check ok; bill created -> items no longer waiting."""
    def __init__(s, waiting=True, pending=False, exceed=False, used_titles=()):
        s.waiting, s.pending, s.exceed, s.titles, s.posts = waiting, pending, exceed, list(used_titles), []
        s.s = s
    def _session_expired(s, r): return False
    def login(s): return True
    def request(s, method, url, params=None, data=None, headers=None, timeout=None):
        u = urlparse(url)
        if "HospDecreeReceipts/Create" in u.path and method == "GET": return R(CREATE_PAGE)
        if "DecreeTreatmentProcedure/Create" in u.path: return R(DECREE_PAGE)
        if u.path.endswith("CheckDecreeTreatmentProcedures"):
            if s.exceed: return R('{"fail":"HasExceedValue"}')
            if not s.waiting: return R('{"fail":"no data in decreeTreatmentProcedureList"}')
            return R('{"fail":"title exists"}' if params["title"] in s.titles else '{"success":"success"}')
        if u.path.endswith("CheckPendingReceipts"): return R("true" if s.pending else "false")
        if u.path.endswith("HospDecreeReceipts/Create") and method == "POST":
            body = json.loads(data.decode()); s.posts.append(body); s.titles.append(body["TITLE"]); s.waiting = False
            return R("<html/>")
        raise AssertionError(url)

def lister(fake):
    def f(smc, decree, bill_unbilled=True):
        return [dict(receipt_id=str(100 + i), title=re.sub(r"\s+"," ",t), amount=2731.51, status_text="معلق") for i, t in enumerate(fake.titles)]
    return f

ok = []
def check(n, c): ok.append(c); print(("PASS " if c else "FAIL ") + n)

f = Fake(); r = ub.ensure_billed(f, "202641504055", BASE, lister(f))
check("items waiting -> bill created", r["status"] == "CREATED" and len(f.posts) == 1)
check("title format '<name> - <nid>' (double space of the name kept)", f.posts[0]["TITLE"] == f"{NAME} - {NID}")
check("DECREEID sent as number", f.posts[0]["DECREEID"] == 202641504055)
r = ub.ensure_billed(f, "202641504055", BASE, lister(f))
check("second pass: nothing waiting -> no second bill", r["status"] == "NOTHING" and len(f.posts) == 1)

f = Fake(waiting=False); r = ub.ensure_billed(f, "1", BASE, lister(f))
check("nothing waiting -> NOTHING, no POST", r["status"] == "NOTHING" and not f.posts)

f = Fake(used_titles=[f"{NAME} - {NID}"]); r = ub.ensure_billed(f, "1", BASE, lister(f))
check("title already used -> '(2)' variant", r["status"] == "CREATED" and f.posts[0]["TITLE"] == f"{NAME} - {NID} (2)")

f = Fake(pending=True); r = ub.ensure_billed(f, "1", BASE, lister(f))
check("pending unsent bill -> BLOCKED, nothing written", r["status"] == "BLOCKED" and not f.posts)
f = Fake(exceed=True); r = ub.ensure_billed(f, "1", BASE, lister(f))
check("exceed value -> BLOCKED", r["status"] == "BLOCKED" and not f.posts)

import os
os.environ["TOPUP_BILL_UNBILLED"] = "dry"
f = Fake(); r = ub.ensure_billed(f, "2", BASE, lister(f))
check("dry mode -> DRY_RUN, nothing written", r["status"] == "DRY_RUN" and not f.posts)
check("blocking() reports DRY_RUN / BLOCKED decrees", [x["decree"] for x in ub.blocking(["2", "1", "9"])] == ["2", "1"])
os.environ["TOPUP_BILL_UNBILLED"] = "0"
f = Fake(); check("off -> SKIPPED", ub.ensure_billed(f, "3", BASE, lister(f))["status"] == "SKIPPED" and not f.posts)
del os.environ["TOPUP_BILL_UNBILLED"]

class Boom(Fake):
    def request(s, *a, **k): raise ConnectionError("down")
f = Boom(); r = ub.ensure_billed(f, "4", BASE, lister(f)); check("network error -> FAILED, never raises", r["status"] == "FAILED")

class NoShow(Fake):
    def request(s, method, url, **k):
        if url.endswith("HospDecreeReceipts/Create") and method == "POST": return R("<html/>")
        return super().request(method, url, **k)
f = NoShow(); r = ub.ensure_billed(f, "5", BASE, lambda *a, **k: []); check("bill not in listing -> FAILED", r["status"] == "FAILED")

check("parse_check", ub.parse_check('{"success":"success"}')["ok"] and ub.parse_check('{"fail":"HasExceedValue"}')["fail"] == "HasExceedValue")
print(f"\n{sum(ok)}/{len(ok)} passed"); sys.exit(0 if all(ok) else 1)
