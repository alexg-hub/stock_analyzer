# S&P 500 Breakout Scanner

Scans all S&P 500 stocks for an upward breakout from a horizontal consolidation
range and sends the results to Discord via a webhook.

## The setup it detects

All three conditions must be true on the most recent trading day:

1. **Horizontal movement** — over the previous 126 trading days (excluding
   today), `(max High − min Low) / min Low ≤ 20%`.
2. **Upward breakout** — today's Close is strictly above that window's max High.
3. **Volume surge** — today's Volume ≥ 1.5 × the average volume of the previous
   30 trading days.

Breakout tickers are enriched with P/E, PEG, and Total Debt/Equity from Yahoo
Finance.

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
| `strategy.max_consolidation_range_pct` | `0.20` | Max high-to-low range of that window |
| `strategy.volume_sma_days` | `30` | Lookback for the average-volume baseline |
| `strategy.volume_surge_multiplier` | `1.5` | Required volume vs. that baseline |
| `fundamentals.enabled` | `true` | Fetch P/E, PEG, Debt/Equity for hits |

## Notes

- The screen is fully vectorized: one bulk `yf.download` for all ~503 tickers,
  then rolling-window math across the whole universe at once. A full scan takes
  about a minute.
- Occasional per-ticker download failures (delistings, transient Yahoo errors)
  are tolerated — those tickers simply drop out of the scan.
- Not investment advice; breakout screens produce false positives. Do your own
  research before trading.
