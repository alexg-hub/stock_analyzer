"""
Universe-wide historical backtest: did the screens actually make money?

Downloads the whole universe once (analysis window + rolling-window warm-up),
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
import trend_line
from scanner_common import (
    download_price_data,
    drop_unsettled_bars,
    load_config,
    output_dir,
    universe_tickers,
    warmup_months,
)

# Every screen the backtest can run, paired with its compute function. The
# modules also supply required_history(), fires_mask() and partial_mask() --
# the same functions the nightly scan uses, so there is one source of truth
# per screen.
SCREENS = [
    (breakout_scanner, breakout_scanner.compute_signals),
    (sma_pullback, sma_pullback.compute_pullback_signals),
    (sma_reclaim, sma_reclaim.compute_reclaim_signals),
    (trend_line, trend_line.compute_trend_signals),
]

BASELINE_LABEL = "ALL stock-days (random entry)"


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# --------------------------------------------------------------------------
# Data: one download, cached to disk
# --------------------------------------------------------------------------

def cache_path(bt_cfg: dict) -> Path:
    """Where the downloaded price panel is cached (inside `output/` unless the
    config gives an absolute path). Public because `tune_screen.py` reads the
    same cache."""
    path = Path(bt_cfg.get("cache_path", "backtest_universe_cache.pkl"))
    return path if path.is_absolute() else output_dir() / path


def cached_panel(bt_cfg: dict) -> pd.DataFrame:
    """The cached panel, or a clear error -- never a silent 500-ticker download.

    For tools that only ever want to reason over history already on disk
    (`tune_screen.py`), so a missing cache fails loudly instead of quietly
    costing minutes.
    """
    path = cache_path(bt_cfg)
    if not path.exists():
        raise SystemExit(
            f"No cached price panel at {path}.\n"
            f"Run `python backtest_universe.py` (optionally with --years N) "
            f"once to download and cache it.")
    panel = drop_unsettled_bars(pd.read_pickle(path))
    print(f"Using cached panel {path.name} ({panel.shape[1] // 6} tickers, "
          f"{len(panel)} days, {panel.index[0].date()} .. "
          f"{panel.index[-1].date()})")
    return panel


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
    path = cache_path(bt_cfg)
    max_age_days = bt_cfg.get("cache_max_age_days", 1)

    if path.exists() and not refresh:
        age_days = (time.time() - path.stat().st_mtime) / 86400
        # A cache written mid-session can end on an unsettled bar; clean it on
        # load so an old pickle behaves like a fresh download.
        cached = drop_unsettled_bars(pd.read_pickle(path))
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

    # Every configured index, alerted or not: a universe gets held back from
    # the alert precisely when nobody has measured it yet, and this is what
    # measures it. Same reason the screen `enabled` flag is ignored here.
    tickers = universe_tickers(cfg)
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
                   excursions: bool, delay: int = 0,
                   dates: bool = True) -> dict[str, pd.DataFrame]:
    """Entry/exit prices and returns for a trade opened on *every* day.

    Two timing knobs, both in TRADING days:
      * `delay` (x) -- how much longer to wait *beyond the earliest tradeable
        bar* before buying. x=0 buys as soon as possible, so look-ahead is
        impossible by construction whatever the entry convention.
      * `holding` (y) -- how long the position is then held after the entry day.

    For a signal on index position i, with `base` = 1 for `next_open` (the
    signal is only known after the close, so the next open is the earliest
    price you could actually pay) and 0 for `signal_close`:

        entry bar = i + base + x        exit bar = entry bar + y

      * `next_open`    -- buy Open[i+1+x], sell Close[i+1+x+y]
      * `signal_close` -- buy Close[i+x],  sell Close[i+x+y]

    Prices are raw Open/Close (split-adjusted by yfinance; dividends ignored).
    A window running past the end of the data yields NaN -- those signals are
    reported as unevaluable, never as a 0% trade.

    `dates=False` skips building the entry/exit date frames, which is the
    expensive part and is only needed when writing per-trade rows.
    """
    if holding < 1:
        raise SystemExit(f"holding period must be >= 1 trading day, got {holding}")
    if delay < 0:
        raise SystemExit(f"entry delay must be >= 0 trading days, got {delay}")
    close, open_ = data["Close"], data["Open"]
    high, low = data["High"], data["Low"]

    if entry == "next_open":
        base, price_at_entry = 1, open_
        # Position is open across the entry bar (bought at its open) through
        # the exit bar, so y+1 bars.
        span = holding + 1
    elif entry == "signal_close":
        base, price_at_entry = 0, close
        # Bought at the entry bar's *close*, so that bar's own high/low
        # happened before entry: the excursion window is y bars.
        span = holding
    else:
        raise SystemExit(f"unknown backtest.entry {entry!r} "
                         f"(expected 'next_open' or 'signal_close')")

    entry_offset = base + delay
    exit_offset = entry_offset + holding
    entry_price = price_at_entry.shift(-entry_offset)
    exit_price = close.shift(-exit_offset)

    out = {
        "entry_price": entry_price,
        "exit_price": exit_price,
        "return_pct": 100 * (exit_price / entry_price - 1),
    }
    if dates:
        # Carried along so each trade row can show what it actually did.
        out["entry_date"] = _shifted_dates(data, entry_offset)
        out["exit_date"] = _shifted_dates(data, exit_offset)
    if excursions:
        # Roll first, then shift back: the trailing `span`-bar extreme ending
        # at the exit bar, moved onto the signal day. (Shifting first and then
        # rolling would need `span`-1 rows *before* the signal day and so would
        # silently NaN out the start of the frame.)
        out["mfe_pct"] = 100 * (high.rolling(span).max().shift(-exit_offset)
                                / entry_price - 1)
        out["mae_pct"] = 100 * (low.rolling(span).min().shift(-exit_offset)
                                / entry_price - 1)
    return out


def _shifted_dates(data: pd.DataFrame, offset: int) -> pd.DataFrame:
    """The trading date `offset` rows ahead of each row, as a (days, tickers)
    frame so it can be reindexed alongside the price frames."""
    dates = pd.Series(data.index, index=data.index).shift(-offset)
    return pd.DataFrame({col: dates for col in data["Close"].columns})


def cohort_values(mask: pd.DataFrame, trades: dict,
                  start: pd.Timestamp) -> dict:
    """The distribution behind one cohort, without building any trade rows.

    Returns the raw 1-D arrays of return/MFE/MAE at the cells where the mask
    fired, plus the counts `describe()` needs. This is the sweep's hot path:
    a delay x holding grid re-simulates many times, and materializing the
    entry/exit *date* frames (see `_shifted_dates`) for each cell would cost
    far more than the statistics themselves. `collect_trades` below still
    builds full rows, but only for the one cell that gets a trades CSV.
    """
    mask = mask.loc[mask.index >= start]
    picked = mask.to_numpy()
    out = {
        "signals": int(picked.sum()),
        "distinct_dates": int(mask.any(axis=1).sum()),
    }
    for name in ("return_pct", "mfe_pct", "mae_pct"):
        if name in trades:
            # Align on the mask's own rows AND columns -- the trade frames still
            # carry the benchmark ticker, which no mask ever selects.
            aligned = trades[name].loc[mask.index, mask.columns]
            out[name] = aligned.to_numpy()[picked]
    return out


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

def describe(returns, n_signals: int, n_dates: int,
             mfe=None, mae=None) -> dict:
    """The stats block for one cohort at one (delay, holding) cell."""
    r = pd.Series(returns, dtype=float).dropna()
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
        row["mean_MFE_%"] = pd.Series(mfe, dtype=float).dropna().mean()
        row["mean_MAE_%"] = pd.Series(mae, dtype=float).dropna().mean()
    return row


def stats_from(vals: dict) -> dict:
    """`describe()` over a `cohort_values()` result."""
    return describe(vals.get("return_pct", []),
                    n_signals=vals["signals"], n_dates=vals["distinct_dates"],
                    mfe=vals.get("mfe_pct"), mae=vals.get("mae_pct"))


def baseline_stats(trades: dict, start: pd.Timestamp, universe: list[str],
                   excursions: bool) -> dict:
    """The random-entry bar every screen has to clear: the same forward-return
    matrix over *all* stock-days in the window, unconditionally.

    Depends only on the holding period: a random entry has no signal to be
    delayed from, and the distribution of y-day returns over every stock-day is
    the same whatever `delay` a screen used. So one baseline per holding
    period is reused down a whole grid column, which also makes `excess_%`
    comparable across delays.
    """
    ret = trades["return_pct"].loc[start:, universe].to_numpy().ravel()
    row = describe(ret, n_signals=int(np.isfinite(ret).sum()),
                   n_dates=len(trades["return_pct"].loc[start:].index))
    if excursions:
        row["mean_MFE_%"] = np.nanmean(
            trades["mfe_pct"].loc[start:, universe].to_numpy())
        row["mean_MAE_%"] = np.nanmean(
            trades["mae_pct"].loc[start:, universe].to_numpy())
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
    cols = [c for c in summary.columns if c not in ("horizon", "delay")]
    shown = summary[cols].copy()
    for col, fmt in FMT.items():
        if col in shown:
            shown[col] = shown[col].map(lambda v: fmt.format(v)
                                        if pd.notna(v) else "n/a")
    print(shown.to_string(index=False))


def print_grids(summary: pd.DataFrame, baseline_label: str) -> None:
    """One wait x hold matrix per screen/cohort: does waiting before buying
    help, and does the answer depend on how long you then hold?"""
    cohorts = summary[summary["cohort"] != "-"]
    base = (summary[summary["screen"] == baseline_label]
            .set_index("horizon")["mean_%"])
    for (screen, cohort), sub in cohorts.groupby(["screen", "cohort"], sort=False):
        print(f"\n{screen} / {cohort}")
        for metric, label in (("mean_%", "mean return %"),
                              ("win_rate_%", "win rate %")):
            grid = sub.pivot_table(index="delay", columns="horizon",
                                   values=metric)
            grid.index.name = "wait\\hold"
            print(f"  {label}:")
            print("    " + grid.round(2).to_string().replace("\n", "\n    "))
    print(f"\n  (random entry for reference: "
          + ", ".join(f"hold {h} = {v:+.2f}%" for h, v in base.items()) + ")")


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
                   help="comma-separated holding periods in TRADING days (y)")
    p.add_argument("--entry-delay",
                   default=",".join(str(d) for d in
                                    bt_cfg.get("entry_delay_days", [0])),
                   help="comma-separated extra TRADING days to wait before "
                        "buying, beyond the earliest tradeable bar (x); 0 = buy "
                        "as soon as possible")
    p.add_argument("--entry", default=bt_cfg.get("entry", "next_open"),
                   choices=["next_open", "signal_close"])
    p.add_argument("--screens", default=",".join(bt_cfg.get("screens", [])),
                   help="comma-separated config keys of the screens to run")
    p.add_argument("--split-by-tier", action="store_true",
                   help="measure full and partial setups as separate cohorts "
                        "(default follows config split_by_tier)")
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

    horizons = sorted({int(h) for h in args.holding_days.split(",") if h.strip()})
    delays = sorted({int(d) for d in args.entry_delay.split(",") if d.strip()})
    wanted = [s.strip() for s in args.screens.split(",") if s.strip()]
    split_tiers = bt_cfg.get("split_by_tier", False) or args.split_by_tier
    excursions = bt_cfg.get("measure_excursions", True)
    benchmark = bt_cfg.get("benchmark_ticker")

    # The one (wait, hold) cell that gets the detailed table, the per-trade CSV,
    # the year breakdown and the bar chart. Explicit in config rather than
    # "first in the list", so widening the swept lists never silently moves it.
    detail_cfg = bt_cfg.get("detail", {})
    detail = (detail_cfg.get("entry_delay_days", delays[0]),
              detail_cfg.get("holding_days", horizons[0]))
    if detail[0] not in delays or detail[1] not in horizons:
        fallback = (delays[0], horizons[0])
        print(f"backtest.detail {detail} is not in the swept grid "
              f"(waits {delays}, holds {horizons}) -- using {fallback}.")
        detail = fallback

    # -- which screens run: whatever backtest.screens/--screens asks for.
    # Deliberately NOT filtered by the strategy's `enabled` flag: that switch
    # governs the nightly *alert*, and a screen is usually switched off exactly
    # when it is underperforming -- which is when you most need to measure it.
    # `backtest.screens` is this tool's own selector. (tune_screen.py ignores
    # `enabled` for the same reason.)
    known = {module.CONFIG_KEY for module, _ in SCREENS}
    for key in wanted:
        if key not in known:
            raise SystemExit(f"unknown screen {key!r} -- known: {sorted(known)}")
    active = []
    for module, compute in SCREENS:
        strategy = cfg.get(module.CONFIG_KEY)
        if not strategy:
            print(f"No '{module.CONFIG_KEY}' section in config.json -- skipping.")
        elif module.CONFIG_KEY not in wanted:
            print(f"'{module.CONFIG_KEY}' not in backtest.screens -- skipping.")
        else:
            if not strategy.get("enabled", True):
                print(f"note: '{module.CONFIG_KEY}' is disabled for the nightly "
                      f"alert -- backtesting it anyway.")
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
    base_bar = "the open after the signal" if args.entry == "next_open" \
        else "the signal's own close"
    print(f"Entry: {args.entry} -- buy at {base_bar}, plus a wait of "
          f"{', '.join(str(d) for d in delays)} extra trading day(s).")
    print(f"Exit: the Close {', '.join(str(h) for h in horizons)} trading "
          f"day(s) after entry. Grid = {len(delays)}x{len(horizons)} = "
          f"{len(delays) * len(horizons)} cell(s); detailed table for "
          f"wait={detail[0]}, hold={detail[1]}.")
    print("\nCAVEATS -- this is a screen-comparison tool, not a tradeable backtest:")
    print("  * Survivorship bias: the universe is TODAY's index membership, so")
    print("    companies dropped/acquired/delisted during the window are absent,")
    print("    and a name promoted out of the mid-caps is backtested in the index")
    print("    it sits in today. Biased up.")
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
        # The alert cohort: exactly what the nightly scan would have sent.
        fires = module.fires_mask(data, signals, strategy).fillna(False)[universe]
        n_fires = int(fires.loc[start:].sum().sum())
        if not split_tiers:
            masks[(module.CONFIG_KEY, "signal")] = fires
            print(f"{module.CONFIG_KEY}: {n_fires} signal(s) in the analysis window")
            continue

        # Split the tiers apart for analysis. Note the pullback screen's
        # partial_mask is NOT part of its fires_mask (it never alerts on
        # partials), so there it is a control cohort, not a sent signal.
        full = signals["signal"].fillna(False)[universe]
        partial = module.partial_mask(data, signals, strategy).fillna(False)[universe]
        masks[(module.CONFIG_KEY, "full")] = full
        masks[(module.CONFIG_KEY, "partial")] = partial
        n_full = int(full.loc[start:].sum().sum())
        n_partial = int(partial.loc[start:].sum().sum())
        # When fires == full the screen stays strict, so its partial cohort is
        # a control group that was never alerted on.
        note = "" if n_fires > n_full else " (partial never alerted -- control only)"
        print(f"{module.CONFIG_KEY}: {n_fires} alerted signal(s) = {n_full} full "
              f"+ {n_partial} partial{note}, in the analysis window")

    # -- STEP 3: simulate every (wait, hold) cell of the grid --
    section("STEP 3 -- Trade simulation")
    summary_rows, trades = [], pd.DataFrame()
    for h in horizons:
        # One baseline per holding period, reused across every delay (see
        # baseline_stats) -- so excess_% is comparable down a grid column.
        base = None
        for d in delays:
            is_detail = (d, h) == detail
            fwd = forward_trades(data, h, args.entry, excursions, delay=d,
                                 dates=is_detail)
            if base is None:
                base = baseline_stats(fwd, start, universe, excursions)
                rows = [{"delay": d, "horizon": h, "screen": BASELINE_LABEL,
                         "cohort": "-", **base}]
                if benchmark:
                    rows.append({"delay": d, "horizon": h, "cohort": "-",
                                 "screen": f"{benchmark} buy-and-hold",
                                 **benchmark_stats(fwd, start, benchmark)})
            else:
                rows = []

            for (screen, cohort), mask in masks.items():
                stats = stats_from(cohort_values(mask, fwd, start))
                stats["excess_%"] = stats["mean_%"] - base["mean_%"]
                rows.append({"delay": d, "horizon": h, "screen": screen,
                             "cohort": cohort, **stats})
            summary_rows += rows

            # Per-trade rows only for the detail cell: a full grid would be
            # ~1M rows, and building the date frames is the expensive part.
            if is_detail:
                trades = collect_trades(masks, fwd, start)
                if not trades.empty:
                    trades = trades.assign(delay=d, horizon=h)
                print(f"\n--- Detailed table: wait {d}, hold {h} trading days "
                      f"(entry {args.entry}) ---")
                print_table(pd.DataFrame(rows))

    summary = pd.DataFrame(summary_rows)

    section("STEP 4 -- Wait x hold grid")
    if len(delays) > 1 or len(horizons) > 1:
        print_grids(summary, BASELINE_LABEL)
    else:
        print("Single (wait, hold) cell -- nothing to compare. Widen "
              "backtest.entry_delay_days / holding_days for a grid.")

    section("STEP 5 -- Regime check")
    if not trades.empty:
        year_breakdown(trades, detail[1])

    # -- STEP 6: outputs --
    section("STEP 6 -- Outputs")
    out_dir = output_dir()
    cols = ["screen", "cohort", "delay", "horizon", "ticker", "signal_date",
            "status", "entry_date", "entry_price", "exit_date", "exit_price",
            "return_pct"]
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
        print(f"{len(trades)} trade(s) for the detail cell "
              f"(wait {detail[0]}, hold {detail[1]}) -> {trades_path}")
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
        dpi = chart_cfg.get("chart_dpi", 120)
        # Bar chart of the detail cell -- the one combination with per-trade rows.
        base_path = out_dir / chart_cfg.get("chart_path", "backtest_universe.png")
        charts.plot_backtest_summary(
            summary[summary["delay"] == detail[0]], detail[1], args.entry,
            BASELINE_LABEL, base_path, dpi=dpi)
        # Heatmap of the whole grid -- only meaningful with something to compare.
        if len(delays) > 1 or len(horizons) > 1:
            grid_path = out_dir / chart_cfg.get("grid_chart_path",
                                                "backtest_universe_grid.png")
            charts.plot_delay_grid(summary, args.entry, BASELINE_LABEL,
                                   grid_path, dpi=dpi)

    print(f"\nDone in {time.time() - started:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
