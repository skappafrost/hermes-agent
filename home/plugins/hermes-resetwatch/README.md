# Usage (plugin id `hermes-resetwatch`, route `/resetwatch`)

> **Locally built, locally patched — nothing here comes from the catalog.** The store, the matcher and
> the page are all this machine's code. `desktop-plugins/hermes-resetwatch/` is a copy the app
> republishes from `desktop/`; edit the source, never the copy.

Per-model call log and cost charts for Hermes Desktop: every provider Hermes called, with the real dong
from the vilao bill attached to it.

## What the seven tabs are

| Tab | Question it answers | Route |
|---|---|---|
| **Live** | every call, newest first, filterable and paged (100/page) | `/calls` |
| **Trend** | which days cost what, and the year at a glance | `/summary?days=` (day rows + day×model matrix) |
| **Log** | what Hermes' own log says per model and per session, not only what vilao billed | `/log`, `/sessions` |
| **Quota** | measured request rate per provider (the free tiers publish no usage API) | `/rate` |
| **Wallet** | the vilao balance, per-key spending, top-up history | `/balance`, `/topups` |
| **Keys** | one consumer key's price book and usage | `/keys/detail` |
| **Models** | the whole marketplace (819 offers) with your own price beside each list price | `/catalog` |

Interactions worth knowing:

- **Keyboard** — `[` `]` page, `/` focus the filter, `1`-`7` switch tab, `Esc` close. Keys are inert
  while a field has focus, and no modifier is taken.
- **Click a header** to sort (numeric columns sort numerically; a second click reverses, a third clears).
  Sorting happens before paging, so "the 10 cheapest offers" means the ten cheapest, not ten of a page.
- **Click a year-grid square** to open the Live tab filtered to exactly that day. Clicking a model name
  filters the log to that model.
- **CSV** exports the column list the table is actually showing, for the rows the filter matched.
- **Columns** in the Models tab: three low-signal columns start hidden and the choice is remembered.
- **đổi giá** appears once the store holds two days of catalogue snapshots; it compares net prices
  (after promo) per offer, and reports offers that appeared or disappeared.

## What the calendar shades, and why it is not money

The GitHub-style year grid shades by **requests per day**, on four quantile cuts of the days that carry
traffic — not by `dong / peak`. The old ramp had one ink colour and pegged its scale to the single most
expensive day of the year, which fails in both directions on real data: one expensive day flattens every
normal day onto the 0.15 floor, and while no outlier exists the same days all sit near 1.0. Measured on
this store (43 days of 2026, peak 23.149đ, median 2.015đ, peak day 4.377 calls, median 622): the median
day rendered at 0.087 of peak, i.e. pinned to the floor, so most of the year looked identical. The request
bands distribute as 9 / 9 / 8 / 9 / 8 days across the five levels, top band 18.6% — and it stays ~20%
however the account grows, because the cuts are recomputed from the window on every render. Money did not
move: it leads the cell's tooltip, alongside the day's top three models.

## Reading the page with the mouse

- **Line charts** (Trend, Log) are hover-readable: the pointer's x maps straight back to a day index —
  the geometry is already `pad + i * step` in viewBox units — and the caption under the chart swaps
  between "rê chuột để đọc từng ngày" and that day's values for each series. A dashed marker and a dot
  track the point. The caption row has a reserved `minHeight`, so reading a value never shifts the chart.
- **Models rows** open a card under the hovered offer with the **Vilao SVG-render check** (`has_svg_test`,
  `last_test_score`, `last_test_status`, `last_test_at`) plus success rate, request and failure counts,
  health-check latency, prices and the seller. The check is how Vilao tells an authentic model from a
  substituted one: a real upstream answers the reference image test. The three labels — `đạt` (≥80),
  `nghi ngờ` (50-79), `không đạt` (<50) — are **this page's reading of the number, not Vilao's wording**,
  so the raw score, status and date always sit next to the label and the bands can be argued with.
  "chưa có kiểm định SVG" is stated as its own case: at integration 799 offers carried the field, only 112
  carried a score and 4 were `failed`, so collapsing "never tested" into "failed" would smear most of the
  marketplace. The card is `pointer-events: none` — a card that could catch the pointer would fight the
  row for the hover and flicker.

## How the numbers stay honest

Two feeds land in one SQLite file (`<root>/plugin-data/resetwatch/calls.sqlite`, shared by all five
profile homes through junctions):

- `source='hook'` — Hermes' observer hooks: every provider, token shape as Hermes reports it
  (input + output), USD estimate from the internal price book.
- `source='bill'` — vilao's billing API: real dong, billed token shape (input + output + cache read).

`match_rows()` pairs a charge to a call by model key and nearest timestamp, and the dong then lives on
the *call* row. Consequences the code depends on:
- A đ/1M price must divide by the **billed** shape (`tok_in + tok_out + tok_cache`), not `tok_total`.
  Measured on one call: 3,145 tokens locally against 110,665 billed.
- The empty `profile` bucket is the billing feed, which belongs to the account, not to a home. The page
  labels it `hóa đơn`; `-` means "a row we cannot attribute".
- Never re-ingest a charge that already paired — the pair folds it into the call row, so the check is
  against `bill_request_id` on the call, not only the bill table.

### Who pulls the bill

`usage_bill.auto_pulse` is the only background job in this plugin, and it is the reason the status bar can
print a price at all: the pairing lives in the pull, so a feed that only refreshes when a tab is open
leaves every new call at `local_only` with no dong. After a call is recorded, the hook schedules one pull
of the newest two bill pages on a daemon thread, 12 s late because vilao writes the charge after the
response finishes.

Three things bound it. The window is 90 s per process and the claim for it is stored in `meta`
(`bill_pulse_at`, via `claim_slot`), so the five profile homes share one pull per window instead of each
paging the same bill. One thread at a time, enforced by a non-blocking lock. And nothing on the turn's
critical path does IO for this: the gate is two float comparisons, measured at +0.07 ms per call
(1.35 ms → 1.42 ms, same process, warm store). Every request is a GET; the token is full-scope, so this
module never posts.

## The rollups

`day_stats` (day × home × feed) and `day_model_stats` (day × home × feed × model) are derived tables,
rebuilt inside the same transaction as every write to `calls` (`touch_days`). Aggregates are stored raw
(`ms_sum`/`ms_n`, not an average) so re-grouping across days or models stays exact.

`rollup_consistent()` fingerprints `calls` (rows, billed dong, newest rowid — over the days the rollup
can describe) and `ensure_rollups()` rebuilds when they disagree, which is how a row written by an older
build or a crash between write and rebuild gets counted instead of silently missing. There is no memo:
the rollup *is* the cache, and reading it is ~1 ms for a year.

`catalog_prices` is the price watch — one row per (day, offer), written by `/catalog` when the
marketplace is read, pruned to `CATALOG_RETENTION_DAYS`.

## Traps this code has already been bitten by

1. Plugin modules load by **file path**: `from . import x` fails; import by the names the loader uses.
2. A pooled SQLite connection in legacy isolation mode serves the snapshot it read at startup forever —
   hence `isolation_level=None` plus explicit `BEGIN IMMEDIATE`.
3. An index over `date(ts,…)` cannot exist; the `day` column has to be stored, and any index on it must
   be created *after* `ALTER TABLE … ADD COLUMN day`, or a migrated store fails with `no such column`.
4. `children` belongs in the props object. `jsxs('div', {…}, [ … ])` puts the array in the **key** slot
   and renders an empty element — which is why the render harness asserts a marker string per tab.
5. A `HERMES_HOME` inside the install root resolves back to the root, so a probe run there writes into
   the live store. Use a temp home.
6. Bill replay can delete rows; a prune that does not rebuild the days it emptied leaves a Trend square
   that never fades.
7. Windows: an open SQLite handle blocks temp-dir deletion, so tests call `close_connections()`.
8. The console is cp1252; Vietnamese assertion text must survive `stdout.reconfigure(utf-8)`.

## Tests

```
py -3.13 tests/test_usage_page.py                 # 45 cases, store + matcher + routes
node ../../scratch/render_check.mjs               # renders all 7 tabs against live payloads
```

`scratch/dump_fixtures.py` writes `fixtures.json` from the running backend so the harness sees the
shapes the routes really return, and `scratch/render_check.mjs` fails (exit 1) on a tab that renders
`undefined`, `NaN`, `[object Object]`, or none of its expected marker.
