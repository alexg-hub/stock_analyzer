"""
Shared infrastructure for all index scanners.

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
from zoneinfo import ZoneInfo

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

# The framing line that must appear on every notification and every report:
# the verdict is an analytical rating, never a buy/sell instruction.
DISCLAIMER = "_Research analysis, not investment advice._"

# Embed side-bar color for a *partial* setup's card (a full setup gets the
# screen's own EMBED_COLOR) -- one card type, tier visible at a glance.
PARTIAL_COLOR = 0x898781

# Side bar for an excluded signal. Deliberately the same red `exits.py` uses:
# both mean "this is the sell side of the ledger", and one colour for one idea
# is easier to read at a glance than a new hue per module.
VETO_COLOR = 0xC0392B

# Reserved column (added by quality.fetch_fast) holding the company name for
# the embed titles; not a registry parameter, so it never renders as an inline
# field.
COMPANY_COL = "Company"

# Reserved columns (added by quality.annotate) holding the tier-2 verdict,
# mirroring the tier-1 Setup/Missing pair: the badge decision and the parameter
# keys whose gate failed. Like COMPANY_COL these are not registry parameters
# and never render as inline fields. They live here rather than in quality.py
# because ledger.py, marking.py and research_report.py all read them without
# needing the engine.
QUALITY_COL = "Quality"
QUALITY_MISSING_COL = "Quality Missing"

# Which index the ticker was a member of when it signalled, written by the scan
# from `universe_constituents`. Recorded for the same reason `Setup` is: S&P
# rewrites index membership at every rebalance, so it cannot be reconstructed
# later, and it is what makes "did the mid-caps pay?" a question tier 4 can
# answer instead of a decision nobody revisits. Written by the scan, so like
# `VETO_COLS` it stays *out* of `merge_history_csv`'s `protect`.
INDEX_COL = "Index"

# The exclusion verdict, written by the same `quality.annotate` pass. Separate
# from the badge on purpose: the badge asks "is this a good company" and most
# tickers fail at least one of its gates, whereas a veto asks the much narrower
# "is this one visibly falling over" and is meant to fire rarely. Folding the
# two together would make every ordinary name look like a disaster.
VETO_COL = "Veto"
VETO_REASONS_COL = "Veto Reasons"
VETO_COLS = [VETO_COL, VETO_REASONS_COL]

# The `deep`-stage half of the same verdict, written hours later by tier 3 (SEC
# filing flags, Beneish, the liquidity pair). It needs its own columns rather
# than extending the pair above precisely *because* of when it is written:
# `merge_history_csv`'s `protect` inherits the recorded value and drops the
# incoming one, which is right for a column only tier 3 fills in and wrong for
# one the scan itself just computed.
DEEP_VETO_COL = "Deep Veto"
DEEP_VETO_REASONS_COL = "Deep Veto Reasons"
DEEP_VETO_COLS = [DEEP_VETO_COL, DEEP_VETO_REASONS_COL]

# The risk/reward coordinates and the quadrant they fall in, written by the same
# `quality.annotate` pass. These are the **fast**-stage axis, which is what makes
# a signal's position comparable to `universe_scan`'s plane -- the deep-stage
# reading adds the SEC filing flags and so sits on a different scale. Like
# `VETO_COLS` and unlike `DEEP_VETO_COLS` they are written by the scan itself, so
# they must stay *out* of `merge_history_csv`'s `protect`: the incoming value is
# the fresh one.
REWARD_COL = "Reward"
RISK_COL = "Risk"
QUADRANT_COL = "Quadrant"
AXIS_COLS = [REWARD_COL, RISK_COL, QUADRANT_COL]


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
    UnicodeEncodeError. That took down the nightly chain in exactly the
    `discord_send: false` configuration used to shake it down.
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
    prints a JSON bundle the `enrich` skill parses. That bundle is assembled by
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
# Step log (every tier's record of what a run actually did)
# --------------------------------------------------------------------------
# One line per step -- timestamp, phase, status, a very short description --
# appended live to `output/logs/<run_id>.log`. The nightly run is one process
# now, but anything it starts still needs an id they agree on: RUN_ID_ENV,
# exported by the .bat and inherited straight through. Unset means "standalone"
# -- a terminal `context`/`scan`/`enrichment` call mints its own id and gets its
# own log rather than going unrecorded.
#
# Two rules this layer must never break:
#   1. It writes to **stderr**, never stdout. `research_report.py context`
#      prints its JSON bundle to stdout and a session reads it; a diagnostic
#      landing in the middle of that JSON is exactly the bug this replaced.
#   2. It never raises. A log write failing must not take down a run, so every
#      call is wrapped -- a broken logger costs you the record, not the report.

RUN_ID_ENV = "STOCK_ANALYZER_RUN_ID"

_LOG_STATE = {"cfg": None, "run_id": None}


def new_run_id() -> str:
    """A short id for one run (8 hex chars is plenty at ~1/day)."""
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


TS_FMT = "%Y-%m-%d %H:%M:%S"
# The date is part of every line on purpose: the nightly run starts at 23:30 and
# can cross midnight, so a bare clock time would sort a run's own steps out of
# order.


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
    per run, at the *start* of the nightly scan rather than at the end: the scan
    has several exit paths (a quiet night returns early) and an exception has
    one more, so pruning first is the only placement that runs unconditionally.

    It used to be called from `log-session`, i.e. from the optional narrative
    pass -- which meant the one job responsible for bounding this directory was
    the one job allowed not to run at all.
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

#: What `sp500_constituents` renames Wikipedia's columns to. The table's own
#: headings have moved before ("GICS Sector" has also appeared as "GICS
#: sector"), so the lookup is case-insensitive and a heading that has vanished
#: yields an empty column rather than a KeyError -- sector is a nice-to-have for
#: peer grouping, never a reason to fail the universe.
_CONSTITUENT_COLS = {"symbol": "ticker",
                     "gics sector": "sector",
                     "gics sub-industry": "sub_industry"}


def _index_table(source_url: str) -> pd.DataFrame:
    """One index's constituent table from Wikipedia: ticker, sector,
    sub_industry.

    The single place that knows the shape of that table -- and one shape covers
    every S&P index, because the S&P 500, 400 and 600 list pages all publish the
    same `Symbol` / `GICS Sector` / `GICS Sub-Industry` headings. That is the
    whole reason adding the MidCap 400 needed no second parser.

    Sector matters because every threshold in the `quality` registry is an
    *absolute* anchor, and several of them are only meaningful relative to a
    peer group -- Altman Z and interest coverage are structurally low for banks
    and anything with a captive finance arm, and a current ratio of 1.2 is
    prudent for Microsoft and alarming for a miner. Sector is what a
    peer-relative percentile would group by. The column was being parsed and
    thrown away until 2026-08-10.
    """
    # Wikipedia rejects requests without a browser-like User-Agent.
    resp = requests.get(
        source_url,
        headers={"User-Agent": "Mozilla/5.0 (breakout-scanner)"},
        timeout=30,
    )
    resp.raise_for_status()

    tables = pd.read_html(io.StringIO(resp.text))
    raw = next(t for t in tables if "Symbol" in t.columns)
    by_lower = {str(c).strip().lower(): c for c in raw.columns}

    out = pd.DataFrame()
    for heading, name in _CONSTITUENT_COLS.items():
        source = by_lower.get(heading)
        out[name] = (raw[source].astype(str).str.strip() if source is not None
                     else pd.Series([""] * len(raw), index=raw.index))
    # Yahoo uses '-' where Wikipedia uses '.' in share-class tickers
    # (BRK.B -> BRK-B), so normalize before anything downloads.
    out["ticker"] = out["ticker"].str.replace(".", "-", regex=False)
    return out


def universe_sources(cfg: dict) -> list[dict]:
    """The configured index sources, normalized.

    `data.universe_sources` is a list of `{name, label, url, alert}`; `label`
    defaults to `name` and is what user-facing text calls the index. The older
    single-index `data.sp500_source_url` is still honoured as a one-source
    universe, so an archived or hand-edited config keeps working unchanged.
    """
    data = cfg.get("data", {})
    sources = data.get("universe_sources")
    if not sources:
        url = data.get("sp500_source_url")
        sources = [{"name": "sp500", "label": "S&P 500", "url": url,
                    "alert": True}] if url else []
    out = []
    for i, src in enumerate(sources):
        if not src.get("url"):
            continue
        name = str(src.get("name") or f"source{i}")
        out.append({"name": name,
                    "label": str(src.get("label") or name),
                    "url": src["url"],
                    "alert": bool(src.get("alert", True))})
    return out


def universe_label(cfg: dict, alert_only: bool = True) -> str:
    """What to call the scanned universe in user-facing text.

    Config-driven for the same reason every threshold and chart label here is:
    the alert header said "S&P 500 Scan" as a literal, which stopped being true
    the moment a second index was configured, and nothing would have flagged
    it. `alert_only` because the header names what the message covers.
    """
    labels = [s["label"] for s in universe_sources(cfg)
              if s["alert"] or not alert_only]
    return " + ".join(labels) if labels else "Universe"


def universe_constituents(cfg: dict, alert_only: bool = False) -> pd.DataFrame:
    """Every configured index as one frame: ticker, sector, sub_industry,
    index_name, alert.

    Replaced the single-URL `sp500_constituents` when the MidCap 400 was added
    on 2026-08-13. Two columns carry what the concatenation would otherwise
    lose:

    * **`index_name`** -- which index the row came from. Named that rather than
      `index` because a column called `index` collides with `DataFrame.index`
      in every `.itertuples()` and `.to_dict()` that touches the frame. It is
      recorded on the signal row for the same reason `Setup` is: membership at
      signal time is not recoverable afterwards, and it is the only thing that
      makes "did mid-cap signals pay?" a question tier 4 can answer.
    * **`alert`** -- whether this source reaches the nightly Discord alert.
      Exactly the semantics `<screen>.enabled` already has: it gates the alert
      and nothing else, so `universe_scan.py`, `peers.py` and
      `backtest_universe.py` all keep grading a source the alert ignores. A
      universe is added precisely when nobody knows yet whether it pays, which
      is when it most needs measuring.

    **Fail-open, per source.** A 404, a moved table shape or an unreachable
    Wikipedia costs that one index and logs `UNIVERSE warn`; whatever parsed
    still runs. Losing the mid-caps must never cost the S&P 500 scan. With no
    source left standing the caller gets an empty frame and the download that
    follows raises -- which is the honest outcome, and loud.
    """
    frames = []
    for src in universe_sources(cfg):
        try:
            table = _index_table(src["url"])
        except Exception as exc:  # noqa: BLE001 - one index must not sink the rest
            log_step("UNIVERSE", "warn",
                     f"{src['name']}: {exc} -- skipped, continuing without it")
            continue
        table["index_name"] = src["name"]
        table["alert"] = src["alert"]
        missing = int((table["sector"] == "").sum())
        log_step("UNIVERSE", "ok" if not missing else "partial",
                 f"{src['name']}: {len(table)} constituents from Wikipedia"
                 + (f" -- {missing} without a sector" if missing else "")
                 + ("" if src["alert"] else " (not alerted)"))
        frames.append(table)

    if not frames:
        log_step("UNIVERSE", "failed", "no index source could be read")
        return pd.DataFrame(columns=["ticker", "sector", "sub_industry",
                                     "index_name", "alert"])

    out = pd.concat(frames, ignore_index=True)
    # First source wins. The S&P indices are mutually exclusive by construction
    # so this drops nothing today, but S&P moves names between them at
    # rebalance, and a duplicate would be downloaded twice, screened twice and
    # counted twice in its sector's peer distribution.
    before = len(out)
    out = out.drop_duplicates(subset="ticker", keep="first").reset_index(drop=True)
    if len(out) != before:
        log_step("UNIVERSE", "warn",
                 f"{before - len(out)} ticker(s) listed by more than one index "
                 f"-- kept the first")

    if len(frames) > 1:
        log_step("UNIVERSE", "ok",
                 f"{len(out)} constituents across {len(frames)} index/indices "
                 f"({int(out['alert'].sum())} alerted)")
    if alert_only:
        out = out[out["alert"]].reset_index(drop=True)
    return out


def universe_tickers(cfg: dict, alert_only: bool = False) -> list[str]:
    """Just the tickers, for the callers that only ever wanted a list.

    `alert_only` is the nightly scan's gate: it drops every source whose
    `alert` is false. Everything that *measures* rather than notifies -- the
    universe plane, the peer statistics, the profit backtest -- deliberately
    leaves it False and grades the whole universe.
    """
    return universe_constituents(cfg, alert_only=alert_only)["ticker"].tolist()


# The US equity session, in exchange-local time. Named constants because the
# rule below is about a *session*, not about a clock reading on this box --
# the scanner runs in Israel, seven hours ahead, which is exactly how an
# in-progress bar came to be scanned as a finished one.
MARKET_TZ = "America/New_York"
MARKET_CLOSE_HOUR = 16


def drop_open_session_bar(data: pd.DataFrame,
                          now: datetime | None = None) -> pd.DataFrame:
    """Drop a trailing bar for a session that has not closed yet.

    The complement of the null-Close guard below, and the case it cannot see.
    Yahoo publishes the *live* session as an ordinary daily row whose Close is
    the last trade price -- a real number, not a NaN -- so `drop_unsettled_bars`
    finds nothing missing and keeps it. Every screen then grades an hour of
    trading as if it were a day.

    Observed 2026-08-14: a scan run at 17:38 Israel time (10:38 ET, ~1h after
    the open) pulled a 502nd bar for that date carrying **5-25% of a normal
    day's volume**. Breakout C3 compares volume against its 30-day average, so
    `is_volume_surge` was true for **0 of 903 tickers** -- an impossible reading
    on settled data -- and the scan reported a confident "nothing today" while
    the same code on the previous settled bar fired two signals. Worse than a
    lost alert: the trend screen *did* fire on that partial bar, and the signal
    was written to `signals.csv` and bought by tier 4, where nothing later
    removes it.

    Two distinct states, and only one of them is a drop:

    * **Session still open** -- the bar is not a bar. Dropped, and the scan
      grades the last settled session instead, exactly as it does for a
      withdrawn close.
    * **Session closed earlier today** -- kept, but warned. Yahoo's volume has
      not necessarily absorbed the closing auction and the late prints: measured
      over the 29 breakout rows in `signals.csv`, `Close` matched a later re-read
      29/29 while `Vol Ratio` was understated in **20/29, mean 12.6%, max 58%**,
      split purely by run time, with every next-morning run matching to ±0.2%.
      That is a calibration hazard rather than a wrong bar, so it is reported and
      not acted on -- the same split `warn_ticker_holes` draws.

    `now` is injectable so the tests can pin both sides without waiting for a
    market session.

    **Half-day caveat:** the close is a fixed 16:00 ET, so on the ~3 early-close
    sessions a year (13:00 ET) a run between 13:00 and 16:00 drops a bar that
    really did finish. That is the conservative direction -- it scans the prior
    settled session and says so -- and it is why this keys off the session
    rather than trying to infer completeness from the volume itself.
    """
    if data.empty or not isinstance(data.index, pd.DatetimeIndex):
        return data
    now_et = now or datetime.now(ZoneInfo(MARKET_TZ))
    last = data.index[-1]
    if last.date() != now_et.date():
        return data                     # not today's bar; nothing is in flight
    if now_et.hour >= MARKET_CLOSE_HOUR:
        log_step("DOWNLOAD", "warn",
                 f"grading {last.date()} on the same day it closed -- Yahoo's "
                 f"volume may not have absorbed the closing auction "
                 f"(measured: understated in 20/29 rows, mean 12.6%); a "
                 f"next-morning re-run is the settled read")
        return data
    if len(data) == 1:
        raise RuntimeError(
            f"The only bar available ({last.date()}) is the session still in "
            f"progress -- nothing settled to scan; retry after 16:00 ET.")
    kept = data.iloc[:-1]
    log_step("DOWNLOAD", "warn",
             f"dropped the trailing {last.date()} bar -- the US session is "
             f"still open ({now_et:%H:%M} ET, closes {MARKET_CLOSE_HOUR}:00), "
             f"so its volume is a fraction of a day; scanning "
             f"{kept.index[-1].date()} instead")
    return kept


def drop_unsettled_bars(data: pd.DataFrame, max_missing_pct: float = 0.5,
                        now: datetime | None = None) -> pd.DataFrame:
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
    Those sub-threshold gaps are not harmless, though -- they just have to be
    reported rather than acted on, which is `warn_ticker_holes` below.
    """
    if "Close" not in data.columns.get_level_values(0):
        return data
    # First, because a live bar carries a real Close and so is invisible to the
    # nullity test below -- it would survive every check and be scanned.
    data = drop_open_session_bar(data, now=now)
    missing = data["Close"].isna().mean(axis=1)   # NaN fraction per bar
    bad = missing > max_missing_pct
    if bad.all() and len(data):
        raise RuntimeError(
            f"No bar has a settled close (checked {len(data)}) -- Yahoo is "
            f"serving unsettled rows; retry later.")

    kept = data
    if bad.any():
        kept = data.loc[~bad]
        dropped = [str(d.date()) for d in data.index[bad]]
        # Trailing drops change *which* bar gets scanned; interior ones silently
        # poison the rolling windows. Both are worth a line, but they are
        # different failures and the log has to say which one happened.
        tail_dropped = bool(bad.iloc[-1])
        # Logged at `warn`, not printed: this is the single diagnostic most
        # worth finding after a suspicious zero-signal night, and burying it in
        # a wall of stdout is how it got missed before.
        log_step("DOWNLOAD", "warn",
                 f"dropped {len(dropped)} bar(s) with no settled close for most "
                 f"tickers: {', '.join(dropped)}"
                 + (f"; scanning {kept.index[-1].date()} instead" if tail_dropped
                    else " (interior -- would have voided every rolling window "
                         "spanning it)"))
    warn_ticker_holes(kept)
    return kept


def warn_ticker_holes(data: pd.DataFrame, max_named: int = 12) -> list[str]:
    """Log the tickers carrying an *interior* Close hole. Returns their names.

    The complement of the guard above, and the case it deliberately does not
    act on. `drop_unsettled_bars` discards a bar only when most of the index has
    no settled close, because a handful of missing tickers must not cost the day
    for the other 490 -- but the handful is not fine, it is simply a per-ticker
    failure rather than a per-bar one. Every `compute_*` builds its baselines
    with `rolling(window)` at the default `min_periods=window`, so one NaN
    inside the window voids that **ticker's** output for the next `window`
    sessions exactly as an index-wide blank voids everyone's.

    Measured on 2026-08-11: Yahoo had no bar at all for 28 constituents (ABBV,
    CARR, PSX, HLT, ...), 5.6% of the index and so nowhere near the 50%
    threshold. Nothing dropped, nothing warned, and those 28 were silently
    unable to fire the breakout screen for the next 312 sessions -- about
    fifteen months. A re-download does not fix it (the bar is absent upstream,
    not lost in the batch), so the only available remedy is knowing.

    **Interior only.** A leading run of NaNs is a young listing and a trailing
    one is a delisting or an unsettled tail; both are legitimate and neither
    poisons a window that any later bar depends on. Only a gap with settled
    closes on *both* sides is a hole. Measured over a normal 2y/503 panel that
    is ~30 tickers, essentially all of them the real defect -- so this stays a
    warning worth reading rather than noise.
    """
    if "Close" not in data.columns.get_level_values(0) or data.empty:
        return []
    ok = data["Close"].notna()
    # Vectorized "has a settled close somewhere before / after this bar": a hole
    # needs both, which is what excludes the leading and trailing runs.
    interior = (ok.cumsum() > 0) & (ok[::-1].cumsum()[::-1] > 0) & ~ok
    per_ticker = interior.sum()
    affected = per_ticker[per_ticker > 0]
    if affected.empty:
        return []

    names = sorted(affected.index)
    # Newest hole across all of them, and how far back it sits -- a hole one bar
    # back is tonight's problem, one 400 bars back may already have aged out of
    # every window. Position 0 can never be interior, so 0 is safe as "none".
    pos = pd.Series(range(len(data)), index=data.index)
    newest = int(interior.mul(pos, axis=0).max().max())
    shown = ", ".join(names[:max_named])
    more = f" (+{len(names) - max_named} more)" if len(names) > max_named else ""
    log_step("DOWNLOAD", "warn",
             f"{len(names)} ticker(s) carry an interior Close hole -- every "
             f"rolling window spanning it is void for that ticker: {shown}{more}"
             f"; newest {data.index[newest].date()}, "
             f"{len(data) - 1 - newest} bar(s) back")
    return names


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
# Statement access + formatting
# --------------------------------------------------------------------------
# The fundamentals themselves -- which values are collected, how they are
# gated, scored and displayed -- live in `quality.py`. What stays here is the
# n/a-tolerant plumbing every layer shares.

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




def build_embeds(module, result: ScanResult, cfg: dict,
                 chart_files: dict[str, Path] = {}) -> list[dict]:
    """One embed card per signal, with its chart image bound in.

    `module` supplies the screen-specific pieces of the registry contract:
    `EMBED_COLOR` and `describe_hit(row, strategy)`. A `partial` setup keeps
    the same card shape but gets the grey `PARTIAL_COLOR` side bar and its
    `Missing` text appended, so one list still shows the tier at a glance.

    The quality badge reads the verdict `quality.annotate` already recorded,
    falling back to re-grading only when the column is absent -- so a card and
    the hand-off row behind it can never disagree.

    A **vetoed** signal keeps its card rather than being dropped. Two reasons:
    the exclusion is the most interesting thing the scan found about that
    ticker, and tier 4 goes on tracking it, so silently hiding it here would
    leave the ledger recording positions the alert never mentioned. It gets the
    veto badge, the muted side bar, and a field naming the rules it tripped --
    and it sorts **after** the clean signals, so the eye lands on what survived.

    `quality` is imported here rather than at module scope so the dependency
    stays one-way: `quality.py` needs this module's logging, statement access
    and formatting, and nothing here needs the registry except this one call.
    """
    import quality

    badge = quality.badge(cfg)
    veto_badge_str = quality.veto_badge(cfg)

    def passed(row) -> bool:
        value = row.get(QUALITY_COL)
        if value is not None and not (isinstance(value, float) and pd.isna(value)):
            return bool(value)
        return bool(quality.verdict_of(row, cfg)[0])

    def veto_of(row) -> list:
        """The recorded veto reasons, re-graded only when nothing was recorded."""
        recorded = row.get(VETO_REASONS_COL)
        if isinstance(recorded, list):
            return recorded
        return quality.veto_of(row, cfg)[1]

    def label(ticker, row) -> str:
        """`TICKER (Company Name)` when the name is available, else the ticker."""
        name = row.get(COMPANY_COL)
        if isinstance(name, str) and name.strip():
            name = name.strip()
            if len(name) > 48:
                name = name[:47].rstrip() + "…"
            return f"{ticker} ({name})"
        return str(ticker)

    clean, excluded = [], []
    for ticker, row in result.hits.iterrows():
        vetoes = veto_of(row)
        prefix = f"{veto_badge_str} " if vetoes and veto_badge_str \
            else (f"{badge} " if badge and passed(row) else "")
        partial = row.get("Setup") == "partial"
        title = f"{prefix}{label(ticker, row)} -- {result.title}"
        if partial:
            title += " (partial)"
        description = module.describe_hit(row, result.strategy)
        missing = row.get("Missing")
        if partial and isinstance(missing, str) and missing:
            description += f"\n**Missing:** {missing}"
        # Where this signal sits on the risk/reward plane, from the columns
        # `quality.annotate` recorded -- so the card and the plane cannot
        # disagree, and a night's signal can be read against the whole index
        # rather than only against the other signals. Absent columns mean the
        # layer did not run, and print nothing at all.
        plane = _plane_line(row, quality)
        if plane:
            description += f"\n{plane}"
        fields = quality.embed_fields(row, cfg)
        if vetoes:
            fields.insert(0, {"name": "Veto",
                              "value": quality.veto_text(vetoes, cfg),
                              "inline": False})
        embed = {
            "title": title,
            "description": description,
            "color": VETO_COLOR if vetoes
                     else (PARTIAL_COLOR if partial else module.EMBED_COLOR),
            "fields": fields,
        }
        if ticker in chart_files:
            embed["image"] = {"url": f"attachment://{chart_files[ticker].name}"}
        (excluded if vetoes else clean).append(embed)
    return clean + excluded


def _plane_line(row, quality) -> str:
    """`**Plane:** low risk · high reward — reward 74 / risk 19`, or `""`.

    Reads only what was recorded. Three cases have to stay distinguishable:
    the layer never ran (no columns -> no line), an axis could not be measured
    (`unknown` -> the line says so rather than printing a number), and a real
    position. Never recomputes -- `annotate` is the single grading call.
    """
    quadrant = row.get(QUADRANT_COL)
    if not isinstance(quadrant, str) or not quadrant:
        return ""
    label = quality.QUADRANT_LABELS.get(quadrant, quadrant)
    reward, risk = row.get(REWARD_COL), row.get(RISK_COL)
    if not (_is_number(reward) and _is_number(risk)):
        return f"**Plane:** {label}"
    return f"**Plane:** {label} — reward {reward:.0f} / risk {risk:.0f}"


def _is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and not pd.isna(value))


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
VERDICT_COLS = [VERDICT_COL, CONVICTION_COL] + DEEP_VETO_COLS


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
