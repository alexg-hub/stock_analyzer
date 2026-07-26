# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **three-tier stock filter** running nightly on this Windows machine via Task
Scheduler, plus historical tooling (single-ticker backtesters, a universe-wide
profit backtest, a threshold tuner).

1. **Tier 1 — technical.** The S&P 500 screens (breakout, pullback; reclaim
   disabled). Records `Setup` (`full`/`partial`) + `Missing`.
2. **Tier 2 — quality.** `fundamentals.quality.rules` graded over the tier-1
   hits only. Records `Quality` (the ⭐ badge) + `Quality Missing`.
3. **Tier 3 — deep dive.** The `deep-dive` skill over Yahoo + IBKR + SEC + web,
   producing a graded report per ticker.

Tiers 1+2 are `run_scanners.py`: one combined Discord alert (text + per-signal
chart images) and the `output/latest_hits.json` hand-off. Tier 3 reads that
hand-off, both automatically (`run_deepdive.bat`, chained from
`run_scanner.bat`) and on demand.

Validation is `python tests/run_all.py` — plain scripts, no test dependency,
asserting **invariants** rather than recorded output (config gets retuned
constantly, so snapshot tests would be stale within a session). The documented
known cases (JNJ 2025 breakout, MSFT 2024 SMA pullbacks, META 2023-02-02 SMA
reclaim) live in `tests/test_path_equivalence.py`, which needs the network and
so runs only with `--network`.

## Commands

```powershell
pip install -r requirements.txt

# Tests. Offline + cache-backed by default; exit 0 pass / 1 fail / 2 skip.
# Never downloads and never sends to Discord.
python tests/run_all.py
python tests/run_all.py --network      # adds the Yahoo round-trip test

# Full production scan (all screens). WARNING: sends a real Discord message
# to the user's channel (config.json contains a live webhook URL). Don't run
# it casually.
python run_scanners.py

# Historical validation of the same logic on one ticker (no Discord send);
# each writes output/backtest_*.csv and output/backtest_*.png
python backtest_breakout.py --ticker JNJ  --start 2025-01-01 --end 2025-10-31
python backtest_pullback.py --ticker MSFT --start 2024-01-01 --end 2025-06-30
python backtest_reclaim.py  --ticker META --start 2023-01-01 --end 2023-12-31

# Universe-wide profit backtest: every screen x all history, sweeping a grid of
# "wait x trading days, then hold y". No Discord. Caches the price panel, so
# re-runs after a config tweak take seconds; --refresh re-downloads.
python backtest_universe.py
python backtest_universe.py --entry-delay 0 --holding-days 30   # one cell
python backtest_universe.py --screens breakout_strategy --entry signal_close
python backtest_universe.py --split-by-tier                     # full vs partial

# Threshold tuning for ONE screen, scored against the random-entry baseline and
# against protected cases that must keep firing. Reads the cached panel only --
# run backtest_universe.py once first. No Discord.
python tune_screen.py sensitivity reclaim_strategy
python tune_screen.py grid breakout_strategy --csv
python tune_screen.py delay reclaim_strategy

# Tier 3 selection: who is worth a deep dive tonight, per the tier-2 gate.
# Reads output/latest_hits.json only. No network, no Discord.
python research_report.py candidates            # the configured gate
python research_report.py candidates --all      # every tier-1 hit
python research_report.py candidates --json     # machine-readable
python research_report.py auto-prompt           # nightly prompt; exit 1 = nothing to do
python research_report.py context MSFT          # the data bundle for one ticker
```

To test alert formatting/sending without spamming the channel, monkeypatch
`run_scanners.get_sp500_tickers` (small ticker list) and
`run_scanners.send_discord_alert` (print instead of POST) in a scratchpad
script and call `run_scanners.main()` — or confirm with the user before any
real send.

## Architecture

- **`run_scanners.py` is the entry point** (also what `run_scanner.bat` /
  Task Scheduler invokes). It downloads the universe once, then runs every
  module in its `SCANNERS` registry. Each screen module implements the same
  contract, consumed by the registry loop:
  - `CONFIG_KEY` — its section name in `config.json`; the section's
    `enabled` flag skips the screen. **`enabled` gates the nightly alert
    only** — `backtest_universe.py` and `tune_screen.py` deliberately ignore
    it (their own selectors are `backtest.screens`/`--screens` and the screen
    argument), because a screen gets switched off precisely when it is
    underperforming, which is when you most need to measure it. Reclaim has
    been off since 2026-07-26 for exactly that reason (excess −1.65 vs the
    random-entry baseline) and must stay measurable.
  - `scan(data, strategy) -> ScanResult` (dataclass in `scanner_common.py`:
    title, one ticker-indexed `hits` DataFrame, the strategy dict).
  - `EMBED_COLOR` + `describe_hit(row, strategy)` — the screen-specific parts
    of its Discord embed cards (`scanner_common.build_embeds` assembles the
    cards: one per signal, fundamentals as inline fields, the chart bound in
    via `attachment://<filename>`).
  - `plot_hit(data, ticker, strategy, chart_cfg, out_path)` — per-hit alert
    chart (delegates to `charts.py`).
  Adding a scanner = new module + one entry in `SCANNERS` + a config section.
- **The `compute_*` function in each screen module is the single source of
  truth** for its condition math (`breakout_scanner.compute_signals`,
  `sma_pullback.compute_pullback_signals`,
  `sma_reclaim.compute_reclaim_signals`). Fully vectorized: every
  input/output is a `(days, tickers)` DataFrame, no per-ticker loops. The
  production scan evaluates only the last row; the backtests
  (`backtest_breakout.py`, `backtest_pullback.py`, `backtest_reclaim.py`)
  evaluate every historical day for one ticker and import the compute
  functions — never reimplement the condition math there.
- **One signal list per screen, two tiers.** There is no separate near-miss
  list: `find_*` returns a single `hits` frame with a **`Setup`** column
  (`full`/`partial`) and **`Missing`** (the failing test, `""` when full),
  sorted full-first. Alongside `compute_*` each module exposes:
  - `required_history(strategy)` — total lookback, used by its own "NO signal
    can ever fire" warning and the universe backtest's warm-up;
  - `partial_mask(data, signals, strategy)` — the *partial-tier* combination
    logic, vectorized over all days like `compute_*`;
  - `fires_mask(...)` = `signal | partial_mask` — every day the screen alerts
    on. **The pullback screen is the exception**: it stays strict, so its
    `fires_mask` is just `signal` and its `partial_mask` (touch-but-no-fire) is
    **backtest-only**, a control cohort that is never alerted.
  `find_*` takes `.iloc[-1]` of `fires_mask`, so production and the backtest
  share one definition. `missing_reason(...)` (breakout: positional args;
  reclaim: a calc-table row) names the failing leg.
  Merging the tiers was driven by the backtest: partial breakout setups
  returned +3.10% vs +0.67% for full ones over 30 days, so suppressing them was
  discarding the better cohort. Keep the tier recorded — it is the only thing
  that preserves that distinction.
- **`backtest_universe.py` is the profit backtest** — the whole universe ×
  all history, one fixed-horizon trade per signal. It reuses the production
  `compute_*` + `fires_mask` unchanged (its own `SCREENS` registry pairs
  each module with its compute function) — one cohort per screen, or `full`
  vs `partial` under `--split-by-tier`/`backtest.split_by_tier` — and reuses
  `scanner_common.download_price_data`/`warmup_months`; the simulation itself
  is a few `shift()`s (`forward_trades`), never a loop. **Two timing knobs,
  swept as a cross product**: `entry_delay_days` (x) = extra trading days to
  wait *beyond the earliest tradeable bar* (x=0 buys as soon as possible, so
  look-ahead is impossible by construction), `holding_days` (y) = trading days
  held after the entry day. Excursion (MFE/MAE) windows must roll *then* shift —
  shifting first needs rows before the signal day and silently NaNs out the
  start of the frame — and span the bars the position is actually open (y+1 for
  `next_open`, y for `signal_close`, whose entry bar's own range predates the
  closing entry). Two things keep an 18-cell sweep fast and its outputs usable:
  stats come from **`cohort_values`** (raw arrays at the mask's cells) rather
  than `collect_trades`, because materializing the entry/exit *date* frames per
  cell dominates the cost; and per-trade rows are written for the single
  **`backtest.detail`** cell only (a full grid is ~1M rows). `detail` is an
  explicit config pair, never "first in the list", so widening the swept lists
  can't silently move the detailed table. The **baseline is computed once per
  holding period** and reused across delays — a random entry has no signal to be
  delayed from — which is what makes `excess_%` comparable down a grid column.
  Its price panel is cached to
  `output/backtest_universe_cache.pkl`; `--refresh` re-downloads. The
  caveats (survivorship bias from using today's index members, no costs, no
  dividends, clustered/overlapping trades) are printed in the run header and
  documented in the README — keep them there, don't quietly drop them.
- **Shared infra lives in `scanner_common.py`** (config, Wikipedia tickers,
  bulk/single downloads, fundamentals, Discord send — content text + embed
  cards, batched automatically under Discord's 10-embed / 10-file / ~6000
  embed-char per-message limits) and **`charts.py`**
  (validated palette + the per-screen chart builders used by both the alert
  and the backtests).
- **`tests/` asserts invariants, never snapshots** — see `tests/CLAUDE.md`,
  which loads whenever you work under that directory.
- **`tune_screen.py` is the threshold tuner** — see the `tune-thresholds`
  skill for how to sweep a screen and read its `protected` column.
- **Tier 2's verdict is computed exactly once**, by
  `scanner_common.annotate_quality`, in `run_scanners.main()` right after the
  fundamentals join and *before* the hand-off is written. `build_embeds` reads
  the recorded `Quality` column (falling back to `quality_check` only when the
  column is absent), so the ⭐ badge and `latest_hits.json` cannot disagree.
  Don't reintroduce a second call site — the bug this fixed was exactly that:
  the badge was computed inside `build_embeds`, which runs *after*
  `write_latest_hits`, so the hand-off never carried it and tier 3 was blind to
  tier 2. **Absent quality columns mean "not evaluated", not "failed"** — the
  helper is a deliberate no-op when the quality layer is off.
- **`research_report.py` is tier 3's deterministic half** (`list_candidates` +
  the gate, the quant score, `report_dir`/`write_report`, the Discord verdict
  posts); the synthesis itself is the `deep-dive` skill, i.e. Claude reasoning,
  not a function — which is why the nightly run invokes `claude -p` from
  `run_deepdive.bat` rather than calling Python. `research_collect.py` (Yahoo)
  and `sec.py` (EDGAR) are its collectors; `RESEARCH_DATA.md` maps what each
  source can and cannot supply. **The IBKR MCP tools must stay in
  `run_deepdive.bat`'s `--allowedTools`** — under `--permission-mode dontAsk` an
  un-allowed tool is refused *silently*, which would drop the moat/competitor
  section from every report with no error to explain it.
- **The signal history CSV is rewritten, not appended** (`archive_scan`).
  The fundamentals columns are config-driven display labels, so retuning
  `config.json` changes the schema and a blind append would misalign every later
  row; de-duplicating on `(scan_date, config_key, ticker)` also makes re-running
  a day idempotent. Cheap at a handful of rows a night — don't "optimize" it
  into an append.
- **Every generated file goes to `output/`** via
  `scanner_common.output_dir()` — logs, `latest_hits.json`, the cached price
  panel, all backtest tables/charts, the tier-3 reports (`output/reports/`) and
  the signal history (`output/history/`). There are no exceptions; Google Drive
  was one until 2026-07-26 and was removed, partly because Claude Code cannot
  `--add-dir` a path containing the U+200F mark in that folder's name. The
  project root holds only inputs (code, `config.json`, docs); `output/` is
  gitignored as one directory. Never write an artifact with
  `Path(__file__).parent` — that is exactly what this replaced. `config.json`
  deliberately still stores **bare filenames** (`backtest_universe_cache.pkl`,
  `reports`, `history`, …) which `output_dir()` resolves, so an absolute path in
  config keeps overriding it (which is how the tests redirect them) and no
  sub-paths leak into config. `PROJECT_ROOT` assumes the code is flat in the
  repo root — the one line to revisit if modules ever move into a package. All
  three `.bat` files must keep their `if not exist output md output` guard:
  `cmd` expands `>>` before Python runs, so `output_dir()`'s `mkdir` would be
  too late.
- **Data layout contract**: `yf.download(..., group_by="column",
  auto_adjust=False)` giving a `(Field, Ticker)` column MultiIndex
  (`data["Close"]["AAPL"]`). Single-ticker frames must be normalized to this
  shape (`scanner_common.download_history` does it;
  `scanner_common.single_ticker_panel` slices one ticker back out of a bulk
  frame).
- **Candle conditions read `Open`/`High`/`Low`** (all single-day, so no
  `download_period` impact), but each screen wants a different shape:
  - Breakout **C4**: a strong **green** candle,
    `close > (1 + min_candle_body_pct) * open` (positive threshold enforces
    green + a minimum body). A `full` breakout needs all four conditions, a
    `partial` one exactly 3 of 4.
  - Reclaim **R5** is **gap-aware** (`is_strong_day`): the body route above
    **OR**, when `min_day_gain_pct` is not null, `close/prev_close - 1 >=
    min_day_gain_pct` while still closing green. A body compares close to
    *open*, so it cannot see an overnight gap — and the biggest reclaims gap
    (META 2023-02-02 closed **+23.3%** on the day with a body of only
    **+2.9%**, having opened +19.8% up; `min_candle_body_pct: 0.03` rejected
    it). The gap route can only ever *add* signals, so leaving
    `min_day_gain_pct: null` reproduces the old behaviour exactly. Don't
    "simplify" R5 back to a body-only test.
  - Pullback **T4** (`sma_pullback.py`): the *opposite* — a small-body,
    long-tailed reversal bar at the touch: `|close-open|/open <=
    max_candle_body_pct` AND `(high-low)/open >= min_candle_range_pct` (body
    red or green). Gated by `require_reversal_candle` (default true); it's a
    strict filter, so most ordinary touch days stop qualifying when it's on.
- **Rolling-window conventions differ by design**: the breakout screen uses
  `shift(1)` so the prior range/volume baseline excludes the current day; the
  pullback and reclaim screens' SMA *includes* the current day
  (charting-standard "touch/cross of the line") while their persistence
  counts (time above/below the SMA) and the reclaim screen's volume baseline
  use `shift(1)`. All are intentional — don't "fix" any of them.
- **The `partial` tier differs by screen.** Breakout = exactly 3 of 4
  conditions; a failing *breakout* leg is additionally filtered to closes
  within `near_miss_max_gap_pct` of the required level (alert only — the
  single-ticker backtest log intentionally shows all 3-of-4 days). Reclaim = a
  genuine fresh cross **out of a long downtrend** (both mandatory, as for a
  full setup) with **1 or 2** of the remaining confirmations (volume, candle,
  and slope when enabled) failing — 0 = full, 3+ dropped; `missing_reasons`
  joins every failing test. The pullback screen never alerts a partial; its
  `partial_mask` is the backtest's touch-but-no-fire control cohort (measured
  at +1.57% vs a +2.10% random-entry baseline — i.e. its filters earn their
  keep).
- **Fundamentals are two config-driven layers** (`scanner_common.py`):
  `fields` = snapshot values from Yahoo `info` (`percent_fields` lists keys
  Yahoo returns as fractions, ×100 before display — but `dividendYield` is
  already a percentage, keep it OUT of `percent_fields`); `statements` =
  per-year metrics computed from `Ticker.income_stmt`/`cash_flow`/
  `balance_sheet` (FCF, OpM, PM per fiscal year; ROE with `info` fallback;
  ROIC = EBIT×(1−tax rate)/Invested Capital). Multi-year values are stored
  as `[(fiscal_year, value)]` lists in the joined DataFrame;
  `fundamentals_fields()` renders them as embed fields. Missing statement
  rows (banks lack Operating Income; a bank's hugely negative FCF is
  genuine) render as `n/a` — same tolerance rule as `info` fields.
  `fetch_fundamentals` also adds a reserved `COMPANY_COL` ("Company")
  column (`info` longName/shortName) that `build_embeds` puts in each card
  title as `TICKER (Company Name)`; it is not a config field and never
  renders as an inline field.
- **Quality badge = tier 2** (`fundamentals.quality` in config):
  `quality_failures(row, fund_cfg)` in `scanner_common.py` evaluates `rules` —
  keyed by the same `info`/metric keys as the display config, each
  `{min, max, increasing}`; strict compares, latest fiscal year for multi-year
  metrics, missing value = rule fails (banks can never pass). `annotate_quality`
  records the result, `build_embeds` prefixes the configured `badge` to any
  passing signal card's title, every screen automatically — **including
  `partial` setups** (it grades fundamentals, which are independent of setup
  completeness; this changed when the tiers merged). The rule set is
  intentionally strict: **most tickers fail at least one rule**, which is why
  the tier-3 gate is a soft, configurable one (`research.auto.gate`:
  `quality_pass` | `all`) — a hard gate would routinely leave tier 3 with
  nothing. If `candidates` keeps coming back empty, that is the rule set doing
  its job, and the fix is a config decision (loosen the rules or switch the gate
  to `all`), not a code change.
- **The tier 1→2→3 hand-off contract** is `output/latest_hits.json`: per screen
  a `config_key`/`title`/`strategy` and a `hits` map of ticker → row, where the
  row is whatever the screen's frame held plus `Setup`/`Missing` (tier 1),
  `Quality`/`Quality Missing` (tier 2), `Company`, and the joined fundamentals
  under their **display labels**. `_json_safe` makes it JSON-native: NaN→`null`,
  numpy scalars→Python, and the `[(year, value)]` series→nested arrays.
  `research_report.quality_failures` still grades a round-tripped row because it
  looks values up by label and indexes the series positionally — that is what
  keeps *archived* scans readable after the format moves on, and
  `list_candidates` relies on it to recompute a verdict a pre-2026-07-26
  hand-off never recorded. Only **enabled** screens appear: the hand-off follows
  the alert, unlike the backtest and tuner.
- **Everything tunable lives in `config.json`** (per-screen strategy
  sections, charts, Discord, fundamentals fields) and all user-facing text
  (alert lines, backtest STEP logs, chart labels) is built from those values
  at runtime — never hardcode a threshold or a literal like "150d SMA".
  Adding a metric to alerts is a config-only change.

## Constraints and gotchas

- `data.download_period` must exceed each screen's total lookback
  (breakout: `consolidation_window_days`; pullback: `sma_days +
  trend_lookback_days`; reclaim: `sma_days + below_lookback_days`; ~21
  trading days per calendar month), or the rolling windows never fill and
  that screen can never fire (each prints a warning). Check this whenever
  any of those values change. `backtest_universe.py` is exempt: it derives its
  own download length from `backtest.years` + `required_history()`.
- The user frequently hand-tunes strategy values in `config.json` between
  sessions — read the file for current values; don't trust README's table or
  prior conversation, and don't revert their changes.
- `config.json` holds the **live Discord webhook URL** and is committed on
  purpose (private repo). Never paste it into issues/PRs or public output.
- Nightly run: Task Scheduler task **"SP500 Breakout Scanner"**, Mon–Fri 23:30
  Israel time → `run_scanner.bat` (tiers 1+2 → `output/scanner_log.txt`) →
  `run_deepdive.bat` (tier 3 → `output/deepdive_log.txt`). One task, all three
  tiers. README's "Nightly schedule" section has the exact
  `Register-ScheduledTask` command and diagnostics; keep it in sync if the
  schedule changes. Result code `3221225786` in `Get-ScheduledTaskInfo` means
  the run was killed mid-scan (usually PC shutdown), and that night's alert is
  simply lost — though the signals themselves are archived before tier 3
  starts. **The task's `ExecutionTimeLimit` has to cover tier 3**: it was
  registered at `PT30M`, which is ample for the scan but will guillotine a run
  of five deep-dives.
- Yahoo quirks the code already tolerates (don't "fix" into hard failures):
  missing `info` fundamentals render as `n/a` (e.g. negative-equity companies
  have no Debt/Equity); individual ticker download failures just drop out of
  the scan; Wikipedia scraping needs the browser-like User-Agent header;
  tickers use `-` not `.` (BRK-B).
- **Unsettled last bar.** Yahoo serves a session it has not settled as an
  ordinary daily row — Open/High/Low/Volume present, **`Close` null** — and it
  sometimes *reverts an already-settled bar to that form hours later* (seen
  2026-07-24: 503 of 504 closes withdrawn on the Saturday, after Friday's
  nightly run had scanned that same bar fine). Every condition compares against
  `Close`, so the row makes each test NaN, `fillna(False)` reads that as "no
  signal", and **the scan reports a confident 0 signals on data that looks
  complete** — no error, no warning. `scanner_common.drop_unsettled_tail` (a
  *fraction*-of-tickers test, since individual tickers legitimately go missing)
  strips such trailing bars in both download functions and on every cache load,
  printing which bar it dropped; that also makes an intraday run scan the last
  settled session instead of a partial one. If a zero-signal night ever looks
  wrong, check `output/scanner_log.txt` for that WARNING first, and never assume
  a NaN close means a failed condition.
- Windows box, Microsoft Store Python 3.13 (`python` on PATH). yfinance's
  progress bar is disabled for non-TTY output so `output/scanner_log.txt` stays
  readable. matplotlib uses the Agg backend (set in `charts.py`).
