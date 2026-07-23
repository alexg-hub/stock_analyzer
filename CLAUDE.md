# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A set of S&P 500 screens that run nightly on this Windows machine via Task
Scheduler and push one combined alert (text + per-hit chart images) to a
Discord channel, plus single-ticker historical backtesters for validating
each screen. No test suite; validation is done by running the backtests
against known cases (JNJ 2025 breakout, MSFT 2024 SMA pullbacks).

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
  - `format_section(result, fund_labels)` — its block of the Discord message.
  - `plot_hit(data, ticker, strategy, chart_cfg, out_path)` — per-hit alert
    chart (delegates to `charts.py`).
  Adding a scanner = new module + one entry in `SCANNERS` + a config section.
- **The `compute_*` function in each screen module is the single source of
  truth** for its condition math (`breakout_scanner.compute_signals`,
  `sma_pullback.compute_pullback_signals`). Fully vectorized: every
  input/output is a `(days, tickers)` DataFrame, no per-ticker loops. The
  production scan evaluates only the last row; the backtests
  (`backtest_breakout.py`, `backtest_pullback.py`) evaluate every historical
  day for one ticker and import the compute functions — never reimplement the
  condition math there.
- **Shared infra lives in `scanner_common.py`** (config, Wikipedia tickers,
  bulk/single downloads, fundamentals, Discord send with chart attachments —
  max 10 files per webhook message, batched automatically) and **`charts.py`**
  (validated palette + the per-screen chart builders used by both the alert
  and the backtests).
- **Data layout contract**: `yf.download(..., group_by="column",
  auto_adjust=False)` giving a `(Field, Ticker)` column MultiIndex
  (`data["Close"]["AAPL"]`). Single-ticker frames must be normalized to this
  shape (`scanner_common.download_history` does it;
  `scanner_common.single_ticker_panel` slices one ticker back out of a bulk
  frame).
- **Rolling-window conventions differ by design**: the breakout screen uses
  `shift(1)` so the prior range/volume baseline excludes the current day; the
  pullback screen's SMA *includes* the current day (charting-standard "touch
  of the 150-day line") while its trend-persistence count uses `shift(1)`.
  Both are intentional — don't "fix" either.
- **Near-misses** (breakout screen only) = exactly 2 of 3 conditions true on
  scan day. Breakout-only failures are additionally filtered to closes within
  `near_miss_max_gap_pct` of the required level (production alert only; the
  backtest log intentionally shows all 2-of-3 days). The pullback screen has
  no production near-miss list; its backtest logs touch days that failed and
  why.
- **Everything tunable lives in `config.json`** (per-screen strategy
  sections, charts, Discord, fundamentals fields) and all user-facing text
  (alert lines, backtest STEP logs, chart labels) is built from those values
  at runtime — never hardcode a threshold or a literal like "150d SMA".
  `fundamentals.fields` maps Yahoo `info` keys → display labels;
  `percent_fields` lists keys Yahoo returns as fractions (×100 before
  display). Adding a metric to alerts is a config-only change.

## Constraints and gotchas

- `data.download_period` must exceed each screen's total lookback
  (breakout: `consolidation_window_days`; pullback: `sma_days +
  trend_lookback_days`; ~21 trading days per calendar month), or the rolling
  windows never fill and that screen can never fire (each prints a warning).
  Check this whenever any of those values change.
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
