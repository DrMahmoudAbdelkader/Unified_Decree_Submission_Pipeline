# -*- coding: utf-8 -*-
"""
sf_receipt_sync.py — DASHBOARD-driven daily feed of the line-level State-fund ledger.

Why: the ledger (audit_sf_service_lines) used to be fed only from dispensed-items exports that somebody had to produce.
This script reads the new receipts straight from the SMC dashboard, the same way it is done by hand:

    dashboard card  P (pending, not yet sent)  [+ R01 returned to the hospital]
        -> decree number + national id of every row                     (DashbaordDetailedDrillDown, 50 rows / page)
        -> the decree's receipt list: receipt id, status, TOTAL VALUE   (POST HospDecreeReceipts/GetHospitalDecreeReceipts)
        -> only when needed: the receipt page with its item rows        (HospDecreeReceipts/Details/<receipt id>)
    item rows -> same columns as the export (ID_Number, Decree_Number, Receipt_ID, Item_Name, Quantity, Unit, Price,
                 Dispensing_Date, Notes) -> the SAME allocation / key / hash code as sf_ledger_incremental.py -> audit_sf_service_lines

The rule for every receipt (registry table audit_sf_receipts)
    never seen, and the ledger has no lines for it ........ NEW        -> read its items, store the lines
    never seen, ledger already has it, same total ......... SEEDED     -> nothing to read (it came with the old exports)
    seen before, same total ................................ SAME       -> ignored (status change only is logged)
    seen before, DIFFERENT total ........................... EDITED     -> read the items again; lines are reconciled:
                                                                          new items added, changed items updated, items that are
                                                                          gone from the receipt are marked removed (kept for audit)
    seen before, gone from its decree's receipt list ....... REMOVED    -> its lines are marked removed
    marked removed before, back in the list ................ REAPPEARED -> read again
Because the state is kept in the database, a run that stops half way simply continues on the next run (nothing to resume by hand).

Needs: sf_ledger_incremental.py + sf_service_ledger.py in the same folder, requests, beautifulsoup4, pandas.
Env:   SMC_RECEIPTS_USERNAME / SMC_RECEIPTS_PASSWORD   (or SMC_DASHBOARD_USERNAME/PASSWORD, or SMC_USERNAME_3 / SMC_PASSWORD_3)
       SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY         (not needed with --store local)
       SMC_BASE_URL (default https://smc.smcegy.com), SMC_HOSPITAL_ID (default 102233), SMC_HOSPITAL_TYPES (default 1)

Examples
  python sf_receipt_sync.py --plan-only                     # which decrees / receipts WOULD be read, nothing fetched beyond the lists
  python sf_receipt_sync.py --dry-run --report actions.csv  # reads everything, shows the line diff, writes nothing
  python sf_receipt_sync.py                                  # the daily run
  python sf_receipt_sync.py --deep                           # ignore the decree-level shortcut (weekly safety net; also automatic every 7 days)
  python sf_receipt_sync.py --cards P,R01,A02                # also follow other cards
"""
import argparse, datetime as dt, hashlib, json, os, re, sys, time
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sf_ledger_incremental as L          # load_lines / build_pieces / MemoryStore / SupabaseStore (same allocation rules as the ledger)

BASE_URL_DEFAULT = 'https://smc.smcegy.com'
FINAL_STATUSES = {'M1'}            # receipts in these statuses are no longer followed by the "departed" sweep (override: --final-statuses)
EXPORT_COLS = ['ID_Number', 'Decree_Number', 'Receipt_ID', 'Item_Name', 'Quantity', 'Unit', 'Price', 'Dispensing_Date', 'Notes']

_AR = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')


# ═════════════════════════════ small helpers ═════════════════════════════
def nd(s):
    """Arabic-Indic / Persian digits -> latin; non-breaking spaces -> space."""
    return (s or '').translate(_AR).replace('\xa0', ' ')

def num(s):
    m = re.search(r'-?\d+(?:\.\d+)?', nd(str(s)).replace(',', ''))
    return float(m.group()) if m else None

def digits(s):
    return re.sub(r'\D', '', nd(s or ''))

def txt(el):
    return re.sub(r'\s+', ' ', nd(el.get_text(' ', strip=True))).strip() if el is not None else ''

def ar(s):
    """for matching Arabic labels: digits normalised and the decorative kashida (ـ) removed (the receipt page writes 'بيــــان')."""
    return nd(s).replace('ـ', '')

def us_datetime(s):
    s = nd(s).strip()
    for f in ('%m/%d/%Y %I:%M:%S %p', '%m/%d/%Y %H:%M:%S', '%m/%d/%Y'):
        try: return dt.datetime.strptime(s, f).isoformat()
        except ValueError: pass
    return None

def cairo_today():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo('Africa/Cairo')).date()
    except Exception:
        return dt.date.today()

def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n): yield seq[i:i + n]


# ═════════════════════════════ HTML parsers (tested on the real pages of the HAR) ═════════════════════════════
def parse_card_page(html):
    """DashbaordDetailedDrillDown page -> {'rows': [...], 'page': n, 'pages': n, 'total': n}
    row = decree, patient_name, patient_id (national id), decree_date, decree_value, invoice_value."""
    soup = BeautifulSoup(html, 'html.parser')
    text = nd(soup.get_text(' '))
    m = re.search(r'Page\s+(\d+)\s+of\s+(\d+)', text); t = re.search(r'Total:\s*(\d+)', text)
    rows = []
    for tr in soup.find_all('tr'):
        tds = tr.find_all('td')
        if len(tds) < 6 or tr.find('th'): continue
        v = [txt(td) for td in tds]
        dec = digits(v[0])
        if not dec: continue
        rows.append({'decree': dec, 'patient_name': v[1], 'patient_id': digits(v[2]), 'decree_date': v[3],
                     'decree_value': num(v[4]), 'invoice_value': num(v[5])})
    return {'rows': rows, 'page': int(m.group(1)) if m else 1, 'pages': int(m.group(2)) if m else 1,
            'total': int(t.group(1)) if t else len(rows)}

def parse_card_summary(html):
    """DashboardDrillDown (summary) page -> (invoice count, cost) from the 'الإجمالي' row, or None."""
    soup = BeautifulSoup(html, 'html.parser')
    for tr in soup.find_all('tr'):
        ths = [txt(x) for x in tr.find_all('th')]
        if len(ths) >= 3 and 'الإجمالي' in ar(ths[0]):
            return int(num(ths[1]) or 0), num(ths[2])
    return None

def parse_receipts_list(html):
    """POST GetHospitalDecreeReceipts -> list of receipts of the decree, or None when the table is missing
    (login page / error page: NEVER treat that as 'no receipts')."""
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table', id='hospDecreeReceiptTblId')
    if table is None: return None
    out = []
    for tr in table.find_all('tr', class_='item'):
        def hid(cls):
            i = tr.find('input', class_=cls); return (i.get('value') or '').strip() if i else ''
        tds = tr.find_all('td')
        if len(tds) < 8: continue
        rid = digits(hid('ID')) or digits(txt(tds[0]))
        val = num(txt(tds[6]))
        if not rid or val is None: continue
        out.append({'receipt_id': rid, 'decree': digits(hid('DECREEID')), 'claim_id': hid('CLAIMID'), 'status': hid('STATUSID'),
                    'status_text': txt(tds[3]), 'registered_at': us_datetime(txt(tds[2])), 'value': val,
                    'claim_no': digits(txt(tds[7])), 'notes': txt(tds[4]), 'editable': bool(txt(tds[5]))})
    return out

def parse_receipt_details(html):
    """HospDecreeReceipts/Details/<id> -> header + item rows, or None when this is not a receipt page."""
    soup = BeautifulSoup(html, 'html.parser')
    text = re.sub(r'\s+', ' ', nd(soup.get_text(' ')))
    text = ar(text)
    m = re.search(r'فاتورة علاج رقم\s*(\d+)', text)
    if not m: return None
    h = {'receipt_no': m.group(1)}
    x = re.search(r'الرقم القومي\s*(\d{10,})', text); h['patient_id'] = x.group(1) if x else ''
    x = re.search(r'قرار وزاري رقم\s*(\d+)\s*بتاريخ\s*([\d\-/]+)\s*بمبلغ\s*([\d.]+)', text)
    h['decree'], h['decree_date'], h['decree_value'] = (x.group(1), x.group(2), float(x.group(3))) if x else ('', '', None)
    x = re.search(r'تاريخ الفاتورة\s*([\d\-/]+)', text); h['invoice_date'] = x.group(1) if x else ''
    x = re.search(r'الخصم \(%\)\s*([\d.]+)', text); h['discount'] = float(x.group(1)) if x else None
    items, total = [], None
    table = None
    for t in soup.find_all('table'):
        if any('بيان' in ar(txt(th)) for th in t.find_all('th')): table = t; break
    if table is None: table = next((t for t in soup.find_all('table') if t.find('td')), None)
    if table is not None:
        for tr in table.find_all('tr'):
            tds = tr.find_all('td')
            if tr.find('th'): continue
            if 'totalCls' in (tr.get('class') or []) or (tds and 'الاجمالي' in ar(txt(tr))):
                total = num(txt(tds[0])) if tds else None; continue
            if len(tds) < 5: continue
            price = num(txt(tds[0])); name = txt(tds[1])
            if price is None or not name: continue
            q = txt(tds[2]); qty = re.sub(r'[^\d.]', '', q) if re.search(r'\d', q) else '1'
            items.append({'Item_Name': name, 'Quantity': qty or '1', 'Unit': txt(tds[3]) or 'N/A', 'Price': price,
                          'Dispensing_Date': txt(tds[4]), 'Notes': txt(tds[5]) if len(tds) > 5 else ''})
    h['items'] = items; h['total'] = total
    return h


# ═════════════════════════════ SMC session (login, expired-session repair, retries) ═════════════════════════════
class FetchFailed(Exception):
    """A request could not be completed. Never interpreted as 'no data'; stops the run so the next run continues."""

_LOGIN_PATH = '/smc/home/index'
_UF = re.compile(r'name\s*=\s*["\']username["\']', re.I); _PF = re.compile(r'name\s*=\s*["\']password["\']', re.I)

def looks_logged_out(resp):
    from urllib.parse import urlparse
    try:
        p = urlparse(resp.url)
        if p.path.lower().rstrip('/') == _LOGIN_PATH or 'returnurl=' in (p.query or '').lower(): return True
        ct = (resp.headers.get('Content-Type') or '').lower()
        if 'html' in ct or not ct:
            head = resp.text[:300000]
            if _UF.search(head) and _PF.search(head): return True
    except Exception:
        pass
    return False

class SMC:
    def __init__(self, base, user, pwd, hospital_id, hospital_types, sleep=0.3, max_wait=900, http=None):
        self.base, self.user, self.pwd = base.rstrip('/'), user, pwd
        self.hid, self.htype, self.sleep, self.max_wait = str(hospital_id), str(hospital_types), sleep, max_wait
        self.http = http or requests.Session()
        self.http.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                                  'X-Requested-With': 'XMLHttpRequest', 'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8',
                                  'Referer': f'{self.base}/smc/Home/Index', 'Origin': self.base})
        self.relogins = 0; self.requests_made = 0

    def login(self):
        r = self.http.get(f'{self.base}/smc/Home/Index', timeout=30)
        if r.status_code != 200: return False
        tok = BeautifulSoup(r.text, 'html.parser').find('input', {'name': '__RequestVerificationToken'})
        data = {'username': self.user, 'password': self.pwd}
        if tok and tok.get('value'): data['__RequestVerificationToken'] = tok['value']
        r = self.http.post(f'{self.base}/smc/Home/Index', data=data, timeout=30, allow_redirects=True)
        return 'logout' in r.text.lower() or 'dashboard' in r.text.lower()

    def login_retry(self, attempts=5):
        for i in range(1, attempts + 1):
            try:
                if self.login(): return True
            except requests.RequestException as e:
                print(f'  login attempt {i} failed: {e}', flush=True)
            time.sleep(min(30, 5 * i))
        return False

    def request(self, method, path, **kw):
        url = path if path.startswith('http') else self.base + path
        kw.setdefault('timeout', 30)
        t0 = None; delay = 2; restores = 0; srv = 0
        while True:
            problem = ''
            try:
                self.requests_made += 1
                r = self.http.request(method, url, **kw)
            except requests.RequestException as e:
                r = None; problem = f'{type(e).__name__}: {str(e)[:100]}'
            if r is not None:
                out = looks_logged_out(r)
                if out or r.status_code in (401, 403):
                    if r.status_code in (401, 403) and not out and restores >= 1: return r
                    restores += 1
                    if restores > 3: raise FetchFailed(f'still sent to the login page after logging in again ({url}) — check the account / single-session limit')
                    problem = 'session expired'
                    self.http.cookies.clear()
                    if self.login_retry(3): self.relogin_done(); continue
                elif r.status_code >= 500 or r.status_code == 429:
                    srv += 1; problem = f'HTTP {r.status_code}'
                    if srv > 4: return r
                else:
                    time.sleep(self.sleep); return r
            t0 = t0 or time.time()
            if time.time() - t0 > self.max_wait: raise FetchFailed(f'{method} {url} kept failing ({problem})')
            print(f'  request problem ({problem}) — retrying in {delay}s', flush=True)
            time.sleep(delay); delay = min(30, delay * 2)

    def relogin_done(self): self.relogins += 1

    # —— the four calls of the manual flow ——
    def _qs(self, flag, d_from, d_to, page=None):
        f = lambda d: quote(f'{d.month:02d}/{d.day:02d}/{d.year} 00:00:00', safe='')
        q = f'dateFrom={f(d_from)}&dateTo={f(d_to)}&flag={flag}&HospitalID={self.hid}&HospitalTypes={self.htype}'
        return q + (f'&page={page}' if page else '')

    def card_summary(self, flag, d_from, d_to):
        r = self.request('GET', '/smc/Decrees/DashboardDrillDown?' + self._qs(flag, d_from, d_to))
        return parse_card_summary(r.text) if r.status_code == 200 else None

    def card_rows(self, flag, d_from, d_to):
        rows, total, page, pages = [], 0, 1, 1
        while page <= pages:
            r = self.request('GET', '/smc/Decrees/DashbaordDetailedDrillDown?' + self._qs(flag, d_from, d_to, page if page > 1 else None))
            if r.status_code != 200: raise FetchFailed(f'card {flag} page {page}: HTTP {r.status_code}')
            p = parse_card_page(r.text)
            if page == 1: pages, total = p['pages'], p['total']
            rows += p['rows']; page += 1
        return rows, total

    def receipts_list(self, decree):
        r = self.request('POST', '/smc//HospDecreeReceipts/GetHospitalDecreeReceipts', data={'decreeId': decree},
                         headers={'Referer': f'{self.base}/smc/HospDecreeReceipts/Create?decreeId={decree}'})
        return parse_receipts_list(r.text) if r.status_code == 200 else None

    def receipt_details(self, rid):
        r = self.request('GET', f'/smc/HospDecreeReceipts/Details/{rid}')
        return parse_receipt_details(r.text) if r.status_code == 200 else None


# ═════════════════════════════ registry (receipt memory) ═════════════════════════════
class LocalRegistry:
    """JSON file in --state-dir (offline rehearsal and tests). Same behaviour as the Supabase tables."""
    name = 'local'
    def __init__(self, state_dir):
        os.makedirs(state_dir, exist_ok=True); self.f = os.path.join(state_dir, 'registry.json')
        s = json.load(open(self.f, encoding='utf-8')) if os.path.exists(self.f) else {}
        self.receipts, self.decrees, self.events = s.get('receipts', {}), s.get('decrees', {}), s.get('events', [])
    def commit(self): json.dump({'receipts': self.receipts, 'decrees': self.decrees, 'events': self.events}, open(self.f, 'w', encoding='utf-8'), ensure_ascii=False)
    def receipts_of_decrees(self, decrees):
        ds = set(decrees); return [dict(r) for r in self.receipts.values() if r['decree_number'] in ds]
    def decree_states(self, decrees): return {d: dict(self.decrees[d]) for d in decrees if d in self.decrees}
    def upsert_receipts(self, rows):
        for r in rows: self.receipts.setdefault(r['receipt_id'], {}).update(r)
    def upsert_decrees(self, rows):
        for r in rows: self.decrees.setdefault(r['decree_number'], {}).update(r)
    def add_events(self, rows): self.events += rows
    def open_receipts(self, exclude_decrees, final_statuses, limit):
        ex = set(exclude_decrees)
        c = [r for r in self.receipts.values() if not r.get('removed_at') and r['decree_number'] not in ex and (r.get('status') or '') not in final_statuses]
        return sorted(c, key=lambda r: r.get('last_seen') or '')[:limit]

class SupabaseRegistry:
    name = 'supabase'
    def __init__(self, store):
        self.s = store                                    # reuse the ledger store's retrying HTTP helper
    def _get_all(self, path):
        out, off = [], 0
        while True:
            rows = self.s._req('GET', f'{path}&limit=1000&offset={off}').json(); out += rows
            if len(rows) < 1000: return out
            off += 1000
    def commit(self): pass
    def receipts_of_decrees(self, decrees):
        out = []
        for c in chunks(decrees, 60): out += self._get_all('audit_sf_receipts?select=*&order=receipt_id.asc&decree_number=in.(' + ','.join(c) + ')')
        return out
    def decree_states(self, decrees):
        out = {}
        for c in chunks(decrees, 80):
            for r in self.s._req('GET', 'audit_sf_decree_state?select=*&decree_number=in.(' + ','.join(c) + ')').json(): out[r['decree_number']] = r
        return out
    def _upsert(self, table, key, rows):
        for c in chunks(rows, 500):
            # PostgREST needs identical keys in every object of a batch: group by key set
            groups = {}
            for r in c: groups.setdefault(tuple(sorted(r)), []).append(r)
            for g in groups.values():
                self.s._req('POST', f'{table}?on_conflict={key}', data=json.dumps(g), headers={'Prefer': 'resolution=merge-duplicates,return=minimal'})
    def upsert_receipts(self, rows): self._upsert('audit_sf_receipts', 'receipt_id', rows)
    def upsert_decrees(self, rows): self._upsert('audit_sf_decree_state', 'decree_number', rows)
    def add_events(self, rows):
        for c in chunks(rows, 500):
            g = {}
            for r in c: g.setdefault(tuple(sorted(r)), []).append(r)
            for x in g.values(): self.s._req('POST', 'audit_sf_receipt_events', data=json.dumps(x), headers={'Prefer': 'return=minimal'})
    def open_receipts(self, exclude_decrees, final_statuses, limit):
        fin = ','.join(f'"{x}"' for x in sorted(final_statuses)) or '""'
        rows = self.s._req('GET', f'audit_sf_receipts?select=*&removed_at=is.null&status=not.in.({fin})&order=last_seen.asc&limit={limit + len(exclude_decrees)}').json()
        ex = set(exclude_decrees); return [r for r in rows if r['decree_number'] not in ex][:limit]


# ═════════════════════════════ ledger access scoped to receipts ═════════════════════════════
def stored_lines(store, rids):
    """rid -> {line_key: (content_hash, first_seen, removed(bool), line_value)} for the given receipts (piece 0 of every stored line)."""
    rids = list(rids); out = {r: {} for r in rids}
    if not rids: return out
    if store.name == 'local':
        z = store.lines[(store.lines.piece_no == 0) & store.lines.receipt_id.isin(set(rids))]
        for k, h, fs, ra, v, rid in zip(z.line_key, z.content_hash, z.first_seen, z.removed_at, z.line_value, z.receipt_id):
            out[rid][k] = (h, fs, ra is not None and not pd.isna(ra), float(v))
        return out
    for c in chunks(rids, 50):
        off = 0
        while True:
            rows = store._req('GET', 'audit_sf_service_lines?select=line_key,receipt_id,content_hash,first_seen,removed_at,line_value&piece_no=eq.0'
                                     f'&order=line_key.asc&limit=1000&offset={off}&receipt_id=in.(' + ','.join(c) + ')').json()
            for x in rows: out[x['receipt_id']][x['line_key']] = (x['content_hash'], x['first_seen'], x['removed_at'] is not None, float(x['line_value']))
            if len(rows) < 1000: break
            off += 1000
    return out

def update_meta(store, patch):
    """Merge into audit_settings['sf_ledger'] (the module's banner) without wiping what the export-based run wrote."""
    if store.name == 'local':
        store.meta = {**(store.meta or {}), **patch}; return
    cur = store._req('GET', 'audit_settings?select=value&key=eq.sf_ledger').json()
    val = {**((cur[0]['value'] if cur else None) or {}), **patch}
    store._req('POST', 'audit_settings?on_conflict=key', data=json.dumps([{'key': 'sf_ledger', 'value': val}]),
               headers={'Prefer': 'resolution=merge-duplicates,return=minimal'})


# ═════════════════════════════ decisions (pure) ═════════════════════════════
def decide(rec, reg, ledger, tol):
    """rec = row of the decree's receipt list, reg = registry row or None, ledger = {line_key: (...)} of that receipt (only used when reg is None).
    -> (action, old_value)"""
    if reg is None:
        active = [v[3] for v in (ledger or {}).values() if not v[2]]
        if active:
            return ('SEEDED', sum(active)) if abs(sum(active) - rec['value']) <= tol else ('EDITED', sum(active))
        return 'NEW', None
    if reg.get('removed_at'): return 'REAPPEARED', reg.get('receipt_value')
    if reg.get('receipt_value') is None or reg.get('items_count') is None: return 'NEW', reg.get('receipt_value')   # earlier read never finished
    if abs(float(reg['receipt_value']) - rec['value']) > tol: return 'EDITED', float(reg['receipt_value'])
    if (reg.get('status') or '') != (rec['status'] or ''): return 'STATUS', float(reg['receipt_value'])
    return 'SAME', float(reg['receipt_value'])

NEEDS_READ = {'NEW', 'EDITED', 'REAPPEARED'}


# ═════════════════════════════ the run ═════════════════════════════
class Ctx:
    pass

def investigate(smc, items_todo, ctx):
    """items_todo: [(decree, meta, rec, action)] -> (DataFrame in export layout, {rid: info}); failures go to ctx.errors."""
    rows, info = [], {}
    for decree, meta, rec, action in items_todo:
        rid = rec['receipt_id']
        try:
            d = smc.receipt_details(rid)
        except FetchFailed: raise
        except Exception as e:
            d = None; ctx.log(f'   receipt {rid}: {e}')
        if d is None or not d['items']:
            ctx.errors.append({'receipt_id': rid, 'decree_number': decree, 'why': 'no items read' if d else 'page not readable'})
            continue
        pid = d['patient_id'] or meta.get('patient_id') or ''
        s = round(sum(i['Price'] for i in d['items']), 2); note = ''
        if d['decree'] and d['decree'] != decree: note = f"page says decree {d['decree']}"
        elif abs(s - rec['value']) > 0.05: note = f"items sum {s:,.2f} != list value {rec['value']:,.2f}"
        info[rid] = {'patient_id': pid, 'items': len(d['items']), 'sum': s, 'note': note}
        for i in d['items']:
            rows.append({'ID_Number': pid, 'Decree_Number': decree, 'Receipt_ID': rid, **i})
    return pd.DataFrame(rows, columns=EXPORT_COLS), info

def apply_lines(store, df, read_rids, removed_rids, ctx):
    """Reconcile the lines of the receipts that were read (and mark the lines of vanished receipts as removed).
    -> per-receipt counters {rid: [new, changed, removed]}"""
    per = {}
    info, P, key_rid, A = {}, [], {}, None
    if len(df):
        A, st = L.load_lines([df]); P, info, st2 = L.build_pieces(A)
        key_rid = dict(zip(A.line_key, A.rid_n)); ctx.no_date += st['no_date']; ctx.no_date_value += st['no_date_value']
    stored = stored_lines(store, list(read_rids) + list(removed_rids))
    state = {k: v for rid in read_rids for k, v in stored.get(rid, {}).items()}
    new = [k for k in info if k not in state]
    changed = [k for k, v in info.items() if k in state and (v['hash'] != state[k][0] or state[k][2])]
    gone = [k for k, s in state.items() if k not in info and not s[2]]
    gone_rid = {k: rid for rid in read_rids for k in stored.get(rid, {})}
    gone_whole = [(k, rid) for rid in removed_rids for k, s in stored.get(rid, {}).items() if not s[2]]
    for k in new: per.setdefault(key_rid[k], [0, 0, 0])[0] += 1
    for k in changed: per.setdefault(key_rid[k], [0, 0, 0])[1] += 1
    for k in gone: per.setdefault(gone_rid[k], [0, 0, 0])[2] += 1
    for k, rid in gone_whole: per.setdefault(rid, [0, 0, 0])[2] += 1
    ctx.v_new += sum(info[k]['value'] for k in new); ctx.v_changed += sum(info[k]['value'] for k in changed)
    ctx.v_removed += sum(state[k][3] for k in gone) + sum(stored[rid][k][3] for k, rid in gone_whole)
    ctx.n_new += len(new); ctx.n_changed += len(changed); ctx.n_removed += len(gone) + len(gone_whole)
    ctx.n_source += len(info)
    cutoff = (ctx.today - dt.timedelta(days=ctx.late_days)).isoformat()
    for k in new:
        m = info[k]['main'].isoformat()[:7]; o = ctx.by_month.setdefault(m, {'lines': 0, 'value': 0.0})
        o['lines'] += 1; o['value'] = round(o['value'] + info[k]['value'], 2); ctx.new_dates.append(info[k]['main'].isoformat())
        if info[k]['main'].isoformat() < cutoff: ctx.late += 1; ctx.late_value += info[k]['value']
    if ctx.write:
        first = {k: state[k][1] for k in changed}
        touch = set(new) | set(changed)
        rows = [{**p, 'first_seen': first.get(p['line_key'], ctx.today.isoformat()), 'run_id': ctx.run_id, 'removed_at': None} for p in P if p['line_key'] in touch]
        if changed: store.delete_keys(changed)
        store.insert(rows)
        mark = gone + [k for k, _ in gone_whole]
        if mark: store.mark_removed(mark, ctx.today.isoformat())
    return per

def process_chunk(ctx, smc, store, reg, chunk):
    """chunk: [(decree, meta)] -> reads the receipt lists, decides, reads items where needed, writes ledger + registry."""
    today = ctx.today.isoformat(); lists = {}; err0 = len(ctx.errors)
    for decree, meta in chunk:
        try: lst = smc.receipts_list(decree)
        except FetchFailed: raise
        if lst is None: ctx.errors.append({'receipt_id': '', 'decree_number': decree, 'why': 'receipt list not readable'}); continue
        lists[decree] = lst
    if not lists: return
    regrows = {r['receipt_id']: r for r in reg.receipts_of_decrees(list(lists))}
    rids_seen = {r['receipt_id'] for lst in lists.values() for r in lst}
    unknown = [rid for rid in rids_seen if rid not in regrows]
    ledger = stored_lines(store, unknown)
    todo, plan = [], []
    for decree, lst in lists.items():
        meta = dict(next(m for d, m in chunk if d == decree))
        for rec in lst:
            action, old = decide(rec, regrows.get(rec['receipt_id']), ledger.get(rec['receipt_id']), ctx.tol)
            plan.append((decree, meta, rec, action, old))
            ctx.count[action] = ctx.count.get(action, 0) + 1
            if action in NEEDS_READ: todo.append((decree, meta, rec, action))
        listed = {x['receipt_id'] for x in lst}
        for rid, r in regrows.items():                                    # receipts we know that are gone from this decree
            if r['decree_number'] == decree and rid not in listed and not r.get('removed_at'):
                plan.append((decree, meta, {'receipt_id': rid, 'value': r.get('receipt_value'), 'status': r.get('status')}, 'REMOVED', r.get('receipt_value')))
                ctx.count['REMOVED'] = ctx.count.get('REMOVED', 0) + 1
    for decree, meta, rec, action, old in plan:
        ctx.actions.append({'receipt_id': rec['receipt_id'], 'decree_number': decree, 'action': action, 'old_value': old, 'new_value': rec.get('value'),
                            'status': rec.get('status'), 'card': meta.get('card')})
    if ctx.plan_only: return
    df, info = investigate(smc, todo, ctx)
    read_rids = list(info)
    removed_rids = [rec['receipt_id'] for _, _, rec, a, _ in plan if a == 'REMOVED']
    per = apply_lines(store, df, read_rids, removed_rids, ctx)
    if not ctx.write: return
    # —— registry + events (written after the lines, so a crash in between just repeats this chunk) ——
    rrows, events = [], []
    for decree, meta, rec, action, old in plan:
        rid = rec['receipt_id']; prev = regrows.get(rid); n = per.get(rid, [0, 0, 0])
        if action == 'REMOVED':
            rrows.append({'receipt_id': rid, 'decree_number': decree, 'removed_at': today, 'last_seen': today})
            events.append(ev(ctx, rid, decree, prev.get('patient_id'), 'REMOVED', old, None, prev.get('status'), None, n, 'gone from the decree receipt list')); continue
        if action in NEEDS_READ and rid not in info: continue            # read failed: stays unknown, retried next run
        i = info.get(rid)
        row = {'receipt_id': rid, 'decree_number': decree, 'patient_id': (i or {}).get('patient_id') or meta.get('patient_id') or (prev or {}).get('patient_id'),
               'status': rec['status'], 'status_text': rec['status_text'], 'card': meta.get('card') or (prev or {}).get('card'), 'receipt_value': rec['value'],
               'claim_no': rec['claim_no'] or None, 'registered_at': rec['registered_at'], 'last_seen': today, 'removed_at': None}
        if prev is None: row['first_seen'] = today
        if i:
            row.update(items_count=i['items'], items_value=i['sum'], last_investigated=today,
                       investigations=int((prev or {}).get('investigations') or 0) + 1, note=i['note'] or None)
            if i['note']: events.append(ev(ctx, rid, decree, row['patient_id'], 'ITEMS_MISMATCH', old, rec['value'], None, rec['status'], n, i['note']))
        if action == 'SEEDED':      # lines already in the ledger (old exports): remember them as "investigated", no page read
            row.update(items_count=sum(1 for v in ledger.get(rid, {}).values() if not v[2]), items_value=old, investigations=0,
                       note='already in the ledger (old exports); total matched')
        if action != 'SAME': events.append(ev(ctx, rid, decree, row['patient_id'], action, old, rec['value'], (prev or {}).get('status'), rec['status'], n, None))
        rrows.append(row)
    reg.upsert_receipts(rrows); reg.add_events(events)
    bad = {e['decree_number'] for e in ctx.errors[err0:]}          # a decree with an unread receipt must be looked at again next run
    reg.upsert_decrees([{'decree_number': d, 'sig': m.get('sig'), 'receipts_n': len(lists[d]), 'checked_at': today}
                        for d, m in chunk if d in lists and m.get('sig') and d not in bad])
    reg.commit(); store.commit()

def ev(ctx, rid, decree, pid, event, old, new, old_s, new_s, n, note):
    return {'run_id': ctx.run_id, 'receipt_id': rid, 'decree_number': decree, 'patient_id': pid, 'event': event, 'old_value': old, 'new_value': new,
            'old_status': old_s, 'new_status': new_s, 'lines_new': n[0], 'lines_changed': n[1], 'lines_removed': n[2], 'note': note}

def scan_cards(smc, a, ctx):
    """-> {decree: {'patient_id', 'card', 'sig', ...}} from every requested dashboard card."""
    out = {}
    for flag in a.cards:
        summ = smc.card_summary(flag, a.date_from, a.date_to)
        rows, total = smc.card_rows(flag, a.date_from, a.date_to)
        ctx.log(f'card {flag}: {len(rows)} rows read (page footer says {total}; summary page says {summ[0] if summ else "?"} invoices'
                f'{f", cost {summ[1]:,.0f}" if summ and summ[1] is not None else ""})')
        if len(rows) != total: ctx.log(f'   WARNING card {flag}: rows read ({len(rows)}) differ from the footer total ({total}) — some pages may have been missed')
        for r in rows:
            m = out.setdefault(r['decree'], {'patient_id': r['patient_id'], 'card': [], 'sig': []})
            m['card'].append(flag); m['sig'].append(f"{flag}:{r['invoice_value']:g}" if r['invoice_value'] is not None else flag)
    for m in out.values(): m['card'] = ','.join(sorted(set(m['card']))); m['sig'] = '|'.join(sorted(m['sig']))
    return out

def run(a, smc=None, store=None, reg=None):
    t0 = time.time(); ctx = Ctx()
    ctx.today = cairo_today(); ctx.run_id = int(time.time()); ctx.tol = a.tol; ctx.late_days = a.late_days
    ctx.write = not (a.dry_run or a.plan_only); ctx.plan_only = a.plan_only
    ctx.count, ctx.actions, ctx.errors, ctx.by_month, ctx.new_dates = {}, [], [], {}, []
    ctx.n_new = ctx.n_changed = ctx.n_removed = ctx.n_source = ctx.late = 0
    ctx.v_new = ctx.v_changed = ctx.v_removed = ctx.late_value = ctx.no_date_value = 0.0; ctx.no_date = 0
    ctx.log = lambda s: print(s, flush=True)
    if a.date_to is None: a.date_to = ctx.today + dt.timedelta(days=1)
    if isinstance(a.cards, str): a.cards = [c.strip() for c in a.cards.split(',') if c.strip()]

    store = store or (L.MemoryStore(a.state_dir) if a.store == 'local' else L.SupabaseStore())
    reg = reg or (LocalRegistry(a.state_dir) if a.store == 'local' else SupabaseRegistry(store))
    if smc is None:
        pairs = [('SMC_RECEIPTS_USERNAME', 'SMC_RECEIPTS_PASSWORD'), ('SMC_DASHBOARD_USERNAME', 'SMC_DASHBOARD_PASSWORD'), ('SMC_USERNAME_3', 'SMC_PASSWORD_3')]
        cred = next(((os.environ[u], os.environ[p]) for u, p in pairs if os.environ.get(u) and os.environ.get(p)), None)
        if not cred: sys.exit('No SMC credentials in the environment (SMC_RECEIPTS_USERNAME / SMC_RECEIPTS_PASSWORD).')
        smc = SMC(os.environ.get('SMC_BASE_URL', BASE_URL_DEFAULT), cred[0], cred[1], os.environ.get('SMC_HOSPITAL_ID', '102233'),
                  os.environ.get('SMC_HOSPITAL_TYPES', '1'), sleep=a.sleep)
        if not smc.login_retry(): sys.exit('SMC login failed.')

    todo = []
    try:
        cards = scan_cards(smc, a, ctx)
        decrees = list(cards)[:a.limit_decrees] if a.limit_decrees else list(cards)
        ds = reg.decree_states(decrees) if decrees else {}
        horizon = (ctx.today - dt.timedelta(days=a.deep_days)).isoformat()
        todo, skipped = [], 0
        for d in decrees:
            s = ds.get(d)
            if (not a.deep) and s and s.get('sig') == cards[d]['sig'] and str(s.get('checked_at') or '') >= horizon: skipped += 1; continue
            todo.append((d, cards[d]))
        ctx.log(f'{len(decrees)} decrees on the cards: {skipped} unchanged since the last look (skipped), {len(todo)} to check')
        for c in chunks(todo, a.chunk):
            process_chunk(ctx, smc, store, reg, c)
            ctx.log(f'   … {sum(ctx.count.values()):,} receipts looked at so far | ' + ', '.join(f'{k} {v}' for k, v in sorted(ctx.count.items())))
        if not a.no_departed:
            dep = reg.open_receipts(decrees, set(a.final_statuses), a.max_departed)
            dd = {}
            for r in dep: dd.setdefault(r['decree_number'], {'patient_id': r.get('patient_id'), 'card': None, 'sig': None})
            if dd:
                ctx.log(f'following {len(dd)} decrees whose receipts left the cards (status sweep, max {a.max_departed} receipts)')
                for c in chunks(list(dd.items()), a.chunk): process_chunk(ctx, smc, store, reg, c)
    except FetchFailed as e:
        ctx.errors.append({'receipt_id': '', 'decree_number': '', 'why': f'RUN STOPPED: {e}'})
        ctx.log(f'RUN STOPPED SAFELY: {e}\nEverything finished so far is saved; the next run continues.')
    return finish(a, ctx, store, reg, smc, t0, len(todo))

def finish(a, ctx, store, reg, smc, t0, n_decrees):
    mode = 'plan' if ctx.plan_only else ('dry-run' if a.dry_run else 'receipts')
    c = ctx.count
    ctx.log('\n' + '=' * 70)
    ctx.log(f"receipts: " + (', '.join(f'{k} {v}' for k, v in sorted(c.items())) or 'none') + f" | decrees read: {n_decrees} | SMC requests: {smc.requests_made}")
    if not ctx.plan_only:
        ctx.log(f"ledger lines: NEW {ctx.n_new:,} ({ctx.v_new:,.0f}) | CHANGED {ctx.n_changed:,} ({ctx.v_changed:,.0f}) | REMOVED {ctx.n_removed:,} ({ctx.v_removed:,.0f}) | "
                f"late new (> {ctx.late_days} d old) {ctx.late:,} ({ctx.late_value:,.0f})")
        if ctx.no_date: ctx.log(f'   {ctx.no_date} lines with an unreadable dispensing date were not stored ({ctx.no_date_value:,.0f})')
    for e in ctx.errors[:15]: ctx.log(f"   ERROR {e['decree_number']} {e['receipt_id']}: {e['why']}")
    if len(ctx.errors) > 15: ctx.log(f'   … and {len(ctx.errors) - 15} more')
    if a.report and ctx.actions: pd.DataFrame(ctx.actions).to_csv(a.report, index=False, encoding='utf-8-sig'); ctx.log(f'report written: {a.report}')
    summ = os.environ.get('GITHUB_STEP_SUMMARY')
    if summ:
        with open(summ, 'a', encoding='utf-8') as fh:
            fh.write(f"### SF receipts sync — {mode}\n| receipts | n |\n|---|---:|\n" + ''.join(f'| {k} | {v} |\n' for k, v in sorted(c.items())) +
                     f"\n| ledger lines | n | value |\n|---|---:|---:|\n| new | {ctx.n_new:,} | {ctx.v_new:,.0f} |\n| changed | {ctx.n_changed:,} | {ctx.v_changed:,.0f} |\n"
                     f"| removed | {ctx.n_removed:,} | {ctx.v_removed:,.0f} |\n\nerrors: {len(ctx.errors)}\n")
    if ctx.write and (ctx.n_new or ctx.n_changed or ctx.n_removed or c):
        entry = {'run_id': ctx.run_id, 'run_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'mode': 'receipts', 'source': 'SMC dashboard: ' + ','.join(a.cards),
                 'lines_in_source': ctx.n_source, 'lines_new': ctx.n_new, 'lines_changed': ctx.n_changed, 'lines_removed': ctx.n_removed,
                 'value_new': round(ctx.v_new, 2), 'value_changed': round(ctx.v_changed, 2), 'value_removed': round(ctx.v_removed, 2),
                 'late_lines': ctx.late, 'late_value': round(ctx.late_value, 2), 'late_days': ctx.late_days,
                 'min_new_date': min(ctx.new_dates) if ctx.new_dates else None, 'max_new_date': max(ctx.new_dates) if ctx.new_dates else None,
                 'by_month': ctx.by_month,
                 'note': (a.note + ' | ' if a.note else '') + 'receipts: ' + ', '.join(f'{k} {v}' for k, v in sorted(c.items())) + (f' | errors {len(ctx.errors)}' if ctx.errors else '')}
        store.write_log(entry)
        if ctx.n_new or ctx.n_changed or ctx.n_removed: store.refresh_daily()
        update_meta(store, {'built_at': dt.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'), 'mode': 'receipts',
                            'last_run': {k: entry[k] for k in ('run_id', 'lines_new', 'lines_changed', 'lines_removed', 'value_new')}})
        store.commit()
    ctx.log(f'done in {time.time() - t0:.0f}s (run_id {ctx.run_id})')
    return 3 if (ctx.errors and not a.allow_errors) else 0

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description='Dashboard-driven daily feed of the State-fund line ledger.')
    ap.add_argument('--cards', default='P,R01', help='dashboard card flags to follow (P pending, R01 returned, A02/A0 under review ...)')
    ap.add_argument('--date-from', default='2026-01-15', type=lambda s: dt.date.fromisoformat(s))
    ap.add_argument('--date-to', default=None, type=lambda s: dt.date.fromisoformat(s), help='default: tomorrow')
    ap.add_argument('--store', choices=['supabase', 'local'], default='supabase')
    ap.add_argument('--state-dir', default='sf_state')
    ap.add_argument('--dry-run', action='store_true', help='read everything, show the line diff, write nothing')
    ap.add_argument('--plan-only', action='store_true', help='only scan the cards + receipt lists and say what would be read')
    ap.add_argument('--deep', action='store_true', help='check every decree even if its card row is unchanged')
    ap.add_argument('--deep-days', type=int, default=7, help='a decree is re-checked at least this often even when unchanged')
    ap.add_argument('--no-departed', action='store_true', help='do not follow receipts that left the cards')
    ap.add_argument('--max-departed', type=int, default=200)
    ap.add_argument('--final-statuses', default='M1', type=lambda s: [x.strip() for x in s.split(',') if x.strip()])
    ap.add_argument('--tol', type=float, default=0.01, help='totals closer than this count as equal')
    ap.add_argument('--late-days', type=int, default=L.LATE_DAYS_DEFAULT)
    ap.add_argument('--chunk', type=int, default=40, help='decrees processed (and saved) together')
    ap.add_argument('--limit-decrees', type=int, default=0, help='testing: only the first N decrees')
    ap.add_argument('--sleep', type=float, default=0.3)
    ap.add_argument('--report', default='', help='CSV with one row per receipt decision')
    ap.add_argument('--allow-errors', action='store_true', help='exit 0 even when some receipts could not be read')
    ap.add_argument('--note', default='')
    return ap.parse_args(argv)

if __name__ == '__main__':
    sys.exit(run(parse_args()))
