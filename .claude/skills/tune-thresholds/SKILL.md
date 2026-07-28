---
name: tune-thresholds
description: Tune one screen's strategy thresholds with tune_screen.py — sweeping parameters over the cached price panel and scoring candidates against the random-entry baseline and the protected cases that must keep firing. Use when asked to tune, sweep, or retune a screen's config values (breakout/pullback/reclaim), or to interpret a sensitivity/grid/delay run.
---

# Threshold tuning

```powershell
# Reads the cached panel only -- run backtest_universe.py once first. No Discord.
python tune_screen.py sensitivity reclaim_strategy
python tune_screen.py grid breakout_strategy --csv
python tune_screen.py delay reclaim_strategy
```

Interactively you may use the `stock_analyzer` MCP tool `tune_screen` instead —
same script, run as a background job, and it checks for the cached panel up
front rather than dying minutes in. Read the results with `tune_results`'s CSV
via `Read`, or from the job's output.

- **`tune_screen.py` is the threshold tuner** — sweeps one screen's parameters
  (`sensitivity` one at a time / `grid` crossing 2-3 / `delay` wait×hold) over
  the **cached** panel via `backtest_universe.cached_panel()`, which raises
  rather than silently re-downloading 500 tickers. It reuses the production
  `compute_*` / `fires_mask` / `partial_mask` and `backtest_universe`'s
  `forward_trades` / `cohort_values` / `stats_from` — never reimplement either.
  **The `protected` column is the point**: `tuning.protected_cases` names setups
  that must keep firing (META 2023-02-02 etc.) and each candidate is graded
  `FULL`/`partial`/`MISSED`, because a config that scores well by dropping the
  wanted setups is not an improvement. Ranges, protected cases and grid axes all
  live in `config.json` → `tuning`; adding a value is a config edit, never a code
  edit. Measured so far: reclaim is **untunable** (all candidates below
  baseline, every tightening hurts) while breakout **does** respond
  (`breakout_multiplier` 1.01→1.02 lifts excess +0.62→+1.33 but demotes the JNJ
  protected case).

Note that `enabled: false` on a screen never hides it from tuning — the screen
argument selects it. A screen is switched off precisely when it is
underperforming, which is when you most need to measure it.
