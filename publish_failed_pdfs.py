#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
publish_failed_pdfs.py
==========================================================================
Keeps the merged submission PDFs of FAILED cases in your repository, so a
failed case can be submitted by hand without redoing sign / report / merge.

WHERE THEY END UP
    branch  : failed-pdfs                       (created automatically)
    folder  : failed_merged_pdfs/<YYYY-MM-DD>/  (the day the case failed)
    files   : case<id>__<national_id>_<mdt_id>.pdf   the ready merged PDF
              case<id>__<national_id>_<mdt_id>.txt   why it failed
    view it : GitHub -> branch dropdown -> failed-pdfs -> failed_merged_pdfs
              https://github.com/<owner>/<repo>/tree/failed-pdfs/failed_merged_pdfs

WHY A SEPARATE BRANCH INSTEAD OF A FOLDER ON main
    Git never forgets. Files committed to main and "deleted" 10 days later
    stay in the repository's history forever - patient documents included -
    and the repo grows with every failure. This branch is instead rebuilt
    as ONE fresh commit on every run (force-pushed), so deleting a file
    really drops it from the branch, and history never accumulates.

RETENTION
    A date folder is removed once it is older than KEEP_DAYS (default 10).
    Every run prunes; cleanup-failed-pdfs.yml also prunes daily and can be
    run by hand (optionally purging everything). You can also just delete
    a folder yourself on GitHub - the next run rebuilds from what is there.

CONCURRENCY
    Parallel batches call this at the same moment. Each run fetches the
    branch, adds its files, and pushes with --force-with-lease pinned to
    the commit it fetched; if another run won the race the push is refused
    and this run starts over from the new tip (jittered retries). No run
    can overwrite another run's files.

ENVIRONMENT
    GITHUB_TOKEN, GITHUB_REPOSITORY   set by the workflow (token needs
                                      `contents: write`)
    SOURCE_DIR       folder holding the new failed PDFs
                     (default /tmp/decree_failed_merged; may be missing/empty)
    KEEP_DAYS        default 10
    PURGE_ALL        1/true -> delete every date folder (manual cleanup)
    FAILED_PDFS_BRANCH   default failed-pdfs
    PUBLISH_REMOTE_URL   testing aid: push to this URL instead of GitHub
    FAILED_PDFS_TODAY    testing aid: pretend today is YYYY-MM-DD
"""

from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

BRANCH = os.environ.get("FAILED_PDFS_BRANCH", "failed-pdfs")
ROOT_DIR = "failed_merged_pdfs"
SOURCE_DIR = os.environ.get("SOURCE_DIR", "/tmp/decree_failed_merged")
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "10") or 10)
PURGE_ALL = os.environ.get("PURGE_ALL", "").strip().lower() in ("1", "true", "yes")
MAX_ATTEMPTS = 12

README = """# Failed merged PDFs

Merged submission PDFs (MDT form + medical report + patient document, already
signed and compressed) for cases that FAILED to submit, kept so they can be
submitted by hand.

- `failed_merged_pdfs/<date>/case<id>__<national_id>_<mdt_id>.pdf` - the file
- `failed_merged_pdfs/<date>/case<id>__<national_id>_<mdt_id>.txt` - why it failed

Folders older than 10 days are removed automatically. This branch is rebuilt
as a single commit on every run; do not expect history here.

Contains patient data - keep the repository private.
"""


def _today() -> date:
    override = os.environ.get("FAILED_PDFS_TODAY")
    if override:
        return date.fromisoformat(override)
    return datetime.now(timezone.utc).date()


def _remote_url() -> str:
    url = os.environ.get("PUBLISH_REMOTE_URL")
    if url:
        return url
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise SystemExit("GITHUB_TOKEN and GITHUB_REPOSITORY are required (or PUBLISH_REMOTE_URL for testing).")
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def _git(work: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=work, capture_output=True, text=True)
    if check and result.returncode != 0:
        # stderr can echo the remote URL (which carries the token) - mask it.
        err = re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", result.stderr or "")
        raise RuntimeError(f"git {args[0]} failed: {err.strip()}")
    return result


def _prune(root: str, today: date) -> int:
    """Removes date folders older than KEEP_DAYS (or all, when purging)."""
    removed = 0
    if not os.path.isdir(root):
        return 0
    cutoff = today - timedelta(days=KEEP_DAYS)
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not (os.path.isdir(path) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", name)):
            continue
        try:
            folder_date = date.fromisoformat(name)
        except ValueError:
            continue
        if PURGE_ALL or folder_date < cutoff:
            shutil.rmtree(path)
            removed += 1
            print(f"  pruned {ROOT_DIR}/{name}")
    return removed


def _add_new_files(root: str, today: date) -> int:
    if not os.path.isdir(SOURCE_DIR):
        return 0
    files = [f for f in sorted(os.listdir(SOURCE_DIR)) if os.path.isfile(os.path.join(SOURCE_DIR, f))]
    if not files:
        return 0
    dest = os.path.join(root, today.isoformat())
    os.makedirs(dest, exist_ok=True)
    for f in files:
        shutil.copyfile(os.path.join(SOURCE_DIR, f), os.path.join(dest, f))
    return len([f for f in files if f.lower().endswith(".pdf")])


def _attempt(url: str, today: date) -> tuple[bool, str]:
    """One fetch -> modify -> push round. Returns (finished, summary);
    finished=False means the push lost a race and the caller should retry."""
    work = tempfile.mkdtemp(prefix="failed_pdfs_")
    try:
        _git(work, "init", "-q")
        _git(work, "config", "user.name", "github-actions[bot]")
        _git(work, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        _git(work, "remote", "add", "origin", url)

        ls = _git(work, "ls-remote", "--heads", "origin", BRANCH)
        tip: Optional[str] = ls.stdout.split()[0] if ls.stdout.strip() else None
        if tip:
            _git(work, "fetch", "-q", "--depth", "1", "origin", BRANCH)
            _git(work, "checkout", "-q", "--detach", "FETCH_HEAD")

        root = os.path.join(work, ROOT_DIR)
        pruned = _prune(root, today)
        added = _add_new_files(root, today)
        with open(os.path.join(work, "README.md"), "w", encoding="utf-8") as fh:
            fh.write(README)

        _git(work, "add", "-A")
        if not _git(work, "status", "--porcelain").stdout.strip():
            return True, "nothing to publish or prune - branch left untouched."

        # Rebuild as a single root commit so history never accumulates and
        # removed files are genuinely gone from the branch.
        _git(work, "checkout", "-q", "--orphan", "publish")
        _git(work, "commit", "-q", "-m", f"failed merged PDFs as of {today.isoformat()}")
        lease = f"--force-with-lease=refs/heads/{BRANCH}:{tip or ''}"
        push = _git(work, "push", "-q", lease, "origin", f"publish:refs/heads/{BRANCH}", check=False)
        if push.returncode != 0:
            return False, "push lost a race with another run"
        return True, f"published {added} new PDF(s), pruned {pruned} old folder(s)."
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    url, today = _remote_url(), _today()
    print(f"Publishing failed merged PDFs from {SOURCE_DIR} to branch '{BRANCH}' "
          f"(keep {KEEP_DAYS} days{', PURGE ALL' if PURGE_ALL else ''}) …")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        finished, summary = _attempt(url, today)
        if finished:
            print(f"  {summary}")
            return 0
        wait = random.uniform(1.0, 4.0)
        print(f"  attempt {attempt}/{MAX_ATTEMPTS}: {summary} - retrying in {wait:.1f}s")
        time.sleep(wait)
    print("  gave up after repeated push races - the failed PDFs are still in this run's "
          "/tmp/decree_failed_merged (and in the run artifact, if enabled).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
