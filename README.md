# S&P 500 Scanners

Nightly scans of all S&P 500 stocks (via Windows Task Scheduler) with results
sent to Discord via a webhook — a short summary line per screen, then one
**embed card per ticker**: colored side-bar (orange = breakout, blue =
pullback, green = reclaim, gray = near-miss), a title with the ticker and
company name, the signal description, a fundamentals field grid, and the
ticker's chart rendered inside the card (for hits and near-misses).

Current screens:

1. **Breakout from consolidation** — upward breakout from a horizontal range.
2. **SMA pullback** — a stock in a year-long uptrend pulling back to its
   rising 150-day SMA.
3. **SMA reclaim** — a stock that spent most of the last year *below* its
   200-day SMA crossing back above it on volume (trend-reversal /
   Weinstein "Stage 2" entry).

## Code layout

| File | Role |
|---|---|
| `run_scanners.py` | Entry point: one bulk download → every enabled screen → one Discord alert |
| `breakout_scanner.py` | Breakout screen module (condition math, alert section, hit chart) |
| `sma_pullback.py` | SMA-pullback screen module (same shape) |
| `sma_reclaim.py` | SMA-reclaim screen module (same shape) |
| `scanner_common.py` | Shared infra: config, tickers, downloads, fundamentals, Discord |
| `charts.py` | Shared chart rendering (palette + per-screen chart builders) |
| `backtest_breakout.py` / `backtest_pullback.py` / `backtest_reclaim.py` | Single-ticker historical validators |
| `backtest_universe.py` | Universe-wide profit backtest: every screen × all history, buy the trigger / sell N days later |

Adding a new scanner = new module exposing `CONFIG_KEY`, `scan()`,
`EMBED_COLOR` + `describe_hit()`, `plot_hit()` + one entry in
`run_scanners.SCANNERS` + a config section with an `enabled` flag.

## Screen 1: breakout from consolidation

All four conditions must be true on the most recent trading day (parameter
names refer to the `breakout_strategy` section of `config.json`):

1. **Horizontal movement** — over the previous `consolidation_window_days`
   trading days (excluding today),
   `(max High − min Low) / min Low ≤ max_consolidation_range_pct`.
2. **Upward breakout** — today's Close is above `breakout_multiplier ×` that
   window's max High (e.g. 1.01 = at least 1% above it, filtering marginal
   pokes above the range).
3. **Volume surge** — today's Volume ≥ `volume_surge_multiplier ×` the average
   volume of the previous `volume_sma_days` trading days.
4. **Long green candle** — today's Close is above
   `(1 + min_candle_body_pct) ×` the day's Open, so the breakout day itself
   closes strongly (green, with a body of at least `min_candle_body_pct`)
   instead of gapping up and fading to a weak or red close.

The alert also lists **near-miss candidates** — tickers that passed exactly
three of the four conditions — with the failed condition explained. Near-misses
that failed only the breakout condition are reported only when the close is
within `near_miss_max_gap_pct` of the required level, so routine volume spikes
deep inside a range don't flood the alert.

## Screen 2: pullback to a rising SMA

All conditions must be true on the most recent trading day (parameter names
refer to the `pullback_strategy` section):

1. **Rising SMA** — the `sma_days` SMA of Close is higher than it was
   `sma_slope_lookback_days` trading days ago.
2. **Sustained uptrend** — the Close was above the SMA on at least
   `min_days_above_sma_pct` of the previous `trend_lookback_days` trading
   days, so today's touch is the exception in a year-long uptrend.
3. **Touch** — today's Close is within `touch_band_pct` of the SMA (either
   side).
4. **Reversal candle** (optional, `require_reversal_candle`) — the touch day
   is a small-body, long-tailed bar: body `|Close − Open| ≤
   max_candle_body_pct` of the Open **and** range `High − Low ≥
   min_candle_range_pct` of the Open. The body may be red or green — the
   shape (open and close close together, high and low far apart) is the
   "buyers stepped in at support" signal. Set `require_reversal_candle` to
   `false` to drop it. This is a strict filter; most ordinary touch days
   don't qualify.
5. **Fresh entry** (optional, `alert_only_on_band_entry`) — yesterday's close
   was still above the band, so the signal fires only on the day the pullback
   actually reaches the SMA instead of re-alerting every night the stock sits
   on it.

Unlike the breakout screen's prior-window (shift-by-one) convention, the SMA
here includes the current day — that is the charting-standard SMA a "touch of
the 150-day line" refers to.

## Screen 3: reclaim of a long-term SMA after a downtrend

The mirror image of screen 2 — instead of a dip in an uptrend, it catches
the *birth* of a new uptrend: a stock that lived below its 200-day SMA for
most of a year crossing back above it. All conditions must be true on the
most recent trading day (parameter names refer to the `reclaim_strategy`
section):

1. **Above the cross level** — today's Close is above `(1 +
   cross_margin_pct) ×` the `sma_days` SMA, so a marginal poke over the
   line doesn't count.
2. **Long prior downtrend** — the Close was below the SMA on at least
   `min_days_below_pct` of the previous `below_lookback_days` trading days,
   so the reclaim is an event, not chop around a flat SMA.
3. **Volume confirmation** — today's Volume ≥ `volume_surge_multiplier ×`
   the average of the previous `volume_sma_days` days (a reclaim on dead
   volume usually fails).
4. **SMA slope floor** (optional, off by default) — when
   `min_sma_slope_pct` is set (not `null`), the SMA's change over
   `sma_slope_lookback_days` must be at least that fraction; filters
   knife-catching in stocks still in freefall, at the cost of later entry.
5. **Long green candle** — today's Close is above `(1 + min_candle_body_pct)
   ×` the day's Open, so the reclaim day itself closes strongly (green, body
   ≥ `min_candle_body_pct`) instead of a weak or red cross.
6. **Fresh cross** (optional, `alert_only_on_cross`) — yesterday's close
   was not yet above the level, so a stock that stays above it doesn't
   re-alert every night.

The alert also lists **near-miss candidates** — tickers that genuinely
crossed above the level today (a fresh cross) *out of a real downtrend*
(the downtrend is required, same as for a hit) but had **one or two** of the
remaining confirmations (volume, green candle, and the slope floor when
enabled) fail, with the failure(s) explained. A cross that wasn't from a
long downtrend, or that misses on too many confirmations, is dropped.

First crosses of a long-term SMA are whipsaw-prone by nature — expect some
signals to fail back below the line; this is a watchlist alert, not an
entry system.

## Fundamentals

Every hit and near-miss ticker gets a fundamentals field grid on its embed
card, from two config-driven layers (`fundamentals` in `config.json`):

- **Snapshot fields** from Yahoo `info` (`fields` map): currently P/E, PEG,
  Total Debt/Equity, latest-quarter YoY revenue growth, dividend yield,
  payout ratio.
- **Statement metrics** computed from the last `statements.years` annual
  reports (income statement / cash flow / balance sheet):
  - FCF per year (cash-flow "Free Cash Flow")
  - Operating margin and profit margin per year
  - ROE = Net Income / Stockholders Equity (falls back to Yahoo's `info`
    value when statement rows are missing)
  - ROIC = EBIT × (1 − tax rate) / Invested Capital

Metrics Yahoo doesn't provide for a company (banks have no operating income,
negative-equity companies no D/E, ...) show as `n/a`. A bank's FCF can be a
large negative number — that is Yahoo's genuine figure (deposit/loan flows
dominate bank cash-flow statements), not a bug.

### Quality badge

A hit whose fundamentals pass **every** rule in `fundamentals.quality.rules`
gets the configured badge (default ⭐) in front of its card title. This is
computed in the shared embed builder, so it applies to the hits of every
screen — current and future — automatically.

Rules are keyed by the same keys as the display config (Yahoo `info` keys
for snapshot fields, metric keys for statement metrics) and support:

- `min` / `max` — strict compare against the value (the **latest fiscal
  year** for multi-year metrics like FCF and margins);
- `increasing: true` — the latest fiscal year must be above the previous
  one (requires ≥ 2 years of data).

A missing value fails its rule — unverifiable quality doesn't earn the
badge (so banks, with no operating income, can never carry it). The default
rule set is deliberately strict (P/E < 35, PEG < 2, D/E < 75, revenue
growth > 10%, a dividend, payout < 50%, ROE > 15%, ROIC > 15%, OpM > 20%
and rising, PM > 15% and rising, positive and rising FCF) — most S&P 500
names fail at least one rule; edit `config.json` to loosen it.

## Usage

```
pip install -r requirements.txt
python run_scanners.py            # full S&P 500 scan + Discord alert
python backtest_breakout.py       # historical validation, breakout screen
python backtest_pullback.py       # historical validation, pullback screen
python backtest_reclaim.py        # historical validation, reclaim screen
python backtest_universe.py       # universe-wide profit backtest (all screens)
```

### Backtests

Each backtest runs the exact production condition math (the shared
`compute_*` functions) over a historical window for one ticker, with
step-by-step logging of every calculation, near-miss analysis, a per-day
calculation table (CSV), and a chart (PNG):

```
python backtest_breakout.py --ticker JNJ  --start 2025-01-01 --end 2025-10-31
python backtest_pullback.py --ticker MSFT --start 2024-01-01 --end 2025-06-30
python backtest_reclaim.py  --ticker META --start 2023-01-01 --end 2023-12-31
```

`run_backtests.bat` runs all three default validation cases in one go and
writes their combined step-by-step output to `backtest_log.txt` (gitignored,
overwritten each run) — separate from the nightly production `scanner_log.txt`.

### Universe backtest — did the screens make money?

The single-ticker backtests prove the *math* fires on a known case.
`backtest_universe.py` answers the different question: **if every signal had
been bought and sold `holding_days` later, what was the profit?**

```
python backtest_universe.py                          # config defaults (3y, h=30)
python backtest_universe.py --holding-days 10,30,60  # several horizons in one run
python backtest_universe.py --screens breakout_strategy --entry signal_close
python backtest_universe.py --years 5 --refresh      # longer window, fresh download
```

It downloads the universe once (analysis window + the longest screen's
rolling-window warm-up) and **caches it to `backtest_universe_cache.pkl`**, so
re-running after a `config.json` tweak takes seconds; `--refresh` re-downloads.
It adds no condition math — every screen's `compute_*` already returns
`(days, tickers)` frames, so the whole `signal` frame is masked against a
vectorized forward-return matrix.

Per screen it measures two cohorts: **hits** and the screen's non-firing
comparison set (the production **near-miss** list for breakout/reclaim;
**touch-no-fire** days for pullback, which has no production near-miss list).
Each is compared against two baselines: **random entry** (the same
forward-return matrix over *all* stock-days — the bar a screen must clear) and
**SPY buy-and-hold**. Outputs: a console stats table + per-signal-year
breakdown, `backtest_universe_trades.csv` (every trade, with `status`
`closed`/`open`), `backtest_universe_summary.csv`, and `backtest_universe.png`.

Entry conventions: `next_open` (default — the signal is only known after the
close, so the earliest tradeable price is the next open) or `signal_close`
(the price shown in the alert). `holding_days` counts **trading** days held
*after* the entry day. `mfe_pct`/`mae_pct` are the best/worst excursion while
the position was open — useful for judging whether a stop or target would help.

**Read these caveats before believing any number** (they are also printed in
the run header):

- **Survivorship bias** — the universe is *today's* S&P 500, so companies
  dropped, acquired or delisted during the window are absent. Results are
  biased upward. This is a screen-*comparison* tool, not a tradeable backtest.
- **No costs, no dividends** — no commission, spread or slippage; returns are
  price-only (splits handled, dividends ignored).
- **No position sizing or capital limit** — every signal is an independent
  equal-weight trade, even when dozens fire on the same day.
- **Signals cluster in time**, so trades overlap and share market beta:
  per-trade statistics are *not* independent samples (`distinct_dates` shows
  how concentrated a cohort is). Don't read a t-statistic off them.

## Configuration (`config.json`)

| Key | Current | Meaning |
|---|---|---|
| `discord.webhook_url` | (set) | Discord webhook URL (Server Settings → Integrations → Webhooks → New Webhook → Copy URL). Until set, the alert prints to the console instead. |
| `discord.send_message_when_no_breakouts` | `true` | Also send a "nothing found" message |
| `data.download_period` | `2y` | History to download — must exceed each screen's total lookback (~21 trading days per calendar month) or its rolling windows never fill and no signal can ever fire; the scanners warn if violated |
| `breakout_strategy.enabled` | `true` | Run the breakout screen |
| `breakout_strategy.consolidation_window_days` | `312` | Length of the prior consolidation window (trading days) |
| `breakout_strategy.max_consolidation_range_pct` | `0.32` | Max high-to-low range of that window |
| `breakout_strategy.breakout_multiplier` | `1.01` | Close must exceed this × the range high (1.01 = +1%) |
| `breakout_strategy.min_candle_body_pct` | `0.01` | Breakout day's Close must exceed its Open by at least this fraction (green candle with a body ≥ 1%); `0.0` = any green candle |
| `breakout_strategy.volume_sma_days` | `30` | Lookback for the average-volume baseline |
| `breakout_strategy.volume_surge_multiplier` | `1.1` | Required volume vs. that baseline |
| `breakout_strategy.near_miss_max_gap_pct` | `0.05` | Report breakout-condition near-misses only if the close is within this fraction of the required level |
| `pullback_strategy.enabled` | `true` | Run the SMA-pullback screen |
| `pullback_strategy.sma_days` | `150` | SMA length (trading days) |
| `pullback_strategy.touch_band_pct` | `0.02` | "Touch" = close within this fraction of the SMA |
| `pullback_strategy.trend_lookback_days` | `252` | Uptrend persistence lookback (~1 year) |
| `pullback_strategy.sma_slope_lookback_days` | `63` | SMA must be higher than this many days ago |
| `pullback_strategy.min_days_above_sma_pct` | `0.9` | Min fraction of the lookback the close spent above the SMA |
| `pullback_strategy.max_candle_body_pct` | `0.01` | Touch day's body `\|Close−Open\|` must be ≤ this fraction of the Open (small body) |
| `pullback_strategy.min_candle_range_pct` | `0.03` | Touch day's range `High−Low` must be ≥ this fraction of the Open (long tails) |
| `pullback_strategy.require_reversal_candle` | `true` | Require the small-body/long-tailed touch candle; `false` disables it |
| `pullback_strategy.alert_only_on_band_entry` | `true` | Alert only on the day the close enters the band from above |
| `reclaim_strategy.enabled` | `true` | Run the SMA-reclaim screen |
| `reclaim_strategy.sma_days` | `200` | SMA length (trading days) |
| `reclaim_strategy.below_lookback_days` | `200` | Downtrend-persistence lookback |
| `reclaim_strategy.min_days_below_pct` | `0.8` | Min fraction of the lookback the close spent below the SMA |
| `reclaim_strategy.cross_margin_pct` | `0.01` | Close must exceed the SMA by this fraction (1.01 × SMA) |
| `reclaim_strategy.min_candle_body_pct` | `0.01` | Reclaim day's Close must exceed its Open by at least this fraction (green candle, body ≥ 1%); `0.0` = any green candle |
| `reclaim_strategy.volume_sma_days` | `30` | Lookback for the average-volume baseline |
| `reclaim_strategy.volume_surge_multiplier` | `1.2` | Required volume vs. that baseline |
| `reclaim_strategy.sma_slope_lookback_days` | `63` | Lookback for the optional SMA-slope floor |
| `reclaim_strategy.min_sma_slope_pct` | `null` | Optional slope floor (e.g. `-0.02`); `null` = off |
| `reclaim_strategy.alert_only_on_cross` | `true` | Alert only on the day the close first crosses the level |
| `charts.enabled` | `true` | Attach a chart image per hit to the Discord alert |
| `charts.near_miss_charts` | `true` | Also attach a chart to near-miss cards (set `false` for hits-only charts) |
| `charts.lookback_days` | `250` | Trading days shown in alert charts |
| `charts.dpi` | `120` | Alert-chart resolution |
| `backtest.years` | `3` | Length of the analysis window; the download adds the warm-up the longest screen lookback needs |
| `backtest.holding_days` | `[30]` | Holding periods in **trading** days, held after the entry day; a list runs several horizons in one pass |
| `backtest.entry` | `next_open` | `next_open` (buy the open after the signal) or `signal_close` (buy the trigger close) |
| `backtest.include_near_misses` | `true` | Also measure each screen's non-firing cohort (near-miss / touch-no-fire) |
| `backtest.measure_excursions` | `true` | Compute MFE/MAE (best/worst excursion while the trade was open) |
| `backtest.benchmark_ticker` | `SPY` | Buy-and-hold benchmark, downloaded alongside the universe |
| `backtest.cache_path` | `backtest_universe_cache.pkl` | Cached price panel (gitignored); `--refresh` re-downloads |
| `backtest.cache_max_age_days` | `1` | Reuse the cache only while it is younger than this |
| `backtest.screens` | all three | Config keys of the screens to include |
| `backtest.output.*` | — | Trades CSV, summary CSV, chart path + DPI (chart names get an `_h<N>` suffix when several horizons run) |
| `fundamentals.enabled` | `true` | Fetch fundamentals for hits and near-misses |
| `fundamentals.fields` | 6 fields | Yahoo `info` key → display label; add/remove entries to change what the alert shows |
| `fundamentals.percent_fields` | `revenueGrowth`, `payoutRatio` | Fields Yahoo returns as fractions, converted to % (note: `dividendYield` is *not* here — Yahoo already returns it as a %) |
| `fundamentals.statements.enabled` | `true` | Compute per-year metrics from the annual statements |
| `fundamentals.statements.years` | `2` | How many recent fiscal years to show |
| `fundamentals.statements.metrics` | 5 metrics | Metric → display label; remove an entry to drop it from the alert |
| `fundamentals.quality.enabled` | `true` | Evaluate the quality rules and badge passing hits |
| `fundamentals.quality.badge` | `⭐` | Prefix added to a passing hit's card title |
| `fundamentals.quality.rules` | 11 rules | Per-metric `min`/`max`/`increasing` thresholds; a hit must pass **all** of them to get the badge |

## Nightly schedule (Windows Task Scheduler)

The scan runs Mon–Fri at **23:30 Israel time** (~30 min after the 16:00 ET US
market close) via the task **"SP500 Breakout Scanner"**, which executes
`run_scanner.bat` and appends all output to `scanner_log.txt`.

To (re)create the task, run in PowerShell:

```powershell
$action   = New-ScheduledTaskAction -Execute "C:\Users\Lenovo\CC\stock_analyzer\run_scanner.bat" -WorkingDirectory "C:\Users\Lenovo\CC\stock_analyzer"
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 23:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "SP500 Breakout Scanner" -Action $action -Trigger $trigger -Settings $settings -Description "Scans S&P 500 for breakouts from consolidation ~30 min after US market close and alerts via Discord webhook."
```

Useful commands:

```powershell
Get-ScheduledTaskInfo -TaskName "SP500 Breakout Scanner"   # last/next run + result code
Start-ScheduledTask   -TaskName "SP500 Breakout Scanner"   # trigger a run right now
Get-Content scanner_log.txt -Tail 20                       # inspect the last run's output
```

Notes:

- `StartWhenAvailable` runs the scan at next boot if the PC was off at 23:30,
  and `WakeToRun` wakes it from sleep — but a run that *started* and was then
  interrupted (e.g. shutting the PC down at ~23:31) is **not** retried, and
  that night's alert is lost. Avoid shutting down between ~23:25 and ~23:35.
- A result code of `0` in `Get-ScheduledTaskInfo` means success;
  `3221225786` (0xC000013A) means the run was terminated mid-scan.

## Notes

- Both screens are fully vectorized: one bulk `yf.download` for all ~503
  tickers, then rolling-window math across the whole universe at once. A full
  scan takes about a minute.
- Discord allows at most 10 embeds / 10 attachments / ~6000 embed characters
  per webhook message; the alert is split into multiple messages
  automatically if more tickers fire.
- Occasional per-ticker download failures (delistings, transient Yahoo errors)
  are tolerated — those tickers simply drop out of the scan.
- Yahoo legitimately lacks some fundamentals for some companies (e.g. no P/E
  when trailing earnings are negative, no Debt/Equity when equity is negative);
  those show as `n/a` in the alert.
- Not investment advice; screens produce false positives. Do your own
  research before trading.
