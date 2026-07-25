# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A set of S&P 500 screens that run nightly on this Windows machine via Task
Scheduler and push one combined alert (text + per-hit chart images) to a
Discord channel, plus single-ticker historical backtesters for validating
each screen. No test suite; validation is done by running the backtests
against known cases (JNJ 2025 breakout, MSFT 2024 SMA pullbacks, META 2023
SMA reclaim).

## Commands

```powershell
pip install -r requirements.txt

# Full production scan (all screens). WARNING: sends a real Discord message
# to the user's channel (config.json contains a live webhook URL). Don't run
# it casually.
python run_scanners.py

# Historical validation of the same logic on one ticker (no Discord send);
# each writes a backtest_*.csv and backtest_*.png (gitignored)
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
    `enabled` flag skips the screen.
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
  `backtest_universe_cache.pkl` (gitignored); `--refresh` re-downloads. The
  caveats (survivorship bias from using today's index members, no costs, no
  dividends, clustered/overlapping trades) are printed in the run header and
  documented in the README — keep them there, don't quietly drop them.
- **Shared infra lives in `scanner_common.py`** (config, Wikipedia tickers,
  bulk/single downloads, fundamentals, Discord send — content text + embed
  cards, batched automatically under Discord's 10-embed / 10-file / ~6000
  embed-char per-message limits) and **`charts.py`**
  (validated palette + the per-screen chart builders used by both the alert
  and the backtests).
- **Data layout contract**: `yf.download(..., group_by="column",
  auto_adjust=False)` giving a `(Field, Ticker)` column MultiIndex
  (`data["Close"]["AAPL"]`). Single-ticker frames must be normalized to this
  shape (`scanner_common.download_history` does it;
  `scanner_common.single_ticker_panel` slices one ticker back out of a bulk
  frame).
- **Candle conditions read `Open`/`High`/`Low`** (all single-day, so no
  `download_period` impact), but each screen wants a different shape:
  - Breakout **C4** and reclaim **R5**: a strong **green** candle,
    `close > (1 + min_candle_body_pct) * open` (positive threshold enforces
    green + a minimum body). A `full` breakout needs all four conditions, a
    `partial` one exactly 3 of 4. Reclaim folds R5 into the signal.
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
- **Quality badge** (`fundamentals.quality` in config): `quality_check(row,
  fund_cfg)` in `scanner_common.py` evaluates `rules` — keyed by the same
  `info`/metric keys as the display config, each `{min, max, increasing}`;
  strict compares, latest fiscal year for multi-year metrics, missing value
  = rule fails (banks can never pass). `build_embeds` prefixes the
  configured `badge` to any passing signal card's title, every screen
  automatically — **including `partial` setups** (it grades fundamentals, which
  are independent of setup completeness; this changed when the tiers merged).
  The default rule set is intentionally strict — most tickers fail at least one
  rule.
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
  Israel time → `run_scanner.bat` → output appended to `scanner_log.txt`
  (gitignored). README's "Nightly schedule" section has the exact
  `Register-ScheduledTask` command and diagnostics; keep it in sync if the
  schedule changes. Result code `3221225786` in `Get-ScheduledTaskInfo` means
  the run was killed mid-scan (usually PC shutdown), and that night's alert is
  simply lost.
- Yahoo quirks the code already tolerates (don't "fix" into hard failures):
  missing `info` fundamentals render as `n/a` (e.g. negative-equity companies
  have no Debt/Equity); individual ticker download failures just drop out of
  the scan; Wikipedia scraping needs the browser-like User-Agent header;
  tickers use `-` not `.` (BRK-B).
- Windows box, Microsoft Store Python 3.13 (`python` on PATH). yfinance's
  progress bar is disabled for non-TTY output so `scanner_log.txt` stays
  readable. matplotlib uses the Agg backend (set in `charts.py`).
