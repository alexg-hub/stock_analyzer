"""Fill the entries and mark the book.

Two things the ledger cannot do for itself:

1. **The entry price does not exist when the signal is recorded.** The nightly
   run fires after the US close; `Open[t+1]` is most of a day away. So `sync`
   records the position as `pending` and this fills it on a later run. A
   position is never "bought" at a price that had not traded yet.
2. **Prices move.** Every horizon's exit and the open mark-to-market are
   recomputed on every run, which also means a re-run repairs anything a killed
   run left half-written.

The arithmetic is **not** reimplemented here. `backtest_universe.forward_trades`
is the repo's one next-day-open convention (buy `Open[i+1+delay]`, sell
`Close[i+1+delay+holding]`, MFE/MAE rolled-then-shifted over the bars the
position is actually open). Calling it means a ledger return and a universe
backtest return are the same measurement, so the two stages can be compared
without an asterisk.
"""

import math

import pandas as pd

from backtest_universe import forward_trades
from research_report import load_facts
from scanner_common import (
    download_price_data,
    drop_unsettled_tail,
    positions_csv_path,
    step,
    warmup_months,
)

from .ledger import (
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_PENDING,
    horizon_cols,
    horizons_of,
    load_positions,
    text_of,
)

# The quant dimensions the deep dive scored, flattened one column per
# dimension. Recorded so tier 4 can ask which of tier 3's checks actually
# predicted the return -- see research_report._facts.
QUANT_PREFIX = "quant_"
QUANT_METRIC_PREFIX = "qm_"


def _period_for(oldest: pd.Timestamp, horizons: list[int]) -> str:
    """A yfinance period long enough to cover the oldest open position.

    Padded by the longest horizon plus `warmup_months`' own slack, so the exit
    bar of the oldest position is inside the window even when it has not
    printed yet.
    """
    days = (pd.Timestamp.today().normalize() - oldest).days
    months = math.ceil(days / 30) + warmup_months(max(horizons)) + 1
    if months <= 6:
        return "6mo"
    return f"{math.ceil(months / 12)}y"


def price_panel(tickers: list[str], oldest: pd.Timestamp, cfg: dict) -> pd.DataFrame:
    """Daily bars for the tickers actually held, plus the benchmark.

    Deliberately a fresh download rather than `backtest_universe`'s cached
    panel: that cache is a day stale at best, is keyed to today's index
    membership, and is refreshed on its own schedule. A handful of held tickers
    is a cheap request, and the ledger must never report a mark it did not
    fetch.
    """
    port_cfg = cfg.get("portfolio", {})
    interval = cfg.get("data", {}).get("download_interval", "1d")
    period = _period_for(oldest, horizons_of(port_cfg))
    panel = download_price_data(tickers, period, interval)
    return drop_unsettled_tail(panel)


def _cell(frame: pd.DataFrame, day, ticker):
    """One value out of a (days, tickers) frame, or None if it isn't there."""
    if frame is None or ticker not in frame.columns or day not in frame.index:
        return None
    value = frame.at[day, ticker]
    if isinstance(value, pd.Series):          # duplicated index label
        value = value.iloc[0]
    return None if pd.isna(value) else value


def _iso(value):
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date().isoformat()


def _tier3_columns(ticker: str, scan_date: str, cfg: dict) -> dict:
    """Tier 3's recorded judgment for this position, flattened for analysis.

    Read from `<TICKER>_<scan_date>_facts.json` rather than recomputed: the
    quant score is the anchor the verdict was actually made on, and
    recomputing it now would score today's fundamentals against a judgment
    reached weeks ago. Absent facts mean the ticker was never deep-dived,
    which is a legitimate cohort -- not a gap to fill in.
    """
    facts = load_facts(ticker, scan_date, cfg)
    if not facts:
        return {}
    out = {}
    score = facts.get("quant_score")
    if score is not None:
        out["quant_score"] = score
    for name, block in (facts.get("quant_dimensions") or {}).items():
        if isinstance(block, dict) and block.get("score") is not None:
            out[f"{QUANT_PREFIX}{name}"] = block["score"]
    for name, value in (facts.get("quant_metrics") or {}).items():
        if value is not None:
            out[f"{QUANT_METRIC_PREFIX}{name}"] = value
    for key, column in (("upside_pct", "Upside %"),
                        ("pe_percentile_2y", "P/E Pctile 2y"),
                        ("days_to_earnings", "Days To Earnings")):
        if facts.get(key) is not None:
            out[column] = facts[key]
    return out


def mark(cfg: dict) -> pd.DataFrame:
    """Fill entry prices, mark every horizon, refresh tier 3. Returns the ledger."""
    with step("MARK", cfg=cfg) as s:
        positions = load_positions(cfg)
        if positions.empty:
            s.status = "warn"
            s.detail = "ledger is empty -- run `open` first"
            return positions

        port_cfg = cfg.get("portfolio", {})
        horizons = horizons_of(port_cfg)
        entry = port_cfg.get("entry", "next_open")
        notional = float(port_cfg.get("notional", 10000))
        excursions = bool(port_cfg.get("measure_excursions", True))
        benchmark = port_cfg.get("benchmark_ticker", "SPY")

        dates = pd.to_datetime(positions["scan_date"], errors="coerce")
        live = (positions["status"].astype(str) != STATUS_CLOSED
                if "status" in positions.columns
                else pd.Series(True, index=positions.index))
        if not live.any():
            s.detail = f"all {len(positions)} position(s) already closed"
            return positions

        tickers = sorted(set(positions.loc[live, "ticker"].astype(str)))
        oldest = dates[live].min()
        panel = price_panel(sorted(set(tickers) | {benchmark}), oldest, cfg)
        available = set(panel["Close"].columns)

        trades = {h: forward_trades(panel, h, entry, excursions, delay=0,
                                    dates=True)
                  for h in horizons}
        closes = panel["Close"]
        last_bar = panel.index[-1]

        filled = closed = missing = 0
        updates = {}
        for idx in positions.index[live]:
            ticker = str(positions.at[idx, "ticker"])
            day = dates.at[idx]
            row = {"mark_date": last_bar.date().isoformat()}
            if ticker not in available or pd.isna(day) or day not in panel.index:
                # A delisted name, or a scan_date that is not a trading day in
                # this panel. Recorded as unpriceable rather than as a 0% trade.
                missing += 1
                updates[idx] = row
                continue

            first = trades[horizons[0]]
            entry_price = _cell(first["entry_price"], day, ticker)
            entry_date = _cell(first["entry_date"], day, ticker)
            if entry_price is None:
                # The entry bar has not traded yet -- correct on the night a
                # signal fires, and the reason `open` and `mark` are separate.
                row["status"] = STATUS_PENDING
                updates[idx] = row
                continue

            filled += 1
            # Round once, then derive everything else from the rounded price:
            # a reader who multiplies the stored share count by the stored
            # entry price has to get the stored notional back, or the ledger
            # contradicts itself in the third column.
            entry_price = round(float(entry_price), 4)
            row["entry_price"] = entry_price
            row["entry_date"] = _iso(entry_date)
            row["shares"] = round(notional / entry_price, 6)
            row["notional"] = notional

            last_close = _cell(closes, last_bar, ticker)
            if last_close is not None:
                row["last_close"] = round(float(last_close), 4)
                row["open_ret_%"] = round(100 * (float(last_close) / entry_price - 1), 4)
            if entry_date is not None:
                held = panel.index[(panel.index >= entry_date) & (panel.index <= last_bar)]
                row["days_held"] = len(held)

            complete = True
            for h in horizons:
                cols, frame = horizon_cols(h), trades[h]
                ret = _cell(frame["return_pct"], day, ticker)
                if ret is None:
                    complete = False
                    continue
                row[cols["ret"]] = round(float(ret), 4)
                row[cols["exit_date"]] = _iso(_cell(frame["exit_date"], day, ticker))
                exit_price = _cell(frame["exit_price"], day, ticker)
                if exit_price is not None:
                    row[cols["exit_price"]] = round(float(exit_price), 4)
                if excursions:
                    for name in ("mfe", "mae"):
                        value = _cell(frame[f"{name}_pct"], day, ticker)
                        if value is not None:
                            row[cols[name]] = round(float(value), 4)
                # The same horizon in the benchmark, entered the same day:
                # "did this beat simply owning the index over these bars?"
                bench = _cell(frame["return_pct"], day, benchmark)
                if bench is not None:
                    row[cols["bench"]] = round(float(bench), 4)
                    row[cols["excess"]] = round(float(ret) - float(bench), 4)

            row["status"] = STATUS_CLOSED if complete else STATUS_OPEN
            closed += int(complete)
            row.update(_tier3_columns(ticker, text_of(positions.at[idx, "scan_date"]), cfg))
            updates[idx] = row

        # Widen every touched column to object *before* writing into it. A
        # column the ledger created as all-NA comes back from CSV as float64,
        # and pandas refuses to put an ISO date string in one -- so the first
        # mark of a fresh ledger would die on `entry_date`. Dtypes are
        # re-inferred on the next read anyway; the CSV is the interface.
        touched = {c for values in updates.values() for c in values}
        for column in touched:
            if column not in positions.columns:
                positions[column] = pd.NA
            positions[column] = positions[column].astype(object)
        for idx, values in updates.items():
            for column, value in values.items():
                positions.at[idx, column] = value

        path = positions_csv_path(cfg)
        positions.to_csv(path, index=False, encoding="utf-8")
        s.detail = (f"{len(positions)} position(s): {filled} filled, "
                    f"{closed} closed, {int(live.sum()) - filled - missing} pending"
                    + (f", {missing} unpriceable" if missing else ""))
        return positions
