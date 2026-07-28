---
name: universe-backtest
description: Run or modify backtest_universe.py — the universe-wide profit backtest that trades every screen's signals across all history and sweeps a wait × hold grid. Use when asked to measure whether a screen makes money, to run/interpret the universe backtest, to change its entry or holding logic, or when touching forward_trades, cohort_values, the excursion (MFE/MAE) windows, or the random-entry baseline.
---

# Universe backtest

The single-ticker backtests prove the condition math fires on a known case.
This answers the different question: **if every signal had been traded, would
it have made money?** The whole universe × all history, one fixed-horizon trade
per signal.

```powershell
python backtest_universe.py
python backtest_universe.py --entry-delay 0 --holding-days 30   # one cell
python backtest_universe.py --screens breakout_strategy --entry signal_close
python backtest_universe.py --split-by-tier                     # full vs partial
python backtest_universe.py --refresh                           # re-download the panel
```

Interactively you may use the `stock_analyzer` MCP tool `backtest_universe`
instead — the same script with the same flags, run as a background job so the
session stays responsive; poll `job_status`. Read the resulting
`output/backtest_universe_summary.csv` with `Read`.

Caches the price panel to `output/backtest_universe_cache.pkl`, so re-runs
after a config tweak take seconds. `tune_screen.py` and the tests read that same
cache — run this once before either.

## How it works

It reuses the **production** `compute_*` + `fires_mask` unchanged (its own
`SCREENS` registry pairs each module with its compute function) — one cohort per
screen, or `full` vs `partial` under `--split-by-tier` / `backtest.split_by_tier`
— and reuses `scanner_common.download_price_data` / `warmup_months`. Never
reimplement the condition math here. The simulation itself is a few `shift()`s
(`forward_trades`), never a loop.

**`enabled: false` is deliberately ignored** — the selector is
`backtest.screens` / `--screens`. A screen gets switched off precisely when it
is underperforming, which is when you most need to measure it.

## Two timing knobs, swept as a cross product

- `entry_delay_days` (x) — extra trading days to wait *beyond the earliest
  tradeable bar*. x=0 buys as soon as possible, so look-ahead is impossible by
  construction.
- `holding_days` (y) — trading days held after the entry day.

## Gotchas that are easy to reintroduce

- **Excursion (MFE/MAE) windows must roll *then* shift.** Shifting first needs
  rows before the signal day and silently NaNs out the start of the frame. The
  window spans the bars the position is actually open: y+1 for `next_open`, y for
  `signal_close` — whose entry bar's own range predates the closing entry.
- **Stats come from `cohort_values`** (raw arrays at the mask's cells), not
  `collect_trades`: materializing the entry/exit *date* frames per cell dominates
  the cost of an 18-cell sweep.
- **Per-trade rows are written for the single `backtest.detail` cell only** — a
  full grid is ~1M rows. `detail` is an explicit config pair, never "first in the
  list", so widening the swept lists can't silently move the detailed table.
- **The baseline is computed once per holding period** and reused across delays —
  a random entry has no signal to be delayed from. That is what makes `excess_%`
  comparable down a grid column.

## Caveats — keep them visible

Survivorship bias (it screens today's index members across all history), no
costs, no dividends, clustered and overlapping trades. These are printed in the
run header and documented in the README. Don't quietly drop them, and don't
present a return from here as a tradeable estimate.
