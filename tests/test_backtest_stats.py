"""The universe backtest's statistics on real data.

Three things, all invariants:

  1. `cohort_values` (the sweep's fast path) must produce byte-identical stats
     to the `collect_trades` path it replaced -- otherwise the 18-cell grid and
     the detailed table are measuring different things.
  2. Real trade rows must re-derive from the raw panel: prices, dates and the
     excursion window, at a nonzero delay (where an off-by-one would hide).
  3. The trades CSV must reconcile with the summary grid and satisfy its
     structural invariants (skipped if no CSV has been generated yet).

Uses the cached panel; never downloads.
"""

import sys

import pandas as pd

from _harness import Checks, cached_panel_or_skip, screens

from backtest_universe import (
    cohort_values,
    collect_trades,
    describe,
    forward_trades,
    stats_from,
)
from scanner_common import output_dir

c = Checks("backtest statistics")
panel, cfg = cached_panel_or_skip()
bt = cfg["backtest"]
benchmark = bt.get("benchmark_ticker")
universe = [t for t in panel["Close"].columns if t != benchmark]
start = panel.index[-1] - pd.DateOffset(years=bt.get("years", 3))

masks = {}
for module, compute, strategy in screens(cfg):
    masks[(module.CONFIG_KEY, "signal")] = module.fires_mask(
        panel, compute(panel, strategy), strategy).fillna(False)[universe]

STAT_KEYS = ["signals", "evaluable", "distinct_dates", "mean_%", "median_%",
             "win_rate_%", "std", "p10", "p90", "best", "worst",
             "mean_MFE_%", "mean_MAE_%"]

# --------------------------------------------------------------------------
c.section("the fast stats path agrees with the collect_trades path")
for d, h in ((0, 30), (3, 30), (5, 10), (2, 60)):
    fwd = forward_trades(panel, h, bt["entry"], True, delay=d)
    frame = collect_trades(masks, fwd, start)
    for key, mask in masks.items():
        fast = stats_from(cohort_values(mask, fwd, start))
        sub = frame[(frame["screen"] == key[0]) & (frame["cohort"] == key[1])]
        slow = describe(sub["return_pct"], n_signals=len(sub),
                        n_dates=sub["signal_date"].nunique(),
                        mfe=sub["mfe_pct"], mae=sub["mae_pct"])
        bad = [k for k in STAT_KEYS
               if not (pd.isna(fast[k]) and pd.isna(slow[k]))
               and abs(float(fast[k]) - float(slow[k])) > 1e-9]
        c.ok(f"x={d} y={h} {key[0].replace('_strategy', '')}", not bad,
             f"n={fast['evaluable']} mean={fast['mean_%']:+.4f}"
             + (f" mismatched: {bad}" if bad else ""))

# --------------------------------------------------------------------------
DELAY, HOLD = 3, 30
c.section(f"real rows re-derive from the panel (x={DELAY}, y={HOLD})")
fwd = forward_trades(panel, HOLD, "next_open", True, delay=DELAY)
frame = collect_trades(masks, fwd, start).dropna(subset=["return_pct"])
idx = panel.index
if frame.empty:
    c.ok("there are trades to check", False, "no closed trades in the window")
else:
    for _, t in frame.sample(min(8, len(frame)), random_state=1).iterrows():
        tk = t["ticker"]
        i = idx.get_loc(t["signal_date"])
        e, x = i + 1 + DELAY, i + 1 + DELAY + HOLD
        checks = {
            "entry bar": idx[e] == t["entry_date"],
            "exit bar": idx[x] == t["exit_date"],
            "entry price": abs(panel["Open"][tk].iloc[e] - t["entry_price"]) < 5e-9,
            "exit price": abs(panel["Close"][tk].iloc[x] - t["exit_price"]) < 5e-9,
            "return": abs(100 * (panel["Close"][tk].iloc[x]
                                 / panel["Open"][tk].iloc[e] - 1)
                          - t["return_pct"]) < 1e-9,
            "mfe": abs(100 * (panel["High"][tk].iloc[e:x + 1].max()
                              / panel["Open"][tk].iloc[e] - 1)
                       - t["mfe_pct"]) < 1e-9,
            "mae": abs(100 * (panel["Low"][tk].iloc[e:x + 1].min()
                              / panel["Open"][tk].iloc[e] - 1)
                       - t["mae_pct"]) < 1e-9,
        }
        bad = [k for k, ok in checks.items() if not ok]
        c.ok(f"{tk} {t['signal_date'].date()}", not bad,
             f"{t['return_pct']:+.2f}%" + (f" mismatched: {bad}" if bad else ""))

    pos = {dt: n for n, dt in enumerate(idx)}
    gaps = frame.apply(
        lambda r: pos[r["entry_date"]] - pos[r["signal_date"]], axis=1)
    c.ok(f"all {len(frame)} entries are exactly {DELAY + 1} bars after the signal",
         (gaps == DELAY + 1).all(), f"observed {sorted(gaps.unique())}")

# --------------------------------------------------------------------------
c.section("the trades CSV reconciles with the summary grid")
trades_csv = output_dir() / bt.get("output", {}).get(
    "trades_csv", "backtest_universe_trades.csv")
summary_csv = output_dir() / bt.get("output", {}).get(
    "summary_csv", "backtest_universe_summary.csv")
if not (trades_csv.exists() and summary_csv.exists()):
    print(f"  (skipped: run `python backtest_universe.py` to generate "
          f"{trades_csv.name})")
else:
    trades = pd.read_csv(trades_csv,
                         parse_dates=["signal_date", "entry_date", "exit_date"])
    summary = pd.read_csv(summary_csv)
    d = int(trades["delay"].iloc[0])
    h = int(trades["horizon"].iloc[0])
    c.ok("the CSV holds exactly one (delay, holding) cell",
         (trades["delay"] == d).all() and (trades["horizon"] == h).all(),
         f"wait={d}, hold={h}")
    cohorts = summary[(summary["cohort"] != "-") & (summary["delay"] == d)
                      & (summary["horizon"] == h)]
    closed = trades[trades["status"] == "closed"]
    opened = trades[trades["status"] == "open"]
    c.ok("entry is after the signal (closed)",
         (closed["entry_date"] > closed["signal_date"]).all())
    c.ok("exit is after the entry (closed)",
         (closed["exit_date"] > closed["entry_date"]).all())
    c.ok("prices are positive (closed)",
         ((closed["entry_price"] > 0) & (closed["exit_price"] > 0)).all())
    c.ok("MFE >= MAE (closed)", (closed["mfe_pct"] >= closed["mae_pct"]).all())
    c.ok("the return lies within [MAE, MFE] (closed)",
         ((closed["return_pct"] <= closed["mfe_pct"] + 1e-6)
          & (closed["return_pct"] >= closed["mae_pct"] - 1e-6)).all())
    c.ok("open trades carry no return", opened["return_pct"].isna().all())
    c.ok("closed count == summed 'evaluable'",
         len(closed) == int(cohorts["evaluable"].sum()),
         f"{len(closed)} vs {int(cohorts['evaluable'].sum())}")
    c.ok("total count == summed 'signals'",
         len(trades) == int(cohorts["signals"].sum()),
         f"{len(trades)} vs {int(cohorts['signals'].sum())}")
    c.ok("no duplicated (screen, cohort, ticker, signal_date)",
         not trades.duplicated(
             ["screen", "cohort", "ticker", "signal_date"]).any())

sys.exit(c.finish())
