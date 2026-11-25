#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smc_dashboard_snapshot.py
==========================================================================
Takes the daily SNAPSHOT of the SMC (state fund) dashboard — Decrees/Dashboard —
and stores it in audit_smc_snapshots, the table the "Claims <-> Revenue Audit"
module (claims-audit.js) reads as its independent evidence of what has been
claimed from the state fund.

What it does (the same thing you do by hand with the bookmarklet):
  1. logs in with the same SMCSession as the other scripts,
  2. opens Decrees/Dashboard and submits the search form
        dateFrom = audit window start (audit_settings.window_start, default 2025-06-01)
        dateTo   = today (Cairo)           <- the form's own default; never an older day
  3. reads the cards: total invoices, paid count/value, unpaid count/value,
  4. opens every drill-down link (A01 A02 A0 PAID P R01 ...) and sums the
     "عدد الفواتير" / "التكلفة" columns of its summary table,
  5. upserts ONE row per (snapshot_date, hospital_id) — a re-run on the same
     day overwrites that day's row, so it is safe to run twice.

SAFETY (nothing wrong is ever written)
  - no cards / no drill-down values / no paid value  -> exit 1, nothing written
  - the new grand total is more than DROP_GUARD_PCT (default 10 %) BELOW the
    previous snapshot's total -> exit 1, nothing written (the site's cumulative
    figures do not shrink that much; it means a bad page, a different date
    range or a different hospital).  Set FORCE=1 to override after checking.

ENVIRONMENT
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
    SMC_DASHBOARD_USERNAME / SMC_DASHBOARD_PASSWORD   OPTIONAL dedicated account (recommended: SMC logs an account out when it
                       is logged in somewhere else, so sharing SMC_USERNAME_3 with the nightly value-sync can break both)
    SMC_USERNAME_3 / SMC_PASSWORD_3   (falls back to _2, then the default account — same chain as decree_value_sync.py)
    SMC_SENDING_SITE   hospital id, default 102233 (same variable decree_status_and_letters_sync.py uses)
    SNAPSHOT_DATE      optional YYYY-MM-DD override of the stored date (default: today, Cairo)
    DATE_FROM          optional override of the search start (default: audit window start)
    DROP_GUARD_PCT     default 10
    FORCE              "1" = skip the drop guard
    DRY_RUN            "1" = parse and print, write nothing
Needs claims-audit-migration.sql applied once (creates audit_smc_snapshots).
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Dict, Optional
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))

import Unified_Decree_Submission_Pipeline as _pipeline_module
from Unified_Decree_Submission_Pipeline import SMCSession
import supabase_client as sb

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("smc_dashboard_snapshot")

BASE_URL = _pipeline_module.BASE_URL
DASH_URL = f"{BASE_URL}/smc/Decrees/Dashboard"
TABLE = "audit_smc_snapshots"
SETTINGS_TABLE = "audit_settings"
DEFAULT_FROM = "2025-06-01"
HOSPITAL_ID = os.environ.get("SMC_SENDING_SITE", "102233")
KNOWN_FLAGS = {"A01", "A02", "A0", "PAID", "P", "R01"}   # same stages as claims-audit.js SMC_STAGES
REQUEST_DELAY = 0.3
MAX_ATTEMPTS = 3
CAIRO = ZoneInfo("Africa/Cairo")

_AR_TRANS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


# ----------------------------------------------------------------------------
# pure parsing (unit-tested against a real browser capture)
# ----------------------------------------------------------------------------
def to_num(text) -> Optional[float]:
    t = str(text or "").translate(_AR_TRANS).replace(",", "").replace("\u00a0", "").strip()
    return float(t) if re.fullmatch(r"-?\d+(\.\d+)?", t) else None


def _clean(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def _hospital_name(html: str) -> str:
    """The input carries both Value="..." (the real name) and value="" — read the non-empty one from the raw tag."""
    m = re.search(r'<input[^>]*\bid="HospitalName"[^>]*>', html or "", re.I)
    if not m:
        return os.environ.get("SMC_HOSPITAL_NAME", "")
    vals = [htmllib.unescape(v) for v in re.findall(r'\bvalue="([^"]*)"', m.group(0), re.I)]
    return next((v for v in vals if v.strip()), os.environ.get("SMC_HOSPITAL_NAME", ""))


def parse_dashboard(html: str) -> dict:
    """Cards of Decrees/Dashboard: totals, paid/unpaid and the drill-down links {flag: (count, href)}."""
    text = _clean(html)

    def grab(pattern):
        m = re.search(pattern, text)
        return to_num(m.group(1)) if m else None

    soup = BeautifulSoup(html or "", "html.parser")
    links: Dict[str, dict] = {}
    for a in soup.find_all("a", href=re.compile(r"DashboardDrillDown", re.I)):
        href = htmllib.unescape(a.get("href", ""))
        flag = (parse_qs(urlparse(href).query).get("flag") or [""])[0]
        if flag and flag not in links:
            links[flag] = {"card_count": to_num(a.get_text(strip=True)), "href": href}

    def field(name):
        el = soup.find("input", {"name": name})
        return (el.get("value") or "").strip() if el else ""

    return {
        "total_invoices": grab(r"إجمالي عدد فواتير الجهة\s*([\d,]+)"),
        "paid_count": grab(r"عدد الفواتير المسددة\s*([\d,]+)"),
        "paid_value": grab(r"إجمالي قيمة الفواتير المسددة\s*([\d,]+)"),
        "unpaid_count": grab(r"عدد الفواتير الغير مسددة\s*([\d,]+)"),
        "unpaid_value": grab(r"إجمالي قيمة الفواتير الغير مسددة\s*([\d,]+)"),
        "links": links,
        "token": field("__RequestVerificationToken"),
        "hospital_id": field("HospitalID"),
        "hospital_name": _hospital_name(html),
        "date_from": field("dateFrom"),
        "date_to": field("dateTo"),
    }


def parse_sum_page(html: str) -> Optional[dict]:
    """DashboardDrillDown summary page -> {count, value}: sums every hospital row (td[1]=count, td[2]=cost)."""
    soup = BeautifulSoup(html or "", "html.parser")
    count = value = 0.0
    found = False
    for tr in soup.find_all("tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td", recursive=False)]
        if len(tds) >= 3:
            c, v = to_num(tds[1]), to_num(tds[2])
            if c is not None and v is not None:
                count += c
                value += v
                found = True
    return {"count": int(count), "value": round(value, 2)} if found else None


def build_row(dash: dict, stages: Dict[str, dict], snapshot_date: str, date_from: str, date_to: str,
              hospital_id: str) -> dict:
    stage_rows = {k: {"count": v["count"], "value": v["value"]} for k, v in stages.items()}
    return {
        "snapshot_date": snapshot_date,
        "date_from": date_from or None,
        "date_to": date_to or None,
        "hospital_id": hospital_id or "",
        "stages": stage_rows,
        "paid_count": dash["paid_count"],
        "paid_value": dash["paid_value"],
        "unpaid_count": dash["unpaid_count"],
        "unpaid_value": dash["unpaid_value"],
        "total_invoices": dash["total_invoices"],
        "source": "github_action",
        "note": None,
    }


def row_total(row: dict) -> float:
    """Same grand total claims-audit.js smcTotals() computes: review + accounts + unsent + returned + paid."""
    st = row.get("stages") or {}
    v = lambda k: float((st.get(k) or {}).get("value") or 0)
    accounts = (st.get("PAID") or {}).get("value")
    accounts = float(accounts) if accounts is not None else float(row.get("unpaid_value") or 0)
    return v("A01") + v("A02") + v("A0") + accounts + v("P") + v("R01") + float(row.get("paid_value") or 0)


# ----------------------------------------------------------------------------
# network
# ----------------------------------------------------------------------------
def _get_smc_credentials():
    for u_key, p_key in (("SMC_DASHBOARD_USERNAME", "SMC_DASHBOARD_PASSWORD"),
                         ("SMC_USERNAME_3", "SMC_PASSWORD_3"),
                         ("SMC_USERNAME_2", "SMC_PASSWORD_2"),
                         ("SMC_USERNAME", "SMC_PASSWORD")):
        u, p = os.environ.get(u_key), os.environ.get(p_key)
        if u and p:
            if u_key == "SMC_USERNAME_3":
                log.info("Using SMC_USERNAME_3 (shared with the nightly value sync — do not run this job while that chain is running).")
            elif u_key != "SMC_DASHBOARD_USERNAME":
                log.warning(f"SMC_USERNAME_3/SMC_PASSWORD_3 not set — using {u_key} instead.")
            return u, p
    return None, None


class SessionLost(Exception):
    """The site served the logged-out page in the middle of a capture."""


DEBUG_DIR = os.environ.get("DEBUG_DIR", "debug_smc")
_LOGIN_PATHS = {"/smc", "/smc/home/index"}


def _path(u: str) -> str:
    return urlparse(u).path.rstrip("/").lower()


def _snippet(r) -> str:
    soup = BeautifulSoup(r.text or "", "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "")
    return f"HTTP {r.status_code} url={r.url} history={[h.status_code for h in r.history]} title={title!r} body={_clean(r.text)[:200]!r}"


def _dump(name: str, text: str):
    """Keep the raw page of a failed step; the workflow uploads debug_smc/ as an artifact when the job fails."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".html"), "w", encoding="utf-8") as f:
            f.write(text or "")
    except Exception:  # noqa: BLE001
        pass


def _lost(session_wrapper, r, requested_url: str) -> bool:
    if session_wrapper._session_expired(r):          # the pipeline's own test: 302 or the "mcit-logged-out" marker
        return True
    # redirected to the login page from some other page
    return _path(r.url) in _LOGIN_PATHS and _path(requested_url) not in _LOGIN_PATHS


def _request(session_wrapper, method: str, url: str, timeout: int = 60, **kw):
    """One request with retries for transient errors. A logged-out answer raises SessionLost (the caller restarts the whole
    capture with a fresh login — re-sending a single request would use a stale anti-forgery token / lost search state)."""
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = session_wrapper.s.request(method, url, timeout=timeout, allow_redirects=True, **kw)
        except Exception as e:  # noqa: BLE001
            last = str(e)
            log.warning(f"  {method} {url[-70:]}: attempt {attempt}/{MAX_ATTEMPTS} — {last}")
            time.sleep(3 * attempt)
            continue
        if _lost(session_wrapper, r, url):
            _dump(f"lost_{method}_{_path(url).split('/')[-1]}", r.text)
            raise SessionLost(_snippet(r))
        if r.status_code == 200:
            return r
        last = _snippet(r)
        log.warning(f"  {method} {url[-70:]}: attempt {attempt}/{MAX_ATTEMPTS} — {last}")
        time.sleep(3 * attempt)
    raise RuntimeError(f"{method} {url} failed: {last}")


def read_settings() -> dict:
    try:
        rows = sb.select(SETTINGS_TABLE, select="value", filters={"key": "eq.main"}, limit=1)
        return (rows[0].get("value") or {}) if rows else {}
    except Exception as e:  # noqa: BLE001
        if "audit_" in str(e) or "PGRST205" in str(e) or "42P01" in str(e):
            log.error("audit_* tables missing — run claims-audit-migration.sql once. Aborting.")
            sys.exit(1)
        log.warning(f"could not read {SETTINGS_TABLE}: {e}")
        return {}


def capture(session_wrapper, date_from: str, date_to: str) -> tuple:
    """Returns (dashboard dict, {flag: {count, value}}, hospital_id)."""
    page = _request(session_wrapper, "GET", DASH_URL)
    form = parse_dashboard(page.text)
    hospital_id = form["hospital_id"] or HOSPITAL_ID
    if not form["token"]:
        raise RuntimeError("dashboard page has no __RequestVerificationToken — login/page layout changed?")

    payload = {
        "__RequestVerificationToken": form["token"],
        "dateFrom": date_from,
        "dateTo": date_to,
        "HospitalTypes": "1",
        "HospitalName": form["hospital_name"],
        "HospitalID": hospital_id,
    }
    t0 = time.time()
    res = _request(session_wrapper, "POST", DASH_URL, timeout=180, data=payload,
                   headers={"Origin": BASE_URL, "Referer": DASH_URL})     # the search itself takes ~20-30 s on the site
    dash = parse_dashboard(res.text)
    log.info(f"Search answered in {time.time() - t0:.0f}s: cards={ {k: v['card_count'] for k, v in dash['links'].items()} }, "
             f"total={dash['total_invoices']}, paid={dash['paid_value']}")
    if not dash["links"]:
        _dump("search_result", res.text)
        raise RuntimeError("search result has no drill-down links (no cards) — " + _snippet(res))

    stages: Dict[str, dict] = {}
    for flag, info in dash["links"].items():
        href = urljoin(DASH_URL, info["href"])
        r = _request(session_wrapper, "GET", href, headers={"Referer": DASH_URL})
        s = parse_sum_page(r.text)
        if s is None:
            _dump(f"drill_{flag}", r.text)
            log.warning(f"  stage {flag}: no summary row in the drill-down page — skipped. {_snippet(r)}")
            continue
        stages[flag] = s
        if info["card_count"] is not None and int(info["card_count"]) != s["count"]:
            log.warning(f"  stage {flag}: card shows {int(info['card_count'])} invoices, drill-down {s['count']} "
                        f"({int(info['card_count']) - s['count']} without a value) — same as the module's warning")
        time.sleep(REQUEST_DELAY)
    return dash, stages, hospital_id


def previous_total(hospital_id: str, before_date: str) -> Optional[tuple]:
    rows = sb.select(TABLE, select="snapshot_date,stages,paid_value,unpaid_value",
                     filters={"hospital_id": f"eq.{hospital_id}", "snapshot_date": f"lt.{before_date}"},
                     order="snapshot_date.desc", limit=1)
    return (rows[0]["snapshot_date"], row_total(rows[0])) if rows else None


def main():
    username, password = _get_smc_credentials()
    if not username or not password:
        log.error("No SMC credentials set (SMC_USERNAME_3/SMC_PASSWORD_3) — aborting.")
        sys.exit(1)
    _pipeline_module.USERNAME = username
    _pipeline_module.PASSWORD = password

    today = datetime.now(CAIRO).date()
    snapshot_date = (os.environ.get("SNAPSHOT_DATE") or today.isoformat()).strip()
    settings = {} if os.environ.get("DRY_RUN") == "1" and not os.environ.get("SUPABASE_URL") else read_settings()
    date_from = (os.environ.get("DATE_FROM") or settings.get("window_start") or DEFAULT_FROM).strip()
    date_to = today.isoformat()
    log.info(f"Snapshot {snapshot_date}: search {date_from} -> {date_to}, hospital {HOSPITAL_ID}")

    dash = stages = hospital_id = None
    for attempt in range(1, 4):
        session_wrapper = SMCSession()
        if not session_wrapper.login():
            log.error("SMC login failed — aborting.")
            sys.exit(1)
        try:
            dash, stages, hospital_id = capture(session_wrapper, date_from, date_to)
            break
        except SessionLost as e:
            log.warning(f"SMC logged the session out in the middle of the capture (attempt {attempt}/3): {e}\n"
                        "  -> usually the SAME SMC account is logged in somewhere else at the same time "
                        "(nightly status/value sync, another person, a browser). Waiting, then starting over with a fresh login.")
            if attempt == 3:
                log.error("Session lost 3 times — not storing anything. Use a dedicated account (secrets SMC_DASHBOARD_USERNAME/"
                          "SMC_DASHBOARD_PASSWORD) or re-run when nothing else uses the account.")
                sys.exit(1)
            time.sleep(45 * attempt)

    problems = []
    if not stages:
        problems.append("no stage values could be read")
    if dash["paid_value"] is None:
        problems.append("paid total (إجمالي قيمة الفواتير المسددة) not found on the page")
    unknown = [f for f in stages if f not in KNOWN_FLAGS]
    if unknown:
        log.warning(f"stage flag(s) {unknown} are not in the module's known stages — stored, but not counted in its total")
    if problems:
        log.error("Refusing to store the snapshot: " + "; ".join(problems))
        sys.exit(1)

    row = build_row(dash, stages, snapshot_date, date_from, date_to, hospital_id)
    total = row_total(row)
    log.info("Stages: " + ", ".join(f"{k}={v['count']}/{v['value']:,.0f}" for k, v in stages.items()))
    log.info(f"paid={row['paid_value']:,.0f} ({row['paid_count']})  unpaid={row['unpaid_value']}  "
             f"cards total={row['total_invoices']}  GRAND TOTAL={total:,.2f}")

    prev = None
    if os.environ.get("DRY_RUN") != "1":
        prev = previous_total(hospital_id, snapshot_date)
    if prev:
        prev_date, prev_total = prev
        guard = float(os.environ.get("DROP_GUARD_PCT", "10")) / 100
        change = total - prev_total
        log.info(f"Change since {prev_date}: {change:+,.2f}")
        if prev_total > 0 and total < prev_total * (1 - guard) and os.environ.get("FORCE") != "1":
            log.error(f"Grand total {total:,.0f} is more than {guard:.0%} below the previous snapshot "
                      f"({prev_date}: {prev_total:,.0f}) — not stored. Check the page/date range, then re-run with FORCE=1.")
            sys.exit(1)

    if os.environ.get("DRY_RUN") == "1":
        log.info("DRY_RUN — nothing written.")
        print(json.dumps(row, ensure_ascii=False, indent=1))
    else:
        sb.upsert(TABLE, [row], on_conflict="snapshot_date,hospital_id")
        log.info(f"Stored snapshot {snapshot_date} in {TABLE}.")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(
                f"## SMC dashboard snapshot — {snapshot_date}\n"
                f"- Search range: **{date_from} → {date_to}**\n"
                f"- Grand total claimed from the state fund: **{total:,.0f}**\n"
                f"- Paid: **{row['paid_value']:,.0f}** · in review: "
                f"**{sum((stages.get(k) or {}).get('value', 0) for k in ('A01', 'A02', 'A0')):,.0f}** · "
                f"returned: **{(stages.get('R01') or {}).get('value', 0):,.0f}**\n"
                + (f"- Change since {prev[0]}: **{total - prev[1]:+,.0f}**\n" if prev else "- First snapshot — nothing to compare with.\n")
            )


if __name__ == "__main__":
    main()
