# S&P 500 Breakout Scanner

Scans all S&P 500 stocks for an upward breakout from a horizontal consolidation
range and sends the results to Discord via a webhook. Runs nightly via Windows
Task Scheduler.

## The setup it detects

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

Breakout tickers are enriched with fundamentals from Yahoo Finance (currently
P/E, PEG, Total Debt/Equity, and latest-quarter YoY revenue growth — the list
is config-driven). The alert also lists **near-miss candidates** — tickers that
passed exactly two of the three conditions — with the failed condition
explained and the same fundamentals attached. Near-misses that failed only the
breakout condition are reported only when the close is within
`near_miss_max_gap_pct` of the required level, so routine volume spikes deep
inside a range don't flood the alert.

## Usage

```
pip install -r requirements.txt
python breakout_scanner.py        # full S&P 500 scan + Discord alert
python backtest_breakout.py       # historical validation on one ticker
```

### Backtest

`backtest_breakout.py` runs the exact same condition math (shared
`compute_signals()`) over a historical window for one ticker, with
step-by-step logging of every calculation, near-miss analysis, a per-day
calculation table (`backtest_<ticker>.csv`), and a price/volume chart
(`backtest_<ticker>.png`):

```
python backtest_breakout.py --ticker JNJ --start 2025-01-01 --end 2025-10-31
```

## Configuration (`config.json`)

| Key | Current | Meaning |
|---|---|---|
| `discord.webhook_url` | (set) | Discord webhook URL (Server Settings → Integrations → Webhooks → New Webhook → Copy URL). Until set, the alert prints to the console instead. |
| `discord.send_message_when_no_breakouts` | `true` | Also send a "nothing found" message |
| `data.download_period` | `2y` | History to download — must exceed the consolidation window (~21 trading days per calendar month) or the rolling window never fills and no signal can ever fire; the scanner warns if violated |
| `strategy.consolidation_window_days` | `312` | Length of the prior consolidation window (trading days) |
| `strategy.max_consolidation_range_pct` | `0.32` | Max high-to-low range of that window |
| `strategy.breakout_multiplier` | `1.01` | Close must exceed this × the range high (1.01 = +1%) |
| `strategy.volume_sma_days` | `30` | Lookback for the average-volume baseline |
| `strategy.volume_surge_multiplier` | `1.1` | Required volume vs. that baseline |
| `strategy.near_miss_max_gap_pct` | `0.05` | Report breakout-condition near-misses only if the close is within this fraction of the required level |
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

- The screen is fully vectorized: one bulk `yf.download` for all ~503 tickers,
  then rolling-window math across the whole universe at once. A full scan takes
  about a minute.
- Occasional per-ticker download failures (delistings, transient Yahoo errors)
  are tolerated — those tickers simply drop out of the scan.
- Yahoo legitimately lacks some fundamentals for some companies (e.g. no P/E
  when trailing earnings are negative, no Debt/Equity when equity is negative);
  those show as `n/a` in the alert.
- Not investment advice; breakout screens produce false positives. Do your own
  research before trading.
