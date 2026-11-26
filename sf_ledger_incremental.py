# -*- coding: utf-8 -*-
"""
sf_ledger_incremental.py — line-level State-fund service ledger, updated INCREMENTALLY (daily).

What it does on every run
  1. reads the dispensed-items export(s)  (xlsb / xlsx / csv, any number of files, globs allowed)
  2. de-duplicates lines present in more than one part, gives every line a stable key (line_key) + a content hash
  3. allocates every line to its real service date(s) (same rules as sf_service_ledger.py: Notes dates / quantities, +-92 days)
  4. compares with what the database already holds:
        NEW      line_key not stored yet                 -> inserted, first_seen = today          ("new purchasings of the day")
        CHANGED  same key, different content hash        -> its pieces are replaced, first_seen kept (notes / patient / decree edited)
        REMOVED  stored line missing from the source     -> only with --full: removed_at = today (line kept for audit, excluded from totals)
        SAME     nothing to do
  5. writes one row to audit_sf_ingest_log, rebuilds audit_sf_service_daily (the table the module reads)
     and refreshes audit_settings['sf_ledger'] (banner in the module).

Stores:  Supabase (default, needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)   |   --store local --state-dir DIR (offline test)
Needs:   sf_service_ledger.py in the same folder (date + notes logic), pandas, requests, openpyxl, pyxlsb (only for .xlsb)

Examples
  python sf_ledger_incremental.py part1.xlsb part2.xlsb Part3.xlsb --dry-run          # first look, writes nothing
  python sf_ledger_incremental.py part1.xlsb part2.xlsb Part3.xlsb                    # first load (slow, ~700k rows) / later: only new lines
  python sf_ledger_incremental.py "exports/dispensed/*.xlsx" --full                   # complete export => also detects removed lines
"""
import argparse, glob, hashlib, json, os, sys, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sf_service_ledger import serial_to_date, parse_notes, allocate, _has, read_xlsb_sheet   # noqa: E402

LATE_DAYS_DEFAULT = 7
PIECE_COLS = ['line_key', 'piece_no', 'receipt_id', 'decree_number', 'patient_id', 'item_name', 'cat', 'kind',
              'service_date', 'main_date', 'value', 'main_value', 'line_value', 'content_hash']


# ───────────────────────────── reading / normalising ─────────────────────────────
def read_source(path):
    p = path.lower()
    if p.endswith('.xlsb'): return read_xlsb_sheet(path)[0]
    if p.endswith('.csv'): return pd.read_csv(path, encoding='utf-8-sig', low_memory=False)
    return pd.read_excel(path, sheet_name=0, engine='openpyxl')

def _id(v):
    """Receipt / national id / decree -> clean string ('20527553.0' and 20527553 both give '20527553')."""
    if v is None or (isinstance(v, float) and pd.isna(v)): return ''
    try: return str(int(float(v)))
    except (TypeError, ValueError): return str(v).strip()

def _h(*parts, n=24):
    return hashlib.sha1('\x1f'.join(str(p) for p in parts).encode('utf-8')).hexdigest()[:n]

def load_lines(frames):
    """-> (lines DataFrame with line_key/content_hash/main_date, stats). Lines present in 2+ parts count once."""
    out = []
    for i, d in enumerate(frames):
        d = d.rename(columns=lambda c: {'unified_name': 'Unified', 'Unified_Name': 'Unified'}.get(str(c).strip(), str(c).strip())).copy()
        for c in ('Unified', 'Notes', 'Unit', 'Decree_Number', 'ID_Number'):
            if c not in d: d[c] = None
        d['Price'] = pd.to_numeric(d['Price'], errors='coerce').fillna(0.0)
        d['Quantity'] = pd.to_numeric(d['Quantity'], errors='coerce').fillna(0.0)
        d['part'] = i + 1; out.append(d)
    A = pd.concat(out, ignore_index=True)
    cache = {}
    def md(v):
        k = v if not isinstance(v, (pd.Timestamp, dt.datetime)) else str(v)
        if k not in cache: cache[k] = serial_to_date(v)
        return cache[k]
    A['main'] = A['Dispensing_Date'].map(md)
    A['rid_n'] = A['Receipt_ID'].map(_id)
    A['_k'] = (A['rid_n'] + '|' + A['Item_Name'].astype(str) + '|' + A['Quantity'].map(lambda x: format(float(x), 'g')) + '|' +
               A['Price'].map(lambda x: f'{float(x):.2f}') + '|' + A['main'].map(lambda d: d.isoformat() if d else 'NA'))
    A['_n'] = A.groupby(['part', '_k']).cumcount()
    A = A.sort_values(['_k', '_n', 'part']).drop_duplicates(['_k', '_n'], keep='first').reset_index(drop=True)
    A['line_key'] = [_h(k, n) for k, n in zip(A['_k'], A['_n'])]
    A['content_hash'] = [_h(_id(a), _id(b), str(c) if _has(c) else '', str(u) if _has(u) else '', str(x) if _has(x) else '', n=16)
                         for a, b, c, u, x in zip(A['ID_Number'], A['Decree_Number'], A['Notes'], A['Unit'], A['Unified'])]
    st = {'lines_raw': int(sum(len(d) for d in out)), 'lines': int(len(A)), 'no_date': int(A['main'].isna().sum()),
          'no_date_value': round(float(A.loc[A['main'].isna(), 'Price'].sum()), 2)}
    return A, st

def build_pieces(A):
    """one dict per (line, service date). Lines without a readable main date are skipped. -> (pieces list, per-line info, stats)"""
    P, info = [], {}
    st = {'qty': 0, 'dates': 0, 'rejected': 0}
    for r in A.itertuples(index=False):
        main = r.main
        if main is None: continue
        kind, toks = parse_notes(r.Notes, main)
        if kind in st: st[kind] += 1
        al = sorted(allocate(main, r.Quantity, kind, toks), key=lambda x: x[0])
        cat = 'drug' if (_has(r.Unit) or _has(r.Unified)) else 'service'
        pid, dec, price = _id(r.ID_Number), _id(r.Decree_Number), float(r.Price)
        item = str(r.Item_Name)[:200]
        info[r.line_key] = {'hash': r.content_hash, 'value': price, 'main': main}
        for i, (d, sh) in enumerate(al):
            P.append({'line_key': r.line_key, 'piece_no': i, 'receipt_id': r.rid_n, 'decree_number': dec, 'patient_id': pid,
                      'item_name': item, 'cat': cat, 'kind': kind, 'service_date': d.isoformat(), 'main_date': main.isoformat(),
                      'value': round(price * sh, 4), 'main_value': round(price, 4) if i == 0 else 0.0,
                      'line_value': round(price, 4), 'content_hash': r.content_hash})
    return P, info, st


# ───────────────────────────── stores ─────────────────────────────
class MemoryStore:
    """Offline store (pickle in --state-dir). Behaves like the Supabase tables, incl. the daily rebuild."""
    name = 'local'
    def __init__(self, state_dir):
        self.dir = state_dir; os.makedirs(state_dir, exist_ok=True); self.f = os.path.join(state_dir, 'state.pkl')
        s = pd.read_pickle(self.f) if os.path.exists(self.f) else {}
        self.lines = s.get('lines', pd.DataFrame(columns=PIECE_COLS + ['first_seen', 'run_id', 'removed_at']))
        self.logs = s.get('logs', []); self.daily = s.get('daily'); self.meta = s.get('meta')
    def _save(self): pd.to_pickle({'lines': self.lines, 'logs': self.logs, 'daily': self.daily, 'meta': self.meta}, self.f)
    def state(self):
        z = self.lines[self.lines.piece_no == 0]
        return {k: (h, fs, ra is not None and not pd.isna(ra), float(v)) for k, h, fs, ra, v in zip(z.line_key, z.content_hash, z.first_seen, z.removed_at, z.line_value)}
    def delete_keys(self, keys):
        self.lines = self.lines[~self.lines.line_key.isin(set(keys))]
    def insert(self, rows):
        if rows: self.lines = pd.concat([self.lines, pd.DataFrame(rows)], ignore_index=True)
    def mark_removed(self, keys, day):
        m = self.lines.line_key.isin(set(keys)); self.lines.loc[m, 'removed_at'] = day
    def write_log(self, e): self.logs.append(e)
    def refresh_daily(self):
        L = self.lines[self.lines.removed_at.isna()].copy()
        for c in ('value', 'main_value'): L[c] = L[c].astype(float)
        a = L.groupby(['service_date', 'cat']).agg(value=('value', 'sum'), pieces=('value', 'size'), receipts=('receipt_id', 'nunique'), patients=('patient_id', 'nunique'))
        b = L[L.main_value != 0].groupby(['main_date', 'cat']).agg(value_main=('main_value', 'sum')); b.index.names = ['service_date', 'cat']
        self.daily = a.join(b, how='outer').fillna(0).reset_index()
    def set_meta(self, m): self.meta = m
    def commit(self): self._save()


class SupabaseStore:
    name = 'supabase'
    def __init__(self, batch=1000, conc=6):
        import requests
        self.rq = requests; self.url = os.environ['SUPABASE_URL'].rstrip('/') + '/rest/v1'; key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
        self.H = {'apikey': key, 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}; self.batch = batch; self.conc = conc
    def _req(self, method, path, **kw):
        extra = kw.pop('headers', {})
        for t in range(4):
            try:
                r = self.rq.request(method, f'{self.url}/{path}', headers={**self.H, **extra}, timeout=180, **kw)
                if r.status_code >= 500 and t < 3: time.sleep(2 * (t + 1)); continue
                r.raise_for_status(); return r
            except (self.rq.ConnectionError, self.rq.Timeout):
                if t == 3: raise
                time.sleep(2 * (t + 1))
    def state(self):
        base = 'audit_sf_service_lines?select=line_key,content_hash,first_seen,removed_at,line_value&piece_no=eq.0&order=line_key.asc'
        r0 = self._req('GET', base + '&limit=1000&offset=0', headers={'Prefer': 'count=exact'})
        rows = r0.json(); cr = r0.headers.get('content-range', ''); total = int(cr.split('/')[1]) if '/' in cr and cr.split('/')[1].isdigit() else len(rows)
        offs = list(range(1000, total, 1000))
        with ThreadPoolExecutor(self.conc) as ex:
            for part in ex.map(lambda o: self._req('GET', f'{base}&limit=1000&offset={o}').json(), offs): rows += part
        return {x['line_key']: (x['content_hash'], x['first_seen'], x['removed_at'] is not None, float(x['line_value'])) for x in rows}
    def delete_keys(self, keys):
        keys = list(keys)
        for i in range(0, len(keys), 80): self._req('DELETE', 'audit_sf_service_lines?line_key=in.(' + ','.join(keys[i:i + 80]) + ')')
    def insert(self, rows):
        for i in range(0, len(rows), self.batch):
            self._req('POST', 'audit_sf_service_lines?on_conflict=line_key,piece_no', data=json.dumps(rows[i:i + self.batch]),
                      headers={'Prefer': 'resolution=merge-duplicates,return=minimal'})
            if (i // self.batch) % 50 == 0: print(f'   inserted {min(i + self.batch, len(rows)):,}/{len(rows):,}', flush=True)
    def mark_removed(self, keys, day):
        keys = list(keys)
        for i in range(0, len(keys), 80): self._req('PATCH', 'audit_sf_service_lines?line_key=in.(' + ','.join(keys[i:i + 80]) + ')', data=json.dumps({'removed_at': day}))
    def write_log(self, e): self._req('POST', 'audit_sf_ingest_log', data=json.dumps([e]), headers={'Prefer': 'return=minimal'})
    def refresh_daily(self): self._req('POST', 'rpc/refresh_sf_service_daily', data='{}')
    def set_meta(self, m):
        self._req('POST', 'audit_settings?on_conflict=key', data=json.dumps([{'key': 'sf_ledger', 'value': m}]), headers={'Prefer': 'resolution=merge-duplicates,return=minimal'})
    def commit(self): pass


# ───────────────────────────── the run ─────────────────────────────
def diff(src, state, full):
    new = [k for k in src if k not in state]
    changed = [k for k, v in src.items() if k in state and (v['hash'] != state[k][0] or state[k][2])]      # edited, or reappeared after removal
    removed = [k for k, s in state.items() if full and k not in src and not s[2]]
    return new, changed, removed

def run(a):
    t0 = time.time(); today = dt.date.today().isoformat(); run_id = int(time.time())
    files = [f for pat in a.files for f in (sorted(glob.glob(pat)) or [pat])]
    frames = []
    for f in files:
        d = read_source(f); frames.append(d); print(f'read {f}: {len(d):,} rows', flush=True)
    A, st = load_lines(frames); P, info, st2 = build_pieces(A); st.update(st2)
    print(f"source: {st['lines']:,} unique lines ({st['lines_raw']:,} raw), value {sum(v['value'] for v in info.values()):,.0f}; "
          f"unreadable dates: {st['no_date']} ({st['no_date_value']:,.0f}); notes read: qty {st['qty']:,}, dates {st['dates']:,}, rejected {st['rejected']:,}")
    if not info: sys.exit('Nothing readable in the source — nothing written.')
    store = MemoryStore(a.state_dir) if a.store == 'local' else SupabaseStore(a.batch)
    state = store.state(); mode = 'initial' if not state else ('full' if a.full else 'incremental')
    active = sum(1 for s in state.values() if not s[2]); print(f'store [{store.name}]: {len(state):,} lines already stored ({active:,} active) -> mode {mode}')
    new, changed, removed = diff(info, state, a.full)
    if a.full and active and len(info) < 0.9 * active and not a.force:
        sys.exit(f'REFUSING: the source has {len(info):,} lines but {active:,} are stored (<90%). A partial file in --full mode would mark real lines as removed. Use --force if this is intended.')
    newv = sum(info[k]['value'] for k in new); chgv = sum(info[k]['value'] for k in changed); remv = sum(state[k][3] for k in removed)
    cutoff = (dt.date.today() - dt.timedelta(days=a.late_days)).isoformat()
    late = [k for k in new if info[k]['main'].isoformat() < cutoff] if mode != 'initial' else []
    bym = {}
    for k in new:
        m = info[k]['main'].isoformat()[:7]; o = bym.setdefault(m, {'lines': 0, 'value': 0.0}); o['lines'] += 1; o['value'] = round(o['value'] + info[k]['value'], 2)
    nd = [info[k]['main'].isoformat() for k in new]
    entry = {'run_id': run_id, 'run_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'mode': mode, 'source': ', '.join(os.path.basename(f) for f in files)[:300],
             'lines_in_source': len(info), 'lines_new': len(new), 'lines_changed': len(changed), 'lines_removed': len(removed),
             'value_new': round(newv, 2), 'value_changed': round(chgv, 2), 'value_removed': round(remv, 2),
             'late_lines': len(late), 'late_value': round(sum(info[k]['value'] for k in late), 2), 'late_days': a.late_days,
             'min_new_date': min(nd) if nd else None, 'max_new_date': max(nd) if nd else None, 'by_month': bym, 'note': a.note or None}
    print(f"NEW {len(new):,} ({newv:,.0f}) | CHANGED {len(changed):,} ({chgv:,.0f}) | REMOVED {len(removed):,} ({remv:,.0f}) | late new lines (> {a.late_days} d old): {len(late):,} ({entry['late_value']:,.0f})")
    for m in sorted(bym)[-6:]: print(f'   new by dispensing month {m}: {bym[m]["lines"]:,} lines, {bym[m]["value"]:,.0f}')
    summ = os.environ.get('GITHUB_STEP_SUMMARY')
    if summ:      # shows the run on the Actions page
        with open(summ, 'a', encoding='utf-8') as fh:
            fh.write(f"### State-fund ledger — {mode}{' (dry run)' if a.dry_run else ''}\n| | lines | value |\n|---|---:|---:|\n"
                     f"| source | {len(info):,} | {sum(v['value'] for v in info.values()):,.0f} |\n| **new** | {len(new):,} | {newv:,.0f} |\n"
                     f"| changed | {len(changed):,} | {chgv:,.0f} |\n| removed | {len(removed):,} | {remv:,.0f} |\n"
                     f"| late new (> {a.late_days} d old) | {len(late):,} | {entry['late_value']:,.0f} |\n")
    if a.dry_run: print('DRY RUN — nothing written.'); return entry
    first = {k: state[k][1] for k in changed if k in state}
    touch = set(new) | set(changed)
    rows = []
    for p in P:
        if p['line_key'] in touch:
            rows.append({**p, 'first_seen': first.get(p['line_key'], today), 'run_id': run_id, 'removed_at': None})
    if changed: store.delete_keys(changed)
    store.insert(rows)
    if removed: store.mark_removed(removed, today)
    store.write_log(entry)
    store.refresh_daily()
    if store.name == 'local':
        total = round(float(store.daily.value.sum()), 2); base = round(float(store.daily.value_main.sum()), 2)
    else: total = base = None
    meta = {'built_at': dt.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'), 'mode': mode, 'files': [os.path.basename(f) for f in files], 'lines': len(info),
            'notes': {'qty': st['qty'], 'dates': st['dates'], 'rejected': st['rejected'], 'no_date': st['no_date']}, 'no_date_value': st['no_date_value'],
            'total': total if total is not None else round(sum(p['value'] for p in P), 2), 'total_main_only': base if base is not None else round(sum(v['value'] for v in info.values()), 2),
            'last_run': {k: entry[k] for k in ('run_id', 'lines_new', 'lines_changed', 'lines_removed', 'value_new')}, 'new_site_start': a.new_site_start}
    store.set_meta(meta); store.commit()
    print(f'written in {time.time() - t0:.0f}s (run_id {run_id})'); return entry

def main():
    ap = argparse.ArgumentParser(description='Incremental line-level State-fund service ledger.')
    ap.add_argument('files', nargs='+', help='dispensed-items export(s): xlsb / xlsx / csv (globs allowed)')
    ap.add_argument('--store', choices=['supabase', 'local'], default='supabase')
    ap.add_argument('--state-dir', default='sf_state', help='folder for --store local')
    ap.add_argument('--full', action='store_true', help='the files are the COMPLETE export: stored lines missing from them are marked removed')
    ap.add_argument('--force', action='store_true', help='allow --full with a source much smaller than what is stored')
    ap.add_argument('--dry-run', action='store_true', help='compute and report the diff, write nothing')
    ap.add_argument('--late-days', type=int, default=LATE_DAYS_DEFAULT, help='a NEW line dispensed more than this many days ago counts as a late entry')
    ap.add_argument('--new-site-start', default='2026-01-15')
    ap.add_argument('--batch', type=int, default=1000)
    ap.add_argument('--note', default='')
    run(ap.parse_args())

if __name__ == '__main__':
    main()
