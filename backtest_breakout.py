"""
Single-ticker historical backtest of the breakout-from-consolidation screen.

Validates the exact production logic (breakout_scanner.compute_signals) on a
known real-world case -- by default JNJ's upward breakout at the end of July
2025 -- with step-by-step logging of every calculation and a chart of the
setup.

Outputs:
  * console log of every step (data listing, thresholds, per-day conditions,
    signal days, partial setups)
  * backtest_<ticker>.csv  -- full per-day calculation table
  * backtest_<ticker>.png  -- price/volume chart of the consolidation + breakout

Usage:
    python backtest_breakout.py                     # JNJ, Jan-Oct 2025
    python backtest_breakout.py --ticker MSFT --start 2024-01-01 --end 2024-12-31
"""

import argparse
import sys

import pandas as pd

import charts
from breakout_scanner import build_calc_table, compute_signals, missing_reason
from scanner_common import download_history, load_config, output_dir


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def log_run(table: pd.DataFrame, strategy: dict, ticker: str) -> None:
    window = strategy["consolidation_window_days"]
    max_range = strategy["max_consolidation_range_pct"]
    brk_mult = strategy.get("breakout_multiplier", 1.0)
    vol_days = strategy["volume_sma_days"]
    vol_mult = strategy["volume_surge_multiplier"]
    min_body = strategy.get("min_candle_body_pct", 0.0)

    section(f"STEP 1 -- Historical data for {ticker} (analysis window)")
    print(f"{len(table)} trading days, {table.index[0].date()} .. {table.index[-1].date()}")
    ohlcv = table[["Open", "High", "Low", "Close", "Volume"]].round(2)
    print("\nFirst 5 days:")
    print(ohlcv.head().to_string())
    print("\nLast 5 days:")
    print(ohlcv.tail().to_string())

    section("STEP 2 -- Screen thresholds (from config.json)")
    print(f"C1 consolidation: (max High - min Low) of the prior {window} trading days"
          f" must be <= {max_range:.0%} of the min Low")
    print(f"C2 breakout:      Close must be > {brk_mult} x the prior {window}-day max High"
          f" (i.e. {brk_mult - 1:+.1%} above it)")
    print(f"C3 volume surge:  Volume must be >= {vol_mult} x the prior {vol_days}-day average volume")
    print(f"C4 long green:    Close must be > {1 + min_body} x the day's Open"
          f" (body >= {min_body:.1%})")

    section(f"STEP 3 -- Days where Close cleared {brk_mult} x the prior "
            f"{window}-day high (C2 true)")
    fmt_cols = ["Close", "PriorHigh", "PriorLow", "RangePct", "VolRatio", "BodyPct",
                "C1_Consolidating", "C2_Breakout", "C3_VolumeSurge", "C4_LongGreen",
                "SIGNAL"]
    c2_days = table[table["C2_Breakout"].fillna(False)]
    if c2_days.empty:
        print(f"None -- price never closed above {brk_mult} x its prior "
              f"{window}-day high in this window.")
    else:
        print(c2_days[fmt_cols].round(2).to_string())

    section("STEP 4 -- SIGNAL days (all four conditions true)")
    hits = table[table["SIGNAL"].fillna(False)]
    if hits.empty:
        print("No day satisfied all four conditions.")
    else:
        for date, row in hits.iterrows():
            print(f"{date.date()}  BREAKOUT CONFIRMED")
            print(f"    Close {row['Close']:.2f} > {brk_mult} x prior {window}d high "
                  f"{row['PriorHigh']:.2f}  (+{(row['Close'] / row['PriorHigh'] - 1) * 100:.2f}%)")
            print(f"    Prior range {row['PriorLow']:.2f} .. {row['PriorHigh']:.2f}"
                  f"  = {row['RangePct']:.1f}% (limit {max_range:.0%})")
            print(f"    Volume {row['Volume']:,.0f} = {row['VolRatio']:.2f}x the {vol_days}d"
                  f" average {row['VolSMA']:,.0f} (needs >= {vol_mult}x)")
            print(f"    Candle Open {row['Open']:.2f} -> Close {row['Close']:.2f} = "
                  f"body {row['BodyPct']:+.2f}% (needs >= {min_body:.1%}, green)")

    section("STEP 5 -- Partial setups (exactly 3 of 4 conditions true)")
    conds = table[["C1_Consolidating", "C2_Breakout", "C3_VolumeSurge",
                   "C4_LongGreen"]].fillna(False)
    near = table[(conds.sum(axis=1) == 3) & ~table["SIGNAL"].fillna(False)]
    if near.empty:
        print("None.")
    else:
        for date, row in near.iterrows():
            reason = missing_reason(row["Close"], row["PriorHigh"],
                                    row["RangePct"] / 100, row["VolRatio"],
                                    row["BodyPct"] / 100, strategy)
            print(f"{date.date()}  failed -> {reason}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest the breakout screen on one ticker.")
    parser.add_argument("--ticker", default="JNJ")
    parser.add_argument("--start", default="2025-01-01", help="analysis window start")
    parser.add_argument("--end", default="2025-10-31", help="analysis window end")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    strategy = load_config()["breakout_strategy"]

    data = download_history(ticker, start, end, strategy["consolidation_window_days"])
    signals = compute_signals(data, strategy)
    table = build_calc_table(data, signals, ticker).loc[start:end]

    log_run(table, strategy, ticker)

    out_dir = output_dir()
    csv_path = out_dir / f"backtest_{ticker}.csv"
    table.round(4).to_csv(csv_path)
    print(f"\nFull per-day calculation table saved to {csv_path}")

    charts.plot_breakout(table, strategy, ticker, out_dir / f"backtest_{ticker}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
