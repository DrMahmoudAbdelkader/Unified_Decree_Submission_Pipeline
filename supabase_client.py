"""
supabase_client.py
==========================================================================
Small, dependency-light PostgREST helper used by both
decree_submission_service.py and request_status_sync.py. Talks to
Supabase with the SERVICE ROLE key (server-side only — never ship this
key to the browser/module), so it bypasses RLS entirely; every write
here is trusted, backend-only code.

Reads SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY from the environment —
in GitHub Actions these come from repository secrets (see
.github/workflows/*.yml), locally from a .env you export yourself.

Call shape (matches how request_status_sync.py and
decree_submission_service.py already use it):

    sb.select(table, select="*", filters={"case_status": "eq.READY_TO_SUBMIT"})
        -> list[dict]

    sb.insert(table, {"col": "value"})
        -> dict (the inserted row, via Prefer: return=representation)

    sb.update(table, row_id, {"col": "value"})
        -> dict (the updated row)   # updates by primary key "id"

    sb.upsert(table, [{"col": "value"}, ...], on_conflict="request_number")
        -> list[dict]
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""

_TIMEOUT = 30


def _check_config():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set — "
            "set them as environment variables (GitHub Actions: repository secrets)."
        )


def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def _raise_for_status(resp: requests.Response):
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"Supabase REST error {resp.status_code} on {resp.url}: {detail}")


def select(table: str, select: str = "*", filters: Optional[Dict[str, str]] = None,
           order: Optional[str] = None, limit: Optional[int] = None) -> List[dict]:
    """filters values must already be PostgREST-encoded, e.g.
    {"case_status": "eq.READY_TO_SUBMIT", "id": "in.(1,2,3)"}."""
    _check_config()
    params = {"select": select}
    if filters:
        params.update(filters)
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = str(limit)
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=_headers(),
                         params=params, timeout=_TIMEOUT)
    _raise_for_status(resp)
    return resp.json() or []


def insert(table: str, row: Dict[str, Any]) -> dict:
    _check_config()
    resp = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=_headers({"Prefer": "return=representation"}),
                          json=row, timeout=_TIMEOUT)
    _raise_for_status(resp)
    data = resp.json() or []
    return data[0] if data else {}


def insert_many(table: str, rows: List[Dict[str, Any]]) -> List[dict]:
    _check_config()
    if not rows:
        return []
    resp = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=_headers({"Prefer": "return=representation"}),
                          json=rows, timeout=_TIMEOUT)
    _raise_for_status(resp)
    return resp.json() or []


def update(table: str, row_id: Any, patch: Dict[str, Any], id_column: str = "id") -> dict:
    _check_config()
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=_headers({"Prefer": "return=representation"}),
        params={id_column: f"eq.{row_id}"},
        json=patch,
        timeout=_TIMEOUT,
    )
    _raise_for_status(resp)
    data = resp.json() or []
    return data[0] if data else {}


def upsert(table: str, rows: List[Dict[str, Any]], on_conflict: str) -> List[dict]:
    """Used by request_status_sync.py's daily export write."""
    _check_config()
    if not rows:
        return []
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=_headers({"Prefer": "resolution=merge-duplicates,return=representation"}),
        params={"on_conflict": on_conflict},
        json=rows,
        timeout=_TIMEOUT,
    )
    _raise_for_status(resp)
    return resp.json() or []


def rpc(function_name: str, body: Optional[Dict[str, Any]] = None) -> Any:
    _check_config()
    resp = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/{function_name}", headers=_headers(),
                          json=body or {}, timeout=_TIMEOUT)
    _raise_for_status(resp)
    return resp.json()
