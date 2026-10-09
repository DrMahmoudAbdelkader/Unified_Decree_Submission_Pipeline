#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_decree_lookup.py
==========================================================================
Runs when you press «فحص قرارات المريض» on a top-up card (the page sets
decree_topup_candidates.lookup_status = 'PENDING' via topup_request_lookup and starts the
topup-evidence-fetch workflow, whose first step is this script; the nightly run also drains
any pending lookups).

For each pending candidate it opens ONE SMC session and, for that patient:
  1. lists ALL the patient's decrees        POST /smc/Decrees/DecreesSearch   (NationalID)
        -> decree number, issue date, type, value, discount, المتبقي (value left on the site),
           "قرار منتهي ؟" (مفتوح / مغلق) and the row colour (orange = past validity, blue = stopped)
  2. opens each decree                       GET  /smc/DecreeTreatmentProcedure/Create/<decree>
        -> الإجراء (the description that maps to a plan), المدة (days) -> expiry = issue date + days,
           التشخيص
  3. subtracts what Enhanced Monitor still has waiting to be submitted for that decree
        (pending_decree_orders.status <> 'completed' -> pending_decree_items.total_value) —
        the exact rule of lookup_patient_decree_value.py — to get the REAL value left
  4. stores one row per decree in decree_topup_decree_lookup. Which of them belong to the same
        plan / protocol as the candidate is decided in the DB (view decree_topup_lookup_view,
        using decree_description_plan_map), so it follows any later edit of the mapping.

Verified against your HAR: the list parser (9 decrees, values, مفتوح, remaining) and the details
parser (description, 120 days, diagnosis, remaining 20580.26) reproduce what the site shows.
NOT verified: a run against live SMC / Supabase.
"""
from __future__ import annotations
import logging, os, re, sys, time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from bs4 import BeautifulSoup

log = logging.getLogger("topup_decree_lookup")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
MAX_LOOKUPS = int(os.environ.get("MAX_LOOKUPS") or "300")          # total per run (was a single batch of 10)
BATCH = int(os.environ.get("LOOKUP_BATCH") or "25")                # fetched from the queue this many at a time
TIME_BUDGET_MIN = int(os.environ.get("LOOKUP_TIME_BUDGET_MIN") or "30")  # stop starting new ones after this
MAX_DECREES_PER_PATIENT = int(os.environ.get("MAX_DECREES_PER_PATIENT") or "40")
MAX_SEARCH_PAGES = 6
DELAY = 0.3
CAND, LOOK = "decree_topup_candidates", "decree_topup_decree_lookup"


# "run only these entries": the page passes candidate ids through the workflow input candidate_ids
# (comma separated). Empty = the whole queue (nightly run / the page's «تشغيل الطلب الآن»).
ONLY_IDS = [int(x) for x in re.findall(r"\d+", os.environ.get("CANDIDATE_IDS") or "")]
ONLY_ID_FILTER = ("in.(" + ",".join(map(str, ONLY_IDS)) + ")") if ONLY_IDS else None

_AR = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def ar2en(s: str) -> str:
    return (s or "").translate(_AR)


def _num(s):
    m = re.search(r"-?\d[\d,]*\.?\d*", ar2en(s or ""))
    return float(m.group(0).replace(",", "")) if m else None


# --------------------------------------------------------------------------- parsers (tested on the HAR)
def parse_decrees_list(html: str) -> list[dict]:
    """DecreesSearch result table -> one dict per decree."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("table#requestTable tr"):
        tds = tr.find_all("td", recursive=False)
        a = tr.select_one('a[href*="DecreeTreatmentProcedure/Create"]')
        if len(tds) < 9 or not a:
            continue
        t = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tds]
        style = (tds[0].get("style") or "").lower().replace(" ", "")
        flag = "expired" if "orange" in style else "stopped" if "lightskyblue" in style else None
        d = None
        try:
            d = datetime.strptime(t[3], "%Y-%m-%d").date()
        except ValueError:
            pass
        out.append({"decree_number": a["href"].rstrip("/").split("/")[-1], "national_id": t[2],
                    "decree_date": d, "decree_type": t[4], "decree_value": _num(t[5]),
                    "value_left_website": _num(t[7]), "open_text": t[8],
                    "is_open": "مفتوح" in t[8] and "مغلق" not in t[8], "flag": flag})
    return out


def parse_decree_details(html: str) -> dict:
    """DecreeTreatmentProcedure/Create/<id> -> الإجراء, المدة, التشخيص, المتبقي."""
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style"]):
        s.decompose()
    kv = {}
    for th in soup.find_all("th"):
        label = re.sub(r"\s+", " ", th.get_text(" ", strip=True))
        if label.endswith(":"):
            td = th.find_next_sibling("td")
            if td is not None:
                kv[label.rstrip(":").strip()] = re.sub(r"\s+", " ", td.get_text(" ", strip=True))
    text = ar2en(re.sub(r"\s+", " ", soup.get_text(" ")))
    rem = re.search(r"المتبقي\s*(-?\d[\d,]*\.?\d*)", text)
    return {"description": kv.get("الإجراء"), "diagnosis": kv.get("التشخيص"),
            "due_days": int(_num(kv.get("المدة")) or 0) or None if kv.get("المدة") else None,
            "remaining_on_page": float(rem.group(1).replace(",", "")) if rem else None}


# --------------------------------------------------------------------------- Supabase (REST, service role)
def _h(extra=None):
    h = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
    h.update(extra or {})
    return h


def _rest(method, path, **kw):
    r = requests.request(method, f"{SUPABASE_URL}/rest/v1/{path}", headers=_h(kw.pop("headers", None)),
                         timeout=60, **kw)
    if not r.ok:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
    return r.json() if r.text.strip() else None


def national_id_of(patient_id) -> str:
    rows = _rest("GET", "patients", params={"select": "national_id", "id": f"eq.{patient_id}", "limit": 1})
    return str(rows[0]["national_id"]) if rows and rows[0].get("national_id") else ""


def pending_unsubmitted_value(decree_number: str) -> float:
    """Enhanced Monitor's 'still to be submitted' bills for this decree — same rule as
    lookup_patient_decree_value.fetch_pending_unsubmitted_value()."""
    rows = _rest("GET", "pending_decree_orders", params={
        "select": "id,items:pending_decree_items(total_value)",
        "decree_number": f"eq.{decree_number}", "status": "neq.completed"}) or []
    return float(sum(float(i.get("total_value") or 0) for o in rows for i in (o.get("items") or [])))


# --------------------------------------------------------------------------- SMC
def search_patient_decrees(smc, nid: str) -> list[dict]:
    """Same form the site's own Decrees page posts (see the HAR), with an open date window.
    Pages until a page adds nothing new."""
    url = f"{smc_base()}/smc/Decrees/DecreesSearch"
    seen, out = set(), []
    today = date.today()
    for page in range(1, MAX_SEARCH_PAGES + 1):
        form = {"decreeID": "", "NationalID": nid, "PatientName": "",
                "dateFrom": "2000-01-01", "dateTo": (today + timedelta(days=1)).isoformat(),
                "Retrieved": "N", "decreeStatus": "", "decreeSource": "1", "stoppedDecree": "N", "page": str(page)}
        hdr = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{smc_base()}/smc/Decrees",
               "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        r = smc.s.post(url, data=form, headers=hdr, timeout=60)
        if smc._session_expired(r):
            if not smc.login():
                raise RuntimeError("SMC re-login failed")
            r = smc.s.post(url, data=form, headers=hdr, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"DecreesSearch HTTP {r.status_code}")
        rows = [d for d in parse_decrees_list(r.text) if ar2en(d["national_id"]) == nid]
        new = [d for d in rows if d["decree_number"] not in seen]
        if not new:
            break
        for d in new:
            seen.add(d["decree_number"])
            out.append(d)
        time.sleep(DELAY)
    return out


def smc_base() -> str:
    import Unified_Decree_Submission_Pipeline as _p
    return _p.BASE_URL


def decree_details(smc, decree_number: str) -> dict:
    r = smc._get(f"{smc_base()}/smc/DecreeTreatmentProcedure/Create/{decree_number}")
    return parse_decree_details(r.text) if r is not None else {}


# --------------------------------------------------------------------------- main
def lookup_one(smc, c: dict):
    cid = c["id"]
    nid = national_id_of(c.get("patient_id"))
    if not nid:
        raise RuntimeError("المريض بلا رقم قومي")
    decrees = search_patient_decrees(smc, nid)[:MAX_DECREES_PER_PATIENT]
    rows = []
    for d in decrees:
        det = decree_details(smc, d["decree_number"])
        time.sleep(DELAY)
        due = det.get("due_days")
        expiry = (d["decree_date"] + timedelta(days=due)) if d["decree_date"] and due else None
        left = d["value_left_website"] if d["value_left_website"] is not None else det.get("remaining_on_page")
        pending = pending_unsubmitted_value(d["decree_number"])
        rows.append({
            "candidate_id": cid, "decree_number": d["decree_number"],
            "decree_date": d["decree_date"].isoformat() if d["decree_date"] else None,
            "due_days": due, "expiry_date": expiry.isoformat() if expiry else None,
            "is_open": d["is_open"], "open_text": d["open_text"], "flag": d["flag"],
            "decree_type": d["decree_type"], "description": det.get("description"),
            "diagnosis": det.get("diagnosis"), "decree_value": d["decree_value"],
            "value_left_website": left, "pending_unsubmitted_value": pending,
            "real_value_left": (left - pending) if left is not None else None,
            "fetched_at": datetime.now(timezone.utc).isoformat()})
    # fresh snapshot: replace this candidate's rows
    _rest("DELETE", LOOK, params={"candidate_id": f"eq.{cid}"})
    if rows:
        _rest("POST", LOOK, json=rows, headers={"Prefer": "return=minimal"})
    return len(rows)


def _set(cid, **body):
    _rest("PATCH", CAND, params={"id": f"eq.{cid}"}, json=body, headers={"Prefer": "return=minimal"})


def main():
    import Unified_Decree_Submission_Pipeline as _p
    from Unified_Decree_Submission_Pipeline import SMCSession
    u = p = ""
    for un, pw in (("SMC_USERNAME_3", "SMC_PASSWORD_3"), ("SMC_USERNAME_2", "SMC_PASSWORD_2"),
                   ("SMC_USERNAME", "SMC_PASSWORD")):
        if os.environ.get(un) and os.environ.get(pw):
            u, p = os.environ[un], os.environ[pw]
            break
    if not u:
        log.error("No SMC credentials set."); sys.exit(1)

    # a crashed earlier run must not leave a lookup stuck in RUNNING
    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    try:
        _rest("PATCH", CAND, params={"lookup_status": "eq.RUNNING", "updated_at": f"lt.{stale}"},
              json={"lookup_status": "PENDING"}, headers={"Prefer": "return=minimal"})
    except Exception as e:
        log.warning(f"stale release skipped: {e}")

    def next_batch(n):
        prm = {"select": "id,patient_id", "lookup_status": "eq.PENDING",
               "order": "lookup_requested_at.asc", "limit": n}
        if ONLY_ID_FILTER:
            prm["id"] = ONLY_ID_FILTER
        return _rest("GET", CAND, params=prm) or []

    todo = next_batch(min(BATCH, MAX_LOOKUPS))
    log.info(f"{len(todo)} lookup(s) in the first batch (cap {MAX_LOOKUPS} per run, {TIME_BUDGET_MIN} min budget).")
    if not todo:
        return
    _p.USERNAME, _p.PASSWORD = u, p
    smc = SMCSession()
    if not smc.login():
        for c in todo:
            _set(c["id"], lookup_status="ERROR", lookup_message="فشل تسجيل الدخول إلى SMC",
                 lookup_finished_at=datetime.now(timezone.utc).isoformat())
        sys.exit(1)
    deadline = time.time() + TIME_BUDGET_MIN * 60
    processed = 0
    while todo:
        for c in todo:
            _set(c["id"], lookup_status="RUNNING")
            try:
                n = lookup_one(smc, c)
                _set(c["id"], lookup_status="DONE", lookup_message=f"{n} قرار",
                     lookup_finished_at=datetime.now(timezone.utc).isoformat())
                log.info(f"candidate {c['id']}: {n} decree(s)")
            except Exception as e:
                log.exception(f"candidate {c['id']} lookup failed")
                _set(c["id"], lookup_status="ERROR", lookup_message=f"{type(e).__name__}: {str(e)[:200]}",
                     lookup_finished_at=datetime.now(timezone.utc).isoformat())
            processed += 1
        if processed >= MAX_LOOKUPS or time.time() > deadline:
            break
        todo = next_batch(min(BATCH, MAX_LOOKUPS - processed))   # ERROR rows are no longer PENDING, so this always ends
    left = len(next_batch(1000))
    log.info(f"done: {processed} processed this run; {left} still PENDING"
             + (" (cap/time budget reached - they run on the next press or the nightly run)" if left else "."))


if __name__ == "__main__":
    main()
