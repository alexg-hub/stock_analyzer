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
    title, ticker-indexed `hits`/`near` DataFrames, the strategy dict).
  - `EMBED_COLOR` + `describe_hit(row, strategy)` — the screen-specific parts
    of its Discord embed cards (`scanner_common.build_embeds` assembles the
    cards: one per hit/near-miss, fundamentals as inline fields, the hit's
    chart bound in via `attachment://<filename>`).
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
- **Green-candle condition** on both the breakout and reclaim screens:
  `close > (1 + min_candle_body_pct) * open` (the only condition reading the
  `Open` field; a positive threshold enforces both green and a minimum body,
  single-day so no `download_period` impact). Breakout calls it C4 (hits need
  all four conditions; near-miss = **exactly 3 of 4**). Reclaim calls it R5
  and folds it into the signal the same way.
- **Rolling-window conventions differ by design**: the breakout screen uses
  `shift(1)` so the prior range/volume baseline excludes the current day; the
  pullback and reclaim screens' SMA *includes* the current day
  (charting-standard "touch/cross of the line") while their persistence
  counts (time above/below the SMA) and the reclaim screen's volume baseline
  use `shift(1)`. All are intentional — don't "fix" any of them.
- **Near-misses** differ by screen. Breakout = exactly 3 of 4 conditions true
  on scan day; breakout-condition failures are additionally filtered to closes
  within `near_miss_max_gap_pct` of the required level (production alert only;
  the backtest log intentionally shows all 3-of-4 days). Reclaim = a genuine
  fresh cross today **out of a long downtrend** (both mandatory, as for a hit)
  with **1 or 2** of the remaining confirmations (volume, candle, and slope
  when enabled) failing — 0 = hit, 3+ dropped; its reason string joins all
  failing tests (`cross_miss_reasons`). The pullback
  screen has no production near-miss list; its backtest logs touch days that
  failed and why.
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
  configured `badge` to a passing **hit** card's title (hits only, every
  screen automatically; near-misses never get it). The default rule set is
  intentionally strict — most tickers fail at least one rule.
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
  any of those values change.
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
