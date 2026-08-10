"""Offline -- the price-based risk metrics (`price_risk.py`).

Invariants, not recorded output:

  * **The formulas are pinned against hand-computed values.** Volatility,
    drawdown, beta and the Ulcer index are published definitions with a right
    answer, so unlike a tuned threshold they do not move when config is retuned.
    Same approach `test_derived.py` takes to Altman/Beneish and
    `portfolio_sim/stats.py` takes to the statistics.
  * **Too little history is `None`, never a number.** A three-year drawdown over
    forty bars is not a small drawdown, it is a wrong one -- and the registry
    reads None correctly (fails a gate, skipped in the score, never vetoes)
    while a plausible wrong number would be scored.
  * **Benchmark series are intersected, not zipped.** A beta against a benchmark
    offset by a day returns a plausible number and raises nothing, which is the
    single most dangerous failure mode in the file.
  * **Polarity**: every registered metric is oriented so that *bigger is worse*,
    matching the anchors in the registry. A metric that ran the other way would
    silently invert part of the risk axis.
  * The module does **no I/O at all**.

Fixtures are synthetic, because these are arithmetic identities: a constructed
series with round numbers is both sufficient and checkable by hand.
"""

import math

import numpy as np
import pandas as pd

from _harness import Checks

import price_risk

c = Checks("price risk")


def series(values, start="2020-01-01"):
    idx = pd.bdate_range(start, periods=len(values))
    return pd.Series([float(v) for v in values], index=idx)


# --------------------------------------------------------------------------
c.section("no I/O, and an empty input is empty output")

c.ok("the module imports nothing that can fetch",
     set(getattr(price_risk, name).__name__ for name in dir(price_risk)
         if type(getattr(price_risk, name)).__name__ == "module")
     <= {"numpy", "pandas", "math"},
     "a resolver that fetches breaks the one-fetch-per-ticker guarantee")

c.ok("no close series yields no metrics", price_risk.metrics(None) == {})
c.ok("an empty series yields no metrics",
     price_risk.metrics(pd.Series(dtype=float)) == {})
one_bar = price_risk.metrics(series([100]))
c.ok("a single bar answers nothing, rather than answering confidently",
     all(v is None for v in one_bar.values()),
     "; ".join(f"{k}={v}" for k, v in one_bar.items() if v is not None)
     or "all None")

# --------------------------------------------------------------------------
c.section("drawdown -- the peak-to-trough identity")

# Rises to 120, falls to 60, recovers to 90. Worst drawdown is 60/120-1 = -50%.
shape = ([100 + i for i in range(21)]          # 100 -> 120
         + [120 - 3 * i for i in range(1, 21)]  # 120 -> 60
         + [60 + 1.5 * i for i in range(1, 21)])  # 60 -> 90
dd = price_risk.max_drawdown_pct(series(shape), 252)
c.ok("max drawdown is the worst peak-to-trough decline, positive",
     dd is not None and abs(dd - 50.0) < 1e-9, f"{dd}")

c.ok("a monotonically rising series has no drawdown",
     price_risk.max_drawdown_pct(series(range(100, 200)), 252) == 0.0)

c.ok("a 3-year drawdown over one year of bars is None, not a small number",
     price_risk.max_drawdown_pct(series(shape), 252 * 3,
                                 min_obs=252) is None,
     f"{len(shape)} bars must not answer a 756-day question")

# The subtler version of the same trap, and the one that actually shipped: two
# years of bars clears a flat 252-bar floor while covering two thirds of a
# 756-bar window, so `max_drawdown_3y` was a 2-year drawdown wearing a 3-year
# label. The nightly scan carries exactly that much history (`download_period`
# is 2y), so this was live on the nightly path and not a hypothetical.
two_years = series(range(100, 100 + 504))
c.ok("two years of bars cannot answer the 3-year drawdown",
     price_risk.metrics(two_years)["max_drawdown_3y"] is None,
     "a mislabelled number is worse than a missing one")
c.ok("...while the 1-year drawdown from the same series is answered",
     price_risk.metrics(two_years)["max_drawdown_1y"] is not None)
five_years = series(range(100, 100 + 1260))
c.ok("five years of bars does answer it",
     price_risk.metrics(five_years)["max_drawdown_3y"] is not None)

# The Ulcer index is the RMS of the same drawdown series, so it is bounded by
# the maximum and strictly positive whenever any drawdown exists.
ulcer = price_risk.ulcer_index(series(shape))
c.ok("the ulcer index sits between zero and the max drawdown",
     ulcer is not None and 0 < ulcer < dd, f"ulcer {ulcer:.2f} vs max {dd:.2f}")

# --------------------------------------------------------------------------
c.section("volatility -- annualization pinned by hand")

# A series that alternates +10%/-10% in log terms has a known daily std.
step = 0.10
alternating = [100.0]
for i in range(120):
    alternating.append(alternating[-1] * math.exp(step if i % 2 == 0 else -step))
alt = series(alternating)
rets = np.log(alt / alt.shift(1)).dropna()
expected = float(rets.std(ddof=1) * math.sqrt(252) * 100)
got = price_risk.volatility_pct(alt, 252)
c.ok("volatility is the sample std of log returns x sqrt(252), in percent",
     got is not None and abs(got - expected) < 1e-9, f"{got:.4f} vs {expected:.4f}")

c.ok("a flat series has zero volatility",
     price_risk.volatility_pct(series([50] * 80), 252) == 0.0)
c.ok("volatility below the minimum observation count is None",
     price_risk.volatility_pct(series([100, 101, 102]), 252) is None)

# Downside deviation ignores up days, so on a series that only rises it is 0,
# and on any mixed series it is no larger than total volatility.
c.ok("downside deviation of a rising series is zero",
     price_risk.downside_deviation_pct(series(range(100, 200))) == 0.0)
mixed_dd = price_risk.downside_deviation_pct(alt)
c.ok("downside deviation never exceeds total volatility",
     mixed_dd is not None and mixed_dd <= got + 1e-9,
     f"downside {mixed_dd:.2f} vs total {got:.2f}")

# --------------------------------------------------------------------------
c.section("beta -- alignment is the dangerous part")

rng = np.random.default_rng(7)
bench_rets = rng.normal(0, 0.01, 300)
bench_px = series(100 * np.exp(np.cumsum(bench_rets)))
# A stock that is exactly 1.5x the benchmark's log return must measure beta 1.5.
stock_px = series(100 * np.exp(np.cumsum(bench_rets * 1.5)))

b = price_risk.beta(stock_px, bench_px)
c.ok("a stock that is exactly 1.5x the benchmark measures beta 1.5",
     b is not None and abs(b - 1.5) < 1e-9, f"{b}")
c.ok("...and correlates perfectly with it",
     abs(price_risk.correlation(stock_px, bench_px) - 1.0) < 1e-9)
c.ok("downside beta of a pure multiple is the same multiple",
     abs(price_risk.downside_beta(stock_px, bench_px) - 1.5) < 1e-9)

# The alignment invariant: shifting the benchmark's *dates* must not silently
# produce a number off the wrong pairs.
offset = bench_px.copy()
offset.index = offset.index + pd.Timedelta(days=7)
shifted = price_risk.beta(stock_px, offset)
c.ok("a date-shifted benchmark is intersected, not zipped",
     shifted is None or abs(shifted - 1.5) > 1e-6,
     f"got {shifted} -- 1.5 here would mean positions were paired blindly")

c.ok("no benchmark yields None for beta, not zero",
     price_risk.beta(stock_px, None) is None
     and price_risk.downside_beta(stock_px, None) is None,
     "zero beta is a claim; None is the absence of one")

# A benchmark with no variance cannot produce a beta.
c.ok("a flat benchmark yields None rather than dividing by zero",
     price_risk.beta(stock_px, series([100] * 300)) is None)

# --------------------------------------------------------------------------
c.section("52-week high and the momentum factor")

rising = series(range(100, 400))
c.ok("a series at its high is 0% below it",
     price_risk.pct_below_52w_high(rising) == 0.0)
falling = series(list(range(100, 200)) + list(range(200, 150, -1)))
below = price_risk.pct_below_52w_high(falling)
c.ok("a series off its high reports a positive percent below",
     below is not None and below > 0, f"{below:.2f}%")

c.ok("momentum needs 12 months plus the skip month",
     price_risk.momentum_12_1_pct(series(range(100, 260))) is None,
     "160 bars cannot answer a 273-bar question")
mom = price_risk.momentum_12_1_pct(series(range(100, 500)))
c.ok("momentum over a rising series is positive and skips the last month",
     mom is not None and mom > 0, f"{mom:.2f}%")

# --------------------------------------------------------------------------
c.section("polarity -- bigger must mean worse for every registered metric")

calm = series(100 + np.cumsum(rng.normal(0.02, 0.004, 300)))
wild = series(100 * np.exp(np.cumsum(rng.normal(-0.001, 0.045, 300))))

pairs = [
    ("volatility_252d", True), ("downside_deviation", True),
    ("max_drawdown_1y", True), ("ulcer_index", True),
]
calm_m = price_risk.metrics(calm, bench_px)
wild_m = price_risk.metrics(wild, bench_px)
for key, bigger_is_worse in pairs:
    a, b2 = calm_m.get(key), wild_m.get(key)
    c.ok(f"{key}: the wild series reads worse than the calm one",
         a is not None and b2 is not None and (b2 > a) == bigger_is_worse,
         f"calm {a:.2f} vs wild {b2:.2f}")

c.ok("every metric key is present even when a value is None",
     set(calm_m) == set(price_risk.metrics(series([100] * 40), None)),
     "a caller reading a missing key must not get a KeyError")

raise SystemExit(c.finish())
