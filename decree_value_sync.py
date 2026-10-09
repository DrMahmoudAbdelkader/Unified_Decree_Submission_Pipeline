#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decree_value_sync.py
==========================================================================
Fills the TOTAL VALUE (قيمة القرار) of every issued decree.

Runs right AFTER decree_status_and_letters_sync.py (see
decree-value-sync.yml: workflow_run + manual trigger). That script stores
each finally-approved request's decree number; this one takes every decree
number that has no value yet and calls

    POST https://smc.smcegy.com/smc/Decrees/DecreesSearch

with the static payload below (only decreeID changes — the date range does
not affect the result), then reads "قيمة القرار" from the row whose decree
number matches. Writes decree_value on decree_request_attempts and
decree_request_month_seed.

LOGIN
--------------------------------------------------------------------------
Same SMCSession login as the status sync, but a DIFFERENT account:
    SMC_USERNAME_3 / SMC_PASSWORD_3
(falls back to SMC_USERNAME_2 / SMC_PASSWORD_2, then SMC_USERNAME /
SMC_PASSWORD, with a warning — the fallback only matters if you have not
created the dedicated account yet).

OTHER CONFIG
--------------------------------------------------------------------------
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
    MAX_VALUE_LOOKUPS   default 1000 — per-run cap; the rest continues on
                        the next run (never-checked rows first).
Needs decree_number_value_migration.sql applied once.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))

import Unified_Decree_Submission_Pipeline as _pipeline_module
from Unified_Decree_Submission_Pipeline import SMCSession
import supabase_client as sb
import sharding   # SHARD_COUNT/SHARD_INDEX: parallel slices (no-op when unset)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("decree_value_sync")

BASE_URL = _pipeline_module.BASE_URL
SEARCH_URL = f"{BASE_URL}/smc/Decrees/DecreesSearch"
REQUEST_DELAY = 0.3
MAX_ATTEMPTS = 3
MAX_VALUE_LOOKUPS = int(os.environ.get("MAX_VALUE_LOOKUPS", "1000"))

ATTEMPTS_TABLE = "decree_request_attempts"
SEED_TABLE = "decree_request_month_seed"

VALUE_HEADER = "قيمة القرار"

# Everything except decreeID is static (the dates do not change the result).
STATIC_PAYLOAD = {
    "NationalID": "",
    "PatientName": "",
    "dateFrom": "2026-10-02",
    "dateTo": "2026-10-03",
    "Retrieved": "N",
    "decreeStatus": "",
    "decreeSource": "1",
    "stoppedDecree": "N",
    "page": "1",
}

_AR_TRANS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


def _get_smc_credentials():
    for u_key, p_key in (("SMC_USERNAME_3", "SMC_PASSWORD_3"),
                         ("SMC_USERNAME_2", "SMC_PASSWORD_2"),
                         ("SMC_USERNAME", "SMC_PASSWORD")):
        u, p = os.environ.get(u_key), os.environ.get(p_key)
        if u and p:
            if u_key != "SMC_USERNAME_3":
                log.warning(f"SMC_USERNAME_3/SMC_PASSWORD_3 not set — using {u_key} instead.")
            return u, p
    return None, None


def parse_amount(text: str) -> Optional[float]:
    t = (text or "").translate(_AR_TRANS).replace(",", "").replace("\u00a0", "").strip()
    return float(t) if re.fullmatch(r"-?\d+(\.\d+)?", t) else None


def extract_decree_value(html: str, decree_id: str) -> Tuple[str, Optional[float]]:
    """Returns (status, value): ('ok', 1000.0) | ('not_found', None) |
    ('bad_page', None) when the results table isn't in the response at all."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="requestTable")
    if table is None:
        return "bad_page", None
    headers = [th.get_text(strip=True) for th in table.find_all("th")]
    if VALUE_HEADER not in headers:
        return "bad_page", None
    idx = headers.index(VALUE_HEADER)
    for tr in table.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) <= idx:
            continue
        if tds[0].get_text(strip=True) != decree_id:
            continue
        value = parse_amount(tds[idx].get_text(strip=True))
        return ("ok", value) if value is not None else ("not_found", None)
    return "not_found", None


def fetch_decree_value(session_wrapper, decree_id: str) -> Tuple[str, Optional[float]]:
    payload = dict(STATIC_PAYLOAD, decreeID=decree_id)
    headers = {"X-Requested-With": "XMLHttpRequest"}  # same as the site's own ajax call
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = session_wrapper.s.post(SEARCH_URL, data=payload, headers=headers, timeout=30)
            if r.status_code == 200 and "Home/Index" in r.url and "username" in r.text.lower():
                log.warning("  session expired — re-logging in …")
                if not session_wrapper.login():
                    return "failed", None
                r = session_wrapper.s.post(SEARCH_URL, data=payload, headers=headers, timeout=30)
        except Exception as e:
            log.warning(f"  {decree_id}: attempt {attempt}/{MAX_ATTEMPTS} error: {e}")
            time.sleep(REQUEST_DELAY * attempt)
            continue
        if r.status_code == 200:
            status, value = extract_decree_value(r.text, decree_id)
            if status != "bad_page":
                return status, value
        log.warning(f"  {decree_id}: attempt {attempt}/{MAX_ATTEMPTS} — HTTP {r.status_code} / unexpected response")
        time.sleep(REQUEST_DELAY * attempt)
    return "failed", None


def load_targets() -> list:
    """(table, row id, decree number) for every decree that has a number but
    no value yet — never-checked rows first, then oldest-checked."""
    order = "decree_value_checked_at.asc.nullsfirst,id.asc"
    targets = []
    for table in (ATTEMPTS_TABLE, SEED_TABLE):
        room = MAX_VALUE_LOOKUPS - len(targets)
        if room <= 0:
            break
        rows = sb.select(
            table, select="id,decree_number",
            filters={"decree_number": "not.is.null", "decree_value": "is.null"},
            order=order, limit=room,
        )
        targets.extend((table, r["id"], str(r["decree_number"]).strip()) for r in rows)
    # keyed by DECREE NUMBER: the same decree on an attempt row and a seed row stays in one shard (one fetch)
    if sharding.enabled():
        before = len(targets)
        targets = [t for t in targets if sharding.in_shard(t[2])]
        log.info(f"{sharding.describe()}: {len(targets)} of {before} decree(s) belong to this shard.")
    return targets


def main():
    username, password = _get_smc_credentials()
    if not username or not password:
        log.error("No SMC credentials set (SMC_USERNAME_3/SMC_PASSWORD_3) — aborting.")
        sys.exit(1)
    _pipeline_module.USERNAME = username
    _pipeline_module.PASSWORD = password

    try:
        targets = load_targets()
    except RuntimeError as e:
        if "decree_" in str(e):
            log.error("decree_number / decree_value columns missing — run "
                      "decree_number_value_migration.sql once. Aborting.")
            sys.exit(1)
        raise

    counters = {"checked": 0, "filled": 0, "not_found": 0, "failed": 0, "crashed": 0}
    if not targets:
        log.info("No decrees waiting for a value.")
    else:
        session_wrapper = SMCSession()
        if not session_wrapper.login():
            log.error("SMC login failed — aborting.")
            sys.exit(1)
        log.info(f"{len(targets)} decree(s) to value (cap {MAX_VALUE_LOOKUPS}).")

        cache: Dict[str, Tuple[str, Optional[float]]] = {}
        for table, row_id, decree_id in targets:
            counters["checked"] += 1
            try:
                if decree_id not in cache:
                    cache[decree_id] = fetch_decree_value(session_wrapper, decree_id)
                    time.sleep(REQUEST_DELAY)
                status, value = cache[decree_id]
                fields: Dict[str, object] = {"decree_value_checked_at": datetime.now().astimezone().isoformat()}
                if status == "ok":
                    fields["decree_value"] = value
                    counters["filled"] += 1
                    log.info(f"  {decree_id} -> {value}")
                elif status == "not_found":
                    counters["not_found"] += 1
                    log.warning(f"  {decree_id}: not listed in DecreesSearch response")
                else:
                    counters["failed"] += 1
                sb.update(table, row_id, fields)
            except Exception as exc:
                counters["crashed"] += 1
                log.error(f"  {decree_id} raised {type(exc).__name__}: {exc}")

    log.info(f"Done. Checked {counters['checked']}, valued {counters['filled']}, "
             f"not listed {counters['not_found']}, failed {counters['failed']}, crashed {counters['crashed']}.")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(
                f"## Decree values\n"
                f"- Decrees looked up: **{counters['checked']}**\n"
                f"- Values filled: **{counters['filled']}**\n"
                f"- Not listed in DecreesSearch: **{counters['not_found']}**\n"
                f"- Failed after retries: **{counters['failed']}**\n"
                f"- Crashed: **{counters['crashed']}**\n"
            )

    if counters["checked"] and counters["crashed"] > counters["checked"] / 2:
        sys.exit(1)


if __name__ == "__main__":
    main()
