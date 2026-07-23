"""
Screen module: upward breakout from a horizontal consolidation range.

Setup detected (all three must be true on the most recent trading day):

1. Horizontal movement -- over the previous `consolidation_window_days`
   trading days (excluding today), (max High - min Low) / min Low must be
   <= `max_consolidation_range_pct`.
2. Upward breakout -- today's Close exceeds `breakout_multiplier` x the max
   High of that prior window (e.g. 1.01 = closes at least 1% above it).
3. Volume surge -- today's Volume >= `volume_surge_multiplier` x the SMA
   of Volume over the previous `volume_sma_days` trading days.

This file contains only the (fully vectorized) condition math and this
screen's alert section/chart. Data download, fundamentals, and Discord
delivery live in scanner_common.py; run_scanners.py is the entry point.

All parameters live in the `strategy` section of config.json.
"""

import pandas as pd

import charts
from scanner_common import ScanResult, fmt_value, fundamentals_lines, single_ticker_panel

# Config section this screen reads (run_scanners.py registry contract).
CONFIG_KEY = "strategy"


# --------------------------------------------------------------------------
# Vectorized breakout screen
# --------------------------------------------------------------------------

def compute_signals(data: pd.DataFrame, strategy: dict) -> dict[str, pd.DataFrame]:
    """Compute every breakout condition for every day and every ticker.

    Each of close/high/low/volume below is a DataFrame of shape
    (days, tickers); every rolling/comparison operates on the whole
    universe simultaneously -- no per-ticker loops.

    Returns a dict of (days, tickers) DataFrames: the intermediate
    series, the three boolean conditions, and the combined `signal`.
    """
    window = strategy["consolidation_window_days"]
    max_range = strategy["max_consolidation_range_pct"]
    brk_mult = strategy.get("breakout_multiplier", 1.0)
    vol_days = strategy["volume_sma_days"]
    vol_mult = strategy["volume_surge_multiplier"]

    close = data["Close"]
    high = data["High"]
    low = data["Low"]
    volume = data["Volume"]

    # Prior-window extremes: shift(1) excludes the current day, so the
    # 126-day range is measured strictly before the potential breakout day.
    prior_high = high.shift(1).rolling(window).max()
    prior_low = low.shift(1).rolling(window).min()

    # Condition 1: tight horizontal range over the prior window.
    range_pct = (prior_high - prior_low) / prior_low
    is_consolidating = range_pct <= max_range

    # Condition 2: today's close clears the prior window's high by the
    # breakout multiplier (e.g. 1.01 = at least 1% above the range high).
    is_breakout = close > brk_mult * prior_high

    # Condition 3: volume surge vs. the prior 30-day average volume.
    prior_vol_sma = volume.shift(1).rolling(vol_days).mean()
    vol_ratio = volume / prior_vol_sma
    is_volume_surge = volume >= vol_mult * prior_vol_sma

    # NaNs (insufficient history / dead tickers) compare as False, so
    # they drop out automatically.
    return {
        "prior_high": prior_high,
        "prior_low": prior_low,
        "range_pct": range_pct,
        "prior_vol_sma": prior_vol_sma,
        "vol_ratio": vol_ratio,
        "is_consolidating": is_consolidating,
        "is_breakout": is_breakout,
        "is_volume_surge": is_volume_surge,
        "signal": is_consolidating & is_breakout & is_volume_surge,
    }


def near_miss_reason(close, prior_high, range_pct, vol_ratio, strategy: dict) -> str:
    """Explain which single condition a 2-of-3 near-miss failed.

    `range_pct` is a fraction (0.28 = 28%), matching compute_signals.
    """
    max_range = strategy["max_consolidation_range_pct"]
    brk_mult = strategy.get("breakout_multiplier", 1.0)
    vol_mult = strategy["volume_surge_multiplier"]
    if range_pct > max_range:
        return f"range too wide: {range_pct * 100:.1f}% > {max_range:.0%} limit"
    if close <= brk_mult * prior_high:
        return (f"no breakout: Close {close:.2f} <= {brk_mult} x prior high "
                f"{prior_high:.2f} = {brk_mult * prior_high:.2f}")
    return f"volume too low: {vol_ratio:.2f}x < {vol_mult}x required"


def find_breakouts(data: pd.DataFrame, strategy: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Screen the whole universe on the most recent trading day.

    Returns two DataFrames indexed by ticker:
      * hits -- tickers passing all three conditions;
      * near-misses -- tickers passing exactly two, with the failed
        condition explained (C2 failures only when the close is within
        `near_miss_max_gap_pct` of the required breakout level, so a
        volume spike deep inside a range doesn't spam the alert).
    """
    window = strategy["consolidation_window_days"]
    if len(data) <= window:
        print(f"WARNING: only {len(data)} rows of history for a {window}-day "
              f"consolidation window -- the rolling window never fills, so NO "
              f"signal can ever fire. Increase data.download_period in config.json.")

    signals = compute_signals(data, strategy)
    last = {name: df.iloc[-1] for name, df in signals.items()}

    def day_stats(tickers: list[str]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Close": data["Close"].iloc[-1][tickers].round(2),
                "Range High": last["prior_high"][tickers].round(2),
                "Range %": (last["range_pct"][tickers] * 100).round(1),
                "Vol Ratio": last["vol_ratio"][tickers].round(2),
            },
            index=pd.Index(tickers, name="Ticker"),
        )

    signal_today = last["signal"].fillna(False)
    hits = day_stats(signal_today[signal_today].index.tolist())

    # Near-misses: exactly two of the three conditions true today.
    conds = pd.DataFrame(
        {c: last[c].fillna(False)
         for c in ("is_consolidating", "is_breakout", "is_volume_surge")}
    )
    near_mask = (conds.sum(axis=1) == 2) & ~signal_today
    # For C2 failures, require the close to be near the breakout level.
    gap = strategy.get("near_miss_max_gap_pct", 0.05)
    brk_level = strategy.get("breakout_multiplier", 1.0) * last["prior_high"]
    near_mask &= conds["is_breakout"] | (data["Close"].iloc[-1] >= (1 - gap) * brk_level)

    near = day_stats(near_mask[near_mask].index.tolist())
    near["Reason"] = [
        near_miss_reason(row["Close"], row["Range High"], row["Range %"] / 100,
                         row["Vol Ratio"], strategy)
        for _, row in near.iterrows()
    ]

    scan_date = data.index[-1].date()
    print(f"Scan date: {scan_date} -- {len(hits)} breakout(s), "
          f"{len(near)} near-miss candidate(s).")
    return hits, near


# --------------------------------------------------------------------------
# Registry contract: scan / format_section / plot_hit
# --------------------------------------------------------------------------

def scan(data: pd.DataFrame, strategy: dict) -> ScanResult:
    hits, near = find_breakouts(data, strategy)
    window = strategy["consolidation_window_days"]
    return ScanResult(
        title=f"Breakout from {window}-day consolidation",
        hits=hits,
        near=near,
        strategy=strategy,
    )


def format_section(result: ScanResult, fund_cfg: dict = None) -> str:
    """This screen's block of the combined Discord message."""
    fund_cfg = fund_cfg or {}
    lines = [f"__{result.title}__"]
    if result.hits.empty and result.near.empty:
        lines.append("No breakouts and no near-miss candidates today.")
        return "\n".join(lines)

    if result.hits.empty:
        lines.append("No confirmed breakouts today.")
    else:
        lines.append(f"{len(result.hits)} breakout(s):")
        for ticker, row in result.hits.iterrows():
            lines.append(
                f"**{ticker}** | Close {fmt_value(row['Close'])} broke range high "
                f"{fmt_value(row['Range High'])} (range {fmt_value(row['Range %'])}%, "
                f"vol {fmt_value(row['Vol Ratio'])}x avg)"
            )
            lines += [f"  {l}" for l in fundamentals_lines(row, fund_cfg)]

    if not result.near.empty:
        lines.append(f"{len(result.near)} near-miss candidate(s) (failed one condition):")
        for ticker, row in result.near.iterrows():
            lines.append(f"**{ticker}** | {row['Reason']}")
            lines += [f"  {l}" for l in fundamentals_lines(row, fund_cfg)]
    return "\n".join(lines)


def build_calc_table(data: pd.DataFrame, signals: dict, ticker: str) -> pd.DataFrame:
    """Flatten one ticker's OHLCV + every intermediate into a per-day table
    (consumed by charts.plot_breakout and the backtest)."""
    def col(field):
        return data[field][ticker]

    return pd.DataFrame(
        {
            "Open": col("Open"),
            "High": col("High"),
            "Low": col("Low"),
            "Close": col("Close"),
            "Volume": col("Volume"),
            "PriorHigh": signals["prior_high"][ticker],
            "PriorLow": signals["prior_low"][ticker],
            "RangePct": signals["range_pct"][ticker] * 100,
            "VolSMA": signals["prior_vol_sma"][ticker],
            "VolRatio": signals["vol_ratio"][ticker],
            "C1_Consolidating": signals["is_consolidating"][ticker],
            "C2_Breakout": signals["is_breakout"][ticker],
            "C3_VolumeSurge": signals["is_volume_surge"][ticker],
            "SIGNAL": signals["signal"][ticker],
        }
    ).rename_axis("Date")


def plot_hit(data: pd.DataFrame, ticker: str, strategy: dict,
             chart_cfg: dict, out_path) -> None:
    """Render the alert chart for one hit: the recent window of its close,
    consolidation band, and volume, with today's breakout marked."""
    panel = single_ticker_panel(data, ticker)
    table = build_calc_table(panel, compute_signals(panel, strategy), ticker)
    table = table.tail(chart_cfg.get("lookback_days", 250))
    charts.plot_breakout(table, strategy, ticker, out_path,
                         dpi=chart_cfg.get("dpi", 150))


if __name__ == "__main__":
    print("This is a screen module -- run the nightly scan with: python run_scanners.py")
