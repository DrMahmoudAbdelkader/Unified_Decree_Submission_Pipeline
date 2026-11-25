/**
 * claims-audit.js — Claims ⇄ Revenue audit («مطابقة المطالبات بالإيرادات»)
 * ─────────────────────────────────────────────────────────────────────────────
 * Proves (or exposes the gap in) the identity
 *
 *      Revenue  =  Claimed  +  Not-claimed-yet  +  Other payers settled on the spot
 *
 * for the whole past window (one cumulative check, then "accept the close") and then month by month.
 *
 * Evidence per payer (nothing here is typed twice — every source is the module that already owns it):
 *   Revenue        revenue_entries (direct rows only) + legacy month totals from monthly_revenue_periods
 *   State fund     SMC dashboard snapshot (review + accounts + unsent + returned + paid)      → claimed
 *                  pending_decree_orders/items (Enhanced Monitor, not completed)               → not claimed yet
 *   HIO            cover letters (audit_claim_batches) BEFORE the app produced claims          → claimed
 *                  financial_claims + purchases (hi-claims module) FROM the app cut-over       → claimed
 *                  purchases with no claim yet (from the cut-over)                             → not claimed yet
 *   UHIA           monthly cover letters (Excel import)                                        → claimed
 *   Months without letters   the closing ledger's per-payer claimed breakdown (monthly_revenue_claimed_breakdown)
 *
 * Tables: run claims-audit-migration.sql once.   Loader id: 'claimsaudit' → window.loadClaimsAudit().
 */
(function () {
    'use strict';

    const SUPABASE_URL = 'https://rwvxhoubyztyeeyhamrn.supabase.co';

    /* ═════════════════════════════════════════════════════════════════════════
     *  PURE CORE (no DOM, no network) — also exercised from Node tests
     * ═════════════════════════════════════════════════════════════════════════ */
    const num = v => { if (typeof v === 'number') return Number.isFinite(v) ? v : 0; const n = parseFloat(String(v == null ? '' : v).replace(/[,\s\u066C]/g, '')); return Number.isFinite(n) ? n : 0; };
    const r2 = v => Math.round((num(v) + Number.EPSILON) * 100) / 100;
    const pad = n => String(n).padStart(2, '0');
    const GROUPS = ['SF', 'HIO', 'UHIA'];
    const GROUP_AR = { SF: 'نفقة الدولة', HIO: 'التأمين الصحي', UHIA: 'التأمين الشامل', OTHER: 'جهات أخرى' };

    /* ── dates: everything is a plain 'YYYY-MM-DD' / 'YYYY-MM' string in Cairo local time ── */
    const CAIRO = new Intl.DateTimeFormat('en-CA', { timeZone: 'Africa/Cairo', year: 'numeric', month: '2-digit', day: '2-digit' });
    function dayOf(v) {
        if (!v) return '';
        if (v instanceof Date) return isNaN(v) ? '' : CAIRO.format(v);
        const s = String(v);
        if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
        if (/^\d{4}-\d{2}-\d{2}[T ]/.test(s)) { if (/[zZ]$|[+-]\d{2}(:?\d{2})?$/.test(s)) { const d = new Date(s); if (!isNaN(d)) return CAIRO.format(d); } return s.slice(0, 10); }
        return s.slice(0, 10);
    }
    const localToday = () => CAIRO.format(new Date());
    const ym = v => dayOf(v).slice(0, 7);
    function addM(k, n) { const [y, m] = String(k).slice(0, 7).split('-').map(Number); const t = y * 12 + (m - 1) + n; return `${Math.floor(t / 12)}-${pad(t % 12 + 1)}`; }
    const mStart = k => String(k).slice(0, 7) + '-01';
    function mEnd(k) { const [y, m] = String(k).slice(0, 7).split('-').map(Number); return `${y}-${pad(m)}-${pad(new Date(Date.UTC(y, m, 0)).getUTCDate())}`; }
    function monthsBetween(a, b) { const out = []; for (let k = String(a).slice(0, 7); k <= String(b).slice(0, 7); k = addM(k, 1)) out.push(k); return out; }
    /** Excel serial · Date · ISO · D/M/Y (or M/D/Y with {us:true}) → 'YYYY-MM-DD' ('' when it is not a date) */
    function toISODate(v, opt) {
        opt = opt || {};
        if (v == null || v === '') return '';
        if (v instanceof Date) return isNaN(v) ? '' : `${v.getFullYear()}-${pad(v.getMonth() + 1)}-${pad(v.getDate())}`;
        if (typeof v === 'number') return (v > 20000 && v < 80000) ? new Date(Date.UTC(1899, 11, 30) + Math.round(v) * 86400000).toISOString().slice(0, 10) : '';
        const s = String(v).trim();
        let m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s); if (m) return `${m[1]}-${m[2]}-${m[3]}`;
        m = /^(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{4})/.exec(s);
        if (m) { const a = +m[1], b = +m[2], d = opt.us ? b : a, mo = opt.us ? a : b; return (mo < 1 || mo > 12 || d < 1 || d > 31) ? '' : `${m[3]}-${pad(mo)}-${pad(d)}`; }
        if (/^\d{5}$/.test(s)) return toISODate(Number(s));
        return '';
    }
    const grossOf = p => r2(num(p.quantity) * num(p.unit_price));

    /* ── State-fund (SMC) dashboard ───────────────────────────────────────────
     * flag → stage. The dashboard cards: «المراجعة» (A01 doctor, A02 pharmacist, A0 finance/admin),
     * «إدارة الحسابات – تم الصرف» (PAID), «لم تُرسل» (P), «مرتجعة» (R01); plus the all-time paid total.        */
    const SMC_STAGES = {
        A01: { ar: 'مراجعة — طبيب', grp: 'review' }, A02: { ar: 'مراجعة — صيدلي', grp: 'review' }, A0: { ar: 'مراجعة — مالي وإداري', grp: 'review' },
        PAID: { ar: 'إدارة الحسابات (تم الصرف)', grp: 'accounts' }, P: { ar: 'لم تُرسل بعد', grp: 'unsent' }, R01: { ar: 'مرتجعة للمستشفى', grp: 'returned' },
    };
    const stripHtml = t => String(t || '').replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/gi, ' ').replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/\s+/g, ' ').trim();
    /** the dashboard page itself: card numbers, paid/unpaid totals, the search form values */
    function parseSmcDashboardHtml(html) {
        const t = stripHtml(html), g = re => { const m = re.exec(t); return m ? num(m[1]) : null; };
        const inp = id => { const m = new RegExp('<input[^>]*\\b(?:id|name)="' + id + '"[^>]*>', 'i').exec(html || ''); if (!m) return ''; const v = /\bvalue="([^"]*)"/i.exec(m[0]); return v ? v[1] : ''; };
        const cards = {}, re = /<a[^>]*href="[^"]*DashboardDrillDown[^"]*[?&;]flag=([A-Za-z0-9_]+)[^"]*"[^>]*>\s*([\d,]+)\s*<\/a>/gi; let m;
        while ((m = re.exec(html || ''))) cards[m[1]] = num(m[2]);
        return {
            total_invoices: g(/إجمالي عدد فواتير الجهة\s*([\d,]+)/),
            paid: { count: g(/عدد الفواتير المسددة\s*([\d,]+)/), value: g(/إجمالي قيمة الفواتير المسددة\s*([\d,]+)/) },
            unpaid: { count: g(/عدد الفواتير الغير مسددة\s*([\d,]+)/), value: g(/إجمالي قيمة الفواتير الغير مسددة\s*([\d,]+)/) },
            cards, date_from: toISODate(inp('dateFrom')), date_to: toISODate(inp('dateTo')), hospital_id: inp('HospitalID'),
        };
    }
    /** one DashboardDrillDown summary page → {count, value} (sums the hospital rows: td[1]=count, td[2]=cost) */
    function parseSmcSumPage(html) {
        let c = 0, v = 0, found = false; const trs = String(html || '').match(/<tr[\s\S]*?<\/tr>/gi) || [];
        for (const tr of trs) { const td = (tr.match(/<td[\s\S]*?<\/td>/gi) || []).map(stripHtml); if (td.length >= 3 && /^[\d,.\s]+$/.test(td[1]) && /^[\d,.\s]+$/.test(td[2])) { c += num(td[1]); v += num(td[2]); found = true; } }
        return found ? { count: c, value: r2(v) } : null;
    }
    function harText(e) {
        const c = (e.response && e.response.content) || {}; let t = c.text || '';
        if (c.encoding === 'base64' && t) { try { const bin = atob(t); t = new TextDecoder('utf-8').decode(Uint8Array.from(bin, ch => ch.charCodeAt(0))); } catch (_) { } }
        return t;
    }
    /** a browser HAR saved while looking at Decrees/Dashboard (+ the drill-down links) → snapshot */
    function parseSmcHar(har) {
        const es = (har && har.log && har.log.entries) || [], stages = {}; let dash = null, dashGet = null, from = '', to = '', hosp = '';
        for (const e of es) {
            const u = (e.request && e.request.url) || '', meth = e.request && e.request.method;
            if (/\/Decrees\/Dashboard(\?|$)/.test(u)) { const h = harText(e); if (h && /DashboardDrillDown/.test(h)) { if (meth === 'POST') dash = { e, html: h }; else dashGet = { e, html: h }; } }
            const m = /\/Decrees\/DashboardDrillDown\?(.*)$/.exec(u);
            if (m) {
                const q = new URLSearchParams(m[1].replace(/&amp;/g, '&')), flag = q.get('flag'); if (!flag) continue;
                const s = parseSmcSumPage(harText(e)); if (!s) continue;
                stages[flag] = s; from = toISODate(q.get('dateFrom'), { us: true }) || from; to = toISODate(q.get('dateTo'), { us: true }) || to; hosp = q.get('HospitalID') || hosp;
            }
        }
        const d = dash || dashGet, p = d ? parseSmcDashboardHtml(d.html) : { cards: {}, paid: {}, unpaid: {} };
        return buildSnapshot({ snapshot_date: d ? dayOf(new Date(d.e.startedDateTime)) : localToday(), date_from: from || p.date_from, date_to: to || p.date_to, hospital_id: hosp || p.hospital_id, total_invoices: p.total_invoices, stages, paid: p.paid, unpaid: p.unpaid, cards: p.cards, source: 'har' });
    }
    /** normalises any of: HAR result · bookmarklet JSON · hand-typed form → the stored snapshot shape */
    function buildSnapshot(j) {
        j = j || {};
        const val = v => (v == null || v === '') ? null : num(v), stages = {}, warnings = [], unvalued = [];
        Object.keys(j.stages || {}).forEach(k => { const s = j.stages[k] || {}; stages[k] = { count: num(s.count), value: val(s.value) }; });
        const cards = j.cards || {};
        Object.keys(cards).forEach(k => {
            if (!SMC_STAGES[k]) { if (num(cards[k]) > 0) warnings.push(`مرحلة غير معروفة (${k}) بها ${num(cards[k]).toLocaleString()} فاتورة — لم تُحتسب`); return; }
            if (!stages[k]) { if (num(cards[k]) > 0) { unvalued.push(k); stages[k] = { count: num(cards[k]), value: null }; } }
            else if (num(cards[k]) !== stages[k].count) warnings.push(`${SMC_STAGES[k].ar}: اللوحة تعرض ${num(cards[k]).toLocaleString()} فاتورة والتفصيل ${stages[k].count.toLocaleString()} (فرق ${(num(cards[k]) - stages[k].count).toLocaleString()} فاتورة بلا قيمة في التفصيل)`);
        });
        Object.keys(stages).forEach(k => { if (stages[k].value == null && !unvalued.includes(k)) unvalued.push(k); });
        const paid = j.paid || { count: j.paid_count, value: j.paid_value }, unpaid = j.unpaid || { count: j.unpaid_count, value: j.unpaid_value };
        return { snapshot_date: j.snapshot_date || localToday(), date_from: toISODate(j.date_from) || '', date_to: toISODate(j.date_to) || '', hospital_id: String(j.hospital_id || ''), total_invoices: val(j.total_invoices), stages, paid_count: val(paid.count), paid_value: val(paid.value), unpaid_count: val(unpaid.count), unpaid_value: val(unpaid.value), source: j.source || 'manual', note: j.note || '', unvalued, warnings };
    }
    /** snapshot → the five buckets of money the state fund still holds an invoice for */
    function smcTotals(s) {
        const st = s.stages || {}, v = k => (st[k] && st[k].value != null) ? num(st[k].value) : 0;
        const t = { review: r2(v('A01') + v('A02') + v('A0')), accounts: r2(st.PAID && st.PAID.value != null ? st.PAID.value : num(s.unpaid_value)), unsent: r2(v('P')), returned: r2(v('R01')), paid: r2(num(s.paid_value)), warnings: [...(s.warnings || [])] };
        t.total = r2(t.review + t.accounts + t.unsent + t.returned + t.paid);
        const un = Object.keys(st).filter(k => st[k].value == null); if (un.length) t.warnings.push('مراحل بلا قيمة: ' + un.join(', '));
        const cnt = Object.values(st).reduce((a, x) => a + num(x.count), 0);
        if (s.total_invoices != null && cnt !== num(s.total_invoices)) t.warnings.push(`مجموع فواتير المراحل ${cnt.toLocaleString()} ≠ إجمالي اللوحة ${num(s.total_invoices).toLocaleString()} (فرق ${(num(s.total_invoices) - cnt).toLocaleString()} فاتورة — غالباً المرتجعة)`);
        return t;
    }
    /** runs INSIDE the SMC site (as a bookmarklet): reads the dashboard cards + every drill-down summary, copies JSON. Self-contained on purpose. */
    function smcCollectorSource() {
        var num = function (s) { var n = parseFloat(String(s == null ? '' : s).replace(/[^0-9.\-]/g, '')); return isFinite(n) ? n : 0; };
        var val = function (id) { var e = document.getElementById(id) || document.querySelector('[name="' + id + '"]'); return e ? e.value : ''; };
        var links = Array.prototype.slice.call(document.querySelectorAll('a[href*="DashboardDrillDown"]'));
        if (!links.length) { alert('افتح صفحة Decrees/Dashboard واضغط بحث أولاً ثم اضغط الإشارة'); return; }
        var txt = (document.body.innerText || document.body.textContent || '').replace(/\s+/g, ' ');
        var pick = function (re) { var m = re.exec(txt); return m ? num(m[1]) : null; };
        var out = {
            v: 1, source: 'bookmarklet', snapshot_date: new Date().toLocaleDateString('en-CA', { timeZone: 'Africa/Cairo' }),
            date_from: val('dateFrom'), date_to: val('dateTo'), hospital_id: val('HospitalID'),
            total_invoices: pick(/إجمالي عدد فواتير الجهة\s*([\d,]+)/),
            paid: { count: pick(/عدد الفواتير المسددة\s*([\d,]+)/), value: pick(/إجمالي قيمة الفواتير المسددة\s*([\d,]+)/) },
            unpaid: { count: pick(/عدد الفواتير الغير مسددة\s*([\d,]+)/), value: pick(/إجمالي قيمة الفواتير الغير مسددة\s*([\d,]+)/) },
            stages: {}, cards: {}
        };
        var jobs = links.map(function (a) {
            var m = /[?&]flag=([^&]+)/.exec(a.href); if (!m) return Promise.resolve();
            var flag = decodeURIComponent(m[1]); if (out.cards[flag] != null) return Promise.resolve();
            out.cards[flag] = num(a.textContent);
            return fetch(a.href, { credentials: 'include' }).then(function (r) { return r.text(); }).then(function (h) {
                var d = new DOMParser().parseFromString(h, 'text/html'), c = 0, v = 0, found = false;
                Array.prototype.forEach.call(d.querySelectorAll('tr'), function (tr) {
                    var td = tr.querySelectorAll('td');
                    if (td.length >= 3 && /^[\d,.\s]+$/.test(td[1].textContent.trim()) && /^[\d,.\s]+$/.test(td[2].textContent.trim())) { c += num(td[1].textContent); v += num(td[2].textContent); found = true; }
                });
                if (found) out.stages[flag] = { count: c, value: v };
            }).catch(function () { });
        });
        Promise.all(jobs).then(function () {
            var t = JSON.stringify(out);
            try { navigator.clipboard.writeText(t); } catch (e) { }
            window.prompt('تم نسخ النتيجة — الصقها في خانة (ج) داخل صفحة المطابقة (Ctrl+C إن لم تُنسخ تلقائياً)', t);
        });
    }

    /* ── pasted "national id ⇥ name ⇥ amount" lines ── */
    function parsePastedLines(text) {
        const out = [], bad = [];
        for (const raw of String(text || '').split(/\r?\n/)) {
            const line = raw.trim(); if (!line) continue;
            let nid = '', name = '', amt = '';
            if (/[\t;]/.test(line)) {
                const parts = line.split(/[\t;]/).map(s => s.trim()).filter(Boolean);
                amt = parts.pop() || ''; nid = parts.find(p => /^\d{10,16}$/.test(p)) || ''; name = parts.filter(p => p !== nid).join(' ');
            } else {
                const m = /^(\d{10,16})?\s*(.*?)\s+(-?[\d,]+(?:\.\d+)?)$/.exec(line);
                if (m) { nid = m[1] || ''; name = m[2]; amt = m[3]; }
            }
            if (!/^-?[\d,]+(\.\d+)?$/.test(amt) || (!nid && !name)) { bad.push(line); continue; }
            out.push({ national_id: nid, patient_name: name, amount: r2(num(amt)), invoice_no: '', note: '' });
        }
        return { rows: out, bad };
    }

    /* ── Excel importers ── */
    /** header row = first of the first 15 rows where every required column regex matches some cell → {at, col:{key:index}} */
    function findHeader(rows, cols, required) {
        for (let i = 0; i < Math.min(rows.length, 15); i++) {
            const row = rows[i] || [], col = {};
            Object.keys(cols).forEach(k => { const j = row.findIndex(c => c != null && cols[k].test(String(c).replace(/\s+/g, ' ').trim())); if (j >= 0) col[k] = j; });
            if (required.every(k => col[k] != null)) return { at: i, col };
        }
        return null;
    }
    const UHIA_COLS = { month: /شهر الصرف|claim month/i, nid: /الرقم القومي|national/i, inv: /رقم الفاتورة|invoice/i, name: /اسم المريض|patient/i, amount: /صافي المبلغ|net claimed/i, note: /ملاحظات|notes/i, branch: /الفرع|branch/i };
    /**
     * UHIA «Claimed values» workbook. The same patient/invoice appears in several sheets (monthly files re-exported), so rows are
     * de-duplicated ACROSS sheets only (same invoice + national id + month + amount) — duplicates inside one sheet are real and kept.
     */
    function parseUhiaSheets(sheets) {
        const rows = [], dropped = [], perSheet = [], seen = new Map();
        for (const sh of sheets) {
            const H = findHeader(sh.rows || [], UHIA_COLS, ['month', 'amount']);
            if (!H) { perSheet.push({ name: sh.name, rows: 0, error: 'لم أجد عمودي «شهر الصرف» و«صافي المبلغ المطالب»' }); continue; }
            const cur = new Map(); let n = 0;
            for (const r of sh.rows.slice(H.at + 1)) {
                const iso = toISODate(r[H.col.month]); if (!iso) continue; n++;
                const g = k => (H.col[k] == null ? '' : r[H.col[k]]);
                const row = { claim_month: mStart(iso), national_id: String(g('nid') == null ? '' : g('nid')).replace(/\D/g, ''), patient_name: String(g('name') || '').trim(), invoice_no: String(g('inv') == null ? '' : g('inv')).trim(), amount: r2(num(g('amount'))), branch: String(g('branch') || '').trim(), note: String(g('note') || '').trim() };
                row.flag = (/مبلغ مقطوع/.test(row.note)) ? 'lump_sum' : '';
                const key = `${row.invoice_no}|${row.national_id}|${row.claim_month}|${row.amount.toFixed(2)}`;
                if ((seen.get(key) || 0) > 0) { seen.set(key, -1); dropped.push(row); continue; }
                rows.push(row); cur.set(key, (cur.get(key) || 0) + 1);
            }
            cur.forEach((c, k) => { if (!(seen.get(k) < 0)) seen.set(k, (seen.get(k) || 0) + c); });
            perSheet.push({ name: sh.name, rows: n });
        }
        const byMonth = {}; rows.forEach(r => { const o = byMonth[r.claim_month] || (byMonth[r.claim_month] = { rows: 0, total: 0, lump: 0 }); o.rows++; o.total = r2(o.total + r.amount); if (r.flag) o.lump++; });
        return { rows, dropped, perSheet, byMonth, total: r2(rows.reduce((s, r) => s + r.amount, 0)), droppedTotal: r2(dropped.reduce((s, r) => s + r.amount, 0)) };
    }

    const HIO_COLS = { month: /^month$|^الشهر$/i, name: /^patient|اسم المريض/i, nid: /national/i, ins: /insurance/i, inv: /invoice/i, appr: /approval/i, law: /benefit law/i, issue: /issue date/i, branch: /^branch|الفرع/i, amount: /claim value|قيمة المطالبة/i, source: /^source$|المصدر/i, note: /^note$|ملاحظة/i };
    const MONTHS_EN = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
    const MONTHS_AR = { 'يناير': 1, 'فبراير': 2, 'مارس': 3, 'ابريل': 4, 'أبريل': 4, 'مايو': 5, 'يونيو': 6, 'يوليو': 7, 'اغسطس': 8, 'أغسطس': 8, 'سبتمبر': 9, 'اكتوبر': 10, 'أكتوبر': 10, 'نوفمبر': 11, 'ديسمبر': 12 };
    function monthsInLabel(label) {
        const out = []; String(label).replace(/\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*/gi, (_, m) => { out.push(MONTHS_EN[m.toLowerCase()]); return _; });
        if (!out.length) Object.keys(MONTHS_AR).forEach(k => { if (String(label).includes(k)) out.push(MONTHS_AR[k]); });
        return out;
    }
    /**
     * HIO «Claims Combined» workbook ("All Claims" sheet). Month labels carry no year ("Dec", "Jan"…): the year is inferred
     * going forward from the audit window start (Dec → Dec 2025 when the window starts Jun 2025), +1 each time the month number drops.
     * A label with a year ("Sep 2026 (Excel)") is taken as written. A label that spans two months ("Sep-Oct 2026 (PDF)") is ONE claim
     * that cannot be split: it is booked on its LAST month and flagged `combined`.
     */
    function parseHioCombined(sheets, opt) {
        opt = opt || {}; const ws = opt.windowStart || '2025-06-01', sy = +ws.slice(0, 4), sm = +ws.slice(5, 7);
        let H = null, sheet = null;
        for (const sh of sheets) { H = findHeader(sh.rows || [], HIO_COLS, ['month', 'amount']); if (H) { sheet = sh; break; } }
        if (!H) return { rows: [], groups: [], perSheet: sheets.map(s => ({ name: s.name, rows: 0 })), total: 0, error: 'لم أجد عمودي «Month» و«Claim Value» في أي ورقة' };
        const labelMonth = new Map(); let lastY = null, lastM = null;
        const resolve = label => {
            if (labelMonth.has(label)) return labelMonth.get(label);
            const ms = monthsInLabel(label), yr = /(20\d\d)/.exec(label); let res = null;
            if (ms.length) {
                const m = ms[ms.length - 1]; let y;
                if (yr) y = +yr[1]; else if (lastY == null) y = m >= sm ? sy : sy + 1; else y = m < lastM ? lastY + 1 : lastY;
                lastY = y; lastM = m; res = { claim_month: `${y}-${pad(m)}-01`, combined: ms.length > 1, from: ms.length > 1 ? `${yr ? +yr[1] : y}-${pad(ms[0])}` : '' };
            }
            labelMonth.set(label, res); return res;
        };
        const rows = [], skipped = [];
        for (const r of sheet.rows.slice(H.at + 1)) {
            const label = String(r[H.col.month] == null ? '' : r[H.col.month]).trim(); if (!label) continue;
            const g = k => (H.col[k] == null ? '' : r[H.col[k]]), mo = resolve(label);
            if (!mo) { skipped.push(label); continue; }
            rows.push({ claim_month: mo.claim_month, combined: mo.combined, label, national_id: String(g('nid') == null ? '' : g('nid')).replace(/\D/g, ''), patient_name: String(g('name') || '').trim(), invoice_no: String(g('inv') == null ? '' : g('inv')).trim(), approval_no: String(g('appr') || '').trim(), amount: r2(num(g('amount'))), branch: String(g('branch') || '').trim(), note: String(g('note') || '').trim(), file: String(g('source') || '').split(/ \(cover/)[0].trim() });
        }
        const gm = new Map();
        rows.forEach(r => { const key = r.claim_month + (r.combined ? '|combined' : ''); let o = gm.get(key); if (!o) { o = { key, claim_month: r.claim_month, combined: r.combined, label: `HIO ${ym(r.claim_month)}${r.combined ? ' PDF' : ''}`, from_label: r.label, rows: 0, total: 0, files: new Set() }; gm.set(key, o); } o.rows++; o.total = r2(o.total + r.amount); if (r.file) o.files.add(r.file); });
        const groups = [...gm.values()].map(o => ({ ...o, files: [...o.files] })).sort((a, b) => a.claim_month.localeCompare(b.claim_month));
        return { rows, groups, skipped: [...new Set(skipped)], perSheet: [{ name: sheet.name, rows: rows.length }], total: r2(rows.reduce((s, r) => s + r.amount, 0)) };
    }

    /* ── letters / batches ── */
    /** cover letters count as evidence only BEFORE the app cut-over (after it the app's own claims are the evidence); manual claims always count */
    function batchCounts(b, cutoff) {
        if (b.status === 'draft') return false;
        if (b.payer === 'UHIA' || b.source === 'manual') return true;
        return !(cutoff && ym(b.claim_month) >= cutoff);
    }
    const batchTotal = b => (b.declared_total != null && b.declared_total !== '') ? num(b.declared_total) : num(b.lines_total);
    function batchChecks(bs) {
        const out = [], seen = {};
        (bs || []).forEach(b => {
            const k = `${b.payer}|${ym(b.claim_month)}`; seen[k] = (seen[k] || 0) + 1;
            const decl = (b.declared_total != null && b.declared_total !== '') ? num(b.declared_total) : null;
            if (decl == null && !b.lines_count) out.push({ batch: b, tone: 'bad', msg: 'لا إجمالي ولا مرضى — الدفعة لا تضيف قيمة' });
            if (decl != null && b.lines_count && Math.abs(decl - num(b.lines_total)) > 1) out.push({ batch: b, tone: 'warn', msg: `إجمالي الخطاب ${decl.toLocaleString()} ≠ مجموع المرضى ${num(b.lines_total).toLocaleString()} (فرق ${r2(num(b.lines_total) - decl).toLocaleString()}) — المحتسب هو إجمالي الخطاب` });
            if (b.lump_count) out.push({ batch: b, tone: 'info', msg: `${b.lump_count} مبلغ مقطوع محتمل (زرع/ثابت) يحتاج مراجعة فاتورة H.I.S.` });
            if (b.status === 'draft') out.push({ batch: b, tone: 'info', msg: 'مسودة — لا تُحتسب' });
        });
        (bs || []).forEach(b => { const k = `${b.payer}|${ym(b.claim_month)}`; if (seen[k] > 1) { out.push({ batch: b, tone: 'info', msg: `يوجد ${seen[k]} دفعات لنفس الشهر — تأكد أنها غير مكررة` }); seen[k] = 0; } });
        return out;
    }

    /* ── revenue ── */
    /** (financial_category_id, department_id) → audit group. Same ids as revenue-cycle-closing.js classifyBucket:
     *  4 = state fund, 1 = HIO, 6 = UHIA; department 9 (O.R) = waiting lists, which no claim cycle covers → OTHER. Everything else = OTHER. */
    function groupOf(fc, dept) {
        fc = Number(fc); dept = Number(dept);
        if (fc === 4) return dept === 9 ? 'OTHER' : 'SF';
        if (fc === 1) return dept === 9 ? 'OTHER' : 'HIO';
        if (fc === 6) return 'UHIA';
        return 'OTHER';
    }
    /** revenue_entries rows → { 'YYYY-MM-DD': {SF,HIO,UHIA,OTHER} }.  Cross-department rows (department ≠ sub-department) are the tracking-only twin of a direct row and are skipped (same rule as the closing ledger). */
    function aggregateRevenue(entries) {
        const by = {};
        (entries || []).forEach(r => {
            if (r.department_id !== r.sub_department_id) return;
            const d = dayOf(r.revenue_date); if (!d) return; const g = groupOf(r.financial_category_id, r.department_id);
            const o = by[d] || (by[d] = { SF: 0, HIO: 0, UHIA: 0, OTHER: 0 }); o[g] = r2(o[g] + num(r.total_value));
        });
        return by;
    }
    /** months of the closing ledger that have no daily entries (the typed legacy months) enter as ONE undivided LEGACY figure on the 1st of the month */
    function addLegacy(byDay, periods) {
        const has = new Set(Object.keys(byDay).filter(d => Object.keys(byDay[d]).some(k => k !== 'LEGACY')).map(d => d.slice(0, 7))), legacy = [];
        (periods || []).forEach(p => {
            const m = ym(p.period_month); if (!m || has.has(m)) return;
            const v = p.actual_revenue_total != null ? num(p.actual_revenue_total) : num(p.billed_total); if (!v) return;
            (byDay[mStart(m)] = byDay[mStart(m)] || {}).LEGACY = r2(v); legacy.push(m);
        });
        return legacy.sort();
    }
    function sumRevenue(D, from, to) {
        const o = { SF: 0, HIO: 0, UHIA: 0, OTHER: 0, LEGACY: 0 };
        Object.keys(D.revenueByDay || {}).forEach(d => { if (d < from || d > to) return; const x = D.revenueByDay[d]; Object.keys(o).forEach(k => { o[k] += x[k] || 0; }); });
        Object.keys(o).forEach(k => { o[k] = r2(o[k]); }); o.total = r2(o.SF + o.HIO + o.UHIA + o.OTHER + o.LEGACY); return o;
    }

    /* ── evidence at a date ── */
    const snapAt = (D, date) => { let best = null; (D.snaps || []).forEach(s => { if (s.snapshot_date <= date && (!best || s.snapshot_date > best.snapshot_date)) best = s; }); return best; };
    function manualAt(D, date) {
        const o = { SF: 0, HIO: 0, UHIA: 0 }, at = {};
        (D.balances || []).forEach(b => { if (b.as_of <= date && o[b.grp] != null && (at[b.grp] == null || b.as_of >= at[b.grp])) { o[b.grp] = num(b.amount); at[b.grp] = b.as_of; } });
        return o;
    }
    /** decree services entered in Enhanced Monitor and not yet registered/billed at `date` */
    function pendingSF(items, date) {
        let count = 0, value = 0;
        (items || []).forEach(i => {
            const d = dayOf(i.dispensing_date || i.order_dispensing_date || i.created_at); if (!d || d > date) return;
            if (i.order_status === 'completed' && (!i.completed_at || dayOf(i.completed_at) <= date)) return;
            count++; value += num(i.total_value);
        });
        return { count, value: r2(value) };
    }
    const cutoffOf = D => (D.settings && D.settings.hi_app_cutoff) ? ym(D.settings.hi_app_cutoff) : '';
    const appStart = D => cutoffOf(D) ? mStart(cutoffOf(D)) : ((D.settings && D.settings.window_start) || '0000-00-00');
    /** HIO services dispensed in the app and not in a claim yet at `date` (only from the cut-over: earlier ones were claimed by letters) */
    function appUnclaimedAt(D, date) {
        let v = 0; const from = appStart(D);
        (D.purchases || []).forEach(p => {
            if (p.is_refunded === true || !p.service_date || p.service_date > date || p.service_date < from) return;
            const cg = p.claim_id != null ? (D.claimGen || {})[p.claim_id] : null;
            if (p.claim_id == null || (cg && dayOf(cg) > date)) v += grossOf(p);
        });
        return r2(v);
    }
    /** gross of purchases inside claims the app generated in [fromDay, toDay] (and not before the cut-over) */
    function appClaimedBetween(D, fromDay, toDay) {
        let v = 0; const cut = cutoffOf(D);
        (D.purchases || []).forEach(p => {
            if (p.is_refunded === true || p.claim_id == null) return; const cg = (D.claimGen || {})[p.claim_id]; if (!cg) return;
            const d = dayOf(cg); if (d < fromDay || d > toDay || (cut && ym(cg) < cut)) return; v += grossOf(p);
        });
        return r2(v);
    }
    const bucketGroup = n => /^state fund$/i.test(String(n).trim()) ? 'SF' : /^hio (out|bmt)/i.test(String(n).trim()) ? 'HIO' : /^uhia/i.test(String(n).trim()) ? 'UHIA' : 'OTHER';
    /** closing-ledger claimed for one payer in one month (null = the ledger has nothing per-payer for that month) */
    function ledgerClaimed(D, grp, m) {
        const rows = (D.breakdown || []).filter(r => ym(r.period_month) === m); if (!rows.length) return null;
        return r2(rows.filter(r => bucketGroup(r.bucket_name) === grp).reduce((s, r) => s + num(r.claimed_amount), 0));
    }
    const batchesIn = (D, payer, m, counted) => (D.batches || []).filter(b => b.payer === payer && ym(b.claim_month) === m && (!counted || batchCounts(b, cutoffOf(D))));
    /** claims of one payer in one month from letters (+ the ledger when there is no letter for that month) */
    function letterClaimed(D, payer, m) {
        const list = batchesIn(D, payer, m, true);
        if (list.length) return { total: r2(list.reduce((s, b) => s + batchTotal(b), 0)), pending: r2(list.filter(b => b.status === 'pending_delivery').reduce((s, b) => s + batchTotal(b), 0)), ledger: 0, src: 'letter' };
        const cut = cutoffOf(D);
        if (payer === 'HIO' && cut && m >= cut) return { total: 0, pending: 0, ledger: 0, src: 'app' };
        const lc = ledgerClaimed(D, payer, m);
        return lc != null && lc > 0 ? { total: lc, pending: 0, ledger: lc, src: 'ledger' } : { total: 0, pending: 0, ledger: 0, src: 'none' };
    }
    function openAt(D, g, date) {
        const man = manualAt(D, date);
        if (g === 'SF') return r2(pendingSF(D.pendingItems, date).value + man.SF);
        if (g === 'HIO') return r2(appUnclaimedAt(D, date) + man.HIO);
        return r2(man.UHIA);
    }

    /** everything the cumulative reconciliation needs, as of `asOf` (YYYY-MM-DD) */
    function evalAt(D, asOf) {
        const S = D.settings || {}, from = S.window_start || '2025-06-01', cut = cutoffOf(D), incomplete = [];
        const revenue = sumRevenue(D, from, asOf), snap = snapAt(D, asOf), smc = snap ? smcTotals(snap) : null, man = manualAt(D, asOf);
        const P = { hioLetters: 0, hioPending: 0, hioApp: 0, hioLedger: 0, uhiaLetters: 0, uhiaPending: 0, uhiaLedger: 0, pendingSF: pendingSF(D.pendingItems, asOf), manual: man };
        const missing = { HIO: [], UHIA: [] };
        monthsBetween(from, asOf).forEach(m => {
            ['HIO', 'UHIA'].forEach(payer => {
                const lc = letterClaimed(D, payer, m);
                if (payer === 'HIO') { P.hioLetters += lc.total - lc.pending - lc.ledger; P.hioPending += lc.pending; P.hioLedger += lc.ledger; } else { P.uhiaLetters += lc.total - lc.pending - lc.ledger; P.uhiaPending += lc.pending; P.uhiaLedger += lc.ledger; }
                if (lc.src === 'none') { const rv = sumRevenue(D, mStart(m), mEnd(m)); if (rv[payer] > 0 || rv.LEGACY > 0) missing[payer].push(m); }
            });
        });
        P.hioApp = appClaimedBetween(D, from, asOf);
        ['hioLetters', 'hioPending', 'hioLedger', 'uhiaLetters', 'uhiaPending', 'uhiaLedger'].forEach(k => { P[k] = r2(P[k]); });
        const claimed = { SF: smc ? smc.total : null, HIO: r2(P.hioLetters + P.hioPending + P.hioLedger + P.hioApp), UHIA: r2(P.uhiaLetters + P.uhiaPending + P.uhiaLedger) };
        const open = { SF: r2(P.pendingSF.value + man.SF), HIO: r2(appUnclaimedAt(D, asOf) + man.HIO), UHIA: r2(man.UHIA) };
        const nonLegacy = revenue.SF + revenue.HIO + revenue.UHIA + revenue.OTHER;
        const manualShare = (S.other_share_pct !== '' && S.other_share_pct != null) ? num(S.other_share_pct) / 100 : null;
        const otherShare = manualShare != null ? manualShare : (nonLegacy > 0 ? revenue.OTHER / nonLegacy : 0);
        const expectedOther = r2(otherShare * revenue.total);
        const op = S.opening || {}, openingAdj = r2(num(op.SF) + num(op.HIO) + num(op.UHIA));
        const evidence = r2((claimed.SF || 0) + claimed.HIO + claimed.UHIA + open.SF + open.HIO + open.UHIA + expectedOther - openingAdj);
        const gap = r2(revenue.total - evidence), gapPct = revenue.total ? gap / revenue.total : 0, tol = num(S.tolerance_pct == null ? 2 : S.tolerance_pct) / 100;
        const status = Math.abs(gapPct) <= tol ? 'ok' : Math.abs(gapPct) <= 2 * tol ? 'warn' : 'bad';
        const rng = a => a.length > 3 ? `${a[0]} … ${a[a.length - 1]} (${a.length} شهر)` : a.join('، ');
        if (!snap) incomplete.push('لا توجد لقطة SMC حتى هذا التاريخ — نفقة الدولة بلا دليل مطالبات');
        else {
            if (snap.date_from && snap.date_from > from) incomplete.push(`لقطة SMC تبدأ من ${snap.date_from} بعد بداية الفترة ${from} — مطالبات ما قبلها غير مشمولة`);
            if (Math.round((new Date(asOf) - new Date(snap.snapshot_date)) / 86400000) > 35) incomplete.push(`أحدث لقطة SMC بتاريخ ${snap.snapshot_date} — قديمة بالنسبة لتاريخ المطابقة`);
        }
        if (missing.HIO.length) incomplete.push('HIO: لا خطاب ولا قيمة في دفتر الإغلاق لـ ' + rng(missing.HIO));
        if (missing.UHIA.length) incomplete.push('UHIA: لا خطاب ولا قيمة في دفتر الإغلاق لـ ' + rng(missing.UHIA));
        if (D.lastRevenueDate && Math.round((new Date(asOf) - new Date(D.lastRevenueDate)) / 86400000) > 3) incomplete.push(`آخر قيد إيراد يومي ${D.lastRevenueDate} — قبل تاريخ المطابقة`);
        return { asOf, revenue, claimed, open, expectedOther, otherShare, openingAdj, evidence, gap, gapPct, status, incomplete, smc, snap, parts: P, cutoff: cut };
    }

    /** month-by-month: gap = revenue − claimed in the month − change in the open balance */
    function monthlyRows(D, startM, endM) {
        return monthsBetween(startM, endM).map(m => {
            const s = mStart(m), e = mEnd(m), pe = mEnd(addM(m, -1)), rv = sumRevenue(D, s, e), groups = {};
            GROUPS.forEach(g => {
                let claimed = null, note = '';
                if (g === 'SF') {
                    const a = snapAt(D, e), b = snapAt(D, pe);
                    if (!a) note = 'لا توجد لقطة SMC';
                    else if (!b) note = 'أول لقطة — لا يوجد أساس للمقارنة';
                    else if (a.snapshot_date === b.snapshot_date) note = 'لا توجد لقطة جديدة في هذا الشهر';
                    else { claimed = r2(smcTotals(a).total - smcTotals(b).total); if (D.today && a.snapshot_date < e && e < D.today) note = 'آخر لقطة بتاريخ ' + a.snapshot_date; }
                } else if (g === 'HIO') {
                    const lc = letterClaimed(D, 'HIO', m); claimed = r2(lc.total + appClaimedBetween(D, s, e));
                    if (lc.src === 'none' && rv.HIO > 0 && !(cutoffOf(D) && m >= cutoffOf(D))) note = 'لا يوجد خطاب لهذا الشهر'; else if (lc.src === 'ledger') note = 'من دفتر الإغلاق';
                } else {
                    const lc = letterClaimed(D, 'UHIA', m); claimed = lc.total;
                    if (lc.src === 'none' && rv.UHIA > 0) note = 'لا يوجد خطاب لهذا الشهر'; else if (lc.src === 'ledger') note = 'من دفتر الإغلاق';
                }
                const dOpen = r2(openAt(D, g, e) - openAt(D, g, pe));
                groups[g] = { revenue: rv[g], claimed, dOpen, gap: claimed == null ? null : r2(rv[g] - claimed - dOpen), note };
            });
            return { month: m, groups, legacy: rv.LEGACY, other: rv.OTHER };
        });
    }

    const core = {
        num, r2, dayOf, localToday, ym, addM, mStart, mEnd, monthsBetween, toISODate, grossOf, GROUPS, GROUP_AR,
        SMC_STAGES, parseSmcDashboardHtml, parseSmcSumPage, parseSmcHar, buildSnapshot, smcTotals, smcCollectorSource,
        parsePastedLines, parseUhiaSheets, parseHioCombined, batchCounts, batchTotal, batchChecks,
        groupOf, aggregateRevenue, addLegacy, sumRevenue, snapAt, manualAt, pendingSF, appUnclaimedAt, appClaimedBetween, ledgerClaimed, letterClaimed, evalAt, monthlyRows,
    };
    

    /* ═════════════════════════════════════════════════════════════════════════
     *  DATA LAYER
     * ═════════════════════════════════════════════════════════════════════════ */
    const hdrs = () => (window.getHeaders ? window.getHeaders() : {});
    const DEFAULTS = { window_start: '2025-06-01', hi_app_cutoff: '', closed_through: '', other_share_pct: '', tolerance_pct: 2, opening: { SF: 0, HIO: 0, UHIA: 0 }, accepted: null };
    const isMissingTable = e => { const t = JSON.stringify(e && e.response && e.response.data || '') + String(e && e.message || ''); return (e && e.response && e.response.status === 404) || /PGRST205|42P01|Could not find the table|does not exist/i.test(t); };

    async function getAll(path, { pageSize = 1000, conc = 4 } = {}) {
        const sep = path.includes('?') ? '&' : '?', url = o => `${SUPABASE_URL}/rest/v1/${path}${sep}limit=${pageSize}&offset=${o}`;
        const first = await axios.get(url(0), { headers: { ...hdrs(), Prefer: 'count=exact' } });
        let rows = first.data || [];
        if (rows.length < pageSize) return rows;
        const cr = first.headers && (first.headers['content-range'] || first.headers['Content-Range']), total = cr ? Number(String(cr).split('/')[1]) : NaN;
        if (Number.isFinite(total)) {
            const offs = []; for (let o = pageSize; o < total; o += pageSize) offs.push(o);
            for (let i = 0; i < offs.length; i += conc) (await Promise.all(offs.slice(i, i + conc).map(o => axios.get(url(o), { headers: hdrs() })))).forEach(r => { rows = rows.concat(r.data || []); });
            return rows;
        }
        for (let o = pageSize; o < 1000000; o += pageSize) { const r = await axios.get(url(o), { headers: hdrs() }); rows = rows.concat(r.data || []); if ((r.data || []).length < pageSize) break; }
        return rows;
    }
    const write = (method, path, body, prefer) => axios({ method, url: `${SUPABASE_URL}/rest/v1/${path}`, data: body, headers: { ...hdrs(), 'Content-Type': 'application/json', ...(prefer ? { Prefer: prefer } : {}) } });

    const mergeSettings = raw => ({ ...DEFAULTS, ...raw, opening: { ...DEFAULTS.opening, ...(raw.opening || {}) } });
    /** first month the app produced HIO claims = where cover letters stop being the evidence (unless the user set it) */
    const withCutoff = (settings, claims) => (!settings.hi_app_cutoff && claims.length) ? { ...settings, hi_app_cutoff: claims.map(c => ym(c.generated_at)).sort()[0] } : settings;
    function joinBatchLines(batches, lines) {
        const sums = {}; lines.forEach(l => { const o = sums[l.batch_id] || (sums[l.batch_id] = { t: 0, n: 0, lump: 0 }); o.t += num(l.amount); o.n++; if (l.flag) o.lump++; });
        return batches.map(b => ({ ...b, lines_total: r2((sums[b.id] || {}).t || 0), lines_count: (sums[b.id] || {}).n || 0, lump_count: (sums[b.id] || {}).lump || 0 }));
    }
    /** after a save: re-read only the audit_* tables (revenue/purchases are big and unchanged) */
    async function refreshAudit() {
        const D = state.D, errs = {};
        const g = (n, p) => getAll(p).catch(e => { errs[n] = e; return null; });
        const [st, sn, ba, li, bl] = await Promise.all([g('audit_settings', 'audit_settings?select=key,value&key=eq.main'), g('audit_smc_snapshots', 'audit_smc_snapshots?select=*&order=snapshot_date.asc'), g('audit_claim_batches', 'audit_claim_batches?select=*&order=claim_month.asc,id.asc'), g('audit_claim_lines', 'audit_claim_lines?select=batch_id,amount,flag&order=id.asc'), g('audit_manual_balances', 'audit_manual_balances?select=*&order=as_of.asc')]);
        if (st) { D.rawSettings = (st[0] && st[0].value) || {}; D.settings = withCutoff(mergeSettings(D.rawSettings), D.claims); }
        if (sn) D.snaps = sn; if (bl) D.balances = bl; if (ba && li) D.batches = joinBatchLines(ba, li);
        Object.assign(D.errors, errs);
    }
    /** loads every source in parallel; a failing source is reported in D.errors and does not stop the others */
    async function loadAll(onProgress) {
        const today = localToday(), D = { errors: {}, loadedAt: Date.now(), today };
        let raw = {}; try { const r = await getAll('audit_settings?select=key,value&key=eq.main'); raw = (r[0] && r[0].value) || {}; } catch (e) { D.errors.audit_settings = e; }
        const settings = mergeSettings(raw);
        const from = settings.window_start;
        const jobs = {
            periods: () => getAll('monthly_revenue_periods?select=id,period_month,status,data_source,billed_total,actual_revenue_total,audit_recovered_amount&order=period_month.asc'),
            entries: () => getAll(`revenue_entries?select=total_value,revenue_date,department_id,sub_department_id,financial_category_id&revenue_date=gte.${from}&revenue_date=lte.${today}&order=id.asc`),
            snaps: () => getAll('audit_smc_snapshots?select=*&order=snapshot_date.asc'),
            batches: () => getAll('audit_claim_batches?select=*&order=claim_month.asc,id.asc'),
            lines: () => getAll('audit_claim_lines?select=batch_id,amount,flag&order=id.asc'),
            balances: () => getAll('audit_manual_balances?select=*&order=as_of.asc'),
            purchases: () => getAll(`purchases?select=id,quantity,unit_price,service_date,claim_id,is_refunded&service_date=gte.${from}&order=id.asc`),
            breakdown: () => getAll('monthly_revenue_claimed_breakdown?select=period_month,bucket_name,claimed_amount&order=period_month.asc'),
            claims: () => getAll('financial_claims?select=id,claim_number,generated_at,status,total_value,net_total&order=id.asc'),
            pending: () => getAll('pending_decree_orders?select=id,decree_number,status,created_at,completed_at,dispensing_date,items:pending_decree_items(total_value,dispensing_date)&order=id.asc'),
        };
        const names = Object.keys(jobs); let done = 0;
        const res = await Promise.allSettled(names.map(n => jobs[n]().then(v => { onProgress && onProgress(++done, names.length, n); return v; })));
        const R = {}; res.forEach((r, i) => { if (r.status === 'fulfilled') R[names[i]] = r.value; else { R[names[i]] = []; D.errors[names[i]] = r.reason; } });

        D.periods = R.periods; D.breakdown = R.breakdown; D.snaps = R.snaps; D.balances = R.balances; D.purchases = R.purchases; D.claims = R.claims;
        D.claimGen = {}; (R.claims || []).forEach(c => { D.claimGen[c.id] = c.generated_at; });
        D.settings = withCutoff(settings, R.claims); D.rawSettings = raw;
        D.revenueByDay = aggregateRevenue(R.entries); D.entryCount = R.entries.length;
        D.legacyMonths = addLegacy(D.revenueByDay, R.periods);
        D.batches = joinBatchLines(R.batches, R.lines);
        D.pendingItems = []; R.pending.forEach(o => (o.items || []).forEach(it => D.pendingItems.push({ total_value: it.total_value, dispensing_date: it.dispensing_date, order_dispensing_date: o.dispensing_date, created_at: o.created_at, order_status: o.status, completed_at: o.completed_at, decree: o.decree_number })));
        D.lastRevenueDate = Object.keys(D.revenueByDay).filter(d => Object.keys(D.revenueByDay[d]).some(k => k !== 'LEGACY')).sort().pop() || '';
        return D;
    }
    async function saveSettings(patch) {
        const S0 = window.ClaimsAudit.state.D;
        const value = { ...(S0.rawSettings || {}), ...patch };
        await write('post', 'audit_settings?on_conflict=key', [{ key: 'main', value, updated_at: new Date().toISOString() }], 'resolution=merge-duplicates,return=minimal');
    }
    async function saveSnapshot(s) {
        const row = { snapshot_date: s.snapshot_date, date_from: s.date_from || null, date_to: s.date_to || null, hospital_id: s.hospital_id || '', stages: s.stages, paid_count: s.paid_count, paid_value: s.paid_value, unpaid_count: s.unpaid_count, unpaid_value: s.unpaid_value, total_invoices: s.total_invoices, source: s.source, note: s.note || null };
        await write('post', 'audit_smc_snapshots?on_conflict=snapshot_date,hospital_id', [row], 'resolution=merge-duplicates,return=minimal');
    }
    /** one batch + its patient lines; replace=true wipes the old lines of the same (payer,label) first */
    async function saveBatch(b, lines, { replace = false } = {}) {
        const row = { payer: b.payer, label: b.label, claim_month: b.claim_month, status: b.status || 'delivered', delivered_on: b.delivered_on || null, service_from: b.service_from || null, service_to: b.service_to || null, declared_total: b.declared_total === '' || b.declared_total == null ? null : num(b.declared_total), source: b.source || 'cover_letter', note: b.note || null };
        const exist = (await axios.get(`${SUPABASE_URL}/rest/v1/audit_claim_batches?select=id&payer=eq.${row.payer}&label=eq.${encodeURIComponent(row.label)}`, { headers: hdrs() })).data[0];
        if (exist && !replace) throw new Error(`الدفعة «${row.label}» موجودة بالفعل — فعّل «استبدال»`);
        let id;
        if (exist) { id = exist.id; await write('patch', `audit_claim_batches?id=eq.${id}`, row, 'return=minimal'); await write('delete', `audit_claim_lines?batch_id=eq.${id}`, null); }
        else id = (await write('post', 'audit_claim_batches', [row], 'return=representation')).data[0].id;
        for (let i = 0; i < (lines || []).length; i += 500) await write('post', 'audit_claim_lines', lines.slice(i, i + 500).map(l => ({ batch_id: id, national_id: l.national_id || null, patient_name: l.patient_name || null, invoice_no: l.invoice_no || null, approval_no: l.approval_no || null, amount: l.amount, flag: l.flag || null, note: l.note || null })), 'return=minimal');
        return id;
    }

    /* ═════════════════════════════════════════════════════════════════════════
     *  UI
     * ═════════════════════════════════════════════════════════════════════════ */
    const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const fmt = n => n == null || Number.isNaN(n) ? '—' : Number(n).toLocaleString('en-US', { maximumFractionDigits: 0 });
    const fmtS = n => n == null ? '—' : (n > 0 ? '+' : '') + fmt(n);
    const dmy = s => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || '')); return m ? `${m[3]}/${m[2]}/${m[1]}` : (s || ''); };
    const monthAr = k => { const [y, m] = String(k).split('-'); return `${['', 'يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو', 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر'][Number(m)] || m} ${y}`; };
    const toast = (m, t) => (window.showToast ? window.showToast(m, t || 'info') : console.log(m));
    const tone = (gap, base) => { if (gap == null) return 'mute'; const a = Math.abs(gap), tol = Math.max(5000, 0.02 * Math.abs(base || 0)); return a <= tol ? 'ok' : a <= 3 * tol ? 'warn' : 'bad'; };
    const state = { tab: 'recon', D: null, asOf: '', busy: false, loadMsg: '', letters: { payer: 'HIO', uhiaPreview: null, form: false }, smc: { draft: null }, linesModal: null };

    const CSS = `
    .cau-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
    .cau-fresh{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin-bottom:14px}
    .cau-fc{border:1px solid #e2e8f0;border-radius:12px;padding:10px 12px;background:#fff;font-size:12px}
    .cau-fc .t{font-weight:800;font-size:13px;margin-bottom:4px;display:flex;justify-content:space-between;gap:6px}
    .cau-fc .h{color:#64748b;margin-top:4px}
    .cau-gap{font-weight:800}.cau-gap.ok{color:#166534}.cau-gap.warn{color:#b45309}.cau-gap.bad{color:#b91c1c}.cau-gap.mute{color:#94a3b8}
    .cau-sub{font-size:11px;color:#94a3b8;font-weight:400;display:block;margin-top:2px}
    .cau-total td{font-weight:800;background:#f8fafc}.cau-sec td{background:#eef2ff;color:#3730a3;font-weight:800;font-size:12px}
    .cau-ind{padding-right:22px!important;color:#334155}
    .cau-grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}
    .cau-pre{direction:ltr;text-align:left;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;background:#0f172a;color:#e2e8f0;border-radius:10px;padding:10px;max-height:120px;overflow:auto;word-break:break-all}
    textarea.hiu-in{min-height:90px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}`;
    function ensureCss() { try { if (window.HIUI && window.HIUI.ensureStyles) window.HIUI.ensureStyles(); } catch (_) { } if (document.getElementById('cauStyles')) return; const s = document.createElement('style'); s.id = 'cauStyles'; s.textContent = CSS; document.head.appendChild(s); }
    const root = () => document.getElementById('claimsAuditSection');
    const B = (text, t) => `<span class="hiu-badge ${t || 'mute'}">${esc(text)}</span>`;
    const notice = (msg, t) => `<div class="hiu-notice ${t || 'info'}">${msg}</div>`;

    function paint() {
        const el = root(); if (!el) return; const D = state.D;
        const tabs = [['recon', 'المطابقة'], ['sf', 'نفقة الدولة (SMC)'], ['letters', 'خطابات المطالبات HIO / UHIA'], ['settings', 'الإعدادات والأرصدة']];
        el.innerHTML = `<div class="hiu-page">
            <div class="hiu-head"><div><div class="hiu-title">مطابقة المطالبات بالإيرادات</div><div class="hiu-sub">Claims ⇄ Revenue audit — الإيراد = المطالب به + غير المطالب به (+ جهات الكاش)</div></div>
              <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="hiu-btn hiu-btn-ghost" onclick="__ca.reload()" ${state.busy ? 'disabled' : ''}>↻ تحديث كل المصادر</button><button class="hiu-btn hiu-btn-ghost" onclick="__ca.exportXlsx()" ${D ? '' : 'disabled'}>⬇ Excel</button></div></div>
            ${state.busy ? `<div class="hiu-card"><div class="hiu-empty"><span class="hiu-spin"></span><div style="margin-top:8px">${esc(state.loadMsg || 'جاري التحميل...')}</div></div></div>` : D ? `
              ${migrationNotice(D)}${freshness(D)}
              <div class="cau-tabs">${tabs.map(([k, t]) => `<button class="hiu-chip ${state.tab === k ? 'active' : ''}" onclick="__ca.tab('${k}')">${t}</button>`).join('')}</div>
              ${state.tab === 'recon' ? viewRecon(D) : state.tab === 'sf' ? viewSF(D) : state.tab === 'letters' ? viewLetters(D) : viewSettings(D)}` : ''}
          </div>${state.linesModal ? linesModalHtml() : ''}`;
    }
    function migrationNotice(D) {
        const missing = ['audit_settings', 'audit_smc_snapshots', 'audit_claim_batches', 'audit_claim_lines', 'audit_manual_balances'].filter(t => D.errors[t] && isMissingTable(D.errors[t]));
        const other = Object.keys(D.errors).filter(k => !missing.includes(k));
        return (missing.length ? notice(`الجداول التالية غير موجودة: <b dir="ltr">${missing.join(', ')}</b> — شغّل ملف <b dir="ltr">claims-audit-migration.sql</b> مرة واحدة في Supabase.`, 'bad') : '')
            + (other.length ? notice('تعذّر تحميل: ' + other.map(k => `<b dir="ltr">${esc(k)}</b> (${esc((D.errors[k].response && D.errors[k].response.status) || D.errors[k].message || '')})`).join('، ') + ' — النتائج التي تعتمد عليها ناقصة.', 'warn') : '');
    }

    /* ───────── data freshness: what updates itself, what needs a human ───────── */
    function freshness(D) {
        const today = D.today, age = d => d ? Math.round((new Date(today) - new Date(d)) / 86400000) : null;
        const lastSnap = (D.snaps || []).map(s => s.snapshot_date).sort().pop(), lastClaim = (D.claims || []).map(c => dayOf(c.generated_at)).sort().pop();
        const lm = p => (D.batches || []).filter(b => b.payer === p).map(b => ym(b.claim_month)).sort().pop();
        const prevM = addM(ym(today), -1), pend = pendingSF(D.pendingItems, today);
        const card = (t, tn, big, hint, auto) => `<div class="cau-fc"><div class="t"><span>${t}</span>${B(tn[0], tn[1])}</div><div>${big}</div><div class="h">${auto ? '⚙ تلقائي — ' : '✋ يدوي — '}${hint}</div></div>`;
        const sAge = age(lastSnap), rAge = age(D.lastRevenueDate);
        return `<div class="cau-fresh">
          ${card('الإيرادات اليومية', rAge == null ? ['لا بيانات', 'bad'] : rAge <= 3 ? ['محدّث', 'ok'] : ['متأخر ' + rAge + ' يوم', 'warn'], `آخر قيد: ${dmy(D.lastRevenueDate) || '—'} · ${fmt(D.entryCount)} قيد`, 'من وحدة الإيرادات', true)}
          ${card('لقطة SMC (نفقة الدولة)', sAge == null ? ['لا توجد', 'bad'] : sAge <= 3 ? ['حديثة', 'ok'] : sAge <= 35 ? ['قديمة ' + sAge + ' يوم — تحقق من الـ Action', 'warn'] : ['قديمة ' + sAge + ' يوم', 'warn'], lastSnap ? `آخر لقطة: ${dmy(lastSnap)}` : '—', 'تُؤخذ تلقائياً كل يوم (GitHub Action: smc-dashboard-snapshot) — ويمكن رفع لقطة يدوياً من تبويب نفقة الدولة', sAge != null && sAge <= 3)}
          ${card('مطالبات HIO بالتطبيق', lastClaim ? ['نشطة', 'ok'] : ['لا توجد', 'warn'], lastClaim ? `آخر مطالبة: ${dmy(lastClaim)} · ${fmt((D.claims || []).length)} مطالبة` : '—', 'تتحدث من وحدة الصرف/المطالبات', true)}
          ${card('خطابات HIO (الأشهر السابقة)', !lm('HIO') ? ['لا توجد', 'warn'] : lm('HIO') >= prevM ? ['محدّثة', 'ok'] : ['ينقص ' + monthAr(addM(lm('HIO'), 1)), 'warn'], lm('HIO') ? `آخر شهر: ${monthAr(lm('HIO'))}` : '—', 'تُدخل يدوياً حتى بداية المطالبات بالتطبيق', false)}
          ${card('خطابات UHIA الشهرية', !lm('UHIA') ? ['لا توجد', 'bad'] : lm('UHIA') >= prevM ? ['محدّثة', 'ok'] : ['ينقص ' + monthAr(addM(lm('UHIA'), 1)), 'warn'], lm('UHIA') ? `آخر شهر: ${monthAr(lm('UHIA'))}` : '—', 'استورد ملف Excel شهرياً (تبويب الخطابات)', false)}
          ${card('قرارات معلقة (Enhanced)', ['مفتوحة', pend.count ? 'info' : 'mute'], `${fmt(pend.count)} بند · ${fmt(pend.value)} ج.م`, 'تُحسب من وحدة متابعة القرارات المغلقة', true)}</div>`;
    }

    /* ───────── TAB 1 — reconciliation ───────── */
    function viewRecon(D) {
        const S = D.settings, asOf = state.asOf || D.today, E = evalAt(D, asOf);
        const tr = (label, val, sub, cls) => `<tr class="${cls || ''}"><td class="${cls ? '' : 'cau-ind'}">${label}${sub ? `<span class="cau-sub">${sub}</span>` : ''}</td><td class="hiu-num" style="text-align:left">${val == null ? '<span class="cau-gap mute">لا يوجد مصدر</span>' : fmt(val)}</td></tr>`;
        const sm = E.smc, P = E.parts;
        const explain = E.gap > 0 ? `إيراد مسجّل بقيمة <b>${fmt(E.gap)}</b> لا تقابله مطالبة ولا رصيد مفتوح: خدمات لم تُفوتر/تُسجَّل في مصدرها، أو مطالبات غير مدخلة في الوحدة.`
            : E.gap < 0 ? `المطالبات والأرصدة المفتوحة تزيد عن الإيراد بقيمة <b>${fmt(-E.gap)}</b>: مطالبات تخص خدمات قبل بداية الفترة، أو مكررة، أو إيراد غير مسجّل.` : 'لا فرق.';
        const closed = S.closed_through, acc = S.accepted;
        const startOpen = closed ? addM(closed, 1) : addM(ym(D.today), -5), endOpen = ym(D.today);
        const rows = monthlyRows(D, startOpen, endOpen);
        return `
        <div class="hiu-card"><div class="hiu-h">١) الماضي المغلق — مطابقة تراكمية (إجمالي)</div>
          <div class="hiu-toolbar" style="margin-bottom:12px"><div class="hiu-field"><label class="hiu-lbl">حتى تاريخ</label><input type="date" class="hiu-in" value="${esc(asOf)}" max="${D.today}" onchange="__ca.setAsOf(this.value)"></div>
            <div class="hiu-field" style="min-width:auto"><label class="hiu-lbl">&nbsp;</label><button class="hiu-btn hiu-btn-primary" onclick="__ca.acceptClose()">✔ اعتماد هذا الإغلاق</button></div></div>
          ${acc ? notice(`<b>الإغلاق معتمد حتى ${monthAr(S.closed_through)}</b> — فرق مقبول ${fmtS(acc.gap)} ج.م (${dmy(acc.at)}). ${esc(acc.note || '')}`, 'ok') : notice('لم يُعتمد إغلاق للماضي بعد. راجع الجدول، فسّر الفرق، ثم اعتمده — بعدها تُطابَق الأشهر الجديدة شهراً بشهر.', 'info')}
          ${E.incomplete.map(m => notice('⚠ ' + esc(m), 'warn')).join('')}
          <div class="hiu-kpis"><div class="hiu-kpi"><div class="l">إجمالي الإيراد</div><div class="v">${fmt(E.revenue.total)}</div><div class="s">${dmy(S.window_start)} ← ${dmy(asOf)}</div></div>
            <div class="hiu-kpi"><div class="l">إجمالي المفسَّر بالمصادر</div><div class="v">${fmt(E.evidence)}</div><div class="s">مطالب + مفتوح + جهات أخرى</div></div>
            <div class="hiu-kpi"><div class="l">الفجوة</div><div class="v cau-gap ${E.status}">${fmtS(E.gap)}</div><div class="s">${(E.gapPct * 100).toFixed(2)}% من الإيراد</div></div>
            <div class="hiu-kpi"><div class="l">تغطية الجهات المُطالَب بها</div><div class="v">${E.revenue.total ? ((E.revenue.SF + E.revenue.HIO + E.revenue.UHIA) / (E.revenue.total - E.revenue.LEGACY || 1) * 100).toFixed(1) : '—'}%</div><div class="s">نفقة + HIO + UHIA من الأشهر الموزّعة</div></div></div>
          <div class="hiu-twrap" style="max-height:none"><table class="hiu-table"><tbody>
            <tr class="cau-sec"><td colspan="2">الإيراد (النظام)</td></tr>
            ${tr('نفقة الدولة', E.revenue.SF)}${tr('التأمين الصحي HIO', E.revenue.HIO)}${tr('التأمين الشامل UHIA', E.revenue.UHIA)}${tr('جهات أخرى (كاش/متعاقدين)', E.revenue.OTHER)}
            ${E.revenue.LEGACY ? tr('إجماليات الأشهر القديمة (دفتر الإغلاق — بدون توزيع)', E.revenue.LEGACY, D.legacyMonths.length + ' شهر بلا قيود يومية') : ''}
            ${tr('إجمالي الإيراد', E.revenue.total, '', 'cau-total')}
            <tr class="cau-sec"><td colspan="2">المطالب به — دليل مستقل</td></tr>
            ${tr('نفقة الدولة — قيد المراجعة', sm && sm.review, sm ? 'لقطة SMC ' + dmy(E.snap.snapshot_date) : 'لا توجد لقطة')}${tr('نفقة الدولة — بالحسابات (تم الصرف، غير مسدد)', sm && sm.accounts)}${tr('نفقة الدولة — لم تُرسل بعد', sm && sm.unsent)}${tr('نفقة الدولة — مرتجعة للمستشفى', sm && sm.returned, 'تحتاج تصحيح وإعادة إرسال')}${tr('نفقة الدولة — مسدّدة', sm && sm.paid)}
            ${tr('التأمين الصحي — خطابات (قبل التطبيق)', P.hioLetters, 'قبل ' + (S.hi_app_cutoff ? monthAr(S.hi_app_cutoff) : '—'))}${tr('التأمين الصحي — مطالبات يدوية بانتظار التسليم', P.hioPending)}${tr('التأمين الصحي — مطالبات التطبيق (إجمالي قبل الخصم)', P.hioApp)}
            ${tr('التأمين الشامل — خطابات', P.uhiaLetters)}${P.hioLedger || P.uhiaLedger ? tr('HIO + UHIA — أشهر بلا خطابات (من دفتر الإغلاق)', P.hioLedger + P.uhiaLedger, 'HIO ' + fmt(P.hioLedger) + ' · UHIA ' + fmt(P.uhiaLedger) + ' — من monthly_revenue_claimed_breakdown') : ''}${P.uhiaPending ? tr('التأمين الشامل — بانتظار التسليم', P.uhiaPending) : ''}
            ${tr('إجمالي المطالب به', (E.claimed.SF || 0) + E.claimed.HIO + E.claimed.UHIA, '', 'cau-total')}
            <tr class="cau-sec"><td colspan="2">غير المطالب به بعد</td></tr>
            ${tr('نفقة الدولة — خدمات بقرارات معلقة', P.pendingSF.value, P.pendingSF.count + ' بند (وحدة المتابعة)')}${tr('التأمين الصحي — صرف بلا مطالبة (التطبيق)', E.open.HIO - P.manual.HIO)}${tr('أرصدة يدوية', P.manual.SF + P.manual.HIO + P.manual.UHIA, 'من تبويب الإعدادات')}
            <tr class="cau-sec"><td colspan="2">تسويات</td></tr>
            ${tr('جهات أخرى متوقعة (نسبة ' + (E.otherShare * 100).toFixed(1) + '% من الإيراد)', E.expectedOther, 'تسوّى فوراً ولا تدخل دورة المطالبات')}${E.openingAdj ? tr('خصم: مطالبات عن خدمات قبل الفترة', -E.openingAdj, 'رصيد افتتاحي من الإعدادات') : ''}
            ${tr('إجمالي المفسَّر', E.evidence, '', 'cau-total')}
            <tr class="cau-total"><td>الفجوة (إيراد − مفسَّر) ${B(E.status === 'ok' ? 'متطابق' : E.status === 'warn' ? 'يحتاج مراجعة' : 'فجوة كبيرة', E.status)}<span class="cau-sub">${explain}</span></td><td class="hiu-num cau-gap ${E.status}" style="text-align:left">${fmtS(E.gap)}</td></tr>
          </tbody></table></div>${sm && sm.warnings.length ? sm.warnings.map(w => notice('SMC: ' + esc(w), 'warn')).join('') : ''}
        </div>
        <div class="hiu-card"><div class="hiu-h">٢) الأشهر المفتوحة — شهر بشهر ${closed ? '' : B('اعتمد الإغلاق أولاً — معروض آخر 6 أشهر', 'warn')}</div>
          <div class="hiu-sub" style="margin-bottom:10px">الفجوة = إيراد الشهر − المطالب خلال الشهر − (تغيّر الرصيد المفتوح). المطالبات تتأخر عن الخدمات، لذلك راقب «التراكمي» أكثر من الشهر منفرداً.</div>
          ${monthlyTable(D, rows)}</div>
        ${ledgerCheck(D, rows)}`;
    }
    function monthlyTable(D, rows) {
        if (!rows.length) return '<div class="hiu-empty">لا أشهر مفتوحة</div>';
        const cum = { SF: 0, HIO: 0, UHIA: 0 }, curM = ym(D.today);
        const body = rows.map(r => {
            let tot = 0, any = false;
            const cells = GROUPS.map(g => { const x = r.groups[g]; if (x.gap != null) { cum[g] += x.gap; tot += x.gap; any = true; }
                return `<td class="hiu-num"><span class="cau-gap ${tone(x.gap, x.revenue)}">${x.gap == null ? '—' : fmtS(x.gap)}</span><span class="cau-sub">إيراد ${fmt(x.revenue)} · مطالب ${x.claimed == null ? '—' : fmt(x.claimed)} · Δمفتوح ${fmtS(x.dOpen)}${x.note ? '<br>' + esc(x.note) : ''}</span></td>`; }).join('');
            const cumTot = cum.SF + cum.HIO + cum.UHIA;
            return `<tr><td class="hiu-num"><b>${monthAr(r.month)}</b>${r.month === curM ? '<span class="cau-sub">جارٍ — غير مكتمل</span>' : ''}${r.legacy ? '<span class="cau-sub">إجمالي قديم بلا توزيع</span>' : ''}</td>${cells}<td class="hiu-num"><span class="cau-gap ${any ? tone(tot, 1e6) : 'mute'}">${any ? fmtS(tot) : '—'}</span></td><td class="hiu-num"><span class="cau-gap ${tone(cumTot, 1e6)}">${fmtS(cumTot)}</span><span class="cau-sub">SF ${fmtS(cum.SF)} · HIO ${fmtS(cum.HIO)} · UHIA ${fmtS(cum.UHIA)}</span></td></tr>`;
        }).join('');
        return `<div class="hiu-twrap"><table class="hiu-table"><thead><tr><th>الشهر</th><th>نفقة الدولة</th><th>التأمين الصحي</th><th>التأمين الشامل</th><th>فجوة الشهر</th><th>تراكمي</th></tr></thead><tbody>${body}</tbody></table></div>`;
    }
    /** closing-ledger claimed (billed_total − audit recovered) against what the independent sources say was claimed that month */
    function ledgerCheck(D, rows) {
        const per = new Map((D.periods || []).map(p => [ym(p.period_month), p]));
        const items = rows.filter(r => per.has(r.month) && GROUPS.every(g => r.groups[g].claimed != null)).map(r => {
            const p = per.get(r.month), ledger = num(p.billed_total) - num(p.audit_recovered_amount), ev = GROUPS.reduce((s, g) => s + r.groups[g].claimed, 0);
            return `<tr><td>${monthAr(r.month)}</td><td>${B(p.status === 'closed' ? 'مغلق' : 'مفتوح', p.status === 'closed' ? 'ok' : 'warn')}</td><td class="hiu-num">${fmt(ledger)}</td><td class="hiu-num">${fmt(ev)}</td><td class="hiu-num cau-gap ${tone(ledger - ev, ledger)}">${fmtS(ledger - ev)}</td></tr>`;
        }).join('');
        return items ? `<div class="hiu-card"><div class="hiu-h">٣) مطابقة «المطالب» في دفتر الإغلاق مع الدليل المستقل</div><div class="hiu-sub" style="margin-bottom:8px">يشمل المطالب في الدفتر الكاش/المتعاقدين أيضاً، لذلك الفرق الصغير الموجب طبيعي. فرق كبير = قيمة مدخلة في الدفتر لا يسندها خطاب أو لقطة.</div><div class="hiu-twrap"><table class="hiu-table"><thead><tr><th>الشهر</th><th>الفترة</th><th>مطالب الدفتر (بعد الاسترداد)</th><th>مطالب الدليل (3 جهات)</th><th>الفرق</th></tr></thead><tbody>${items}</tbody></table></div></div>` : '';
    }

    /* ───────── TAB 2 — State fund ───────── */
    function viewSF(D) {
        const snaps = [...(D.snaps || [])].sort((a, b) => a.snapshot_date.localeCompare(b.snapshot_date));
        const bookmarklet = 'javascript:(' + encodeURIComponent('' + smcCollectorSource) + ')()';
        let prev = null;
        const rows = snaps.map(s => { const t = smcTotals(s), d = prev ? t.total - prev.total : null; prev = t;
            return `<tr><td class="hiu-num">${dmy(s.snapshot_date)}</td><td class="hiu-num">${fmt(t.review)}</td><td class="hiu-num">${fmt(t.accounts)}</td><td class="hiu-num">${fmt(t.unsent)}</td><td class="hiu-num">${fmt(t.returned)}</td><td class="hiu-num">${fmt(t.paid)}</td><td class="hiu-num"><b>${fmt(t.total)}</b></td><td class="hiu-num cau-gap ${d == null ? 'mute' : 'ok'}">${d == null ? '—' : fmtS(d)}</td><td>${B(s.source || '', 'mute')} ${t.warnings.length ? B('⚠ ' + t.warnings.length, 'warn') : ''}</td><td><button class="hiu-btn hiu-btn-danger hiu-btn-sm" onclick="__ca.delSnap('${esc(s.id)}')">حذف</button></td></tr>`; }).reverse().join('');
        const pend = (D.pendingItems || []).filter(i => i.order_status !== 'completed'), byM = {};
        pend.forEach(i => { const k = ym(i.dispensing_date || i.order_dispensing_date || i.created_at); const o = byM[k] || (byM[k] = { n: 0, v: 0 }); o.n++; o.v += num(i.total_value); });
        const dr = state.smc.draft;
        return `
        <div class="hiu-card"><div class="hiu-h">لقطة SMC جديدة</div>
          ${notice('موقع SMC بتسجيل دخول منفصل فلا يستطيع النظام قراءته مباشرة. اختر طريقة: <b>(أ)</b> زر الـ Bookmarklet يجمع كل المراحل بضغطة واحدة من صفحة Dashboard، أو <b>(ب)</b> ارفع ملف HAR/HTML محفوظ من الجلسة، أو <b>(ج)</b> الصق JSON.', 'info')}
          <div class="cau-grid2">
            <div><label class="hiu-lbl">(أ) Bookmarklet — اسحبه لشريط الإشارات</label><a class="hiu-btn hiu-btn-ghost" href="${bookmarklet}" onclick="event.preventDefault();__ca.bmHint()">⬇ SMC → المطابقة</a> <button class="hiu-btn hiu-btn-ghost hiu-btn-sm" onclick="__ca.copyBookmarklet()">نسخ الكود</button>
              <div class="hiu-sub">افتح Decrees/Dashboard، اضغط بحث (من 01/06/2025)، ثم اضغط الإشارة؛ ثم الصق النتيجة هنا.</div></div>
            <div><label class="hiu-lbl">(ب) ملف HAR / HTML / JSON</label><input type="file" class="hiu-in" accept=".har,.json,.html,.htm" onchange="__ca.smcFile(this.files[0])"></div></div>
          <label class="hiu-lbl" style="margin-top:10px">(ج) الصق JSON الناتج</label><textarea class="hiu-in" id="cauSmcPaste" placeholder='{"v":1,"source":"bookmarklet",...}'></textarea>
          <button class="hiu-btn hiu-btn-primary" style="margin-top:8px" onclick="__ca.smcPaste()">قراءة</button>
          ${dr ? smcDraftForm(dr) : ''}</div>
        <div class="hiu-card"><div class="hiu-h">لقطات SMC المحفوظة</div>${snaps.length ? `<div class="hiu-twrap"><table class="hiu-table"><thead><tr><th>التاريخ</th><th>قيد المراجعة</th><th>بالحسابات</th><th>لم تُرسل</th><th>مرتجعة</th><th>مسدّدة</th><th>الإجمالي</th><th>الزيادة عن السابقة</th><th></th><th></th></tr></thead><tbody>${rows}</tbody></table></div><div class="hiu-sub" style="margin-top:6px">الزيادة بين لقطتين = ما أُرسل إلى SMC في الفترة — وهو «المطالب خلال الشهر» في المطابقة الشهرية. خذ لقطة في آخر كل شهر.</div>` : '<div class="hiu-empty">لا توجد لقطات بعد</div>'}</div>
        <div class="hiu-card"><div class="hiu-h">خدمات بقرارات معلقة (من وحدة متابعة القرارات المغلقة)</div>${pend.length ? `<div class="hiu-twrap"><table class="hiu-table"><thead><tr><th>شهر الصرف</th><th>عدد البنود</th><th>القيمة</th></tr></thead><tbody>${Object.keys(byM).sort().map(k => `<tr><td>${esc(k)}</td><td class="hiu-num">${fmt(byM[k].n)}</td><td class="hiu-num">${fmt(byM[k].v)}</td></tr>`).join('')}</tbody><tfoot><tr><td>الإجمالي</td><td class="hiu-num">${fmt(pend.length)}</td><td class="hiu-num">${fmt(pend.reduce((s, i) => s + num(i.total_value), 0))}</td></tr></tfoot></table></div><div class="hiu-sub" style="margin-top:6px">تسجيل التواريخ في الوحدة بدأ من 22/10/2026؛ ما قبله لا يظهر هنا (استخدم الرصيد اليدوي/الافتتاحي).</div>` : '<div class="hiu-empty">لا توجد بنود معلقة</div>'}</div>`;
    }
    function smcDraftForm(s) {
        const t = smcTotals(s), f = (id, label, v, type) => `<div class="hiu-field"><label class="hiu-lbl">${label}</label><input class="hiu-in" id="cauD_${id}" type="${type || 'text'}" value="${esc(v ?? '')}"></div>`;
        const stages = Object.entries(SMC_STAGES).map(([k, c]) => f('st_' + k, `${c.ar} (${k}) — القيمة`, s.stages[k] ? s.stages[k].value : '', 'number')).join('');
        return `<div style="margin-top:14px;border-top:1px dashed #cbd5e1;padding-top:12px"><div class="hiu-h">مراجعة اللقطة قبل الحفظ</div>
          ${(t.warnings || []).map(w => notice('⚠ ' + esc(w), 'warn')).join('')}
          <div class="hiu-toolbar">${f('date', 'تاريخ اللقطة', s.snapshot_date, 'date')}${f('from', 'من (فاتورة)', s.date_from, 'date')}${f('to', 'إلى', s.date_to, 'date')}${f('hosp', 'كود المستشفى', s.hospital_id)}${f('paid', 'قيمة المسدّد', s.paid_value, 'number')}${f('total', 'إجمالي عدد الفواتير', s.total_invoices, 'number')}</div>
          <div class="hiu-toolbar" style="margin-top:8px">${stages}</div>
          <div class="hiu-kpis" style="margin-top:10px"><div class="hiu-kpi"><div class="l">إجمالي نفقة الدولة في اللقطة</div><div class="v">${fmt(t.total)}</div></div></div>
          <button class="hiu-btn hiu-btn-ok" onclick="__ca.smcSave()">حفظ اللقطة</button> <button class="hiu-btn hiu-btn-ghost" onclick="__ca.smcCancel()">إلغاء</button></div>`;
    }

    /* ───────── TAB 3 — cover letters ───────── */
    function viewLetters(D) {
        const L = state.letters, payer = L.payer, bs = (D.batches || []).filter(b => b.payer === payer), cutoff = D.settings.hi_app_cutoff;
        const issues = batchChecks(bs, []);
        const first = ym(D.settings.window_start), last = addM(ym(D.today), -1), months = monthsBetween(first, last);
        const byM = {}; bs.forEach(b => { (byM[ym(b.claim_month)] = byM[ym(b.claim_month)] || []).push(b); });
        const appByM = {}; (D.purchases || []).forEach(p => { if (p.is_refunded === true || p.claim_id == null) return; const cg = D.claimGen[p.claim_id]; if (cg) appByM[ym(cg)] = (appByM[ym(cg)] || 0) + grossOf(p); });
        const cov = months.map(m => { const list = byM[m] || [], lc = letterClaimed(D, payer, m), tot = list.reduce((s, b) => s + batchTotal(b), 0), app = payer === 'HIO' ? appByM[m] : null, appTerritory = payer === 'HIO' && cutoff && m >= cutoff;
            const c1 = list.length ? fmt(tot) : lc.src === 'ledger' ? `${fmt(lc.total)}<span class="cau-sub">من دفتر الإغلاق (لا خطاب)</span>` : appTerritory ? '<span class="cau-gap mute">مطالبات التطبيق</span>' : '<span class="cau-gap bad">⚠ لا يوجد خطاب</span>';
            return `<tr><td>${monthAr(m)}</td><td class="hiu-num">${c1}</td><td class="hiu-num">${list.reduce((s, b) => s + b.lines_count, 0) || '—'}</td>${payer === 'HIO' ? `<td class="hiu-num">${app ? fmt(app) : '—'}</td><td class="hiu-num">${app && list.length ? `<span class="cau-gap ${tone(tot - app, app)}">${fmtS(tot - app)}</span>` : ''}</td>` : ''}</tr>`; }).reverse().join('');
        return `
        <div class="hiu-card"><div class="cau-tabs" style="margin:0"><button class="hiu-chip ${payer === 'HIO' ? 'active' : ''}" onclick="__ca.payer('HIO')">التأمين الصحي HIO</button><button class="hiu-chip ${payer === 'UHIA' ? 'active' : ''}" onclick="__ca.payer('UHIA')">التأمين الشامل UHIA</button>
          <button class="hiu-btn hiu-btn-primary hiu-btn-sm" style="margin-right:auto" onclick="__ca.toggleForm()">＋ دفعة / خطاب جديد</button></div></div>
        ${L.form ? batchForm(payer) : ''}${payer === 'UHIA' ? uhiaImport(D) : hioImport(D)}
        ${issues.length ? `<div class="hiu-card"><div class="hiu-h">ملاحظات جودة البيانات</div>${issues.map(i => notice(`<b>${esc(i.batch.label)}</b> — ${esc(i.msg)}`, i.tone)).join('')}</div>` : ''}
        <div class="hiu-card"><div class="hiu-h">تغطية الأشهر — ${payer === 'HIO' ? 'HIO' : 'UHIA'}</div>${payer === 'HIO' ? `<div class="hiu-sub" style="margin-bottom:6px">الخطابات تُحتسب كدليل للأشهر قبل ${cutoff ? monthAr(cutoff) : 'أول مطالبة بالتطبيق'}؛ بعدها يُحتسب التطبيق ويُستخدم الخطاب للمقارنة فقط (عمود الفرق).</div>` : ''}
          ${months.length ? `<div class="hiu-twrap"><table class="hiu-table"><thead><tr><th>شهر المطالبة</th><th>إجمالي الخطابات</th><th>عدد المرضى</th>${payer === 'HIO' ? '<th>مطالبات التطبيق</th><th>خطاب − تطبيق</th>' : ''}</tr></thead><tbody>${cov}</tbody></table></div>` : '<div class="hiu-empty">لا توجد خطابات بعد</div>'}</div>
        <div class="hiu-card"><div class="hiu-h">الدفعات المسجّلة (${bs.length})</div>${bs.length ? `<div class="hiu-twrap"><table class="hiu-table"><thead><tr><th>الدفعة</th><th>الشهر</th><th>الحالة</th><th>المصدر</th><th>إجمالي الخطاب</th><th>مجموع المرضى</th><th>المحتسب</th><th></th></tr></thead><tbody>${[...bs].reverse().map(b => `<tr><td>${esc(b.label)}${b.note ? `<span class="cau-sub">${esc(b.note)}</span>` : ''}</td><td>${monthAr(ym(b.claim_month))}</td><td>${B(b.status === 'delivered' ? 'سُلّم' : b.status === 'pending_delivery' ? 'بانتظار التسليم' : 'مسودة', b.status === 'delivered' ? 'ok' : b.status === 'draft' ? 'mute' : 'warn')}</td><td>${B(b.source, 'mute')}</td><td class="hiu-num">${b.declared_total == null ? '—' : fmt(b.declared_total)}</td><td class="hiu-num">${b.lines_count ? fmt(b.lines_total) + `<span class="cau-sub">${b.lines_count} سطر</span>` : '—'}</td><td class="hiu-num"><b>${fmt(batchTotal(b))}</b>${payer === 'HIO' && !batchCounts(b, cutoff) ? '<span class="cau-sub">للمقارنة فقط (بعد بدء التطبيق)</span>' : ''}</td><td style="white-space:nowrap">${b.lines_count ? `<button class="hiu-btn hiu-btn-ghost hiu-btn-sm" onclick="__ca.viewLines('${esc(b.id)}')">المرضى</button> ` : ''}<button class="hiu-btn hiu-btn-danger hiu-btn-sm" onclick="__ca.delBatch('${esc(b.id)}')">حذف</button></td></tr>`).join('')}</tbody></table></div>` : '<div class="hiu-empty">لا توجد دفعات</div>'}</div>`;
    }
    function batchForm(payer) {
        const f = (id, label, type, extra) => `<div class="hiu-field"><label class="hiu-lbl">${label}</label><input class="hiu-in" id="cauB_${id}" type="${type || 'text'}" ${extra || ''}></div>`;
        return `<div class="hiu-card"><div class="hiu-h">دفعة / خطاب مطالبة جديد — ${payer}</div>
          <div class="hiu-toolbar">${f('month', 'شهر المطالبة', 'month', `value="${addM(ym(localToday()), 0)}"`)}${f('total', 'الإجمالي المطبوع على الخطاب', 'number', 'step="0.01"')}
            <div class="hiu-field"><label class="hiu-lbl">الحالة</label><select class="hiu-in" id="cauB_status"><option value="delivered">سُلّم</option><option value="pending_delivery">بانتظار التسليم</option><option value="draft">مسودة (لا يُحتسب)</option></select></div>
            <div class="hiu-field"><label class="hiu-lbl">المصدر</label><select class="hiu-in" id="cauB_source"><option value="cover_letter">خطاب مطالبة (تاريخي)</option><option value="manual">مطالبة يدوية خارج التطبيق</option></select></div>
            ${f('from', 'خدمات من', 'date')}${f('to', 'خدمات إلى', 'date')}${f('deliv', 'تاريخ التسليم', 'date')}${f('note', 'ملاحظة')}</div>
          <label class="hiu-lbl" style="margin-top:10px">أسماء المرضى (اختياري) — الصق من Excel: رقم قومي ⇥ اسم ⇥ قيمة، سطر لكل مريض</label><textarea class="hiu-in" id="cauB_lines"></textarea>
          <div class="hiu-sub">يمكن ترك المرضى فارغين وإدخال الإجمالي فقط. إن أدخلت الاثنين يقارن النظام مجموع المرضى بإجمالي الخطاب.</div>
          <button class="hiu-btn hiu-btn-ok" style="margin-top:8px" onclick="__ca.saveBatchForm('${payer}')">حفظ</button></div>`;
    }
    function uhiaImport(D) {
        const pv = state.letters.uhiaPreview, existing = new Map((D.batches || []).filter(b => b.payer === 'UHIA').map(b => [ym(b.claim_month), b]));
        let preview = '';
        if (pv && pv.error) preview = notice(esc(pv.error), 'bad');
        else if (pv) {
            const ks = Object.keys(pv.byMonth).sort();
            preview = `${pv.perSheet.map(s => notice(`ورقة <b>${esc(s.name)}</b>: ${s.error ? esc(s.error) : s.rows + ' سطر'}`, s.error ? 'bad' : 'info')).join('')}
              ${pv.dropped.length ? notice(`<b>تم حذف ${fmt(pv.dropped.length)} سطر مكرر</b> (${fmt(pv.droppedTotal)} ج.م) ظهر في أكثر من ورقة بنفس (الرقم القومي، الفاتورة، الشهر، القيمة) — بدون هذا الحذف كان الإجمالي سيتضاعف.`, 'warn') : ''}
              <div class="hiu-twrap"><table class="hiu-table"><thead><tr><th>شهر المطالبة</th><th>عدد الأسطر</th><th>الإجمالي بعد إزالة التكرار</th><th>الموجود حالياً</th><th>مبالغ مقطوعة للمراجعة</th></tr></thead><tbody>${ks.map(k => { const ex = existing.get(ym(k)); return `<tr><td>${monthAr(ym(k))}</td><td class="hiu-num">${pv.byMonth[k].rows}</td><td class="hiu-num"><b>${fmt(pv.byMonth[k].total)}</b></td><td class="hiu-num">${ex ? fmt(batchTotal(ex)) + (Math.abs(batchTotal(ex) - pv.byMonth[k].total) > 1 ? ` <span class="cau-gap warn">(فرق ${fmtS(pv.byMonth[k].total - batchTotal(ex))})</span>` : ' ✓') : '—'}</td><td class="hiu-num">${pv.byMonth[k].lump || ''}</td></tr>`; }).join('')}</tbody><tfoot><tr><td>الإجمالي</td><td class="hiu-num">${fmt(pv.rows.length)}</td><td class="hiu-num">${fmt(pv.total)}</td><td></td><td></td></tr></tfoot></table></div>
              <div class="hiu-toolbar" style="margin-top:10px"><label style="font-size:13px"><input type="checkbox" id="cauU_replace" ${[...existing.keys()].some(k => pv.byMonth[k + '-01']) ? 'checked' : ''}> استبدال الشهور الموجودة</label><button class="hiu-btn hiu-btn-ok" onclick="__ca.uhiaSave()">حفظ ${ks.length} دفعة شهرية</button><button class="hiu-btn hiu-btn-ghost" onclick="__ca.uhiaCancel()">إلغاء</button></div>`;
        }
        return `<div class="hiu-card"><div class="hiu-h">استيراد ملف مطالبات UHIA (Excel)</div><div class="hiu-sub" style="margin-bottom:8px">يقرأ كل الأوراق، يحدد الأعمدة بعناوينها (شهر الصرف، الرقم القومي، رقم الفاتورة، صافي المبلغ المطالب)، يزيل السطور المكررة بين الأوراق، ثم ينشئ دفعة لكل شهر بمرضاها.</div>
          <input type="file" class="hiu-in" accept=".xlsx,.xls" onchange="__ca.uhiaFile(this.files[0])">${preview}</div>`;
    }
    function hioImport(D) {
        const pv = state.letters.hioPreview, cutoff = D.settings.hi_app_cutoff, existing = new Map((D.batches || []).filter(b => b.payer === 'HIO').map(b => [b.label, b]));
        let preview = '';
        if (pv && pv.error) preview = notice(esc(pv.error), 'bad');
        else if (pv) {
            preview = `${pv.skipped && pv.skipped.length ? notice('تعذّر تحديد الشهر للتسميات: ' + esc(pv.skipped.join('، ')) + ' — تم تجاهل أسطرها.', 'warn') : ''}
              ${pv.groups.some(g => g.combined) ? notice('<b>مطالبة مجمّعة</b> (مثل Sep-Oct PDF) لا يمكن فصلها على شهرين، لذلك تُسجَّل على <b>آخر شهر</b> فيها وتُعلَّم كمجمّعة.', 'info') : ''}
              <div class="hiu-twrap"><table class="hiu-table"><thead><tr><th>الدفعة</th><th>شهر المطالبة</th><th>عدد المرضى</th><th>الإجمالي</th><th>الموجود حالياً</th><th>الأثر</th></tr></thead><tbody>${pv.groups.map(g => { const ex = existing.get(g.label), cmp = !!cutoff && ym(g.claim_month) >= cutoff;
                return `<tr><td>${esc(g.label)}${g.combined ? ' ' + B('مجمّعة', 'warn') : ''}<span class="cau-sub">${esc(g.from_label)} — ${esc(g.files.join('، '))}</span></td><td>${monthAr(ym(g.claim_month))}</td><td class="hiu-num">${fmt(g.rows)}</td><td class="hiu-num"><b>${fmt(g.total)}</b></td><td class="hiu-num">${ex ? fmt(batchTotal(ex)) + (Math.abs(batchTotal(ex) - g.total) > 1 ? ` <span class="cau-gap warn">(فرق ${fmtS(g.total - batchTotal(ex))})</span>` : ' ✓') : '—'}</td><td>${cmp ? B('للمقارنة مع التطبيق فقط', 'mute') : B('دليل مطالبات', 'ok')}</td></tr>`; }).join('')}</tbody><tfoot><tr><td>الإجمالي</td><td></td><td class="hiu-num">${fmt(pv.rows.length)}</td><td class="hiu-num">${fmt(pv.total)}</td><td></td><td></td></tr></tfoot></table></div>
              <div class="hiu-toolbar" style="margin-top:10px"><label style="font-size:13px"><input type="checkbox" id="cauH_replace" ${pv.groups.some(g => existing.has(g.label)) ? 'checked' : ''}> استبدال الدفعات الموجودة</label><button class="hiu-btn hiu-btn-ok" onclick="__ca.hioSave()">حفظ ${pv.groups.length} دفعة</button><button class="hiu-btn hiu-btn-ghost" onclick="__ca.hioCancel()">إلغاء</button></div>`;
        }
        return `<div class="hiu-card"><div class="hiu-h">استيراد ملف مطالبات HIO (Excel — «HIO Claims Combined»)</div><div class="hiu-sub" style="margin-bottom:8px">يقرأ ورقة «All Claims» (الشهر، المريض، الرقم القومي، الفاتورة، القيمة)، يستنتج سنة كل شهر من بداية فترة المراجعة، وينشئ دفعة لكل شهر بمرضاها. الأشهر قبل أول خطاب (قبل ${monthAr(ym(D.settings.window_start))} ← أول خطاب) تُؤخذ من دفتر الإغلاق.</div>
          <input type="file" class="hiu-in" accept=".xlsx,.xls" onchange="__ca.hioFile(this.files[0])">${preview}</div>`;
    }
    function linesModalHtml() {
        const m = state.linesModal; if (!m) return '';
        const q = (m.q || '').trim().toLowerCase(), rows = (m.lines || []).filter(l => !q || String(l.patient_name || '').toLowerCase().includes(q) || String(l.national_id || '').includes(q) || String(l.invoice_no || '').includes(q));
        return `<div class="hiu-ov" onclick="if(event.target===this)__ca.closeLines()"><div class="hiu-modal" style="max-width:900px"><div class="hiu-modal-h">${esc(m.label)} — ${fmt(m.lines.length)} مريض · ${fmt(m.lines.reduce((s, l) => s + num(l.amount), 0))} ج.م</div><div class="hiu-modal-b">
          <input class="hiu-in" placeholder="بحث بالاسم أو الرقم القومي أو الفاتورة" value="${esc(m.q || '')}" oninput="__ca.linesQ(this.value)" style="margin-bottom:10px">
          <div class="hiu-twrap" style="max-height:55vh"><table class="hiu-table"><thead><tr><th>الرقم القومي</th><th>المريض</th><th>الفاتورة</th><th>القيمة</th><th>ملاحظة</th></tr></thead><tbody>${rows.slice(0, 600).map(l => `<tr><td class="hiu-num">${esc(l.national_id)}</td><td>${esc(l.patient_name)}</td><td class="hiu-num">${esc(l.invoice_no)}</td><td class="hiu-num">${fmt(l.amount)}</td><td>${l.flag ? B('مراجعة H.I.S.', 'warn') : ''}</td></tr>`).join('')}</tbody></table></div>${rows.length > 600 ? `<div class="hiu-sub">عرض أول 600 من ${rows.length}</div>` : ''}</div>
          <div class="hiu-modal-f"><button class="hiu-btn hiu-btn-ghost" onclick="__ca.closeLines()">إغلاق</button></div></div></div>`;
    }

    /* ───────── TAB 4 — settings & manual balances ───────── */
    function viewSettings(D) {
        const S = D.settings, f = (id, label, v, type, extra) => `<div class="hiu-field"><label class="hiu-lbl">${label}</label><input class="hiu-in" id="cauS_${id}" type="${type || 'text'}" value="${esc(v ?? '')}" ${extra || ''}></div>`;
        const bal = (D.balances || []).map(b => `<tr><td>${GROUP_AR[b.grp] || b.grp}</td><td class="hiu-num">${dmy(b.as_of)}</td><td class="hiu-num">${fmt(b.amount)}</td><td>${esc(b.label || '')}</td><td><button class="hiu-btn hiu-btn-danger hiu-btn-sm" onclick="__ca.delBal('${esc(b.id)}')">حذف</button></td></tr>`).join('');
        return `<div class="hiu-card"><div class="hiu-h">إعدادات المطابقة</div><div class="hiu-toolbar">
            ${f('window_start', 'بداية فترة المراجعة', S.window_start, 'date')}${f('hi_app_cutoff', 'أول شهر مطالبات HIO بالتطبيق (YYYY-MM)', S.hi_app_cutoff, 'month')}${f('tolerance_pct', 'نسبة التسامح %', S.tolerance_pct, 'number', 'step="0.1"')}${f('other_share_pct', 'نسبة الجهات الأخرى % (فارغ = تلقائي)', S.other_share_pct, 'number', 'step="0.1"')}</div>
          <div class="hiu-sub" style="margin:10px 0 4px">رصيد افتتاحي: مطالبات داخل لقطة SMC/الخطابات تخص خدمات <b>قبل</b> بداية الفترة (تُخصم من الدليل). اتركه صفراً إن لم يوجد.</div>
          <div class="hiu-toolbar">${f('op_SF', 'نفقة الدولة', S.opening.SF, 'number')}${f('op_HIO', 'التأمين الصحي', S.opening.HIO, 'number')}${f('op_UHIA', 'التأمين الشامل', S.opening.UHIA, 'number')}</div>
          <div class="hiu-toolbar" style="margin-top:12px"><button class="hiu-btn hiu-btn-primary" onclick="__ca.saveSettingsForm()">حفظ الإعدادات</button>${S.closed_through ? `<button class="hiu-btn hiu-btn-danger" onclick="__ca.reopenClose()">إلغاء اعتماد الإغلاق (${monthAr(S.closed_through)})</button>` : ''}</div></div>
        <div class="hiu-card"><div class="hiu-h">أرصدة يدوية «غير مطالب بها»</div><div class="hiu-sub" style="margin-bottom:8px">لأي جهة لا يوجد لها مصدر في النظام (مثلاً UHIA غير المطالب به، أو قوائم الانتظار). يُستخدم أحدث رصيد حتى تاريخ المطابقة.</div>
          <div class="hiu-toolbar"><div class="hiu-field"><label class="hiu-lbl">الجهة</label><select class="hiu-in" id="cauM_grp"><option value="SF">نفقة الدولة</option><option value="HIO">التأمين الصحي</option><option value="UHIA">التأمين الشامل</option></select></div>${f('M_as', 'بتاريخ', localToday(), 'date').replace('cauS_', 'cauM_')}${f('M_amt', 'القيمة', '', 'number').replace('cauS_', 'cauM_')}${f('M_lbl', 'البيان', '').replace('cauS_', 'cauM_')}<button class="hiu-btn hiu-btn-ok" onclick="__ca.addBal()">إضافة</button></div>
          ${bal ? `<div class="hiu-twrap" style="margin-top:10px"><table class="hiu-table"><thead><tr><th>الجهة</th><th>بتاريخ</th><th>القيمة</th><th>البيان</th><th></th></tr></thead><tbody>${bal}</tbody></table></div>` : ''}</div>`;
    }

    /* ═════════════════════════════════════════════════════════════════════════
     *  ACTIONS (wired to inline handlers as window.__ca.*)
     * ═════════════════════════════════════════════════════════════════════════ */
    const val = id => { const e = document.getElementById(id); return e ? e.value : ''; };
    const guard = fn => async (...a) => { try { return await fn(...a); } catch (e) { console.error('[claims-audit]', e); toast('خطأ: ' + (e && e.response && e.response.data && (e.response.data.message || e.response.data.hint) || e.message || e), 'error'); } };
    const afterWrite = async () => { await refreshAudit(); paint(); };
    const readFile = (f, as) => new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = () => rej(r.error); as === 'buf' ? r.readAsArrayBuffer(f) : r.readAsText(f); });

    async function reload() {
        state.busy = true; state.loadMsg = 'جاري تحميل المصادر...'; paint();
        try { state.D = await loadAll((d, n, name) => { state.loadMsg = `تم تحميل ${d} من ${n} (${name})`; const el = root(); const m = el && el.querySelector('.hiu-empty div'); if (m) m.textContent = state.loadMsg; }); if (!state.asOf) state.asOf = state.D.today; }
        catch (e) { console.error(e); toast('تعذّر تحميل البيانات: ' + (e.message || e), 'error'); }
        state.busy = false; paint();
    }

    const A = {
        tab: k => { state.tab = k; paint(); },
        reload: guard(reload),
        setAsOf: v => { state.asOf = v; paint(); },
        payer: p => { state.letters.payer = p; state.letters.uhiaPreview = null; state.letters.hioPreview = null; paint(); },
        toggleForm: () => { state.letters.form = !state.letters.form; paint(); },

        acceptClose: guard(async () => {
            const D = state.D, asOf = state.asOf || D.today;
            if (asOf !== mEnd(ym(asOf))) return toast('اختر آخر يوم في الشهر لاعتماد الإغلاق (مثلاً ' + dmy(mEnd(addM(ym(D.today), -1))) + ')', 'warning');
            const E = evalAt(D, asOf);
            const note = prompt(`اعتماد إغلاق الماضي حتى ${monthAr(ym(asOf))}\nالفجوة الحالية: ${fmtS(E.gap)} ج.م (${(E.gapPct * 100).toFixed(2)}%)\n\nاكتب تفسير/ملاحظة الفرق المقبول:`, '');
            if (note === null) return;
            await saveSettings({ closed_through: ym(asOf), accepted: { asOf, gap: E.gap, revenue: E.revenue.total, evidence: E.evidence, note, at: localToday() } });
            await afterWrite(); toast('تم اعتماد الإغلاق — الأشهر التالية تُطابَق شهراً بشهر', 'success');
        }),
        reopenClose: guard(async () => { if (!confirm('إلغاء اعتماد الإغلاق؟')) return; await saveSettings({ closed_through: '', accepted: null }); await afterWrite(); }),
        saveSettingsForm: guard(async () => {
            const prevStart = state.D.settings.window_start;
            const patch = { window_start: val('cauS_window_start') || DEFAULTS.window_start, hi_app_cutoff: val('cauS_hi_app_cutoff'), tolerance_pct: num(val('cauS_tolerance_pct') || 2), other_share_pct: val('cauS_other_share_pct'), opening: { SF: num(val('cauS_op_SF')), HIO: num(val('cauS_op_HIO')), UHIA: num(val('cauS_op_UHIA')) } };
            await saveSettings(patch); toast('تم الحفظ', 'success');
            if (patch.window_start !== prevStart) await reload(); else await afterWrite();
        }),
        addBal: guard(async () => {
            const amount = val('cauM_M_amt'); if (amount === '') return toast('أدخل القيمة', 'warning');
            await write('post', 'audit_manual_balances', [{ grp: val('cauM_grp'), as_of: val('cauM_M_as') || localToday(), amount: num(amount), label: val('cauM_M_lbl') || null }], 'return=minimal'); await afterWrite();
        }),
        delBal: guard(async id => { if (!confirm('حذف الرصيد؟')) return; await write('delete', `audit_manual_balances?id=eq.${id}`); await afterWrite(); }),

        /* ── SMC ── */
        bmHint: () => toast('اسحب هذا الزر إلى شريط الإشارات (Bookmarks). إن لم يعمل السحب استخدم «نسخ الكود» وألصقه كعنوان لإشارة جديدة.', 'info'),
        copyBookmarklet: guard(async () => { await navigator.clipboard.writeText('javascript:(' + encodeURIComponent('' + smcCollectorSource) + ')()'); toast('تم نسخ الكود — الصقه في خانة العنوان (URL) لإشارة مرجعية جديدة', 'success'); }),
        smcFile: guard(async f => {
            if (!f) return; const text = await readFile(f), name = f.name.toLowerCase();
            let snap = null, json = null; try { json = JSON.parse(text); } catch (_) { }
            if (json && json.log) snap = parseSmcHar(json);
            else if (json && json.stages) snap = buildSnapshot(json);
            else if (!json && /<html|<table|<div/i.test(text)) { const p = parseSmcDashboardHtml(text); snap = buildSnapshot({ ...p, stages: {}, source: 'html' }); toast('ملف HTML يحتوي الأعداد فقط — أدخل قيم المراحل يدوياً أو استخدم الـ HAR/Bookmarklet', 'warning'); }
            if (!snap) return toast('لم أتعرف على محتوى الملف (' + name + ')', 'error');
            state.smc.draft = { ...snap, _counts: Object.fromEntries(Object.entries(snap.stages).map(([k, v]) => [k, v.count])) }; paint();
        }),
        smcPaste: guard(async () => {
            const t = val('cauSmcPaste').trim(); if (!t) return;
            const j = JSON.parse(t); const snap = buildSnapshot(j); state.smc.draft = snap; paint();
        }),
        smcSave: guard(async () => {
            const d = state.smc.draft; if (!d) return;
            const stages = {};
            for (const k of Object.keys(SMC_STAGES)) { const v = val('cauD_st_' + k); if (v !== '') stages[k] = { count: num(d.stages[k] && d.stages[k].count || (d._counts || {})[k] || 0), value: num(v) }; }
            const snap = buildSnapshot({ snapshot_date: val('cauD_date'), date_from: val('cauD_from'), date_to: val('cauD_to'), hospital_id: val('cauD_hosp'), total_invoices: val('cauD_total') === '' ? null : num(val('cauD_total')), stages, paid: { count: d.paid_count, value: val('cauD_paid') === '' ? null : num(val('cauD_paid')) }, unpaid: { count: d.unpaid_count, value: d.unpaid_value }, source: d.source });
            if (snap.paid_value == null) return toast('قيمة المسدّد مطلوبة', 'warning');
            if (!snap.snapshot_date) return toast('تاريخ اللقطة مطلوب', 'warning');
            await saveSnapshot(snap); state.smc.draft = null; await afterWrite(); toast('تم حفظ لقطة SMC', 'success');
        }),
        smcCancel: () => { state.smc.draft = null; paint(); },
        delSnap: guard(async id => { if (!confirm('حذف هذه اللقطة؟')) return; await write('delete', `audit_smc_snapshots?id=eq.${id}`); await afterWrite(); }),

        /* ── cover letters ── */
        saveBatchForm: guard(async payer => {
            const month = val('cauB_month'); if (!month) return toast('اختر شهر المطالبة', 'warning');
            const pasted = parsePastedLines(val('cauB_lines')), declared = val('cauB_total');
            if (declared === '' && !pasted.rows.length) return toast('أدخل إجمالي الخطاب أو أسماء المرضى', 'warning');
            if (pasted.bad.length && !confirm(`${pasted.bad.length} سطر لم يُفهم وسيتم تجاهله (مثال: ${pasted.bad[0].slice(0, 40)}). متابعة؟`)) return;
            const sameMonth = state.D.batches.filter(b => b.payer === payer && ym(b.claim_month) === month).length;
            const manual = val('cauB_source') === 'manual', label = `${payer} ${month}${manual ? ' يدوي' : ''}${sameMonth ? ' #' + (sameMonth + 1) : ''}`;
            if (declared !== '' && pasted.rows.length) { const sum = r2(pasted.rows.reduce((s, r) => s + r.amount, 0)); if (Math.abs(sum - num(declared)) > 1 && !confirm(`مجموع المرضى ${fmt(sum)} ≠ إجمالي الخطاب ${fmt(num(declared))} (فرق ${fmtS(sum - num(declared))}). حفظ رغم ذلك؟ سيظهر التحذير في ملاحظات الجودة.`)) return; }
            await saveBatch({ payer, label, claim_month: mStart(month), status: val('cauB_status'), source: val('cauB_source'), service_from: val('cauB_from'), service_to: val('cauB_to'), delivered_on: val('cauB_deliv'), declared_total: declared, note: val('cauB_note') }, pasted.rows);
            state.letters.form = false; await afterWrite(); toast('تم حفظ الدفعة', 'success');
        }),
        delBatch: guard(async id => { if (!confirm('حذف الدفعة وكل مرضاها؟')) return; await write('delete', `audit_claim_batches?id=eq.${id}`); await afterWrite(); }),
        viewLines: guard(async id => {
            const b = state.D.batches.find(x => String(x.id) === String(id)); state.linesModal = { label: b ? b.label : '', lines: [], q: '' }; paint();
            state.linesModal.lines = await getAll(`audit_claim_lines?select=national_id,patient_name,invoice_no,amount,flag&batch_id=eq.${id}&order=id.asc`); paint();
        }),
        closeLines: () => { state.linesModal = null; paint(); },
        linesQ: v => { if (state.linesModal) { state.linesModal.q = v; const pos = v.length; paint(); const i = document.querySelector('.hiu-modal input'); if (i) { i.focus(); i.setSelectionRange(pos, pos); } } },
        uhiaFile: guard(async f => {
            if (!f) return;
            if (typeof XLSX === 'undefined') { state.letters.uhiaPreview = { error: 'مكتبة XLSX غير محمّلة' }; return paint(); }
            const wb = XLSX.read(await readFile(f, 'buf'), { type: 'array' });
            const sheets = wb.SheetNames.map(n => ({ name: n, rows: XLSX.utils.sheet_to_json(wb.Sheets[n], { header: 1, raw: true, blankrows: false }) }));
            const pv = parseUhiaSheets(sheets);
            state.letters.uhiaPreview = pv.rows.length ? pv : { error: 'لم أجد أسطراً صالحة — تأكد من وجود عمودي «شهر الصرف» و«صافي المبلغ المطالب»' }; paint();
        }),
        uhiaCancel: () => { state.letters.uhiaPreview = null; paint(); },
        uhiaSave: guard(async () => {
            const pv = state.letters.uhiaPreview; if (!pv || !pv.rows) return;
            const replace = !!(document.getElementById('cauU_replace') || {}).checked, ks = Object.keys(pv.byMonth).sort();
            const existing = new Set(state.D.batches.filter(b => b.payer === 'UHIA').map(b => b.label));
            let done = 0, skipped = 0;
            for (const k of ks) {
                const label = `UHIA ${ym(k)}`; if (existing.has(label) && !replace) { skipped++; continue; }
                const lines = pv.rows.filter(r => r.claim_month === k).map(r => ({ national_id: r.national_id, patient_name: r.patient_name, invoice_no: r.invoice_no, amount: r.amount, flag: r.flag, note: [r.branch, r.note].filter(Boolean).join(' — ').slice(0, 300) }));
                await saveBatch({ payer: 'UHIA', label, claim_month: k, status: 'delivered', source: 'excel_import', note: 'استيراد Excel — مكرر بين الأوراق تمت إزالته' }, lines, { replace: true }); done++;
                state.loadMsg = `حفظ ${done}/${ks.length}`;
            }
            state.letters.uhiaPreview = null; await afterWrite(); toast(`تم حفظ ${done} دفعة${skipped ? ` وتخطي ${skipped} موجودة` : ''}`, 'success');
        }),

        hioFile: guard(async f => {
            if (!f) return;
            if (typeof XLSX === 'undefined') { state.letters.hioPreview = { error: 'مكتبة XLSX غير محمّلة' }; return paint(); }
            const wb = XLSX.read(await readFile(f, 'buf'), { type: 'array' });
            const sheets = wb.SheetNames.map(n => ({ name: n, rows: XLSX.utils.sheet_to_json(wb.Sheets[n], { header: 1, raw: true, blankrows: false }) }));
            const pv = parseHioCombined(sheets, { windowStart: state.D.settings.window_start });
            state.letters.hioPreview = pv.rows.length ? pv : { error: pv.error || 'لم أجد أسطراً صالحة' }; paint();
        }),
        hioCancel: () => { state.letters.hioPreview = null; paint(); },
        hioSave: guard(async () => {
            const pv = state.letters.hioPreview; if (!pv || !pv.groups) return;
            const replace = !!(document.getElementById('cauH_replace') || {}).checked, existing = new Set(state.D.batches.filter(b => b.payer === 'HIO').map(b => b.label));
            let done = 0, skipped = 0;
            for (const g of pv.groups) {
                if (existing.has(g.label) && !replace) { skipped++; continue; }
                const lines = pv.rows.filter(r => r.claim_month === g.claim_month && !!r.combined === !!g.combined).map(r => ({ national_id: r.national_id, patient_name: r.patient_name, invoice_no: r.invoice_no, approval_no: r.approval_no, amount: r.amount, note: [r.branch, r.note].filter(Boolean).join(' — ').slice(0, 300) }));
                await saveBatch({ payer: 'HIO', label: g.label, claim_month: g.claim_month, status: 'delivered', source: 'excel_import', note: (g.combined ? 'مطالبة مجمّعة (' + g.from_label + ') — لا تُفصل على شهرين. ' : '') + 'استيراد Excel: ' + g.files.join('، ').slice(0, 150) }, lines, { replace: true }); done++;
            }
            state.letters.hioPreview = null; await afterWrite(); toast(`تم حفظ ${done} دفعة HIO${skipped ? ` وتخطي ${skipped} موجودة` : ''}`, 'success');
        }),

        exportXlsx: guard(async () => {
            if (typeof XLSX === 'undefined') return toast('مكتبة XLSX غير محمّلة', 'error');
            const D = state.D, asOf = state.asOf || D.today, E = evalAt(D, asOf), S = D.settings, wb = XLSX.utils.book_new();
            const T = [['البند', 'القيمة'], ['حتى تاريخ', asOf], ['إيراد نفقة الدولة', E.revenue.SF], ['إيراد HIO', E.revenue.HIO], ['إيراد UHIA', E.revenue.UHIA], ['إيراد جهات أخرى', E.revenue.OTHER], ['إيراد قديم (إجماليات)', E.revenue.LEGACY], ['إجمالي الإيراد', E.revenue.total],
                ['مطالب — نفقة الدولة (SMC)', E.claimed.SF], ['مطالب — HIO', E.claimed.HIO], ['مطالب — UHIA', E.claimed.UHIA], ['مفتوح — نفقة الدولة', E.open.SF], ['مفتوح — HIO', E.open.HIO], ['مفتوح — UHIA', E.open.UHIA], ['جهات أخرى متوقعة', E.expectedOther], ['رصيد افتتاحي مخصوم', E.openingAdj], ['إجمالي المفسَّر', E.evidence], ['الفجوة', E.gap], ['نسبة الفجوة', E.gapPct]];
            XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet(T), 'Total');
            const start = S.closed_through ? addM(S.closed_through, 1) : addM(ym(D.today), -5), M = [['الشهر', ...GROUPS.flatMap(g => [g + ' إيراد', g + ' مطالب', g + ' Δمفتوح', g + ' فجوة'])]];
            monthlyRows(D, start, ym(D.today)).forEach(r => M.push([r.month, ...GROUPS.flatMap(g => [r.groups[g].revenue, r.groups[g].claimed, r.groups[g].dOpen, r.groups[g].gap])]));
            XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet(M), 'Monthly');
            XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([['تاريخ', 'مراجعة', 'حسابات', 'لم تُرسل', 'مرتجعة', 'مسدّدة', 'الإجمالي'], ...(D.snaps || []).map(s => { const t = smcTotals(s); return [s.snapshot_date, t.review, t.accounts, t.unsent, t.returned, t.paid, t.total]; })]), 'SMC');
            XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([['الجهة', 'الدفعة', 'الشهر', 'الحالة', 'المصدر', 'إجمالي الخطاب', 'مجموع المرضى', 'المحتسب'], ...(D.batches || []).map(b => [b.payer, b.label, ym(b.claim_month), b.status, b.source, b.declared_total, b.lines_total, batchTotal(b)])]), 'Letters');
            XLSX.writeFile(wb, `claims_audit_${asOf}.xlsx`);
        }),
    };
    window.__ca = A;

    /* ═════════════ registration (loader.js contract) ═════════════ */
    function ensureSection() {
        if (root()) return;
        const c = document.querySelector('.flex-1.p-6.bg-gray-50.min-h-screen') || document.querySelector('main .flex-1') || document.querySelector('main') || document.body;
        const div = document.createElement('div'); div.id = 'claimsAuditSection'; div.className = 'section hidden'; c.appendChild(div);
    }
    window.loadClaimsAudit = async function () {
        ensureCss(); ensureSection();
        if (!state.D || Date.now() - state.D.loadedAt > 10 * 60 * 1000) await reload(); else paint();
    };
    window.ClaimsAudit = { core, state, _paint: paint };
    window.claimsAuditReady = true;
})();
