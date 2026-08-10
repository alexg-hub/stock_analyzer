"""Price-based risk metrics: the `price_risk` resolver.

The risk axis was built entirely out of accounting data, which left it with no
volatility, no drawdown and no beta -- the three most directly *measurable* risks
a listed company has. This closes that, and costs nothing: the nightly run
already downloads the whole universe's price panel, and
`backtest_universe_cache.pkl` keeps it.

Three rules, mirroring `derived.py`:

  * **No I/O.** Every function takes a `close` series (and optionally a
    benchmark) that the caller already has. `quality.collect` passes the same
    `close` it already threads through to `research_collect` for the PE
    percentile, so nothing here adds a round trip.
  * **Too little history returns `None`, never a number.** A 3-year drawdown
    computed over 40 bars is not a small drawdown, it is a wrong one -- and the
    registry's missing-value rules already handle None correctly (it fails a
    gate, is skipped in the score, and never vetoes).
  * **Everything is a *risk* orientation** except where noted, so an anchor pair
    reads the same way as the rest of the group: bigger is worse.

Returns are **log** returns for volatility and beta (additive across time, which
is what annualizing by sqrt(252) assumes) and simple returns for drawdown, where
the actual peak-to-trough loss is the quantity of interest.
"""

import numpy as np
import pandas as pd

TRADING_DAYS = 252

#: Below this many observations a window is not measured at all. 30 bars is
#: enough for a 60-day volatility to mean something; a 252-day window needs its
#: own minimum, applied per metric.
MIN_OBS = 30


def _clean(close) -> pd.Series:
    """A float series indexed by date, deduplicated, NaNs dropped."""
    if close is None:
        return pd.Series(dtype=float)
    series = pd.Series(close).dropna()
    if series.empty:
        return series
    series = series.astype(float)
    if not isinstance(series.index, pd.DatetimeIndex):
        try:
            series.index = pd.to_datetime(series.index)
        except Exception:  # noqa: BLE001 - an unusable index means no metrics
            return pd.Series(dtype=float)
    return series[~series.index.duplicated(keep="last")].sort_index()


def _log_returns(close: pd.Series) -> pd.Series:
    if len(close) < 2:
        return pd.Series(dtype=float)
    return np.log(close / close.shift(1)).dropna()


def _tail(series: pd.Series, days: int) -> pd.Series:
    return series.iloc[-days:] if len(series) > days else series


def volatility_pct(close: pd.Series, days: int, min_obs: int = MIN_OBS):
    """Annualized standard deviation of log returns, in percent."""
    window = _tail(_log_returns(close), days)
    if len(window) < min_obs:
        return None
    return float(window.std(ddof=1) * np.sqrt(TRADING_DAYS) * 100)


def downside_deviation_pct(close: pd.Series, days: int = TRADING_DAYS,
                           min_obs: int = MIN_OBS):
    """Annualized deviation of the *negative* returns only.

    Volatility punishes a stock for rising sharply; this does not. Kept
    alongside rather than instead of it, because a name whose upside and downside
    volatility diverge is telling you something neither number says alone.
    """
    window = _tail(_log_returns(close), days)
    if len(window) < min_obs:
        return None
    losses = window[window < 0]
    if losses.empty:
        return 0.0
    return float(np.sqrt((losses ** 2).mean()) * np.sqrt(TRADING_DAYS) * 100)


def max_drawdown_pct(close: pd.Series, days: int, min_obs: int = MIN_OBS):
    """Worst peak-to-trough decline in the window, as a positive percent."""
    window = _tail(close, days)
    if len(window) < min_obs:
        return None
    peak = window.cummax()
    drawdown = window / peak - 1.0
    # `abs`, not negation: a series that never fell gives `min() == 0.0`, and
    # negating that yields -0.0, which formats as "-0.00%" in every table.
    return float(abs(drawdown.min()) * 100)


def ulcer_index(close: pd.Series, days: int = TRADING_DAYS,
                min_obs: int = MIN_OBS):
    """RMS of the drawdown series -- depth *and* time spent under water.

    A max drawdown says how bad the worst moment was; this says how much of the
    period was spent recovering, which is closer to what actually makes a
    position untenable.
    """
    window = _tail(close, days)
    if len(window) < min_obs:
        return None
    drawdown = (window / window.cummax() - 1.0) * 100
    return float(np.sqrt((drawdown ** 2).mean()))


def _aligned_returns(close: pd.Series, benchmark: pd.Series, days: int):
    """Both return series on their shared dates -- intersected, never zipped.

    Same rule as `derived._aligned`: a beta computed against a benchmark shifted
    by a day or two produces a plausible number and raises nothing.
    """
    stock = _tail(_log_returns(close), days)
    bench = _tail(_log_returns(_clean(benchmark)), days)
    if stock.empty or bench.empty:
        return None, None
    shared = stock.index.intersection(bench.index)
    if len(shared) < MIN_OBS:
        return None, None
    return stock.reindex(shared), bench.reindex(shared)


def beta(close: pd.Series, benchmark, days: int = TRADING_DAYS):
    """Ordinary least-squares beta against the benchmark."""
    stock, bench = _aligned_returns(close, benchmark, days)
    if stock is None:
        return None
    var = float(bench.var(ddof=1))
    if not var:
        return None
    return float(bench.cov(stock) / var)


def downside_beta(close: pd.Series, benchmark, days: int = TRADING_DAYS):
    """Beta measured only on days the benchmark fell.

    The asymmetry is the point: a stock with an ordinary beta of 1.0 that
    delivers 1.6 when the index drops is not a market-risk-neutral holding, and
    plain beta cannot see the difference.
    """
    stock, bench = _aligned_returns(close, benchmark, days)
    if stock is None:
        return None
    down = bench < 0
    if int(down.sum()) < MIN_OBS // 2:
        return None
    var = float(bench[down].var(ddof=1))
    if not var:
        return None
    return float(bench[down].cov(stock[down]) / var)


def correlation(close: pd.Series, benchmark, days: int = TRADING_DAYS):
    stock, bench = _aligned_returns(close, benchmark, days)
    if stock is None:
        return None
    value = float(stock.corr(bench))
    return None if pd.isna(value) else value


def return_skew(close: pd.Series, days: int = TRADING_DAYS,
                min_obs: int = MIN_OBS):
    """Skewness of daily returns. Negative = crash-prone (a risk)."""
    window = _tail(_log_returns(close), days)
    if len(window) < min_obs:
        return None
    value = float(window.skew())
    return None if pd.isna(value) else value


def pct_below_52w_high(close: pd.Series, days: int = TRADING_DAYS,
                       min_obs: int = MIN_OBS):
    """How far under the trailing high the price sits, as a positive percent.

    Needs its own minimum like every other window here: with a handful of bars
    the running maximum is whatever the series happened to touch, so a young or
    barely-traded listing would read a confident 0% below its high.
    """
    window = _tail(close, days)
    if len(window) < min_obs:
        return None
    high = float(window.max())
    if not high:
        return None
    return float((1 - float(window.iloc[-1]) / high) * 100)


def momentum_12_1_pct(close: pd.Series):
    """The academic momentum factor: 12-month return skipping the last month.

    A **reward** reading, not a risk one -- the skip-month is what removes the
    short-term reversal that pollutes a raw 12-month return. Computed here
    because the series is in hand; no registry parameter consumes it yet, and
    whichever one does must sit on the reward axis.
    """
    if len(close) < TRADING_DAYS + 21:
        return None
    start = float(close.iloc[-(TRADING_DAYS + 21)])
    end = float(close.iloc[-22])
    if not start:
        return None
    return float((end / start - 1) * 100)


def metrics(close, benchmark=None) -> dict:
    """Every price-risk reading for one ticker. Missing history yields `None`.

    `benchmark` is a close series (SPY, normally); without it the three
    market-relative readings are None and the rest are unaffected -- the same
    fail-open behaviour every other resolver has.
    """
    series = _clean(close)
    if series.empty:
        return {}
    return {
        "volatility_60d": volatility_pct(series, 60),
        "volatility_252d": volatility_pct(series, TRADING_DAYS),
        "downside_deviation": downside_deviation_pct(series),
        "max_drawdown_1y": max_drawdown_pct(series, TRADING_DAYS),
        "max_drawdown_3y": max_drawdown_pct(series, TRADING_DAYS * 3,
                                            min_obs=TRADING_DAYS),
        "ulcer_index": ulcer_index(series),
        "beta": beta(series, benchmark),
        "downside_beta": downside_beta(series, benchmark),
        "spy_correlation": correlation(series, benchmark),
        "return_skew": return_skew(series),
        "pct_below_52w_high": pct_below_52w_high(series),
        "momentum_12_1": momentum_12_1_pct(series),
    }
