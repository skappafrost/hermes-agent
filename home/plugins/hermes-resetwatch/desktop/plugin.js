/**
 * Usage: per-model call log and cost charts for Hermes Desktop.
 *
 * The page keeps Resetwatch's identity (plugin id, `/resetwatch` route, watch icon, sidebar entry,
 * palette command, keybind) and replaces everything that used to live in it. Rows come from the
 * plugin's own backend (`/api/plugins/resetwatch/`), which stores one row per model call from two
 * feeds: Hermes' observer hooks (every provider, USD estimate) and vilao's billing API (real dong).
 * A new call arrives by event, not by polling; aggregates refresh every 5 minutes.
 *
 * House rules inherited from the previous page: no polling faster than 5 minutes, no cookies and no
 * credential CLIs. Colour comes from the app's own CSS tokens with a neutral fallback, which is why
 * the filter widgets are hand-built instead of native: a native dropdown paints its popup with the
 * OS palette, and on a dark skin that arrived as a white box carrying the page's light text.
 */

import * as sdk from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

/* Must equal the agent-side plugin.yaml name: ctx.rest addresses /api/plugins/<this id>/ and the
 * dashboard drops any dashboard manifest whose name is not a known agent plugin (spoof guard), so a
 * mismatched id 404s the whole page. */
const PLUGIN_ID = 'hermes-resetwatch'
const PLUGIN_NAME = 'Usage'
const ROUTE = '/resetwatch'
const VERSION = '0.6.0'
const POLL_MS = 5 * 60 * 1000
const TABS = ['live', 'trend', 'log', 'quota', 'wallet', 'keys', 'models']
const TAB_LABEL = { live: 'Live', trend: 'Trend', log: 'Log', quota: 'Quota', wallet: 'Wallet',
  keys: 'Keys', models: 'Models' }

const host = sdk.host
const { useValue, atom, useQuery, queryClient, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, KEYBINDS_AREA } = sdk

let rest = null
let onEvent = typeof host.onEvent === 'function' ? host.onEvent.bind(host) : null

const $tab = atom('live')
const $error = atom(null)
const $sync = atom(null)
const $mode = atom('hermes')      /* 'hermes' = mọi lượt gọi Hermes ghi | 'vilao' = hóa đơn vilao */
const $provider = atom('')
const $key = atom('')             /* vilao consumer_key_id, '' = mọi key */
const $range = atom('')           /* hours, '' = toàn bộ store */
const $outcome = atom('')         /* '' | 'ok' | 'error' */
const $search = atom('')
const $mainOnly = atom(false)
const $profile = atom('')          /* '' = mọi home; '-' = các dòng hóa đơn không rõ home */
const $page = atom(1)              /* trang của bảng log; bộ lọc đổi là về trang 1 */
const $modelsPage = atom(1)        /* trang bảng offer — để phím [ ] đổi được trang ở mọi tab */
const $day = atom('')              /* '' = mọi ngày; một ngày cụ thể khi bấm vào ô lịch */
const $openRow = atom(null)

/* The store is paged because it holds tens of thousands of calls and the page used to read the first 400
 * and call that the whole picture. */
const PER_PAGE = 100
/* A billing row carries no home: a charge belongs to the account and to the API key it was billed to.
 * The backend reads this exact value back as "the unattributed ones". */
const NO_HOME = '-'

/* ---------- formatting ---------- */

/* Dong prints after the amount, the way the account itself writes it: 500đ, never ₫500. */
const dong = v => Math.round(Number(v) || 0).toLocaleString('vi-VN') + 'đ'
const num = v => (Number(v) || 0).toLocaleString('en-US')
const pct = v => (Math.round((Number(v) || 0) * 10) / 10).toFixed(1) + '%'

/* Hermes prices models in dollars and vilao bills in dong, and the page is read in dong, so an
 * estimate is converted before it is printed. The rate is the backend's -- one cached table shared with
 * provider-usage, so the status bar and this page cannot disagree. If that table has never arrived, the
 * page converts at the same bootstrap rate the backend hardcodes rather than showing a dollar sign: a
 * figure labelled as an estimate is honest, a mixed-unit page is not. */
const FALLBACK_USD_VND = 26200
let FX = null
const rate = () => Number(FX && FX.usd_vnd) || FALLBACK_USD_VND
const est = v => v == null || v === '' ? '—' : dong(Number(v) * rate())
const estTitle = v => v == null ? '' : 'Hermes ước tính ' + Number(v).toFixed(4) + ' USD × ' +
  rate().toLocaleString('vi-VN') + 'đ/USD' + (FX && FX.usd_vnd ? '' : ' (tỷ giá cố định)')

/* vi-VN writes the time FIRST and the date last ("21:48:49 8/10/2026"), so slicing the tail of the
 * combined locale string returns the date, not the clock -- which is the bug this replaces. Each part
 * is formatted on its own, with an explicit 24-hour cycle rather than hour12, because hour12 is only a
 * preference and some ICU builds still answer with CH/SA. */
const CLOCK = new Intl.DateTimeFormat('vi-VN',
  { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' })
const DATE = new Intl.DateTimeFormat('vi-VN', { day: '2-digit', month: '2-digit', year: 'numeric' })
const LONG = new Intl.DateTimeFormat('vi-VN', { weekday: 'short', day: '2-digit', month: '2-digit',
  year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' })
const dateOf = ts => ts ? DATE.format(new Date(Number(ts) * 1000)) : '—'
const clockOf = ts => ts ? CLOCK.format(new Date(Number(ts) * 1000)) : '—'
const longOf = ts => ts ? LONG.format(new Date(Number(ts) * 1000)) : '—'

/* Token counts are read as magnitude, not digits: 107520 is a wall of text, 107,5k is a number. */
/* Tokens are read as magnitude: 110665 is a wall of digits, 110,7k is a number you can compare. */
const tok = v => {
  const n = Number(v) || 0
  if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1).replace('.', ',') + 'M'
  if (n >= 1000) return (n / 1000).toFixed(n >= 100000 ? 0 : 1).replace('.', ',') + 'k'
  return String(Math.round(n))
}

function ago(ts) {
  if (!ts) return '—'
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)))
  if (secs < 60) return secs + 's trước'
  if (secs < 3600) return Math.floor(secs / 60) + 'p trước'
  if (secs < 86400) return Math.floor(secs / 3600) + 'h trước'
  return Math.floor(secs / 86400) + 'ngày trước'
}

function shortModel(model) {
  const parts = String(model || '?').split('/')
  return parts.length > 1 ? parts.slice(-2).join('/') : parts[0]
}

/* A session id is a uuid; the first block is the only part a human can compare by eye, and the tooltip
   carries the whole thing. */
const shortSession = id => String(id || '?').slice(0, 8)

/* How long a session ran, in the unit that fits. Same-day spans are the common case, so hours and
   minutes carry the detail rather than a bare "0 ngày". */
function spanOf(first, last) {
  const secs = Math.max(0, Number(last) - Number(first))
  if (!secs) return '—'
  if (secs < 60) return Math.round(secs) + 's'
  if (secs < 3600) return Math.round(secs / 60) + 'p'
  if (secs < 86400) return (secs / 3600).toFixed(secs < 36000 ? 1 : 0).replace('.', ',') + 'h'
  return Math.round(secs / 86400) + ' ngày'
}

const money = row => (row.bill_cost_vnd != null ? dong(row.bill_cost_vnd) : est(row.est_cost_usd))

/* ---------- data ---------- */

async function api(path, options) {
  if (!rest) throw new Error('plugin backend not mounted')
  return rest(path, options)
}

const CALLS_KEY = ['usage', 'calls']
const VILAO_KEY = ['usage', 'vilao']
const SUMMARY_KEY = ['usage', 'summary']
const BALANCE_KEY = ['usage', 'balance']
const KEYS_KEY = ['usage', 'keys']
const PROFILES_KEY = ['usage', 'profiles']
const RATE_KEY = ['usage', 'rate']
const CATALOG_KEY = ['usage', 'catalog']
const TOPUPS_KEY = ['usage', 'topups']
const DETAIL_KEY = ['usage', 'key-detail']
const LOG_KEY = ['usage', 'log']
const SESSIONS_KEY = ['usage', 'sessions']

const q = params => Object.entries(params)
  .filter(([, v]) => v !== '' && v != null)
  .map(([k, v]) => k + '=' + encodeURIComponent(v))
  .join('&')

/* Both log queries are one page of a paged table: the answer carries `total`/`pages`, so the pager knows
 * whether there is a next page instead of guessing from a short list. */
function useCalls(active) {
  const provider = useValue($provider)
  const search = useValue($search)
  const mainOnly = useValue($mainOnly)
  const range = useValue($range)
  const outcome = useValue($outcome)
  const profile = useValue($profile)
  const page = useValue($page)
  const day = useValue($day)
  return useQuery({
    queryKey: [...CALLS_KEY, provider, search, mainOnly, range, outcome, profile, page, day],
    queryFn: () => api('/calls?' + q({
      per: PER_PAGE, page, provider, model: search, outcome, hours: day ? '' : range, profile,
      day, main_only: mainOnly ? 1 : ''
    })),
    enabled: active,
    refetchInterval: active ? POLL_MS : false,
    staleTime: 20000
  })
}

/* The Vilao mode of the Live tab: the same store, restricted to rows vilao actually billed, and
 * refreshed by pulsing the bill endpoint (the backend rate-limits the pulse, not the read). */
function useVilaoCalls(active) {
  const search = useValue($search)
  const key = useValue($key)
  const range = useValue($range)
  const outcome = useValue($outcome)
  const profile = useValue($profile)
  const page = useValue($page)
  const day = useValue($day)
  return useQuery({
    queryKey: [...VILAO_KEY, key, search, range, outcome, profile, page, day],
    queryFn: () => api('/calls?' + q({
      per: PER_PAGE, page, billed: 1, pulse: 1, key_id: key, model: search, outcome,
      hours: day ? '' : range, profile, day
    })),
    enabled: active,
    refetchInterval: active ? POLL_MS : false,
    staleTime: 15000
  })
}

/* Which homes are contributing. Without this a profile that never called looks identical to one whose
 * plugin is switched off, and the second is a configuration problem, not a usage fact. */
function useProfiles() {
  return useQuery({ queryKey: PROFILES_KEY, queryFn: () => api('/profiles'), refetchInterval: POLL_MS, staleTime: 30000 })
}

function useRate(hours) {
  const profile = useValue($profile)
  return useQuery({
    queryKey: [...RATE_KEY, hours, profile],
    queryFn: () => api('/rate?' + q({ hours, profile })),
    refetchInterval: POLL_MS,
    staleTime: 20000
  })
}

function useCatalog(search) {
  return useQuery({
    queryKey: [...CATALOG_KEY, search],
    queryFn: () => api('/catalog?' + q({ q: search })),
    refetchInterval: 30 * POLL_MS,
    staleTime: 10 * 60 * 1000
  })
}

function useTopups() {
  return useQuery({ queryKey: TOPUPS_KEY, queryFn: () => api('/topups'), refetchInterval: 6 * POLL_MS, staleTime: 5 * 60 * 1000 })
}

function useKeyDetail(id) {
  return useQuery({
    queryKey: [...DETAIL_KEY, id],
    queryFn: () => api('/keys/detail?' + q({ key_id: id })),
    enabled: !!id,
    staleTime: 60000
  })
}

function useKeys(enabled) {
  return useQuery({
    queryKey: KEYS_KEY,
    queryFn: () => api('/keys'),
    enabled,
    refetchInterval: enabled ? POLL_MS : false,
    staleTime: 60000
  })
}

function keyName(map, id) {
  if (!id) return 'không rõ key'
  const k = map && map[id]
  return k ? (k.name || k.key_prefix || id.slice(0, 8)) : id.slice(0, 8)
}

function useSummary(days, source) {
  const profile = useValue($profile)
  return useQuery({
    queryKey: [...SUMMARY_KEY, days, profile, source || ''],
    queryFn: () => api('/summary?' + q({ days, profile, source })),
    refetchInterval: POLL_MS,
    staleTime: 60000
  })
}

/* Per-model totals from the Hermes log itself. ``source=hook`` is the point: this is the view that covers
 * every provider Hermes can route to, and mixing in the billing feed would hide the free tiers behind
 * rows that only exist because vilao charged for something else. */
function useLog(days) {
  const profile = useValue($profile)
  return useQuery({
    queryKey: [...LOG_KEY, days, profile],
    queryFn: () => api('/log?' + q({ days, profile, source: 'hook' })),
    refetchInterval: POLL_MS,
    staleTime: 60000
  })
}

/* What one conversation cost. Only hook rows carry a session id, so this is a log view by construction. */
function useSessions(days) {
  const profile = useValue($profile)
  return useQuery({
    queryKey: [...SESSIONS_KEY, days, profile],
    queryFn: () => api('/sessions?' + q({ days, profile })),
    refetchInterval: POLL_MS,
    staleTime: 60000
  })
}

const FX_KEY = ['usage', 'fx']
/* The conversion table is read once and kept for the session: the backend holds the real cache (six
 * hours) in the same file provider-usage reads, so a refetch here is a disk read, not an API call.
 * Assigning to module state is deliberate -- every money formatter needs the rate and none of them
 * takes a prop. */
function useFx() {
  const found = useQuery({
    queryKey: FX_KEY,
    queryFn: () => api('/fx'),
    staleTime: 30 * 60 * 1000,
    refetchInterval: 6 * POLL_MS
  })
  useEffect(() => {
    FX = (found.data && found.data.fx) || null
  }, [found.data])
  return found
}

const rateLine = fx => fx && fx.usd_vnd
  ? '1 USD ≈ ' + dong(fx.usd_vnd) + (fx.cny_vnd ? ' · 1 CNY ≈ ' + dong(fx.cny_vnd) : '') +
    ' · nguồn ' + (fx.source || '') + (fx.stale ? ' (giá cũ)' : '')
  : 'Chưa lấy được tỷ giá USD→đ, nên phần ước tính vẫn in bằng đô la.'

function useBalance(enabled) {
  return useQuery({
    queryKey: BALANCE_KEY,
    queryFn: () => api('/balance'),
    enabled,
    refetchInterval: enabled ? POLL_MS : false,
    staleTime: 30000
  })
}

/* A new call is prepended straight into the cache, so the log moves without a refetch.
 * The gateway hands the listener the whole event frame ({type, session_id, payload}) and the row is the
 * payload: reading the frame as if it were the row drops every live call silently, because `frame.ts`
 * is undefined and the guard below then returns early. */
function useCallFeed() {
  useEffect(() => {
    if (!onEvent || !queryClient) return undefined
    return onEvent('plugin.' + PLUGIN_ID + '.call', event => {
      const row = event && event.payload
      if (!row || !row.ts) return
      if ($mode.get() === 'vilao') {
        /* The hook row is Hermes' estimate; the Vilao mode lists what the account was charged, and
         * that only exists after the pulse reads the bill. */
        queryClient.invalidateQueries({ queryKey: VILAO_KEY })
        return
      }
      for (const base of [CALLS_KEY, VILAO_KEY]) {
        const cached = queryClient.getQueryData(base)
        if (!cached || !Array.isArray(cached.calls)) continue
        /* Prepending belongs to the newest page only; shoving a row into page 4 would show a call that
         * is not part of that slice, and the pager's count would disagree with the table. */
        if (cached.page !== 1) continue
        if (cached.calls.some(c => c.ts === row.ts && c.model === row.model)) continue
        queryClient.setQueryData(base, {
          ...cached,
          calls: [row, ...cached.calls].slice(0, cached.per || 100),
          total: (Number(cached.total) || 0) + 1
        })
      }
    })
  }, [])
}

function refreshCaches() {
  if (!queryClient) return
  queryClient.invalidateQueries(SUMMARY_KEY)
  queryClient.invalidateQueries(CALLS_KEY)
  queryClient.invalidateQueries(VILAO_KEY)
  queryClient.invalidateQueries(BALANCE_KEY)
}

/* The backend pulls a few pages per request; the page drives the walk until vilao reports done.
 * No worker thread on our side, so a long backfill never holds an HTTP request open.
 *
 * ``pulse`` is the routine refresh: it stops at the first page that is already known, so keeping the
 * log current costs the new rows rather than a re-read of the whole horizon. ``full`` walks everything
 * vilao still bills for, and a plain walk covers the stored 30 days -- what refills the API-key tags
 * on rows ingested before the bill carried them. */
/* The backend pulls a few pages per request and keeps the cursor; the page drives the walk. No worker
 * thread on our side, so a long backfill never holds an HTTP request open.
 *
 * ``pulse`` is the routine refresh: newest pages only, stopping at the first page already known, so
 * keeping the log current costs the new rows rather than a re-read of the horizon.
 *
 * ``restart`` is sent on the FIRST request of a deep run only. The walk resumes from the saved cursor,
 * and a run that restarted every request would re-read the same newest pages forever and never reach
 * the older charges -- which is how a twelve-month grid stays half empty. */
async function runSync({ days = 30, pulse = false, restart = false } = {}) {
  $error.set(null)
  const steps = pulse ? 4 : (days >= 365 ? 200 : 60)
  for (let step = 0; step < steps; step++) {
    let body = null
    try {
      body = await api('/sync?' + q({ pages: 3, days, pulse: pulse ? 1 : '',
        restart: restart && step === 0 ? 1 : '' }), { method: 'POST' })
    } catch (err) {
      $error.set(String((err && err.message) || err))
      return
    }
    $sync.set(body)
    if (!body || body.started === false) { refreshCaches(); return }
    refreshCaches()
    if (body.done) return
    await new Promise(resolve => setTimeout(resolve, 250))
  }
}

function useFirstRunBackfill(days) {
  const started = useRef(false)
  const summary = useSummary(days)
  useEffect(() => {
    if (started.current || !summary.data) return
    started.current = true
    const rows = Number((summary.data.summary || {}).calls || 0)
    // An empty store has nothing to lose: read the year the calendar draws. A filled one just refreshes.
    runSync(rows === 0 ? { days: 365, restart: true } : { pulse: true })
  }, [summary.data])
  return summary
}

/* ---------- primitives ---------- */

/* The page is painted by whatever skin the app is wearing, so colour comes from the app's own tokens
 * with a neutral fallback. A native <select> was the one place that could not be themed: its popup is
 * drawn by the OS, and over a dark surface it arrived as a white box with the page's light text on it. */
const tk = (name, fallback) => 'var(' + name + ', ' + fallback + ')'
const INK = tk('--ui-text-primary', 'currentColor')
const INK_FAINT = tk('--ui-text-secondary', 'rgba(128,128,128,0.85)')
const HAIRLINE = tk('--ui-stroke-tertiary', 'rgba(128,128,128,0.22)')
const EDGE = tk('--ui-stroke-secondary', 'rgba(128,128,128,0.32)')
const FILL = tk('--ui-control-hover-background', 'rgba(128,128,128,0.14)')
const PANEL = tk('--ui-widget-surface-background', tk('--ui-bg-chrome', 'rgba(128,128,128,0.16)'))
const TRACK = 'rgba(128,128,128,0.20)'
const ACCENT = tk('--ui-accent', 'currentColor')

const gap = { display: 'flex', gap: 8, alignItems: 'center' }
const card = { border: '1px solid ' + HAIRLINE, borderRadius: 8, padding: 12 }
const faint = { color: INK_FAINT, fontSize: 11 }
const mono = { fontVariantNumeric: 'tabular-nums' }
const numCell = { ...mono, textAlign: 'right', whiteSpace: 'nowrap' }
const field = { ...gap, justifyContent: 'space-between', gap: 6, padding: '3px 8px', fontSize: 12,
  borderRadius: 6, border: '1px solid ' + HAIRLINE, background: 'transparent', color: INK, cursor: 'pointer' }

function Stat({ label, value, hint }) {
  return jsx('div', { style: { ...card, minWidth: 130 }, children: jsxs('div', { children: [
    jsx('div', { style: faint, children: label }),
    jsx('div', { style: { fontSize: 19, fontWeight: 600, ...mono, color: INK }, children: value }),
    hint ? jsx('div', { style: faint, children: hint }) : null
  ] }) })
}

/* One option row, reused by the segmented control and the dropdown. */
function Choice({ on, onClick, children, wide }) {
  return jsx('button', {
    type: 'button', onClick, 'aria-pressed': on,
    style: { ...gap, justifyContent: 'space-between', gap: 10, width: wide ? '100%' : 'auto',
      padding: '4px 10px', borderRadius: 6, cursor: 'pointer', fontSize: 12, color: INK,
      background: on ? FILL : 'transparent', border: '1px solid ' + (on ? EDGE : 'transparent'),
      textAlign: 'left' },
    children
  })
}

function Segmented({ options, value, onChange }) {
  return jsx('div', { style: { ...gap, gap: 4 }, children: options.map(o => jsx(Choice, {
    on: value === o.value, onClick: () => onChange(o.value), children: o.label
  }, o.value || 'all')) })
}

/* A themed dropdown that replaces <select>: the trigger and the list are both our own DOM, so both
 * pick up the active skin. Keyboard: Escape closes, Enter/Space picks. */
function Pick({ label, value, options, onChange, width }) {
  const [open, setOpen] = useState(false)
  const box = useRef(null)
  const current = options.find(o => o.value === value) || options[0]
  useEffect(() => {
    if (!open) return undefined
    const away = e => { if (box.current && !box.current.contains(e.target)) setOpen(false) }
    const key = e => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', key)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', key)
    }
  }, [open])
  return jsxs('div', { ref: box, style: { position: 'relative', width: width || undefined }, children: [
    jsxs('button', {
      type: 'button', onClick: () => setOpen(v => !v), 'aria-haspopup': 'listbox', 'aria-expanded': open,
      title: current ? current.title || current.label : '',
      style: { ...field, minWidth: 120, width: '100%' },
      children: [
        jsx('span', { style: { ...faint, marginRight: 6 }, children: label }),
        jsx('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
          children: current ? current.label : '—' }),
        jsx('span', { style: { ...faint, transform: open ? 'rotate(180deg)' : 'none' }, children: '▾' })
      ]
    }),
    open ? jsx('div', {
      role: 'listbox',
      style: { position: 'absolute', zIndex: 30, top: '100%', left: 0, marginTop: 4, minWidth: '100%',
        maxHeight: 280, overflowY: 'auto', padding: 4, borderRadius: 8, background: PANEL,
        border: '1px solid ' + EDGE, boxShadow: '0 10px 28px rgba(0,0,0,0.35)' },
      children: jsxs('div', { style: { display: 'grid', gap: 2 }, children: options.map(o => jsxs('button', {
        type: 'button', role: 'option', 'aria-selected': o.value === value,
        onClick: () => { onChange(o.value); setOpen(false) },
        title: o.title || o.label,
        style: { ...gap, justifyContent: 'space-between', gap: 12, width: '100%', padding: '4px 8px',
          borderRadius: 6, fontSize: 12, color: INK, cursor: 'pointer', border: 'none',
          background: o.value === value ? FILL : 'transparent', textAlign: 'left' },
        children: [
          jsx('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
            children: o.label }, o.value),
          jsx('span', { style: { ...faint, flexShrink: 0 }, children: o.meta || (o.value === value ? '●' : '') },
            o.value + 'm')
        ]
      }, o.value)) })
    }) : null
  ] })
}

function TabBar() {
  const active = useValue($tab)
  return jsx('div', { style: { display: 'flex', gap: 4, borderBottom: '1px solid rgba(128,128,128,0.28)', padding: '8px 0' },
    children: TABS.map(key => jsx('button', {
      type: 'button',
      onClick: () => $tab.set(key),
      style: { ...gap, padding: '4px 12px', borderRadius: 999, cursor: 'pointer', fontSize: 12,
        background: active === key ? 'rgba(128,128,128,0.22)' : 'transparent',
        color: 'inherit', border: '1px solid transparent' },
      children: TAB_LABEL[key]
    }, key)) })
}

function Banner() {
  const err = useValue($error)
  const sync = useValue($sync)
  if (!err && !sync) return null
  if (err) return jsx('div', { style: { ...faint, padding: '6px 0' }, children: 'Lỗi: ' + String(err).slice(0, 180) })
  const label = sync.pending
    ? 'Đang quét hóa đơn vilao ' + (sync.days || 30) + ' ngày… trang ' + (sync.page || 1) +
      ' · ' + (sync.read || 0) + ' dòng đã đọc'
    : 'Đồng bộ: ' + [(sync.read || 0) + ' dòng đọc', (sync.stored || 0) + ' mới', (sync.matched || 0) + ' khớp'].join(', ')
  return jsx('div', { style: { ...faint, padding: '6px 0' }, children: label })
}

/* Label | bar | value on one grid row, so the numbers of every row share a column. */
function Bars({ rows, valueKey, labelKey, format, max, titleKey }) {
  const top = max || Math.max(1, ...rows.map(r => Number(r[valueKey]) || 0))
  return jsx('div', { style: { display: 'grid', gap: 5 }, children: rows.map(r => {
    const value = Number(r[valueKey]) || 0
    return jsxs('div', {
      style: { display: 'grid', gridTemplateColumns: 'minmax(90px, 170px) 1fr auto', gap: 10,
        alignItems: 'center', fontSize: 12 },
      children: [
        jsx('span', { style: { ...mono, color: INK, overflow: 'hidden', textOverflow: 'ellipsis',
          whiteSpace: 'nowrap' }, title: titleKey ? r[titleKey] : String(r[labelKey]),
          children: String(r[labelKey]) }),
        jsx('span', { style: { height: 7, borderRadius: 4, background: TRACK, overflow: 'hidden' },
          children: jsx('span', { style: { display: 'block', height: '100%',
            width: (value / top * 100).toFixed(1) + '%', borderRadius: 4,
            background: ACCENT, opacity: 0.55 } }) }),
        jsx('span', { style: { ...numCell, minWidth: 74 }, children: format(value) })
      ]
    }, String(r[labelKey]))
  }) })
}

/* A line chart in plain SVG: no chart library is bundled with the desktop plugin host, and 30 points of
 * daily totals do not need one. Two series so the two questions people actually ask sit on one axis --
 * how much money, and how much work -- each scaled to its own maximum because dong and token counts
 * never share a sensible scale.
 *
 * Hover reads the point. The x mapping is already pure arithmetic (pad + i * step in viewBox units), so a
 * mouse position converts back to a row index without any measurement of the polyline itself, and both
 * series share that index because they share the rows array. The readout row is always rendered -- empty
 * when nothing is hovered -- so reading a value never changes the chart's height under the cursor. */
function Lines({ rows, series, height = 128 }) {
  const [idx, setIdx] = useState(-1)
  if (!rows || !rows.length) return jsx('div', { style: faint, children: '—' })
  const w = 100, pad = 3
  const step = rows.length > 1 ? (w - pad * 2) / (rows.length - 1) : 0
  const firstRow = rows[0], lastRow = rows[rows.length - 1]
  const hover = idx >= 0 && idx < rows.length ? rows[idx] : null
  /* One handler for both series: the two svgs are the same width and the same x scale, so whichever one
   * the pointer is over addresses the same day. */
  const track = e => {
    const el = e.currentTarget
    if (!el || !el.getBoundingClientRect) return
    const r = el.getBoundingClientRect()
    if (!r.width) return
    const pct = ((e.clientX - r.left) / r.width) * w
    const i = step > 0 ? Math.round((pct - pad) / step) : 0
    setIdx(Math.max(0, Math.min(rows.length - 1, i)))
  }
  const mark = s => {
    if (!hover) return null
    const values = rows.map(r => Number(r[s.key]) || 0)
    const top = Math.max(1, ...values)
    return jsxs('g', { children: [
      jsx('line', { x1: (pad + idx * step).toFixed(2), x2: (pad + idx * step).toFixed(2), y1: 0, y2: 100,
        stroke: s.color || ACCENT, strokeWidth: 0.4, strokeDasharray: '2 2', opacity: 0.55 }, 'v'),
      jsx('circle', { cx: (pad + idx * step).toFixed(2),
        cy: (100 - (values[idx] / top) * 92).toFixed(2), r: 1.6, fill: s.color || ACCENT }, 'c')
    ] })
  }
  return jsxs('div', { style: { display: 'grid', gap: 6 }, children: [
    ...series.map(s => {
      const values = rows.map(r => Number(r[s.key]) || 0)
      const top = Math.max(1, ...values)
      const pts = values.map((v, i) => (pad + i * step).toFixed(2) + ',' +
        (100 - (v / top) * 92).toFixed(2))
      return jsxs('div', { style: { display: 'grid', gap: 2 }, children: [
        jsxs('div', { style: { ...gap, justifyContent: 'space-between' }, children: [
          jsx('span', { style: { ...faint, color: s.color || ACCENT }, children: s.label }, 'l'),
          jsx('span', { style: { ...numCell, fontSize: 11, color: INK },
            children: 'đỉnh ' + s.format(top) }, 'm')
        ] }),
        jsx('svg', { viewBox: '0 0 100 100', preserveAspectRatio: 'none',
          onMouseMove: track, onMouseLeave: () => setIdx(-1),
          style: { width: '100%', height, display: 'block', overflow: 'visible', cursor: 'crosshair' },
          children: jsxs('g', { children: [
            jsx('polyline', { fill: 'none', stroke: s.color || ACCENT, strokeWidth: 0.8,
              strokeLinejoin: 'round', strokeLinecap: 'round', points: pts.join(' ') }, 'p'),
            mark(s)
          ] }) }, s.key)
      ] }, s.key)
    }),
    jsx('div', { style: { ...gap, justifyContent: 'flex-start', fontSize: 11, minHeight: 16,
      lineHeight: '16px', color: hover ? INK : INK_FAINT, fontVariantNumeric: 'tabular-nums' },
      children: hover
        ? hover.day + ' · ' + series.map(s => s.label + ' ' + s.format(Number(hover[s.key]) || 0)).join(' · ')
        : 'rê chuột vào đồ thị để đọc từng ngày' }, 'read'),
    jsxs('div', { style: { ...gap, justifyContent: 'space-between', ...faint, fontSize: 10 }, children: [
      jsx('span', { children: String(rows.length) + ' ngày · từ '
        + String((firstRow && firstRow.day) || '—') }, 'a'),
      jsx('span', { children: 'tới ' + String((lastRow && lastRow.day) || '—')
        + ' · ' + series[0].label + ' ' + series[0].format(Number(lastRow && lastRow[series[0].key]) || 0) }, 'b')
    ] }, 'axis')
  ] })
}

/* GitHub's contribution graph, drawn the same way: seven weekday rows, one square per day, and enough
 * week columns to cover twelve months. Cells stay square rather than stretching to the pane, and the
 * grid scrolls sideways when the window is narrower than the year -- which it always is.
 *
 * Intensity is that day's request count on a quantile scale, and the tooltip leads with the billed dong
 * plus the models that day's money went to, so the day x model detail this replaced is still one hover
 * away. Days the store never ingested look exactly like days with no calls, so the card states the window
 * it actually holds. */
const CELL = 13
const CELL_GAP = 3
const GUTTER = 30

function dayKey(date) {
  const pad = n => String(n).padStart(2, '0')
  return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate())
}

/* One calendar year as whole weeks starting Monday: Jan 1 is pulled back to the Monday before it and
 * Dec 31 pushed to the Sunday after, so every column is a week and T1 always comes first. The old view
 * was a rolling 365 days, which started wherever the year happened to be and read as T10 → T10. */
function weekColumns(year) {
  const start = new Date(year, 0, 1)
  start.setDate(start.getDate() - ((start.getDay() + 6) % 7))
  const end = new Date(year, 11, 31)
  end.setDate(end.getDate() + (6 - ((end.getDay() + 6) % 7)))
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const cols = []
  for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
    const row = (d.getDay() + 6) % 7
    if (row === 0) cols.push([])
    cols[cols.length - 1].push(dayKey(d))
  }
  return { cols: cols.map(c => { while (c.length < 7) c.push(null); return c }), today: dayKey(today) }
}

const MONTH_LABEL = m => 'T' + m

function Calendar({ matrix, bill }) {
  const picked = useValue($day)
  const byDay = useMemo(() => {
    const acc = new Map()
    for (const r of matrix) {
      const item = acc.get(r.day) || { calls: 0, dong: 0, models: [] }
      item.calls += Number(r.calls) || 0
      item.dong += Number(r.dong) || 0
      item.models.push([shortModel(r.model_key), Number(r.dong) || 0])
      acc.set(r.day, item)
    }
    for (const v of acc.values()) v.models.sort((a, b) => b[1] - a[1])
    return acc
  }, [matrix])
  const year = new Date().getFullYear()
  const { cols: weeks, today } = useMemo(() => weekColumns(year), [year])
  const firstDay = bill && bill.first_ts ? dayKey(new Date(bill.first_ts * 1000)) : '0000-00-00'
  const filled = Array.from(byDay.values()).filter(v => v.calls > 0).length
  /* Shade by how many requests a day carried rather than by what it cost. Two reasons, both about reading
   * the page on a busy account: a cost ramp spends its whole life at the darkest square -- every working
   * day clears any fixed ceiling -- and the number that answers "was I at the keyboard that day" is the
   * request count. The money did not move: it leads the tooltip, where it is still the first thing shown.
   *
   * The four cut points are quantiles of the days that carry traffic, so "dark" means one of this year's
   * busiest days instead of a constant nobody calibrated. Re-derived per render on purpose: an absolute
   * threshold would have to be re-tuned every time the account's volume changes, and a relative scale
   * always keeps the top band at the same ~20% of days no matter how big the account grows. */
  const dayCalls = Array.from(byDay.values(), v => v.calls).filter(c => c > 0).sort((a, b) => a - b)
  const cut = q => (dayCalls.length
    ? dayCalls[Math.min(dayCalls.length - 1, Math.floor(dayCalls.length * q))] : Infinity)
  const LEVELS = [0.2, 0.4, 0.62, 0.84, 1]
  const q20 = cut(0.2), q40 = cut(0.4), q60 = cut(0.6), q80 = cut(0.8)
  const levelOf = c => (c > 0
    ? (c > q80 ? 4 : c > q60 ? 3 : c > q40 ? 2 : c > q20 ? 1 : 0) : -1)
  let shownMonth = -1
  /* The first column usually begins in the December before Jan 1 (weeks are whole), and the last one can
   * finish in the January after Dec 31. Labelling those cells would print T12 before T1, so a column is
   * labelled from the first day inside the year it actually belongs to. */
  const months = weeks.map(w => {
    const inYear = w.filter(d => d && String(d).slice(0, 4) === String(year))
    const m = inYear.length ? Number(String(inYear[0]).slice(5, 7)) : 0
    const label = m && m !== shownMonth ? MONTH_LABEL(m) : ''
    if (label) shownMonth = m
    return label
  })
  const cols = `repeat(${weeks.length}, ${CELL}px)`
  const cellStyle = o => ({ width: CELL, height: CELL, borderRadius: 2, background: 'currentColor',
    opacity: o })
  return jsxs('div', { style: { position: 'relative', paddingLeft: GUTTER }, children: [
    jsx('div', { style: { ...faint, position: 'absolute', left: 0, top: 16, display: 'grid',
      gridTemplateRows: `repeat(7, ${CELL + CELL_GAP}px)`, gap: CELL_GAP, fontSize: 9, width: GUTTER,
      fontVariantNumeric: 'tabular-nums' },
      children: ['T2', '', 'T4', '', 'T6', '', ''].map((w, i) =>
        jsx('span', { style: { height: CELL, lineHeight: CELL + 'px' }, children: w }, 'w' + i)) }),
    jsx('div', { style: { overflowX: 'auto', paddingBottom: 4 }, children: jsxs('div', { children: [
      jsx('div', { style: { display: 'grid', gridTemplateColumns: cols, gap: CELL_GAP, height: 14,
        marginBottom: 2 }, children: months.map((m, i) =>
        jsx('span', { style: { fontSize: 9, color: INK_FAINT, whiteSpace: 'nowrap' }, children: m },
          'm' + i)) }),
      jsx('div', { style: { display: 'grid', gridTemplateColumns: cols, gridTemplateRows:
        `repeat(7, ${CELL}px)`, gridAutoFlow: 'column', gap: CELL_GAP, width:
        weeks.length * (CELL + CELL_GAP) },
        children: weeks.flatMap((w, i) => w.map((day, row) => {
          if (!day) return jsx('div', { style: cellStyle(0) }, 'e' + i + row)
          const item = byDay.get(day)
          const value = item ? item.dong : 0
          const calls = item ? item.calls : 0
          const future = day > today
          const lv = levelOf(calls)
          const tip = new Date(day + 'T12:00:00').toLocaleDateString('vi-VN')
          const chosen = day === picked
          /* A square is a question and an answer at once: it shows the day's spend and, on click, opens
           * the log filtered to exactly that day. The empty ones stay clickable too -- "what happened on
           * a day I spent nothing" is a real question, and an unresponsive cell reads as a broken grid. */
          return jsx('button', {
            type: 'button',
            onClick: () => { $day.set(chosen ? '' : day); $page.set(1); if (!chosen) $tab.set('live') },
            title: (calls > 0
              ? [tip + ' · ' + calls + ' lượt · ' + dong(value),
                 item.models.slice(0, 3).map(x => x[0] + ' ' + dong(x[1])).join(', ')].join('\n')
              : future ? tip + ' · chưa tới'
              : day < firstDay ? tip + ' · sớm hơn dữ liệu hóa đơn sớm nhất ('
                + firstDay.slice(8) + '/' + firstDay.slice(5, 7) + ')'
              : tip + ' · không có lượt gọi nào') + '\nbấm để xem log ngày này',
            style: { ...cellStyle(lv >= 0 ? LEVELS[lv]
              : future ? 0.02 : day < firstDay ? 0.04 : 0.07),
              border: chosen ? '1px solid ' + ACCENT : '1px solid transparent', padding: 0,
              cursor: future ? 'default' : 'pointer', outline: 'none' }
          }, day)
        })) })
    ] }) }),
    jsxs('div', { style: { ...gap, justifyContent: 'space-between', marginTop: 8 }, children: [
      jsx('span', { style: faint, children: bill && bill.first_ts
        ? 'Năm ' + year + ' (T1→T12) · store giữ hóa đơn từ ' + clockOf(bill.first_ts).slice(0, 10)
          + ' · ' + filled + ' ngày có lượt gọi tới hôm nay · bấm một ô để xem log ngày đó'
        : 'Chưa có hóa đơn trong store.' }),
      jsxs('div', { style: gap, children: [
        jsx('span', { style: faint, children: 'ít' }),
        LEVELS.map(o => jsx('span', { style: { width: 11, height: 11,
          borderRadius: 2, flexShrink: 0, background: 'currentColor', opacity: o } }, 'l' + o)),
        jsx('span', { style: faint, children: 'nhiều (lượt/ngày)' })
      ] })
    ] })
  ] })
}

/* ---------- tabs ---------- */

const RANGES = [{ value: '', label: 'toàn bộ' }, { value: '24', label: '24 giờ' },
  { value: '168', label: '7 ngày' }, { value: '720', label: '30 ngày' }]
const OUTCOMES = [{ value: '', label: 'mọi kết quả' }, { value: 'ok', label: 'thành công' },
  { value: 'error', label: 'lỗi' }]
const STATE = { matched: 'đã ghép hai nguồn', local_only: 'chỉ log Hermes', bill_only: 'chỉ hóa đơn vilao' }

/* Two feeds, one table. 'hermes' is every call the agent logged through any provider; 'vilao' is the
 * account's own bill, which also carries calls Hermes never saw (other machines, other keys). */
function homeOptions(rows) {
  /* Every home the store has calls for, plus the billing bucket: a vilao charge is an account-level
   * event, so it carries no home and would otherwise be unreachable once the filter exists. */
  const out = [{ value: '', label: 'mọi home' }]
  for (const h of rows || []) {
    if (!h.profile) {
      out.push({ value: NO_HOME, label: 'hóa đơn · không rõ home',
        meta: tok(h.calls) + ' lượt · ' + dong(h.dong),
        title: 'Những dòng vilao tính tiền nhưng log Hermes không có lượt tương ứng — '
          + 'charges thuộc về account, không thuộc về home nào cả.' })
      continue
    }
    out.push({ value: h.profile, label: h.profile,
      meta: tok(h.calls) + ' lượt · ' + dong(h.dong),
      title: h.profile + ' · ' + num(h.last_ts ? longOf(h.last_ts) : '') })
  }
  return out
}

/* Page numbers around the current page, so a 400-page table still fits in one row of buttons. */
function pageList(page, pages) {
  const out = []
  const push = p => { if (p >= 1 && p <= pages && out[out.length - 1] !== p) out.push(p) }
  push(1)
  for (let p = page - 2; p <= page + 2; p++) push(p)
  push(pages)
  return out.filter((p, i, a) => i === 0 || p !== a[i - 1])
}

/* The Pager of whichever table is on screen claims this slot, so one keyboard handler can page through
 * the Live log or the offer catalogue without either tab plumbing its page count up to the shell. */
const PAGER = { go: null }

function Pager({ data, onPage }) {
  const total = Number(data && data.total) || 0
  const pages = Number(data && data.pages) || 1
  const page = Number(data && data.page) || 1
  useEffect(() => {
    if (!total) return undefined
    PAGER.go = d => {
      const next = Math.min(pages, Math.max(1, page + d))
      if (next !== page) onPage(next)
    }
    return () => { PAGER.go = null }
  }, [page, pages, total])
  if (!total) return null
  const btn = { padding: '2px 8px', fontSize: 11, borderRadius: 6, cursor: 'pointer',
    border: '1px solid ' + HAIRLINE, background: 'transparent', color: INK }
  const off = { ...btn, opacity: 0.45, cursor: 'default' }
  const go = p => { if (p >= 1 && p <= pages) { onPage(p); $openRow.set(null) } }
  return jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 6, padding: '7px 10px',
      borderTop: '1px solid ' + EDGE, flexWrap: 'wrap' }, children: [
    jsx('span', { style: { ...faint, fontSize: 11 },
      children: 'trang ' + page + '/' + pages + ' · ' + num(total) + ' dòng khớp · ' + (data.per || PER_PAGE) + '/trang' }, 'info'),
    jsx('button', { type: 'button', style: page > 1 ? btn : off, onClick: () => go(1), children: '«' }, 'first'),
    jsx('button', { type: 'button', style: page > 1 ? btn : off, onClick: () => go(page - 1), children: '‹ trước' }, 'prev'),
    ...pageList(page, pages).map(p => jsx('button', {
      type: 'button',
      style: p === page ? { ...btn, borderColor: ACCENT, color: ACCENT, fontWeight: 600 } : btn,
      onClick: () => go(p), children: String(p) }, p)),
    jsx('button', { type: 'button', style: page < pages ? btn : off, onClick: () => go(page + 1), children: 'sau ›' }, 'next'),
    jsx('button', { type: 'button', style: page < pages ? btn : off, onClick: () => go(pages), children: '»' }, 'last')
  ] })
}

function LiveTab() {
  const mode = useValue($mode)
  const vilao = mode === 'vilao'
  const provider = useValue($provider)
  const key = useValue($key)
  const range = useValue($range)
  const outcome = useValue($outcome)
  const search = useValue($search)
  const mainOnly = useValue($mainOnly)
  const profile = useValue($profile)
  const day = useValue($day)
  const hermes = useCalls(!vilao)
  const billed = useVilaoCalls(vilao)
  const keys = useKeys(vilao)
  const open = useValue($openRow)
  const query = vilao ? billed : hermes
  const rows = (query.data && query.data.calls) || []
  const byId = useMemo(() => {
    const map = {}
    for (const k of (keys.data && keys.data.keys) || []) map[k.id] = k
    return map
  }, [keys.data])
  const profiles = useProfiles()
  const homeRows = (profiles.data && profiles.data.profiles) || []
  const providers = useMemo(() => Array.from(new Set(rows.map(r => r.provider).filter(Boolean))).sort(), [rows])
  const hotDong = useMemo(() => quantile(rows, 'bill_cost_vnd', 0.9), [rows])
  const slowMs = useMemo(() => quantile(rows, 'duration_ms', 0.85), [rows])
  const keyOptions = useMemo(() => {
    const used = Array.from(new Set(rows.map(r => r.bill_key_id).filter(Boolean)))
    const seen = new Set()
    const opts = [{ value: '', label: 'mọi key' }]
    for (const k of (keys.data && keys.data.keys) || []) {
      if (seen.has(k.id)) continue
      seen.add(k.id)
      opts.push({ value: k.id, label: k.name || k.key_prefix, meta: (k.active ? '' : 'ngưng · ') + dong(Number(k.total_spent) || 0),
        title: k.key_prefix + ' · ' + num(k.total_requests) + ' lượt' })
    }
    for (const id of used) if (!seen.has(id)) opts.push({ value: id, label: id.slice(0, 8), meta: 'không còn liệt kê' })
    return opts
  }, [keys.data, rows])
  return jsxs('div', { style: { display: 'grid', gap: 10 }, children: [
    jsxs('div', { style: { ...gap, flexWrap: 'wrap', rowGap: 8 }, children: [
      jsx(Segmented, { options: [
        { value: 'hermes', label: 'Hermes gọi' }, { value: 'vilao', label: 'Hóa đơn Vilao' }
      ], value: mode, onChange: v => { $mode.set(v); $page.set(1) } }),
      vilao
        ? jsx(Pick, { label: 'key', value: key, options: keyOptions,
            onChange: v => { $key.set(v); $page.set(1) }, width: 230 })
        : jsx(Pick, { label: 'provider', value: provider,
            options: [{ value: '', label: 'mọi provider' }, ...providers.map(p => ({ value: p, label: p }))],
            onChange: v => { $provider.set(v); $page.set(1) }, width: 190 }),
      jsx(Pick, { label: 'home', value: profile, width: 210,
          options: homeOptions(homeRows),
          onChange: v => { $profile.set(v); $page.set(1) } }),
      jsx(Pick, { label: 'khung giờ', value: range, options: RANGES,
        onChange: v => { $range.set(v); $page.set(1) }, width: 168 }),
      /* The chip exists because a day filter silently overrides the range: without a visible record of
       * it, "why is the log showing October 3rd" has no answer on screen. */
      day ? jsxs('button', {
        type: 'button', onClick: () => { $day.set(''); $page.set(1) },
        title: 'đang lọc đúng một ngày nên khung giờ không áp dụng — bấm để bỏ',
        style: { ...field, border: '1px solid ' + ACCENT, color: ACCENT },
        children: [jsx('span', { children: 'ngày ' + day.slice(8) + '/' + day.slice(5, 7) }),
          jsx('span', { children: ' ✕' })]
      }, 'day') : null,
      jsx(Pick, { label: 'kết quả', value: outcome, options: OUTCOMES,
        onChange: v => { $outcome.set(v); $page.set(1) }, width: 168 }),
      jsx('input', { style: { ...field, minWidth: 170, cursor: 'text' }, placeholder: 'lọc model…',
        'data-usage-search': 'live', value: search,
        onChange: e => { $search.set(e.target.value); $page.set(1) } }),
      vilao ? null : jsxs('label', { style: { ...gap, fontSize: 12, cursor: 'pointer', color: INK }, children: [
        jsx('input', { type: 'checkbox', checked: mainOnly,
          onChange: e => { $mainOnly.set(e.target.checked); $page.set(1) } }),
        'chỉ lượt gọi chính'
      ] }),
      jsx('button', { type: 'button', style: { ...field, border: '1px solid ' + HAIRLINE },
        title: 'Đọc thêm hóa đơn cho tới trang đầu tiên đã có sẵn trong store.',
        onClick: () => runSync({ pulse: true }), children: 'Đồng bộ hóa đơn' }),
      jsx('button', { type: 'button', style: { ...field, border: '1px solid ' + HAIRLINE },
        title: 'Quét lại 30 ngày hóa đơn: nạp dòng mới, ghép cặp với log Hermes và điền API key cho những dòng cũ.',
        onClick: () => runSync({ days: 30, restart: true }), children: 'Quét lại 30 ngày' })
    ] }),
    vilao ? jsx('div', { style: faint, children: rows.length
      ? 'Hóa đơn vilao đọc bằng PAT; bộ lọc áp lên ' + num((query.data || {}).total || 0)
        + ' dòng khớp trong số đã cập nhật về máy.'
      : 'Chưa có dòng hóa đơn nào khớp bộ lọc trong số đã cập nhật. Bấm “Quét lại 30 ngày” để đọc sâu hơn.' }) : null,
    query.isError ? jsx('div', { style: faint, children: 'Không đọc được log: ' + String(query.error) }) : null,
    rows.length ? jsxs('div', { style: { ...card, padding: 0, overflowX: 'auto' }, children: [
      jsx('div', { style: { ...LOG_GRID, padding: '4px 10px', minWidth: 900 },
        children: LOG_CELLS.map((c, j) => jsx('span', { style: { ...faint, fontSize: 10,
          textTransform: 'uppercase', letterSpacing: '0.06em', textAlign: j > 3 ? 'right' : 'left' },
          children: c }, c)) }),
      ...rows.map((row, i) => jsx(CallRow, { row, i, byId, vilao,
        hotDong, slowMs }, row.id || i)),
      jsx(LogTotals, { rows, data: query.data }),
      jsx(Pager, { data: query.data, onPage: p => $page.set(p) })
    ] }) : jsx('div', { style: { ...card, ...faint }, children: 'Chưa có lượt gọi nào trong store. Lần mở tab này sẽ quét ngược hóa đơn vilao; log tại máy đầy dần khi bạn chạy agent.' }),
    open ? jsx(RowDetail, { row: open, byId }) : null
  ] })
}

/* One grid definition, shared by the header and the rows, so every number sits in its own column.
 * Tokens get their own columns because "how much did I burn" is only half the question: the other half
 * is how much context went through the cache, which is what actually drives the dong. */
/* The store is shared by all five homes, so a row without its profile is a row that cannot be
 * attributed -- one key column is not enough once a second machine starts calling too. */
const LOG_GRID = { display: 'grid', gridTemplateColumns: '78px 68px 74px minmax(110px, 1fr) 92px 88px 62px 72px 50px 82px',
  gap: 8, alignItems: 'center' }
const LOG_CELLS = ['giờ', 'ngày', 'profile', 'model', 'key / provider', 'vào → ra', 'cache', 'tổng token', 'delay', '₫']

/* Colour follows the content, not a fixed style: a call is only flagged against what the loaded window
 * actually looks like, so "expensive" means expensive today, not expensive against a constant. */
function quantile(rows, key, q) {
  const v = rows.map(r => Number(r[key]) || 0).filter(x => x > 0).sort((a, b) => a - b)
  return v.length ? v[Math.min(v.length - 1, Math.floor(v.length * q))] : Infinity
}

const HOT = 'text-amber-600 dark:text-amber-300'

function CallRow({ row, i, byId, vilao, hotDong, slowMs }) {
  const src = vilao ? keyName(byId, row.bill_key_id) : (row.provider || '?')
  const failed = row.finish_reason === 'error'
  const cost = Number(row.bill_cost_vnd) || 0
  const total = Number(row.tok_total) || 0
  const cache = Number(row.tok_cache_read) || 0
  const slow = row.duration_ms && row.duration_ms >= slowMs
  return jsxs('div', {
    onClick: () => $openRow.set(row),
    title: longOf(row.ts) + ' · ' + (row.model || '?') + (failed ? ' · FAILED' : ''),
    style: { ...LOG_GRID, padding: '5px 10px', fontSize: 12, color: INK, cursor: 'pointer',
      borderTop: '1px solid ' + HAIRLINE, background: i % 2 ? 'transparent' : 'rgba(128,128,128,0.04)' },
    children: [
      jsxs('span', { style: { ...mono, fontSize: 11, color: INK_FAINT }, children: [
        String(clockOf(row.ts)),
        Date.now() / 1000 - Number(row.ts) < 90 ? jsx('span', { className: HOT, style: { marginLeft: 5,
          fontSize: 9 }, children: '●' }) : null
      ] }),
      jsx('span', { style: { ...mono, fontSize: 11, color: INK_FAINT }, children: dateOf(row.ts) }),
      jsx('span', { style: { ...faint, fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis',
          whiteSpace: 'nowrap' },
        title: row.profile ? 'home: ' + row.profile
          : 'Dòng hóa đơn vilao — account-level charge, log Hermes không có lượt tương ứng nên không home nào gọi cả',
        children: row.profile || 'hóa đơn' }),
      jsxs('span', { style: { ...mono, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis',
          whiteSpace: 'nowrap' }, title: row.model, children: [
        /* Clicking the name filters the log to that model -- the row's own click opens the detail, so
         * the two actions have to stay separate or one of them is unreachable. */
        jsx('button', {
          type: 'button', title: 'chỉ hiện model này',
          onClick: e => { e.stopPropagation(); $search.set(row.model_key || row.model || ''); $page.set(1) },
          style: { background: 'transparent', border: 'none', padding: 0, margin: 0, font: 'inherit',
            color: 'inherit', cursor: 'pointer', maxWidth: '100%', overflow: 'hidden',
            textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'left' },
          children: String(shortModel(row.model)) }),
        row.task ? jsx('span', { style: { ...faint, marginLeft: 6, fontSize: 10 },
          children: 'aux:' + row.task }) : null,
        failed ? jsx('span', { className: HOT, style: { marginLeft: 6, fontSize: 10 },
          children: 'lỗi' }) : null
      ] }),
      jsx('span', { style: { ...faint, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
        title: src, children: src }),
      jsxs('span', { style: { ...numCell, fontSize: 11 }, children: [
        String(tok(row.tok_in)), ' → ', String(tok(row.tok_out))
      ] }),
      jsx('span', { style: { ...numCell, fontSize: 11, ...(total && cache / total > 0.5
        ? { color: ACCENT } : {}) }, title: total ? 'cache = ' + pct(cache / total * 100) + ' của token' : '',
        children: String(tok(cache)) }),
      jsx('span', { style: { ...numCell, fontWeight: 600 }, children: String(tok(total)) }),
      jsx('span', { className: slow ? HOT : undefined, style: numCell,
        children: row.duration_ms ? (row.duration_ms / 1000).toFixed(1) + 's' : '—' }),
      jsxs('span', { className: cost >= hotDong ? HOT : undefined, style: numCell, children: [
        String(money(row)),
        row.match_state === 'local_only' ? jsx('span', { style: { ...faint, fontWeight: 400, fontSize: 10 },
          children: ' est' }) : null
      ] })
    ]
  })
}

/* A totals strip, so the window you are looking at answers "bao nhiêu" without scrolling to a card.
 * Cache share is the number that actually explains the dong, so it gets its own slot. */
function LogTotals({ rows, data }) {
  const s = rows.reduce((a, r) => {
    a.tokIn += Number(r.tok_in) || 0
    a.tokOut += Number(r.tok_out) || 0
    a.cache += Number(r.tok_cache_read) || 0
    a.total += Number(r.tok_total) || 0
    a.dong += Number(r.bill_cost_vnd) || 0
    a.usd += Number(r.est_cost_usd) || 0
    a.ms += Number(r.duration_ms) || 0
    if (r.profile) a.homes[r.profile] = 1
    a.n += 1
    return a
  }, { tokIn: 0, tokOut: 0, cache: 0, total: 0, dong: 0, usd: 0, ms: 0, n: 0, homes: {} })
  if (!s.n) return null
  const cell = (k, v, strong) => jsx('span', { style: { ...numCell, fontSize: 11,
    fontWeight: strong ? 600 : 400, color: strong ? INK : INK_FAINT }, children: v }, k)
  return jsx('div', { style: { ...LOG_GRID, padding: '6px 10px', borderTop: '1px solid ' + EDGE,
    background: 'rgba(128,128,128,0.06)' }, children: [
    jsx('span', { style: { ...faint, fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em' },
      children: 'tổng trang này' }, 'l'),
    jsx('span', { style: { ...faint, fontSize: 10 },
      children: s.n + '/' + num((data && data.per) || PER_PAGE) + ' dòng' }, 'n'),
    jsx('span', { style: { ...faint, fontSize: 10 },
      children: Object.keys(s.homes).length + ' home' }, 'h'),
    jsx('span', { style: { ...faint, fontSize: 10 },
      children: 'bộ lọc khớp ' + num(((data && data.total) || s.n)) + ' dòng' }, 'w'),
    cell(pct(s.total ? s.cache / s.total * 100 : 0) + ' cache', false),
    cell(tok(s.tokIn), false),
    cell(tok(s.tokOut), false),
    cell(tok(s.total), true),
    cell(s.n ? (s.ms / s.n / 1000).toFixed(1) + 's' : '—', false),
    cell(s.dong ? dong(s.dong) : est(s.usd), true)
  ].filter(Boolean) })
}

function RowDetail({ row, byId }) {
  const fields = [
    ['Thời điểm', longOf(row.ts)],
    ['Provider / model', (row.provider || '?') + ' · ' + (row.model || '?')],
    ['Vilao API key', row.bill_key_id ? keyName(byId, row.bill_key_id) : '—'],
    ['api_mode', row.api_mode || '—'],
    ['Session', row.session_id || '—'],
    ['Nền tảng', row.platform || '—'],
    ['Loại', row.task ? 'auxiliary · ' + row.task : 'chính'],
    ['Tokens vào/ra', num(row.tok_in) + ' / ' + num(row.tok_out)],
    ['Cache đọc/ghi', num(row.tok_cache_read) + ' / ' + num(row.tok_cache_write)],
    ['Reasoning', num(row.tok_reasoning)],
    ['Độ trễ / TTFT', (row.duration_ms ? (row.duration_ms / 1000).toFixed(2) + 's' : '—') + ' / ' + (row.ttft_ms ? (row.ttft_ms / 1000).toFixed(2) + 's' : '—')],
    ['Kết quả', row.finish_reason === 'error' ? 'lỗi' : (row.finish_reason || '—')],
    ['Context', row.context_length ? num(row.context_length) : '—'],
    ['Hermes ước tính', est(row.est_cost_usd), estTitle(row.est_cost_usd)],
    ['Vilao hóa đơn', row.bill_cost_vnd != null ? dong(row.bill_cost_vnd) : '—'],
    ['Khớp hóa đơn', STATE[row.match_state] || row.match_state]
  ]
  return jsx('div', { style: { ...card, position: 'relative' }, children: jsxs('div', { children: [
    jsxs('div', { style: { ...gap, justifyContent: 'space-between' }, children: [
      jsx('strong', { children: 'Chi tiết lượt gọi' }),
      jsx(Choice, { on: false, onClick: () => $openRow.set(null), children: 'Đóng' })
    ] }),
    jsx('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 4, marginTop: 8 },
      children: fields.map(([k, v, tip]) => jsxs('div', { style: { ...gap, justifyContent: 'space-between', fontSize: 12 }, children: [
        jsx('span', { style: faint, children: k }),
        jsx('span', { style: { ...mono, color: INK, textAlign: 'right', minWidth: 0,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, title: tip || String(v),
          children: String(v) })
      ] }, k)) })
  ] }) })
}

/* A twelve-month walk is a few hundred requests, so it can be paused by closing the page and picked up
 * again: the cursor lives in the store. Only a walk that has nothing pending restarts from page 1. */
function DeepScanButton({ sync }) {
  const paused = Number((sync || {}).page || 1) > 1
  return jsx('button', {
    type: 'button', style: { ...field, border: '1px solid ' + HAIRLINE },
    title: paused
      ? 'Quét tiếp từ trang ' + sync.page + ' (đang dở). Store chỉ tô được những ngày nó đang giữ.'
      : 'Đọc hóa đơn vilao ngược về 12 tháng (~41.000 dòng, tính bằng phút).',
    onClick: () => runSync({ days: 365, restart: !paused }),
    children: paused ? 'Quét tiếp 12 tháng' : 'Quét 12 tháng'
  })
}

function TrendTab() {
  const [days, setDays] = useState(30)
  const summary = useSummary(days)
  const year = useSummary(365)
  const data = (summary.data && summary.data.summary) || {}
  const byDate = data.days_by_date || []
  const dongRows = byDate.filter(r => Number(r.dong) > 0)
  const burn = Number(data.dong_per_day) || 0
  const tot = byDate.reduce((a, r) => {
    for (const k of ['tokens', 'tok_in', 'tok_out', 'tok_cache_read']) a[k] = (a[k] || 0) + (Number(r[k]) || 0)
    return a
  }, {})
  return jsxs('div', { style: { display: 'grid', gap: 12 }, children: [
    jsxs('div', { style: gap, children: [7, 30, 90, 365].map(n => jsx('button', {
      type: 'button', onClick: () => setDays(n),
      style: { fontSize: 12, padding: '2px 10px', borderRadius: 999, cursor: 'pointer',
        background: days === n ? 'rgba(128,128,128,0.22)' : 'transparent', color: 'inherit', border: '1px solid transparent' },
      children: n + ' ngày'
    }, n)) }),
    jsxs('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' }, children: [
      jsx(Stat, { label: 'Lượt gọi', value: num(data.calls) }),
      jsx(Stat, { label: 'Token', value: tok(data.tokens),
        hint: 'vào ' + tok(tot.tok_in) + ' · ra ' + tok(tot.tok_out) + ' · cache ' + tok(tot.tok_cache_read) }),
      jsx(Stat, { label: 'Tiền theo hóa đơn', value: dong(data.dong), hint: 'tiền thật đã trừ vào ví' }),
      jsx(Stat, { label: 'Hermes ước tính', value: est(data.usd_estimated), hint: 'bảng giá nội bộ, quy đổi ra đ' }),
      jsx(Stat, { label: 'Trung bình/ngày', value: dong(burn), hint: burn > 0 ? '≈ ' + dong(burn * 30) + '/tháng' : 'chưa có chi tiêu' })
    ] }),
    burn > 0 && data.dong > 0 ? jsxs('div', { style: faint, children: [
      'Chi tiêu ', dong(burn), '/ngày trong ', Math.round(data.dong / burn), ' ngày gần đây có hoạt động.'
    ] }) : null,
    jsx('div', { style: card, children: jsxs('div', { children: [
      jsxs('div', { style: { ...gap, justifyContent: 'space-between', marginBottom: 8 }, children: [
        jsx('div', { style: faint, children: 'Năm ' + new Date().getFullYear() + ' · T1→T12, mỗi ô vuông là một ngày, đậm theo tiền đã bị trừ' }),
        jsx(DeepScanButton, { sync: (year.data || {}).sync })
      ] }),
      jsx(Calendar, { matrix: (year.data || {}).matrix || [], bill: (year.data || {}).bill })
    ] }) }),
    jsx('div', { style: card, children: jsxs('div', { children: [
      jsx('div', { style: { ...faint, marginBottom: 6 }, children: 'Usage mỗi ngày · ' + days + ' ngày gần nhất' }),
      byDate.length ? jsx(Lines, { rows: byDate, series: [
        { key: 'dong', label: 'tiền bị trừ (đ)', format: dong },
        { key: 'tokens', label: 'token', format: tok, color: 'var(--ui-success, #3fb27f)' }
      ] }) : jsx('div', { style: faint, children: 'Chưa có ngày nào trong cửa sổ này.' })
    ] }) }),
    jsxs('div', { style: card, children: [
      jsx('div', { style: { ...faint, marginBottom: 8 }, children: 'Tiền mỗi ngày (đ, chỉ ngày có chi tiêu)' }),
      dongRows.length ? jsx(Bars, { rows: dongRows.slice(-14), valueKey: 'dong', labelKey: 'day', format: dong })
        : jsx('div', { style: faint, children: 'Chưa có dòng nào khớp hóa đơn.' })
    ] }),
    jsxs('div', { style: card, children: [
      jsx('div', { style: { ...faint, marginBottom: 8 }, children: 'Độ trễ trung bình mỗi ngày (ms)' }),
      byDate.length ? jsx(Bars, { rows: byDate.slice(-14), valueKey: 'avg_ms', labelKey: 'day', format: v => Math.round(v) + 'ms' })
        : jsx('div', { style: faint, children: '—' })
    ] })
  ] })
}

/* ---------- shared table primitives ---------- */

/* A cell value for CSV: whatever the column would rather export than show. Render functions return
 * vnodes for links and tinted cells, and str() of a vnode is "[object Object]" -- so a column that
 * renders anything but text names its raw accessor. Falling back to the sort key is deliberate: it is
 * already the number behind the formatted string. */
function cellValue(c, r) {
  if (c.raw) return c.raw(r)
  const v = c.render(r)
  return typeof v === 'string' || typeof v === 'number' ? v : (c.sort ? c.sort(r) : '')
}

function csvOf(cols, rows) {
  const esc = v => {
    const s = String(v == null ? '' : v)
    return /[",\n;]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s
  }
  return [cols.map(c => esc(c.label)).join(',')]
    .concat(rows.map(r => cols.map(c => esc(cellValue(c, r))).join(','))).join('\r\n')
}

/* Export writes through the host's download when there is one and through a blob link otherwise.
 * The page is a plugin route with no server side, so "export" is a client-side file, and the only
 * rows it can offer are the ones already loaded -- the title says so rather than implying "all". */
function ExportButton({ cols, rows, name, hint }) {
  const save = () => {
    const body = csvOf(cols, rows)
    const fname = (name || 'usage') + '-' + new Date().toISOString().slice(0, 10) + '.csv'
    try {
      const blob = new Blob(['' + body], { type: 'text/csv;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = fname
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 4000)
    } catch (e) { /* no DOM download available: the table is still on screen */ }
  }
  return jsx('button', {
    type: 'button', onClick: save, disabled: !rows.length,
    title: hint || ('xuất ' + rows.length + ' dòng đang hiển thị ra file CSV'),
    style: { ...field, border: '1px solid ' + HAIRLINE, opacity: rows.length ? 1 : 0.45 },
    children: 'CSV'
  })
}

function nextSort(cur, col) {
  if (!cur || cur.label !== col) return { label: col, dir: -1 }
  if (cur.dir === -1) return { label: col, dir: 1 }
  return null
}

function sortRows(rows, cols, sort) {
  if (!sort) return rows
  const col = cols.find(c => c.label === sort.label)
  if (!col || !col.sort) return rows
  const sign = sort.dir === 1 ? 1 : -1
  return rows.map((r, i) => [r, i]).sort((a, b) => {
    const x = col.sort(a[0])
    const y = col.sort(b[0])
    if (typeof x === 'number' && typeof y === 'number') return (x - y) * sign || (a[1] - b[1])
    return String(x == null ? '' : x).localeCompare(String(y == null ? '' : y)) * sign || (a[1] - b[1])
  }).map(p => p[0])
}

/* Column template shared by header and body: one cell per column, so digits line up in a column
 * instead of drifting inside a flex row -- the tables looked aligned per row and misaligned as one.
 * v2 adds clickable headers (a column sorts when it names a sort accessor), a scrolling body with the
 * header pinned, and a shared empty node, so every table on the page behaves the same way. */
function Grid({ cols, rows, keyOf, sort, onSort, maxHeight = 560, empty, hover }) {
  const [local, setLocal] = useState(null)
  const [over, setOver] = useState(null)
  const state = onSort ? sort : local
  const apply = (label) => {
    const next = nextSort(state, label)
    if (onSort) onSort(next)
    else setLocal(next)
  }
  const shown = onSort ? rows : sortRows(rows, cols, state)
  const template = cols.map(c => c.width || 'auto').join(' ')
  const line = extra => ({ display: 'grid', gridTemplateColumns: template, gap: 10,
    padding: '5px 10px', ...extra })
  const head = jsx('div', { style: line({ padding: '2px 10px', position: 'sticky', top: 0, zIndex: 3,
      background: PANEL, backdropFilter: 'blur(6px)', borderBottom: '1px solid ' + HAIRLINE,
      scrollbarGutter: 'stable' }),
    children: cols.map(c => (c.sort ? jsx('button', {
      type: 'button', onClick: () => apply(c.label),
      title: 'sắp xếp theo ' + c.label,
      style: { ...faint, fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em', cursor: 'pointer',
        textAlign: c.align || 'left', color: state && state.label === c.label ? ACCENT : INK_FAINT,
        background: 'transparent', border: 'none', padding: '4px 0', fontWeight: state && state.label === c.label ? 700 : 400 },
      children: c.label + (state && state.label === c.label ? (state.dir === 1 ? ' ▲' : ' ▼') : '')
    }, c.label) : jsx('span', { style: { ...faint, fontSize: 10, textTransform: 'uppercase',
      letterSpacing: '0.06em', textAlign: c.align || 'left' }, children: c.label }, c.label)))
  }, 'head')
  if (!shown.length) {
    return jsxs('div', { children: [head, jsx('div', {
      style: { ...faint, padding: '14px 10px' }, children: empty || 'Không có dòng nào.' }, 'empty')] }, 'grid')
  }
  return jsxs('div', { style: { maxHeight, overflowY: 'auto', overflowX: 'auto' }, children: [
    head,
    ...shown.map((r, i) => {
      const k = keyOf ? keyOf(r, i) : i
      /* A hover card belongs to the row it describes: the row becomes the positioned ancestor and the
       * card hangs below it, so nothing is measured and no coordinate math depends on the host's chrome.
       * Tables that never pass `hover` keep the exact markup they had -- no handlers, no extra node. */
      const body = hover && over === k ? hover(r) : null
      return jsx('div', {
        onMouseEnter: hover ? () => setOver(k) : undefined,
        onMouseLeave: hover ? () => setOver(null) : undefined,
        style: line({ borderTop: '1px solid ' + HAIRLINE, fontSize: 12,
          color: INK, background: i % 2 ? 'transparent' : 'rgba(128,128,128,0.04)',
          ...(body ? { position: 'relative', zIndex: 12 } : {}) }),
        children: [
          ...cols.map(c => jsx('span', { className: (c.cls && c.cls(r)) || undefined,
            style: { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              ...(c.num ? numCell : {}),
              ...(typeof c.style === 'function' ? (c.style(r) || {}) : (c.style || {})) },
            title: c.title ? String(c.title(r)) : undefined, children: c.render(r) }, c.label)),
          body
        ]
      }, k)
    })
  ] }, 'grid')
}

/* The one message every "nothing here yet" branch used to write by hand, in three different wordings. */
function Empty({ children }) {
  return jsx('div', { style: { ...card, ...faint }, children: children })
}

function Err({ source, children }) {
  return jsxs('div', { style: { ...faint, color: 'var(--ui-error, #d9534f)' }, children: [
    children, source ? ' (' + String(source).slice(0, 90) + ')' : ''
  ] })
}

/* The key table is defined out of the component because the CSV export has to use the exact same
 * column list the table renders -- a second list would drift on the next edit and export the wrong
 * numbers under the right headers. */
const KEY_COLS = [
  { label: 'key', width: '1fr', title: k => k.key_prefix,
    sort: k => String(k.name || k.key_prefix || ''), raw: k => (k.name || '?') + ' ' + (k.key_prefix || ''),
    render: k => (k.name || '?') + ' ' + (k.key_prefix || '') },
  { label: 'trạng thái', width: '92px', sort: k => (k.active ? 0 : 2),
    raw: k => (k.active ? 'đang dùng' : (k.revoked_at ? 'đã thu hồi' : 'ngưng')),
    render: k => k.active ? 'đang dùng' : (k.revoked_at ? 'đã thu hồi' : 'ngưng') },
  { label: 'rpm', width: '58px', align: 'right', num: true, sort: k => Number(k.rate_limit_rpm) || 0,
    render: k => num(k.rate_limit_rpm) },
  { label: 'lượt gọi', width: '80px', align: 'right', num: true, sort: k => Number(k.total_requests) || 0,
    render: k => num(k.total_requests) },
  { label: 'model', width: '62px', align: 'right', num: true, sort: k => Number(k.subscriptions) || 0,
    render: k => num(k.subscriptions) },
  { label: '24 h', width: '86px', align: 'right', num: true, sort: k => Number(k._dong24) || 0,
    render: k => dong(k._dong24 || 0) },
  { label: 'đã tiêu', width: '104px', align: 'right', num: true, sort: k => Number(k.total_spent) || 0,
    render: k => dong(Number(k.total_spent) || 0) }
]

function WalletTab() {
  const balance = useBalance(true)
  const snap = (balance.data && balance.data.balance) || {}
  const wallet = snap.wallet || {}
  /* vilao's per-key totals are lifetime; the 24 h column comes from the calls we have billed rows for. */
  const d24 = (balance.data && balance.data.per_key_24h) || {}
  const keys = (snap.keys || []).map(k => ({ ...k, _dong24: Number(d24[k.id]) || 0 }))
  const pending = snap.pending_topups || []
  const spent24 = Number((balance.data || {}).spent_24h) || 0
  const balanceValue = Number(wallet.balance) || 0
  const used = Number(wallet.used_balance) || 0
  const lifetime = balanceValue + used
  return jsxs('div', { style: { display: 'grid', gap: 12 }, children: [
    jsxs('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' }, children: [
      jsx(Stat, { label: 'Còn lại', value: dong(balanceValue), hint: lifetime > 0 ? pct(balanceValue / lifetime * 100) + ' của tổng đã nạp' : null }),
      jsx(Stat, { label: '24 giờ qua', value: dong(spent24), hint: spent24 > 0 ? '≈ ' + dong(spent24 * 7) + '/tuần' : 'chưa có chi tiêu' }),
      jsx(Stat, { label: 'Đã tiêu (cả đời)', value: dong(used) }),
      jsx(Stat, { label: 'Tổng đã nạp (suy ra)', value: dong(lifetime) }),
      jsx(Stat, { label: 'Rút được', value: dong(Number(wallet.withdrawable_balance) || 0) })
    ] }),
    balance.isError ? jsx('div', { style: faint, children: 'Không đọc được ví vilao: ' + String(balance.error) }) : null,
    balanceValue < 20000 ? jsxs('div', { style: { ...card, borderColor: 'currentColor' }, children: [
      jsx('strong', { children: 'Sắp hết tiền' }),
      jsx('div', { style: faint, children: 'Còn ' + dong(balanceValue) + '. Nạp thêm trong app vilao trước khi các lượt gọi fail.' })
    ] }) : null,
    pending.length ? jsxs('div', { style: card, children: [
      jsx('div', { style: { ...faint, marginBottom: 6 }, children: 'Lệnh nạp đang chờ (QR hết hạn sau 30 phút)' }),
      jsx(Grid, { cols: [
        { label: 'số tiền', width: '110px', num: true, render: t => dong(Number(t.amount_expected) || 0) },
        { label: 'mã giao dịch', width: '1fr', render: t => String(t.transaction_id || '') },
        { label: 'hết hạn', width: '140px', align: 'right', num: true,
          render: t => t.expires_at ? new Date(t.expires_at).toLocaleTimeString('vi-VN', { hour12: false }) : '—' }
      ], rows: pending, keyOf: t => t.transaction_id })
    ] }) : null,
    jsx('div', { style: card, children: jsxs('div', { children: [
      jsxs('div', { style: { ...gap, justifyContent: 'space-between', marginBottom: 6 }, children: [
        jsx('div', { style: faint, children: 'API key của bạn trên vilao · cột “24 h” lấy từ log cuộc gọi đã sync' }),
        jsx(ExportButton, { cols: KEY_COLS, rows: keys, name: 'vilao-keys', hint: 'xuất toàn bộ key đang hiển thị' })
      ] }),
      keys.length ? jsx(Grid, { cols: KEY_COLS, rows: keys, keyOf: k => k.id || k.key_prefix,
        empty: 'Chưa có API key nào trên tài khoản.' }) : jsx('div', { style: faint, children: '—' })
    ] }) }),
    jsx(Topups, {}),
    jsx('div', { style: faint, children: 'Cửa sổ quota/reset của từng provider vẫn nằm ở popover trên status bar (Provider Usage).' })
  ] })
}

/* ---------- quota ---------- */

/* The free tiers publish a request limit and say nothing about usage: no ``/usage`` route, no
 * ``x-ratelimit-*`` header on the response. The only meter that can exist is the one built from this
 * plugin's own call log, so the table reports what was measured and stops there. There is deliberately
 * no limit column: a comparison against a number nobody publishes would look like data. */
const QUOTA_WINDOWS = [{ value: '1', label: '1 giờ' }, { value: '24', label: '24 giờ' },
  { value: '168', label: '7 ngày' }]

const PROVIDER_COLS = [
  { label: 'provider', width: 'minmax(150px, 1fr)', title: r => r.provider, sort: r => String(r.provider || ''),
    render: r => r.provider },
  { label: 'lượt', width: '78px', align: 'right', num: true, sort: r => Number(r.calls) || 0, render: r => num(r.calls) },
  { label: 'token', width: '96px', align: 'right', num: true, sort: r => Number(r.tokens) || 0, render: r => tok(r.tokens) },
  { label: '₫ hóa đơn', width: '98px', align: 'right', num: true, sort: r => Number(r.dong) || 0,
    render: r => (Number(r.dong) ? dong(r.dong) : '—') },
  { label: 'rpm bây giờ', width: '96px', align: 'right', num: true, sort: r => Number(r.rpm_now) || 0,
    render: r => num(r.rpm_now) },
  { label: 'đỉnh rpm', width: '78px', align: 'right', num: true, sort: r => Number(r.peak_rpm) || 0,
    style: r => (Number(r.peak_rpm) >= 30 ? { className: HOT } : null), render: r => num(r.peak_rpm) },
  { label: 'đỉnh rph', width: '78px', align: 'right', num: true, sort: r => Number(r.peak_rph) || 0,
    render: r => num(r.peak_rph) },
  { label: 'lỗi', width: '56px', align: 'right', num: true, sort: r => Number(r.errors) || 0,
    style: r => (r.errors > 0 ? { className: HOT } : null), render: r => (r.errors ? String(r.errors) : '—') },
  { label: 'gần nhất', width: '132px', align: 'right', num: true, style: () => faint,
    sort: r => Number(r.last_ts) || 0, raw: r => (r.last_ts ? longOf(r.last_ts) : ''),
    render: r => (r.last_ts ? longOf(r.last_ts) : '—') }
]

function QuotaTab() {
  const [hours, setHours] = useState('24')
  const rate = useRate(Number(hours))
  const rows = (rate.data || {}).by_provider || []
  const overall = (rate.data || {}).overall || {}
  return jsxs('div', { style: { display: 'grid', gap: 12 }, children: [
    jsxs('div', { style: { ...gap, flexWrap: 'wrap' }, children: [
      jsx(Segmented, { options: QUOTA_WINDOWS, value: hours, onChange: setHours }),
      jsx('span', { style: faint, children: 'đo từ log Hermes — các provider này không có API usage để đọc' })
    ] }),
    rate.isError ? jsx('div', { style: faint, children: 'Không đọc được nhịp gọi: ' + String(rate.error) }) : null,
    jsx('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' }, children: [
      jsx(Stat, { label: 'Lượt trong cửa sổ', value: num(overall.calls) }, 'a'),
      jsx(Stat, { label: 'Token', value: tok(overall.tokens) }, 'b'),
      jsx(Stat, { label: 'đỉnh mỗi phút', value: num(overall.peak_rpm) + '/phút' }, 'c'),
      jsx(Stat, { label: 'đỉnh mỗi giờ', value: num(overall.peak_rph) + '/giờ' }, 'd'),
      jsx(Stat, { label: 'Bị trừ tiền', value: dong(overall.dong) }, 'e')
    ] }),
    rows.length ? jsx('div', { style: { ...card, padding: '6px 0' }, children: jsxs('div', { children: [
      jsxs('div', { style: { ...gap, justifyContent: 'flex-end', padding: '0 10px 4px' }, children: [
        jsx(ExportButton, { cols: PROVIDER_COLS, rows, name: 'providers' })
      ] }),
      jsx(Grid, { cols: PROVIDER_COLS, rows, keyOf: r => r.provider,
        empty: 'Chưa có lượt gọi nào trong cửa sổ này.' })
    ] }) }) : jsx(Empty, { children: 'Chưa có lượt gọi nào trong cửa sổ này.' })
  ] })
}

/* ---------- vilao console, inside the page ---------- */

/* Catalogue prices arrive as dong per 1,000,000 tokens (verified against a measured charge), which is
 * the only way they are comparable; a promo row shows the old price struck through rather than
 * replacing it, because the discount is what the console is advertising. */
function priceCell(v, promo) {
  if (v == null) return jsx('span', { style: faint, children: '—' })
  const off = Number(promo) || 0
  return jsxs('span', { style: numCell, children: [
    off > 0 ? jsx('s', { style: { ...faint, marginRight: 5 }, children: dong(v) }) : null,
    String(dong(off > 0 ? v * (1 - off / 100) : v)), '/1M'
  ] })
}

/* The desktop shell exposes window.hermesDesktop.openExternal for exactly this: a plugin that wants the
 * system browser rather than a navigation inside the app.
 *
 * The URL comes from the marketplace API, so it is checked here as well as at the source: https, and a
 * host the console actually is. Handing an arbitrary string to the OS shell is how a field that today
 * holds a model slug could tomorrow hold something else. */
const ALLOWED_LINK_HOSTS = ['vilao.ai', 'www.vilao.ai']

function safeExternal(url) {
  let u
  try {
    u = new URL(String(url))
  } catch (e) {
    return null
  }
  if (u.protocol !== 'https:' || ALLOWED_LINK_HOSTS.indexOf(u.hostname.toLowerCase()) < 0) return null
  return u.href
}

function openExternal(url) {
  const safe = safeExternal(url)
  if (!safe) return
  try {
    if (window.hermesDesktop && typeof window.hermesDesktop.openExternal === 'function') {
      void window.hermesDesktop.openExternal(safe)
      return
    }
  } catch (e) {
    /* fall through to a plain anchor navigation */
  }
  window.open(safe, '_blank', 'noopener')
}

const MODELS_PER = 100
/* Dong per 1,000,000 billed tokens, from the invoice rather than the price list. Returns null when the
 * row has no billed mass: the page would otherwise print a number that means "nothing measured". */
/* The price watch names the field that moved instead of averaging the offer's prices into one number:
 * a list-price cut and a cache-price rise are different decisions to make about an offer. */
function priceDelta(m) {
  const chg = (m && m.chg) || []
  if (!chg.length) return '—'
  const first = chg.find(c => c.kind === 'price')
  if (!first) return chg[0].kind === 'new' ? 'mới bán' : 'ngừng bán'
  const pct = Number(first.pct) || 0
  return (pct > 0 ? '+' : '') + Math.round(pct) + '% ' + first.field
}

function effectivePrice(m) {
  const mine = m && m.mine
  if (!mine) return null
  const billed = Number(mine.tok_billed) || 0
  const dongPaid = Number(mine.dong) || 0
  if (!billed || !dongPaid) return null
  return dongPaid / billed * 1000000
}

/* Price after promo, in dong/1M -- the number a sort or a CSV has to use, because the cell shows two
 * prices (struck-through list and net) and only one of them is what the invoice charges. An offer with
 * no price has to come back as null, not 0: Number(null) is 0, which would sort an unpriced offer as
 * the cheapest thing on the marketplace. */
function netPrice(m, field) {
  const raw = m[field]
  if (raw === null || raw === undefined || raw === '') return null
  const v = Number(raw)
  if (!isFinite(v)) return null
  const off = Number(m.promo_off) || 0
  return off > 0 ? v * (1 - off / 100) : v
}

const MODEL_COLS = [
  { label: 'model', width: 'minmax(140px, 1.2fr)', title: m => m.name + (m.seller ? ' · ' + m.seller : ''),
    sort: m => String(m.id || ''), raw: m => m.id, render: m => m.id },
  { label: 'provider', width: 'minmax(96px, 150px)',
    sort: m => String(m.provider_name || m.provider || ''), raw: m => m.provider_name || m.provider || '',
    render: m => m.provider_name || m.provider || '—' },
  { label: 'loại', width: '76px', hide: true, style: () => faint, sort: m => String(m.kind || ''),
    raw: m => m.kind || '', render: m => m.kind || '—' },
  { label: 'ngữ cảnh', width: '78px', align: 'right', num: true, sort: m => Number(m.context) || 0,
    raw: m => m.context || '', render: m => (m.context ? tok(m.context) : '—') },
  { label: 'giá vào', width: '116px', sort: m => (netPrice(m, 'in') == null ? -1 : netPrice(m, 'in')),
    raw: m => (netPrice(m, 'in') == null ? '' : netPrice(m, 'in')), render: m => priceCell(m.in, m.promo_off) },
  { label: 'giá ra', width: '116px', sort: m => (netPrice(m, 'out') == null ? -1 : netPrice(m, 'out')),
    raw: m => (netPrice(m, 'out') == null ? '' : netPrice(m, 'out')), render: m => priceCell(m.out, m.promo_off) },
  { label: 'cache đọc', width: '112px', hide: true, sort: m => (netPrice(m, 'cache_read') == null ? -1 : netPrice(m, 'cache_read')),
    raw: m => (netPrice(m, 'cache_read') == null ? '' : netPrice(m, 'cache_read')),
    render: m => priceCell(m.cache_read, null) },
  { label: 'sàn/lượt', width: '72px', hide: true, align: 'right', num: true, sort: m => Number(m.floor) || 0,
    raw: m => (m.floor == null ? '' : m.floor), render: m => (m.floor == null ? '—' : dong(m.floor)) },
  { label: 'thành công', width: '82px', align: 'right', num: true,
    sort: m => (m.success_rate == null ? -1 : Number(m.success_rate)),
    raw: m => (m.success_rate == null ? '' : m.success_rate),
    style: m => (m.success_rate != null && m.success_rate < 95 ? { className: HOT } : null),
    title: m => 'độ trễ ' + (m.latency_ms ? Math.round(m.latency_ms) + 'ms' : '—')
      + ' · ' + (m.requests ? num(m.requests) + ' lượt toàn marketplace' : ''),
    render: m => (m.success_rate == null ? '—' : m.success_rate.toFixed(1) + '%') },
  { label: 'bạn đã gọi', width: '86px', align: 'right', num: true,
    sort: m => Number(m.mine && m.mine.calls) || 0, raw: m => Number(m.mine && m.mine.calls) || 0,
    style: m => (m.mine ? { fontWeight: 600, color: INK } : faint),
    render: m => (m.mine ? tok(m.mine.calls) : '—') },
  { label: 'bạn đã trả', width: '96px', align: 'right', num: true,
    sort: m => Number(m.mine && m.mine.dong) || 0, raw: m => Number(m.mine && m.mine.dong) || 0,
    render: m => (m.mine ? dong(m.mine.dong) : '—') },
  /* Effective price: what the invoice says per 1M billed tokens, against the list price of this
   * offer. The denominator is the billing shape (in + out + cache read) because that is what vilao
   * charges on -- using the local tok_total would inflate the figure by exactly the cached share.
   * A row that has no billed tokens, or no dong, has no answer here, so it shows a dash rather
   * than a division by zero dressed up as a price. */
  { label: 'đ/1M bạn trả', width: '116px', align: 'right', num: true,
    sort: m => {
      const eff = effectivePrice(m)
      return eff == null ? -1 : eff
    },
    raw: m => {
      const eff = effectivePrice(m)
      return eff == null ? '' : Math.round(eff)
    },
    title: m => (m.mine && m.mine.tok_billed
      ? 'hóa đơn ' + dong(m.mine.dong) + ' / ' + num(m.mine.tok_billed) + ' token (gồm cache đọc)'
      : 'chưa đủ dữ liệu để suy ra giá'),
    style: m => {
      const eff = effectivePrice(m)
      if (eff == null) return faint
      const list = Number(netPrice(m, 'in')) || 0
      return list && eff > list * 1.2 ? { className: HOT } : { color: INK }
    },
    render: m => {
      const eff = effectivePrice(m)
      return eff == null ? '—' : dong(eff)
    } },
  { label: 'đổi giá', width: '104px', align: 'right', num: true,
    sort: m => (m.chg ? Math.max(...m.chg.map(c => Math.abs(Number(c.pct) || 0))) : -1),
    raw: m => (m.chg || []).map(c => c.field + ':' + (c.pct == null ? c.kind : Math.round(c.pct) + '%'))
      .join(' '),
    title: m => 'so với giá đã lưu ngày trước\n'
      + (m.chg || []).map(c => c.kind === 'new' ? 'offer mới xuất bản'
        : c.kind === 'gone' ? 'offer không còn trên marketplace'
          : c.field + ': ' + dong(Math.round(c.from)) + ' → ' + dong(Math.round(c.to))
            + ' (' + (c.pct > 0 ? '+' : '') + Math.round(c.pct) + '%)').join('\n'),
    style: m => (m.chg ? { color: ACCENT, fontWeight: 600 } : faint),
    render: m => priceDelta(m) },
  { label: 'mở', width: '54px', align: 'right', sort: m => (m.url ? 0 : 1), raw: m => m.url || '',
    render: m => m.url ? jsx('a', {
      href: m.url, target: '_blank', rel: 'noreferrer', title: m.url,
      onClick: e => { e.preventDefault(); openExternal(m.url) },
      style: { fontSize: 11, color: ACCENT, textDecoration: 'none', whiteSpace: 'nowrap' },
      children: 'vilao ↗' }) : jsx('span', { style: faint, children: '—' }) }
]

/* Which columns are hidden is a preference, not a layout bug: the offer table is 14 columns wide and
 * the default view drops the three nobody compares by eye. Persisted through the host's own storage
 * (the same mechanism the status-bar plugin uses for its refresh cadence) rather than localStorage,
 * because that is the surface the app guarantees a plugin can write. */
const COLS_PREF = 'models-cols'

let pref = null

function prefGet(key, fallback) {
  if (!pref) return fallback
  try {
    const v = pref.get(key, fallback)
    return v === undefined || v === null ? fallback : v
  } catch (e) {
    return fallback
  }
}

function prefSet(key, value) {
  if (!pref) return
  try {
    pref.set(key, value)
  } catch (e) {
    /* storage unavailable -- the choice then lasts the session, which is what it did before it persisted */
  }
}

function useHiddenCols(cols) {
  const [hidden, setHidden] = useState(() => {
    const stored = prefGet(COLS_PREF, null)
    const list = Array.isArray(stored) ? stored : (typeof stored === 'string' ? safeJson(stored) : null)
    return new Set(Array.isArray(list) ? list : cols.filter(c => c.hide).map(c => c.label))
  })
  const toggle = label => setHidden(prev => {
    const next = new Set(prev)
    if (next.has(label)) next.delete(label)
    else next.add(label)
    prefSet(COLS_PREF, Array.from(next))
    return next
  })
  return { hidden, toggle }
}

function safeJson(text) {
  try {
    return JSON.parse(text)
  } catch (e) {
    return null
  }
}

function ColsMenu({ cols, hidden, toggle }) {
  const [open, setOpen] = useState(false)
  const box = useRef(null)
  useEffect(() => {
    if (!open) return undefined
    const away = e => { if (box.current && !box.current.contains(e.target)) setOpen(false) }
    const key = e => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', key)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', key)
    }
  }, [open])
  return jsx('div', { ref: box, style: { position: 'relative' }, children: jsxs('div', { children: [
    jsx('button', { type: 'button', onClick: () => setOpen(v => !v), 'aria-expanded': open,
      style: { ...field, border: '1px solid ' + HAIRLINE },
      title: 'chọn cột muốn xem (lưu lại cho lần sau)',
      children: 'cột · ' + (cols.length - hidden.size) }, 'btn'),
    open ? jsx('div', { style: { position: 'absolute', zIndex: 30, right: 0, top: '100%', marginTop: 4,
        padding: 6, borderRadius: 8, background: PANEL, border: '1px solid ' + EDGE, minWidth: 190,
        boxShadow: '0 10px 28px rgba(0,0,0,0.35)', display: 'grid', gap: 2 },
      children: cols.map(c => jsxs('label', { style: { ...gap, gap: 8, fontSize: 12, cursor: 'pointer',
          padding: '3px 6px', borderRadius: 6, color: INK },
        children: [
          jsx('input', { type: 'checkbox', checked: !hidden.has(c.label), onChange: () => toggle(c.label) }),
          jsx('span', { children: c.label })
        ] }, c.label)) }) : null
  ] }) })
}

/* Vilao runs an SVG-render check against marketplace offers: a genuine upstream answers the reference
 * image test, a substituted or resold model tends not to, and `last_test_score` is the agreement with the
 * expected result. The three band labels below are THIS page's reading of that number -- Vilao publishes
 * the score, not the wording -- so the raw score, status and timestamp always sit beside the label and the
 * reader can disagree with the band. An offer with no record says "chưa có", because never-tested and
 * tested-and-failed are different facts and collapsing them is how a quiet listing looks like a guilty one.
 *
 * Real shape of the feed at integration time (799 offers read live): 718 enrolled in the test, 116 with a
 * timestamp, only 112 carrying a score (0-100, median 80), 4 with status "failed" and no score at all -- so
 * the no-score branch is the common case, not an error path. */
const SVG_BANDS = [[80, 'đạt'], [50, 'nghi ngờ'], [0, 'không đạt']]

function svgVerdict(m) {
  if (m.test_status === 'failed') {
    return { label: 'kiểm định thất bại', color: 'var(--ui-error, #d9534f)' }
  }
  const s = Number(m.score)
  if (!m.svg_test || !isFinite(s)) {
    return { label: 'chưa có kiểm định SVG', color: INK_FAINT }
  }
  const band = SVG_BANDS.find(x => s >= x[0])
  return {
    label: band[1] + ' · ' + Math.round(s) + '/100',
    color: s >= 80 ? 'var(--ui-success, #3fb27f)'
      : s >= 50 ? 'var(--ui-warning, #d9a13b)' : 'var(--ui-error, #d9534f)'
  }
}

/* The test date arrives as ISO text from a network feed. It is never made into a link and never trusted
 * as a number: parsed only to print, and an unparseable stamp prints as "không rõ ngày" rather than
 * "NaN/NaN". */
function testDate(iso) {
  const text = String(iso || '')
  if (!/^\d{4}-\d{2}-\d{2}/.test(text)) return 'không rõ ngày'
  const d = new Date(text)
  return isFinite(d.getTime()) ? d.toLocaleDateString('vi-VN') : 'không rõ ngày'
}

function ModelCard({ m }) {
  const v = svgVerdict(m)
  const line = (k, val, color) => jsxs('div', { style: gap, children: [
    jsx('span', { style: { ...faint, width: 92, flexShrink: 0 }, children: k }, 'k'),
    jsx('span', { style: { ...numCell, fontSize: 11, color: color || INK, textAlign: 'left' },
      children: val }, 'v')
  ] })
  return jsx('div', {
    style: { ...card, position: 'absolute', left: 8, top: '100%', marginTop: 2, zIndex: 30,
      minWidth: 330, maxWidth: 460, background: PANEL, border: '1px solid ' + EDGE, padding: '8px 10px',
      boxShadow: '0 12px 30px rgba(0,0,0,0.38)', pointerEvents: 'none', fontSize: 11 },
    children: jsxs('div', { style: { display: 'grid', gap: 3 }, children: [
      jsxs('div', { style: { ...gap, justifyContent: 'space-between' }, children: [
        jsx('span', { style: { color: INK, fontWeight: 600 },
          children: (m.provider || '?') + '/' + (m.id || '?') }, 't'),
        jsx('span', { style: { color: v.color, fontWeight: 600 }, children: v.label }, 'verdict')
      ] }),
      line('SVG test', m.score == null ? (m.test_status === 'failed' ? 'thất bại, không có điểm'
        : 'chưa chạy') : Math.round(Number(m.score)) + '/100 · ' + testDate(m.test_at)),
      line('phản hồi', m.requests
        ? pct(m.success_rate) + ' thành công · ' + num(m.requests) + ' lượt'
        + (m.failed_count ? ' · ' + num(m.failed_count) + ' lỗi' : '')
        : 'chưa có số liệu'),
      line('độ trễ', m.latency_ms ? Math.round(Number(m.latency_ms)) + ' ms'
        + (m.tps ? ' · ' + (Math.round(Number(m.tps) * 10) / 10) + ' tok/s' : '') : '—'),
      line('giá vào/ra', (m.in != null ? dong(m.in) : '—') + ' / ' + (m.out != null ? dong(m.out) : '')
        + 'đ/1M'),
      jsxs('div', { style: { ...faint, fontSize: 10, marginTop: 2 }, children: [
        'seller: ' + (m.seller || '?') + (m.verified ? ' · provider đã xác minh' : ' · chưa xác minh'),
        m.url ? ' · nhấn "mở" để xem trang Vilao của offer này' : ''
      ] })
    ] })
  })
}

function ModelsTab() {
  const [search, setSearch] = useState('')
  const [provider, setProvider] = useState('')
  const [sort, setSort] = useState({ label: 'bạn đã gọi', dir: -1 })
  const [movedOnly, setMovedOnly] = useState(false)
  const page = useValue($modelsPage)
  const setPage = p => $modelsPage.set(p)
  const cat = useCatalog(search)
  const { hidden, toggle } = useHiddenCols(MODEL_COLS)
  const cols = MODEL_COLS.filter(c => !hidden.has(c.label))
  const all = (((cat.data || {}).models) || [])
  const providers = useMemo(() => {
    const acc = new Map()
    for (const m of all) {
      const key = m.provider || '?'
      const item = acc.get(key) || { value: key, label: m.provider_name || key, n: 0 }
      item.n += 1
      acc.set(key, item)
    }
    return Array.from(acc.values()).sort((a, b) => b.n - a.n)
  }, [all])
  /* Sorted before the slice, not after: sorting the page in place would offer "the 10 cheapest offers
   * on page 3", which is not the same question and reads as if it were. */
  const rows = useMemo(() => {
    let filtered = provider ? all.filter(m => m.provider === provider) : all.slice()
    if (movedOnly) filtered = filtered.filter(m => m.chg)
    return sortRows(filtered, MODEL_COLS, sort)
  }, [all, provider, sort, movedOnly])
  const pages = Math.max(1, Math.ceil(rows.length / MODELS_PER))
  const current = Math.min(page, pages)
  const slice = rows.slice((current - 1) * MODELS_PER, current * MODELS_PER)
  const mine = rows.filter(r => r.mine).length
  /* Counted from the provider-filtered set, not from `rows`: once the toggle is on, rows only holds the
   * changed offers, and a control that reads "đổi giá · 0" while showing 40 rows is self-contradictory. */
  const changed = useMemo(
    () => all.filter(m => m.chg && (!provider || m.provider === provider)),
    [all, provider])
  const watchDay = ((((cat.data || {}).watch || {}).days || [])[1] || '').slice(8).replace('-', '/')
    || 'hôm trước'
  return jsxs('div', { style: { display: 'grid', gap: 12 }, children: [
    jsxs('div', { style: { ...gap, flexWrap: 'wrap' }, children: [
      jsx('input', { style: { ...field, minWidth: 210, cursor: 'text' }, placeholder: 'tìm model / provider…',
        'data-usage-search': 'models', value: search,
        onChange: e => { setSearch(e.target.value); setPage(1) } }),
      jsx(Pick, { label: 'provider', value: provider, width: 210,
        options: [{ value: '', label: 'mọi provider (' + all.length + ')' },
          ...providers.map(p => ({ value: p.value, label: p.label, meta: p.n + ' offer' }))],
        onChange: v => { setProvider(v); setPage(1) } }),
      jsx('span', { style: faint, children: cat.isFetching
        ? 'đang đọc bảng giá từ vilao…'
        : rows.length + ' offer đang bán · ' + new Set(rows.map(r => r.id)).size
          + ' model · ' + mine + ' offer có model bạn từng trả tiền' }),
      /* The watch is the part a price list cannot do without memory: the store kept yesterday's snapshot
       * of the same offer, so "what moved since you last looked" is a fact here, not a guess. */
      changed.length ? jsxs(Choice, { on: movedOnly,
        onClick: () => { setMovedOnly(v => !v); setPage(1) },
        children: [jsx('span', { children: 'đổi giá · ' + changed.length }),
          jsx('span', { style: faint, children: ' so với ' + watchDay })] }) : null,
      cat.data && cat.data.error ? jsx('span', { style: faint, children: String(cat.data.error).slice(0, 90) }) : null,
      jsx('button', { type: 'button', style: { ...field, border: '1px solid ' + HAIRLINE },
        title: 'Bảng giá là trang công khai, cache 6 giờ; bấm để đọc lại ngay.',
        onClick: () => queryClient && queryClient.invalidateQueries(CATALOG_KEY), children: 'Đọc lại bảng giá' })
    ] }),
    cat.isError ? jsx(Err, { source: 'models', children: 'Không đọc được bảng model từ vilao.' }) : null,
    jsxs('div', { style: { ...card, padding: 0 }, children: [
      jsxs('div', { style: { ...gap, justifyContent: 'space-between', flexWrap: 'wrap',
          padding: '6px 10px 0' }, children: [
        jsx('div', { style: { ...faint, fontSize: 10 },
          children: 'Mỗi dòng là một offer: cùng một model có nhiều provider bán với giá khác nhau — đó là lý do bảng này dài hơn số model.' }),
        jsxs('div', { style: { ...gap, gap: 6 }, children: [
          jsx(ColsMenu, { cols: MODEL_COLS, hidden, toggle }),
          jsx(ExportButton, { cols, rows, name: 'vilao-catalog',
            hint: 'xuất ' + rows.length + ' offer đang lọc (toàn bộ, không chỉ trang này)' })
        ] })
      ] }),
      rows.length ? jsx(Grid, { cols, rows: slice, sort, onSort: setSort,
        keyOf: m => m.key || (m.provider + '/' + m.id), maxHeight: 620,
        hover: m => jsx(ModelCard, { m }, 'card'),
        empty: 'Không offer nào khớp bộ lọc.' }) : jsx('div', { style: { ...faint, padding: 10 },
        children: cat.isFetching ? 'đang đọc bảng giá…' : 'Không offer nào khớp bộ lọc.' }),
      jsx(Pager, { data: { total: rows.length, page: current, pages, per: MODELS_PER }, onPage: setPage })
    ] })
  ] })
}

function until(iso) {
  if (!iso || String(iso).startsWith('0001')) return null
  const ms = new Date(iso).getTime() - Date.now()
  if (!isFinite(ms) || ms < 0) return 'đã quá hạn'
  const days = Math.floor(ms / 86400000)
  const hrs = Math.floor((ms % 86400000) / 3600000)
  return days > 0 ? days + ' ngày ' + hrs + 'h' : hrs + 'h'
}

function KeysTab() {
  const keys = useKeys(true)
  const rows = (keys.data && keys.data.keys) || []
  const [picked, setPicked] = useState('')
  const active = picked || (rows.find(k => k.active) || rows[0] || {}).id || ''
  const detail = useKeyDetail(active)
  const k = (detail.data || {}).key || {}
  const book = ((detail.data || {}).models) || []
  const daily = until(k.daily_next_reset)
  const monthly = until(k.monthly_next_reset)
  return jsxs('div', { style: { display: 'grid', gap: 12 }, children: [
    jsx('div', { style: { ...card, padding: '6px 0' }, children: rows.length ? jsx(Grid, { cols: [
      { label: 'key', width: 'minmax(140px, 1fr)', render: x => x.name || x.prefix },
      { label: 'trạng thái', width: '98px', style: x => (x.active ? { color: ACCENT, fontWeight: 600 } : faint),
        render: x => (x.active ? 'đang dùng' : x.revoked_at ? 'đã thu hồi' : 'ngưng') },
      { label: 'rpm', width: '58px', align: 'right', num: true, render: x => num(x.rpm) },
      { label: 'model', width: '68px', align: 'right', num: true, render: x => num(x.models) },
      { label: 'tổng lượt', width: '88px', align: 'right', num: true, render: x => num(x.requests) },
      { label: 'tổng đã trả', width: '112px', align: 'right', num: true, render: x => dong(x.spent) },
      { label: 'mở từ', width: '96px', align: 'right', num: true, style: faint,
        render: x => (x.created_at ? String(x.created_at).slice(0, 10) : '—') },
      { label: '', width: '78px', render: x => jsx(Choice, { on: x.id === active, wide: true,
        onClick: () => setPicked(x.id), children: 'chi tiết' }) }
    ], rows, keyOf: x => x.id }) : jsx('div', { style: faint, children: 'Không đọc được danh sách key.' }) }),
    active ? jsxs('div', { style: card, children: [
      jsxs('div', { style: { ...gap, justifyContent: 'space-between', marginBottom: 8 }, children: [
        jsxs('strong', { children: [String(k.name || 'key'), ' ', jsx('span', { style: faint,
          children: String(k.prefix || '') })] }),
        jsx('button', { type: 'button', style: { ...field, border: '1px solid ' + HAIRLINE },
          onClick: () => queryClient && queryClient.invalidateQueries(DETAIL_KEY), children: 'Đọc lại' })
      ] }),
      jsxs('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' }, children: [
        jsx(Stat, { label: 'Ngân sách ngày', value: k.daily_budget ? dong(k.daily_budget) : 'không đặt',
          hint: daily ? 'reset sau ' + daily : 'vilao không trả lời ngày reset' }),
        jsx(Stat, { label: 'Ngân sách tháng', value: k.monthly_budget ? dong(k.monthly_budget) : 'không đặt',
          hint: monthly ? 'reset sau ' + monthly : null }),
        jsx(Stat, { label: 'Lượt của key', value: num(k.requests) }),
        jsx(Stat, { label: 'Key này đã trả', value: dong(k.spent), hint: 'cả đời, theo vilao' })
      ] }),
      jsx('div', { style: { ...faint, marginTop: 8 },
        children: 'vilao trả về ngày reset nhưng không trả về số đã tiêu trong kỳ — hai ô ngân sách là hạn mức, không phải thanh tiến trình.' }),
      jsx('div', { style: { marginTop: 10 }, children: book.length ? jsx(Grid, { cols: [
        { label: 'model key này gọi được', width: 'minmax(150px, 1fr)', render: m => m.model },
        { label: 'giá vào', width: '112px', render: m => priceCell(m.in, m.promo_off) },
        { label: 'giá ra', width: '112px', render: m => priceCell(m.out, m.promo_off) },
        { label: 'sàn mỗi lượt', width: '98px', align: 'right', num: true,
          render: m => (m.floor == null ? '—' : dong(m.floor)) },
        { label: 'lượt', width: '72px', align: 'right', num: true, render: m => num(m.requests) },
        { label: '₫', width: '98px', align: 'right', num: true, render: m => dong(m.spent) },
        { label: 'token', width: '86px', align: 'right', num: true, render: m => tok(m.tokens) },
        { label: 'health', width: '88px', style: m => (m.health && m.health !== 'healthy' ? { color: INK } : faint),
          render: m => m.health || '—' }
      ], rows: book.slice().sort((a, b) => (b.spent || 0) - (a.spent || 0)), keyOf: m => m.model })
        : jsx('div', { style: faint, children: 'Key này không có bảng giá riêng.' }) })
    ] }) : null
  ] })
}

function Topups() {
  const t = useTopups()
  const rows = (t.data && t.data.transactions) || []
  const acct = (t.data || {}).account || {}
  const about = [acct.role ? 'vai ' + acct.role : null,
    acct.created_at ? 'mở ' + String(acct.created_at).slice(0, 10) : null,
    acct.last_activity_at ? 'hoạt động ' + String(acct.last_activity_at).slice(0, 16).replace('T', ' ') : null]
    .filter(Boolean).join(' · ')
  return jsxs('div', { style: card, children: [
    jsxs('div', { style: { ...gap, justifyContent: 'space-between', marginBottom: 6 }, children: [
      jsx('div', { style: faint, children: 'Lịch sử nạp tiền · tổng đã trả ' + ((t.data || {}).paid_total ? dong(t.data.paid_total) : '—') }),
      acct.username ? jsx('span', { style: faint, title: acct.email || '',
        children: 'tài khoản ' + acct.username + (about ? ' · ' + about : '') }, 'acct') : null
    ] }),
    rows.length ? jsx(Grid, { cols: [
      { label: 'số tiền', width: '104px', align: 'right', num: true, render: x => dong(x.paid || x.expected) },
      { label: 'trạng thái', width: '96px', style: x => (x.status === 'paid' ? { color: ACCENT } : faint),
        render: x => (x.status === 'paid' ? 'đã trả' : x.status === 'pending' ? 'đang chờ' : x.status || '—') },
      { label: 'hình thức', width: '110px', render: x => x.method || '—' },
      { label: 'lúc tạo', width: '150px', align: 'right', num: true, style: faint,
        render: x => (x.created_at ? String(x.created_at).slice(0, 16).replace('T', ' ') : '—') },
      { label: 'tiền về ví', width: '150px', align: 'right', num: true,
        render: x => (x.paid_at ? String(x.paid_at).slice(0, 16).replace('T', ' ') : '—') }
    ], rows, keyOf: x => x.id }) : jsx('div', { style: faint, children: t.isLoading ? 'đang đọc…' : 'Không đọc được lịch sử nạp.' })
  ] })
}

/* Every model Hermes has touched, from the call log rather than from the bill. This is the tab that
 * covers the free tiers: NVIDIA, OpenCode Zen and Kilo never appear in a vilao invoice, so a bill-driven
 * view cannot see them at all, while the log already holds their tokens, delays and error counts.
 *
 * It replaces the old Providers tab, which was the same model rollup restricted to billed dong -- and
 * therefore blank for exactly the providers worth looking at. */
const LOG_WINDOWS = [{ value: '30', label: '1 tháng' }, { value: '90', label: '3 tháng' },
  { value: '365', label: '1 năm' }]

/* Both Log-tab tables are defined outside the component so the CSV export can use the identical column
 * list the screen does, and so each sort accessor sits next to the render it has to agree with. */
const SESSION_COLS = [
  { label: 'phiên', width: 'minmax(120px, 1fr)', title: s => String(s.session_id || ''),
    sort: s => String(s.session_id || ''), raw: s => s.session_id, render: s => shortSession(s.session_id) },
  { label: 'home', width: 'minmax(80px, 120px)', style: () => faint, sort: s => String(s.profile || ''),
    raw: s => s.profile || '', render: s => s.profile || '—' },
  { label: 'nền', width: '64px', style: () => faint, sort: s => String(s.platform || ''),
    raw: s => s.platform || '', render: s => s.platform || '—' },
  { label: 'lượt', width: '62px', align: 'right', num: true, sort: s => Number(s.calls) || 0,
    render: s => num(s.calls) },
  { label: 'model', width: '62px', align: 'right', num: true, sort: s => Number(s.models) || 0,
    render: s => num(s.models) },
  { label: 'token', width: '86px', align: 'right', num: true, sort: s => Number(s.tokens) || 0,
    render: s => tok(s.tokens) },
  { label: 'cache', width: '76px', align: 'right', num: true, sort: s => Number(s.tok_cache) || 0,
    style: s => (Number(s.tokens) && Number(s.tok_cache) / Number(s.tokens) > 0.5 ? { color: ACCENT } : faint),
    render: s => tok(s.tok_cache) },
  { label: '₫ hóa đơn', width: '92px', align: 'right', num: true, sort: s => Number(s.dong) || 0,
    raw: s => Math.round(Number(s.dong) || 0), render: s => (Number(s.dong) ? dong(s.dong) : '—') },
  { label: 'lỗi', width: '50px', align: 'right', num: true, sort: s => Number(s.errors) || 0,
    style: s => (Number(s.errors) ? { className: HOT } : null), render: s => (s.errors ? num(s.errors) : '—') },
  { label: 'dài', width: '76px', align: 'right', num: true, style: () => faint,
    sort: s => (Number(s.last_ts) || 0) - (Number(s.first_ts) || 0), raw: s => spanOf(s.first_ts, s.last_ts),
    title: s => 'từ ' + longOf(s.first_ts) + ' tới ' + longOf(s.last_ts),
    render: s => spanOf(s.first_ts, s.last_ts) }
]

const LOG_MODEL_COLS = [
  { label: 'model', width: 'minmax(160px, 1fr)', title: m => m.model_key + ' · bấm để xem từng lượt gọi',
    sort: m => String(m.model_key || ''), raw: m => m.model_key,
    render: m => jsx('button', {
      type: 'button',
      onClick: () => { $search.set(m.model_key); $mode.set('hermes'); $page.set(1); $tab.set('live') },
      style: { background: 'transparent', border: 'none', padding: 0, margin: 0, font: 'inherit',
        color: 'inherit', cursor: 'pointer', textAlign: 'left', maxWidth: '100%', overflow: 'hidden',
        textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
      children: shortModel(m.model_key) }) },
  { label: 'provider', width: 'minmax(110px, 150px)', style: faint, sort: m => String(m.providers || ''),
    raw: m => m.providers || '', render: m => m.providers || '—' },
  { label: 'home', width: 'minmax(90px, 130px)', style: faint, sort: m => String(m.profiles || ''),
    raw: m => m.profiles || '', render: m => m.profiles || '—' },
  { label: 'lượt', width: '66px', align: 'right', num: true, sort: m => Number(m.calls) || 0,
    render: m => num(m.calls) },
  { label: 'token', width: '86px', align: 'right', num: true, sort: m => Number(m.tokens) || 0,
    render: m => tok(m.tokens) },
  { label: 'cache', width: '78px', align: 'right', num: true, sort: m => Number(m.tok_cache) || 0,
    style: m => (Number(m.tokens) && Number(m.tok_cache) / Number(m.tokens) > 0.5 ? { color: ACCENT } : faint),
    render: m => tok(m.tok_cache) },
  { label: '₫ hóa đơn', width: '92px', align: 'right', num: true, sort: m => Number(m.dong) || 0,
    raw: m => Math.round(Number(m.dong) || 0), render: m => (Number(m.dong) ? dong(m.dong) : '—') },
  { label: 'ước tính', width: '84px', align: 'right', num: true, style: faint, sort: m => Number(m.usd) || 0,
    raw: m => Number(m.usd) || 0, title: m => estTitle(m.usd), render: m => est(m.usd) },
  { label: 'lỗi', width: '50px', align: 'right', num: true, sort: m => Number(m.errors) || 0,
    style: m => (Number(m.errors) ? { className: HOT } : null), render: m => (m.errors ? num(m.errors) : '—') },
  { label: 'trễ TB', width: '68px', align: 'right', num: true, sort: m => Number(m.avg_ms) || 0,
    raw: m => (m.avg_ms ? Math.round(m.avg_ms) : ''), render: m => (m.avg_ms ? Math.round(m.avg_ms) + 'ms' : '—') },
  { label: 'ngày dùng', width: '74px', align: 'right', num: true, sort: m => Number(m.days) || 0,
    render: m => num(m.days) },
  { label: 'lần cuối', width: '132px', align: 'right', num: true, style: faint, sort: m => Number(m.last_ts) || 0,
    raw: m => (m.last_ts ? longOf(m.last_ts) : ''), render: m => (m.last_ts ? longOf(m.last_ts) : '—') }
]

function LogTab() {
  const [days, setDays] = useState('30')
  const profile = useValue($profile)
  const log = useLog(Number(days))
  const sessions = useSessions(Number(days))
  const daily = useSummary(Number(days), 'hook')
  const homes = useProfiles()
  const models = (log.data && log.data.models) || []
  const byDate = ((daily.data || {}).summary || {}).days_by_date || []
  const homeList = useMemo(() => homeOptions((homes.data && homes.data.profiles) || []), [homes.data])
  const byProfile = useMemo(() => {
    const acc = new Map()
    for (const m of models) {
      for (const h of String(m.profiles || '').split(',')) {
        if (!h) continue
        acc.set(h, (acc.get(h) || 0) + Number(m.calls || 0))
      }
    }
    return Array.from(acc, ([profile, calls]) => ({ profile, calls })).sort((a, b) => b.calls - a.calls)
  }, [models])
  const [sessSort, setSessSort] = useState({ label: '₫ hóa đơn', dir: -1 })
  const [modelSort, setModelSort] = useState({ label: 'lượt', dir: -1 })
  const sessionRows = useMemo(
    () => sortRows((sessions.data && sessions.data.sessions) || [], SESSION_COLS, sessSort),
    [sessions.data, sessSort])
  const modelRows = useMemo(() => sortRows(models, LOG_MODEL_COLS, modelSort), [models, modelSort])
  const totals = models.reduce((a, m) => {
    a.calls += Number(m.calls) || 0
    a.tokens += Number(m.tokens) || 0
    a.dong += Number(m.dong) || 0
    a.errors += Number(m.errors) || 0
    return a
  }, { calls: 0, tokens: 0, dong: 0, errors: 0 })
  return jsxs('div', { style: { display: 'grid', gap: 12 }, children: [
    jsxs('div', { style: { ...gap, flexWrap: 'wrap' }, children: [
      jsx(Segmented, { options: LOG_WINDOWS, value: days, onChange: setDays }),
      jsx(Pick, { label: 'home', value: profile, width: 210, options: homeList,
        onChange: v => $profile.set(v) }),
      jsx('span', { style: faint, children: 'nguồn: log Hermes (mọi provider), không chỉ hóa đơn vilao' })
    ] }),
    jsx('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' }, children: [
      jsx(Stat, { label: 'Lượt gọi', value: num(totals.calls) }, 'a'),
      jsx(Stat, { label: 'Token', value: tok(totals.tokens) }, 'b'),
      jsx(Stat, { label: 'Model đã dùng', value: num(models.length) }, 'c'),
      jsx(Stat, { label: 'Bị trừ tiền', value: dong(totals.dong), hint: 'chỉ phần khớp được hóa đơn' }, 'd'),
      jsx(Stat, { label: 'Lỗi', value: num(totals.errors) }, 'e')
    ] }),
    jsx('div', { style: card, children: jsxs('div', { children: [
      jsx('div', { style: { ...faint, marginBottom: 6 },
        children: 'Usage mỗi ngày · ' + days + ' ngày (log Hermes)' }),
      byDate.length ? jsx(Lines, { rows: byDate, series: [
        { key: 'tokens', label: 'token', format: tok },
        { key: 'calls', label: 'lượt gọi', format: num, color: 'var(--ui-success, #3fb27f)' },
        { key: 'tok_cache_read', label: 'token đọc từ cache', format: tok, color: 'var(--ui-warning, #d9a13b)' }
      ] }) : jsx('div', { style: faint, children: 'Chưa có ngày nào trong cửa sổ này.' })
    ] }) }),
    byProfile.length ? jsx('div', { style: card, children: jsxs('div', { children: [
      jsx('div', { style: { ...faint, marginBottom: 6 }, children: 'Lượt gọi chia theo home' }),
      jsx(Bars, { rows: byProfile, valueKey: 'calls', labelKey: 'profile', format: num })
    ] }) }) : null,
    /* Sessions, because the question when the wallet drops is "what did that run do", and a per-call log
     * of 100-row pages cannot answer it. Cache share is shown next to the tokens: it is what makes a long
     * conversation cheap or expensive. */
    (sessions.data && sessions.data.sessions) ? jsxs('div', { style: { ...card, padding: '6px 0' }, children: [
      jsx('div', { style: { ...gap, justifyContent: 'flex-end', padding: '0 10px' },
        children: jsx(ExportButton, { cols: SESSION_COLS, rows: sessionRows, name: 'log-sessions' }) }),
      sessionRows.length ? jsx(Grid, { cols: SESSION_COLS, rows: sessionRows,
        keyOf: s => String(s.session_id), sort: sessSort, onSort: setSessSort, maxHeight: 420 })
        : jsx('div', { style: { ...faint, padding: 10 }, children: 'Chưa có phiên nào trong cửa sổ này.' }) ] }) : null,
    models.length ? jsxs('div', { style: { ...card, padding: '6px 0' }, children: [
      jsx('div', { style: { ...gap, justifyContent: 'flex-end', padding: '0 10px' },
        children: jsx(ExportButton, { cols: LOG_MODEL_COLS, rows: modelRows, name: 'log-models' }) }),
      jsx(Grid, { cols: LOG_MODEL_COLS, rows: modelRows, keyOf: m => m.model_key,
        sort: modelSort, onSort: setModelSort, maxHeight: 480 }) ] })
      : jsx(Empty, { children: 'Chưa có model nào trong log ở cửa sổ này.' })
  ] })
}

/* Keyboard, because this page is a browser over tens of thousands of rows and the mouse is the slowest
 * way to move through them: [ ] page, / focus the filter of the tab you are on, Esc close the open row,
 * 1-7 jump between tabs. No modifier is hijacked, and every key is inert while a field has focus --
 * typing "/" into a filter must stay a slash. */
function useHotkeys(tab) {
  useEffect(() => {
    const onKey = e => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const t = e.target
      const typing = !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)
      if (e.key === 'Escape') {
        if (typing) { t.blur(); return }
        $openRow.set(null)
        return
      }
      if (typing) return
      if (e.key === '[' || e.key === ']') {
        if (PAGER.go) { e.preventDefault(); PAGER.go(e.key === ']' ? 1 : -1) }
        return
      }
      if (e.key === '/') {
        const box = document.querySelector('[data-usage-search]')
        if (box) { e.preventDefault(); box.focus(); if (box.select) box.select() }
        return
      }
      const n = Number(e.key)
      if (Number.isInteger(n) && n >= 1 && n <= TABS.length && TABS[n - 1] !== tab) $tab.set(TABS[n - 1])
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [tab])
}

function Page() {
  const tab = useValue($tab)
  const fx = useFx()
  useCallFeed()
  useFirstRunBackfill(30)
  useHotkeys(tab)
  return jsxs('div', { style: { padding: 16, display: 'grid', gap: 12, alignContent: 'start', height: '100%', overflowY: 'auto' }, children: [
    jsxs('div', { style: { ...gap, justifyContent: 'space-between' }, children: [
      jsx('h1', { style: { fontSize: 18, fontWeight: 600, margin: 0 }, children: 'Usage' }),
      jsx('span', { style: faint, children: 'v' + VERSION })
    ] }),
    jsx(TabBar, {}),
    jsx(Banner, {}),
    tab === 'live' ? jsx(LiveTab, {}) : null,
    tab === 'trend' ? jsx(TrendTab, {}) : null,
    tab === 'log' ? jsx(LogTab, {}) : null,
    tab === 'wallet' ? jsx(WalletTab, {}) : null,
    tab === 'quota' ? jsx(QuotaTab, {}) : null,
    tab === 'keys' ? jsx(KeysTab, {}) : null,
    tab === 'models' ? jsx(ModelsTab, {}) : null,
    jsxs('div', { style: { ...faint, paddingTop: 4, display: 'flex', gap: 12, flexWrap: 'wrap' }, children: [
      jsx('span', { children: rateLine((fx.data || {}).fx) }, 'fx'),
      jsx('span', { style: { marginLeft: 'auto' },
        children: 'phím: [ ] trang · / lọc · 1-7 tab · Esc đóng' }, 'keys')
    ] })
  ] })
}

export default {
  id: PLUGIN_ID,
  name: PLUGIN_NAME,
  description: 'Per-model call log and cost charts: every provider Hermes called, with real dong from the vilao bill.',
  defaultEnabled: true,
  register(ctx) {
    try { rest = ctx.rest || null } catch (e) { rest = null }
    try { pref = ctx.storage || null } catch (e) { pref = null }
    const onDispose = typeof ctx.onDispose === 'function' ? fn => ctx.onDispose(fn) : () => {}
    onDispose(() => { rest = null })

    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => jsx(Page, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 62, data: { path: ROUTE, label: PLUGIN_NAME, codicon: 'watch' } },
      { id: 'open', area: PALETTE_AREA, data: {
        id: 'resetwatch.open', label: 'Usage: Open', keywords: ['usage', 'cost', 'log', 'calls', 'dong', 'vilao', 'quota'],
        run: () => host.navigate(ROUTE)
      } },
      ...(KEYBINDS_AREA ? [{ id: 'open-key', area: KEYBINDS_AREA, data: {
        id: 'resetwatch.open', label: 'Open Usage', category: PLUGIN_NAME, defaults: ['mod+alt+r'],
        run: () => host.navigate(ROUTE)
      } }] : [])
    ])
  }
}
