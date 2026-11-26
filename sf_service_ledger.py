# -*- coding: utf-8 -*-
"""Shared logic: dedupe the three dispensing parts + allocate every line to its real service date(s)."""
import re, datetime as dt
import pandas as pd

AR = str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789')
WINDOW_DAYS = 92                    # extra dates must lie within ~3 months of the main dispensing date
UNIT_AFTER = re.compile(r'^\s*(?:vial|amp|tab|cap|ml|mg|mcg|iu|cc|gm|g\b|مل|ملجم|جم|امبول|أمبول|فيال|قرص|حقن|شريط|\*|x\b)', re.I)
QTY_TOK = re.compile(r'عدد\s*(\d+(?:\.\d+)?)\s*\(\s*(\d{1,4})\s*[/\-.]\s*(\d{1,2})(?:\s*[/\-.]\s*(\d{2,4}))?\s*\)')
ISO_TOK = re.compile(r'(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)')
DMY_TOK = re.compile(r'(?<![\d/.\-])(\d{1,2})\s*[/\-.]\s*(\d{1,2})(?:\s*[/\-.]\s*(\d{2,4}))?(?![\d])')

def serial_to_date(v):
    """Dispensing date -> date. Accepts Excel serials (xlsb), datetime/Timestamp (xlsx), ISO or d/m/y strings.
    Year typos like 5025/2505 are repaired to 2025; anything unreadable returns None."""
    if v is None: return None
    if isinstance(v, float) and pd.isna(v): return None
    if isinstance(v, pd.Timestamp):
        d = None if pd.isna(v) else v.date()
    elif isinstance(v, dt.datetime): d = v.date()
    elif isinstance(v, dt.date): d = v
    else:
        d = None
        try:
            f = float(v)
            try: d = dt.date(1899, 12, 30) + dt.timedelta(days=int(f))
            except OverflowError: return None
        except (TypeError, ValueError):
            m = re.match(r'^\s*(\d{4})-(\d{1,2})-(\d{1,2})', str(v))
            if m: d = _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:
                m = re.match(r'^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\s*$', str(v))
                if m: d = _safe(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if d is None: return None
    if d.year in (5025, 2505, 3025, 2052): d = d.replace(year=2025)
    elif d.year == 2062: d = d.replace(year=2026)
    if d.year < 2000 or d.year > 2100: return None      # implausible (e.g. text turned into year-0001 dates) -> counted as unreadable
    return d

def _safe(y, m, d):
    try: return dt.date(y, m, d)
    except ValueError: return None

def resolve(main, d, m, y=None):
    """d/m[/y] -> date nearest to `main` and inside the +-92 day window (else None). Tries d/m first, then m/d."""
    best = None
    for dd, mm in ((d, m), (m, d)):
        if not (1 <= mm <= 12 and 1 <= dd <= 31): continue
        if y is not None:
            yy = y + 2000 if y < 100 else y
            cands = [_safe(yy, mm, dd)]
        else:
            cands = [_safe(main.year + k, mm, dd) for k in (-1, 0, 1)]
        cands = [c for c in cands if c]
        if not cands: continue
        c = min(cands, key=lambda c: abs((c - main).days))
        if abs((c - main).days) <= WINDOW_DAYS: return c
        if y is not None:                      # year typed wrong? retry nearest year
            cc = min([_safe(main.year + k, mm, dd) for k in (-1, 0, 1) if _safe(main.year + k, mm, dd)], key=lambda c: abs((c - main).days), default=None)
            if cc and abs((cc - main).days) <= WINDOW_DAYS and best is None: best = cc
    return best

def parse_notes(note, main):
    """-> (kind, [(date, qty|None)]) ; kind in none|qty|dates|rejected"""
    if note is None or (isinstance(note, float) and pd.isna(note)): return 'none', []
    s = str(note).translate(AR).replace('\r', '\n')
    qt = []
    for m in QTY_TOK.finditer(s):
        d = resolve(main, int(m.group(2)), int(m.group(3)), int(m.group(4)) if m.group(4) else None)
        if d: qt.append((d, float(m.group(1))))
    if qt: return 'qty', qt
    s2 = QTY_TOK.sub(' ', s); out = []
    for m in ISO_TOK.finditer(s2):
        d = _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d and abs((d - main).days) <= WINDOW_DAYS: out.append((d, None))
    s3 = ISO_TOK.sub(' ', s2)
    for m in DMY_TOK.finditer(s3):
        if UNIT_AFTER.match(s3[m.end():m.end() + 8]): continue
        a, b = int(m.group(1)), int(m.group(2)); y = int(m.group(3)) if m.group(3) else None
        if y is None and a <= 2 and b <= 12 and len(s3.strip()) <= 6: pass   # lone "1/2"-style tokens are tested below
        d = resolve(main, a, b, y)
        if d: out.append((d, None))
    if out: return 'dates', out
    return ('rejected' if re.search(r'\d{1,2}\s*[/\-.]\s*\d{1,2}', s) else 'none'), []

def allocate(main, qty, kind, toks):
    """-> [(date, share)] shares sum to 1. qty tokens split by quantity (remainder stays on the main date);
    plain date lists split equally over the listed dates + the main date."""
    if kind == 'qty':
        tot = sum(q for _, q in toks); q0 = qty if qty and qty > 0 else tot
        if tot >= q0: w = {}; [w.__setitem__(d, w.get(d, 0) + q / tot) for d, q in toks]; return list(w.items())
        w = {main: (q0 - tot) / q0}
        for d, q in toks: w[d] = w.get(d, 0) + q / q0
        return list(w.items())
    if kind == 'dates':
        ds = sorted({d for d, _ in toks} | {main}); return [(d, 1 / len(ds)) for d in ds]
    return [(main, 1.0)]

def load_parts(frames):
    """frames: list of DataFrames (part1..3). Cross-part duplicates removed, repeats inside one part kept."""
    key = ['Receipt_ID', 'Item_Name', 'Quantity', 'Price', 'Dispensing_Date']
    out = []
    for i, d in enumerate(frames):
        d = d.rename(columns=lambda c: {'unified_name': 'Unified', 'Unified_Name': 'Unified'}.get(c, c)).copy()
        for c in ('Unified', 'Notes', 'Unit'):
            if c not in d: d[c] = None
        d['part'] = i + 1; d['_k'] = d[key].astype(str).agg('|'.join, axis=1); d['_n'] = d.groupby('_k').cumcount(); out.append(d)
    A = pd.concat(out, ignore_index=True)
    A = A.sort_values(['_k', '_n', 'part']).drop_duplicates(['_k', '_n'], keep='first')     # same line present in 2 parts -> one
    return A.drop(columns=['_k', '_n']).reset_index(drop=True)

def _has(v):
    return v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip() not in ('', 'N/A', 'nan', 'None')

def build(A, keep_review=400):
    """-> (pieces, main, review, stats)
    pieces : one row per (line, allocated service date)  — value = Price * share
    main   : one row per line on its MAIN dispensing date only (the baseline without any note handling)
    review : lines whose notes could not be read / moved value, for manual checking"""
    P, M, rej, moved = [], [], [], []
    st = {'lines': len(A), 'no_date': 0, 'qty': 0, 'dates': 0, 'rejected': 0, 'no_date_value': 0.0}
    for r in A.itertuples(index=False):
        main = serial_to_date(r.Dispensing_Date)
        if main is None:
            st['no_date'] += 1; st['no_date_value'] += float(r.Price or 0); continue
        kind, toks = parse_notes(r.Notes, main)
        if kind in ('qty', 'dates'): st[kind] += 1
        elif kind == 'rejected': st['rejected'] += 1
        pid = str(int(r.ID_Number)) if pd.notna(r.ID_Number) else ''
        cat = 'drug' if (_has(r.Unit) or _has(r.Unified)) else 'service'
        M.append((main, r.Price, pid, str(r.Receipt_ID), cat))
        al = allocate(main, r.Quantity, kind, toks)
        for d, sh in al: P.append((d, kind, r.Price * sh, pid, str(r.Receipt_ID), cat))
        if kind == 'rejected': rej.append((str(r.Receipt_ID), r.Item_Name, main, str(r.Notes).replace('\r', ' | ')[:120], r.Price))
        elif kind in ('qty', 'dates'):
            mv = r.Price * (1 - sum(sh for d, sh in al if d == main))
            if mv > 0: moved.append((str(r.Receipt_ID), r.Item_Name, main, kind, str(r.Notes).replace('\r', ' | ')[:120], r.Price, mv, ', '.join(f"{d:%d/%m}:{sh:.0%}" for d, sh in al)))
    pieces = pd.DataFrame(P, columns=['date', 'kind', 'value', 'pid', 'receipt', 'cat'])
    main = pd.DataFrame(M, columns=['date', 'value', 'pid', 'receipt', 'cat'])
    rej.sort(key=lambda x: -x[4]); moved.sort(key=lambda x: -x[6])
    review = {'rejected': rej[:keep_review], 'moved': moved[:keep_review]}
    return pieces, main, review, st

def daily(pieces, main):
    """daily aggregate per (service date, category) for audit_sf_service_daily"""
    a = pieces.groupby(['date', 'cat']).agg(value=('value', 'sum'), pieces=('value', 'size'), receipts=('receipt', 'nunique'), patients=('pid', 'nunique'))
    b = main.groupby(['date', 'cat']).agg(value_main=('value', 'sum'))
    d = a.join(b, how='outer').fillna(0).reset_index()
    d['value'] = d.value.round(2); d['value_main'] = d.value_main.round(2)
    for c in ('pieces', 'receipts', 'patients'): d[c] = d[c].astype(int)
    return d.sort_values(['date', 'cat']).reset_index(drop=True)

# ════════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════════
import argparse, os, sys, json, time

def read_xlsb_sheet(path, sheet=None):
    from pyxlsb import open_workbook
    with open_workbook(path) as wb:
        name = sheet if sheet in wb.sheets else wb.sheets[0]          # first sheet = all dispensed items (the "Medication" sheet is a subset)
        with wb.get_sheet(name) as sh:
            it = sh.rows(); hdr = [str(c.v).strip() if c.v is not None else '' for c in next(it)]
            rows = [[c.v for c in r] for r in it]
    return pd.DataFrame(rows, columns=hdr), name

def read_any(path, sheet=None):
    if path.lower().endswith('.xlsb'): return read_xlsb_sheet(path, sheet)
    x = pd.read_excel(path, sheet_name=sheet or 0, engine='openpyxl'); return x, str(sheet or 0)

def main():
    ap = argparse.ArgumentParser(description='Allocate every dispensed state-fund line to its real SERVICE date(s) and (optionally) push daily totals to Supabase.')
    ap.add_argument('files', nargs='+', help='part1.xlsb part2.xlsb Part3.xlsb (xlsx also works)')
    ap.add_argument('--out', default='sf_ledger_out', help='output folder (review workbook + daily csv)')
    ap.add_argument('--new-site-start', default='2026-01-15', help='first service day that exists on the NEW SMC site/dashboard')
    ap.add_argument('--push', action='store_true', help='replace audit_sf_service_daily in Supabase (needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True); t0 = time.time(); frames, meta_parts = [], []
    for f in a.files:
        df, nm = read_any(f); frames.append(df); meta_parts.append({'file': os.path.basename(f), 'sheet': nm, 'rows': int(len(df))}); print(f'read {f} [{nm}] {len(df):,} rows', flush=True)
    A = load_parts(frames); print(f'after removing lines present in 2 parts: {len(A):,} (value {A.Price.sum():,.0f})')
    P, M, rv, st = build(A); D = daily(P, M); D['date'] = D['date'].astype(str)
    print('notes handling:', st)
    ns = a.new_site_start; D['site'] = D.date.map(lambda x: 'new' if x >= ns else 'old')
    D['month'] = D.date.str[:7]
    mo = D.groupby('month').agg(value=('value', 'sum'), value_main=('value_main', 'sum'), receipts=('receipts', 'sum'), patients=('patients', 'sum')).reset_index()
    mo['moved_by_notes'] = (mo.value - mo.value_main).round(2); mo['site'] = mo.month.map(lambda m: 'new' if m + '-01' >= ns else ('mixed' if m == ns[:7] else 'old'))
    cat = D.pivot_table(index='month', columns='cat', values='value', aggfunc='sum').fillna(0).reset_index(); mo = mo.merge(cat, on='month', how='left')
    anomalies = pd.DataFrame([{'issue': 'service date missing/unreadable', 'lines': st['no_date'], 'value': round(st['no_date_value'], 2)},
        {'issue': 'service dates after today', 'lines': int((D.date > time.strftime('%Y-%m-%d')).sum()), 'value': round(float(D[D.date > time.strftime('%Y-%m-%d')].value.sum()), 2)},
        {'issue': 'service dates before 2025-06-01', 'lines': int((D.date < '2025-06-01').sum()), 'value': round(float(D[D.date < '2025-06-01'].value.sum()), 2)}])
    kinds = pd.DataFrame([{'notes kind': k, 'lines': st[k]} for k in ('qty', 'dates', 'rejected')])
    rej = pd.DataFrame(rv['rejected'], columns=['receipt', 'item', 'main_date', 'note', 'price'])
    mov = pd.DataFrame(rv['moved'], columns=['receipt', 'item', 'main_date', 'kind', 'note', 'price', 'value_moved_off_main_date', 'allocation'])
    xl = os.path.join(a.out, 'sf_service_ledger_review.xlsx')
    with pd.ExcelWriter(xl, engine='openpyxl') as w:
        mo.to_excel(w, sheet_name='Monthly', index=False); anomalies.to_excel(w, sheet_name='Data_quality', index=False); kinds.to_excel(w, sheet_name='Notes_kinds', index=False)
        mov.to_excel(w, sheet_name='Moved_by_notes_top', index=False); rej.to_excel(w, sheet_name='Notes_not_understood_top', index=False)
    D.drop(columns=['month']).to_csv(os.path.join(a.out, 'audit_sf_service_daily.csv'), index=False, encoding='utf-8-sig')
    print(mo.tail(14).round(0).to_string(index=False)); print('written:', xl)
    if a.push:
        import requests
        url, key = os.environ['SUPABASE_URL'].rstrip('/'), os.environ['SUPABASE_SERVICE_ROLE_KEY']
        H = {'apikey': key, 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
        r = requests.delete(f'{url}/rest/v1/audit_sf_service_daily?service_date=gte.1900-01-01', headers={**H, 'Prefer': 'return=minimal'}, timeout=120); r.raise_for_status()
        rows = [{'service_date': x.date, 'cat': x.cat, 'value': x.value, 'value_main': x.value_main, 'pieces': x.pieces, 'receipts': x.receipts, 'patients': x.patients} for x in D.itertuples()]
        for i in range(0, len(rows), 500):
            r = requests.post(f'{url}/rest/v1/audit_sf_service_daily?on_conflict=service_date,cat', headers={**H, 'Prefer': 'resolution=merge-duplicates,return=minimal'}, data=json.dumps(rows[i:i + 500]), timeout=120); r.raise_for_status()
        meta = {'built_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'parts': meta_parts, 'lines': st['lines'], 'notes': {k: st[k] for k in ('qty', 'dates', 'rejected', 'no_date')}, 'no_date_value': round(st['no_date_value'], 2), 'total': round(float(D.value.sum()), 2), 'total_main_only': round(float(D.value_main.sum()), 2), 'new_site_start': ns}
        r = requests.post(f'{url}/rest/v1/audit_settings?on_conflict=key', headers={**H, 'Prefer': 'resolution=merge-duplicates,return=minimal'}, data=json.dumps([{'key': 'sf_ledger', 'value': meta}]), timeout=60); r.raise_for_status()
        print(f'pushed {len(rows):,} daily rows + meta to Supabase')
    print(f'done in {time.time() - t0:.0f}s')

if __name__ == '__main__':
    main()
