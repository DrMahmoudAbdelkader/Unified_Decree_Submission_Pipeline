#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topup_manual_bill.py
==========================================================================
The simple "كشف حساب" page attached after the invoices when the previous decree still had dispensings
waiting to be billed in Enhanced Monitor (pending_decree_orders / pending_decree_items). Documentation only —
it is never uploaded back to SMC on its own, it is just one more page of the merged request PDF.

Layout follows Decree_Bill_Template.pdf (logo, addressee, «كشف حساب» heading, patient line, period), with the
category table replaced by:   date | description | value   -> one row per extracted invoice, one row for the
unbilled dispensings (with its date), and the total.  The template has no national-ID field, so the ID is
printed on its own line under the patient name.

    collect(candidate_id)            -> dict | None   (None = nothing unbilled -> no bill)
    render_pdf(bill)                 -> bytes (one Letter page, same size as the template)
    bill_pdf_for_candidate(cid)      -> bytes | None
"""
from __future__ import annotations

import base64
import html as _html
import logging
import os
import re
from datetime import date, datetime
from typing import List, Optional

import requests

log = logging.getLogger("topup_manual_bill")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""

LOGO_B64 = "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"


def _h():
    return {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}


def _get(path: str, params: dict):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=_h(), params=params, timeout=60)
    r.raise_for_status()
    return r.json() or []


def _money(x: float) -> str:
    return f"{x:,.2f}".replace(",", "")


def _date_key(s: str):
    """Sort key for dates written as yyyy-mm-dd, dd/mm/yyyy or dd-mm-yyyy; unknown -> very old."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return date.min


def _pending_orders(decree_number: str) -> List[dict]:
    """Open Enhanced-Monitor orders of this decree: [{'date': 'YYYY-MM-DD'|None, 'value': float}]."""
    for sel in ("id,created_at,items:pending_decree_items(total_value)", "id,items:pending_decree_items(total_value)"):
        try:
            rows = _get("pending_decree_orders", {"select": sel, "decree_number": f"eq.{decree_number}",
                                                   "status": "neq.completed"})
            out = []
            for o in rows:
                v = sum(float(i.get("total_value") or 0) for i in (o.get("items") or []))
                if v > 0:
                    out.append({"date": (o.get("created_at") or "")[:10] or None, "value": v})
            return out
        except Exception as e:                      # created_at missing -> retry without the date
            log.warning(f"pending orders lookup ({sel[:12]}...) failed: {e}")
    return []


def collect(candidate_id: int) -> Optional[dict]:
    """Bill data for a candidate, or None when no previous decree used for the invoices has unbilled value."""
    cand = _get("decree_topup_candidates", {"select": "id,patient_id", "id": f"eq.{candidate_id}", "limit": 1})
    if not cand:
        return None
    inv = _get("decree_topup_invoices", {
        "select": "decree_number,receipt_id,verification,doc_kind", "candidate_id": f"eq.{candidate_id}",
        "status": "eq.OK", "r2_key": "not.is.null", "order": "decree_number.asc,id.asc"})
    inv = [i for i in inv if (i.get("doc_kind") or "INVOICE") != "PREV_REPORT"]
    if not inv:
        return None
    decrees = list(dict.fromkeys(i["decree_number"] for i in inv))

    unbilled = []
    for d in decrees:
        for o in _pending_orders(d):
            unbilled.append({**o, "decree": d})
    if not unbilled:
        return None

    lines = []
    for i in inv:
        li = ((i.get("verification") or {}).get("listing") or {})
        lines.append({"date": str(li.get("reg_date") or "").strip(), "receipt": i["receipt_id"],
                      "value": float(li["amount"]) if li.get("amount") not in (None, "") else None})
    lines.sort(key=lambda x: _date_key(x["date"]))

    pat = _get("patients", {"select": "*", "id": f"eq.{cand[0]['patient_id']}", "limit": 1})
    p = pat[0] if pat else {}
    name = p.get("name") or p.get("full_name") or p.get("patient_name") or ""
    return {"patient_name": name, "national_id": str(p.get("national_id") or ""),
            "invoices": lines, "unbilled": unbilled}


def render_pdf(bill: dict) -> bytes:
    inv, unb = bill["invoices"], bill["unbilled"]
    e = _html.escape
    inv_total = sum(x["value"] or 0 for x in inv)
    unb_total = sum(x["value"] for x in unb)
    grand = inv_total + unb_total
    dates = [x["date"] for x in inv if x["date"]] + [x["date"] for x in unb if x["date"]]
    dates.sort(key=_date_key)
    period = (f"<div class='row'>عن الفترة من <b><bdi dir='ltr'>{e(dates[0])}</bdi></b> &nbsp; إلى <b><bdi dir='ltr'>{e(dates[-1])}</bdi></b></div>"
              if dates else "")
    rows = ""
    for x in inv:
        v = _money(x["value"]) if x["value"] is not None else "—"
        rows += f"<tr><td><bdi dir='ltr'>{e(x['date'] or '—')}</bdi></td><td>فاتورة رقم {e(str(x['receipt']))}</td><td>{v} جنيه</td></tr>"
    for x in unb:
        rows += (f"<tr class='unb'><td><bdi dir='ltr'>{e(x['date'] or '—')}</bdi></td>"
                 f"<td>قيمة صرف لم تتم فوترتها بعد (قرار {e(str(x['decree']))})</td><td>{_money(x['value'])} جنيه</td></tr>")
    doc = f"""<!doctype html><html dir="rtl" lang="ar"><head><meta charset="utf-8"><style>
@page {{ size: Letter; margin: 0 }}
body {{ margin: 0; font-family: Arial, Tahoma, 'Amiri', 'DejaVu Sans', sans-serif; color:#000; }}
.page {{ width: 8.5in; box-sizing: border-box; padding: 0.45in 0.9in 0.6in 0.9in; }}
.logo {{ direction: ltr; text-align: left; }} .logo img {{ height: 0.75in }}
h2 {{ font-size: 17pt; margin: 0.35in 0 0.18in 0; font-weight: 700 }}
.row {{ font-size: 12pt; margin: 0.12in 0 }} .row b {{ font-weight: 700 }}
table {{ width: 100%; border-collapse: collapse; margin-top: 0.3in; font-size: 11.5pt }}
th, td {{ border: 1px solid #000; padding: 7px 8px; text-align: center }}
th {{ font-weight: 700 }} tr.unb td {{ font-weight: 700 }}
.tot td {{ font-weight: 700; font-size: 12.5pt }}
.note {{ font-size: 10pt; margin-top: 0.25in }}
</style></head><body><div class="page">
<div class="logo"><img src="data:image/jpeg;base64,{LOGO_B64}"></div>
<h2>السيد/ مدير عام المجالس الطبيه</h2>
<div class="row"><b>تحيه طيبه وبعد ؛؛</b></div>
<div class="row"><b>مقدم لسيادتكم كشف حساب</b></div>
<div class="row"><b>بيان بمدفوعات المريض / {e(bill['patient_name'])}</b></div>
<div class="row"><b>الرقم القومي / {e(bill['national_id'])}</b></div>
{period}
<table><thead><tr><th style="width:24%">التاريخ</th><th>البيان</th><th style="width:24%">القيمة</th></tr></thead><tbody>
{rows}
<tr class="tot"><td colspan="2">الإجمالي</td><td>{_money(grand)} جنيه</td></tr></tbody></table>
</div></body></html>"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        try:
            pg = br.new_page()
            pg.set_content(doc, wait_until="load")
            return pg.pdf(width="8.5in", height="11in", print_background=True, prefer_css_page_size=True)
        finally:
            br.close()


def bill_pdf_for_candidate(candidate_id: int) -> Optional[bytes]:
    bill = collect(candidate_id)
    if not bill:
        return None
    log.info(f"  [manual bill] candidate {candidate_id}: {len(bill['invoices'])} invoice(s) + "
             f"{len(bill['unbilled'])} unbilled order(s)")
    return render_pdf(bill)
