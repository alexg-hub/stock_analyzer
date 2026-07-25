"""
Universe-wide historical backtest: did the screens actually make money?

Downloads the whole S&P 500 once (analysis window + rolling-window warm-up),
runs every enabled screen over *all* history, and simulates one fixed-horizon
trade per signal: buy at the trigger, sell `holding_days` trading days later.

This adds no condition math. Every screen's `compute_*` already returns
(days, tickers) DataFrames including a boolean `signal` frame -- the nightly
scan just throws away every row but the last. Here the whole frame is kept and
masked against a vectorized forward-return matrix, so the simulation is a
handful of `shift()` calls with no loops.

Outputs (all paths from config `backtest.output`):
  * console  -- per-screen stats table per horizon + a per-year breakdown
  * backtest_universe_trades.csv   -- every simulated trade
  * backtest_universe_summary.csv  -- the stats table
  * backtest_universe.png          -- mean return + win rate per screen

Read the caveats printed in the run header before believing any number: the
universe is *today's* index (survivorship bias), there are no costs, and
dividends are ignored.

Usage:
    python backtest_universe.py                            # config defaults
    python backtest_universe.py --years 5 --refresh
    python backtest_universe.py --holding-days 10,30,60
    python backtest_universe.py --screens breakout_strategy --entry signal_close
"""

import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import breakout_scanner
import charts
import sma_pullback
import sma_reclaim
from scanner_common import (
    download_price_data,
    get_sp500_tickers,
    load_config,
    warmup_months,
)

# Every screen the backtest can run, paired with its compute function. The
# modules also supply required_history() and near_miss_mask() -- the same
# functions the nightly scan uses, so there is one source of truth per screen.
SCREENS = [
    (breakout_scanner, breakout_scanner.compute_signals),
    (sma_pullback, sma_pullback.compute_pullback_signals),
    (sma_reclaim, sma_reclaim.compute_reclaim_signals),
]

BASELINE_LABEL = "ALL stock-days (random entry)"


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# --------------------------------------------------------------------------
# Data: one download, cached to disk
# --------------------------------------------------------------------------

def _cache_path(bt_cfg: dict) -> Path:
    path = Path(bt_cfg.get("cache_path", "backtest_universe_cache.pkl"))
    return path if path.is_absolute() else Path(__file__).with_name(str(path))


def load_panel(bt_cfg: dict, cfg: dict, years: int, warmup_days: int,
               refresh: bool = False) -> pd.DataFrame:
    """The (Field, Ticker) OHLCV panel for the universe + benchmark.

    Cached on disk: re-downloading ~500 tickers for every parameter tweak is
    the slowest part of the run by far. The cache is reused when it is younger
    than `cache_max_age_days` and already covers the requested history.
    """
    # Enough calendar years to cover the analysis window plus the warm-up the
    # longest rolling window needs (~21 trading days per month).
    period_years = years + math.ceil(warmup_months(warmup_days) / 12)
    period = f"{period_years}y"
    path = _cache_path(bt_cfg)
    max_age_days = bt_cfg.get("cache_max_age_days", 1)

    if path.exists() and not refresh:
        age_days = (time.time() - path.stat().st_mtime) / 86400
        cached = pd.read_pickle(path)
        span_years = (cached.index[-1] - cached.index[0]).days / 365.25
        if age_days <= max_age_days and span_years >= period_years - 0.2:
            print(f"Using cached panel {path.name} "
                  f"({cached.shape[1] // 6} tickers, {len(cached)} days, "
                  f"{cached.index[0].date()} .. {cached.index[-1].date()}, "
                  f"age {age_days:.1f}d). Use --refresh to re-download.")
            return cached
        print(f"Cache {path.name} unusable (age {age_days:.1f}d > "
              f"{max_age_days}d or span {span_years:.1f}y < {period_years}y) "
              f"-- re-downloading.")

    tickers = get_sp500_tickers(cfg["data"]["sp500_source_url"])
    benchmark = bt_cfg.get("benchmark_ticker")
    if benchmark and benchmark not in tickers:
        tickers = tickers + [benchmark]  # not an index constituent
    print(f"Downloading {period} (= {years}y analysis + warm-up for the "
          f"{warmup_days}-day longest lookback)")
    data = download_price_data(tickers, period=period,
                              interval=cfg["data"]["download_interval"])
    data.to_pickle(path)
    print(f"Cached panel to {path}")
    return data


# --------------------------------------------------------------------------
# Trade simulation -- vectorized over (days, tickers)
# --------------------------------------------------------------------------

def forward_trades(data: pd.DataFrame, holding: int, entry: str,
                   excursions: bool) -> dict[str, pd.DataFrame]:
    """Entry/exit prices and returns for a trade opened on *every* day.

    `holding` (h) is the number of TRADING days the position is held after the
    entry day, so for a signal on index position i:
      * `next_open`    -- buy Open[i+1] (earliest tradeable price: the signal
                          is only known after the close), sell Close[i+1+h]
      * `signal_close` -- buy Close[i] (the price shown in the alert),
                          sell Close[i+h]

    Prices are raw Open/Close (split-adjusted by yfinance; dividends ignored).
    A window running past the end of the data yields NaN -- those signals are
    reported as unevaluable, never as a 0% trade.
    """
    if holding < 1:
        raise SystemExit(f"holding period must be >= 1 trading day, got {holding}")
    close, open_ = data["Close"], data["Open"]
    high, low = data["High"], data["Low"]

    if entry == "next_open":
        entry_price = open_.shift(-1)
        exit_price = close.shift(-(1 + holding))
        # Position is open across positions i+1 .. i+1+h (entry day's open
        # through the exit day's close), so h+1 bars.
        offset, span = 1 + holding, holding + 1
    elif entry == "signal_close":
        entry_price = close
        exit_price = close.shift(-holding)
        # Bought at day i's close, so day i's own high/low happened *before*
        # entry: the excursion window is i+1 .. i+h, h bars.
        offset, span = holding, holding
    else:
        raise SystemExit(f"unknown backtest.entry {entry!r} "
                         f"(expected 'next_open' or 'signal_close')")

    out = {
        "entry_price": entry_price,
        "exit_price": exit_price,
        "return_pct": 100 * (exit_price / entry_price - 1),
        # Dates carried along so each trade row can show what it actually did.
        "entry_date": _shifted_dates(data, 1 if entry == "next_open" else 0),
        "exit_date": _shifted_dates(data, offset),
    }
    if excursions:
        # Roll first, then shift back: the trailing `span`-bar extreme ending
        # at the exit day, moved onto the signal day. (Shifting first and then
        # rolling would need `span`-1 rows *before* the signal day and so would
        # silently NaN out the start of the frame.)
        out["mfe_pct"] = 100 * (high.rolling(span).max().shift(-offset)
                                / entry_price - 1)
        out["mae_pct"] = 100 * (low.rolling(span).min().shift(-offset)
                                / entry_price - 1)
    return out


def _shifted_dates(data: pd.DataFrame, offset: int) -> pd.DataFrame:
    """The trading date `offset` rows ahead of each row, as a (days, tickers)
    frame so it can be reindexed alongside the price frames."""
    dates = pd.Series(data.index, index=data.index).shift(-offset)
    return pd.DataFrame({col: dates for col in data["Close"].columns})


def collect_trades(masks: dict, trades: dict, start: pd.Timestamp) -> pd.DataFrame:
    """Turn {(screen, cohort): mask} + the forward-trade frames into one long
    table -- one row per simulated trade. Signals before `start` exist only to
    warm the rolling windows and are dropped."""
    rows = []
    for (screen, cohort), mask in masks.items():
        mask = mask.loc[mask.index >= start]
        if not mask.any().any():
            continue
        idx = mask.stack()
        idx = idx[idx].index  # the (date, ticker) pairs that fired
        frame = pd.DataFrame(
            {name: df.stack().reindex(idx) for name, df in trades.items()},
            index=idx,
        )
        frame.index.names = ["signal_date", "ticker"]
        rows.append(frame.reset_index().assign(screen=screen, cohort=cohort))
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

def describe(returns: pd.Series, n_signals: int, n_dates: int,
             mfe: pd.Series = None, mae: pd.Series = None) -> dict:
    """The stats block for one cohort at one horizon."""
    r = returns.dropna()
    row = {
        "signals": n_signals,
        "evaluable": len(r),
        "distinct_dates": n_dates,
        "mean_%": r.mean(),
        "median_%": r.median(),
        "win_rate_%": 100 * (r > 0).mean() if len(r) else np.nan,
        "std": r.std(),
        "p10": r.quantile(0.10) if len(r) else np.nan,
        "p90": r.quantile(0.90) if len(r) else np.nan,
        "best": r.max() if len(r) else np.nan,
        "worst": r.min() if len(r) else np.nan,
    }
    if mfe is not None:
        row["mean_MFE_%"] = mfe.dropna().mean()
        row["mean_MAE_%"] = mae.dropna().mean()
    return row


def baseline_stats(trades: dict, start: pd.Timestamp, universe: list[str],
                   excursions: bool) -> dict:
    """The random-entry bar every screen has to clear: the same forward-return
    matrix over *all* stock-days in the window, unconditionally."""
    ret = trades["return_pct"].loc[start:, universe]
    flat = ret.stack()
    row = describe(flat, n_signals=int(ret.notna().sum().sum()),
                   n_dates=len(ret.index))
    if excursions:
        row["mean_MFE_%"] = trades["mfe_pct"].loc[start:, universe].stack().mean()
        row["mean_MAE_%"] = trades["mae_pct"].loc[start:, universe].stack().mean()
    return row


def benchmark_stats(trades: dict, start: pd.Timestamp, ticker: str) -> dict:
    """Buy-and-hold the benchmark for the same horizon, entered every day."""
    ret = trades["return_pct"].loc[start:, ticker].dropna()
    return describe(ret, n_signals=len(ret), n_dates=len(ret))


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

FMT = {c: "{:.2f}" for c in ("mean_%", "median_%", "win_rate_%", "std", "p10",
                             "p90", "best", "worst", "mean_MFE_%", "mean_MAE_%",
                             "excess_%")}


def print_table(summary: pd.DataFrame) -> None:
    cols = [c for c in summary.columns if c != "horizon"]
    shown = summary[cols].copy()
    for col, fmt in FMT.items():
        if col in shown:
            shown[col] = shown[col].map(lambda v: fmt.format(v)
                                        if pd.notna(v) else "n/a")
    print(shown.to_string(index=False))


def year_breakdown(trades: pd.DataFrame, horizon: int) -> None:
    """Mean return per calendar year -- regime dependence is the first thing
    that explains a good or bad headline number."""
    sub = trades[(trades["horizon"] == horizon) & trades["return_pct"].notna()]
    if sub.empty:
        return
    sub = sub.assign(year=pd.to_datetime(sub["signal_date"]).dt.year)
    table = sub.pivot_table(index=["screen", "cohort"], columns="year",
                            values="return_pct", aggfunc=["mean", "count"])
    print(f"\nMean return % / trade count by signal year (h={horizon}):")
    print(table.round(2).to_string())


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args(bt_cfg: dict) -> argparse.Namespace:
    out = bt_cfg.get("output", {})
    p = argparse.ArgumentParser(
        description="Universe-wide profit backtest of every screen.")
    p.add_argument("--years", type=int, default=bt_cfg.get("years", 3),
                   help="length of the analysis window")
    p.add_argument("--holding-days",
                   default=",".join(str(h) for h in bt_cfg.get("holding_days", [30])),
                   help="comma-separated holding periods in TRADING days")
    p.add_argument("--entry", default=bt_cfg.get("entry", "next_open"),
                   choices=["next_open", "signal_close"])
    p.add_argument("--screens", default=",".join(bt_cfg.get("screens", [])),
                   help="comma-separated config keys of the screens to run")
    p.add_argument("--no-near-misses", action="store_true",
                   help="hits only (default follows config include_near_misses)")
    p.add_argument("--refresh", action="store_true",
                   help="re-download the universe, ignoring the cache")
    p.add_argument("--no-chart", action="store_true")
    p.add_argument("--trades-csv",
                   default=out.get("trades_csv", "backtest_universe_trades.csv"))
    p.add_argument("--summary-csv",
                   default=out.get("summary_csv", "backtest_universe_summary.csv"))
    return p.parse_args()


def main() -> int:
    started = time.time()
    cfg = load_config()
    bt_cfg = cfg.get("backtest", {})
    args = parse_args(bt_cfg)

    horizons = [int(h) for h in args.holding_days.split(",") if h.strip()]
    wanted = [s.strip() for s in args.screens.split(",") if s.strip()]
    near_ok = bt_cfg.get("include_near_misses", True) and not args.no_near_misses
    excursions = bt_cfg.get("measure_excursions", True)
    benchmark = bt_cfg.get("benchmark_ticker")

    # -- which screens run: enabled in config AND requested --
    known = {module.CONFIG_KEY for module, _ in SCREENS}
    for key in wanted:
        if key not in known:
            raise SystemExit(f"unknown screen {key!r} -- known: {sorted(known)}")
    active = []
    for module, compute in SCREENS:
        strategy = cfg.get(module.CONFIG_KEY)
        if not strategy:
            print(f"No '{module.CONFIG_KEY}' section in config.json -- skipping.")
        elif not strategy.get("enabled", True):
            print(f"'{module.CONFIG_KEY}' is disabled -- skipping.")
        elif module.CONFIG_KEY not in wanted:
            print(f"'{module.CONFIG_KEY}' not in backtest.screens -- skipping.")
        else:
            active.append((module, compute, strategy))
    if not active:
        raise SystemExit("No screens to run.")

    warmup_days = max(module.required_history(strategy)
                      for module, _, strategy in active)

    section("STEP 1 -- Data")
    data = load_panel(bt_cfg, cfg, args.years, warmup_days, args.refresh)
    start = data.index[-1] - pd.DateOffset(years=args.years)
    universe = [t for t in data["Close"].columns if t != benchmark]
    analysis_days = len(data.loc[start:])
    print(f"Universe {len(universe)} tickers"
          + (f" (+ {benchmark} as benchmark)" if benchmark else ""))
    print(f"Analysis window {start.date()} .. {data.index[-1].date()} "
          f"({analysis_days} trading days); {len(data) - analysis_days} earlier "
          f"days used only to warm up the {warmup_days}-day longest lookback.")
    print(f"Entry: {args.entry}; exits at the Close after "
          f"{', '.join(str(h) for h in horizons)} trading day(s).")
    print("\nCAVEATS -- this is a screen-comparison tool, not a tradeable backtest:")
    print("  * Survivorship bias: the universe is TODAY's S&P 500, so companies")
    print("    dropped/acquired/delisted during the window are absent. Biased up.")
    print("  * No costs (commission/spread/slippage) and no dividends (price-only")
    print("    returns; splits are handled). High-yield names are understated.")
    print("  * No position sizing or capital limit: every signal is an independent")
    print("    equal-weight trade, even when dozens fire the same day.")
    print("  * Signals cluster in time, so trades overlap and share market beta --")
    print("    per-trade stats are NOT independent samples.")

    # -- STEP 2: signal masks, straight from the production compute functions --
    section("STEP 2 -- Signals (production compute_* over all history)")
    masks = {}
    for module, compute, strategy in active:
        signals = compute(data, strategy)
        hit = signals["signal"].fillna(False)[universe]
        masks[(module.CONFIG_KEY, "hit")] = hit
        n_hit = int(hit.loc[start:].sum().sum())
        line = f"{module.CONFIG_KEY}: {n_hit} hit(s)"
        if near_ok:
            # Most screens' second cohort is their production near-miss list;
            # the pullback screen has none, so it names its own (touch-no-fire).
            cohort = getattr(module, "NEAR_COHORT", "near")
            near = module.near_miss_mask(data, signals, strategy)
            near = near.fillna(False)[universe]
            masks[(module.CONFIG_KEY, cohort)] = near
            line += f", {int(near.loc[start:].sum().sum())} {cohort}"
        print(line + " in the analysis window")

    # -- STEP 3: simulate, per horizon --
    section("STEP 3 -- Trade simulation")
    all_trades, summary_rows = [], []
    for h in horizons:
        fwd = forward_trades(data, h, args.entry, excursions)
        frame = collect_trades(masks, fwd, start)
        if not frame.empty:
            all_trades.append(frame.assign(horizon=h))

        base = baseline_stats(fwd, start, universe, excursions)
        rows = [{"horizon": h, "screen": BASELINE_LABEL, "cohort": "-", **base}]
        if benchmark:
            rows.append({"horizon": h, "screen": f"{benchmark} buy-and-hold",
                         "cohort": "-", **benchmark_stats(fwd, start, benchmark)})
        for (screen, cohort), mask in masks.items():
            sub = frame[(frame["screen"] == screen) & (frame["cohort"] == cohort)] \
                if not frame.empty else pd.DataFrame(columns=["return_pct"])
            n_dates = sub["signal_date"].nunique() if not sub.empty else 0
            stats = describe(
                sub["return_pct"] if not sub.empty else pd.Series(dtype=float),
                n_signals=len(sub), n_dates=n_dates,
                mfe=sub.get("mfe_pct") if excursions and not sub.empty else None,
                mae=sub.get("mae_pct") if excursions and not sub.empty else None,
            )
            stats["excess_%"] = stats["mean_%"] - base["mean_%"]
            rows.append({"horizon": h, "screen": screen, "cohort": cohort, **stats})
        summary_rows += rows

        print(f"\n--- Holding period: {h} trading days "
              f"(entry {args.entry}) ---")
        print_table(pd.DataFrame(rows))

    summary = pd.DataFrame(summary_rows)
    trades = pd.concat(all_trades, ignore_index=True) if all_trades \
        else pd.DataFrame()

    section("STEP 4 -- Regime check")
    for h in horizons:
        if not trades.empty:
            year_breakdown(trades, h)

    # -- STEP 5: outputs --
    section("STEP 5 -- Outputs")
    out_dir = Path(__file__).parent
    cols = ["screen", "cohort", "horizon", "ticker", "signal_date", "status",
            "entry_date", "entry_price", "exit_date", "exit_price", "return_pct"]
    if excursions:
        cols += ["mfe_pct", "mae_pct"]
    if not trades.empty:
        # Signals in the last `h` days have no exit yet: flag them so nobody
        # averages the blank rows in as zeros.
        trades["status"] = np.where(trades["return_pct"].notna(), "closed", "open")
        n_open = int((trades["status"] == "open").sum())
        trades_path = out_dir / args.trades_csv
        out = trades[cols].copy()
        num = out.select_dtypes("number").columns  # dates must not be rounded
        out[num] = out[num].round(4)
        out.to_csv(trades_path, index=False)
        print(f"{len(trades)} trade(s) -> {trades_path}")
        if n_open:
            print(f"  ({n_open} still 'open': the signal fired too recently for a "
                  f"full holding period, so they have no return and are excluded "
                  f"from every statistic above.)")
    else:
        print("No trades to write (no signals in the analysis window).")

    summary_path = out_dir / args.summary_csv
    summary.round(4).to_csv(summary_path, index=False)
    print(f"Summary table -> {summary_path}")

    chart_cfg = bt_cfg.get("output", {})
    if not args.no_chart:
        base_path = out_dir / chart_cfg.get("chart_path", "backtest_universe.png")
        for h in horizons:
            # One chart per horizon; the configured name is used as-is for a
            # single horizon, suffixed when there are several.
            path = base_path if len(horizons) == 1 else \
                base_path.with_name(f"{base_path.stem}_h{h}{base_path.suffix}")
            charts.plot_backtest_summary(
                summary, h, args.entry, BASELINE_LABEL, path,
                dpi=chart_cfg.get("chart_dpi", 120))

    print(f"\nDone in {time.time() - started:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
