"""
Screen module: a near-linear multi-month uptrend.

The other three screens fire on a one-day event -- a range break, a touch of an
SMA, a cross back above one. This one asks a question about the *shape of the
whole year*: has this stock been grinding up something close to a straight line
for the better part of it? A rolling least-squares fit over the last
`trend_window_days` answers that, and the three parameters the fit produces are
exactly the three knobs the setup needs -- the angle, the tightness around the
line, and (as the window itself) the duration.

Setup detected (evaluated on the most recent trading day):

T1. Angle -- the fitted line's annualized slope is between
    `min_annual_slope_pct` and `max_annual_slope_pct`. **Mandatory.** The
    ceiling earns its keep: a parabolic blow-off scores a very high r-squared
    (which rises with slope for a given noise level), so linearity alone will
    not reject one. Set `max_annual_slope_pct` to null to drop it.
    **The two bounds are not symmetric.** The floor is a qualifier -- a trend
    can begin by getting steep enough -- while the ceiling is a *disqualifier*,
    applied to the signal rather than to the state that freshness watches.
    Folding it in made the screen fire on the day a hot trend cooled through
    the bar: a deceleration entry dressed as a trend entry, and 18% of all
    signals, against 0% that ever fired by crossing the floor upward.
T2. Linearity -- the fit's r-squared is at least `min_r_squared`.
T3. Volatility around the line -- the residual standard deviation, as a percent
    of price, is at most `max_residual_pct`. This is the "distance from the
    line" measure: it counts every bar in the window, not just today's.
T4. Not extended (optional) -- when `max_last_dev_pct` is set (not null),
    today's close is within that fraction of the fitted line, so a name that has
    torn away from its own trend does not qualify while it is stretched.

T1 is always mandatory; T2, T3 and T4-when-enabled are the confirmations.

**This screen stays strict, like the pullback screen**: it alerts only when
every confirmation holds, so every alerted row is `Setup == "full"`. Its
`partial_mask` -- the angle holding while up to `max_partial_fails`
confirmations fail -- is a **backtest-only control cohort**, never sent. That
cohort is the measurement the screen exists to justify: it is the same trend
without the linearity requirement, so comparing the two says whether demanding
a straight line is worth anything. Measured over 3 years of the 904-name panel,
it is not: the loose cohort reads an excess of -0.91 over 30 days against the
strict cohort's -1.79. Keep it measured.

The two tiers are armed independently, and that is deliberate. An earlier
version gated both off one combined "in trend" state, which meant the far wider
loose door swallowed nearly every trend before it tightened -- 33 full signals
against 1512 partial ones, from a rule that produces 656 on its own. Each tier
now watches its own condition become true, so widening the control cohort
cannot starve the signal.

A transition also needs something to transition FROM (`had_fit` below). On the
first bar with a full window, and on the first bar after an interior Close hole
rolls out of one, yesterday has no fit at all -- so "not in trend yesterday, in
trend today" is an artefact of where the data starts rather than anything the
market did. Without that guard a hole manufactures a phantom trend exactly
`trend_window_days` bars later, and 116 of the 904 tickers on the 5y panel carry
one. Same family as `drop_unsettled_bars`: a missing measurement must never read
as a finding.

The fit runs on **log price** by default (`fit_on_log_price`), because a
constant-percent grower is a straight line there -- which is what a linear
uptrend means for an equity over ten months -- and because the residual is then
a scale-free fraction that reads directly as "percent away from the line". Set
the flag false to fit raw price instead; the slope and the residual are then
normalized by the window's mean price so the same config numbers stay roughly
comparable.

**The signal is the day the trend first qualifies**, not every day it holds
(`alert_only_on_new_trend`). A ten-month trend is a *state* that stays true for
months: alerting on the state puts ~53 tickers a night in the message and gives
the backtest no dated entry to score, while alerting on the transition is ~0.57
a night and is an event like every other screen here. Same idea as the reclaim
screen's `is_fresh_cross`. The trend re-arms: if it breaks and later re-forms,
that is a new signal.

All parameters live in the `trend_strategy` section of config.json.
"""

import numpy as np
import pandas as pd

import charts
from scanner_common import (ScanResult, fmt_value, log_step, screen_hits,
                            single_ticker_panel)

# Config section this screen reads (run_scanners.py registry contract).
CONFIG_KEY = "trend_strategy"
# Side-bar color of this screen's Discord embed cards (violet -- deliberately
# none of the other three screens' colors, nor the exit detector's red).
EMBED_COLOR = 0x7A5AA8

# Trading days in a year, for annualizing the fitted slope.
TRADING_DAYS = 252


def required_history(strategy: dict) -> int:
    """Trading days of history needed before this screen can ever fire.

    The window itself, plus one bar: the fresh-qualification test compares
    today's state against yesterday's, so the first day the window is full is
    still not a day this screen can call an event.
    """
    return strategy["trend_window_days"] + 1


# --------------------------------------------------------------------------
# Vectorized rolling least-squares fit
# --------------------------------------------------------------------------

def rolling_fit(y: pd.DataFrame, window: int) -> dict[str, pd.DataFrame]:
    """Least-squares fit of every trailing `window` of every column, at once.

    Returns `slope` / `intercept` (in local window coordinates, x = 0 at the
    window's first bar), `r2`, and `resid` (the residual standard deviation, in
    whatever units `y` is in).

    Closed-form from rolling sums, so this stays fully vectorized over the
    (days, tickers) panel like every other compute_* in the project -- no
    per-ticker loop and no `apply`. The one non-obvious step is Sxy: rolling
    sums cannot see the window's own x coordinates, so it is taken over the
    ABSOLUTE bar index and then re-based to the window start. Getting that
    re-basing wrong produces a plausible slope and raises nothing, which is why
    tests/test_trend_line.py checks the result against numpy.polyfit.

    Every rolling call runs at the default `min_periods=window`, exactly as the
    other screens' baselines do, so a window that is short or that spans an
    interior Close hole yields NaN rather than a fit over the bars that
    happened to survive.
    """
    n = window
    # The absolute bar index, broadcast across every ticker column.
    x_abs = pd.DataFrame(
        np.repeat(np.arange(len(y), dtype="float64")[:, None], y.shape[1], axis=1),
        index=y.index, columns=y.columns,
    )

    sum_y = y.rolling(n).sum()
    sum_yy = (y * y).rolling(n).sum()
    # Re-base the absolute-index cross term onto the window's own x = 0..n-1.
    sum_xy = (x_abs * y).rolling(n).sum() - (x_abs - (n - 1)) * sum_y

    # x is 0..n-1 in every window, so these two are constants.
    sum_x = n * (n - 1) / 2.0
    sum_xx = (n - 1) * n * (2 * n - 1) / 6.0

    cov_xy = sum_xy - sum_x * sum_y / n
    var_x = sum_xx - sum_x * sum_x / n
    var_y = sum_yy - sum_y * sum_y / n

    slope = cov_xy / var_x
    mean_y = sum_y / n
    intercept = mean_y - slope * sum_x / n

    # Clipped at zero: the algebraic form can go a hair negative on a perfect
    # fit through floating-point cancellation, and a negative SSE would make
    # the residual NaN on exactly the cleanest trend in the panel.
    sse = (var_y - slope * cov_xy).clip(lower=0)
    # var_y is 0 for a perfectly flat series; r2 is undefined there, not 1.
    r2 = 1 - sse / var_y.where(var_y > 0)
    resid = np.sqrt(sse / (n - 2))

    return {"slope": slope, "intercept": intercept, "r2": r2, "resid": resid,
            "mean_y": mean_y}


def compute_trend_signals(data: pd.DataFrame, strategy: dict) -> dict[str, pd.DataFrame]:
    """Compute every trend condition for every day and every ticker.

    Same conventions as the other compute_* functions: every input and output
    is a (days, tickers) DataFrame, no per-ticker loops, and NaNs (insufficient
    history, or a window spanning a withdrawn bar) compare as False and drop
    out.
    """
    window = strategy["trend_window_days"]
    use_log = strategy.get("fit_on_log_price", True)
    min_ann = strategy["min_annual_slope_pct"]
    max_ann = strategy.get("max_annual_slope_pct")
    min_r2 = strategy["min_r_squared"]
    max_resid = strategy["max_residual_pct"]
    max_dev = strategy.get("max_last_dev_pct")
    max_fails = strategy.get("max_partial_fails", 1)

    close = data["Close"]
    # A non-positive close has no logarithm; `where` turns it into the NaN the
    # rolling windows already know how to drop.
    y = np.log(close.where(close > 0)) if use_log else close

    fit = rolling_fit(y, window)
    slope, r2, resid, mean_y = fit["slope"], fit["r2"], fit["resid"], fit["mean_y"]

    # The fit passes through the window centroid, so its value on the last bar
    # is the mean plus half a window of slope.
    fitted_last = mean_y + slope * (window - 1) / 2.0

    if use_log:
        # A log slope is a per-day continuously compounded return.
        annual_slope = np.exp(slope * TRADING_DAYS) - 1
        trend_gain = np.exp(slope * (window - 1)) - 1
        resid_pct = resid * 100
        fitted_price = np.exp(fitted_last)
    else:
        # Price space: normalize by the window's mean price so that the same
        # config thresholds mean roughly the same thing in both modes.
        base = mean_y.where(mean_y > 0)
        annual_slope = slope * TRADING_DAYS / base
        trend_gain = slope * (window - 1) / base
        resid_pct = resid / base * 100
        fitted_price = fitted_last

    dist_pct = close / fitted_price - 1
    # The angle of the line on a (log price, years) plot -- a stated convention,
    # displayed only. A chart angle depends on the axis scaling, so it can never
    # define a threshold; the gates use annual_slope, of which this is a
    # monotone restatement.
    angle_deg = np.degrees(np.arctan(np.log1p(annual_slope)))

    # T1: the angle, mandatory. NaN compares False, which is what we want.
    # The floor and the ceiling are NOT symmetric, and this is load-bearing.
    # The floor is a qualifier -- a trend can begin by getting steep enough.
    # The ceiling is a DISQUALIFIER: nothing begins a trend by slowing down, so
    # it is applied to the signal rather than folded into the state freshness
    # watches. Folded in, it fired on the day a hot trend cooled through the
    # bar -- a deceleration entry wearing a trend entry's clothes, and 18% of
    # all signals (against 0% that ever fired by crossing the floor upward).
    is_angle_ok = annual_slope >= min_ann
    is_not_parabolic = (annual_slope <= max_ann if max_ann is not None
                        else pd.DataFrame(True, index=close.index,
                                          columns=close.columns))

    # T2/T3/T4: the confirmations that decide the tier.
    is_linear = r2 >= min_r2
    is_tight = resid_pct <= max_resid * 100
    is_near_line = dist_pct.abs() <= max_dev if max_dev is not None else None

    fails = ((~is_linear.fillna(False)).astype(int)
             + (~is_tight.fillna(False)).astype(int))
    if is_near_line is not None:
        fails = fails + (~is_near_line.fillna(False)).astype(int)

    is_angle_ok = is_angle_ok.fillna(False)
    is_not_parabolic = is_not_parabolic.fillna(False)
    # The shape freshness watches: the angle floor plus the confirmations. The
    # ceiling is deliberately NOT in here -- see is_not_parabolic above.
    shape_full = is_angle_ok & (fails == 0)
    # The control cohort's shape: the same floor, with the confirmations
    # allowed to fail. Never alerted -- see partial_mask.
    shape_loose = is_angle_ok & (fails <= max_fails)

    # The event: the day a trend first qualifies. The two shapes are armed
    # INDEPENDENTLY -- gating both off the loose state would let the far wider
    # loose door swallow a trend before it tightened, starving the signal tier
    # (measured: 33 full against 1512 partial, from a rule worth 656 alone).
    # A transition is only an event if there was something to transition FROM.
    # On the first bar with a full window -- and on the first bar after an
    # interior Close hole rolls out of one -- the previous day has no fit at
    # all, so "not in trend yesterday, in trend today" is an artefact of where
    # the data starts rather than anything the market did. Without this guard a
    # hole manufactures a phantom trend exactly `window` bars later, and 116 of
    # the 904 tickers on the 5y panel carry one.
    had_fit = slope.notna().shift(1, fill_value=False)
    if strategy.get("alert_only_on_new_trend", True):
        is_new_trend = shape_full & ~shape_full.shift(1, fill_value=False) & had_fit
        is_new_loose = shape_loose & ~shape_loose.shift(1, fill_value=False) & had_fit
    else:
        is_new_trend, is_new_loose = shape_full, shape_loose

    signal = is_new_trend & is_not_parabolic

    return {
        "slope": slope,
        "intercept": fit["intercept"],
        "r2": r2,
        "resid_pct": resid_pct,
        "fit": fitted_price,
        "dist_pct": dist_pct,
        "annual_slope": annual_slope,
        "trend_gain": trend_gain,
        "angle_deg": angle_deg,
        "fails": fails,
        "is_angle_ok": is_angle_ok,
        "is_not_parabolic": is_not_parabolic,
        "is_linear": is_linear,
        "is_tight": is_tight,
        "is_near_line": is_near_line,
        "shape_full": shape_full,
        "shape_loose": shape_loose,
        "is_new_trend": is_new_trend,
        "is_new_loose": is_new_loose,
        "signal": signal,
    }


def partial_mask(data: pd.DataFrame, signals: dict, strategy: dict) -> pd.DataFrame:
    """(days, tickers) mask of loose trends that did NOT fire -- **backtest only**.

    A newly qualifying trend whose angle holds (T1, mandatory exactly as for a
    full setup) but where up to `max_partial_fails` of the confirmations -- T2
    linearity, T3 tightness, T4 distance when enabled -- fail.

    This screen deliberately stays strict, so this cohort is *not* part of
    `fires_mask`. It exists to be measured: it is the same trend with the
    linearity requirement relaxed, which is the only way to find out whether
    demanding a straight line is worth anything. Measured, it is not: this
    cohort reads an excess of -0.91 over 30 days against the strict cohort's
    -1.79, which is exactly the kind of finding it is here to surface.
    `missing_reason` explains an individual one.

    `max_partial_fails: 0` makes it empty -- the screen with no control.

    Vectorized over every day like the compute_* functions: the nightly scan
    takes `.iloc[-1]`, the universe backtest uses the whole frame.
    """
    return (signals["is_new_loose"].fillna(False)
            & signals["is_not_parabolic"].fillna(False)
            & ~signals["signal"].fillna(False))


def fires_mask(data: pd.DataFrame, signals: dict, strategy: dict) -> pd.DataFrame:
    """(days, tickers) mask of every day this screen alerts on.

    Like the pullback screen and unlike breakout and reclaim, this is just the
    strict signal -- every alerted trend is a `full` setup, and `partial_mask`
    above is excluded on purpose.
    """
    return signals["signal"].fillna(False)


compute = compute_trend_signals


def missing_reasons(row: pd.Series, strategy: dict) -> list[str]:
    """Every confirmation test a loose (`partial_mask`) day failed, in order.

    `row` is a build_calc_table row (percent columns already x100). Returns one
    string per failing confirmation (T2 linearity, T3 tightness, T4 distance
    when enabled); empty when all pass. T1 is mandatory, so it never appears
    here -- a day that fails it is in no cohort at all.

    The screen is strict, so this never explains an *alerted* row: it explains a
    control-cohort day, the way `sma_pullback.touch_miss_reason` does.
    """
    min_r2 = strategy["min_r_squared"]
    max_resid = strategy["max_residual_pct"]
    max_dev = strategy.get("max_last_dev_pct")
    window = strategy["trend_window_days"]

    reasons = []
    if not row["T2_Linear"]:
        reasons.append(f"not linear enough: r2 {row['R2']:.2f} < {min_r2:.2f} "
                       f"required over {window}d")
    if not row["T3_Tight"]:
        reasons.append(f"too loose around the line: residual "
                       f"{row['ResidPct']:.1f}% > {max_resid:.1%} allowed")
    if max_dev is not None and not row["T4_NearLine"]:
        reasons.append(f"extended: {row['DistPct']:+.1f}% from the line > "
                       f"{max_dev:.1%} allowed")
    return reasons


def missing_reason(row: pd.Series, strategy: dict) -> str:
    """Join every failing confirmation into one string (see missing_reasons)."""
    return "; ".join(missing_reasons(row, strategy))


def find_trends(data: pd.DataFrame, strategy: dict) -> pd.DataFrame:
    """Screen the whole universe on the most recent trading day.

    Returns a ticker-indexed DataFrame of every ticker whose trailing window
    newly qualifies as a near-linear uptrend. This screen stays strict, so
    every row is `Setup == "full"`.
    """
    needed = required_history(strategy)
    if len(data) <= needed:
        log_step("SCREEN", "warn",
                 f"{CONFIG_KEY}: only {len(data)} rows but needs > {needed} "
                 f"({strategy['trend_window_days']}d fit window + 1) -- NO "
                 f"signal can ever fire; raise data.download_period")

    signals = compute_trend_signals(data, strategy)
    last = {name: (df.iloc[-1] if df is not None else None)
            for name, df in signals.items()}

    def day_stats(tickers: list[str]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Close": data["Close"].iloc[-1][tickers].round(2),
                "Fit": last["fit"][tickers].round(2),
                "Dist %": (last["dist_pct"][tickers] * 100).round(2),
                "Slope %/yr": (last["annual_slope"][tickers] * 100).round(1),
                "Angle": last["angle_deg"][tickers].round(1),
                "Trend %": (last["trend_gain"][tickers] * 100).round(1),
                "R2": last["r2"][tickers].round(3),
                "Resid %": last["resid_pct"][tickers].round(2),
            },
            index=pd.Index(tickers, name="Ticker"),
        )

    # Strict: fires == signal, so every row is `full` and no reason is needed.
    return screen_hits(CONFIG_KEY, data, signals,
                       fires_mask(data, signals, strategy), day_stats)


# --------------------------------------------------------------------------
# Registry contract: scan / describe_hit / plot_hit
# --------------------------------------------------------------------------

def scan(data: pd.DataFrame, strategy: dict) -> ScanResult:
    months = strategy["trend_window_days"] / 21.0
    return ScanResult(
        title=f"Near-linear uptrend over {months:.0f} months",
        hits=find_trends(data, strategy),
        strategy=strategy,
    )


def describe_hit(row, strategy: dict) -> str:
    """Embed-card description of one trend signal."""
    months = strategy["trend_window_days"] / 21.0
    return (f"Close {fmt_value(row['Close'])} on a {months:.0f}-month line "
            f"rising {row['Slope %/yr']:+.0f}%/yr ({row['Angle']:.0f} deg, "
            f"{row['Trend %']:+.0f}% over the window), "
            f"r2 {row['R2']:.2f}, residual {row['Resid %']:.1f}%, "
            f"{row['Dist %']:+.1f}% from the line")


def build_calc_table(data: pd.DataFrame, signals: dict, ticker: str) -> pd.DataFrame:
    """Flatten one ticker's OHLCV + every intermediate into a per-day table
    (consumed by charts.plot_trend and the backtest)."""
    def col(field):
        return data[field][ticker]

    near = signals["is_near_line"]
    return pd.DataFrame(
        {
            "Open": col("Open"),
            "High": col("High"),
            "Low": col("Low"),
            "Close": col("Close"),
            "Volume": col("Volume"),
            "Fit": signals["fit"][ticker],
            "Slope": signals["slope"][ticker],
            "Intercept": signals["intercept"][ticker],
            "DistPct": signals["dist_pct"][ticker] * 100,
            "AnnualPct": signals["annual_slope"][ticker] * 100,
            "AngleDeg": signals["angle_deg"][ticker],
            "TrendPct": signals["trend_gain"][ticker] * 100,
            "R2": signals["r2"][ticker],
            "ResidPct": signals["resid_pct"][ticker],
            "Fails": signals["fails"][ticker],
            "T1_Angle": (signals["is_angle_ok"] & signals["is_not_parabolic"])[ticker],
            "T2_Linear": signals["is_linear"][ticker],
            "T3_Tight": signals["is_tight"][ticker],
            "T4_NearLine": (True if near is None else near[ticker]),
            "NewTrend": signals["is_new_trend"][ticker],
            "NewLoose": signals["is_new_loose"][ticker],
            "SIGNAL": signals["signal"][ticker],
        }
    ).rename_axis("Date")


def plot_hit(data: pd.DataFrame, ticker: str, strategy: dict,
             chart_cfg: dict, out_path) -> None:
    """Render the alert chart for one hit: the fit window of its close with the
    fitted line and its residual band, plus the r-squared panel."""
    panel = single_ticker_panel(data, ticker)
    table = build_calc_table(panel, compute_trend_signals(panel, strategy), ticker)
    # The whole fit window has to be visible or the line has nothing to sit on,
    # so this floors the configured lookback rather than simply obeying it.
    window = strategy["trend_window_days"]
    table = table.tail(max(chart_cfg.get("lookback_days", 250), window + 30))
    charts.plot_trend(table, strategy, ticker, out_path,
                      dpi=chart_cfg.get("dpi", 150))


if __name__ == "__main__":
    print("This is a screen module -- run the nightly scan with: python run_scanners.py")
