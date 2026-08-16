"""
Shared chart rendering for all scanners.

One place for the (validated) palette, axis styling, and the per-ticker
chart of each screen -- used both by the nightly Discord alert (recent
window of a hit) and by the backtests (full analysis window).

Every label/threshold shown on a chart is derived from the strategy config
passed in; nothing is hardcoded.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.ticker import ScalarFormatter

from scanner_common import fmt_compact

# Validated light-mode palette (dataviz reference instance).
C = {
    "surface": "#fcfcfb",
    "ink": "#0b0b0b",
    "ink2": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "close": "#2a78d6",   # series slot 1 (blue)
    "event": "#eb6834",   # series slot 2 (orange) -- signal highlights
}


def _two_panel_figure():
    """Styled (price, indicator) panel pair on the shared surface."""
    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(12, 7.5), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08},
    )
    fig.patch.set_facecolor(C["surface"])
    for ax in (ax_top, ax_bot):
        ax.set_facecolor(C["surface"])
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(C["axis"])
        ax.tick_params(colors=C["muted"], labelsize=9)
        ax.grid(axis="y", color=C["grid"], linewidth=0.8)
        ax.set_axisbelow(True)
    return fig, ax_top, ax_bot


def _grid_figure(rows: int, cols: int, figsize, **kwargs):
    """A styled rows x cols panel grid on the shared surface.

    The multi-panel sibling of `_two_panel_figure` (which is fixed at two
    panels in a 3:1 split). Applies the same five styling steps every builder
    in this module repeats by hand.
    """
    fig, axes = plt.subplots(rows, cols, figsize=figsize, **kwargs)
    fig.patch.set_facecolor(C["surface"])
    for ax in fig.axes:
        ax.set_facecolor(C["surface"])
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(C["axis"])
        ax.tick_params(colors=C["muted"], labelsize=9)
        ax.grid(axis="y", color=C["grid"], linewidth=0.8)
        ax.set_axisbelow(True)
    return fig, axes


def _title(ax, main: str, subtitle: str) -> None:
    ax.set_title(main + "\n", loc="left", fontsize=13, color=C["ink"], fontweight="bold")
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9.5, color=C["ink2"])


def _mark_signals(ax, hits: pd.DataFrame, y_col: str, label: str) -> None:
    """Orange markers + annotation on the first signal day."""
    if hits.empty:
        return
    ax.scatter(hits.index, hits[y_col], color=C["event"], s=70, zorder=5,
               edgecolor=C["surface"], linewidth=1.5, label=label)
    first = hits.iloc[0]
    ax.annotate(
        f"{hits.index[0].date()}\nclose {first[y_col]:.2f}",
        xy=(hits.index[0], first[y_col]),
        xytext=(12, 18), textcoords="offset points",
        fontsize=9, color=C["ink"],
        arrowprops={"arrowstyle": "-", "color": C["muted"], "linewidth": 0.8},
    )


# --------------------------------------------------------------------------
# Breakout-from-consolidation chart
# --------------------------------------------------------------------------

def plot_breakout(table: pd.DataFrame, strategy: dict, ticker: str,
                  out_path: Path, dpi: int = 150) -> None:
    """Price + volume chart of the consolidation/breakout calc table
    (columns as produced by breakout_scanner.build_calc_table)."""
    vol_mult = strategy["volume_surge_multiplier"]
    vol_days = strategy["volume_sma_days"]
    window = strategy["consolidation_window_days"]
    hits = table[table["SIGNAL"].fillna(False)]

    fig, ax_p, ax_v = _two_panel_figure()

    # -- price panel: consolidation band, prior high, close, signal markers --
    ax_p.fill_between(table.index, table["PriorLow"], table["PriorHigh"],
                      color=C["grid"], alpha=0.55,
                      label=f"prior {window}d range", linewidth=0)
    ax_p.plot(table.index, table["PriorHigh"], color=C["ink2"], linewidth=1.2,
              linestyle="--", label=f"prior {window}d high")
    ax_p.plot(table.index, table["Close"], color=C["close"], linewidth=2, label="close")
    _mark_signals(ax_p, hits, "Close", "breakout signal")
    ax_p.set_ylabel("Price (USD)", color=C["ink2"], fontsize=10)
    ax_p.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=C["ink2"])

    # -- volume panel: bars + surge threshold --
    surge = table["C3_VolumeSurge"].fillna(False)
    ax_v.bar(table.index[~surge], table["Volume"][~surge] / 1e6,
             color=C["axis"], width=1.0)
    ax_v.bar(table.index[surge], table["Volume"][surge] / 1e6,
             color=C["event"], width=1.0, label=f"volume >= {vol_mult}x {vol_days}d avg")
    ax_v.plot(table.index, vol_mult * table["VolSMA"] / 1e6, color=C["ink2"],
              linewidth=1.2, linestyle="--", label=f"{vol_mult}x {vol_days}d avg volume")
    ax_v.set_ylabel("Volume (M)", color=C["ink2"], fontsize=10)
    ax_v.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=C["ink2"])

    n_sig = len(hits)
    subtitle = (f"{n_sig} signal day(s)" if n_sig else "no signal days") + \
        f" -- range limit {strategy['max_consolidation_range_pct']:.0%}, " \
        f"volume {vol_mult}x {vol_days}d average, " \
        f"candle body >= {strategy.get('min_candle_body_pct', 0.0):.1%}"
    _title(ax_p, f"{ticker} -- breakout from {window}-day consolidation", subtitle)

    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor=C["surface"])
    plt.close(fig)
    print(f"Chart saved to {out_path}")


# --------------------------------------------------------------------------
# SMA-reclaim chart
# --------------------------------------------------------------------------

def plot_reclaim(table: pd.DataFrame, strategy: dict, ticker: str,
                 out_path: Path, dpi: int = 150) -> None:
    """Price + volume chart of the SMA-reclaim calc table
    (columns as produced by sma_reclaim.build_calc_table)."""
    sma_days = strategy["sma_days"]
    margin = strategy["cross_margin_pct"]
    vol_days = strategy["volume_sma_days"]
    vol_mult = strategy["volume_surge_multiplier"]
    hits = table[table["SIGNAL"].fillna(False)]

    fig, ax_p, ax_v = _two_panel_figure()

    # -- price panel: close, SMA, cross level, signal markers --
    ax_p.plot(table.index, table["SMA"], color=C["ink2"], linewidth=1.4,
              label=f"{sma_days}d SMA")
    if margin:
        ax_p.plot(table.index, (1 + margin) * table["SMA"], color=C["ink2"],
                  linewidth=1.0, linestyle="--",
                  label=f"cross level (SMA +{margin:.0%})")
    ax_p.plot(table.index, table["Close"], color=C["close"], linewidth=2, label="close")
    _mark_signals(ax_p, hits, "Close", "reclaim signal")
    ax_p.set_ylabel("Price (USD)", color=C["ink2"], fontsize=10)
    ax_p.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=C["ink2"])

    # -- volume panel: bars + surge threshold --
    surge = table["R3_VolumeSurge"].fillna(False)
    ax_v.bar(table.index[~surge], table["Volume"][~surge] / 1e6,
             color=C["axis"], width=1.0)
    ax_v.bar(table.index[surge], table["Volume"][surge] / 1e6,
             color=C["event"], width=1.0, label=f"volume >= {vol_mult}x {vol_days}d avg")
    ax_v.plot(table.index, vol_mult * table["VolSMA"] / 1e6, color=C["ink2"],
              linewidth=1.2, linestyle="--", label=f"{vol_mult}x {vol_days}d avg volume")
    ax_v.set_ylabel("Volume (M)", color=C["ink2"], fontsize=10)
    ax_v.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=C["ink2"])

    n_sig = len(hits)
    min_gain = strategy.get("min_day_gain_pct")
    strength = f"body >= {strategy.get('min_candle_body_pct', 0.0):.1%}"
    if min_gain is not None:
        strength += f" or day >= {min_gain:.1%} vs prev close"
    subtitle = (f"{n_sig} signal day(s)" if n_sig else "no signal days") + \
        f" -- below SMA >= {strategy['min_days_below_pct']:.0%} of last " \
        f"{strategy['below_lookback_days']}d, volume {vol_mult}x {vol_days}d average, " \
        f"{strength}"
    _title(ax_p, f"{ticker} -- reclaim of {sma_days}-day SMA after downtrend", subtitle)

    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor=C["surface"])
    plt.close(fig)
    print(f"Chart saved to {out_path}")


# --------------------------------------------------------------------------
# SMA-pullback chart
# --------------------------------------------------------------------------

def plot_pullback(table: pd.DataFrame, strategy: dict, ticker: str,
                  out_path: Path, dpi: int = 150) -> None:
    """Price + distance-from-SMA chart of the pullback calc table
    (columns as produced by sma_pullback.build_calc_table)."""
    sma_days = strategy["sma_days"]
    band = strategy["touch_band_pct"]
    lookback = strategy["trend_lookback_days"]
    hits = table[table["SIGNAL"].fillna(False)]

    fig, ax_p, ax_d = _two_panel_figure()

    # -- price panel: close, SMA, touch band, signal markers --
    ax_p.fill_between(table.index,
                      (1 - band) * table["SMA"], (1 + band) * table["SMA"],
                      color=C["grid"], alpha=0.75,
                      label=f"SMA +/-{band:.0%} touch band", linewidth=0)
    ax_p.plot(table.index, table["SMA"], color=C["ink2"], linewidth=1.4,
              label=f"{sma_days}d SMA")
    ax_p.plot(table.index, table["Close"], color=C["close"], linewidth=2, label="close")
    _mark_signals(ax_p, hits, "Close", "pullback signal")
    ax_p.set_ylabel("Price (USD)", color=C["ink2"], fontsize=10)
    ax_p.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=C["ink2"])

    # -- distance panel: % distance from SMA vs the touch band --
    ax_d.plot(table.index, table["DistPct"], color=C["close"], linewidth=1.6,
              label=f"close vs {sma_days}d SMA")
    ax_d.axhline(0, color=C["axis"], linewidth=1.0)
    for level in (band * 100, -band * 100):
        ax_d.axhline(level, color=C["ink2"], linewidth=1.2, linestyle="--")
    ax_d.axhspan(-band * 100, band * 100, color=C["grid"], alpha=0.55)
    if not hits.empty:
        ax_d.scatter(hits.index, hits["DistPct"], color=C["event"], s=45, zorder=5,
                     edgecolor=C["surface"], linewidth=1.2)
    ax_d.set_ylabel("Dist from SMA (%)", color=C["ink2"], fontsize=10)
    ax_d.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=C["ink2"])

    n_sig = len(hits)
    subtitle = (f"{n_sig} signal day(s)" if n_sig else "no signal days") + \
        f" -- touch band +/-{band:.0%}, rising SMA, " \
        f"above SMA >= {strategy['min_days_above_sma_pct']:.0%} of last {lookback}d"
    if strategy.get("require_reversal_candle", True):
        subtitle += (f", reversal candle body<={strategy.get('max_candle_body_pct', 0.0):.1%}"
                     f" range>={strategy.get('min_candle_range_pct', 0.0):.1%}")
    _title(ax_p, f"{ticker} -- pullback to rising {sma_days}-day SMA", subtitle)

    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor=C["surface"])
    plt.close(fig)
    print(f"Chart saved to {out_path}")


# --------------------------------------------------------------------------
# Near-linear trend chart
# --------------------------------------------------------------------------

def _fit_line(table: pd.DataFrame, window: int, use_log: bool):
    """The fitted line and its +/-1 residual band, as three aligned series.

    Anchored on the last SIGNAL row when there is one -- the line you would
    have drawn on the day the screen fired -- and otherwise on the last row
    with a usable fit. Returns (index, line, low, high) or None when no row in
    the table has a fit at all (a table shorter than the window).
    """
    usable = table["Slope"].notna()
    if not usable.any():
        return None
    hits = table[table["SIGNAL"].fillna(False) & usable]
    anchor = hits.index[-1] if not hits.empty else table.index[usable][-1]

    end = table.index.get_loc(anchor)
    span = table.index[max(0, end - window + 1):end + 1]
    row = table.loc[anchor]
    # x runs 0..n-1 across the window, exactly as the fit was computed.
    x = np.arange(len(span), dtype="float64") + (window - len(span))
    fitted = row["Intercept"] + row["Slope"] * x
    resid = row["ResidPct"] / 100.0

    if use_log:
        line = np.exp(fitted)
        low, high = line * np.exp(-resid), line * np.exp(resid)
    else:
        line = fitted
        low, high = line * (1 - resid), line * (1 + resid)
    return span, line, low, high


def plot_trend(table: pd.DataFrame, strategy: dict, ticker: str,
               out_path: Path, dpi: int = 150) -> None:
    """Price + r-squared chart of the linear-trend calc table
    (columns as produced by trend_line.build_calc_table)."""
    window = strategy["trend_window_days"]
    use_log = strategy.get("fit_on_log_price", True)
    min_r2 = strategy["min_r_squared"]
    max_resid = strategy["max_residual_pct"]
    hits = table[table["SIGNAL"].fillna(False)]

    fig, ax_p, ax_r = _two_panel_figure()

    # -- price panel: the fitted line, its residual band, close, signals --
    fit = _fit_line(table, window, use_log)
    if fit is not None:
        span, line, low, high = fit
        ax_p.fill_between(span, low, high, color=C["grid"], alpha=0.75,
                          label="+/-1 residual", linewidth=0)
        ax_p.plot(span, line, color=C["ink2"], linewidth=1.4, linestyle="--",
                  label=f"{window}d fitted line")
    ax_p.plot(table.index, table["Close"], color=C["close"], linewidth=2,
              label="close")
    _mark_signals(ax_p, hits, "Close", "trend signal")
    ax_p.set_ylabel("Price (USD)", color=C["ink2"], fontsize=10)
    if use_log:
        # The fit is a straight line in log space, so this is the axis on which
        # "near-linear" is the thing being judged by eye.
        ax_p.set_yscale("log")
        ax_p.yaxis.set_major_formatter(ScalarFormatter())
        ax_p.yaxis.set_minor_formatter(ScalarFormatter())
    ax_p.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=C["ink2"])

    # -- r-squared panel: how linear the trailing window is, vs the gate --
    ax_r.plot(table.index, table["R2"], color=C["close"], linewidth=1.6,
              label=f"r2 of the trailing {window}d fit")
    ax_r.axhline(min_r2, color=C["ink2"], linewidth=1.2, linestyle="--")
    ax_r.axhspan(min_r2, 1.0, color=C["grid"], alpha=0.55)
    if not hits.empty:
        ax_r.scatter(hits.index, hits["R2"], color=C["event"], s=45, zorder=5,
                     edgecolor=C["surface"], linewidth=1.2)
    ax_r.set_ylim(0, 1)
    ax_r.set_ylabel("r-squared", color=C["ink2"], fontsize=10)
    # Lower right: the gate sits high on this axis, and an upper-left legend
    # lands straight on top of its dashed threshold line.
    ax_r.legend(loc="lower right", frameon=False, fontsize=9, labelcolor=C["ink2"])

    months = window / 21.0
    n_sig = len(hits)
    cap = strategy.get("max_annual_slope_pct")
    band = (f"{strategy['min_annual_slope_pct']:.0%}"
            + (f"..{cap:.0%}" if cap is not None else "+"))
    subtitle = (f"{n_sig} signal day(s)" if n_sig else "no signal days") + \
        f" -- slope {band}/yr, r2 >= {min_r2:.2f}, residual <= {max_resid:.1%}" \
        f", fit on {'log ' if use_log else ''}price"
    _title(ax_p, f"{ticker} -- near-linear uptrend over {months:.0f} months",
           subtitle)

    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor=C["surface"])
    plt.close(fig)
    print(f"Chart saved to {out_path}")


# --------------------------------------------------------------------------
# Double-top exit chart
# --------------------------------------------------------------------------

def plot_double_top(table: pd.DataFrame, strategy: dict, ticker: str,
                    out_path: Path, dpi: int = 120,
                    lookback_days: int = 250) -> None:
    """Price + volume chart of the double-top calc table
    (columns as produced by portfolio_sim.exits.build_calc_table).

    The only chart in this module that marks a *sell*. It uses the same two
    hues as every other one -- blue for the series, orange for the event --
    because the palette has exactly two, and the card's red side bar is what
    carries the "this is an exit" meaning.
    """
    recent = strategy.get("recent_window_days", 20)
    prior = strategy.get("prior_window_days", 90)
    view = table.tail(lookback_days) if lookback_days else table
    hits = view[view["SIGNAL"].fillna(False)]

    fig, ax_p, ax_v = _two_panel_figure()

    # -- price panel: the two peak levels, the neckline, close, the break --
    ax_p.plot(view.index, view["PEAK2"], color=C["ink2"], linewidth=1.1,
              linestyle=":", label=f"peak of last {recent}d")
    ax_p.plot(view.index, view["PEAK1"], color=C["ink2"], linewidth=1.1,
              linestyle="--", label=f"peak of prior {prior}d")
    ax_p.plot(view.index, view["NECKLINE"], color=C["event"], linewidth=1.3,
              linestyle="--", label="neckline")
    ax_p.fill_between(view.index, view["NECKLINE"], view["PEAK2"],
                      color=C["grid"], alpha=0.45, linewidth=0)
    ax_p.plot(view.index, view["Close"], color=C["close"], linewidth=2,
              label="close")
    _mark_signals(ax_p, hits, "Close", "neckline break")
    ax_p.set_ylabel("Price (USD)", color=C["ink2"], fontsize=10)
    ax_p.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=C["ink2"])

    ax_v.bar(view.index, view["Volume"] / 1e6, color=C["axis"], width=1.0)
    ax_v.set_ylabel("Volume (M)", color=C["ink2"], fontsize=10)

    n_sig = len(hits)
    subtitle = (f"{n_sig} break day(s)" if n_sig else "no break days") + \
        f" -- peaks within {strategy.get('max_peak_diff_pct', 0.0):.1%}, " \
        f"trough >= {strategy.get('min_trough_depth_pct', 0.0):.1%} below, " \
        f"close < neckline by {strategy.get('break_confirm_pct', 0.0):.2%}"
    _title(ax_p, f"{ticker} -- double top, neckline break", subtitle)

    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor=C["surface"])
    plt.close(fig)
    print(f"Chart saved to {out_path}")


# --------------------------------------------------------------------------
# Universe-backtest summary
# --------------------------------------------------------------------------

# Cohort -> hue, assigned in fixed order (never cycled). The default run has
# one cohort per screen ("signal"); --split-by-tier breaks it into the full
# setup (primary hue) and the partial one (secondary).
COHORT_COLOR = {"signal": C["close"], "full": C["close"], "partial": C["event"]}


def plot_backtest_summary(summary: pd.DataFrame, horizon: int, entry: str,
                          baseline_label: str, out_path: Path,
                          dpi: int = 120) -> None:
    """Mean return and win rate per screen/cohort for one holding period.

    `summary` is the stats table backtest_universe.py builds (one row per
    horizon x screen x cohort, plus the baseline/benchmark reference rows).
    Two panels, both horizontal bars on one axis each -- the baseline's mean
    return and win rate are drawn as reference lines rather than bars, since
    they are the bar every screen has to clear.
    """
    rows = summary[summary["horizon"] == horizon]
    base = rows[rows["screen"] == baseline_label]
    bars = rows[(rows["screen"] != baseline_label)
                & ~rows["screen"].str.contains("buy-and-hold")]
    bars = bars[bars["evaluable"] > 0]
    if bars.empty:
        print("No cohort had an evaluable trade -- skipping the summary chart.")
        return

    labels = [f"{s.replace('_strategy', '')} · {c}  (n={int(n)})"
              for s, c, n in zip(bars["screen"], bars["cohort"], bars["evaluable"])]
    colors = [COHORT_COLOR.get(c, C["muted"]) for c in bars["cohort"]]
    y = range(len(bars))

    fig, (ax_ret, ax_win) = plt.subplots(
        2, 1, figsize=(11, 2.0 + 0.95 * len(bars)),
        gridspec_kw={"hspace": 0.5})
    fig.patch.set_facecolor(C["surface"])
    for ax in (ax_ret, ax_win):
        ax.set_facecolor(C["surface"])
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(C["axis"])
        ax.tick_params(colors=C["muted"], labelsize=9)
        ax.grid(axis="x", color=C["grid"], linewidth=0.8)
        ax.set_axisbelow(True)
        ax.set_yticks(list(y))
        ax.set_yticklabels(labels, color=C["ink2"], fontsize=9.5)
        ax.invert_yaxis()

    def draw(ax, values, ref, xlabel, fmt, from_zero: bool):
        """Horizontal bars + a dashed reference line for the random-entry bar.

        The reference value goes in the axis label rather than a legend: with
        bars this close to the line, a legend box lands on top of them.
        """
        ax.barh(list(y), values, color=colors, height=0.62)
        if pd.notna(ref):
            ax.axvline(ref, color=C["ink2"], linewidth=1.2, linestyle="--")
            xlabel += f"      - - - random entry {fmt.format(ref)}"
        ax.axvline(0, color=C["axis"], linewidth=1.0)
        ax.set_xlabel(xlabel, color=C["ink2"], fontsize=10)

        lo = 0 if from_zero else min(0, min(values))
        hi = max(0, max(values), ref if pd.notna(ref) else 0)
        span = (hi - lo) or 1
        for i, v in enumerate(values):
            # Label just outside the bar end, on the side the bar points to --
            # but flipped inside when the outside spot would sit on the
            # reference line.
            near_ref = pd.notna(ref) and abs(v - ref) < 0.08 * span
            inside = near_ref and abs(v) > 0.12 * span
            sign = 1 if v >= 0 else -1
            offset = 0.015 * span * (-sign if inside else sign)
            ax.text(v + offset, i, fmt.format(v), va="center",
                    ha=("right" if v >= 0 else "left") if inside
                    else ("left" if v >= 0 else "right"),
                    fontsize=9, color=C["surface"] if inside else C["ink"])
        # Headroom on the right for the longest value label.
        ax.set_xlim(lo - (0 if from_zero else 0.10 * span), hi + 0.18 * span)

    draw(ax_ret, bars["mean_%"].tolist(), base["mean_%"].squeeze(),
         f"Mean return after {horizon} trading days (%)", "{:+.2f}%",
         from_zero=False)
    draw(ax_win, bars["win_rate_%"].tolist(), base["win_rate_%"].squeeze(),
         "Win rate (%)", "{:.1f}%", from_zero=True)

    # Cohort legend below both panels, where it can't cover a bar -- grouped by
    # hue so cohorts sharing one become a single entry. Skipped entirely for a
    # single cohort: the y-axis labels already carry identity, so a one-entry
    # legend is pure noise (and would sit on the axis label).
    by_hue = {}
    for cohort in bars["cohort"]:
        by_hue.setdefault(COHORT_COLOR.get(cohort, C["muted"]), []).append(cohort)
    if len(by_hue) > 1:
        fig.legend(
            handles=[plt.Rectangle((0, 0), 1, 1, color=hue) for hue in by_hue],
            labels=[" / ".join(dict.fromkeys(cs)) for cs in by_hue.values()],
            loc="lower center", ncol=len(by_hue), frameon=False, fontsize=9.5,
            labelcolor=C["ink2"], bbox_to_anchor=(0.5, -0.06))

    _title(ax_ret,
           f"Screen performance -- buy at {entry.replace('_', ' ')}, "
           f"sell {horizon} trading days later",
           "Index constituents, price-only returns, no costs; survivorship-biased "
           "(today's index members only)")
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor=C["surface"])
    plt.close(fig)
    print(f"Chart saved to {out_path}")


# --------------------------------------------------------------------------
# Wait x hold sweep grid
# --------------------------------------------------------------------------

def _diverging_cmap():
    """Two hues from the palette with a neutral midpoint -- the diverging ramp
    for a value whose sign is the point (above/below the baseline). Never a
    rainbow, and never a hue at the middle."""
    return LinearSegmentedColormap.from_list(
        "excess", [C["event"], C["grid"], C["close"]])


def plot_delay_grid(summary: pd.DataFrame, entry: str, baseline_label: str,
                    out_path: Path, dpi: int = 120) -> None:
    """Small-multiple heatmaps: does waiting before buying help?

    One panel per screen/cohort, x = holding period, y = extra days waited,
    each cell annotated with its mean return and coloured by *excess over the
    random-entry baseline* on a diverging scale whose neutral point is exactly
    zero -- so blue means "beat buying at random", orange means "worse".
    """
    cohorts = summary[summary["cohort"] != "-"]
    if cohorts.empty:
        print("No cohort to grid -- skipping the sweep chart.")
        return
    groups = list(cohorts.groupby(["screen", "cohort"], sort=False))
    delays = sorted(cohorts["delay"].unique())
    holds = sorted(cohorts["horizon"].unique())

    # Symmetric limits so the neutral colour lands on true zero.
    limit = max(abs(cohorts["excess_%"].min()), abs(cohorts["excess_%"].max()), 0.5)
    cmap = _diverging_cmap()

    fig, axes = plt.subplots(
        1, len(groups), figsize=(1.6 + 2.1 * len(holds) * len(groups),
                                 1.9 + 0.42 * len(delays)),
        squeeze=False)
    fig.patch.set_facecolor(C["surface"])
    mesh = None
    for ax, ((screen, cohort), sub) in zip(axes[0], groups):
        excess = sub.pivot_table(index="delay", columns="horizon",
                                 values="excess_%").reindex(
                                     index=delays, columns=holds)
        mean = sub.pivot_table(index="delay", columns="horizon",
                               values="mean_%").reindex(
                                   index=delays, columns=holds)
        ax.set_facecolor(C["surface"])
        mesh = ax.imshow(excess.to_numpy(), cmap=cmap, vmin=-limit, vmax=limit,
                         aspect="auto")
        ax.set_xticks(range(len(holds)), [str(h) for h in holds])
        ax.set_yticks(range(len(delays)), [str(d) for d in delays])
        ax.tick_params(colors=C["muted"], labelsize=9, length=0)
        for side in ax.spines.values():
            side.set_visible(False)
        # Surface-coloured gap between adjacent cells, so the fills read as
        # discrete values rather than one continuous wash.
        ax.set_xticks([x - 0.5 for x in range(1, len(holds))], minor=True)
        ax.set_yticks([y - 0.5 for y in range(1, len(delays))], minor=True)
        ax.grid(which="minor", color=C["surface"], linewidth=2)
        ax.tick_params(which="minor", length=0)
        ax.set_xlabel("hold (trading days)", color=C["ink2"], fontsize=9.5)
        ax.set_title(f"{screen.replace('_strategy', '')} · {cohort}",
                     fontsize=10.5, color=C["ink"], pad=8)
        if ax is axes[0][0]:
            ax.set_ylabel("extra days waited", color=C["ink2"], fontsize=9.5)
        # Value labels: ink on the pale middle of the ramp, surface on the
        # saturated ends, so they stay readable either way.
        for i in range(len(delays)):
            for j in range(len(holds)):
                value, shade = mean.iat[i, j], excess.iat[i, j]
                if pd.isna(value):
                    continue
                strong = abs(shade) > 0.55 * limit
                ax.text(j, i, f"{value:+.2f}", ha="center", va="center",
                        fontsize=9, color=C["surface"] if strong else C["ink"])

    bar = fig.colorbar(mesh, ax=axes[0], fraction=0.025, pad=0.02)
    bar.set_label("excess vs random entry (percentage points)",
                  color=C["ink2"], fontsize=9.5)
    bar.ax.tick_params(colors=C["muted"], labelsize=9)
    bar.outline.set_visible(False)

    fig.suptitle(f"Does waiting help?  entry = {entry.replace('_', ' ')} + x days"
                 f"  ·  cells show mean return %",
                 fontsize=12.5, color=C["ink"], fontweight="bold", x=0.02,
                 ha="left", y=1.04)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor=C["surface"])
    plt.close(fig)
    print(f"Chart saved to {out_path}")


# --------------------------------------------------------------------------
# Tier-3 financial trend
# --------------------------------------------------------------------------
# Small multiples, one metric per row: revenue dwarfs everything else (PGR is
# ~$88B revenue against ~$11B earnings), so a shared axis would flatten the
# other four into invisible slivers. Each panel gets its own scale, and the
# latest period is highlighted -- "where is it now" is the whole question.

# (key, label, kind) -- the row order of the grid.
FINANCIAL_ROWS = [
    ("revenue",        "Revenue",       "currency"),
    ("earnings",       "Earnings",      "currency"),
    ("margin_pct",     "Margin",        "percent"),
    ("fcf",            "Free cash flow", "currency"),
    ("debt_to_equity", "Debt / equity", "percent"),
]


def _pct_label(value) -> str:
    return f"{value:.1f}%"


def _margin_label(rows: list[dict]) -> str:
    """Name the margin actually plotted -- it is issuer-dependent.

    Yahoo reports no Operating Income for banks or insurers, so the collector
    falls back to pretax. Saying which one is on screen keeps the panel from
    silently comparing unlike things across tickers.
    """
    kinds = {r.get("margin_kind") for r in rows if r.get("margin_kind")}
    if kinds == {"pretax"}:
        return "Pretax margin\n(no Operating Income)"
    if kinds == {"operating"}:
        return "Operating margin"
    if not kinds:                    # nothing computed at all -- don't imply a basis
        return "Margin"
    return "Margin (mixed basis)"


def _draw_metric(ax, rows: list[dict], key: str, kind: str) -> None:
    """One panel: bars for currency, line+markers for a ratio, latest hot."""
    values = [r.get(key) for r in rows]
    labels = [r.get("period", "") for r in rows]

    def set_x() -> None:
        """Tick labels plus side padding, so the first/last never sit on the
        spine (a one-point series otherwise hangs its label off the panel)."""
        if labels:
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels)
            ax.set_xlim(-0.6, len(labels) - 0.4)
        else:
            ax.set_xticks([])

    if not any(v is not None for v in values):
        ax.text(0.5, 0.5, "n/a", transform=ax.transAxes, ha="center",
                va="center", fontsize=11, color=C["muted"])
        set_x()
        ax.set_yticks([])
        return

    x = range(len(values))
    plot = [v if v is not None else float("nan") for v in values]
    fmt = fmt_compact if kind == "currency" else _pct_label
    # The latest period carries the read, so it gets the accent hue.
    colors = [C["close"]] * len(plot)
    if colors:
        colors[-1] = C["event"]

    if kind == "currency":
        ax.bar(x, plot, color=colors, width=0.62)
    else:
        ax.plot(x, plot, color=C["close"], linewidth=1.8, marker="o",
                markersize=5, markerfacecolor=C["close"],
                markeredgecolor=C["surface"])
        ax.plot([len(plot) - 1], [plot[-1]], marker="o", markersize=7,
                color=C["event"], markeredgecolor=C["surface"])

    finite = [v for v in plot if v == v]
    if any(v < 0 for v in finite):
        ax.axhline(0, color=C["axis"], linewidth=1.0)

    for xi, v in zip(x, plot):
        if v != v:
            continue
        ax.annotate(fmt(v), (xi, v), textcoords="offset points",
                    xytext=(0, 5 if v >= 0 else -12), ha="center",
                    fontsize=8.5, color=C["ink"])
    ax.margins(y=0.28)
    set_x()
    ax.tick_params(axis="y", labelleft=False)   # every point is labelled


def plot_financials(fin: dict, ticker: str, out_path: Path,
                    dpi: int = 120) -> None:
    """The tier-3 financial trend: five metrics x (annual | quarterly).

    `fin` is `research_collect._financials` output -- {"annual": [...],
    "quarterly": [...]}, each a list of period dicts oldest -> newest. Panels
    with no data anywhere render `n/a` rather than an empty box.
    """
    annual = fin.get("annual") or []
    quarterly = fin.get("quarterly") or []
    columns = [("Annual", annual), ("Quarterly", quarterly)]

    fig, axes = _grid_figure(len(FINANCIAL_ROWS), 2, figsize=(11.5, 11.5))
    for r, (key, label, kind) in enumerate(FINANCIAL_ROWS):
        for c, (heading, rows) in enumerate(columns):
            ax = axes[r][c]
            _draw_metric(ax, rows, key, kind)
            if r == 0:
                ax.set_title(heading, loc="left", fontsize=10.5,
                             color=C["ink"], pad=8)
            if c == 0:
                text = _margin_label(annual + quarterly) if key == "margin_pct" else label
                ax.set_ylabel(text, color=C["ink2"], fontsize=9.5)

    subtitle = (f"{annual[0]['period']}-{annual[-1]['period']}" if annual else "no annual data")
    subtitle += (f"  ·  {quarterly[0]['period']}-{quarterly[-1]['period']}"
                 if quarterly else "  ·  no quarterly data")
    fig.suptitle(f"{ticker} -- financial trend  ·  {subtitle}",
                 fontsize=12.5, color=C["ink"], fontweight="bold", x=0.02,
                 ha="left", y=1.0)
    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor=C["surface"])
    plt.close(fig)
    print(f"Chart saved to {out_path}")


# --------------------------------------------------------------------------
# Universe risk/reward quadrant scatter
# --------------------------------------------------------------------------

#: How many names get a text label. 500 labels do not fit on a page, and the
#: ones worth naming are the extremes -- the best of the buy quadrant and the
#: worst of the avoid quadrant.
_SCATTER_LABELS = 12

#: Minimum gap, in axis units, between two placed labels. Both axes are 0-100 so
#: one number covers them.
_LABEL_GAP = 6.0


def _label_extremes(ax, points: pd.DataFrame, xt: float, rt: float) -> None:
    """Name the most extreme point in each quadrant, skipping collisions.

    "Extreme" is distance from the quadrant threshold corner, so each quadrant
    contributes its own most characteristic names rather than the whole label
    budget going to one end of one axis.
    """
    corners = ((-1, 1), (1, 1), (-1, -1), (1, -1))   # (risk dir, reward dir)
    per_corner = max(1, _SCATTER_LABELS // len(corners))
    placed: list[tuple[float, float]] = []
    for dx, dy in corners:
        side = points[((points["risk"] > xt) == (dx > 0))
                      & ((points["reward"] > rt) == (dy > 0))]
        if side.empty:
            continue
        # Distance from the crossing point, so "most extreme" means furthest into
        # the corner on both axes at once.
        score = ((side["risk"] - xt) * dx + (side["reward"] - rt) * dy)
        # Consider more candidates than needed, since collisions skip some.
        shown = 0
        for _, row in side.loc[score.nlargest(per_corner * 4).index].iterrows():
            if shown >= per_corner or len(placed) >= _SCATTER_LABELS:
                break
            x, y = float(row["risk"]), float(row["reward"])
            if any(abs(x - px) < _LABEL_GAP and abs(y - py) < _LABEL_GAP
                   for px, py in placed):
                continue
            ax.annotate(str(row["ticker"]), xy=(x, y), xytext=(6, 4),
                        textcoords="offset points", fontsize=8.5,
                        color=C["ink2"], zorder=6)
            placed.append((x, y))
            shown += 1


def plot_risk_reward(table: pd.DataFrame, chart_cfg: dict, out_path: Path,
                     dpi: int = 120) -> None:
    """The quadrant plane: reward (y) against risk (x), one point per ticker.

    `table` is `universe_scan.build_table` output; `chart_cfg` is the `universe`
    config section, read only for the two thresholds and the axis labels. The
    caller shapes the frame -- this does no scoring and no lookups, same contract
    as every other builder here.

    **Colour is by hue role, not by quadrant.** The palette has exactly two hues,
    so four quadrant colours would mean inventing two -- instead the quadrant is
    drawn with divider lines and the points carry the one distinction that is
    actually categorical: `event` (orange) for a name the veto excluded, `close`
    (blue) for everything else. That way the chart answers the question the veto
    exists to raise -- do excluded names really sit in the high-risk half -- and
    it answers it visually rather than by assertion.

    Unmeasurable rows are dropped rather than plotted at zero: a `None` axis has
    no position, and plotting it as 0 would file it in the best quadrant.
    """
    rt = float(chart_cfg.get("reward_min", 60))
    xt = float(chart_cfg.get("risk_max", 25))

    points = table.dropna(subset=["reward", "risk"]) if not table.empty \
        else table
    dropped = len(table) - len(points)

    fig, ax = _grid_figure(1, 1, figsize=(11, 8.5))
    ax.grid(axis="both", color=C["grid"], linewidth=0.8)

    # The quadrant dividers, drawn under the points.
    ax.axvline(xt, color=C["axis"], linewidth=1.1, zorder=1)
    ax.axhline(rt, color=C["axis"], linewidth=1.1, zorder=1)

    if not points.empty:
        vetoed = points["vetoed"].fillna(False).astype(bool) \
            if "vetoed" in points.columns else pd.Series(False, index=points.index)
        for mask, colour, label, size in (
                (~vetoed, C["close"], "not excluded", 42),
                (vetoed, C["event"], "excluded by a veto rule", 66)):
            subset = points[mask]
            if subset.empty:
                continue
            ax.scatter(subset["risk"], subset["reward"], s=size, color=colour,
                       alpha=0.75, edgecolor=C["surface"], linewidth=0.7,
                       zorder=3, label=f"{label} ({len(subset)})")

        # Name the corner-most point of each quadrant rather than the top and
        # bottom of one axis: sorting by reward alone picks a cluster of near-
        # identical names and stacks their labels on top of each other.
        # `_label_extremes` also skips any label that would collide with one
        # already placed, so a dense corner silently shows fewer names instead of
        # an illegible pile.
        _label_extremes(ax, points, xt, rt)

    ax.set_xlabel("risk  (higher = more dangerous)", color=C["ink2"],
                  fontsize=10)
    ax.set_ylabel("reward  (higher = better expected return)", color=C["ink2"],
                  fontsize=10)
    ax.set_xlim(-2, 102)
    ax.set_ylim(-2, 102)

    # Quadrant names in the corners, so the plane reads without a legend.
    for x, y, text, align in ((1, 99, "low risk · high reward", "left"),
                              (99, 99, "high risk · high reward", "right"),
                              (1, 1, "low risk · low reward", "left"),
                              (99, 1, "high risk · low reward", "right")):
        ax.text(x, y, text, fontsize=9, color=C["muted"], ha=align,
                va="top" if y > 50 else "bottom", zorder=2)

    if not points.empty:
        # Below the axis, not inside it: at 500 points every interior position
        # covers data, and the upper-centre default sat right on the dense band.
        ax.legend(frameon=False, fontsize=9, labelcolor=C["ink2"],
                  loc="upper center", bbox_to_anchor=(0.5, -0.09), ncol=2)

    stage = (table["stage"].dropna().iloc[0]
             if "stage" in table.columns and table["stage"].notna().any()
             else "?")
    used = table["risk_metrics_used"].mean() \
        if "risk_metrics_used" in table.columns else float("nan")
    total = table["risk_metrics_total"].max() \
        if "risk_metrics_total" in table.columns else float("nan")
    # The stage belongs on the chart, not just in the log: a `fast` risk reading
    # omits every SEC filing flag, so it is not comparable to a `deep` one and a
    # reader who does not know that will over-trust the x axis.
    subtitle = (f"{len(points)} of {len(table)} tickers plotted"
                + (f" · {dropped} unmeasurable, not shown" if dropped else "")
                + f" · stage {stage}"
                + (f" · risk from {used:.1f}/{total:.0f} metrics"
                   if pd.notna(used) and pd.notna(total) else "")
                + f" · thresholds reward {rt:g} / risk {xt:g}")
    _title(ax, "Universe risk vs reward", subtitle)

    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor=C["surface"])
    plt.close(fig)
    print(f"Chart saved to {out_path}")


# --------------------------------------------------------------------------
# Industry valuation: what a price move was actually made of
# --------------------------------------------------------------------------

def plot_industry_valuation(table: pd.DataFrame, chart_cfg: dict, out_path: Path,
                            dpi: int = 120, window_days: int = 126,
                            max_rows: int = 24) -> None:
    """Per industry, the multiple change beside the earnings change.

    `table` is `industry_valuation.build_table`'s frame, already sorted with the
    most de-rated first. Grouped horizontal bars: over the window, an industry's
    price move splits exactly into what the market paid (the multiple) and what
    the companies earned. The pairing is the whole point -- a bucket whose
    earnings bar is up while its multiple bar is down has been de-rated, which is
    a different event from one where both fell together.

    **Two hues, and they carry the one genuinely categorical distinction here**:
    multiple vs earnings. The palette has exactly two, so colouring by sector was
    never available -- there are eleven of them. Same ruling `plot_risk_reward`
    records for its quadrants.

    With ~70 buckets the chart would be unreadable, so it shows the extremes:
    the most de-rated and the most re-rated, which is where a dislocation lives
    by definition. The subtitle says how many were dropped, and carries the
    survivorship caveat -- membership is today's, applied backwards.
    """
    rows = table.dropna(subset=["multiple_chg_pct", "earnings_chg_pct"])
    if rows.empty:
        print("No bucket had a measurable decomposition -- skipping the chart.")
        return

    # Selection: **flagged buckets first, then the extremes.** Sorting by
    # multiple change and keeping both tails looks right and is wrong -- a
    # de-rated industry is one whose multiple fell while earnings held, which
    # puts it in the *middle* of that sort, not at either end. The first live
    # chart dropped Pharmaceuticals and Semiconductors for exactly that reason,
    # i.e. it hid the two buckets the run had actually concluded something
    # about. Whatever the module flagged has to survive the truncation.
    total = len(rows)
    if total > max_rows:
        flagged = rows[rows["flag"].fillna("") != ""]
        if len(flagged) > max_rows:         # too many to show: the most extreme
            flagged = flagged.reindex(
                flagged["pe_z"].abs().sort_values(ascending=False).index[:max_rows])
        room = max_rows - len(flagged)
        rest = rows[~rows.index.isin(flagged.index)]
        half = room // 2
        rows = pd.concat([rest.head(half), flagged, rest.tail(room - half)]
                         if room > 0 else [flagged])
        rows = rows.sort_values("multiple_chg_pct", ascending=True)
    rows = rows.iloc[::-1]                  # most de-rated ends up on top

    labels = [f"{b}{'  · ' + f if f else ''}  (n={int(n)})"
              for b, f, n in zip(rows["bucket"], rows["flag"].fillna(""),
                                 rows["window_members_n"])]
    y = np.arange(len(rows))
    height = 0.38

    fig, ax = plt.subplots(figsize=(11, 2.2 + 0.52 * len(rows)))
    fig.patch.set_facecolor(C["surface"])
    ax.set_facecolor(C["surface"])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(C["axis"])
    ax.tick_params(colors=C["muted"], labelsize=9)
    ax.grid(axis="x", color=C["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, color=C["ink2"], fontsize=9.5)

    multiple = rows["multiple_chg_pct"].astype(float).tolist()
    earnings = rows["earnings_chg_pct"].astype(float).tolist()
    ax.barh(y + height / 2, multiple, height=height, color=C["event"],
            label="multiple (P/E)")
    ax.barh(y - height / 2, earnings, height=height, color=C["close"],
            label="earnings (TTM EPS)")
    ax.axvline(0, color=C["axis"], linewidth=1.0)

    span = (max(max(multiple), max(earnings), 0)
            - min(min(multiple), min(earnings), 0)) or 1
    for offset, values in ((height / 2, multiple), (-height / 2, earnings)):
        for i, v in enumerate(values):
            sign = 1 if v >= 0 else -1
            ax.text(v + 0.012 * span * sign, y[i] + offset, f"{v:+.1f}%",
                    va="center", ha="left" if v >= 0 else "right",
                    fontsize=8.5, color=C["ink"])
    lo = min(min(multiple), min(earnings), 0)
    hi = max(max(multiple), max(earnings), 0)
    ax.set_xlim(lo - 0.14 * span, hi + 0.14 * span)
    ax.set_xlabel(f"Change over the last {window_days} trading days (%)",
                  color=C["ink2"], fontsize=10)

    dropped = total - len(rows)
    scan_date = str(table["scan_date"].iat[0]) if "scan_date" in table else ""
    # Not `_title`: it offsets the subtitle by a fixed *axes fraction*, and this
    # panel's height grows with the bucket count, so at 30 rows that fraction is
    # a third of an inch and the lines collide. Both go in offset points, which
    # is invariant to the panel height, and the subtitle needs two lines here --
    # so the title is stacked above it rather than left to `set_title`, which
    # reserves only one.
    ax.annotate(f"price = multiple x earnings, split in logs · {total} bucket(s)"
                + (f", {dropped} unflagged mid-range not shown" if dropped else "")
                + f"\nmembership as of {scan_date}, applied backwards "
                  "(survivorship-biased)",
                xy=(0, 1), xycoords="axes fraction", xytext=(0, 8),
                textcoords="offset points", fontsize=9.5, color=C["ink2"],
                va="bottom")
    ax.annotate("Industry valuation -- what the price move was made of",
                xy=(0, 1), xycoords="axes fraction", xytext=(0, 40),
                textcoords="offset points", fontsize=13, color=C["ink"],
                fontweight="bold", va="bottom")

    fig.tight_layout()
    # Below the whole figure, never inside the axes: the longest bar is by
    # construction at one end of the sort, so every in-axes corner is a corner
    # some bucket reaches. Figure coordinates, so it does not drift as the
    # panel grows with the bucket count.
    fig.legend(*ax.get_legend_handles_labels(), loc="lower center",
               bbox_to_anchor=(0.5, -0.03), ncol=2, frameon=False,
               fontsize=9.5, labelcolor=C["ink2"])
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor=C["surface"])
    plt.close(fig)
    print(f"Chart saved to {out_path}")
