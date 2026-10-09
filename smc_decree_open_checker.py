# smc_decree_open_checker.py
# ---------------------------------------------------------------------------
# Reads every decree number that still has PENDING (unsubmitted) entries in
# Enhanced Monitor (pending_decree_orders.status <> 'completed'), asks SMC for
# its current state (DecreesSearch) and records:
#   * open / closed          ("قرار منتهي ؟" column: مفتوح / مغلق)
#   * decree value + value left ("المتبقي")
#   * first time it was spotted open (cycle clock for the statistics)
#   * last time it was checked / last time seen still closed
# and flips pending_decree_orders.current_status to 'open' / 'closed' so the
# Enhanced Monitor list shows the new state.
#
# It NEVER submits anything to SMC and never clears entries - a user does that
# from the new page after exporting the ready list.
#
# Secrets / env (GitHub repo secrets):
#   SMC_USERNAME_3, SMC_PASSWORD_3, SUPABASE_URL, SUPABASE_SERVICE_KEY
# Optional env:
#   ONLY_DECREE   check just one decree number (manual runs)
#   DRY_RUN       "true" -> read SMC, print results, write nothing
#   DELAY         seconds between SMC calls (default 0.6)
#   TRIGGER_TYPE  schedule | workflow_dispatch | local
# ---------------------------------------------------------------------------

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

import json
import sharding   # SHARD_COUNT/SHARD_INDEX: parallel slices (no-op when unset)

# ── CONFIG ──────────────────────────────────────────────────────────────────
def env(name, default=""):
    """Read an env var and strip stray whitespace/newlines (a very common
    copy-paste artefact in GitHub secrets that breaks HTTP headers)."""
    return (os.environ.get(name, default) or "").strip()


USERNAME = env("SMC_USERNAME_3")
PASSWORD = env("SMC_PASSWORD_3")
SUPABASE_URL = env("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = env("SUPABASE_SERVICE_KEY")
ONLY_DECREE = env("ONLY_DECREE")
DRY_RUN = env("DRY_RUN", "false").lower() == "true"
DELAY = float(env("DELAY", "0.6"))
TRIGGER_TYPE = env("TRIGGER_TYPE", "local")

BASE_URL = "https://smc.smcegy.com"
LOGIN_URL = BASE_URL + "/smc/Home/Index"
SEARCH_URL = BASE_URL + "/smc/Decrees/DecreesSearch"

MAX_RETRIES = 3

# ── SMC SESSION ─────────────────────────────────────────────────────────────
SMC = requests.Session()
SMC.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE_URL + "/smc/Decrees",
    "Origin": BASE_URL,
})

# ── SUPABASE (PostgREST) ────────────────────────────────────────────────────
SB = requests.Session()
SB.headers.update({
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
})


def log(msg):
    print(msg, flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def sb_get_all(path):
    """GET with pagination (PostgREST caps responses at 1000 rows)."""
    rows, offset, page = [], 0, 1000
    while True:
        sep = "&" if "?" in path else "?"
        r = SB.get(f"{SUPABASE_URL}/rest/v1/{path}{sep}limit={page}&offset={offset}", timeout=60)
        r.raise_for_status()
        chunk = r.json()
        rows += chunk
        if len(chunk) < page:
            return rows
        offset += page


def sb_patch(path, payload):
    if DRY_RUN:
        return None
    r = SB.patch(f"{SUPABASE_URL}/rest/v1/{path}", json=payload,
                 headers={"Prefer": "return=minimal"}, timeout=60)
    r.raise_for_status()


def sb_post(path, payload, return_rep=False):
    if DRY_RUN:
        return None
    h = {"Prefer": "return=representation" if return_rep else "return=minimal"}
    r = SB.post(f"{SUPABASE_URL}/rest/v1/{path}", json=payload, headers=h, timeout=60)
    r.raise_for_status()
    return r.json() if return_rep else None


# ── SMC LOGIN ───────────────────────────────────────────────────────────────
def extract_token(html):
    m = re.search(r'<input[^>]+name="__RequestVerificationToken"[^>]+value="([^"]+)"', html)
    return m.group(1) if m else ""


def login():
    log("Logging in to SMC…")
    try:
        r = SMC.get(LOGIN_URL, timeout=30)
        data = {"username": USERNAME, "password": PASSWORD}
        tok = extract_token(r.text)
        if tok:
            data["__RequestVerificationToken"] = tok
        r2 = SMC.post(LOGIN_URL, data=data, allow_redirects=True, timeout=30)
    except requests.RequestException as e:
        log(f"  login network error: {e}")
        return False
    low = r2.text.lower()
    ok = "logout" in low or "dashboard" in low or "مرحباً" in r2.text
    log("  login OK ✓" if ok else "  login FAILED – check SMC_USERNAME_3 / SMC_PASSWORD_3")
    return ok


# ── SMC DECREE LOOKUP ───────────────────────────────────────────────────────
def to_number(text):
    t = (text or "").replace(",", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def parse_search_html(html, decree_id):
    """Returns dict(status, decree_value, value_left, approval_text) or None if
    the decree row is not in the response (also None when the page is not the
    results table at all, e.g. session expired)."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table#requestTable")
    if not table:
        return None

    # map columns by header text so a layout change does not silently break us
    headers = [th.get_text(strip=True) for th in table.select("tr th")]

    def col(*needles):
        for i, h in enumerate(headers):
            if any(n in h for n in needles):
                return i
        return None

    i_value = col("قيمة القرار")
    i_left = col("المتبقي")
    i_state = col("قرار منتهي")
    i_appr = col("حالة القرار")
    # sensible fallbacks = the layout in your sample response
    i_value = 5 if i_value is None else i_value
    i_left = 7 if i_left is None else i_left
    i_state = 8 if i_state is None else i_state
    i_appr = 9 if i_appr is None else i_appr

    for row in table.select("tr"):
        cells = row.find_all("td")
        if len(cells) <= max(i_value, i_left, i_state):
            continue
        if str(decree_id) not in cells[0].get_text():
            continue

        state_cell = cells[i_state]
        state_txt = state_cell.get_text(strip=True)
        if "مفتوح" in state_txt or "مقتوح" in state_txt:
            status = "open"
        elif "مغلق" in state_txt:
            status = "closed"
        else:
            status = "unknown"

        return {
            "status": status,
            "state_text": state_txt,
            "decree_value": to_number(cells[i_value].get_text(strip=True)),
            "value_left": to_number(cells[i_left].get_text(strip=True)),
            "approval_text": cells[i_appr].get_text(" ", strip=True) if len(cells) > i_appr else "",
        }
    return None


def check_decree(decree_id):
    """Returns (info|None, error_str)."""
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    payload = {
        "decreeID": decree_id, "NationalID": "", "PatientName": "",
        "dateFrom": yesterday, "dateTo": today,   # dates are ignored by SMC when decreeID is given
        "Retrieved": "N", "decreeStatus": "", "decreeSource": "1",
        "stoppedDecree": "N", "page": "1",
    }
    relogged = False
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SMC.post(SEARCH_URL, data=payload, timeout=45)
        except requests.RequestException as e:
            err = f"request error: {e}"
            time.sleep(2 * attempt)
            continue
        if r.status_code != 200:
            err = f"HTTP {r.status_code}"
            time.sleep(2 * attempt)
            continue
        info = parse_search_html(r.text, decree_id)
        if info:
            return info, ""
        # no table -> session probably expired; log in once and retry
        if "requestTable" not in r.text and not relogged:
            relogged = True
            if login():
                continue
        return None, "decree row not found in SMC response"
    return None, err


# ── MAIN ────────────────────────────────────────────────────────────────────
def main():
    missing = [n for n, v in [("SMC_USERNAME_3", USERNAME), ("SMC_PASSWORD_3", PASSWORD),
                              ("SUPABASE_URL", SUPABASE_URL),
                              ("SUPABASE_SERVICE_KEY", SUPABASE_KEY)] if not v]
    if missing:
        log(f"Missing environment variables: {', '.join(missing)}")
        sys.exit(2)

    log("=" * 64)
    log(f"Decree open-status check  |  {TRIGGER_TYPE}  |  dry_run={DRY_RUN}")
    log("=" * 64)

    # 1) pending orders + their value, grouped per decree number
    orders = sb_get_all(
        "pending_decree_orders?select=id,decree_number,patient_id,patients(name,national_id)"
        "&status=neq.completed&decree_number=not.is.null")
    pend = {}
    for o in orders:
        d = str(o["decree_number"]).strip()
        if ONLY_DECREE and d != ONLY_DECREE:
            continue
        g = pend.setdefault(d, {"order_ids": [], "patient_id": o.get("patient_id"),
                                "name": (o.get("patients") or {}).get("name"),
                                "nid": (o.get("patients") or {}).get("national_id"),
                                "value": 0.0})
        g["order_ids"].append(o["id"])

    if pend:
        all_ids = [i for g in pend.values() for i in g["order_ids"]]
        owner = {i: d for d, g in pend.items() for i in g["order_ids"]}
        for b in range(0, len(all_ids), 200):
            ids = ",".join(map(str, all_ids[b:b + 200]))
            for it in sb_get_all(f"pending_decree_items?select=order_id,total_value&order_id=in.({ids})"):
                pend[owner[it["order_id"]]]["value"] += float(it.get("total_value") or 0)

    log(f"Decrees with pending entries: {len(pend)}")

    # 2) live cycles
    cycles = {c["decree_number"]: c for c in
              sb_get_all("decree_open_tracker?select=*&cleared_at=is.null")}

    SHARDED = sharding.enabled()
    if os.environ.get("RUN_ID", "").strip():
        # sharded run: the planner created the ONE decree_check_runs row; open_check_finish.py completes it
        rid = os.environ["RUN_ID"].strip()
        run_id = int(rid) if rid.isdigit() else rid
    else:
        run = sb_post("decree_check_runs", {"trigger_type": TRIGGER_TYPE, "decrees_total": len(pend)},
                      return_rep=True)
        run_id = run[0]["id"] if run else None

    def write_shard_stats(stats, newly, over):
        path = os.environ.get("OPEN_STATS_FILE")
        if SHARDED and path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"checked": len(pend), **stats, "newly_opened_list": newly,
                           "overage_list": over}, fh, ensure_ascii=False)

    # safety net: live cycles whose decree has nothing pending any more
    if not ONLY_DECREE and sharding.SHARD_INDEX == 0:     # once per run, against the FULL pending set
        for d, c in cycles.items():
            if d not in pend:
                sb_patch(f"decree_open_tracker?id=eq.{c['id']}",
                         {"cleared_at": now_iso(), "cleared_reason": "no_pending",
                          "cleared_open": c["smc_status"] == "open", "updated_at": now_iso()})
                log(f"  closed orphan cycle for {d}")

    if SHARDED:
        _total = len(pend)
        pend = {d: g for d, g in pend.items() if sharding.in_shard(d)}
        log(f"{sharding.describe()}: {len(pend)} of {_total} decree(s) belong to this shard.")

    if not pend:
        log("Nothing pending – done.")
        stats0 = dict(open=0, closed=0, newly_opened=0, errors=0, overage=0)
        write_shard_stats(stats0, [], [])
        if run_id and not SHARDED:
            sb_patch(f"decree_check_runs?id=eq.{run_id}", {"finished_at": now_iso()})
        return

    if not login():
        if run_id:
            sb_patch(f"decree_check_runs?id=eq.{run_id}",
                     {"finished_at": now_iso(), "notes": "SMC login failed"})
        sys.exit(1)

    stats = dict(open=0, closed=0, newly_opened=0, errors=0, overage=0)
    newly_opened_list, overage_list = [], []

    for n, (d, g) in enumerate(pend.items(), 1):
        info, err = check_decree(d)
        c = cycles.get(d)
        if c is None:                       # pending but trigger/backfill missed it
            created = sb_post("decree_open_tracker",
                              {"decree_number": d, "patient_id": g["patient_id"],
                               "pending_since": now_iso(), "smc_status": "unchecked"},
                              return_rep=True)
            c = created[0] if created else {"id": None, "first_opened_at": None,
                                            "smc_status": "unchecked", "flap_count": 0}

        upd = {"patient_name": g["name"], "national_id": g["nid"],
               "patient_id": g["patient_id"], "updated_at": now_iso()}

        if not info:
            stats["errors"] += 1
            upd.update({"last_error": err, "smc_status": "not_found" if "not found" in err else c["smc_status"]})
            log(f"[{n}/{len(pend)}] {d}  ⚠ {err}")
        else:
            st = info["status"]
            upd.update({
                "smc_status": st, "decree_value": info["decree_value"],
                "value_left": info["value_left"], "approval_text": info["approval_text"],
                "last_checked_at": now_iso(), "last_error": None,
            })
            if st == "open":
                stats["open"] += 1
                if not c.get("first_opened_at"):
                    upd["first_opened_at"] = now_iso()
                    stats["newly_opened"] += 1
                    newly_opened_list.append(d)
            elif st == "closed":
                stats["closed"] += 1
                upd["last_closed_seen_at"] = now_iso()
                if c.get("first_opened_at"):           # opened before, closed again
                    upd["first_opened_at"] = None
                    upd["flap_count"] = (c.get("flap_count") or 0) + 1

            over = (info["value_left"] is not None and g["value"] > info["value_left"])
            if over:
                stats["overage"] += 1
                overage_list.append((d, g["value"], info["value_left"]))

            log(f"[{n}/{len(pend)}] {d}  {st.upper():6}  left={info['value_left']}  "
                f"pending={g['value']:.2f}" + ("  ⛔ PENDING > VALUE LEFT" if over else ""))

            sb_post("decree_status_checks",
                    {"decree_number": d, "smc_status": st, "decree_value": info["decree_value"],
                     "value_left": info["value_left"], "pending_value": g["value"], "run_id": run_id})

            # reflect the state on the Enhanced Monitor rows
            if st in ("open", "closed"):
                ids = ",".join(map(str, g["order_ids"]))
                sb_patch(f"pending_decree_orders?id=in.({ids})", {"current_status": st})

        if c.get("id"):
            sb_patch(f"decree_open_tracker?id=eq.{c['id']}", upd)
        time.sleep(DELAY)

    write_shard_stats(stats, newly_opened_list, overage_list)
    if run_id and not SHARDED:
        sb_patch(f"decree_check_runs?id=eq.{run_id}", {
            "finished_at": now_iso(), "decrees_open": stats["open"],
            "decrees_closed": stats["closed"], "newly_opened": stats["newly_opened"],
            "errors": stats["errors"], "overage_alerts": stats["overage"]})

    log("\n" + "=" * 64)
    log(f"Checked {len(pend)} | open {stats['open']} | closed {stats['closed']} | "
        f"newly opened {stats['newly_opened']} | errors {stats['errors']} | overage {stats['overage']}")
    log("=" * 64)

    # nice summary in the GitHub Actions run page
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write("### Decree open-status check\n\n"
                    f"| Checked | Open | Closed | Newly opened | Errors | Overage alerts |\n|---|---|---|---|---|---|\n"
                    f"| {len(pend)} | {stats['open']} | {stats['closed']} | {stats['newly_opened']} "
                    f"| {stats['errors']} | {stats['overage']} |\n")
            if newly_opened_list:
                f.write("\n**Newly opened:** " + ", ".join(newly_opened_list) + "\n")
            if overage_list:
                f.write("\n**⛔ Pending value exceeds value left:**\n\n")
                for d, p, l in overage_list:
                    f.write(f"- `{d}` pending {p:,.2f} > left {l:,.2f}\n")

    # a login/parse disaster (nothing readable at all) should fail the run visibly
    if stats["errors"] == len(pend) and len(pend) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
