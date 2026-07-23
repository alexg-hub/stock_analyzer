"""
Screen module: pullback to a rising long-term SMA within an uptrend.

Setup detected (all must be true on the most recent trading day):

T1. Rising SMA -- the `sma_days` SMA of Close is higher than it was
    `sma_slope_lookback_days` trading days ago.
T2. Sustained uptrend -- the Close was above the SMA on at least
    `min_days_above_sma_pct` of the previous `trend_lookback_days`
    trading days (excluding today), i.e. today's touch is the exception
    in a year-long uptrend, not life on the SMA.
T3. Touch -- today's Close is within `touch_band_pct` of the SMA
    (either side of it).

Optionally (`alert_only_on_band_entry`), the signal only fires on the day
the close *enters* the band from above, so a stock sitting on its SMA does
not re-alert every night.

Note: unlike the breakout screen's shift(1) prior windows, the SMA here
includes the current day -- that is the charting-standard SMA a "touch of
the 150-day line" refers to. The trend-persistence count (T2) does use
shift(1) so the touch day doesn't count against itself.

All parameters live in the `pullback_strategy` section of config.json.
"""

import pandas as pd

import charts
from scanner_common import ScanResult, fmt_value, single_ticker_panel

# Config section this screen reads (run_scanners.py registry contract).
CONFIG_KEY = "pullback_strategy"
# Side-bar color of this screen's Discord embed cards (palette blue).
EMBED_COLOR = 0x2A78D6


# --------------------------------------------------------------------------
# Vectorized pullback screen
# --------------------------------------------------------------------------

def compute_pullback_signals(data: pd.DataFrame, strategy: dict) -> dict[str, pd.DataFrame]:
    """Compute every pullback condition for every day and every ticker.

    Same conventions as breakout_scanner.compute_signals: every input and
    output is a (days, tickers) DataFrame, no per-ticker loops, and NaNs
    (insufficient history) compare as False and drop out.
    """
    sma_days = strategy["sma_days"]
    band = strategy["touch_band_pct"]
    lookback = strategy["trend_lookback_days"]
    slope_days = strategy["sma_slope_lookback_days"]
    min_above = strategy["min_days_above_sma_pct"]

    close = data["Close"]
    sma = close.rolling(sma_days).mean()

    dist_pct = close / sma - 1
    sma_slope_pct = sma / sma.shift(slope_days) - 1

    # T1: the SMA itself is rising.
    is_sma_rising = sma > sma.shift(slope_days)

    # T2: close spent enough of the prior lookback above the SMA. The
    # comparison is False where the SMA is still NaN, which only deflates
    # the ratio during warm-up (already excluded by the NaN rolling mean).
    above = (close > sma).astype(float)
    pct_days_above = above.shift(1).rolling(lookback).mean()
    is_trend_persistent = pct_days_above >= min_above

    # T3: today's close is within the touch band around the SMA.
    is_touch = dist_pct.abs() <= band

    # Fresh entry: yesterday's close was still above the band, so today is
    # the day the pullback actually reached the SMA.
    is_band_entry = close.shift(1) > (1 + band) * sma.shift(1)

    signal = is_sma_rising & is_trend_persistent & is_touch
    if strategy.get("alert_only_on_band_entry", True):
        signal &= is_band_entry

    return {
        "sma": sma,
        "dist_pct": dist_pct,
        "pct_days_above": pct_days_above,
        "sma_slope_pct": sma_slope_pct,
        "is_sma_rising": is_sma_rising,
        "is_trend_persistent": is_trend_persistent,
        "is_touch": is_touch,
        "is_band_entry": is_band_entry,
        "signal": signal,
    }


def touch_miss_reason(row: pd.Series, strategy: dict) -> str:
    """Explain why a touch day (T3 true) did not fire the signal.

    `row` is a build_calc_table row (percent columns already x100).
    """
    lookback = strategy["trend_lookback_days"]
    slope_days = strategy["sma_slope_lookback_days"]
    min_above = strategy["min_days_above_sma_pct"]
    if not row["T1_RisingSMA"]:
        return (f"SMA not rising: {row['SmaSlopePct']:+.2f}% vs "
                f"{slope_days} days ago")
    if not row["T2_TimeAbove"]:
        return (f"trend too weak: above SMA only {row['AbovePct']:.0f}% of the "
                f"last {lookback} days < {min_above:.0%} required")
    return "already inside the touch band (no fresh entry from above)"


def find_pullbacks(data: pd.DataFrame, strategy: dict) -> pd.DataFrame:
    """Screen the whole universe on the most recent trading day.

    Returns a ticker-indexed DataFrame of the tickers whose close touched
    their rising SMA today after a sustained uptrend.
    """
    needed = strategy["sma_days"] + strategy["trend_lookback_days"]
    if len(data) <= needed:
        print(f"WARNING: only {len(data)} rows of history but the pullback "
              f"screen needs > {needed} ({strategy['sma_days']}d SMA + "
              f"{strategy['trend_lookback_days']}d trend lookback) -- NO "
              f"signal can ever fire. Increase data.download_period in config.json.")

    signals = compute_pullback_signals(data, strategy)
    last = {name: df.iloc[-1] for name, df in signals.items()}

    signal_today = last["signal"].fillna(False)
    tickers = signal_today[signal_today].index.tolist()
    hits = pd.DataFrame(
        {
            "Close": data["Close"].iloc[-1][tickers].round(2),
            "SMA": last["sma"][tickers].round(2),
            "Dist %": (last["dist_pct"][tickers] * 100).round(2),
            "Above %": (last["pct_days_above"][tickers] * 100).round(1),
            "SMA Slope %": (last["sma_slope_pct"][tickers] * 100).round(2),
        },
        index=pd.Index(tickers, name="Ticker"),
    )

    scan_date = data.index[-1].date()
    print(f"Scan date: {scan_date} -- {len(hits)} SMA-pullback setup(s).")
    return hits


# --------------------------------------------------------------------------
# Registry contract: scan / format_section / plot_hit
# --------------------------------------------------------------------------

def scan(data: pd.DataFrame, strategy: dict) -> ScanResult:
    hits = find_pullbacks(data, strategy)
    sma_days = strategy["sma_days"]
    return ScanResult(
        title=f"Pullback to rising {sma_days}-day SMA",
        hits=hits,
        strategy=strategy,
    )


def describe_hit(row, strategy: dict) -> str:
    """Embed-card description of one confirmed pullback setup."""
    return (f"Close {fmt_value(row['Close'])} touched the "
            f"{strategy['sma_days']}d SMA {fmt_value(row['SMA'])} "
            f"({row['Dist %']:+.1f}%), above SMA {row['Above %']:.0f}% of last "
            f"{strategy['trend_lookback_days']}d, "
            f"SMA {row['SMA Slope %']:+.1f}% over "
            f"{strategy['sma_slope_lookback_days']}d")


def build_calc_table(data: pd.DataFrame, signals: dict, ticker: str) -> pd.DataFrame:
    """Flatten one ticker's OHLCV + every intermediate into a per-day table
    (consumed by charts.plot_pullback and the backtest)."""
    def col(field):
        return data[field][ticker]

    return pd.DataFrame(
        {
            "Open": col("Open"),
            "High": col("High"),
            "Low": col("Low"),
            "Close": col("Close"),
            "Volume": col("Volume"),
            "SMA": signals["sma"][ticker],
            "DistPct": signals["dist_pct"][ticker] * 100,
            "AbovePct": signals["pct_days_above"][ticker] * 100,
            "SmaSlopePct": signals["sma_slope_pct"][ticker] * 100,
            "T1_RisingSMA": signals["is_sma_rising"][ticker],
            "T2_TimeAbove": signals["is_trend_persistent"][ticker],
            "T3_Touch": signals["is_touch"][ticker],
            "BandEntry": signals["is_band_entry"][ticker],
            "SIGNAL": signals["signal"][ticker],
        }
    ).rename_axis("Date")


def plot_hit(data: pd.DataFrame, ticker: str, strategy: dict,
             chart_cfg: dict, out_path) -> None:
    """Render the alert chart for one hit: the recent window of its close,
    SMA, and touch band, with today's touch marked."""
    panel = single_ticker_panel(data, ticker)
    table = build_calc_table(panel, compute_pullback_signals(panel, strategy), ticker)
    table = table.tail(chart_cfg.get("lookback_days", 250))
    charts.plot_pullback(table, strategy, ticker, out_path,
                         dpi=chart_cfg.get("dpi", 150))


if __name__ == "__main__":
    print("This is a screen module -- run the nightly scan with: python run_scanners.py")
