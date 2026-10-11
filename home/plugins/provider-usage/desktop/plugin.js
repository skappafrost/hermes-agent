/**
 * Provider Usage: desktop status-bar item (right cluster).
 *
 * Two data sources, both on this machine:
 *
 * - `/usage` reads the one remote meter this account has: vilao's wallet. Every other provider Hermes
 *   calls here is a free tier with no usage endpoint, so there is nothing to poll for them.
 * - `/recent` reads the newest calls out of the Usage page's shared SQLite store, which is the only
 *   place those providers become measurable at all (Hermes logs every call itself).
 *
 * The popover has exactly two value modes, and the whole point is that neither one repeats itself:
 *
 * - **Hôm nay** — today's spend in front of the balance (`12.345đ / 47.001đ`), meter filled by today's
 *   share of what is left, so a full bar means today alone would empty the wallet.
 * - **Còn lại** — the balance in front of the lifetime top-up total (`47.001đ / 190.000đ`), meter filled
 *   by what remains of everything ever paid in.
 *
 * Poll cadence is 5 s by default (picker offers 5s/15s/30s/1m/5m). `/recent` is a local SQLite read, so
 * polling it hard is free; `/usage` costs one GET to vilao per tick, which at 12/min is a tenth of the
 * account's 120 req/min ceiling.
 *
 * Live without waiting on the timer at all: the Usage page's observer hook broadcasts
 * `plugin.hermes-resetwatch.call` for every model call from every profile, and a new row invalidates the
 * recent list. The listener receives the whole gateway event (`{type, session_id, payload}`) -- the row
 * is `event.payload`, not the argument itself.
 *
 * Money is dong everywhere: whole numbers with the sign after the amount (500đ), converted at the
 * backend's shared rate for anything a provider quotes in another currency.
 *
 * Colour follows the content, not the row index: a failed call is red, a call costing most of what the
 * visible five cost together is amber, a call whose tokens were mostly cache reads is accent-coloured,
 * and a wallet at zero tints the bar.
 *
 * Native notification (ctx.os.notify) fires once per day when the wallet reaches zero.
 */
import * as sdk from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const { STATUSBAR_AREAS, atom, host, queryClient, useQuery, useQueryClient, useValue } = sdk

const USAGE_KEY = ['plugin-provider-usage', 'backend']
const RECENT_KEY = ['plugin-provider-usage', 'recent']
/* The Usage page's plugin broadcasts the call events; the name is the wire contract, so it is spelled
 * out here rather than derived. */
const CALL_EVENT = 'plugin.hermes-resetwatch.call'
const RECENT_COUNT = 5
/* The bar prints the provider id shortened for reading; ids are the wire name, the label is the UI. */
const CODE = { 'vilao-ai': 'vilao' }

const INTERVALS = [
  { label: '5s', ms: 5000 },
  { label: '15s', ms: 15000 },
  { label: '30s', ms: 30000 },
  { label: '1m', ms: 60000 },
  { label: '5m', ms: 300000 }
]

const $interval = atom(5000)
const $mode = atom('used') // 'used' = hôm nay | 'remaining' = còn lại / tổng đã nạp

const STORE_INTERVAL = 'intervalMs'
const STORE_MODE = 'valueMode'
const STORE_ALERTS = 'alerts'

let storage = null
let osNotify = null
let rest = null
let liveWired = false

function persist(key, value) {
  if (!storage) return
  try {
    storage.set(key, value)
  } catch (e) {
    /* storage unavailable, session-only */
  }
}

function bindStorage(s) {
  storage = s
  try {
    let ms = storage.get(STORE_INTERVAL, null)
    /* 300000 was the previous default, and almost nobody picked it deliberately -- upgrade that stored
     * value to the new default instead of leaving the bar polling at 5 minutes forever. */
    if (ms === 300000) ms = 5000
    if (INTERVALS.some(o => o.ms === ms)) $interval.set(ms)
    const mode = storage.get(STORE_MODE, null)
    if (mode === 'used' || mode === 'remaining') $mode.set(mode)
  } catch (e) {
    /* ignore malformed persisted state */
  }
  $interval.listen(v => persist(STORE_INTERVAL, v))
  $mode.listen(v => persist(STORE_MODE, v))
}

async function fetchUsage(fresh) {
  if (!rest) throw new Error('plugin backend not bound')
  return rest(fresh ? '/usage?fresh=1' : '/usage')
}

async function fetchRecent() {
  if (!rest) throw new Error('plugin backend not bound')
  return rest('/recent?limit=' + RECENT_COUNT)
}

function useUsage() {
  const interval = useValue($interval)
  return useQuery({
    queryKey: USAGE_KEY,
    queryFn: () => fetchUsage(true),
    refetchInterval: interval,
    retry: 1,
    staleTime: 2000
  })
}

/* Same cadence as the balance, and the call event makes it instant on top of that. The floor poll is
 * what catches bill rows, which arrive from the Usage page's billing pulse rather than from a hook. */
function useRecent() {
  const interval = useValue($interval)
  return useQuery({
    queryKey: RECENT_KEY,
    queryFn: fetchRecent,
    refetchInterval: interval,
    retry: 1,
    staleTime: 2000
  })
}

function wireLiveFeed() {
  if (liveWired) return
  const onEvent = typeof host.onEvent === 'function' ? host.onEvent.bind(host) : null
  if (!onEvent || !queryClient) return
  liveWired = true
  onEvent(CALL_EVENT, event => {
    const row = event && event.payload
    if (!row || !row.ts) return
    queryClient.invalidateQueries(RECENT_KEY)
  })
}

/* ---------- formatting ---------- */

/* Dong after the amount, whole numbers: 5.167đ. The sign never prefixes a dong figure and the account
 * has no sub-dong unit, so no cent is ever meaningful. */
const CUR = { USD: '$', CNY: '\u00a5' }
const money = (v, cur) => {
  const n = Number(v) || 0
  if (!cur || cur === 'VND') return Math.round(n).toLocaleString('vi-VN') + 'đ'
  return (CUR[cur] || '$') + n.toFixed(2)
}
const dong = v => money(v, 'VND')

/* Tokens are read as magnitude: 110665 is a wall of digits, 110,7k is a number you can compare. */
const tk = v => {
  const n = Number(v) || 0
  if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1).replace('.', ',') + 'M'
  if (n >= 1000) return (n / 1000).toFixed(n >= 100000 ? 0 : 1).replace('.', ',') + 'k'
  return String(Math.round(n))
}

/* vi-VN puts the time before the date, so a locale string is sliced by part, never by position. */
const CLOCK = new Intl.DateTimeFormat('vi-VN',
  { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' })
const clock = ts => (ts ? CLOCK.format(new Date(Number(ts) * 1000)) : '—')
const hhmm = iso => {
  const d = new Date(iso)
  return isNaN(d) ? '—' : CLOCK.format(d)
}

function ago(ts) {
  if (!ts) return '—'
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)))
  if (secs < 60) return secs + 's'
  if (secs < 3600) return Math.floor(secs / 60) + 'p'
  if (secs < 86400) return Math.floor(secs / 3600) + 'h'
  return Math.floor(secs / 86400) + 'ngày'
}

const shortModel = m => {
  const parts = String(m || '?').split('/')
  return parts.length > 1 ? parts.slice(-2).join('/') : String(m || '?')
}

/* ---------- surfaces ---------- */

const INK = 'var(--ui-text-primary, currentColor)'
const INK_FAINT = 'var(--ui-text-secondary, rgba(128,128,128,0.85))'
const HAIRLINE = 'var(--ui-stroke-tertiary, rgba(128,128,128,0.22))'
const FILL = 'var(--ui-control-hover-background, rgba(128,128,128,0.14))'
const ACCENT = 'var(--ui-accent, #6f9dff)'
const HOT = 'text-amber-600 dark:text-amber-300'
const MONO = { fontVariantNumeric: 'tabular-nums' }
const card = { border: '1px solid ' + HAIRLINE, borderRadius: 9, padding: '8px 9px' }
const section = { fontSize: 10, letterSpacing: '0.09em', textTransform: 'uppercase', color: INK_FAINT }

function isWarn(p) {
  if (!p || p.status !== 'ok') return false
  return (p.money || []).some(m => Number(m.left) <= 0)
}

/* ---------- bar / tooltip ---------- */

function balanceRow(p) {
  const rows = Array.isArray(p.money) ? p.money : []
  return rows.find(m => m.label === 'Balance') || rows[0] || null
}

/* One-line summary for the bar. Two readings of the same wallet: the mode named for today prints
 * today's spend, the mode named for what is left prints the balance. With nothing spent yet today there
 * is nothing to report as used, so the bar falls back to the balance instead of printing a bare 0đ all
 * morning. */
function summary(p, mode) {
  if (!p || p.status !== 'ok') return '-'
  const m = balanceRow(p)
  if (!m) return '-'
  const today = Number(m.today_spent) || 0
  if (mode === 'used' && today > 0) return money(today, m.cur)
  return money(m.left, m.cur)
}

function tipLine(p, mode) {
  if (!p || p.status !== 'ok') return 'unavailable'
  const m = balanceRow(p)
  if (!m) return 'no balance reported'
  const today = Number(m.today_spent) || 0
  const total = Number(m.total) || 0
  if (mode === 'used') {
    return 'hôm nay ' + money(today, m.cur) + ' · còn ' + money(m.left, m.cur)
  }
  return 'còn ' + money(m.left, m.cur) + (total ? ' / đã nạp ' + money(total, m.cur) : '')
    + (today ? ' · hôm nay ' + money(today, m.cur) : '')
}

/* The bar's number is the wallet. The moment a charge actually lands, the amount that left is the more
 * interesting figure for a second or two, so the chip swaps to it and swaps back.
 *
 * Two things this has to survive. (1) No reflow: the amount sits in its own inline-block with tabular
 * numerals and reserves the wider of the two strings, so the swap cannot grow or shrink the status-bar
 * slot. In practice the reserve equals the running total's own width anyway -- a per-call charge is
 * hundreds of dong while the number shown is tens of thousands -- so the bar looks exactly as wide as it
 * did before this existed. (2) Truth about timing: the trigger is money *leaving*, measured as today's
 * spend rising between polls, not a call finishing. A call's charge reaches the invoice a few seconds
 * later, and flashing a spend for a call that failed or was free would train the reader to ignore the
 * flash. */
const FLASH_MS = 1500

/* Which provider's wallet just moved, or none. Split from the component because this is the part that can
 * lie: a call that failed, a call that has not reached the invoice yet, or the first poll after launch
 * (no previous number to compare against) must all produce no flash. Kept pure so the harness can assert
 * those branches without a browser, a timer or a second render. */
function spendDelta(providers, seen) {
  let hit = null
  for (const p of (providers || [])) {
    const m = balanceRow(p)
    const today = m ? Number(m.today_spent) || 0 : 0
    const before = seen[p.id]
    seen[p.id] = today
    if (p.status === 'ok' && before != null && today > before) {
      hit = { id: p.id, amount: today - before, cur: (m && m.cur) || 'VND' }
    }
  }
  return hit
}

function BarDetail() {
  const { data, isError } = useUsage()
  const mode = useValue($mode)
  const seen = useRef(Object.create(null))
  const timer = useRef(null)
  const [flash, setFlash] = useState(null)

  useEffect(() => {
    if (data) checkAlerts(data)
  }, [data])

  useEffect(() => {
    if (!data) return
    const hit = spendDelta(data.providers || [], seen.current)
    if (!hit) return
    setFlash(hit)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setFlash(null), FLASH_MS)
  }, [data])

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  if (isError) return 'backend down'
  if (!data) return '…'
  const parts = []
  ;(data.providers || []).forEach((p, i) => {
    if (i) parts.push(' · ')
    const normal = summary(p, mode)
    const fl = flash && flash.id === p.id
      ? '−' + money(flash.amount, flash.cur || 'VND') : null
    /* Measured in characters on purpose: the two strings are the same shape (grouped digits + one sign),
       so the count is a stable floor for the box and no DOM measurement is needed. */
    const reserve = Math.max(String(normal).length, fl ? String(fl).length : 0)
    parts.push(jsx('span', {
      className: isWarn(p) ? HOT : undefined,
      children: [
        (CODE[p.id] || p.id) + ' ',
        jsx('span', {
          style: { ...MONO, display: 'inline-block', minWidth: reserve + 'ch', textAlign: 'right',
            whiteSpace: 'nowrap', overflow: 'hidden', verticalAlign: 'bottom',
            color: fl ? 'var(--ui-warning, #d9a13b)' : undefined,
            transition: 'color 220ms ease-out' },
          title: fl ? 'vừa chi ' + money(flash.amount, flash.cur) : undefined,
          children: fl || normal
        }, 'amt')
      ]
    }, p.id))
  })
  // The host renders plugin detail content in text-muted-foreground/80 (a double-dimmed tone);
  // force the same token built-in statusbar values use.
  return jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: parts })
}

function BarTip() {
  const { data } = useUsage()
  const mode = useValue($mode)
  const rows = data && Array.isArray(data.providers) ? data.providers : []
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 2, minWidth: 235, fontSize: 11, textAlign: 'left' },
    children: rows.length
      ? rows.map(p =>
          jsxs('div', {
            style: { display: 'flex', justifyContent: 'space-between', gap: 12 },
            children: [
              jsx('span', { style: { fontWeight: 600 }, children: p.name }, 'n'),
              jsx('span', { style: { opacity: 0.8, ...MONO }, children: tipLine(p, mode) }, 'v')
            ]
          })
        )
      : [jsx('div', { children: 'Provider usage: loading…' }, 'load')]
  })
}

/* ---------- alerts (native notification, deduped per day) ---------- */

function checkAlerts(data) {
  if (!storage || !data || !Array.isArray(data.providers)) return
  try {
    const seen = storage.get(STORE_ALERTS, {}) || {}
    const now = Date.now()
    const day = Math.floor(now / 86400000)
    let dirty = false

    for (const p of data.providers) {
      if (p.status !== 'ok') continue
      const m = balanceRow(p)
      if (!m || Number(m.left) > 0) continue
      const key = p.id + ':empty:' + day
      if (seen[key]) continue
      seen[key] = now
      dirty = true
      if (osNotify) {
        try {
          osNotify({ title: p.name + ': hết tiền', body: 'Còn ' + money(m.left, m.cur) + ' — nạp thêm để gọi model tiếp.' })
        } catch (e) {
          /* native notification unavailable */
        }
      }
    }

    if (dirty) {
      const entries = Object.entries(seen).filter(e => e[1] > now - 30 * 86400000)
        .sort((a, b) => b[1] - a[1]).slice(0, 80)
      storage.set(STORE_ALERTS, Object.fromEntries(entries))
    }
  } catch (e) {
    /* never break the bar over alerts */
  }
}

/* ---------- popover blocks ---------- */

function Meter({ width }) {
  const raw = Number(width)
  const w = isFinite(raw) && raw > 0 ? Math.max(3, Math.min(100, raw)) : 0
  return jsx('span', {
    style: { flex: '1 1 auto', height: 3, borderRadius: 3, background: FILL, overflow: 'hidden' },
    children: w > 0
      ? jsx('span', {
          style: { display: 'block', height: '100%', width: w + '%', borderRadius: 3, background: ACCENT, opacity: 0.9 }
        })
      : null
  })
}

function Row({ label, value, width, meta, warn }) {
  return jsxs('div', {
    style: { display: 'flex', alignItems: 'center', gap: 8, padding: '2px 4px', margin: '0 -4px', borderRadius: 6 },
    children: [
      jsx('span', {
        style: { width: 72, flex: 'none', fontSize: 12, color: INK_FAINT, whiteSpace: 'nowrap',
          overflow: 'hidden', textOverflow: 'ellipsis' },
        children: label
      }, 'label'),
      jsx(Meter, { width }, 'bar'),
      jsx('span', {
        style: { flex: 'none', textAlign: 'right', fontWeight: 600, fontSize: 12, color: INK, ...MONO },
        className: warn ? HOT : undefined,
        children: value
      }, 'value'),
      jsx('span', {
        style: { width: 96, flex: 'none', textAlign: 'right', fontSize: 11, color: INK_FAINT, ...MONO,
          whiteSpace: 'nowrap' },
        children: meta
      }, 'meta')
    ]
  })
}

function ProviderBlock({ p, mode }) {
  const rows = []
  if (p.status !== 'ok') {
    rows.push(jsx('div', { style: { fontSize: 11, color: INK_FAINT },
      children: 'unavailable: ' + (p.error || 'fetch failed') }, 'err'))
  }
  for (const m of p.money || []) {
    const left = Number(m.left) || 0
    const today = Number(m.today_spent) || 0
    const total = Number(m.total) || 0
    /* The two modes must not print the same pair twice. Hôm nay reads today against the balance (meter =
     * today's share of what is left, so a full bar means today alone would empty the wallet); Còn lại
     * reads the balance against everything ever paid in (meter = the fraction of the lifetime top-up
     * still unused). */
    if (mode === 'used' && today > 0 && left > 0) {
      rows.push(jsx(Row, {
        label: m.label,
        value: money(today, m.cur),
        width: (today / left) * 100,
        meta: '/ ' + money(left, m.cur),
        warn: left <= 0
      }, 'm' + m.label))
      continue
    }
    rows.push(jsx(Row, { label: m.label, value: money(left, m.cur),
      width: total > 0 ? (left / total) * 100 : 0,
      meta: total > 0 ? '/ ' + money(total, m.cur) : '', warn: left <= 0 }, 'm' + m.label))
  }
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 3 },
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 2 },
        children: [
          jsx('span', { style: { ...section, color: INK, fontWeight: 650, fontSize: 11 }, children: p.name }, 'n'),
          jsx('span', { style: { fontSize: 11, color: INK_FAINT }, children: p.tag || '' }, 't')
        ]
      }, 'head'),
      ...rows
    ]
  })
}

const GRID = { display: 'grid', gridTemplateColumns: '52px 1fr 58px 58px', gap: 7, alignItems: 'center' }

function RecentRow({ row, hotDong }) {
  const cost = Number(row.dong) || 0
  const total = Number(row.tok_total) || 0
  const cache = Number(row.tok_cache_read) || 0
  const fresh = Date.now() / 1000 - Number(row.ts) < 90
  const heavy = hotDong > 0 && cost >= hotDong
  const ms = Number(row.duration_ms) || 0
  return jsxs('div', {
    title: clock(row.ts) + ' · ' + (row.model || '?') + ' · ' + (row.profile || '?') +
      (ms ? ' · ' + (ms / 1000).toFixed(1) + 's' : '') +
      (row.error ? ' · FAILED' : '') + (row.billed ? '' : ' · Hermes ước tính, chưa khớp hóa đơn'),
    style: { ...GRID, fontSize: 11, color: INK, padding: '2px 0',
      borderTop: '1px solid ' + HAIRLINE },
    children: [
      jsxs('span', { style: { ...MONO, color: fresh ? ACCENT : INK_FAINT }, children: [
        ago(row.ts), fresh ? ' ●' : ''
      ] }, 'age'),
      jsxs('span', { style: { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis',
        whiteSpace: 'nowrap', color: row.error ? 'var(--ui-danger, #e5484d)' : INK },
        children: [
          shortModel(row.model),
          row.error ? ' · lỗi' : '',
          jsx('span', { style: { color: INK_FAINT, marginLeft: 5, fontSize: 10 },
            children: row.provider || '?' }, 'prov')
        ] }, 'model'),
      jsx('span', { style: { ...MONO, textAlign: 'right', color: total && cache / total > 0.5 ? ACCENT : INK_FAINT },
        title: total ? 'cache = ' + Math.round(cache / total * 100) + '% số token' : '',
        children: tk(total) }, 'tok'),
      jsx('span', { className: heavy || row.error ? HOT : undefined,
        style: { ...MONO, textAlign: 'right', fontWeight: 600 },
        children: row.dong == null ? '—' : dong(row.dong) + (row.billed ? '' : '*') }, 'dong')
    ]
  })
}

/* The store answers per-day dong only for days that had bill rows, so the gaps are filled here: a day
 * with no spending is a zero on the line, not a skipped point -- a chart that quietly drops the flat
 * days makes a quiet week look like no data at all. */
function sparkSeries(days, span) {
  const map = Object.create(null)
  for (const pair of (days || [])) map[String(pair[0])] = Number(pair[1]) || 0
  const out = []
  const now = new Date()
  for (let i = span - 1; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i)
    const key = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
      '-' + String(d.getDate()).padStart(2, '0')
    out.push([key, map[key] || 0])
  }
  return out
}

function Spark({ series }) {
  const [idx, setIdx] = useState(-1)
  const W = 150, H = 22
  if (!series || !series.length) return null
  const max = Math.max(1, ...series.map(s => s[1]))
  const step = W / (series.length - 1 || 1)
  const pts = series.map((s, i) => [i * step, H - (s[1] / max) * (H - 2) - 1])
  const line = pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ')
  const total = series.reduce((a, s) => a + s[1], 0)
  const last7 = series.slice(-7).reduce((a, s) => a + s[1], 0)
  const read = idx >= 0 && idx < series.length ? series[idx] : null
  /* The line is 150 px wide and 14 points, so a mouse position maps back to a day by simple division --
   * no library, no measurement of the polyline. The caption swaps between "14 days · total" and the one day
   * under the cursor; both are single-line and the svg width is fixed, so hovering cannot move the popover.
   */
  const track = e => {
    const el = e.currentTarget
    if (!el || !el.getBoundingClientRect) return
    const r = el.getBoundingClientRect()
    if (!r.width) return
    const i = Math.round((((e.clientX - r.left) / r.width) * W) / step)
    setIdx(Math.max(0, Math.min(series.length - 1, i)))
  }
  const last = pts[pts.length - 1]
  return jsxs('div', {
    style: { display: 'flex', alignItems: 'center', gap: 7 },
    title: '14 ngày gần nhất · tổng ' + dong(Math.round(total)) + '\n7 ngày gần: '
      + dong(Math.round(last7)) + ' · cao nhất ' + dong(Math.round(max)) + '/ngày',
    children: [
      jsx('svg', {
        viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, 'aria-hidden': 'true',
        onMouseMove: track, onMouseLeave: () => setIdx(-1),
        style: { flexShrink: 0, overflow: 'visible', cursor: 'crosshair' },
        children: [
          jsx('polyline', { points: line, fill: 'none', stroke: 'currentColor', strokeWidth: 1.2,
            strokeLinejoin: 'round', opacity: 0.85 }, 'l'),
          read ? jsx('line', { x1: pts[idx][0].toFixed(1), x2: pts[idx][0].toFixed(1), y1: 0, y2: H,
            stroke: 'currentColor', strokeWidth: 0.6, strokeDasharray: '2 2', opacity: 0.6 }, 'v') : null,
          jsx('circle', { cx: read ? pts[idx][0].toFixed(1) : last[0],
            cy: read ? pts[idx][1].toFixed(1) : last[1], r: 1.9, fill: 'currentColor' }, 'dot')
        ]
      }, 'svg'),
      jsxs('span', { style: { ...MONO, fontSize: 10, color: INK_FAINT }, children: [
        jsx('span', { children: read ? read[0] : '14 ngày' }, 'a'),
        jsx('span', { style: { marginLeft: 5, color: read ? INK : 'inherit' },
          children: dong(Math.round(read ? read[1] : total)) }, 'b')
      ] }, 'label')
    ]
  })
}

function RecentBlock({ data, onOpen }) {
  const rows = (data && data.rows) || []
  const today = data && data.today
  const series = data && data.days ? sparkSeries(data.days, 14) : null
  const hotDong = rows.length
    ? Math.max(...rows.map(r => Number(r.dong) || 0)) * 0.6
    : 0
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 4 },
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8 },
        children: [
          jsx('span', { style: section, children: 'request gần đây' }, 'h'),
          onOpen ? jsx('button', {
            type: 'button', onClick: onOpen,
            style: { fontSize: 11, padding: '1px 7px', borderRadius: 6, border: '1px solid ' + HAIRLINE,
              background: 'transparent', color: INK_FAINT, cursor: 'pointer' },
            children: 'tab Usage →'
          }, 'open') : null
        ]
      }, 'head'),
      today
        ? jsxs('div', {
            style: { ...MONO, fontSize: 11, color: INK_FAINT, display: 'flex', gap: 8, flexWrap: 'wrap' },
            children: [
              jsx('span', { children: 'hôm nay ' + today.calls + ' lượt' }, 'c'),
              jsx('span', { children: tk(today.tokens) + ' token' }, 't'),
              today.cache ? jsx('span', { children: tk(today.cache) + ' cache' }, 'k') : null,
              jsx('span', { style: { fontWeight: 600, color: INK }, children: dong(today.dong) }, 'd')
            ]
          }, 'day')
        : null,
      /* The trend is the part the two-number readout cannot carry: "44.352đ hôm nay" says nothing about
       * whether today is normal. Fifteen pixels of line says whether the week is going quiet or loud. */
      series && series.length ? jsx(Spark, { series }) : null,
      rows.length
        ? rows.map((r, i) => jsx(RecentRow, { row: r, hotDong }, String(r.ts) + ':' + i))
        : jsx('div', { style: { fontSize: 11, color: data && data.error ? 'var(--ui-danger, #e5484d)' : INK_FAINT },
            /* "no calls yet" and "the store could not be read" must not look the same, or a broken store
             * spends the week passing for a quiet one. */
            children: data && data.error
              ? 'không đọc được store: ' + String(data.error)
              : 'chưa có lượt gọi nào trong store — bật tab Usage để ghi lại' }, 'empty'),
    ]
  })
}

/* ---------- footer controls ---------- */

function ModeSelect() {
  const mode = useValue($mode)
  const seg = (key, label) => {
    const active = mode === key
    return jsx('button', {
      type: 'button',
      onClick: () => $mode.set(key),
      style: {
        padding: '1px 7px', fontSize: 11, lineHeight: '16px', cursor: 'pointer', borderRadius: 6,
        border: '1px solid ' + (active ? ACCENT : HAIRLINE),
        background: active ? FILL : 'transparent',
        color: active ? INK : INK_FAINT
      },
      children: label
    }, key)
  }
  return jsx('span', {
    style: { display: 'inline-flex', gap: 4 },
    children: [seg('used', 'Hôm nay'), seg('remaining', 'Còn lại')]
  })
}

function IntervalSelect({ onChanged }) {
  const interval = useValue($interval)
  const [open, setOpen] = useState(false)
  const wrapRef = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const onDoc = e => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  const current = INTERVALS.find(o => o.ms === interval) || INTERVALS[2]

  return jsxs('span', {
    ref: wrapRef,
    style: { position: 'relative', display: 'inline-flex', alignItems: 'center', gap: 5, color: INK_FAINT },
    children: [
      jsx('span', { children: 'làm mới số dư' }, 'label'),
      jsxs('button', {
        type: 'button',
        onClick: () => setOpen(v => !v),
        title: 'Refresh interval',
        style: {
          display: 'inline-flex', alignItems: 'center', gap: 3, padding: '1px 6px', fontSize: 11, lineHeight: '16px',
          border: '1px solid ' + HAIRLINE, borderRadius: 6, background: 'transparent',
          color: INK, cursor: 'pointer'
        },
        children: [
          jsx('span', { children: current.label }, 'v'),
          jsx('svg', {
            viewBox: '0 0 24 24', width: 9, height: 9, fill: 'none', stroke: 'currentColor',
            strokeWidth: 2.5, strokeLinecap: 'round', strokeLinejoin: 'round',
            children: jsx('path', { d: 'm6 9 6 6 6-6' })
          }, 'caret')
        ]
      }, 'btn'),
      open &&
        jsx('div', {
          style: {
            position: 'absolute', bottom: 'calc(100% + 4px)', left: 0, zIndex: 40,
            display: 'flex', flexDirection: 'column', gap: 1, minWidth: 58, padding: 3,
            border: '1px solid ' + HAIRLINE, borderRadius: 8, background: 'var(--ui-bg-elevated, #26272b)',
            boxShadow: '0 6px 18px rgba(0,0,0,.35)'
          },
          children: INTERVALS.map(o =>
            jsx('button', {
              type: 'button',
              onClick: () => {
                $interval.set(o.ms)
                setOpen(false)
                if (onChanged) onChanged(o.ms)
              },
              style: {
                textAlign: 'left', padding: '2px 8px', fontSize: 11, border: 'none', borderRadius: 6,
                cursor: 'pointer', color: INK,
                background: o.ms === interval ? FILL : 'transparent'
              },
              children: o.label
            }, o.ms)
          )
        })
    ]
  })
}

/* ---------- popover ---------- */

function UsagePanel() {
  const { data, isError } = useUsage()
  const { data: live } = useRecent()
  const qc = useQueryClient()
  const mode = useValue($mode)
  const [spinning, setSpinning] = useState(false)

  useEffect(() => {
    wireLiveFeed()
  }, [])

  const refresh = () => {
    if (spinning) return
    setSpinning(true)
    Promise.all([fetchUsage(true), fetchRecent()])
      .then(([usage, recent]) => {
        qc.setQueryData(USAGE_KEY, usage)
        qc.setQueryData(RECENT_KEY, recent)
      })
      .catch(() => {})
      .then(() => setTimeout(() => setSpinning(false), 600))
  }

  const provs = data && Array.isArray(data.providers) ? data.providers : []
  const cards = []
  provs.forEach(p => cards.push(jsx(ProviderBlock, { p, mode }, p.id)))
  if (isError) {
    cards.push(jsx('div', { style: { fontSize: 11, color: INK },
      children: 'Backend unreachable. Enable the plugin and retry.' }, 'down'))
  }
  cards.push(jsx(RecentBlock, {
    data: live,
    onOpen: typeof host.navigate === 'function' ? () => host.navigate('/resetwatch') : null
  }, 'recent'))
  /* The backend converts dollar and yuan balances to dong, which is the unit the account that pays
   * for these calls is denominated in. Say what it converted at: the figure is an approximation of a
   * provider's own number, and a rate that is silently wrong is worse than a rate that is visibly old. */
  let fxLine = null
  if (data && data.fx && data.fx.usd_vnd) {
    fxLine = jsx('div', {
      style: { fontSize: 11, color: INK_FAINT, ...MONO },
      children: 'Tỷ giá quy đổi: ' + dong(data.fx.usd_vnd) + '/USD' +
        (data.fx.cny_vnd ? ', ' + dong(data.fx.cny_vnd) + '/CNY' : '') +
        (data.fx.stale ? ' (tỷ giá cũ)' : '') + ' · ' + (data.fx.source || '')
    }, 'fx')
  }

  const stamp = data
    ? 'cập nhật ' + hhmm(data.fetched_at)
    : isError ? 'unreachable' : 'loading…'

  return jsxs('div', {
    style: { display: 'flex', width: 344, flexDirection: 'column', gap: 10, padding: '11px 12px 10px',
      fontSize: 12, color: INK },
    children: [
      jsx('style', { children: '@keyframes hermes-usage-spin{to{transform:rotate(360deg)}}' }, 'kf'),
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 },
        children: [
          jsx('span', { style: { fontSize: 12, fontWeight: 650 }, children: 'Provider Usage' }, 'title'),
          jsxs('span', {
            style: { display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11, color: INK_FAINT },
            children: [
              jsx('span', { style: MONO, children: stamp }, 't'),
              jsx('button', {
                type: 'button',
                onClick: refresh,
                title: 'Refresh usage',
                'aria-label': 'Refresh usage',
                style: {
                  display: 'inline-flex', width: 21, height: 21, alignItems: 'center', justifyContent: 'center',
                  border: '1px solid ' + HAIRLINE, borderRadius: 7,
                  background: 'transparent', color: INK, cursor: 'pointer'
                },
                children: jsx('svg', {
                  viewBox: '0 0 24 24', width: 11, height: 11, fill: 'none', stroke: 'currentColor',
                  strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round',
                  style: spinning ? { animation: 'hermes-usage-spin .7s linear 2' } : undefined,
                  children: [
                    jsx('path', { d: 'M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8' }, 'p1'),
                    jsx('path', { d: 'M21 3v5h-5' }, 'p2'),
                    jsx('path', { d: 'M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16' }, 'p3'),
                    jsx('path', { d: 'M8 16H3v5' }, 'p4')
                  ]
                }, 'icon')
              }, 'refresh')
            ]
          }, 'right')
        ]
      }, 'head'),
      ...cards.map((node, i) => jsx('div', { style: card, children: node }, 'card' + i)),
      fxLine,
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
          borderTop: '1px solid ' + HAIRLINE, paddingTop: 7, fontSize: 11 },
        children: [
          jsx(IntervalSelect, { onChanged: () => qc.refetchQueries({ queryKey: USAGE_KEY }) }, 'int'),
          jsx(ModeSelect, {}, 'mode')
        ]
      }, 'foot')
    ]
  })
}

/* ---------- registration ---------- */

const ICON = jsx('svg', {
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  style: { width: 12, height: 12, display: 'block' },
  children: [jsx('path', { d: 'm12 14 4-4' }, 'a'), jsx('path', { d: 'M3.34 19a10 10 0 1 1 17.32 0' }, 'b')]
})

export default {
  id: 'provider-usage',
  name: 'Provider Usage',
  description: 'Vilao wallet plus the five most recent model calls, in the status bar.',
  register(ctx) {
    if (!storage) bindStorage(ctx.storage)
    try {
      osNotify = ctx.os && typeof ctx.os.notify === 'function' ? ctx.os.notify.bind(ctx.os) : null
    } catch (e) {
      osNotify = null
    }
    try {
      rest = ctx.rest || null
    } catch (e) {
      rest = null
    }
    // Subscribe at registration, not inside the popover: the event is dropped when nobody listens, so a
    // subscription created on open can never warm anything before the panel is drawn.
    wireLiveFeed()
    ctx.register({
      id: 'status',
      area: STATUSBAR_AREAS.right,
      order: 40,
      data: {
        id: 'provider-usage',
        icon: ICON,
        detail: jsx(BarDetail, {}),
        variant: 'menu',
        menuAlign: 'end',
        menuClassName: 'w-auto border-(--ui-stroke-secondary) p-0',
        menuContent: () => jsx(UsagePanel, {}),
        title: jsx(BarTip, {}),
        toggleLabel: 'Provider usage'
      }
    })
  }
}
