# S&P 500 Breakout Scanner

Scans all S&P 500 stocks for an upward breakout from a horizontal consolidation
range and sends the results to Discord via a webhook.

## The setup it detects

All three conditions must be true on the most recent trading day:

1. **Horizontal movement** — over the previous 126 trading days (excluding
   today), `(max High − min Low) / min Low ≤ 30%`.
2. **Upward breakout** — today's Close is above `breakout_multiplier ×` that
   window's max High (1.01 = at least 1% above it, filtering marginal pokes).
3. **Volume surge** — today's Volume ≥ 1.3 × the average volume of the previous
   30 trading days.

Breakout tickers are enriched with P/E, PEG, and Total Debt/Equity from Yahoo
Finance. The alert also lists near-miss candidates -- tickers that passed two of
the three conditions -- with the failed condition explained.

Note: `data.download_period` must cover more than
`strategy.consolidation_window_days` trading days (~21 per calendar month), or
the rolling window never fills and no signal can ever fire; the scanner warns if
this is violated.

## Usage

```
pip install -r requirements.txt
python breakout_scanner.py
```

## Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `discord.webhook_url` | placeholder | Discord webhook URL (Server Settings → Integrations → Webhooks → New Webhook → Copy URL). Until set, the alert prints to the console instead. |
| `discord.send_message_when_no_breakouts` | `true` | Also send a "no breakouts" message |
| `data.download_period` | `1y` | History to download (must exceed the consolidation window) |
| `strategy.consolidation_window_days` | `126` | Length of the prior consolidation window (trading days) |
| `strategy.max_consolidation_range_pct` | `0.30` | Max high-to-low range of that window |
| `strategy.breakout_multiplier` | `1.01` | Close must exceed this × the range high (1.01 = +1%) |
| `strategy.volume_sma_days` | `30` | Lookback for the average-volume baseline |
| `strategy.volume_surge_multiplier` | `1.3` | Required volume vs. that baseline |
| `strategy.near_miss_max_gap_pct` | `0.05` | Near-misses failing only the breakout condition are reported only if the close is within this fraction of the required level |
| `fundamentals.enabled` | `true` | Fetch P/E, PEG, Debt/Equity for hits |

## Notes

- The screen is fully vectorized: one bulk `yf.download` for all ~503 tickers,
  then rolling-window math across the whole universe at once. A full scan takes
  about a minute.
- Occasional per-ticker download failures (delistings, transient Yahoo errors)
  are tolerated — those tickers simply drop out of the scan.
- Not investment advice; breakout screens produce false positives. Do your own
  research before trading.
