"""
Screen module: first reclaim of a long-term SMA after a long downtrend
(Weinstein "Stage 2 transition").

Setup detected (all must be true on the most recent trading day):

R1. Above the cross level -- today's Close is above
    `(1 + cross_margin_pct) x` the `sma_days` SMA, so a marginal poke
    over the line doesn't count.
R2. Long prior downtrend -- the Close was below the SMA on at least
    `min_days_below_pct` of the previous `below_lookback_days` trading
    days (excluding today), so the reclaim is an event, not chop around
    a flat SMA.
R3. Volume confirmation -- today's Volume is at least
    `volume_surge_multiplier x` the average of the previous
    `volume_sma_days` days (a reclaim on dead volume usually fails).
R4. (optional) SMA no longer falling steeply -- when `min_sma_slope_pct`
    is set (not null), the SMA's change over `sma_slope_lookback_days`
    must be at least that fraction. Defaults to off (null): it filters
    knife-catching but also delays entry.
R5. Long green candle -- today's Close exceeds the Open by at least
    `min_candle_body_pct` (Close > (1 + min_candle_body_pct) x Open), so
    the reclaim day itself closes strongly instead of a weak/red cross.

Optionally (`alert_only_on_cross`), the signal only fires on the day the
close first crosses the level, so a stock that stays above it does not
re-alert every night.

Near-misses: a genuine fresh cross above the level *out of a real downtrend*
(R2 required, like a hit) where one OR two of the remaining confirmations
{R3 volume, R5 candle, R4 slope when enabled} fail (0 failing = hit; 3+ =
too far off, dropped).

Same conventions as the other screens: the SMA includes the current day
(charting-standard "crossed the 200-day line"); the below-count (R2) and
the volume baseline (R3) use shift(1) so the cross day doesn't count
toward its own history.

All parameters live in the `reclaim_strategy` section of config.json.
"""

import pandas as pd

import charts
from scanner_common import ScanResult, fmt_value, single_ticker_panel

# Config section this screen reads (run_scanners.py registry contract).
CONFIG_KEY = "reclaim_strategy"
# Side-bar color of this screen's Discord embed cards (green -- new trend).
EMBED_COLOR = 0x2E9C6B


def required_history(strategy: dict) -> int:
    """Trading days of history needed before this screen can ever fire."""
    return strategy["sma_days"] + strategy["below_lookback_days"]


# --------------------------------------------------------------------------
# Vectorized reclaim screen
# --------------------------------------------------------------------------

def compute_reclaim_signals(data: pd.DataFrame, strategy: dict) -> dict[str, pd.DataFrame]:
    """Compute every reclaim condition for every day and every ticker.

    Same conventions as the other compute_* functions: every input and
    output is a (days, tickers) DataFrame, no per-ticker loops, and NaNs
    (insufficient history) compare as False and drop out.
    """
    sma_days = strategy["sma_days"]
    lookback = strategy["below_lookback_days"]
    min_below = strategy["min_days_below_pct"]
    margin = strategy["cross_margin_pct"]
    vol_days = strategy["volume_sma_days"]
    vol_mult = strategy["volume_surge_multiplier"]
    slope_days = strategy["sma_slope_lookback_days"]
    min_slope = strategy.get("min_sma_slope_pct")
    min_body = strategy.get("min_candle_body_pct", 0.0)

    close = data["Close"]
    open_ = data["Open"]
    volume = data["Volume"]
    sma = close.rolling(sma_days).mean()
    level = (1 + margin) * sma

    dist_pct = close / sma - 1
    sma_slope_pct = sma / sma.shift(slope_days) - 1

    # R1: close above the cross level (SMA + margin).
    is_above_level = close > level

    # R2: close spent enough of the prior lookback below the SMA.
    below = (close < sma).astype(float)
    pct_days_below = below.shift(1).rolling(lookback).mean()
    is_downtrend = pct_days_below >= min_below

    # R3: volume surge vs the prior-days average (breakout convention:
    # the baseline excludes the cross day itself).
    vol_sma = volume.shift(1).rolling(vol_days).mean()
    vol_ratio = volume / vol_sma
    is_volume_surge = volume >= vol_mult * vol_sma

    # R4 (optional): the SMA is no longer falling steeply.
    is_slope_ok = sma_slope_pct >= min_slope if min_slope is not None else None

    # R5: the reclaim day is a green candle whose body (Close over Open) is
    # at least min_candle_body_pct -- a weak/red cross usually fails back.
    body_pct = close / open_ - 1
    is_long_green_candle = close > (1 + min_body) * open_

    # Fresh cross: yesterday's close was not yet above the level, so today
    # is the day the reclaim actually happened.
    is_fresh_cross = is_above_level & ~(close.shift(1) > level.shift(1))

    signal = is_above_level & is_downtrend & is_volume_surge & is_long_green_candle
    if is_slope_ok is not None:
        signal &= is_slope_ok
    if strategy.get("alert_only_on_cross", True):
        signal &= is_fresh_cross

    return {
        "sma": sma,
        "dist_pct": dist_pct,
        "pct_days_below": pct_days_below,
        "sma_slope_pct": sma_slope_pct,
        "vol_sma": vol_sma,
        "vol_ratio": vol_ratio,
        "body_pct": body_pct,
        "is_above_level": is_above_level,
        "is_downtrend": is_downtrend,
        "is_volume_surge": is_volume_surge,
        "is_long_green_candle": is_long_green_candle,
        "is_fresh_cross": is_fresh_cross,
        "signal": signal,
    }


def near_miss_mask(data: pd.DataFrame, signals: dict, strategy: dict) -> pd.DataFrame:
    """(days, tickers) mask of reclaim near-misses.

    A genuine fresh cross above the level *out of a real downtrend* (R1+R2,
    both mandatory exactly as for a hit) where **one or two** of the remaining
    confirmations -- R3 volume, R5 candle, and R4 slope when enabled -- fail.
    Zero failing = a hit; three or more = too far off, dropped.

    Vectorized over every day like the compute_* functions: the nightly scan
    takes `.iloc[-1]`, the universe backtest uses the whole frame.
    """
    fresh = signals["is_fresh_cross"].fillna(False)
    downtrend = signals["is_downtrend"].fillna(False)
    fails = ((~signals["is_volume_surge"].fillna(False)).astype(int)
             + (~signals["is_long_green_candle"].fillna(False)).astype(int))
    min_slope = strategy.get("min_sma_slope_pct")
    if min_slope is not None:
        is_slope_ok = signals["sma_slope_pct"] >= min_slope
        fails += (~is_slope_ok.fillna(False)).astype(int)
    return (fresh & downtrend & (fails >= 1) & (fails <= 2)
            & ~signals["signal"].fillna(False))


def cross_miss_reasons(row: pd.Series, strategy: dict) -> list[str]:
    """Every confirmation test a fresh-cross day failed, in condition order.

    `row` is a build_calc_table row (percent columns already x100). Returns
    one string per failing active confirmation (R2 downtrend, R3 volume, R4
    slope when enabled, R5 candle); empty when all pass.
    """
    lookback = strategy["below_lookback_days"]
    min_below = strategy["min_days_below_pct"]
    vol_days = strategy["volume_sma_days"]
    vol_mult = strategy["volume_surge_multiplier"]
    min_slope = strategy.get("min_sma_slope_pct")
    min_body = strategy.get("min_candle_body_pct", 0.0)

    reasons = []
    if not row["R2_TimeBelow"]:
        reasons.append(f"not a long downtrend: below SMA only {row['BelowPct']:.0f}% "
                       f"of the last {lookback} days < {min_below:.0%} required")
    if not row["R3_VolumeSurge"]:
        reasons.append(f"no volume confirmation: {row['VolRatio']:.2f}x < "
                       f"{vol_mult}x {vol_days}d avg required")
    if min_slope is not None and row["SmaSlopePct"] < min_slope * 100:
        reasons.append(f"SMA still falling: {row['SmaSlopePct']:+.2f}% < "
                       f"{min_slope:.1%} required")
    if not row["R5_LongGreen"]:
        reasons.append(f"reclaim candle too weak: body {row['BodyPct']:+.1f}% < "
                       f"{min_body:.1%} required")
    return reasons


def cross_miss_reason(row: pd.Series, strategy: dict) -> str:
    """Join every failing confirmation into one string (see cross_miss_reasons)."""
    return "; ".join(cross_miss_reasons(row, strategy))


def find_reclaims(data: pd.DataFrame, strategy: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Screen the whole universe on the most recent trading day.

    Returns two ticker-indexed DataFrames:
      * hits -- tickers that reclaimed their SMA today after a long stretch
        below it (all conditions true);
      * near-misses -- tickers that freshly crossed the level today out of a
        real downtrend but had one or two of the remaining confirmations
        (volume, candle, slope) fail, with the failure(s) explained.
    """
    needed = required_history(strategy)
    if len(data) <= needed:
        print(f"WARNING: only {len(data)} rows of history but the reclaim "
              f"screen needs > {needed} ({strategy['sma_days']}d SMA + "
              f"{strategy['below_lookback_days']}d below-lookback) -- NO "
              f"signal can ever fire. Increase data.download_period in config.json.")

    signals = compute_reclaim_signals(data, strategy)
    last = {name: df.iloc[-1] for name, df in signals.items()}

    def day_stats(tickers: list[str]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Close": data["Close"].iloc[-1][tickers].round(2),
                "SMA": last["sma"][tickers].round(2),
                "Dist %": (last["dist_pct"][tickers] * 100).round(2),
                "Below %": (last["pct_days_below"][tickers] * 100).round(1),
                "Vol Ratio": last["vol_ratio"][tickers].round(2),
                "SMA Slope %": (last["sma_slope_pct"][tickers] * 100).round(2),
                "Body %": (last["body_pct"][tickers] * 100).round(2),
            },
            index=pd.Index(tickers, name="Ticker"),
        )

    signal_today = last["signal"].fillna(False)
    hits = day_stats(signal_today[signal_today].index.tolist())

    # Near-misses: a genuine fresh cross OUT OF A DOWNTREND today with 1 or 2
    # confirmations failing (the mask is computed for every day; the scan only
    # needs the last one).
    near_mask = near_miss_mask(data, signals, strategy).iloc[-1]
    near = day_stats(near_mask[near_mask].index.tolist())
    if not near.empty:
        near["Reason"] = [
            cross_miss_reason(
                pd.Series({
                    "R2_TimeBelow": last["is_downtrend"].get(ticker, False),
                    "R3_VolumeSurge": last["is_volume_surge"].get(ticker, False),
                    "R5_LongGreen": last["is_long_green_candle"].get(ticker, False),
                    "BelowPct": near.at[ticker, "Below %"],
                    "VolRatio": near.at[ticker, "Vol Ratio"],
                    "SmaSlopePct": near.at[ticker, "SMA Slope %"],
                    "BodyPct": near.at[ticker, "Body %"],
                }),
                strategy,
            )
            for ticker in near.index
        ]

    scan_date = data.index[-1].date()
    print(f"Scan date: {scan_date} -- {len(hits)} SMA-reclaim setup(s), "
          f"{len(near)} near-miss(es).")
    return hits, near


# --------------------------------------------------------------------------
# Registry contract: scan / describe_hit / plot_hit
# --------------------------------------------------------------------------

def scan(data: pd.DataFrame, strategy: dict) -> ScanResult:
    hits, near = find_reclaims(data, strategy)
    sma_days = strategy["sma_days"]
    return ScanResult(
        title=f"Reclaim of {sma_days}-day SMA after downtrend",
        hits=hits,
        near=near,
        strategy=strategy,
    )


def describe_hit(row, strategy: dict) -> str:
    """Embed-card description of one confirmed reclaim setup."""
    return (f"Close {fmt_value(row['Close'])} crossed above the "
            f"{strategy['sma_days']}d SMA {fmt_value(row['SMA'])} "
            f"({row['Dist %']:+.1f}%), below SMA {row['Below %']:.0f}% of last "
            f"{strategy['below_lookback_days']}d, "
            f"vol {row['Vol Ratio']:.1f}x {strategy['volume_sma_days']}d avg, "
            f"green candle {row['Body %']:+.1f}%")


def build_calc_table(data: pd.DataFrame, signals: dict, ticker: str) -> pd.DataFrame:
    """Flatten one ticker's OHLCV + every intermediate into a per-day table
    (consumed by charts.plot_reclaim and the backtest)."""
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
            "BelowPct": signals["pct_days_below"][ticker] * 100,
            "VolSMA": signals["vol_sma"][ticker],
            "VolRatio": signals["vol_ratio"][ticker],
            "SmaSlopePct": signals["sma_slope_pct"][ticker] * 100,
            "BodyPct": signals["body_pct"][ticker] * 100,
            "R1_AboveLevel": signals["is_above_level"][ticker],
            "R2_TimeBelow": signals["is_downtrend"][ticker],
            "R3_VolumeSurge": signals["is_volume_surge"][ticker],
            "R5_LongGreen": signals["is_long_green_candle"][ticker],
            "FreshCross": signals["is_fresh_cross"][ticker],
            "SIGNAL": signals["signal"][ticker],
        }
    ).rename_axis("Date")


def plot_hit(data: pd.DataFrame, ticker: str, strategy: dict,
             chart_cfg: dict, out_path) -> None:
    """Render the alert chart for one hit: the recent window of its close
    and SMA with the reclaim marked, plus the volume confirmation panel."""
    panel = single_ticker_panel(data, ticker)
    table = build_calc_table(panel, compute_reclaim_signals(panel, strategy), ticker)
    table = table.tail(chart_cfg.get("lookback_days", 250))
    charts.plot_reclaim(table, strategy, ticker, out_path,
                        dpi=chart_cfg.get("dpi", 150))


if __name__ == "__main__":
    print("This is a screen module -- run the nightly scan with: python run_scanners.py")
