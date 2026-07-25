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
import pandas as pd

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
    subtitle = (f"{n_sig} signal day(s)" if n_sig else "no signal days") + \
        f" -- below SMA >= {strategy['min_days_below_pct']:.0%} of last " \
        f"{strategy['below_lookback_days']}d, volume {vol_mult}x {vol_days}d average, " \
        f"candle body >= {strategy.get('min_candle_body_pct', 0.0):.1%}"
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
# Universe-backtest summary
# --------------------------------------------------------------------------

# Cohort -> hue, assigned in fixed order (never cycled): hits are the primary
# series, the non-firing cohorts the secondary one.
COHORT_COLOR = {"hit": C["close"], "near": C["event"],
                "touch-no-fire": C["event"]}


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

    # Cohort legend once, below both panels, where it can't cover a bar.
    # Grouped by hue, so the cohorts that share one (every "did not fire"
    # flavour) become a single entry instead of two identical swatches.
    by_hue = {}
    for cohort in bars["cohort"]:
        by_hue.setdefault(COHORT_COLOR.get(cohort, C["muted"]), []).append(cohort)
    fig.legend(
        handles=[plt.Rectangle((0, 0), 1, 1, color=hue) for hue in by_hue],
        labels=[" / ".join(dict.fromkeys(cs)) for cs in by_hue.values()],
        loc="lower center", ncol=len(by_hue), frameon=False, fontsize=9.5,
        labelcolor=C["ink2"], bbox_to_anchor=(0.5, -0.02))

    _title(ax_ret,
           f"Screen performance -- buy at {entry.replace('_', ' ')}, "
           f"sell {horizon} trading days later",
           "S&P 500, price-only returns, no costs; survivorship-biased "
           "(today's index members only)")
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor=C["surface"])
    plt.close(fig)
    print(f"Chart saved to {out_path}")
