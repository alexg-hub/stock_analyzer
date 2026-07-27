"""
Shared infrastructure for all S&P 500 scanners.

Everything here is scanner-agnostic: config loading, ticker universe,
bulk/single-ticker price downloads, fundamentals, and Discord delivery.
Screen modules (breakout_scanner.py, sma_pullback.py, ...) contain only
their own condition math and formatting; run_scanners.py orchestrates.
"""

import io
import json
import math
import os
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

# The code lives flat in the project root; this is the one line to revisit if
# it ever moves into a package directory.
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"


def output_dir(create: bool = True) -> Path:
    """The single directory every *generated* artifact goes to.

    Logs, the `latest_hits.json` scan hand-off, backtest tables and charts, and
    the cached price panel -- all of it lands here, so the project root holds
    only inputs (code, config.json, docs). Config keeps storing bare filenames
    and this resolves them; an absolute path in config still wins.
    """
    out = PROJECT_ROOT / "output"
    if create:
        out.mkdir(parents=True, exist_ok=True)
    return out

# Discord hard limit is 2000 chars per message; stay under it so long
# hit lists get split across several messages instead of being rejected.
DISCORD_CHAR_LIMIT = 1900
# Discord allows at most 10 embeds and 10 attachments per webhook message,
# and at most 6000 chars across all embeds of one message.
DISCORD_MAX_EMBEDS = 10
DISCORD_EMBED_CHAR_BUDGET = 5500

# Embed side-bar color for a *partial* setup's card (a full setup gets the
# screen's own EMBED_COLOR) -- one card type, tier visible at a glance.
PARTIAL_COLOR = 0x898781

# Reserved column (added by fetch_fundamentals) holding the company name for
# the embed titles; not a config-driven fundamentals field, so it never
# renders as an inline field.
COMPANY_COL = "Company"

# Reserved columns (added by annotate_quality) holding the tier-2 quality
# verdict, mirroring the tier-1 Setup/Missing pair: the badge decision and the
# rules that failed. Like COMPANY_COL these are not config-driven fundamentals
# fields and never render as inline fields.
QUALITY_COL = "Quality"
QUALITY_MISSING_COL = "Quality Missing"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def enable_utf8_output() -> None:
    """Make stdout/stderr able to carry the text this project actually prints.

    Windows picks the locale codepage when output is redirected (cp1255 on this
    box), and the quality badge -- a real character in the Discord payload --
    has no mapping there, so a dry-run print of a passing ticker raises
    UnicodeEncodeError. That would take down `run_deepdive.bat` in exactly the
    `discord_send: false` configuration used to shake the nightly run down.
    `errors="replace"` keeps it non-fatal even if reconfigure is unavailable.

    Called from entry-point `__main__` blocks rather than at import, so
    importing this module never mutates global streams.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):   # not a reconfigurable stream
            pass


@contextmanager
def stdout_to_stderr():
    """Run a block with stdout aliased to stderr; yield the real stdout.

    For a command whose stdout *is* its output -- `research_report.py context`
    prints a JSON bundle the deep-dive skill parses. That bundle is assembled by
    calling straight through tiers 1 and 2, whose progress lines
    ("Downloading 2y of 1d data...", the unsettled-bar WARNING, the per-screen
    counts) are written to stdout because for `run_scanners.py` stdout is the
    log. Reached this way they land *in front of the JSON*, and the diagnostic
    most worth seeing -- the WARNING that a bar was dropped -- is exactly the
    one that breaks the parse.

    Redirecting here rather than converting those prints keeps the fix at the
    one boundary that knows stdout is structured, and holds for any future
    callee too. Used from `__main__` only, like `enable_utf8_output`: importing
    a module must never mutate global streams.
    """
    saved = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield saved
    finally:
        sys.stdout = saved


# --------------------------------------------------------------------------
# Step log (tier 3's record of what a deep-dive actually did)
# --------------------------------------------------------------------------
# One line per step -- timestamp, phase, status, a very short description --
# appended live to `output/logs/<run_id>.log`. Tier 3 spans three processes
# (this one, the headless `claude` run, and the `context` subprocess it
# spawns), so a run needs an id they can all agree on: RUN_ID_ENV, exported by
# the .bat and inherited straight through. Unset means "standalone" -- a
# terminal `context`/`scan` mints its own id and gets its own log rather than
# going unrecorded.
#
# Two rules this layer must never break:
#   1. It writes to **stderr**, never stdout. `research_report.py context`
#      prints its JSON bundle to stdout and the skill reads it; a diagnostic
#      landing in the middle of that JSON is exactly the bug this replaced.
#   2. It never raises. A log write failing must not take down a deep-dive, so
#      every call is wrapped -- a broken logger costs you the record, not the
#      report.

RUN_ID_ENV = "STOCK_ANALYZER_RUN_ID"

_LOG_STATE = {"cfg": None, "run_id": None}


def new_run_id() -> str:
    """A short id for one deep-dive run (8 hex chars is plenty at ~1/day)."""
    return uuid.uuid4().hex[:8]


def run_id() -> str:
    """This process's run id: the inherited one, or a freshly minted one."""
    if not _LOG_STATE["run_id"]:
        _LOG_STATE["run_id"] = os.environ.get(RUN_ID_ENV) or new_run_id()
    return _LOG_STATE["run_id"]


def configure_logging(cfg: dict | None = None, rid: str | None = None) -> None:
    """Pin the config and/or run id the step log uses (the tests redirect both)."""
    if cfg is not None:
        _LOG_STATE["cfg"] = cfg
    if rid is not None:
        _LOG_STATE["run_id"] = rid


def _log_cfg(cfg: dict | None = None) -> dict:
    if cfg is not None:
        return cfg
    if _LOG_STATE["cfg"] is None:
        try:
            _LOG_STATE["cfg"] = load_config()
        except Exception:  # noqa: BLE001 - logging must not need a readable config
            _LOG_STATE["cfg"] = {}
    return _LOG_STATE["cfg"]


def logging_cfg(cfg: dict | None = None) -> dict:
    return _log_cfg(cfg).get("research", {}).get("logging", {})


def logs_dir(cfg: dict | None = None, create: bool = True) -> Path:
    """Where the run logs live (`research.logging.dir` under output/).

    Same bare-name-resolved rule as `history_dir`: config stores a plain
    folder name, an absolute path overrides it -- which is how the tests keep
    their logs out of the real `output/`.
    """
    path = Path(logging_cfg(cfg).get("dir", "logs"))
    if not path.is_absolute():
        path = output_dir(create) / path
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def run_log_path(cfg: dict | None = None, rid: str | None = None,
                 create: bool = True) -> Path:
    return logs_dir(cfg, create) / f"{rid or run_id()}.log"


def run_result_path(cfg: dict | None = None, rid: str | None = None,
                    create: bool = True) -> Path:
    """Where the headless run's `--output-format json` blob is captured.

    It used to be appended to `deepdive_log.txt`, which buried a readable log
    under ~6 KB of JSON per run. Kept as its own file so the closing narrative
    and the usage/cost figures survive without cluttering what you read.
    """
    return logs_dir(cfg, create) / f"{rid or run_id()}_result.json"


# One row per deep-dive run. Keyed on run_id, so re-running `log-session` for
# a run updates its row instead of adding a second one.
RUN_KEYS = ["run_id"]


def manifest_csv_path(cfg: dict | None = None, create: bool = True) -> Path:
    """The run manifest -- what tier 3 did, how long it took, what it cost."""
    path = Path(logging_cfg(cfg).get("manifest", "deepdive_runs.csv"))
    return path if path.is_absolute() else logs_dir(cfg, create) / path


TS_FMT = "%Y-%m-%d %H:%M:%S"
# The date is part of every line on purpose: the nightly chain starts at 23:30
# and a deep-dive routinely crosses midnight, so a bare clock time would sort
# the run's own steps out of order when log-session merges them.


def format_step(phase: str, status: str = "ok", detail: str = "",
                ms: float | None = None, when: datetime | None = None) -> str:
    """One log line. Fixed-width columns so a run scans vertically."""
    stamp = (when or datetime.now()).strftime(TS_FMT)
    detail = " ".join(str(detail).split())          # never let a step wrap
    if ms is not None:
        detail = f"{detail}  ({ms / 1000:.1f}s)".strip()
    return f"{stamp}  {phase:<9.9s} {status:<7.7s} {detail}".rstrip()


def log_step(phase: str, status: str = "ok", detail: str = "",
             ms: float | None = None, cfg: dict | None = None,
             echo: bool = True) -> None:
    """Record one step. Silent no-op when logging is off; never raises."""
    try:
        if not logging_cfg(cfg).get("enabled", True):
            return
        line = format_step(phase, status, detail, ms)
        with open(run_log_path(cfg), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if echo:
            print(line, file=sys.stderr)
    except Exception:  # noqa: BLE001 - a lost log line must not sink the run
        pass


class _Step:
    """Handle a `step()` block fills in once it knows what happened."""

    __slots__ = ("detail", "status")

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        self.status = "ok"


@contextmanager
def step(phase: str, detail: str = "", cfg: dict | None = None):
    """Time a block and log it as one step, then **re-raise** on failure.

    Re-raising is the point: every caller in the tier-3 path already has its
    own `except Exception` doing something n/a-tolerant, and this must record
    the failure without changing that behaviour.

        with step("YAHOO") as s:
            ...
            s.detail = f"{ok}/{total} groups"
    """
    handle = _Step(detail)
    t0 = time.perf_counter()
    try:
        yield handle
    except Exception as exc:  # noqa: BLE001
        ms = (time.perf_counter() - t0) * 1000
        note = f"{handle.detail} -- {exc}" if handle.detail else str(exc)
        log_step(phase, "failed", note, ms, cfg)
        raise
    ms = (time.perf_counter() - t0) * 1000
    log_step(phase, handle.status, handle.detail, ms, cfg)


def prune_run_logs(cfg: dict | None = None) -> int:
    """Keep the newest `research.logging.keep_runs` runs, drop the rest.

    `deepdive_log.txt` grew without bound; per-run files would too. Called once
    per run from `log-session`, never on the hot path.
    """
    try:
        keep = int(logging_cfg(cfg).get("keep_runs", 200))
        if keep <= 0:
            return 0
        logs = sorted(logs_dir(cfg, create=False).glob("*.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        removed = 0
        for old in logs[keep:]:
            old.with_name(f"{old.stem}_result.json").unlink(missing_ok=True)
            old.unlink(missing_ok=True)
            removed += 1
        return removed
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------------
# Scan result contract (what every screen module's scan() returns)
# --------------------------------------------------------------------------

@dataclass
class ScanResult:
    """Output of one screen over the whole universe on the scan day.

    `hits` is a ticker-indexed DataFrame of every signal the screen fired --
    one list, with a `Setup` column of `full`/`partial` and `Missing` naming
    the failing test on a partial. `strategy` is the config section the screen
    ran with, kept here so formatting/plotting stay config-driven.
    """
    title: str
    hits: pd.DataFrame
    strategy: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------

def get_sp500_tickers(source_url: str) -> list[str]:
    """Scrape the current S&P 500 constituents from Wikipedia.

    Yahoo uses '-' where Wikipedia uses '.' in share-class tickers
    (BRK.B -> BRK-B), so normalize before downloading.
    """
    # Wikipedia rejects requests without a browser-like User-Agent.
    resp = requests.get(
        source_url,
        headers={"User-Agent": "Mozilla/5.0 (breakout-scanner)"},
        timeout=30,
    )
    resp.raise_for_status()

    tables = pd.read_html(io.StringIO(resp.text))
    constituents = next(t for t in tables if "Symbol" in t.columns)
    tickers = (
        constituents["Symbol"]
        .astype(str)
        .str.strip()
        .str.replace(".", "-", regex=False)
        .tolist()
    )
    log_step("UNIVERSE", "ok", f"{len(tickers)} S&P 500 tickers from Wikipedia")
    return tickers


def drop_unsettled_bars(data: pd.DataFrame, max_missing_pct: float = 0.5) -> pd.DataFrame:
    """Drop every bar that has no settled close -- trailing *or* interior.

    Yahoo serves an unsettled session as an ordinary daily row with Open/High/
    Low/Volume filled in but **Close null** -- and it sometimes reverts an
    already-settled bar to that form hours later (observed 2026-07-24: 503 of
    504 closes withdrawn on the Saturday after Friday's nightly run had scanned
    that same bar successfully). Every condition in every screen compares
    against Close, so such a row makes each test NaN, `fillna(False)` turns
    that into "no signal", and the scan reports a confident zero on data that
    looks complete. Scanning the last *settled* bar instead is also the right
    behaviour for an intraday run.

    **Interior bars matter as much as trailing ones, and for longer.** A
    withdrawn bar stops being the tail as soon as the next session lands on top
    of it, and every `compute_*` builds its baselines with `rolling(window)` at
    the default `min_periods=window` -- so one NaN *inside* the window makes the
    output NaN for the next `window` sessions, not just for that day. That is
    the 2026-07-27 night: Yahoo still had 2026-07-24 blank for 502 of 503
    tickers, Monday's bar sat on top of it, and the tail-only guard no longer
    applied. `prior_high` went NaN for 502 tickers, every condition followed,
    and the scan announced "nothing today" -- suppressing 6 real signals, with
    312 more sessions of the same to come before the bad bar aged out of the
    breakout window. A bar no longer being last is not a bar becoming valid.

    Dropped, not forward-filled: a session Yahoo has withdrawn is not a session,
    and inventing a flat bar there would corrupt the volume baselines and the
    candle tests instead of just shortening the window by a day.

    A fraction, not `any`: individual tickers legitimately go missing
    (delistings, per-ticker download failures) and must not discard the day.
    """
    if "Close" not in data.columns.get_level_values(0):
        return data
    missing = data["Close"].isna().mean(axis=1)   # NaN fraction per bar
    bad = missing > max_missing_pct
    if not bad.any():
        return data
    if bad.all():
        raise RuntimeError(
            f"No bar has a settled close (checked {len(data)}) -- Yahoo is "
            f"serving unsettled rows; retry later.")

    kept = data.loc[~bad]
    dropped = [str(d.date()) for d in data.index[bad]]
    # Trailing drops change *which* bar gets scanned; interior ones silently
    # poison the rolling windows. Both are worth a line, but they are different
    # failures and the log has to say which one happened.
    tail_dropped = bool(bad.iloc[-1])
    # Logged at `warn`, not printed: this is the single diagnostic most worth
    # finding after a suspicious zero-signal night, and burying it in a wall of
    # stdout is how it got missed before.
    log_step("DOWNLOAD", "warn",
             f"dropped {len(dropped)} bar(s) with no settled close for most "
             f"tickers: {', '.join(dropped)}"
             + (f"; scanning {kept.index[-1].date()} instead" if tail_dropped
                else " (interior -- would have voided every rolling window "
                     "spanning it)"))
    return kept


def download_price_data(tickers: list[str], period: str, interval: str) -> pd.DataFrame:
    """Bulk-download OHLCV for all tickers in one threaded yfinance call.

    Returns a DataFrame with a (Field, Ticker) column MultiIndex, e.g.
    data["Close"]["AAPL"] is the close series for AAPL.
    """
    t0 = time.perf_counter()
    data = yf.download(
        tickers,
        period=period,
        interval=interval,
        group_by="column",
        auto_adjust=False,
        threads=True,
        progress=sys.stdout.isatty(),  # no progress-bar spam in log files
    )
    if data.empty:
        log_step("DOWNLOAD", "failed",
                 f"{period}/{interval} for {len(tickers)} ticker(s): no data",
                 ms=(time.perf_counter() - t0) * 1000)
        raise RuntimeError("yfinance returned no data -- check connectivity.")
    # Logged after the call, so the line carries what actually came back --
    # a short panel is the tell for a screen that "never fires".
    got = len(data["Close"].columns) if "Close" in data.columns.get_level_values(0) \
        else len(tickers)
    log_step("DOWNLOAD", "ok",
             f"{period} of {interval}: {got}/{len(tickers)} ticker(s), "
             f"{len(data)} bars",
             ms=(time.perf_counter() - t0) * 1000)
    if len(tickers) == 1 and not isinstance(data.columns, pd.MultiIndex):
        # yfinance flattens the column index for a one-ticker request, and the
        # whole data contract downstream is (Field, Ticker) -- restore it, the
        # same normalization `download_history` does. Matters for the
        # on-demand single-ticker scan.
        data.columns = pd.MultiIndex.from_product([data.columns, list(tickers)])
    return drop_unsettled_bars(data)


def warmup_months(window: int) -> int:
    """Extra calendar months to download before a backtest's analysis window
    so a rolling window of `window` trading days (~21/month) is fully warmed
    up by the first analysis day."""
    return math.ceil(window / 21) + 2


def download_history(ticker: str, start: pd.Timestamp, end: pd.Timestamp,
                     window: int) -> pd.DataFrame:
    """Download one ticker's OHLCV in the same (Field, Ticker) column layout
    the scanners use, so the compute_* functions run unchanged."""
    months = warmup_months(window)
    dl_start = start - pd.DateOffset(months=months)
    log_step("DOWNLOAD", "ok",
             f"{ticker} {dl_start.date()}..{end.date()} "
             f"(+{months}mo warm-up for a {window}-day window)")
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
    return drop_unsettled_bars(data)


def single_ticker_panel(data: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Slice one ticker out of a bulk (Field, Ticker) frame, keeping the
    MultiIndex layout so the compute_* functions run unchanged."""
    return data.loc[:, pd.IndexSlice[:, [ticker]]]


# --------------------------------------------------------------------------
# Fundamentals for the hits
# --------------------------------------------------------------------------

def stmt_value(df: pd.DataFrame, row_name: str, column) -> float | None:
    """One cell of a financial statement, or None when it isn't there.

    Tolerant by design -- a missing row, a missing column and a NaN cell all
    come back as None, because Yahoo genuinely omits rows per issuer (banks
    have no Operating Income, negative-equity companies no Debt/Equity).
    Shared with `research_collect._financials`, which reads the same frames
    for the tier-3 chart.
    """
    if df.empty or row_name not in df.index or column not in df.columns:
        return None
    value = df.loc[row_name, column]
    return float(value) if pd.notna(value) else None


def _yearly_series(values: list[tuple[int, float | None]]) -> list[tuple[int, float]]:
    """Drop missing years; keep (fiscal_year, value) oldest -> newest."""
    return [(year, value) for year, value in values if value is not None]


def _statement_metrics(tk: "yf.Ticker", stmt_cfg: dict, info: dict) -> dict:
    """Compute the statement-based metrics for one ticker.

    Multi-year metrics (FCF, margins) are stored as [(fiscal_year, value)]
    lists oldest -> newest; ROE/ROIC as scalar percents. Anything Yahoo
    doesn't provide for this company simply stays None -> 'n/a'.
    """
    years = stmt_cfg.get("years", 2)
    metrics = stmt_cfg.get("metrics", {})

    def statement(name: str) -> pd.DataFrame:
        try:
            df = getattr(tk, name)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
        except Exception as exc:  # noqa: BLE001 - missing statements must not kill the alert
            log_step("YAHOO", "failed", f"{name} for {tk.ticker}: {exc}")
        return pd.DataFrame()

    income = statement("income_stmt")
    cashflow = statement("cash_flow")
    balance = statement("balance_sheet")

    # The `years` most recent annual columns, oldest -> newest.
    inc_cols = sorted(income.columns)[-years:] if not income.empty else []
    cf_cols = sorted(cashflow.columns)[-years:] if not cashflow.empty else []

    row = {}
    if "fcf" in metrics:
        row[metrics["fcf"]] = _yearly_series(
            [(c.year, stmt_value(cashflow, "Free Cash Flow", c)) for c in cf_cols]
        )
    if "operating_margin" in metrics or "profit_margin" in metrics:
        def margin(numerator_row):
            series = []
            for c in inc_cols:
                num = stmt_value(income, numerator_row, c)
                rev = stmt_value(income, "Total Revenue", c)
                series.append((c.year, 100 * num / rev if num is not None and rev else None))
            return _yearly_series(series)

        if "operating_margin" in metrics:
            row[metrics["operating_margin"]] = margin("Operating Income")
        if "profit_margin" in metrics:
            row[metrics["profit_margin"]] = margin("Net Income")

    if "roe" in metrics:
        roe = None
        for c in reversed(inc_cols):  # latest year with both rows present
            net = stmt_value(income, "Net Income", c)
            equity = stmt_value(balance, "Stockholders Equity", c)
            if net is not None and equity:
                roe = 100 * net / equity
                break
        if roe is None and isinstance(info.get("returnOnEquity"), (int, float)):
            roe = 100 * info["returnOnEquity"]
        row[metrics["roe"]] = roe

    if "roic" in metrics:
        roic = None
        for c in reversed(inc_cols):
            ebit = stmt_value(income, "EBIT", c)
            tax = stmt_value(income, "Tax Provision", c)
            pretax = stmt_value(income, "Pretax Income", c)
            invested = stmt_value(balance, "Invested Capital", c)
            if None in (ebit, tax, pretax) or not invested or pretax <= 0:
                continue
            nopat = ebit * (1 - tax / pretax)
            roic = 100 * nopat / invested
            break
        row[metrics["roic"]] = roic

    return row


def fetch_fundamentals(tickers: list[str], fund_cfg: dict) -> pd.DataFrame:
    """Pull the configured fundamentals from Yahoo for each ticker.

    Two layers, both config-driven (`fundamentals` section):
      * `fields`  -- snapshot values from `info` (fields listed in
        `percent_fields` come from Yahoo as fractions and are x100;
        note dividendYield is already a percentage);
      * `statements` -- per-year metrics computed from the annual income
        statement / cash flow / balance sheet (FCF, margins, ROE, ROIC).

    Only runs on the (small) list of signalling tickers, so a plain
    loop is fine here.
    """
    fields = fund_cfg["fields"]
    percent_fields = fund_cfg.get("percent_fields", [])
    stmt_cfg = fund_cfg.get("statements", {})

    t0 = time.perf_counter()
    rows, failed = {}, []
    for ticker in tickers:
        tk = yf.Ticker(ticker)
        try:
            info = tk.info
        except Exception as exc:  # noqa: BLE001 - a bad ticker must not kill the alert
            log_step("YAHOO", "failed", f"fundamentals for {ticker}: {exc}")
            failed.append(ticker)
            info = {}
        row = {COMPANY_COL: info.get("longName") or info.get("shortName")}
        for key, label in fields.items():
            value = info.get(key)
            if key in percent_fields and isinstance(value, (int, float)):
                value *= 100
            row[label] = value
        if stmt_cfg.get("enabled"):
            row.update(_statement_metrics(tk, stmt_cfg, info))
        rows[ticker] = row
    log_step("YAHOO", "ok" if not failed else "partial",
             f"fundamentals for {len(rows) - len(failed)}/{len(tickers)} ticker(s)"
             + (f" -- missing {', '.join(failed)}" if failed else ""),
             ms=(time.perf_counter() - t0) * 1000)
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("Ticker")


def fmt_bytes(n) -> str:
    """Compact byte count for a log line (`435 KB`, `2.1 MB`)."""
    if not isinstance(n, (int, float)):
        return "n-a"
    for unit, scale in (("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{int(n)} B"


def fmt_value(value) -> str:
    """n/a-tolerant number formatting shared by all alert sections."""
    return f"{value:.2f}" if isinstance(value, (int, float)) and pd.notna(value) else "n/a"


def fmt_compact(value) -> str:
    """19.3B / 850M style formatting for large currency amounts."""
    if not isinstance(value, (int, float)) or pd.isna(value):
        return "n/a"
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(value) >= divisor:
            return f"{value / divisor:.1f}{suffix}"
    return f"{value:,.0f}"


def _fmt_pct(value) -> str:
    return f"{value:.1f}%" if isinstance(value, (int, float)) and pd.notna(value) else "n/a"


def _fmt_yearly(series, fmt) -> str:
    """'18.1B->19.3B' for a [(fiscal_year, value)] list, oldest -> newest."""
    if not isinstance(series, list) or not series:
        return "n/a"
    return "->".join(fmt(value) for _, value in series)


def _year_span(series) -> str:
    """'FY24->FY25' label for a [(fiscal_year, value)] list."""
    if not isinstance(series, list) or not series:
        return ""
    years = [f"FY{year % 100:02d}" for year, _ in series]
    return f" ({years[0]}->{years[-1]})" if len(years) > 1 else f" ({years[0]})"


def fundamentals_fields(row, fund_cfg: dict) -> list[dict]:
    """The per-ticker fundamentals as Discord embed fields (inline, so the
    client lays them out as a 3-per-row grid -- the 'table')."""
    if not fund_cfg.get("enabled"):
        return []

    def field(name, value):
        return {"name": name, "value": value, "inline": True}

    fields = [field(label, fmt_value(row.get(label)))
              for label in fund_cfg["fields"].values()]

    stmt_cfg = fund_cfg.get("statements", {})
    metrics = stmt_cfg.get("metrics", {})
    if not stmt_cfg.get("enabled") or not metrics:
        return fields

    for key in ("roe", "roic"):
        if key in metrics:
            fields.append(field(metrics[key], _fmt_pct(row.get(metrics[key]))))
    for key in ("operating_margin", "profit_margin"):
        if key in metrics:
            series = row.get(metrics[key])
            fields.append(field(metrics[key] + _year_span(series),
                                _fmt_yearly(series, _fmt_pct)))
    if "fcf" in metrics:
        series = row.get(metrics["fcf"])
        fields.append(field(metrics["fcf"] + _year_span(series),
                            _fmt_yearly(series, fmt_compact)))
    return fields


def _rule_value(row, key: str, fund_cfg: dict):
    """Resolve a quality-rule key to the row's value.

    Rule keys are the same keys used elsewhere in the fundamentals config:
    Yahoo `info` keys for snapshot fields (trailingPE, ...) and metric keys
    for statement metrics (roe, fcf, ...), so renaming a display label
    never breaks a rule.
    """
    if key in fund_cfg.get("fields", {}):
        return row.get(fund_cfg["fields"][key])
    metrics = fund_cfg.get("statements", {}).get("metrics", {})
    if key in metrics:
        return row.get(metrics[key])
    return None


def quality_failures(row, fund_cfg: dict) -> list[str]:
    """Which quality rules (fundamentals.quality.rules) this ticker fails.

    Rule semantics: `min`/`max` are strict compares against the value
    (latest fiscal year for multi-year metrics); `increasing` requires the
    latest year above the previous one. A missing value fails its rule --
    unverifiable quality doesn't earn the badge.
    """
    failed = []
    for key, rule in fund_cfg.get("quality", {}).get("rules", {}).items():
        value = _rule_value(row, key, fund_cfg)
        series = value if isinstance(value, list) else None
        if series is not None:
            value = series[-1][1] if series else None
        ok = isinstance(value, (int, float)) and pd.notna(value)
        if ok and "min" in rule:
            ok = value > rule["min"]
        if ok and "max" in rule:
            ok = value < rule["max"]
        if ok and rule.get("increasing"):
            ok = series is not None and len(series) >= 2 and series[-1][1] > series[-2][1]
        if not ok:
            failed.append(key)
    return failed


def quality_check(row, fund_cfg: dict) -> bool:
    """True when the ticker passes every configured quality rule."""
    if not (fund_cfg.get("enabled") and fund_cfg.get("quality", {}).get("enabled")):
        return False
    return not quality_failures(row, fund_cfg)


def quality_enabled(fund_cfg: dict) -> bool:
    """Whether the tier-2 quality layer is switched on at all."""
    return bool(fund_cfg.get("enabled") and fund_cfg.get("quality", {}).get("enabled"))


def annotate_quality(hits: pd.DataFrame, fund_cfg: dict) -> pd.DataFrame:
    """Record the tier-2 verdict on a hits frame, mirroring Setup/Missing.

    Adds `Quality` (passed every rule?) and `Quality Missing` (the rule keys
    that failed, `[]` when it passed). Computing it here rather than inside
    `build_embeds` gives the badge and the research hand-off **one** source of
    truth -- they used to be a Discord-only decision that the hand-off never
    saw, so nothing downstream could tell whether a ticker earned the star.

    A no-op when the quality layer is disabled: the columns stay *absent*,
    which downstream readers must treat as "not evaluated" rather than as a
    failure. Mutates and returns `hits`.
    """
    if hits.empty or not quality_enabled(fund_cfg):
        return hits
    failures = [quality_failures(row, fund_cfg) for _, row in hits.iterrows()]
    hits[QUALITY_COL] = [not f for f in failures]
    hits[QUALITY_MISSING_COL] = pd.Series(failures, index=hits.index, dtype=object)
    return hits


def build_embeds(module, result: ScanResult, fund_cfg: dict,
                 chart_files: dict[str, Path] = {}) -> list[dict]:
    """One embed card per signal, with its chart image bound in.

    `module` supplies the screen-specific pieces of the registry contract:
    `EMBED_COLOR` and `describe_hit(row, strategy)`. A `partial` setup keeps
    the same card shape but gets the grey `PARTIAL_COLOR` side bar and its
    `Missing` text appended, so one list still shows the tier at a glance.

    The quality badge reads the verdict `annotate_quality` already recorded,
    falling back to computing it only when the column is absent -- so a card
    and the hand-off row behind it can never disagree.
    """
    badge = fund_cfg.get("quality", {}).get("badge", "")

    def passed(row) -> bool:
        value = row.get(QUALITY_COL)
        return bool(value) if value is not None else quality_check(row, fund_cfg)

    def label(ticker, row) -> str:
        """`TICKER (Company Name)` when the name is available, else the ticker."""
        name = row.get(COMPANY_COL)
        if isinstance(name, str) and name.strip():
            name = name.strip()
            if len(name) > 48:
                name = name[:47].rstrip() + "…"
            return f"{ticker} ({name})"
        return str(ticker)

    embeds = []
    for ticker, row in result.hits.iterrows():
        prefix = f"{badge} " if badge and passed(row) else ""
        partial = row.get("Setup") == "partial"
        title = f"{prefix}{label(ticker, row)} -- {result.title}"
        if partial:
            title += " (partial)"
        description = module.describe_hit(row, result.strategy)
        missing = row.get("Missing")
        if partial and isinstance(missing, str) and missing:
            description += f"\n**Missing:** {missing}"
        embed = {
            "title": title,
            "description": description,
            "color": PARTIAL_COLOR if partial else module.EMBED_COLOR,
            "fields": fundamentals_fields(row, fund_cfg),
        }
        if ticker in chart_files:
            embed["image"] = {"url": f"attachment://{chart_files[ticker].name}"}
        embeds.append(embed)
    return embeds


def _embed_size(embed: dict) -> int:
    return (len(embed.get("title", "")) + len(embed.get("description", ""))
            + sum(len(f["name"]) + len(str(f["value"]))
                  for f in embed.get("fields", [])))


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------

def send_discord_alert(content: str, discord_cfg: dict, embeds: list[dict] = (),
                       image_paths: list[Path] = ()) -> None:
    """POST the alert to a Discord webhook (free tier -- no bot needed).

    `content` is the short header/summary text; `embeds` are the per-ticker
    cards (built by build_embeds), batched to respect Discord's limits of 10
    embeds / 10 attachments / ~6000 embed chars per message. Chart files in
    `image_paths` are attached with the batch whose embed references them
    (via attachment://<filename>), so each chart renders inside its card.
    """
    url = discord_cfg["webhook_url"]
    username = discord_cfg.get("username", "Breakout Scanner")
    timeout = discord_cfg.get("request_timeout_seconds", 15)
    paths = {Path(p).name: Path(p) for p in image_paths}

    if not url or "PASTE_YOUR" in url:
        log_step("DISCORD", "dry-run",
                 f"no webhook configured -- {len(embeds)} card(s) printed instead")
        print("\nDiscord webhook URL not configured -- printing message instead:\n")
        print(content)
        for embed in embeds:
            print(f"\n[{embed['title']}]")
            print(embed.get("description", ""))
            print(" | ".join(f"{f['name']} {f['value']}"
                             for f in embed.get("fields", [])))
        return

    batches, current, size = [], [], 0
    for embed in embeds:
        embed_size = _embed_size(embed)
        if current and (len(current) >= DISCORD_MAX_EMBEDS
                        or size + embed_size > DISCORD_EMBED_CHAR_BUDGET):
            batches.append(current)
            current, size = [], 0
        current.append(embed)
        size += embed_size
    if current or not batches:
        batches.append(current)  # a content-only message when no embeds

    for i, batch in enumerate(batches):
        payload = {"username": username}
        if i == 0:
            payload["content"] = content[:DISCORD_CHAR_LIMIT]
        if batch:
            payload["embeds"] = batch

        files = {}
        for embed in batch:
            image_url = embed.get("image", {}).get("url", "")
            name = image_url.removeprefix("attachment://")
            if name != image_url and name in paths:
                files[f"files[{len(files)}]"] = (name, paths[name].read_bytes(),
                                                 "image/png")
        if files:
            resp = requests.post(url, data={"payload_json": json.dumps(payload)},
                                 files=files, timeout=timeout)
        else:
            resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
    log_step("DISCORD", "sent", f"{len(batches)} message(s), "
             f"{len(embeds)} card(s), {len(paths)} chart(s)")


# --------------------------------------------------------------------------
# Research hand-off (Stage 1 -> Stage 2)
# --------------------------------------------------------------------------
# The nightly scan writes latest_hits.json so the deep-dive -- whether the
# nightly unattended run or an on-demand interactive session -- picks up
# exactly what fired, with no Discord read-back. It carries both tiers: the
# technical Setup/Missing and the tier-2 Quality/Quality Missing verdict.

def _json_safe(obj):
    """Recursively convert pandas/numpy row values to JSON-native types.

    NaN/NaT -> None; numpy scalars -> Python scalars; Timestamps -> str;
    the fundamentals' [(year, value)] lists survive as nested arrays.
    """
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    item = getattr(obj, "item", None)  # numpy / pandas scalar
    if callable(item):
        try:
            return _json_safe(obj.item())
        except (ValueError, TypeError):
            pass
    if isinstance(obj, pd.Timestamp):
        return str(obj)
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    return str(obj)


def build_hits_payload(scan_date, results) -> dict:
    """The tier-1/2 hand-off structure, without writing it anywhere.

    `results` is the list of (module, ScanResult) the nightly run already
    holds. Rows are whatever each screen put in its hits frame (day stats,
    the Setup/Missing tier columns, the Quality/Quality Missing tier-2
    verdict, and any joined fundamentals), made JSON-safe.

    Split out of `write_latest_hits` so an *on-demand* single-ticker scan
    (`run_scanners.scan_ticker`) can produce the identical shape without
    overwriting the nightly hand-off. Everything downstream -- `find_ticker`,
    the tier-2 re-grade, `_facts` -- then consumes both without knowing which
    it got.
    """
    payload = {
        "scan_date": str(scan_date),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "screens": [],
    }
    for module, result in results:
        payload["screens"].append({
            "config_key": module.CONFIG_KEY,
            "title": result.title,
            "strategy": _json_safe(result.strategy),
            "hits": {str(t): _json_safe(row.to_dict())
                     for t, row in result.hits.iterrows()},
        })
    return payload


def write_latest_hits(path: Path, scan_date, results) -> dict:
    """Write the hand-off to `path` for the deep-dive.

    Returns the payload so `archive_scan` can keep a dated copy without
    re-serializing it.
    """
    payload = build_hits_payload(scan_date, results)
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                          encoding="utf-8")
    n = sum(len(s["hits"]) for s in payload["screens"])
    log_step("HANDOFF", "ok", f"{Path(path).name}: {n} row(s) across "
             f"{len(payload['screens'])} screen(s)")
    return payload


# --------------------------------------------------------------------------
# Signal history (the long-term record of tiers 1 + 2)
# --------------------------------------------------------------------------
# `latest_hits.json` is overwritten nightly, so without this every scan's
# output is gone within a day and there is nothing to study later. Two forms,
# because they answer different questions: a dated JSON snapshot preserves the
# hand-off exactly as the deep-dive saw it, while one flat CSV is what you
# actually load into pandas to join signals against forward returns.
#
# Two CSVs, split by provenance rather than by content: `signals.csv` holds
# what the nightly screens fired on, `on_demand_scans_results.csv` holds
# tickers you asked about yourself. An ad-hoc look has no signal row to attach
# to, so folding the two together would silently drop it.

HISTORY_KEYS = ["scan_date", "config_key", "ticker"]

# A verdict is about a *ticker on a date*, so the on-demand table -- one row
# per ad-hoc look, not per screen -- keys on the pair.
ON_DEMAND_KEYS = ["scan_date", "ticker"]

# Tier 3's judgment, written into a row tier 1 created hours earlier. Every
# rewrite of a history table has to carry these forward; see merge_history_csv.
VERDICT_COL = "Verdict"
CONVICTION_COL = "Conviction"
VERDICT_COLS = [VERDICT_COL, CONVICTION_COL]


def history_dir(cfg: dict, create: bool = True) -> Path:
    """Where the signal history lives (`research.history.dir` under output/)."""
    hist_cfg = cfg.get("research", {}).get("history", {})
    path = Path(hist_cfg.get("dir", "history"))
    if not path.is_absolute():
        path = output_dir(create) / path
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _history_csv(cfg: dict, key: str, default: str, create: bool = True) -> Path:
    """One of the history tables, `research.history.<key>` under `history_dir`.

    Config stores a bare filename that resolves inside `output/`; an absolute
    path still overrides it, which is how the tests redirect these files.
    """
    hist_cfg = cfg.get("research", {}).get("history", {})
    path = Path(hist_cfg.get(key, default))
    return path if path.is_absolute() else history_dir(cfg, create) / path


def signals_csv_path(cfg: dict, create: bool = True) -> Path:
    """The signal history table -- one row per (scan_date, screen, ticker)."""
    return _history_csv(cfg, "csv", "signals.csv", create)


def on_demand_csv_path(cfg: dict, create: bool = True) -> Path:
    """The on-demand scan table -- one row per (scan_date, ticker)."""
    return _history_csv(cfg, "on_demand_csv", "on_demand_scans_results.csv",
                        create)


# -- Tier 4: the virtual portfolio -----------------------------------------
#
# Its own directory rather than a fourth file in `history/`, because the
# ledger is a *derived* table with a different lifecycle: history records what
# the scan saw and is never revised, while a position is rewritten on every
# mark. Same config convention as everything else -- bare filenames that
# `output_dir()` resolves, absolute paths still overriding (how the tests
# redirect them).

def portfolio_dir(cfg: dict, create: bool = True) -> Path:
    """Where the virtual portfolio lives (`portfolio.dir` under output/)."""
    path = Path(cfg.get("portfolio", {}).get("dir", "portfolio"))
    if not path.is_absolute():
        path = output_dir(create) / path
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _portfolio_csv(cfg: dict, key: str, default: str, create: bool = True) -> Path:
    port_cfg = cfg.get("portfolio", {})
    path = Path(port_cfg.get(key, default))
    return path if path.is_absolute() else portfolio_dir(cfg, create) / path


def positions_csv_path(cfg: dict, create: bool = True) -> Path:
    """The ledger -- one row per virtually bought (signal_date, ticker, screen)."""
    return _portfolio_csv(cfg, "positions_csv", "positions.csv", create)


def findings_csv_path(cfg: dict, create: bool = True) -> Path:
    """The attribution report the analysis writes."""
    return _portfolio_csv(cfg, "findings_csv", "findings.csv", create)


def exits_csv_path(cfg: dict, create: bool = True) -> Path:
    """The sell record -- one row per position an exit rule closed.

    Deliberately its own narrow table rather than more columns on the ledger:
    it exists to grade the *exit* rule, so it carries only what a sell is
    (which name, in at what, out at what, when) and joins back to everything
    else on `position_id`.
    """
    return _portfolio_csv(cfg, "exits_csv", "exits.csv", create)


def history_rows(payload: dict) -> list[dict]:
    """Flatten one scan payload to one row per (scan_date, screen, ticker).

    Public because the on-demand table is built from it too: sharing the
    flattening is what keeps both CSVs carrying identical, config-driven
    fundamentals labels, so the two are directly comparable.
    """
    rows = []
    for screen in payload.get("screens", []):
        for ticker, hit in screen.get("hits", {}).items():
            row = {
                "scan_date": payload.get("scan_date"),
                "generated_at": payload.get("generated_at"),
                "config_key": screen.get("config_key"),
                "screen": screen.get("title"),
                "ticker": ticker,
            }
            for key, value in hit.items():
                # Lists (the [year, value] series, the failed-rule keys) have
                # to survive a CSV round trip -- JSON keeps them re-readable.
                row[key] = json.dumps(value) if isinstance(value, list) else value
            rows.append(row)
    return rows


def merge_history_csv(path: Path, rows: list[dict], keys: list[str],
                      protect: list[str] | None = None) -> pd.DataFrame:
    """Merge `rows` into a history table: read, concat, de-duplicate, rewrite.

    Rewriting rather than appending, for two reasons: the fundamentals columns
    are *config-driven display labels*, so retuning the config changes the
    schema and a blind append would misalign every later row; and
    de-duplicating on `keys` makes a same-day re-run idempotent instead of
    double-counting it. At a handful of rows a night the full rewrite stays
    trivially cheap for years.

    `protect` names columns only a *later* writer fills in -- the tier-3
    verdict, recorded hours after tier 1 created the row. Incoming rows never
    carry them, so they are inherited from the row already on file. Without
    that, re-running a scan for a date it already covered would silently erase
    the verdict recorded against it, with no error to explain the loss.
    """
    frame = pd.DataFrame(rows)
    if path.exists():
        previous = pd.read_csv(path, dtype={"scan_date": str})
        carry = [c for c in (protect or []) if c in previous.columns]
        if carry and not frame.empty:
            frame = frame.drop(columns=carry, errors="ignore").merge(
                previous[keys + carry].drop_duplicates(subset=keys, keep="last"),
                on=keys, how="left")
        frame = pd.concat([previous, frame], ignore_index=True)
    if not frame.empty:
        frame = frame.drop_duplicates(subset=keys, keep="last")
        frame = frame.sort_values(keys, kind="stable")
    frame.to_csv(path, index=False, encoding="utf-8")
    return frame


def _row_mask(frame: pd.DataFrame, match: dict):
    """Boolean mask of rows whose `match` columns all equal the wanted values.

    Compared as strings: these tables round-trip through CSV, so a scan_date is
    text on the way back in and a conviction may be int or float.
    """
    mask = pd.Series(True, index=frame.index)
    for column, wanted in match.items():
        if column not in frame.columns:
            return None
        mask &= frame[column].astype(str) == str(wanted)
    return mask


def count_csv_rows(path: Path, match: dict) -> int:
    """How many rows of `path` match -- 0 for a missing file or column.

    Read-only, so callers can ask "is this ticker already recorded elsewhere?"
    without rewriting anything.
    """
    if not path.exists():
        return 0
    frame = pd.read_csv(path, dtype={"scan_date": str})
    if frame.empty:
        return 0
    mask = _row_mask(frame, match)
    return 0 if mask is None else int(mask.sum())


def update_csv_rows(path: Path, match: dict, values: dict) -> int:
    """Set `values` on every row of `path` matching `match`; return the count.

    How the tier-3 verdict reaches a row tier 1 wrote hours earlier. A ticker
    that fired on two screens has two rows and *both* get it -- the verdict is
    about the ticker that night, not about one screen's view of it. (Which is
    why an analysis that groups by verdict must de-duplicate on
    (scan_date, ticker) first, or those names count twice.)

    Returns 0 -- no write, no error -- when the file or the matching row is
    absent: that is an on-demand ticker, which the caller routes to the
    on-demand table instead.
    """
    if not path.exists():
        return 0
    frame = pd.read_csv(path, dtype={"scan_date": str})
    if frame.empty:
        return 0
    mask = _row_mask(frame, match)
    if mask is None:
        return 0
    count = int(mask.sum())
    if not count:
        return 0
    for column, value in values.items():
        if column not in frame.columns:
            frame[column] = pd.NA
        frame.loc[mask, column] = value
    frame.to_csv(path, index=False, encoding="utf-8")
    return count


def archive_scan(payload: dict, cfg: dict) -> None:
    """Append this scan to the permanent record under `output/history/`.

    Writes `hits_<scan_date>.json` (the payload verbatim) and merges the
    flattened rows into `signals.csv`, preserving any tier-3 verdict already
    recorded against those rows.
    """
    hist_cfg = cfg.get("research", {}).get("history", {})
    if not hist_cfg.get("enabled", True):
        log_step("ARCHIVE", "skip", "research.history.enabled is false", cfg=cfg)
        return
    out = history_dir(cfg)

    scan_date = payload.get("scan_date", "unknown")
    snapshot = out / f"hits_{scan_date}.json"
    snapshot.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")

    rows = history_rows(payload)
    csv_path = signals_csv_path(cfg)
    frame = merge_history_csv(csv_path, rows, HISTORY_KEYS, protect=VERDICT_COLS)
    log_step("ARCHIVE", "ok", f"{len(rows)} row(s) -> {snapshot.name}; "
             f"{csv_path.name} now {len(frame)} row(s)", cfg=cfg)
