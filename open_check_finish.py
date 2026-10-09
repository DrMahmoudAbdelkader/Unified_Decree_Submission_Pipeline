#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open_check_finish.py - last job of decree-open-check.yml.

Every shard of smc_decree_open_checker.py writes its counters to
$OPEN_STATS_DIR/shard-<n>.json (uploaded as an artifact). This script adds them
up and completes the ONE decree_check_runs row the planner created, so the
tracker page still sees one run per day with the combined numbers.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, RUN_ID, OPEN_STATS_DIR, EXPECTED_SHARDS
"""
import glob, json, os, sys
from datetime import datetime, timezone

import requests

url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
key = os.environ.get("SUPABASE_SERVICE_KEY") or ""
run_id = (os.environ.get("RUN_ID") or "").strip()
d = os.environ.get("OPEN_STATS_DIR") or "open-stats"
expected = int(os.environ.get("EXPECTED_SHARDS") or "0")

files = sorted(glob.glob(os.path.join(d, "*.json")))
tot = dict(checked=0, open=0, closed=0, newly_opened=0, errors=0, overage=0)
newly, over = [], []
for f in files:
    s = json.load(open(f, encoding="utf-8"))
    for k in tot:
        tot[k] += int(s.get(k, 0))
    newly += s.get("newly_opened_list", [])
    over += s.get("overage_list", [])
missing = max(0, expected - len(files))
print(f"{len(files)}/{expected} shard report(s): {tot}")

summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a", encoding="utf-8") as f:
        f.write("### Decree open-status check (all shards)\n\n"
                "| Checked | Open | Closed | Newly opened | Errors | Overage alerts |\n|---|---|---|---|---|---|\n"
                f"| {tot['checked']} | {tot['open']} | {tot['closed']} | {tot['newly_opened']} "
                f"| {tot['errors']} | {tot['overage']} |\n")
        if newly:
            f.write("\n**Newly opened:** " + ", ".join(newly) + "\n")
        for dn, p, l in over:
            f.write(f"- ⛔ `{dn}` pending {p:,.2f} > left {l:,.2f}\n")
        if missing:
            f.write(f"\n> ⚠️ {missing} shard(s) produced no report (crashed or cancelled) - their decrees were not counted.\n")

if run_id:
    body = {"finished_at": datetime.now(timezone.utc).isoformat(), "decrees_open": tot["open"],
            "decrees_closed": tot["closed"], "newly_opened": tot["newly_opened"],
            "errors": tot["errors"], "overage_alerts": tot["overage"]}
    if missing:
        body["notes"] = f"{missing} of {expected} shard(s) reported nothing"
    r = requests.patch(f"{url}/rest/v1/decree_check_runs", params={"id": f"eq.{run_id}"}, json=body,
                       headers={"apikey": key, "Authorization": f"Bearer {key}", "Prefer": "return=minimal"},
                       timeout=60)
    r.raise_for_status()
if missing:
    sys.exit(1)
