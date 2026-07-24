"""
Single-ticker historical backtest of the SMA-reclaim screen.

Validates the exact production logic (sma_reclaim.compute_reclaim_signals)
over a historical window, with step-by-step logging of every calculation and
a chart of the setup.

Outputs:
  * console log of every step (data listing, thresholds, fresh-cross days,
    signal days, cross days that failed and why)
  * backtest_reclaim_<ticker>.csv  -- full per-day calculation table
  * backtest_reclaim_<ticker>.png  -- price/SMA chart with the volume panel

Usage:
    python backtest_reclaim.py --ticker META --start 2023-01-01 --end 2023-12-31
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

import charts
from scanner_common import download_history, load_config
from sma_reclaim import build_calc_table, compute_reclaim_signals, cross_miss_reason


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def log_run(table: pd.DataFrame, strategy: dict, ticker: str) -> None:
    sma_days = strategy["sma_days"]
    lookback = strategy["below_lookback_days"]
    min_below = strategy["min_days_below_pct"]
    margin = strategy["cross_margin_pct"]
    vol_days = strategy["volume_sma_days"]
    vol_mult = strategy["volume_surge_multiplier"]
    slope_days = strategy["sma_slope_lookback_days"]
    min_slope = strategy.get("min_sma_slope_pct")
    min_body = strategy.get("min_candle_body_pct", 0.0)
    cross_only = strategy.get("alert_only_on_cross", True)

    section(f"STEP 1 -- Historical data for {ticker} (analysis window)")
    print(f"{len(table)} trading days, {table.index[0].date()} .. {table.index[-1].date()}")
    ohlcv = table[["Open", "High", "Low", "Close", "Volume"]].round(2)
    print("\nFirst 5 days:")
    print(ohlcv.head().to_string())
    print("\nLast 5 days:")
    print(ohlcv.tail().to_string())

    section("STEP 2 -- Screen thresholds (from config.json)")
    print(f"R1 cross level:  Close above {1 + margin:.2f} x the {sma_days}d SMA")
    print(f"R2 downtrend:    Close below the SMA on >= {min_below:.0%} of the prior {lookback} trading days")
    print(f"R3 volume:       Volume >= {vol_mult}x the prior {vol_days}d average")
    if min_slope is not None:
        print(f"R4 slope:        the SMA's change over {slope_days} days >= {min_slope:.1%}")
    print(f"R5 candle:       Close must be > {1 + min_body} x the day's Open"
          f" (green, body >= {min_body:.1%})")
    if cross_only:
        print("Entry filter:    signal only on the day the close first crosses the level")

    section(f"STEP 3 -- Days where Close freshly crossed the level (R1 + fresh cross)")
    fmt_cols = ["Close", "SMA", "DistPct", "BelowPct", "VolRatio", "SmaSlopePct", "BodyPct",
                "R1_AboveLevel", "R2_TimeBelow", "R3_VolumeSurge", "R5_LongGreen",
                "FreshCross", "SIGNAL"]
    crosses = table[table["FreshCross"].fillna(False)]
    if crosses.empty:
        print(f"None -- the close never crossed {1 + margin:.2f} x its {sma_days}d SMA "
              f"from below in this window.")
    else:
        print(crosses[fmt_cols].round(2).to_string())

    section("STEP 4 -- SIGNAL days (all conditions true)")
    hits = table[table["SIGNAL"].fillna(False)]
    if hits.empty:
        print("No day satisfied all conditions.")
    else:
        for date, row in hits.iterrows():
            print(f"{date.date()}  RECLAIM CONFIRMED")
            print(f"    Close {row['Close']:.2f} crossed above the {sma_days}d SMA "
                  f"{row['SMA']:.2f}  ({row['DistPct']:+.2f}%, level SMA +{margin:.0%})")
            print(f"    Below the SMA {row['BelowPct']:.0f}% of the prior {lookback}d "
                  f"(needs >= {min_below:.0%})")
            print(f"    Volume {row['VolRatio']:.2f}x the prior {vol_days}d average "
                  f"(needs >= {vol_mult}x)")
            print(f"    Candle Open {row['Open']:.2f} -> Close {row['Close']:.2f} = "
                  f"body {row['BodyPct']:+.2f}% (needs >= {min_body:.1%}, green)")

    section("STEP 5 -- Fresh-cross days that did NOT fire (and why)")
    print("(the production near-miss list is the subset failing only 1 or 2 tests)")
    misses = table[table["FreshCross"].fillna(False) & ~table["SIGNAL"].fillna(False)]
    if misses.empty:
        print("None.")
    else:
        for date, row in misses.iterrows():
            print(f"{date.date()}  failed -> {cross_miss_reason(row, strategy)}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest the SMA-reclaim screen on one ticker.")
    parser.add_argument("--ticker", default="META")
    parser.add_argument("--start", default="2023-01-01", help="analysis window start")
    parser.add_argument("--end", default="2023-12-31", help="analysis window end")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    strategy = load_config()["reclaim_strategy"]

    warmup = strategy["sma_days"] + strategy["below_lookback_days"]
    data = download_history(ticker, start, end, warmup)
    signals = compute_reclaim_signals(data, strategy)
    table = build_calc_table(data, signals, ticker).loc[start:end]

    log_run(table, strategy, ticker)

    out_dir = Path(__file__).parent
    csv_path = out_dir / f"backtest_reclaim_{ticker}.csv"
    table.round(4).to_csv(csv_path)
    print(f"\nFull per-day calculation table saved to {csv_path}")

    charts.plot_reclaim(table, strategy, ticker,
                        out_dir / f"backtest_reclaim_{ticker}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
