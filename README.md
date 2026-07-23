# S&P 500 Scanners

Nightly scans of all S&P 500 stocks (via Windows Task Scheduler) with results
sent to Discord via a webhook — one combined alert with a text section per
screen and a chart image attached for every hit.

Current screens:

1. **Breakout from consolidation** — upward breakout from a horizontal range.
2. **SMA pullback** — a stock in a year-long uptrend pulling back to its
   rising 150-day SMA.

## Code layout

| File | Role |
|---|---|
| `run_scanners.py` | Entry point: one bulk download → every enabled screen → one Discord alert |
| `breakout_scanner.py` | Breakout screen module (condition math, alert section, hit chart) |
| `sma_pullback.py` | SMA-pullback screen module (same shape) |
| `scanner_common.py` | Shared infra: config, tickers, downloads, fundamentals, Discord |
| `charts.py` | Shared chart rendering (palette + per-screen chart builders) |
| `backtest_breakout.py` / `backtest_pullback.py` | Single-ticker historical validators |

Adding a new scanner = new module exposing `CONFIG_KEY`, `scan()`,
`format_section()`, `plot_hit()` + one entry in `run_scanners.SCANNERS` + a
config section with an `enabled` flag.

## Screen 1: breakout from consolidation

All three conditions must be true on the most recent trading day (parameter
names refer to the `strategy` section of `config.json`):

1. **Horizontal movement** — over the previous `consolidation_window_days`
   trading days (excluding today),
   `(max High − min Low) / min Low ≤ max_consolidation_range_pct`.
2. **Upward breakout** — today's Close is above `breakout_multiplier ×` that
   window's max High (e.g. 1.01 = at least 1% above it, filtering marginal
   pokes above the range).
3. **Volume surge** — today's Volume ≥ `volume_surge_multiplier ×` the average
   volume of the previous `volume_sma_days` trading days.

The alert also lists **near-miss candidates** — tickers that passed exactly
two of the three conditions — with the failed condition explained. Near-misses
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
4. **Fresh entry** (optional, `alert_only_on_band_entry`) — yesterday's close
   was still above the band, so the signal fires only on the day the pullback
   actually reaches the SMA instead of re-alerting every night the stock sits
   on it.

Unlike the breakout screen's prior-window (shift-by-one) convention, the SMA
here includes the current day — that is the charting-standard SMA a "touch of
the 150-day line" refers to.

Hits of both screens are enriched with fundamentals from Yahoo Finance
(currently P/E, PEG, Total Debt/Equity, and latest-quarter YoY revenue
growth — the list is config-driven).

## Usage

```
pip install -r requirements.txt
python run_scanners.py            # full S&P 500 scan + Discord alert
python backtest_breakout.py       # historical validation, breakout screen
python backtest_pullback.py       # historical validation, pullback screen
```

### Backtests

Both backtests run the exact production condition math (the shared
`compute_*` functions) over a historical window for one ticker, with
step-by-step logging of every calculation, near-miss analysis, a per-day
calculation table (CSV), and a chart (PNG):

```
python backtest_breakout.py --ticker JNJ  --start 2025-01-01 --end 2025-10-31
python backtest_pullback.py --ticker MSFT --start 2024-01-01 --end 2025-06-30
```

## Configuration (`config.json`)

| Key | Current | Meaning |
|---|---|---|
| `discord.webhook_url` | (set) | Discord webhook URL (Server Settings → Integrations → Webhooks → New Webhook → Copy URL). Until set, the alert prints to the console instead. |
| `discord.send_message_when_no_breakouts` | `true` | Also send a "nothing found" message |
| `data.download_period` | `2y` | History to download — must exceed each screen's total lookback (~21 trading days per calendar month) or its rolling windows never fill and no signal can ever fire; the scanners warn if violated |
| `strategy.enabled` | `true` | Run the breakout screen |
| `strategy.consolidation_window_days` | `312` | Length of the prior consolidation window (trading days) |
| `strategy.max_consolidation_range_pct` | `0.32` | Max high-to-low range of that window |
| `strategy.breakout_multiplier` | `1.01` | Close must exceed this × the range high (1.01 = +1%) |
| `strategy.volume_sma_days` | `30` | Lookback for the average-volume baseline |
| `strategy.volume_surge_multiplier` | `1.1` | Required volume vs. that baseline |
| `strategy.near_miss_max_gap_pct` | `0.05` | Report breakout-condition near-misses only if the close is within this fraction of the required level |
| `pullback_strategy.enabled` | `true` | Run the SMA-pullback screen |
| `pullback_strategy.sma_days` | `150` | SMA length (trading days) |
| `pullback_strategy.touch_band_pct` | `0.02` | "Touch" = close within this fraction of the SMA |
| `pullback_strategy.trend_lookback_days` | `252` | Uptrend persistence lookback (~1 year) |
| `pullback_strategy.sma_slope_lookback_days` | `63` | SMA must be higher than this many days ago |
| `pullback_strategy.min_days_above_sma_pct` | `0.9` | Min fraction of the lookback the close spent above the SMA |
| `pullback_strategy.alert_only_on_band_entry` | `true` | Alert only on the day the close enters the band from above |
| `charts.enabled` | `true` | Attach a chart image per hit to the Discord alert |
| `charts.lookback_days` | `250` | Trading days shown in alert charts |
| `charts.dpi` | `120` | Alert-chart resolution |
| `fundamentals.enabled` | `true` | Fetch fundamentals for hits and near-misses |
| `fundamentals.fields` | 4 fields | Yahoo `info` key → display label; add/remove entries to change what the alert shows |
| `fundamentals.percent_fields` | `revenueGrowth` | Fields Yahoo returns as fractions, converted to % |

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
- Discord allows at most 10 attachments per webhook message; chart images are
  sent in batches automatically if more hits fire.
- Occasional per-ticker download failures (delistings, transient Yahoo errors)
  are tolerated — those tickers simply drop out of the scan.
- Yahoo legitimately lacks some fundamentals for some companies (e.g. no P/E
  when trailing earnings are negative, no Debt/Equity when equity is negative);
  those show as `n/a` in the alert.
- Not investment advice; screens produce false positives. Do your own
  research before trading.
