"""
Single-ticker historical backtest of the SMA-pullback screen.

Validates the exact production logic (sma_pullback.compute_pullback_signals)
over a historical window, with step-by-step logging of every calculation and
a chart of the setup.

Outputs:
  * console log of every step (data listing, thresholds, touch days,
    signal days, touch days that failed and why)
  * backtest_pullback_<ticker>.csv  -- full per-day calculation table
  * backtest_pullback_<ticker>.png  -- price/SMA chart with the touch band

Usage:
    python backtest_pullback.py --ticker MSFT --start 2024-01-01 --end 2025-06-30
"""

import sys

import pandas as pd

import charts
import sma_pullback
import single_backtest
from single_backtest import section
from sma_pullback import touch_miss_reason


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def log_run(table: pd.DataFrame, strategy: dict, ticker: str) -> None:
    sma_days = strategy["sma_days"]
    band = strategy["touch_band_pct"]
    lookback = strategy["trend_lookback_days"]
    slope_days = strategy["sma_slope_lookback_days"]
    min_above = strategy["min_days_above_sma_pct"]
    max_body = strategy.get("max_candle_body_pct", 1.0)
    min_range = strategy.get("min_candle_range_pct", 0.0)
    reversal = strategy.get("require_reversal_candle", True)
    entry_only = strategy.get("alert_only_on_band_entry", True)

    section(f"STEP 1 -- Historical data for {ticker} (analysis window)")
    print(f"{len(table)} trading days, {table.index[0].date()} .. {table.index[-1].date()}")
    ohlcv = table[["Open", "High", "Low", "Close", "Volume"]].round(2)
    print("\nFirst 5 days:")
    print(ohlcv.head().to_string())
    print("\nLast 5 days:")
    print(ohlcv.tail().to_string())

    section("STEP 2 -- Screen thresholds (from config.json)")
    print(f"T1 rising SMA:   the {sma_days}d SMA must be higher than {slope_days} trading days ago")
    print(f"T2 trend:        Close above the SMA on >= {min_above:.0%} of the prior {lookback} trading days")
    print(f"T3 touch:        Close within +/-{band:.0%} of the {sma_days}d SMA")
    if reversal:
        print(f"T4 candle:       small body (|Close-Open| <= {max_body:.1%} of Open) "
              f"AND wide range (High-Low >= {min_range:.1%} of Open)")
    if entry_only:
        print(f"Entry filter:    signal only on the day the close enters the band from above"
              f" (previous close > {1 + band:.2f} x SMA)")

    section(f"STEP 3 -- Days where Close touched the {sma_days}d SMA (T3 true)")
    fmt_cols = ["Close", "SMA", "DistPct", "AbovePct", "SmaSlopePct", "BodyPct", "RangePct",
                "T1_RisingSMA", "T2_TimeAbove", "T3_Touch", "T4_ReversalCandle",
                "BandEntry", "SIGNAL"]
    touch_days = table[table["T3_Touch"].fillna(False)]
    if touch_days.empty:
        print(f"None -- the close never came within {band:.0%} of its {sma_days}d SMA "
              f"in this window.")
    else:
        print(touch_days[fmt_cols].round(2).to_string())

    section("STEP 4 -- SIGNAL days (all conditions true)")
    hits = table[table["SIGNAL"].fillna(False)]
    if hits.empty:
        print("No day satisfied all conditions.")
    else:
        for date, row in hits.iterrows():
            print(f"{date.date()}  PULLBACK CONFIRMED")
            print(f"    Close {row['Close']:.2f} touched the {sma_days}d SMA "
                  f"{row['SMA']:.2f}  ({row['DistPct']:+.2f}%, band +/-{band:.0%})")
            print(f"    Above the SMA {row['AbovePct']:.0f}% of the prior {lookback}d "
                  f"(needs >= {min_above:.0%})")
            print(f"    SMA {row['SmaSlopePct']:+.2f}% vs {slope_days} days ago (must be rising)")
            if reversal:
                print(f"    Reversal candle: body {row['BodyPct']:.2f}% (max {max_body:.1%}), "
                      f"range {row['RangePct']:.2f}% (min {min_range:.1%})")

    section("STEP 5 -- Touch days that did NOT fire (and why)")
    misses = table[table["T3_Touch"].fillna(False) & ~table["SIGNAL"].fillna(False)]
    if misses.empty:
        print("None.")
    else:
        for date, row in misses.iterrows():
            print(f"{date.date()}  failed -> {touch_miss_reason(row, strategy)}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    return single_backtest.run(
        sma_pullback, log_run, charts.plot_pullback,
        ticker="MSFT", start="2024-01-01", end="2025-06-30",
        description="Backtest the SMA-pullback screen on one ticker.",
        prefix="backtest_pullback")


if __name__ == "__main__":
    sys.exit(main())
