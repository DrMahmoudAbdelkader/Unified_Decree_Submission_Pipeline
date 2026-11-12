#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
supabase_bulk.py
==========================================================================
Tiny helpers that replace ROW-BY-ROW REST calls with a few BATCHED ones.

WHY: every PostgREST call is one API-gateway log line (log ingestion) and, from a browser,
also a CORS preflight. A nightly script that does 3 writes for each of 2,900 decrees writes
~9,000 log lines; the same work done in batches of 150-500 rows writes a few dozen.

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SERVICE_KEY) from the environment, so it
works in every workflow without extra secrets.

    get_in(table, column, values, select="*", extra=None)   rows where column IN (values)   (chunked + paged)
    patch_in(table, column, values, payload)                ONE payload applied to many rows (chunked)
    post_many(table, rows)                                  bulk insert (chunked, return=minimal)
    EventQueue(table, flush_at=100)                         .add(row) ... .flush()   (buffered inserts)
    StampQueue(table, column, payload_fn)                   .add(id) ... .flush()    (buffered "touch" updates)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Dict, Iterable, List, Optional

import requests

log = logging.getLogger("supabase_bulk")
_S = requests.Session()


def _cfg():
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    return url, key


def chunks(seq: List, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _req(method: str, path: str, **kw) -> requests.Response:
    url, key = _cfg()
    hdr = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    hdr.update(kw.pop("headers", None) or {})
    last = None
    for attempt in range(1, 4):                       # 3 tries for network errors / 5xx / 429
        try:
            r = _S.request(method, f"{url}/rest/v1/{path}", headers=hdr, timeout=90, **kw)
        except requests.RequestException as e:
            last = e
            time.sleep(1.5 * attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = RuntimeError(f"{method} {path[:80]} -> {r.status_code}")
            time.sleep(1.5 * attempt)
            continue
        if not r.ok:
            raise RuntimeError(f"{method} {path[:80]} -> {r.status_code}: {r.text[:200]}")
        return r
    raise RuntimeError(f"{method} {path[:80]} failed after 3 tries: {last}")


def _in_list(values: Iterable, quote: bool) -> str:
    out = []
    for v in values:
        s = str(v)
        out.append('"' + s.replace('"', '\\"') + '"' if quote else s)
    return "in.(" + ",".join(out) + ")"


def get_in(table: str, column: str, values: Iterable, select: str = "*", extra: Optional[Dict[str, str]] = None,
           chunk: int = 150, quote: bool = False, order: str = "id.asc") -> List[dict]:
    """Rows of `table` whose `column` is in `values`. One request per `chunk` values (more only when a chunk returns
    >1000 rows; those are paged with a stable `order`, default id.asc - pass another column if the table has no id).
    Use quote=True for text values."""
    vals = [v for v in dict.fromkeys(values) if v is not None]
    out: List[dict] = []
    for part in chunks(vals, chunk):
        params = {"select": select, column: _in_list(part, quote), "order": order}
        if extra:
            params.update(extra)
        offset = 0
        while True:
            rows = _req("GET", table, params=dict(params, limit=1000, offset=offset)).json() or []
            out.extend(rows)
            if len(rows) < 1000:
                break
            offset += 1000
    return out


def patch_in(table: str, column: str, values: Iterable, payload: dict, chunk: int = 150, quote: bool = False) -> int:
    """Apply ONE payload to every row whose `column` is in `values`. Returns how many ids were sent."""
    vals = [v for v in dict.fromkeys(values) if v is not None]
    for part in chunks(vals, chunk):
        _req("PATCH", table, params={column: _in_list(part, quote)}, json=payload,
             headers={"Prefer": "return=minimal"})
    return len(vals)


def post_many(table: str, rows: List[dict], chunk: int = 500) -> int:
    """Bulk insert. return=minimal (no rows echoed back = no egress); missing=default keeps column defaults."""
    for part in chunks(rows, chunk):
        _req("POST", table, json=part, headers={"Prefer": "return=minimal,missing=default"})
    return len(rows)


class EventQueue:
    """Buffer inserts and send them in bulk. Call flush() at the end (also in a finally block)."""

    def __init__(self, table: str, flush_at: int = 100):
        self.table, self.flush_at, self.rows = table, flush_at, []

    def add(self, row: dict) -> None:
        self.rows.append(row)
        if len(self.rows) >= self.flush_at:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        rows, self.rows = self.rows, []
        try:
            post_many(self.table, rows)
        except Exception as e:                        # never lose the rows silently: fall back to smaller pieces
            log.warning(f"bulk insert into {self.table} failed ({e}) - retrying row groups of 20")
            for part in chunks(rows, 20):
                try:
                    post_many(self.table, part)
                except Exception as e2:
                    log.error(f"insert into {self.table} dropped {len(part)} row(s): {e2}")


class StampQueue:
    """Buffer row ids whose ONLY change is a 'checked now' stamp; one PATCH per chunk instead of one per row."""

    def __init__(self, table: str, payload_fn: Callable[[], dict], column: str = "id", flush_at: int = 300):
        self.table, self.payload_fn, self.column, self.flush_at, self.ids = table, payload_fn, column, flush_at, []

    def add(self, row_id) -> None:
        self.ids.append(row_id)
        if len(self.ids) >= self.flush_at:
            self.flush()

    def flush(self) -> None:
        if not self.ids:
            return
        ids, self.ids = self.ids, []
        patch_in(self.table, self.column, ids, self.payload_fn())
