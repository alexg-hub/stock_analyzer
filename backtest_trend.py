"""
Single-ticker historical backtest of the near-linear trend screen.

Validates the exact production logic (trend_line.compute_trend_signals) over a
historical window, with step-by-step logging of every calculation and a chart
of the setup.

Outputs:
  * console log of every step (data listing, thresholds, newly qualifying
    windows, signal days, qualifying days that failed a confirmation and why)
  * backtest_trend_<ticker>.csv  -- full per-day calculation table
  * backtest_trend_<ticker>.png  -- price chart with the fitted line, its
    residual band and the r-squared panel

Usage:
    python backtest_trend.py --ticker COST --start 2023-06-01 --end 2024-06-30
"""

import argparse
import sys

import pandas as pd

import charts
from scanner_common import download_history, load_config, output_dir
from trend_line import (
    build_calc_table,
    compute_trend_signals,
    missing_reason,
    required_history,
)


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def log_run(table: pd.DataFrame, strategy: dict, ticker: str) -> None:
    window = strategy["trend_window_days"]
    use_log = strategy.get("fit_on_log_price", True)
    min_ann = strategy["min_annual_slope_pct"]
    max_ann = strategy.get("max_annual_slope_pct")
    min_r2 = strategy["min_r_squared"]
    max_resid = strategy["max_residual_pct"]
    max_dev = strategy.get("max_last_dev_pct")
    max_fails = strategy.get("max_partial_fails", 1)
    fresh_only = strategy.get("alert_only_on_new_trend", True)

    section(f"STEP 1 -- Historical data for {ticker} (analysis window)")
    print(f"{len(table)} trading days, {table.index[0].date()} .. {table.index[-1].date()}")
    ohlcv = table[["Open", "High", "Low", "Close", "Volume"]].round(2)
    print("\nFirst 5 days:")
    print(ohlcv.head().to_string())
    print("\nLast 5 days:")
    print(ohlcv.tail().to_string())

    section("STEP 2 -- Screen thresholds (from config.json)")
    print(f"Fit:             least squares on {'log ' if use_log else ''}price "
          f"over the trailing {window} trading days (~{window / 21:.0f} months)")
    print(f"T1 angle:        annualized slope >= {min_ann:.1%}"
          + (f" and <= {max_ann:.1%}" if max_ann is not None else " (no ceiling)"))
    print(f"T2 linearity:    r-squared of that fit >= {min_r2:.2f}")
    print(f"T3 volatility:   residual std <= {max_resid:.1%} of price")
    if max_dev is not None:
        print(f"T4 not extended: today's close within {max_dev:.1%} of the fitted line")
    if fresh_only:
        print("Entry filter:    signal only on the day the trend first qualifies")
    print(f"Control cohort:  T1 holding with up to {max_fails} confirmation(s) "
          f"failing -- measured, never alerted")

    fmt_cols = ["Close", "Fit", "DistPct", "AnnualPct", "AngleDeg", "TrendPct",
                "R2", "ResidPct", "Fails", "T1_Angle", "T2_Linear", "T3_Tight",
                "T4_NearLine", "NewTrend", "NewLoose", "SIGNAL"]

    section("STEP 3 -- Days where the trailing window newly qualified (T1 + fresh)")
    fresh = table[table["NewLoose"].fillna(False) | table["NewTrend"].fillna(False)]
    if fresh.empty:
        print(f"None -- no {window}d window in this range newly met T1 with at "
              f"most {max_fails} confirmation(s) failing.")
    else:
        print(fresh[fmt_cols].round(2).to_string())

    section("STEP 4 -- SIGNAL days (all conditions true)")
    hits = table[table["SIGNAL"].fillna(False)]
    if hits.empty:
        print("No day satisfied all conditions.")
    else:
        for date, row in hits.iterrows():
            print(f"{date.date()}  TREND CONFIRMED")
            print(f"    Close {row['Close']:.2f} vs the fitted line {row['Fit']:.2f} "
                  f"({row['DistPct']:+.2f}% from it)")
            print(f"    Slope {row['AnnualPct']:+.1f}%/yr = {row['AngleDeg']:.1f} deg "
                  f"(needs {min_ann:.0%}"
                  + (f"..{max_ann:.0%})" if max_ann is not None else "+)"))
            print(f"    The line rose {row['TrendPct']:+.1f}% across the "
                  f"{window}d window")
            print(f"    r-squared {row['R2']:.3f} (needs >= {min_r2:.2f})")
            print(f"    Residual {row['ResidPct']:.2f}% of price "
                  f"(needs <= {max_resid:.1%})")

    section("STEP 5 -- Newly qualifying days that did NOT fire (and why)")
    print("(the backtest's control cohort: the same trend with the linearity "
          "requirement relaxed -- never alerted, only measured)")
    misses = table[table["NewLoose"].fillna(False) & ~table["SIGNAL"].fillna(False)]
    if misses.empty:
        print("None.")
    else:
        for date, row in misses.iterrows():
            print(f"{date.date()}  failed -> {missing_reason(row, strategy)}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backtest the near-linear trend screen on one ticker.")
    parser.add_argument("--ticker", default="COST")
    parser.add_argument("--start", default="2023-06-01", help="analysis window start")
    parser.add_argument("--end", default="2024-06-30", help="analysis window end")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    strategy = load_config()["trend_strategy"]

    data = download_history(ticker, start, end, required_history(strategy))
    signals = compute_trend_signals(data, strategy)
    table = build_calc_table(data, signals, ticker).loc[start:end]

    log_run(table, strategy, ticker)

    out_dir = output_dir()
    csv_path = out_dir / f"backtest_trend_{ticker}.csv"
    table.round(4).to_csv(csv_path)
    print(f"\nFull per-day calculation table saved to {csv_path}")

    charts.plot_trend(table, strategy, ticker,
                      out_dir / f"backtest_trend_{ticker}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
