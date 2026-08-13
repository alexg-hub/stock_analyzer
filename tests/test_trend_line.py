"""The near-linear trend screen's fit and its event semantics.

Offline -- a synthetic panel where every expected value is known in closed
form, so nothing here depends on the cache or the network.

Four things, all invariants:

  1. The vectorized rolling fit is a real least-squares fit. It is computed
     from rolling sums rather than by fitting each window, so an off-by-one in
     the cross-term re-basing yields a *plausible* slope and raises nothing --
     numpy.polyfit is the only thing that catches it.
  2. Missing history stays missing. A window that is short, or that spans an
     interior Close hole, must be NaN rather than a fit over the bars that
     happened to survive.
  3. The screen is strict and its cohorts are disjoint -- what
     backtest_universe relies on to measure the two separately.
  4. The event semantics: one signal per trend, re-arming after a break; the
     angle ceiling disqualifying rather than qualifying; and the control
     cohort's width never changing the signal count.
"""

import sys

import numpy as np
import pandas as pd

from _harness import Checks

import trend_line as tl

c = Checks("near-linear trend screen (synthetic panel)")

WINDOW = 30
STRATEGY = {
    "trend_window_days": WINDOW,
    "fit_on_log_price": True,
    "min_annual_slope_pct": 0.20,
    "max_annual_slope_pct": 0.80,
    "min_r_squared": 0.85,
    "max_residual_pct": 0.08,
    "max_last_dev_pct": None,
    "max_partial_fails": 1,
    "alert_only_on_new_trend": True,
}

TRADING_DAYS = tl.TRADING_DAYS


def daily(annual_pct: float) -> float:
    """The per-bar log slope of a line rising `annual_pct` a year."""
    return np.log1p(annual_pct) / TRADING_DAYS


def panel(closes: dict[str, np.ndarray], start="2022-01-03") -> pd.DataFrame:
    """A (Field, Ticker) OHLCV panel from close series alone.

    The screen reads Close only; the other fields exist because the data
    layout contract says a panel has them.
    """
    n = max(len(v) for v in closes.values())
    idx = pd.bdate_range(start, periods=n)
    close = pd.DataFrame(closes, index=idx, dtype=float)
    return pd.concat(
        {"Open": close, "High": close, "Low": close, "Close": close,
         "Volume": close * 0 + 1e6}, axis=1)


def line(annual_pct: float, n: int, base: float = 100.0, start_i: int = 0):
    """`n` bars of a perfectly log-linear path at `annual_pct` a year."""
    return base * np.exp(daily(annual_pct) * (np.arange(n) + start_i))


def signals_for(data, strategy):
    return tl.compute_trend_signals(data, strategy)


# --------------------------------------------------------------------------
c.section("1. the rolling fit is a real least-squares fit")

rng = np.random.default_rng(20260813)
walk = {t: 50 + np.abs(rng.normal(0, 1, 400).cumsum()) + 20
        for t in ("W1", "W2", "W3")}
y = np.log(pd.DataFrame(walk, index=pd.bdate_range("2022-01-03", periods=400)))
fit = tl.rolling_fit(y, WINDOW)

worst = 0.0
for ticker in walk:
    for end in (WINDOW - 1, 137, 399):
        w = y[ticker].iloc[end - WINDOW + 1:end + 1].to_numpy()
        x = np.arange(WINDOW, dtype=float)
        slope, intercept = np.polyfit(x, w, 1)
        sse = ((w - (intercept + slope * x)) ** 2).sum()
        want = {
            "slope": slope,
            "intercept": intercept,
            "r2": 1 - sse / ((w - w.mean()) ** 2).sum(),
            "resid": np.sqrt(sse / (WINDOW - 2)),
        }
        for key, ref in want.items():
            worst = max(worst, abs(float(fit[key][ticker].iloc[end]) - ref))
c.ok("every slope/intercept/r2/residual matches numpy.polyfit",
     worst < 1e-9, f"largest disagreement {worst:.2e} over 9 windows")

perfect = panel({"L": line(0.40, 300)})
sig = signals_for(perfect, STRATEGY)
c.close("a perfect line recovers the annual slope it was built with",
        sig["annual_slope"]["L"].iloc[-1], 0.40, tol=1e-9)
c.close("...scores r2 exactly 1", sig["r2"]["L"].iloc[-1], 1.0, tol=1e-9)
c.ok("...and a residual of essentially zero",
     abs(float(sig["resid_pct"]["L"].iloc[-1])) < 1e-5,
     f"{float(sig['resid_pct']['L'].iloc[-1]):.2e}%")
c.close("...and sits on its own line (distance 0)",
        sig["dist_pct"]["L"].iloc[-1], 0.0, tol=1e-9)

# --------------------------------------------------------------------------
c.section("2. missing history stays missing")

c.ok(f"no fit before the window is full (first {WINDOW - 1} bars)",
     bool(sig["r2"]["L"].iloc[:WINDOW - 1].isna().all()))
c.ok("...and the first fit lands on the bar that fills it",
     bool(pd.notna(sig["r2"]["L"].iloc[WINDOW - 1])))

holed = line(0.40, 300)
holed[150] = np.nan
hole_sig = signals_for(panel({"L": holed}), STRATEGY)
spans = hole_sig["r2"]["L"].iloc[150:150 + WINDOW]
c.ok("an interior Close hole voids every window spanning it",
     bool(spans.isna().all()),
     f"bars 150..{150 + WINDOW - 1} are all NaN, not a fit over the survivors")
c.ok("...and the fit recovers once the hole rolls out of the window",
     bool(pd.notna(hole_sig["r2"]["L"].iloc[150 + WINDOW])))

# --------------------------------------------------------------------------
c.section("3. the screen is strict and its cohorts are disjoint")

# Four shapes at once: a clean trend, a noisy one, a flat line and a decline.
# The two trends open flat so that they visibly BEGIN inside the data -- a
# trend already running on the first fittable bar is deliberately never
# announced (section 7), so a fixture without the flat run signals nothing.
clean = np.concatenate([np.full(40, 100.0), line(0.40, 260)])
mixed = panel({"CLEAN": clean, "NOISY": clean * np.exp(rng.normal(0, 0.05, 300)),
               "FLAT": np.full(300, 100.0), "DOWN": line(-0.30, 300)})
msig = signals_for(mixed, STRATEGY)
fires = tl.fires_mask(mixed, msig, STRATEGY)
strict = msig["signal"].fillna(False)
part = tl.partial_mask(mixed, msig, STRATEGY)

c.ok("fires_mask is the strict signal -- partials are never alerted",
     bool((fires == strict).all().all()))
c.ok("the signal and control cohorts never overlap",
     bool(not (strict & part).any().any()))
c.ok("a flat line never signals", bool(not strict["FLAT"].any()))
c.ok("a decline never signals", bool(not strict["DOWN"].any()))
c.ok("the clean trend does signal", bool(strict["CLEAN"].any()))

hits = tl.find_trends(mixed, STRATEGY)
c.ok("find_trends reports every row as `full`",
     hits.empty or bool((hits["Setup"] == "full").all()),
     f"{len(hits)} hit(s) on the last bar")
c.ok("...and carries the tier columns every screen carries",
     {"Setup", "Missing"} <= set(hits.columns))

# --------------------------------------------------------------------------
c.section("4. event semantics: one signal per trend")

# Flat, then a long clean trend, then a decisive break, then a second trend.
seg = np.concatenate([
    np.full(40, 100.0),                       # warm-up, nothing to fit
    line(0.40, 90, base=100.0),               # trend 1
    line(-0.60, 60, base=100.0 * np.exp(daily(0.40) * 89)),   # the break
    line(0.40, 110, base=60.0),               # trend 2
])
episodes = panel({"E": seg})
esig = signals_for(episodes, STRATEGY)
n_sig = int(esig["signal"]["E"].sum())
c.ok("a trend that holds for months signals ONCE, not once a day",
     n_sig == 2, f"{n_sig} signal(s) across two separate trend episodes")
c.ok("...and the second one is the re-arm after the break",
     n_sig == 2 and bool(esig["signal"]["E"].idxmax()
                         < esig["signal"]["E"][::-1].idxmax()))

held = int(esig["shape_full"]["E"].sum())
c.ok("the underlying state is true on far more days than it signals",
     held > 5 * max(n_sig, 1), f"in shape on {held} bars, {n_sig} signals")

# --------------------------------------------------------------------------
c.section("5. the angle ceiling disqualifies, it does not qualify")

# A trend far above the ceiling that later decelerates through it. Folding the
# ceiling into the state freshness watches made this fire on the day it cooled
# past the bar -- a deceleration entry dressed as a trend entry.
fast = line(2.00, 150)
cooled = np.concatenate([np.full(40, 100.0), fast,
                         line(0.50, 150, base=fast[-1])])
cool_panel = panel({"C": cooled})
capped = signals_for(cool_panel, STRATEGY)
c.ok("a trend steeper than the ceiling never signals",
     int(capped["signal"]["C"].sum()) == 0,
     "including on the day it decelerates back through the bar")
c.ok("...while the same path signals with the ceiling removed",
     int(signals_for(cool_panel,
                     dict(STRATEGY, max_annual_slope_pct=None))["signal"]["C"]
         .sum()) > 0)
c.ok("the shape underneath it does qualify -- only the ceiling holds it back",
     bool(capped["shape_full"]["C"].any()))

# --------------------------------------------------------------------------
c.section("6. the control cohort's width never changes the signal")

base_n = int(signals_for(mixed, dict(STRATEGY, max_partial_fails=0))["signal"]
             .sum().sum())
counts = {k: int(signals_for(mixed, dict(STRATEGY, max_partial_fails=k))["signal"]
                 .sum().sum()) for k in (0, 1, 2, 3)}
c.ok("widening max_partial_fails leaves the signal count untouched",
     len(set(counts.values())) == 1, f"{counts} (each tier is armed separately)")
c.ok("...and max_partial_fails: 0 empties the control cohort",
     bool(not tl.partial_mask(
         mixed, signals_for(mixed, dict(STRATEGY, max_partial_fails=0)),
         dict(STRATEGY, max_partial_fails=0)).any().any()),
     f"the screen with no control; signal still {base_n}")

# --------------------------------------------------------------------------
c.section("7. a transition needs something to transition FROM")

need = tl.required_history(STRATEGY)
c.ok("a panel too short to fit anything cannot signal",
     int(signals_for(panel({"S": line(0.40, need)}),
                     STRATEGY)["signal"]["S"].sum()) == 0,
     f"required_history is {need} bars (window + the freshness comparison)")
c.ok("a trend already running when the data starts is never announced",
     int(sig["signal"]["L"].sum()) == 0,
     "the first fittable bar is where we started looking, not where it began")

# The reason that guard matters in production: an interior Close hole voids
# every window spanning it, so the bar where the fit comes back looks exactly
# like a fresh qualification. 116 of the 904 tickers on the 5y panel carry one.
punched = seg.copy()
in_trend_bar = int(np.flatnonzero(esig["shape_full"]["E"].to_numpy())[5])
punched[in_trend_bar] = np.nan
punched_sig = signals_for(panel({"E": punched}), STRATEGY)
c.ok("a hole inside a held trend does not manufacture a fresh one",
     int(punched_sig["signal"]["E"].sum()) <= n_sig,
     f"{int(punched_sig['signal']['E'].sum())} signal(s) with a hole at bar "
     f"{in_trend_bar}, {n_sig} without it")

sys.exit(c.finish())
