"""The double-top exit: the first thing in this repo that says *sell*.

Every other tier is entry-side. Tiers 1-3 decide what looks worth buying and
tier 4 buys it virtually, but tier 4's exit is purely time-based --
`ret_10d/30d/60d`, a horizon that expires whatever the chart is doing. This
watches the names actually on the book and records a virtual sell when one
completes a double top and breaks its neckline.

Three rules hold it together:

1. **It flags the position, it does not close it.** `status` and every
   `ret_*d_%` keep running exactly as before. That is the whole point: the
   fixed horizon and the double-top exit become two measurements of the *same*
   position, so a later analysis can ask which one you should have taken. A
   version of this that closed the position would answer that question by
   deleting the evidence.
2. **It is not a screen.** It is deliberately absent from
   `run_scanners.SCANNERS` and `backtest.screens`, because both consumers read
   a signal as a *buy*: `backtest_universe.forward_trades` would happily score
   "buy the neckline break, sell 30 days later", the exact inverse of what this
   means. It lives here, next to the ledger it reads.
3. **It is re-runnable.** A position that already carries `dt_signal_date` is
   never re-examined, and detection searches every bar since entry rather than
   only the last one -- so a second run in the same night changes nothing, and
   a night the scan did not run is picked up by the next one instead of being
   lost. Same discipline as `ledger.sync`.

This is also the one place tier 4 speaks to Discord. `open`, `mark` and
`analyze` never do, and must not start: they are bookkeeping, and a measurement
does not need announcing. An exit is different -- it is the only thing tier 4
produces that is actionable on the day it happens.
"""

import pandas as pd

from charts import plot_double_top
from scanner_common import (
    exits_csv_path,
    log_step,
    merge_history_csv,
    portfolio_dir,
    positions_csv_path,
    send_discord_alert,
    step,
)

from . import marking
from .ledger import (
    EXIT_FILLED,
    EXIT_FLAG_COL,
    EXIT_PENDING,
    STATUS_OPEN,
    load_positions,
    text_of,
)

# Red, and deliberately not one of the three screen colours: a card in this
# colour is the only one in the channel that means "get out", and it must not
# be mistakable at a glance for a card that means "get in".
EMBED_COLOR = 0xC0392B

# Where a signal chart lands: under `portfolio_dir`, not `output_dir`. Its own
# subdirectory keeps a night's exit charts out of the entry charts the scan
# alert built -- and being under the portfolio directory means it follows
# `portfolio.dir`, so a test that redirects the ledger redirects the charts
# with it instead of writing PNGs into the real output/.
CHART_SUBDIR = "exit_charts"


# --------------------------------------------------------------------------
# The pattern
# --------------------------------------------------------------------------

def required_history(strategy: dict) -> int:
    """Total lookback before this rule can fire at all."""
    recent = int(strategy.get("recent_window_days", 20))
    prior = int(strategy.get("prior_window_days", 90))
    need = 1 + recent + prior
    if strategy.get("min_volume_ratio") is not None:
        need = max(need, int(strategy.get("volume_sma_days", 30)) + 1)
    return need


def compute_double_top(data: pd.DataFrame, strategy: dict) -> dict[str, pd.DataFrame]:
    """Every intermediate and the signal, as `(days, tickers)` frames.

    A **rolling-extrema** double top, not a swing-pivot one: the two peaks are
    the maxima of two disjoint trailing windows and the neckline is the minimum
    of the recent one. That is an approximation, and an intentional one -- it
    keeps the whole rule vectorized over the entire panel, exactly like every
    `compute_*` in this repo, instead of walking each ticker's bars looking for
    pivots. Don't "fix" it into a per-ticker loop.

        peak2   = highest high of the last `recent` bars      (the right shoulder)
        peak1   = highest high of the `prior` bars before those
        trough  = lowest low of the last `recent` bars        (the neckline)

    Everything is `shift`ed by at least one bar, so the pattern is complete
    *before* the bar that breaks it -- the same reason the breakout screen
    measures its prior range with `shift(1)`.
    """
    recent = int(strategy.get("recent_window_days", 20))
    prior = int(strategy.get("prior_window_days", 90))
    max_diff = float(strategy.get("max_peak_diff_pct", 0.03))
    min_depth = float(strategy.get("min_trough_depth_pct", 0.05))
    confirm = float(strategy.get("break_confirm_pct", 0.005))
    vol_days = int(strategy.get("volume_sma_days", 30))
    min_vol = strategy.get("min_volume_ratio")

    close, high, low = data["Close"], data["High"], data["Low"]

    peak2 = high.shift(1).rolling(recent).max()
    peak1 = high.shift(recent + 1).rolling(prior).max()
    trough = low.shift(1).rolling(recent).min()
    lower_peak = peak1.where(peak1 <= peak2, peak2)

    peak_diff_pct = (peak2 - peak1).abs() / peak1
    trough_depth_pct = (lower_peak - trough) / lower_peak
    neckline = trough * (1 - confirm)

    # D1: the two peaks are the same height, which is what makes it a *double*
    # top rather than a trend that happens to have pulled back.
    is_twin_peaks = peak_diff_pct <= max_diff
    # D2: a real valley between them. Without this, a flat drift sideways has
    # two "equal peaks" and a neckline a hair below them, and every wobble
    # fires.
    is_deep_trough = trough_depth_pct >= min_depth
    # D3: the break itself.
    is_break = close < neckline
    # D4: only the *first* close below the neckline. A name that stays under it
    # satisfies D3 every night, so without this it re-alerts until the horizon
    # expires. Same fresh-event guard as the reclaim screen's `is_fresh_cross`.
    is_fresh_break = ~(close.shift(1) < neckline.shift(1)).fillna(False)

    signal = is_twin_peaks & is_deep_trough & is_break
    if strategy.get("alert_only_on_break", True):
        signal = signal & is_fresh_break

    out = {
        "peak1": peak1,
        "peak2": peak2,
        "trough": trough,
        "neckline": neckline,
        "peak_diff_pct": peak_diff_pct,
        "trough_depth_pct": trough_depth_pct,
        "is_twin_peaks": is_twin_peaks,
        "is_deep_trough": is_deep_trough,
        "is_break": is_break,
        "is_fresh_break": is_fresh_break,
    }

    # The volume leg is off by default: `null` means "condition not applied",
    # the same convention as reclaim's `min_day_gain_pct`. It can only ever
    # remove signals, so leaving it null reproduces the rule above exactly.
    if min_vol is not None:
        volume = data["Volume"]
        vol_ratio = volume / volume.shift(1).rolling(vol_days).mean()
        is_volume_surge = vol_ratio >= float(min_vol)
        out["vol_ratio"] = vol_ratio
        out["is_volume_surge"] = is_volume_surge
        signal = signal & is_volume_surge

    out["signal"] = signal.fillna(False)
    return out


def build_calc_table(data: pd.DataFrame, signals: dict, ticker: str) -> pd.DataFrame:
    """One ticker's per-day working, for the chart. Same shape as a screen's."""
    table = pd.DataFrame({
        "Close": data["Close"][ticker],
        "High": data["High"][ticker],
        "Low": data["Low"][ticker],
        "Volume": data["Volume"][ticker],
        "PEAK1": signals["peak1"][ticker],
        "PEAK2": signals["peak2"][ticker],
        "NECKLINE": signals["neckline"][ticker],
        "SIGNAL": signals["signal"][ticker],
    })
    table.index.name = "Date"
    return table


# --------------------------------------------------------------------------
# The scan
# --------------------------------------------------------------------------

def _held(positions: pd.DataFrame) -> pd.Series:
    """The rows an exit rule can still act on.

    `pending` has no entry price to sell against; `closed` has no horizon left
    to shorten; an already-flagged row has had its answer. Anything else is
    live money, virtually speaking.
    """
    if positions.empty:
        return pd.Series(dtype=bool)
    status = (positions["status"].astype(str) if "status" in positions.columns
              else pd.Series(STATUS_OPEN, index=positions.index))
    live = status == STATUS_OPEN
    if "entry_price" in positions.columns:
        live &= positions["entry_price"].notna()
    else:
        live &= False
    if EXIT_FLAG_COL in positions.columns:
        live &= positions[EXIT_FLAG_COL].isna()
    return live


def _first_break(fires: pd.Series, entry_date) -> pd.Timestamp | None:
    """The first neckline break at or after entry, or None.

    Deliberately not `.iloc[-1]`. Scanning the whole window since entry is what
    makes a missed night recoverable: the scan does not have to have been
    running on the day the pattern completed for the exit to be recorded.
    """
    if entry_date is not None and not pd.isna(entry_date):
        fires = fires[fires.index >= pd.Timestamp(entry_date)]
    hits = fires[fires.fillna(False)]
    return None if hits.empty else hits.index[0]


def _next_open(panel: pd.DataFrame, day: pd.Timestamp, ticker: str):
    """`Open[t+1]` -- the repo's one entry/exit convention, applied to a sell.

    Returns `(date, price)`, or `(None, None)` when the next bar has not traded
    yet. A break on the last settled bar is the normal case for that, and it is
    why an exit has a `pending` state at all.
    """
    later = panel.index[panel.index > day]
    if len(later) == 0:
        return None, None
    nxt = later[0]
    opens = panel["Open"]
    if ticker not in opens.columns:
        return None, None
    price = opens.at[nxt, ticker]
    if isinstance(price, pd.Series):
        price = price.iloc[0]
    if pd.isna(price):
        return None, None
    return nxt, float(price)


def _round(value, digits=4):
    return None if value is None or pd.isna(value) else round(float(value), digits)


def _chart(table: pd.DataFrame, ticker: str, day, strategy: dict,
           chart_cfg: dict, cfg: dict):
    """The signal chart, or None when charts are off or the render fails.

    A missing chart must cost the picture, never the alert -- the same
    tolerance the scan alert applies to its own.
    """
    if not chart_cfg.get("enabled", True):
        return None
    directory = portfolio_dir(cfg) / CHART_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ticker}_{pd.Timestamp(day).date().isoformat()}_doubletop.png"
    try:
        plot_double_top(table, strategy, ticker, path,
                        dpi=chart_cfg.get("dpi", 120),
                        lookback_days=chart_cfg.get("lookback_days", 250))
    except Exception as exc:                          # noqa: BLE001
        log_step("EXIT", "warn", f"{ticker} chart failed: "
                 f"{type(exc).__name__}: {exc}", cfg=cfg)
        return None
    return path


def _embed(row: dict, chart_path) -> dict:
    """One exit card. Figures come from the recorded row, never retyped."""
    name = row.get("name") or ""
    title = f"{row['ticker']} ({name}) -- double top" if name \
        else f"{row['ticker']} -- double top"
    fields = [
        {"name": "Entry", "value": f"{row['entry_price']:.2f}", "inline": True},
        {"name": "Exit", "value": (f"{row['exit_price']:.2f}"
                                   if row.get("exit_price") is not None
                                   else "next open"), "inline": True},
        {"name": "Return", "value": (f"{row['ret_%']:+.2f}%"
                                     if row.get("ret_%") is not None
                                     else "n/a"), "inline": True},
        {"name": "Peak 1", "value": f"{row['peak1']:.2f}", "inline": True},
        {"name": "Peak 2", "value": f"{row['peak2']:.2f}", "inline": True},
        {"name": "Neckline", "value": f"{row['neckline']:.2f}", "inline": True},
    ]
    embed = {
        "title": title,
        "color": EMBED_COLOR,
        "description": (f"Neckline broken {row['signal_date']} -- entered "
                        f"{row['entry_date']} on {row['config_key']}."),
        "fields": fields,
    }
    if chart_path is not None:
        embed["image"] = {"url": f"attachment://{chart_path.name}"}
    return embed


def exit_scan(cfg: dict, send: bool = True) -> pd.DataFrame:
    """Detect double tops on the book, flag them, record the sells, alert.

    Returns the frame of exits *recorded on this run* (empty when nothing
    fired), not the whole sell table.
    """
    with step("EXIT", cfg=cfg) as s:
        strategy = cfg.get("exit_strategy", {})
        if not strategy.get("enabled", True):
            s.status = "off"
            s.detail = "exit_strategy.enabled is false"
            return pd.DataFrame()

        positions = load_positions(cfg)
        if positions.empty:
            s.status = "warn"
            s.detail = "ledger is empty -- run `open` first"
            return pd.DataFrame()

        held = _held(positions)
        if not held.any():
            s.detail = f"no open position to watch ({len(positions)} on the book)"
            return pd.DataFrame()

        tickers = sorted(set(positions.loc[held, "ticker"].astype(str)))
        entry_dates = pd.to_datetime(positions["entry_date"], errors="coerce")
        oldest = entry_dates[held].min()
        if pd.isna(oldest):
            oldest = pd.to_datetime(positions.loc[held, "scan_date"],
                                    errors="coerce").min()
        panel = marking.price_panel(tickers, oldest, cfg,
                                    extra_bars=required_history(strategy))
        signals = compute_double_top(panel, strategy)
        fires = signals["signal"]
        available = set(panel["Close"].columns)

        chart_cfg = cfg.get("charts", {})
        updates, sells, embeds, charts = {}, [], [], []
        # One price series per ticker, but possibly several positions on it (a
        # name that fired on two screens is two rows). Detect once, flag each.
        detected = {}
        for idx in positions.index[held]:
            ticker = str(positions.at[idx, "ticker"])
            if ticker not in available or ticker not in fires.columns:
                continue
            entry_date = entry_dates.at[idx]
            day = _first_break(fires[ticker], entry_date)
            if day is None:
                continue

            entry_price = float(positions.at[idx, "entry_price"])
            exit_date, exit_price = _next_open(panel, day, ticker)
            row = {
                EXIT_FLAG_COL: pd.Timestamp(day).date().isoformat(),
                "dt_peak1": _round(signals["peak1"].at[day, ticker]),
                "dt_peak2": _round(signals["peak2"].at[day, ticker]),
                "dt_neckline": _round(signals["neckline"].at[day, ticker]),
                "dt_status": EXIT_FILLED if exit_price is not None else EXIT_PENDING,
            }
            ret = None
            if exit_price is not None:
                exit_price = round(exit_price, 4)
                ret = round(100 * (exit_price / entry_price - 1), 4)
                row["dt_exit_date"] = pd.Timestamp(exit_date).date().isoformat()
                row["dt_exit_price"] = exit_price
                row["dt_ret_%"] = ret
            updates[idx] = row

            # The sell record itself. A pending exit writes no row: a sell
            # with no exit price is not a sell, and it lands on the next run
            # once the open has traded.
            record = {
                "position_id": text_of(positions.at[idx, "position_id"]),
                "ticker": ticker,
                "name": text_of(positions.at[idx, "Company"])
                        if "Company" in positions.columns else "",
                "entry_date": text_of(positions.at[idx, "entry_date"]),
                "entry_price": round(entry_price, 4),
                "exit_date": row.get("dt_exit_date"),
                "exit_price": exit_price,
                "ret_%": ret,
            }
            if exit_price is not None:
                sells.append(record)

            if ticker not in detected:
                detected[ticker] = True
                chart = _chart(build_calc_table(panel, signals, ticker), ticker,
                               day, strategy, chart_cfg, cfg)
                if chart is not None:
                    charts.append(chart)
                embeds.append(_embed({**record,
                                      "signal_date": row[EXIT_FLAG_COL],
                                      "config_key": text_of(
                                          positions.at[idx, "config_key"]),
                                      "peak1": row["dt_peak1"],
                                      "peak2": row["dt_peak2"],
                                      "neckline": row["dt_neckline"]}, chart))

        if not updates:
            s.detail = (f"{int(held.sum())} position(s) watched, "
                        f"no double top")
            return pd.DataFrame()

        _apply(positions, updates, cfg)
        recorded = pd.DataFrame(sells)
        if sells:
            merge_history_csv(exits_csv_path(cfg), sells, ["position_id"])

        pending = len(updates) - len(sells)
        s.detail = (f"{int(held.sum())} position(s) watched, {len(updates)} "
                    f"double top(s): {len(sells)} sold, {pending} pending")

        if send and strategy.get("discord_alert", True) and embeds:
            _alert(embeds, charts, cfg)
        return recorded


def _apply(positions: pd.DataFrame, updates: dict, cfg: dict) -> None:
    """Write the flags onto the ledger, leaving every other column alone."""
    # Widen to object before assigning, for the same reason `marking.mark`
    # does: a column the ledger created as all-NA comes back from CSV as
    # float64 and pandas refuses to put an ISO date string in one.
    touched = {c for values in updates.values() for c in values}
    for column in touched:
        if column not in positions.columns:
            positions[column] = pd.NA
        positions[column] = positions[column].astype(object)
    for idx, values in updates.items():
        for column, value in values.items():
            positions.at[idx, column] = value
    positions.to_csv(positions_csv_path(cfg), index=False, encoding="utf-8")


def _alert(embeds: list[dict], charts: list, cfg: dict) -> None:
    """Post the cards. A failed send must not lose the recorded exit.

    The flags and the sell rows are already on disk by the time this runs, so
    an outage costs the notification and nothing else -- which is the right way
    round. Re-running will not re-alert, because those positions are now
    flagged.
    """
    plural = "s" if len(embeds) != 1 else ""
    content = (f"**Exit signals -- double top**\n"
               f"{len(embeds)} held position{plural} broke the neckline.")
    try:
        send_discord_alert(content, cfg.get("discord", {}), embeds, charts)
    except Exception as exc:                          # noqa: BLE001
        log_step("EXIT", "warn", f"Discord send failed: "
                 f"{type(exc).__name__}: {exc}", cfg=cfg)
