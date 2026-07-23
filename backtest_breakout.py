"""
Single-ticker historical backtest of the breakout-from-consolidation screen.

Validates the exact production logic (breakout_scanner.compute_signals) on a
known real-world case -- by default JNJ's upward breakout at the end of July
2025 -- with step-by-step logging of every calculation and a chart of the
setup.

Outputs:
  * console log of every step (data listing, thresholds, per-day conditions,
    signal days, near-misses)
  * backtest_<ticker>.csv  -- full per-day calculation table
  * backtest_<ticker>.png  -- price/volume chart of the consolidation + breakout

Usage:
    python backtest_breakout.py                     # JNJ, Jan-Oct 2025
    python backtest_breakout.py --ticker MSFT --start 2024-01-01 --end 2024-12-31
"""

import argparse
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import yfinance as yf

from breakout_scanner import compute_signals, load_config, near_miss_reason


def warmup_months(window: int) -> int:
    """Extra calendar months to download before the analysis window so the
    rolling consolidation window (in trading days, ~21/month) is fully
    warmed up by the first analysis day."""
    return math.ceil(window / 21) + 2

# Validated light-mode palette (dataviz reference instance).
C = {
    "surface": "#fcfcfb",
    "ink": "#0b0b0b",
    "ink2": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "close": "#2a78d6",   # series slot 1 (blue)
    "event": "#eb6834",   # series slot 2 (orange) -- breakout highlights
}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def download_history(ticker: str, start: pd.Timestamp, end: pd.Timestamp,
                     window: int) -> pd.DataFrame:
    """Download one ticker's OHLCV in the same (Field, Ticker) column layout
    the scanner uses, so compute_signals runs unchanged."""
    months = warmup_months(window)
    dl_start = start - pd.DateOffset(months=months)
    print(f"Downloading {ticker} daily data {dl_start.date()} .. {end.date()} "
          f"(includes {months} months of warm-up for the {window}-day rolling window)")
    data = yf.download(
        ticker,
        start=dl_start,
        end=end + pd.Timedelta(days=1),
        interval="1d",
        group_by="column",
        auto_adjust=False,
        progress=False,
    )
    if data.empty:
        raise SystemExit(f"No data returned for {ticker} -- check the ticker/dates.")
    if not isinstance(data.columns, pd.MultiIndex):
        data.columns = pd.MultiIndex.from_product([data.columns, [ticker]])
    return data


def build_calc_table(data: pd.DataFrame, signals: dict, ticker: str) -> pd.DataFrame:
    """Flatten OHLCV + every intermediate into one per-day table."""
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


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def log_run(table: pd.DataFrame, strategy: dict, ticker: str) -> None:
    window = strategy["consolidation_window_days"]
    max_range = strategy["max_consolidation_range_pct"]
    brk_mult = strategy.get("breakout_multiplier", 1.0)
    vol_days = strategy["volume_sma_days"]
    vol_mult = strategy["volume_surge_multiplier"]

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

    section(f"STEP 3 -- Days where Close cleared {brk_mult} x the prior "
            f"{window}-day high (C2 true)")
    fmt_cols = ["Close", "PriorHigh", "PriorLow", "RangePct", "VolRatio",
                "C1_Consolidating", "C2_Breakout", "C3_VolumeSurge", "SIGNAL"]
    c2_days = table[table["C2_Breakout"].fillna(False)]
    if c2_days.empty:
        print(f"None -- price never closed above {brk_mult} x its prior "
              f"{window}-day high in this window.")
    else:
        print(c2_days[fmt_cols].round(2).to_string())

    section("STEP 4 -- SIGNAL days (all three conditions true)")
    hits = table[table["SIGNAL"].fillna(False)]
    if hits.empty:
        print("No day satisfied all three conditions.")
    else:
        for date, row in hits.iterrows():
            print(f"{date.date()}  BREAKOUT CONFIRMED")
            print(f"    Close {row['Close']:.2f} > {brk_mult} x prior {window}d high "
                  f"{row['PriorHigh']:.2f}  (+{(row['Close'] / row['PriorHigh'] - 1) * 100:.2f}%)")
            print(f"    Prior range {row['PriorLow']:.2f} .. {row['PriorHigh']:.2f}"
                  f"  = {row['RangePct']:.1f}% (limit {max_range:.0%})")
            print(f"    Volume {row['Volume']:,.0f} = {row['VolRatio']:.2f}x the {vol_days}d"
                  f" average {row['VolSMA']:,.0f} (needs >= {vol_mult}x)")

    section("STEP 5 -- Near-misses (exactly 2 of 3 conditions true)")
    conds = table[["C1_Consolidating", "C2_Breakout", "C3_VolumeSurge"]].fillna(False)
    near = table[(conds.sum(axis=1) == 2) & ~table["SIGNAL"].fillna(False)]
    if near.empty:
        print("None.")
    else:
        for date, row in near.iterrows():
            reason = near_miss_reason(row["Close"], row["PriorHigh"],
                                      row["RangePct"] / 100, row["VolRatio"], strategy)
            print(f"{date.date()}  failed -> {reason}")


# --------------------------------------------------------------------------
# Chart
# --------------------------------------------------------------------------

def plot_backtest(table: pd.DataFrame, strategy: dict, ticker: str, out_path: Path) -> None:
    vol_mult = strategy["volume_surge_multiplier"]
    vol_days = strategy["volume_sma_days"]
    window = strategy["consolidation_window_days"]
    hits = table[table["SIGNAL"].fillna(False)]

    fig, (ax_p, ax_v) = plt.subplots(
        2, 1, figsize=(12, 7.5), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08},
    )
    fig.patch.set_facecolor(C["surface"])

    for ax in (ax_p, ax_v):
        ax.set_facecolor(C["surface"])
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(C["axis"])
        ax.tick_params(colors=C["muted"], labelsize=9)
        ax.grid(axis="y", color=C["grid"], linewidth=0.8)
        ax.set_axisbelow(True)

    # -- price panel: consolidation band, prior high, close, signal markers --
    ax_p.fill_between(table.index, table["PriorLow"], table["PriorHigh"],
                      color=C["grid"], alpha=0.55,
                      label=f"prior {window}d range", linewidth=0)
    ax_p.plot(table.index, table["PriorHigh"], color=C["ink2"], linewidth=1.2,
              linestyle="--", label=f"prior {window}d high")
    ax_p.plot(table.index, table["Close"], color=C["close"], linewidth=2, label="close")
    if not hits.empty:
        ax_p.scatter(hits.index, hits["Close"], color=C["event"], s=70, zorder=5,
                     edgecolor=C["surface"], linewidth=1.5, label="breakout signal")
        first = hits.iloc[0]
        ax_p.annotate(
            f"{hits.index[0].date()}\nclose {first['Close']:.2f}",
            xy=(hits.index[0], first["Close"]),
            xytext=(12, 18), textcoords="offset points",
            fontsize=9, color=C["ink"],
            arrowprops={"arrowstyle": "-", "color": C["muted"], "linewidth": 0.8},
        )
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
        f"volume {vol_mult}x {strategy['volume_sma_days']}d average"
    ax_p.set_title(f"{ticker} -- breakout from {window}-day consolidation\n",
                   loc="left", fontsize=13, color=C["ink"], fontweight="bold")
    ax_p.text(0, 1.02, subtitle, transform=ax_p.transAxes, fontsize=9.5, color=C["ink2"])

    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=C["surface"])
    plt.close(fig)
    print(f"\nChart saved to {out_path}")


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
    strategy = load_config()["strategy"]

    data = download_history(ticker, start, end, strategy["consolidation_window_days"])
    signals = compute_signals(data, strategy)
    table = build_calc_table(data, signals, ticker).loc[start:end]

    log_run(table, strategy, ticker)

    out_dir = Path(__file__).parent
    csv_path = out_dir / f"backtest_{ticker}.csv"
    table.round(4).to_csv(csv_path)
    print(f"\nFull per-day calculation table saved to {csv_path}")

    plot_backtest(table, strategy, ticker, out_dir / f"backtest_{ticker}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
