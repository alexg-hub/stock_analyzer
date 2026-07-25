"""backtest_universe.forward_trades on a synthetic panel where every expected
value is computable by hand. Offline -- no cache, no network.

Semantics under test (both in TRADING days):
  delay (x)   extra bars to wait beyond the earliest tradeable bar
  holding (y) bars held after the entry bar

  next_open     signal i -> buy Open[i+1+x],  sell Close[i+1+x+y]
  signal_close  signal i -> buy Close[i+x],   sell Close[i+x+y]

This is where the two subtle bugs lived: the MFE/MAE window has to roll *then*
shift (shifting first needs bars before the signal and NaNs out the frame
start), and for `signal_close` the entry bar's own high/low predate the entry
so they must be excluded.
"""

import sys

import pandas as pd

from _harness import Checks

from backtest_universe import collect_trades, forward_trades

c = Checks("forward_trades arithmetic (synthetic panel)")

N = 12
idx = pd.bdate_range("2025-01-01", periods=N)
# Distinct, easy numbers: Close 100,110,120...  Open = Close-5,
# High = Close+3, Low = Open-4.
close = pd.Series([100 + 10 * i for i in range(N)], index=idx, dtype=float)
open_, high, low = close - 5, close + 3, close - 9
data = pd.concat(
    {"Open": open_.to_frame("T"), "High": high.to_frame("T"),
     "Low": low.to_frame("T"), "Close": close.to_frame("T"),
     "Volume": pd.Series(1e6, index=idx).to_frame("T")}, axis=1)


def cell(frames, key, i):
    return frames[key]["T"].iloc[i]


c.section("next_open, no delay: buy Open[i+1], sell Close[i+1+y]")
for h in (1, 3):
    f = forward_trades(data, holding=h, entry="next_open", excursions=True)
    i, e, x = 3, 4, 4 + h
    c.close(f"h={h} entry_price", cell(f, "entry_price", i), open_.iloc[e])
    c.close(f"h={h} exit_price", cell(f, "exit_price", i), close.iloc[x])
    c.close(f"h={h} return_pct", cell(f, "return_pct", i),
            100 * (close.iloc[x] / open_.iloc[e] - 1))
    c.same_date(f"h={h} entry_date", cell(f, "entry_date", i), idx[e])
    c.same_date(f"h={h} exit_date", cell(f, "exit_date", i), idx[x])
    # The position is open across bars e..x inclusive.
    c.close(f"h={h} mfe spans entry..exit", cell(f, "mfe_pct", i),
            100 * (high.iloc[e:x + 1].max() / open_.iloc[e] - 1))
    c.close(f"h={h} mae spans entry..exit", cell(f, "mae_pct", i),
            100 * (low.iloc[e:x + 1].min() / open_.iloc[e] - 1))

c.section("next_open with a delay: buy Open[i+1+x], sell Close[i+1+x+y]")
for d in (1, 2, 3):
    f = forward_trades(data, holding=2, entry="next_open", excursions=True, delay=d)
    i = 1
    e, x = i + 1 + d, i + 1 + d + 2
    c.close(f"x={d} entry_price", cell(f, "entry_price", i), open_.iloc[e])
    c.close(f"x={d} exit_price", cell(f, "exit_price", i), close.iloc[x])
    c.same_date(f"x={d} entry_date", cell(f, "entry_date", i), idx[e])
    c.same_date(f"x={d} exit_date", cell(f, "exit_date", i), idx[x])
    c.close(f"x={d} mfe spans entry..exit", cell(f, "mfe_pct", i),
            100 * (high.iloc[e:x + 1].max() / open_.iloc[e] - 1))

c.section("signal_close: buy Close[i+x], and the entry bar's own range is "
          "BEFORE the entry")
f = forward_trades(data, holding=3, entry="signal_close", excursions=True, delay=2)
i, e, x = 1, 3, 6
c.close("entry_price", cell(f, "entry_price", i), close.iloc[e])
c.close("exit_price", cell(f, "exit_price", i), close.iloc[x])
c.same_date("entry_date", cell(f, "entry_date", i), idx[e])
c.same_date("exit_date", cell(f, "exit_date", i), idx[x])
c.close("mfe excludes the entry bar", cell(f, "mfe_pct", i),
        100 * (high.iloc[e + 1:x + 1].max() / close.iloc[e] - 1))
c.close("mae excludes the entry bar", cell(f, "mae_pct", i),
        100 * (low.iloc[e + 1:x + 1].min() / close.iloc[e] - 1))

c.section("MFE/MAE must be defined from the very first row "
          "(regression: roll-then-shift)")
f = forward_trades(data, holding=3, entry="next_open", excursions=True)
c.ok("row 0 has an MFE", pd.notna(cell(f, "mfe_pct", 0)))
c.ok("row 0 has an MAE", pd.notna(cell(f, "mae_pct", 0)))

c.section("windows past the end of the data are unevaluable, not zero")
f1 = forward_trades(data, holding=1, entry="next_open", excursions=False)
c.ok("last row is NaN", pd.isna(cell(f1, "return_pct", N - 1)))
c.close("N-3 is a real number", cell(f1, "return_pct", N - 3),
        100 * (close.iloc[N - 1] / open_.iloc[N - 2] - 1))
prev = None
for d in (0, 1, 2, 3):
    n = int(forward_trades(data, holding=2, entry="next_open", excursions=False,
                           delay=d, dates=False)["return_pct"]["T"].notna().sum())
    c.ok(f"evaluable count shrinks at x={d}", prev is None or n < prev, f"{n}")
    prev = n

c.section("delay=0 is identical to the undelayed call")
a = forward_trades(data, holding=3, entry="next_open", excursions=True)
b = forward_trades(data, holding=3, entry="next_open", excursions=True, delay=0)
for key in ("entry_price", "exit_price", "return_pct", "mfe_pct", "mae_pct"):
    c.ok(f"{key} identical", a[key]["T"].equals(b[key]["T"]))

c.section("dates=False skips the expensive date frames")
lean = forward_trades(data, holding=2, entry="next_open", excursions=True,
                      dates=False)
c.ok("no entry_date key", "entry_date" not in lean)
c.ok("return_pct still present", "return_pct" in lean)

c.section("collect_trades maps a mask to rows and drops pre-window signals")
mask = pd.DataFrame(False, index=idx, columns=["T"])
mask.iloc[2] = True   # before `start` -> dropped
mask.iloc[6] = True
f3 = forward_trades(data, holding=3, entry="next_open", excursions=True)
frame = collect_trades({("test_strategy", "signal"): mask}, f3, start=idx[4])
c.ok("one row kept", len(frame) == 1, f"{len(frame)} rows")
if len(frame) == 1:
    c.same_date("kept the right signal date", frame["signal_date"].iloc[0], idx[6])
    c.close("row return matches the frame", frame["return_pct"].iloc[0],
            cell(f3, "return_pct", 6))

c.section("bad inputs are rejected")
for kwargs, what in (({"holding": 0}, "holding=0"),
                     ({"holding": 2, "delay": -1}, "delay=-1"),
                     ({"holding": 2, "entry": "nonsense"}, "unknown entry")):
    kwargs.setdefault("entry", "next_open")
    try:
        forward_trades(data, excursions=False, **kwargs)
        c.ok(f"{what} rejected", False, "it was accepted")
    except SystemExit:
        c.ok(f"{what} rejected", True)

sys.exit(c.finish())
