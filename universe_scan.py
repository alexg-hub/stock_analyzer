"""
Universe-wide risk/reward pass: every constituent on the quadrant plane.

Grades the whole index through the `quality` registry's **`fast`** stage and
records the two axis coordinates `quality.axis_scores` produces, so the thesis
question -- own the index minus the losers -- can finally be *looked at* rather
than argued about. Tiers 1-3 only ever grade what fired a screen or passed a
gate; this is the first thing here that grades everything.

Three things about it are deliberate:

  * **`fast` stage only.** The `deep` stage means an EDGAR fetch and a
    `collect_yahoo` pass per ticker, which is affordable for a handful of
    candidates and not for 500 names. So the risk axis here is the 13 `fast`
    distress parameters and *not* the 18 `sec_flags` ones -- a genuinely partial
    reading, which is why every output carries `stage` and the metrics-used
    counts. A fast-stage risk score is not comparable to a deep-stage one, and
    nothing here pretends otherwise.
  * **A per-ticker cache, not a whole-file one.** `backtest_universe`'s price
    panel is one frame with one age check because it is one download. This is
    500 independent fetches where any one can fail, so freshness is tracked per
    ticker: a failed or stale name is re-fetched without disturbing 499 good
    ones.
  * **It is polite to Yahoo.** The `fast` stage is 4 sequential HTTP calls per
    ticker (`info` plus three statements) and nothing else in this repo has ever
    run that pattern more than ~21 times in a row. There is a delay between
    tickers and a bounded retry with backoff -- the only such handling in the
    codebase, and it exists because 500x is a load shape we have not measured.

**Scope decides which files a run may write.** The cache is shared -- it is keyed
per ticker, so any run refreshes the rows it touched -- but the table, scatter and
page are named for the population behind them, because they are date-stamped and
a ten-ticker run used to resolve to the *same* filenames as the 503-ticker pass
and silently replace the day's whole-index plane with a plane of ten.

Outputs (all under `output/<universe.dir>/`):
  * `fundamentals_cache.pkl`       -- per-ticker values + axes + collected_at
  * `risk_reward_<date>.csv` + `.png`/`.html`     -- the full-index pass
  * `signals_plane_<date>.csv` + `.png`/`.html`   -- `--from-signals`
  * `subset_plane_<date>.csv` + `.png`/`.html`    -- `--tickers`

Usage:
    python universe_scan.py                        # whole index, cache-aware
    python universe_scan.py --from-signals         # what the screens flagged
    python universe_scan.py --from-signals 14      # ...in the last 14 days
    python universe_scan.py --tickers MSFT KO      # just these
    python universe_scan.py --refresh              # ignore the cache
    python universe_scan.py --no-fetch             # re-render from cache only
    python universe_scan.py --limit 30             # first N, for a timing probe

The weekly scheduled task runs `--from-signals`; the full pass is on no schedule,
because it is the base population for the sector-relative percentiles the scoring
still needs and is re-run deliberately rather than automatically.
"""

import argparse
import json
import pickle
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import charts
import peers
import quality
from scanner_common import (
    enable_utf8_output,
    load_config,
    log_step,
    output_dir,
    universe_constituents,
)

CONFIG_KEY = "universe"

#: Re-exported from `quality`, which owns the plane's definition so the nightly
#: card, this table and the interactive page cannot disagree about where "buy" is.
BUY = quality.QUADRANT_BUY
SPECULATIVE = quality.QUADRANT_SPECULATIVE
DULL = quality.QUADRANT_DULL
AVOID = quality.QUADRANT_AVOID
UNKNOWN = quality.QUADRANT_UNKNOWN

TABLE_COLUMNS = [
    "ticker", "company", "sector", "sub_industry", "index_name",
    "reward", "risk", "safety", "quadrant",
    "vetoed", "veto_reasons",
    "stage", "risk_metrics_used", "risk_metrics_total",
    "reward_metrics_used", "reward_metrics_total",
    "worst_risk", "collected_at", "error",
]

# What a run covers, which decides **which files it may write**. The cache is
# shared by every scope (it is keyed per ticker, so a subset run simply refreshes
# the rows it touched), but the table, PNG and page are not: they are named for
# the population behind them.
#
# This exists because the artifacts are date-stamped, so a ten-ticker run and the
# 503-ticker pass resolved to the *same* filenames -- and the subset silently
# replaced the full day's table, scatter and page. Nothing errored; the plane
# just quietly became a plane of ten names.
SCOPE_UNIVERSE = "universe"     # every constituent
SCOPE_SIGNALS = "signals"       # what the screens flagged in a recent window
SCOPE_SUBSET = "subset"         # tickers named on the command line

#: Filename stems per scope. Only the universe stem is configurable, because
#: only it is referenced from elsewhere (`mcp_tools.universe` globs it, and the
#: published page is built from it).
_SCOPE_STEMS = {SCOPE_SIGNALS: "signals_plane", SCOPE_SUBSET: "subset_plane"}


def artifact_stem(cfg: dict, scope: str) -> str:
    if scope == SCOPE_UNIVERSE:
        return section(cfg).get("table_csv", "risk_reward")
    return _SCOPE_STEMS.get(scope, scope)


def artifact_paths(cfg: dict, scope: str) -> dict:
    """Where this scope's table, scatter and page go.

    The universe scope keeps the exact configured names so the MCP tools and the
    published page keep resolving; every other scope is derived from its stem and
    therefore cannot collide with it.
    """
    stem = artifact_stem(cfg, scope)
    stamp = pd.Timestamp.today().date().isoformat()
    directory = universe_dir(cfg)
    if scope == SCOPE_UNIVERSE:
        png = section(cfg).get("chart_path", "risk_reward.png")
        html = section(cfg).get("html_path", "risk_reward.html")
    else:
        png, html = f"{stem}.png", f"{stem}.html"
    return {"csv": directory / f"{stem}_{stamp}.csv",
            "png": directory / png,
            "html": directory / html}


def section(cfg: dict) -> dict:
    return cfg.get(CONFIG_KEY) or {}


def universe_dir(cfg: dict, create: bool = True) -> Path:
    """`output/<universe.dir>` -- its own directory, like the ledger's.

    A whole-universe artifact belongs to no tier: it is not per-signal
    (`reports/`), not per-position (`portfolio/`) and not a scan record
    (`history/`). Same bare-filename-in-config convention as everything else, so
    an absolute path in config still overrides it and the tests can redirect it.
    """
    configured = Path(section(cfg).get("dir", "universe"))
    path = configured if configured.is_absolute() else output_dir() / configured
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def cache_path(cfg: dict) -> Path:
    return universe_dir(cfg) / section(cfg).get("cache", "fundamentals_cache.pkl")


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------

def load_cache(cfg: dict) -> dict:
    """Ticker -> entry. A missing or unreadable cache is an empty one.

    Never raises: a corrupt pickle must cost you the cache, not the run. The
    entries are re-fetchable by definition, so the safe failure is to refetch.
    """
    path = cache_path(cfg)
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("rb") as fh:
            data = pickle.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - a bad cache is a cold cache
        log_step("UNIVERSE", "warn", f"cache unreadable, starting cold: {exc}",
                 cfg=cfg)
        return {}


def save_cache(cfg: dict, cache: dict) -> Path:
    path = cache_path(cfg)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(cache, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)               # atomic, so a kill mid-write cannot truncate
    return path


def _age_days(entry: dict) -> float:
    stamp = pd.to_datetime(entry.get("collected_at"), errors="coerce", utc=True)
    if pd.isna(stamp):
        return float("inf")
    return (pd.Timestamp.now(tz="UTC") - stamp).total_seconds() / 86400.0


def is_stale(entry: dict, cfg: dict) -> bool:
    """Whether this ticker needs re-fetching.

    An entry that errored is always stale -- a transient Yahoo failure should not
    pin a ticker out of the table for a week.
    """
    if not entry or entry.get("error"):
        return True
    max_age = float(section(cfg).get("cache_max_age_days", 7))
    return _age_days(entry) > max_age


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

def load_prices(cfg: dict, tickers: list[str]):
    """One bulk price download for the whole universe, plus the benchmark.

    This is why the `price_risk` metrics are free at universe scale: `yf.download`
    batches server-side, so 500 tickers of history is one threaded call rather
    than 500 -- the same asymmetry that makes the nightly price scan cheap and
    the per-ticker fundamentals pass expensive.

    Deliberately not `backtest_universe.cached_panel`: that pickle is keyed to a
    fixed universe and is a day stale at best (13 days, as it happens), and a
    volatility reading two weeks out of date is wrong in a way nobody would
    notice. Returns `(closes, benchmark)`, either possibly None -- with no panel
    the price metrics simply read as missing and the axis says so.
    """
    from scanner_common import download_price_data
    bench = cfg.get("backtest", {}).get("benchmark_ticker", "SPY")
    period = str(section(cfg).get("price_period", "5y"))
    interval = cfg.get("data", {}).get("download_interval", "1d")
    try:
        panel = download_price_data(sorted(set(tickers) | {bench}), period,
                                    interval)
        closes = panel["Close"]
        series = closes[bench] if bench in closes.columns else None
        log_step("UNIVERSE", "ok",
                 f"prices for {closes.shape[1]} ticker(s), {closes.shape[0]} bars"
                 + ("" if series is not None else f" -- no {bench}, no beta"),
                 cfg=cfg)
        return closes, series
    except Exception as exc:  # noqa: BLE001 - fail open; price metrics go missing
        log_step("UNIVERSE", "failed",
                 f"price panel: {exc} -- price risk will read as missing",
                 cfg=cfg)
        return None, None


def collect_one(ticker: str, cfg: dict, retries: int = 2,
                backoff_s: float = 1.5, close=None, benchmark=None) -> dict:
    """One ticker's `fast`-stage values and axis coordinates.

    Retries are new to this repo and exist only because of the volume: a single
    ticker failing in the nightly scan is one missing row, while a rate-limit
    wall at ticker 200 of 500 would silently halve the universe. `collect`
    itself already swallows per-source failures, so what is caught here is the
    harder kind -- a throttle or a socket error taking the whole ticker down.
    """
    last_error = None
    for attempt in range(retries + 1):
        try:
            bundle = quality.collect(ticker, cfg, quality.STAGE_FAST,
                                     close=close, benchmark=benchmark)
            values = quality.resolve(bundle, cfg, quality.STAGE_FAST)
            # `regrade` recomputes all of this at render time, so the axes stored
            # here are never what gets plotted -- but they are what a reader of
            # the cache sees, and grading them without the sector made the stored
            # veto count disagree with the rendered one (92 against 59). Store the
            # same answer the table will show.
            result = quality.evaluate(values, cfg, quality.STAGE_FAST,
                                      peers.sector_of(ticker, cfg))
            return {
                "collected_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"),
                "stage": quality.STAGE_FAST,
                "company": bundle.get(quality.COMPANY_COL),
                "values": values,
                "reward": result.reward,
                "risk": result.risk,
                "safety": result.safety,
                "groups": result.groups,
                "vetoed": result.vetoed,
                "veto_reasons": list(result.veto_reasons or []),
                "quality_passed": result.passed,
                "score": result.score,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - one ticker never kills the pass
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(backoff_s * (attempt + 1))
    log_step("UNIVERSE", "failed", f"{ticker}: {last_error}", cfg=cfg)
    return {"collected_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "stage": quality.STAGE_FAST, "error": last_error,
            "values": {}, "reward": None, "risk": None, "safety": None,
            "groups": {}, "vetoed": None, "veto_reasons": []}


def scan(cfg: dict, tickers: list[str], refresh: bool = False,
         fetch: bool = True) -> dict:
    """Fill the cache for `tickers`, returning it. Progress logged as it goes.

    The per-N progress line is not cosmetic: `mcp_tools.backtests.run_script`
    kills a child whose log is still empty after 60s, and `quality.fetch_fast`
    logs once *after* its whole loop -- which at universe scale would look
    exactly like the stalled interpreter that watchdog exists to catch.
    """
    cache = load_cache(cfg)
    every = max(1, int(section(cfg).get("progress_every", 10)))
    delay = float(section(cfg).get("request_delay_s", 0.2))
    retries = int(section(cfg).get("retries", 2))

    todo = [t for t in tickers if refresh or is_stale(cache.get(t, {}), cfg)]
    log_step("UNIVERSE", "ok",
             f"{len(tickers)} ticker(s); {len(todo)} to fetch, "
             f"{len(tickers) - len(todo)} cached", cfg=cfg)
    if not fetch:
        if todo:
            log_step("UNIVERSE", "skip",
                     f"--no-fetch: {len(todo)} stale ticker(s) left as they are",
                     cfg=cfg)
        return cache

    closes, benchmark = load_prices(cfg, todo) if todo else (None, None)

    started = time.time()
    for i, ticker in enumerate(todo, start=1):
        close = (closes[ticker] if closes is not None
                 and ticker in closes.columns else None)
        cache[ticker] = collect_one(ticker, cfg, retries=retries,
                                    close=close, benchmark=benchmark)
        if i % every == 0 or i == len(todo):
            rate = (time.time() - started) / i
            left = rate * (len(todo) - i)
            log_step("UNIVERSE", "ok",
                     f"{i}/{len(todo)} fetched ({rate:.2f}s/ticker, "
                     f"~{left/60:.1f}min left)", cfg=cfg)
            save_cache(cfg, cache)      # checkpoint, so a kill loses ~10 tickers
        if delay and i < len(todo):
            time.sleep(delay)

    save_cache(cfg, cache)
    ok = sum(1 for t in todo if not cache.get(t, {}).get("error"))
    log_step("UNIVERSE", "ok" if ok == len(todo) else "partial",
             f"fetched {ok}/{len(todo)} in {time.time() - started:.0f}s",
             cfg=cfg)
    return cache


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------

def _axis_counts(entry: dict, axis: str) -> tuple[int, int]:
    """Metrics that produced a value, and metrics available, on one axis."""
    used = total = 0
    for group in (entry.get("groups") or {}).values():
        if group.get("axis") != axis:
            continue
        used += int(group.get("metrics_used") or 0)
        total += int(group.get("metrics_total") or 0)
    return used, total


def _worst_risk(entry: dict, cfg: dict, sector: str = "", top: int = 3) -> str:
    """The lowest-scoring risk readings, named. What actually drove the axis.

    Selected by **axis**, not by group name: accounting distress and market risk
    are two groups on one axis, and naming only one of them would hide the very
    readings most likely to be driving a high risk score.

    `sector` is not optional in practice. This column exists to explain the
    `risk` number beside it, so it has to read each metric on the same basis the
    axis did -- via `quality.normalized_of`, which consults the peer
    distribution for the 30 `sector_relative` parameters. Normalizing against
    the absolute anchors here instead made the column contradict its own row:
    NEE reported "Altman Z 0.00; Int coverage 0.00; ST debt/cash 0.00" beside a
    risk of 35.2, when peer-aware those read 0.82, 0.11 and 0.73 and two of the
    three were not among the worst three at all. Utilities finance a rate base,
    so their absolute Altman Z is structurally low -- the exact bias `peers.py`
    removes from the score, leaking back in through the explanation.
    """
    specs = quality.parameters(cfg, quality.STAGE_FAST)
    groups = quality.groups(cfg)
    values = entry.get("values") or {}
    scored = []
    for key, spec in specs.items():
        group = groups.get(spec.get("group")) or {}
        if quality.axis_of(group) != quality.AXIS_RISK:
            continue
        reading = quality.normalized_of(key, spec, values.get(key), cfg, sector)
        if reading is None:
            continue
        scored.append((reading, quality.label_of(key, spec)))
    scored.sort()
    return "; ".join(f"{label} {score:.2f}" for score, label in scored[:top])


def regrade(entry: dict, sector: str, cfg: dict) -> dict:
    """Re-evaluate one cached entry against the rules in force **now**.

    The cache's expensive content is `values` -- what Yahoo said. The axes,
    quadrant and veto reasons stored beside them are just what the *config at
    collection time* made of those numbers, and config is retuned constantly: the
    day sector-relative scoring was switched on, every stored axis became stale
    while every stored value stayed perfectly good.

    So grading happens here, at render time, from the values. That makes a scoring
    change visible with `--no-fetch` and no network at all, and it is the same
    rule tier 3 already follows in `quality.verdict_of` -- re-grade rather than
    trust a recorded verdict, because a stale answer to a changed question is
    worse than no answer.

    Falls back to the stored verdict for an entry that has no values (an errored
    fetch), so a failed ticker still reports what it reported.
    """
    values = (entry or {}).get("values") or {}
    if not values:
        return {"reward": entry.get("reward"), "risk": entry.get("risk"),
                "safety": entry.get("safety"), "groups": entry.get("groups") or {},
                "vetoed": entry.get("vetoed"),
                "veto_reasons": entry.get("veto_reasons") or []}
    result = quality.evaluate(values, cfg, entry.get("stage") or quality.STAGE_FAST,
                              sector)
    return {"reward": result.reward, "risk": result.risk, "safety": result.safety,
            "groups": result.groups, "vetoed": result.vetoed,
            "veto_reasons": list(result.veto_reasons or [])}


def build_table(cfg: dict, cache: dict, constituents: pd.DataFrame
                ) -> pd.DataFrame:
    """One row per ticker, sorted best-quadrant-first then by reward.

    Every row is re-graded against the current config (see `regrade`), so this is
    where a threshold change or a peer-stats rebuild takes effect.
    """
    sectors = constituents.set_index("ticker") if not constituents.empty \
        else pd.DataFrame()
    rows = []
    for ticker, entry in cache.items():
        meta = sectors.loc[ticker] if ticker in sectors.index else {}
        sector = (meta.get("sector") if hasattr(meta, "get") else "") or ""
        graded = regrade(entry, sector, cfg)
        fresh = dict(entry or {}, **graded)
        r_used, r_total = _axis_counts(fresh, quality.AXIS_RISK)
        w_used, w_total = _axis_counts(fresh, quality.AXIS_REWARD)
        rows.append({
            "ticker": ticker,
            "company": entry.get("company"),
            "sector": sector,
            "sub_industry": (meta.get("sub_industry")
                             if hasattr(meta, "get") else "") or "",
            # Which index the name came from. Carried so the by-index skew is
            # readable straight off the table -- adding a second population to
            # one set of peer distributions is a hypothesis, not a fact.
            "index_name": (meta.get("index_name")
                           if hasattr(meta, "get") else "") or "",
            "reward": graded["reward"],
            "risk": graded["risk"],
            "safety": graded["safety"],
            "quadrant": quality.quadrant_of(graded["reward"], graded["risk"], cfg),
            "vetoed": graded["vetoed"],
            "veto_reasons": quality.veto_text(graded["veto_reasons"], cfg),
            "stage": entry.get("stage"),
            "risk_metrics_used": r_used,
            "risk_metrics_total": r_total,
            "reward_metrics_used": w_used,
            "reward_metrics_total": w_total,
            "worst_risk": _worst_risk(entry, cfg, sector),
            "collected_at": entry.get("collected_at"),
            "error": entry.get("error"),
        })
    table = pd.DataFrame(rows, columns=TABLE_COLUMNS)
    if table.empty:
        return table
    order = {BUY: 0, SPECULATIVE: 1, DULL: 2, AVOID: 3, UNKNOWN: 4}
    table["_q"] = table["quadrant"].map(order).fillna(9)
    table = (table.sort_values(["_q", "reward"], ascending=[True, False])
                  .drop(columns="_q").reset_index(drop=True))
    return table


def write_table(cfg: dict, table: pd.DataFrame,
                scope: str = SCOPE_UNIVERSE) -> Path:
    path = artifact_paths(cfg, scope)["csv"]
    table.round(2).to_csv(path, index=False)
    return path


def _with_sectors(cfg: dict, tickers: list[str]) -> pd.DataFrame:
    """A constituents frame for `tickers`, sector filled in where known.

    One Wikipedia request buys the sector labels, which the chart colours nothing
    by but the table and the peer lookup both use. Fail-open: a scoped run must
    still work when Wikipedia is unreachable, just without sectors.
    """
    blank = pd.DataFrame({"ticker": tickers, "sector": "", "sub_industry": "",
                          "index_name": ""})
    try:
        full = universe_constituents(cfg)
    except Exception as exc:  # noqa: BLE001 - sector is a nicety, not a gate
        log_step("UNIVERSE", "warn",
                 f"no sector labels ({exc}) -- grading anyway", cfg=cfg)
        return blank
    merged = blank[["ticker"]].merge(full, on="ticker", how="left")
    return merged.fillna({"sector": "", "sub_industry": "", "index_name": ""})


def _limited(constituents: pd.DataFrame, limit: int) -> pd.DataFrame:
    """The first `limit` names, sampled across every index rather than off the
    top of the concatenation.

    `--limit` is a timing probe: it exists to answer "how long will the full
    pass take" before committing ~20 minutes to it. A plain `.head()` reads
    entirely off the first source, so the probe would time 30 large caps and
    say nothing about the mid caps whose thinner Yahoo coverage is exactly what
    might slow the run down.
    """
    if constituents.empty or "index_name" not in constituents.columns:
        return constituents.head(limit)
    groups = constituents["index_name"].nunique()
    per_source = -(-limit // max(1, groups))  # ceil, so the total is never short
    return (constituents.groupby("index_name", sort=False)
            .head(per_source).head(limit))


def signal_tickers(cfg: dict, days: int) -> list[str]:
    """Tickers the screens flagged in the last `days` days, newest first.

    Read from `signals.csv`, which holds one row per (scan_date, screen, ticker)
    and **only** screen signals -- an ad-hoc look is recorded in the on-demand
    table instead, so "identified by a strategy" needs no extra filtering here.
    A ticker that fired on two screens, or on two nights, appears once.

    Goes through `read_table` rather than `pd.read_csv` because older quiet
    nights left a headerless file, and reading that raises.
    """
    from scanner_common import read_table, signals_csv_path

    frame = read_table(signals_csv_path(cfg))
    if frame.empty or "ticker" not in frame.columns:
        return []
    dates = pd.to_datetime(frame.get("scan_date"), errors="coerce")
    cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=int(days))
    recent = frame[dates >= cutoff].copy()
    recent["_d"] = dates[dates >= cutoff]
    ordered = recent.sort_values("_d", ascending=False)["ticker"].astype(str)
    return list(dict.fromkeys(ordered))            # de-duplicated, order kept


def summarize(table: pd.DataFrame, cfg: dict) -> str:
    """The one-paragraph honest summary: counts per quadrant and the caveat."""
    if table.empty:
        return "No tickers graded."
    counts = table["quadrant"].value_counts()
    parts = [f"{counts.get(q, 0)} {q}"
             for q in (BUY, SPECULATIVE, DULL, AVOID, UNKNOWN)]
    stage = (table["stage"].dropna().iloc[0]
             if table["stage"].notna().any() else "?")
    risk_used = table["risk_metrics_used"].mean()
    risk_total = table["risk_metrics_total"].max()
    return (f"{len(table)} tickers, {', '.join(parts)}. "
            f"Stage {stage}: risk axis from {risk_used:.1f}/{risk_total:.0f} "
            f"metrics on average"
            + (" -- the SEC filing flags are deep-stage and absent here."
               if stage == quality.STAGE_FAST else "."))


# --------------------------------------------------------------------------
# The interactive rendering
# --------------------------------------------------------------------------

_HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Universe risk vs reward</title></head><body>
"""

_HTML_TAIL = "</body></html>\n"

#: Inline CSS and JS only -- no CDN, no external font, no fetch. That is what
#: lets the same bytes work as a local file opened from `output/` and as a
#: published Artifact, where a strict CSP blocks every external host.
_HTML_BODY = """
<style>
/* Palette and type tokens. The two hues are `charts.py`'s validated pair, so the
   PNG and this page describe the same data in the same colours; the semantic
   trio is used ONLY on the quadrant chips, where it encodes a judgment, and
   never on the marks, where the sole categorical split is excluded/not. */
:root {
  --bg: #fcfcfb; --panel: #ffffff; --sunk: #f6f5f1;
  --ink: #14140f; --ink2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --hair: #ecebe4;
  --series: #2a78d6; --event: #eb6834;
  --good: #276b47; --warn: #8a6212; --crit: #a33a22; --idle: #6b6a64;
  --shadow: rgba(20,20,15,.09);
  --display: "Helvetica Neue", Helvetica, Arial, system-ui, sans-serif;
  --body: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --data: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #131311; --panel: #1c1c1a; --sunk: #191917;
    --ink: #f4f3ef; --ink2: #b8b6ae; --muted: #8a8880;
    --grid: #2c2c28; --axis: #3d3d38; --hair: #24241f;
    --series: #5fa3ee; --event: #f4884f;
    --good: #6cc08d; --warn: #d9a83c; --crit: #ef8163; --idle: #9b9a92;
    --shadow: rgba(0,0,0,.45);
  }
}
:root[data-theme="dark"] {
  --bg: #131311; --panel: #1c1c1a; --sunk: #191917;
  --ink: #f4f3ef; --ink2: #b8b6ae; --muted: #8a8880;
  --grid: #2c2c28; --axis: #3d3d38; --hair: #24241f;
  --series: #5fa3ee; --event: #f4884f;
  --good: #6cc08d; --warn: #d9a83c; --crit: #ef8163; --idle: #9b9a92;
  --shadow: rgba(0,0,0,.45);
}
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 24px 48px; background: var(--bg);
  color: var(--ink); font: 15px/1.55 var(--body); }
.wrap { max-width: 1120px; margin: 0 auto;
  display: flex; flex-direction: column; gap: 1.15rem; }
header { display: flex; flex-direction: column; gap: .35rem; }
.eyebrow { font: 600 .7rem/1 var(--data); letter-spacing: .1em;
  text-transform: uppercase; color: var(--muted); }
h1 { font: 600 1.75rem/1.15 var(--display); letter-spacing: -.022em; margin: 0;
  text-wrap: balance; }
.sub { color: var(--ink2); font-size: .875rem; max-width: 68ch; margin: 0; }

.counts { display: flex; flex-wrap: wrap; gap: .45rem; }
.chip { display: inline-flex; align-items: baseline; gap: .4rem;
  padding: .34rem .7rem .34rem .6rem; border-radius: 4px; font-size: .78rem;
  background: var(--sunk); border: 1px solid var(--hair); color: var(--ink2);
  border-left: 3px solid var(--idle); }
.chip b { font: 600 .95rem/1 var(--data); font-variant-numeric: tabular-nums;
  color: var(--ink); }
.chip.buy { border-left-color: var(--good); }
.chip.avoid { border-left-color: var(--crit); }
.chip.speculative { border-left-color: var(--warn); }
.chip.excluded { border-left-color: var(--event); }

.controls { display: flex; flex-wrap: wrap; gap: 1.1rem 1.4rem;
  align-items: center; padding: .8rem 1rem; background: var(--panel);
  border: 1px solid var(--grid); border-radius: 6px; font-size: .82rem; }
.controls label { color: var(--ink2); display: flex; align-items: center;
  gap: .45rem; }
.controls b { font: 600 .82rem/1 var(--data); font-variant-numeric: tabular-nums;
  color: var(--ink); min-width: 2ch; text-align: right; }
select, input[type=range] { accent-color: var(--series); }
select { background: var(--bg); color: var(--ink); border: 1px solid var(--axis);
  border-radius: 4px; padding: .25rem .4rem; font: inherit; }
:is(select, input, .dot):focus-visible { outline: 2px solid var(--series);
  outline-offset: 2px; }

.plotwrap { position: relative; background: var(--panel);
  border: 1px solid var(--grid); border-radius: 6px; padding: 10px;
  overflow-x: auto; }
svg { display: block; width: 100%; height: auto; min-width: 580px; }
svg text { font-family: var(--data); }
.dot { cursor: pointer; }
#tip { position: absolute; pointer-events: none; opacity: 0;
  transition: opacity .09s; background: var(--panel); color: var(--ink);
  border: 1px solid var(--axis); border-radius: 5px; padding: .55rem .7rem;
  font-size: .78rem; max-width: 290px; box-shadow: 0 6px 18px var(--shadow);
  z-index: 5; }
#tip .t { font: 600 .85rem/1.3 var(--display); letter-spacing: -.01em; }
#tip .m { color: var(--ink2); }
#tip .n { font-family: var(--data); font-variant-numeric: tabular-nums; }

.tablewrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .8rem;
  min-width: 620px; }
th, td { text-align: left; padding: .42rem .6rem;
  border-bottom: 1px solid var(--hair); }
th { color: var(--muted); font: 600 .68rem/1 var(--data);
  letter-spacing: .07em; text-transform: uppercase; }
td.t { font-weight: 600; }
td.num { text-align: right; font-family: var(--data);
  font-variant-numeric: tabular-nums; }
caption { text-align: left; color: var(--ink2); font-size: .82rem;
  padding-bottom: .55rem; }
.note { color: var(--muted); font-size: .78rem; border-top: 1px solid var(--grid);
  padding-top: .9rem; max-width: 78ch; }
.note strong { color: var(--ink2); }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>

<div class="wrap">
  <header>
    <div class="eyebrow">S&amp;P 500 &middot; deterministic scoring</div>
    <h1>Every name on the risk/reward plane</h1>
    <p class="sub" id="sub"></p>
  </header>

  <div class="counts" id="counts"></div>

  <div class="controls">
    <label>Sector <select id="sector"><option value="">all</option></select></label>
    <label>Reward &ge; <input type="range" id="rt" min="0" max="100" step="1">
      <b id="rtv"></b></label>
    <label>Risk &le; <input type="range" id="xt" min="0" max="100" step="1">
      <b id="xtv"></b></label>
    <label><input type="checkbox" id="onlyv"> only excluded</label>
  </div>

  <div class="plotwrap">
    <svg id="plot" viewBox="0 0 760 560" role="img"
         aria-label="Scatter of reward against risk, one point per ticker"></svg>
    <div id="tip"></div>
  </div>

  <div class="tablewrap">
    <table id="buytable">
      <caption id="buycap"></caption>
      <thead><tr><th>Ticker</th><th>Company</th><th>Sector</th>
        <th class="num">Reward</th><th class="num">Risk</th>
        <th>Worst risk readings</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <p class="note" id="note"></p>
</div>

<script>
const DATA = __DATA__, META = __META__;
const P = {l: 62, r: 18, t: 18, b: 52}, W = 760, H = 560;
const sx = v => P.l + (v / 100) * (W - P.l - P.r);
const sy = v => H - P.b - (v / 100) * (H - P.t - P.b);
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

$("sub").textContent = META.subtitle;
$("note").innerHTML = META.note;
$("rt").value = META.reward_threshold; $("xt").value = META.risk_threshold;

const sectors = [...new Set(DATA.map(d => d.sector).filter(Boolean))].sort();
for (const s of sectors) {
  const o = document.createElement("option"); o.value = o.textContent = s;
  $("sector").appendChild(o);
}

function quadrant(d, rt, xt) {
  if (d.reward == null || d.risk == null) return "unknown";
  if (d.reward >= rt) return d.risk <= xt ? "buy" : "speculative";
  return d.risk <= xt ? "dull" : "avoid";
}

function draw() {
  const rt = +$("rt").value, xt = +$("xt").value;
  const sector = $("sector").value, onlyv = $("onlyv").checked;
  $("rtv").textContent = rt; $("xtv").textContent = xt;

  const shown = DATA.filter(d => d.reward != null && d.risk != null
    && (!sector || d.sector === sector) && (!onlyv || d.vetoed));

  const parts = [];
  // Axis frame, gridlines, then the quadrant dividers, then the points --
  // painted in that order so nothing sits on top of a label.
  for (let v = 0; v <= 100; v += 20) {
    parts.push(`<line x1="${sx(v)}" y1="${P.t}" x2="${sx(v)}" y2="${H - P.b}"
      stroke="var(--grid)" stroke-width="1"/>`);
    parts.push(`<line x1="${P.l}" y1="${sy(v)}" x2="${W - P.r}" y2="${sy(v)}"
      stroke="var(--grid)" stroke-width="1"/>`);
    parts.push(`<text x="${sx(v)}" y="${H - P.b + 18}" text-anchor="middle"
      font-size="11" fill="var(--muted)">${v}</text>`);
    parts.push(`<text x="${P.l - 10}" y="${sy(v) + 4}" text-anchor="end"
      font-size="11" fill="var(--muted)">${v}</text>`);
  }
  parts.push(`<line x1="${sx(xt)}" y1="${P.t}" x2="${sx(xt)}" y2="${H - P.b}"
    stroke="var(--axis)" stroke-width="1.5"/>`);
  parts.push(`<line x1="${P.l}" y1="${sy(rt)}" x2="${W - P.r}" y2="${sy(rt)}"
    stroke="var(--axis)" stroke-width="1.5"/>`);
  parts.push(`<text x="${(W) / 2}" y="${H - 12}" text-anchor="middle"
    font-size="12" fill="var(--ink2)">risk &rarr; more dangerous</text>`);
  parts.push(`<text x="14" y="${H / 2}" transform="rotate(-90 14 ${H / 2})"
    text-anchor="middle" font-size="12" fill="var(--ink2)">reward &rarr; better</text>`);
  parts.push(`<text x="${P.l + 6}" y="${P.t + 14}" font-size="11"
    fill="var(--muted)">low risk &middot; high reward</text>`);
  parts.push(`<text x="${W - P.r - 6}" y="${H - P.b - 6}" text-anchor="end"
    font-size="11" fill="var(--muted)">high risk &middot; low reward</text>`);

  for (const d of shown) {
    const c = d.vetoed ? "var(--event)" : "var(--series)";
    parts.push(`<circle class="dot" cx="${sx(d.risk).toFixed(1)}"
      cy="${sy(d.reward).toFixed(1)}" r="${d.vetoed ? 6 : 4.5}" fill="${c}"
      fill-opacity="0.78" stroke="var(--panel)" stroke-width="1"
      data-i="${DATA.indexOf(d)}"/>`);
  }
  $("plot").innerHTML = parts.join("");

  const tally = {buy: 0, speculative: 0, dull: 0, avoid: 0, unknown: 0};
  for (const d of DATA) {
    if (sector && d.sector !== sector) continue;
    tally[quadrant(d, rt, xt)]++;
  }
  const LABEL = {buy: "low risk · high reward", speculative: "high reward · high risk",
    dull: "low risk · low reward", avoid: "high risk · low reward",
    unknown: "not measurable"};
  $("counts").innerHTML = Object.entries(tally).map(([k, v]) =>
    `<span class="chip ${k}"><b>${v}</b> ${LABEL[k]}</span>`).join("")
    + `<span class="chip excluded"><b>${DATA.filter(d => d.vetoed
        && (!sector || d.sector === sector)).length}</b> excluded by a rule</span>`;

  const buys = shown.filter(d => quadrant(d, rt, xt) === "buy")
    .sort((a, b) => b.reward - a.reward);
  $("buycap").textContent =
    `Low-risk / high-reward quadrant — ${buys.length} name(s)`;
  $("buytable").querySelector("tbody").innerHTML = buys.map(d => `<tr>
    <td class="t">${esc(d.ticker)}</td><td>${esc(d.company)}</td>
    <td>${esc(d.sector)}</td>
    <td class="num">${d.reward.toFixed(1)}</td>
    <td class="num">${d.risk.toFixed(1)}</td>
    <td>${esc(d.worst_risk)}</td></tr>`).join("")
    || `<tr><td colspan="6">Nothing in this quadrant at these thresholds.</td></tr>`;
}

const tip = $("tip");
$("plot").addEventListener("mouseover", e => {
  const i = e.target.dataset && e.target.dataset.i;
  if (i == null) return;
  const d = DATA[+i];
  tip.innerHTML = `<div class="t">${esc(d.ticker)}${d.company
      ? " — " + esc(d.company) : ""}</div>
    <div class="m">${esc(d.sector) || "sector n/a"}</div>
    <div class="n">reward ${d.reward.toFixed(1)} &nbsp; risk ${d.risk.toFixed(1)}</div>
    ${d.vetoed ? `<div class="m">excluded: ${esc(d.veto_reasons)}</div>` : ""}
    ${d.worst_risk ? `<div class="m n">worst: ${esc(d.worst_risk)}</div>` : ""}`;
  const box = $("plot").getBoundingClientRect();
  const host = tip.parentElement.getBoundingClientRect();
  tip.style.left = (e.clientX - host.left + 14) + "px";
  tip.style.top = (e.clientY - host.top + 10) + "px";
  tip.style.opacity = 1;
});
$("plot").addEventListener("mouseout", () => { tip.style.opacity = 0; });
for (const id of ["rt", "xt", "sector", "onlyv"])
  $(id).addEventListener("input", draw);
draw();
</script>
"""


def html_payload(table: pd.DataFrame, cfg: dict) -> tuple[str, dict]:
    """The JSON the page embeds, plus its metadata. Unmeasurables included.

    Rows with a missing axis are carried so the page can *count* them; `draw`
    filters them out of the plot rather than positioning them at zero.
    """
    keep = ["ticker", "company", "sector", "reward", "risk", "vetoed",
            "veto_reasons", "worst_risk", "quadrant"]
    records = table.reindex(columns=keep).to_dict(orient="records")
    for row in records:
        # NaN -> None per value, not via `DataFrame.where`: that preserves the
        # column dtype, so `None` lands back as NaN and `allow_nan=False` below
        # raises. Which is the right guard -- JSON has no NaN and no browser can
        # parse one -- but it means the conversion has to actually happen here.
        for key, value in list(row.items()):
            if isinstance(value, float) and pd.isna(value):
                row[key] = None
        row["vetoed"] = bool(row.get("vetoed"))
        for key in ("company", "sector", "veto_reasons", "worst_risk"):
            row[key] = row.get(key) or ""
    meta = {
        "subtitle": summarize(table, cfg),
        "reward_threshold": quality.quadrant_thresholds(cfg)[0],
        "risk_threshold": quality.quadrant_thresholds(cfg)[1],
        "note": ("<strong>How to read this.</strong> Every score is computed in "
                 "Python from the quality registry in force at collection time; "
                 "no model produced any number here. Thresholds are display-only "
                 "&mdash; moving them re-labels the quadrants and changes no "
                 "recorded score. A fast-stage risk reading omits the SEC filing "
                 "flags, so it is not comparable to a deep-stage one. "
                 + _sector_caveat(table)),
    }
    return json.dumps(records, allow_nan=False), meta


def _sector_caveat(table: pd.DataFrame) -> str:
    """The measured sector skew, stated on the page rather than left to be found.

    Every anchor in the registry is *absolute*, and several are only meaningful
    against a peer group -- so the plane ranks sectors as well as companies. That
    is a real limitation of the current scoring and belongs next to the chart, not
    in a commit message. Computed rather than asserted, so it stops appearing when
    it stops being true.
    """
    if table.empty or "sector" not in table.columns:
        return ""
    buys = table[(table["quadrant"] == BUY) & (table["sector"] != "")]
    if buys.empty:
        return ""
    counts = buys["sector"].value_counts()
    top, n = counts.index[0], int(counts.iloc[0])
    share = n / len(buys) * 100
    base = int((table["sector"] == top).sum()) / max(1, len(table)) * 100
    if share < 1.5 * base:
        return ""
    return (f"<strong>Known bias:</strong> {top} is {share:.0f}% of the "
            f"low-risk/high-reward quadrant ({n} of {len(buys)}) against "
            f"{base:.0f}% of the index. The registry's thresholds are absolute, "
            f"and several only mean something against a peer group &mdash; "
            f"Altman Z and interest coverage are structurally low for banks and "
            f"for capital-intensive regulated businesses. Read the quadrant as a "
            f"shortlist to examine, not a portfolio, until scoring is "
            f"peer-relative.")


def write_html(table: pd.DataFrame, cfg: dict, out_path: Path,
               standalone: bool = True) -> Path:
    """Render the interactive page. `standalone=False` omits the doc wrapper.

    The two modes exist because a published Artifact supplies its own
    `<!doctype>`/`<head>`/`<body>` skeleton and rejects one of ours, while a file
    opened from `output/` needs the whole document.
    """
    data, meta = html_payload(table, cfg)
    body = (_HTML_BODY.replace("__DATA__", data)
                      .replace("__META__", json.dumps(meta)))
    out_path.write_text(
        (_HTML_HEAD + body + _HTML_TAIL) if standalone else body,
        encoding="utf-8")
    return out_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--tickers", nargs="*", help="grade only these")
    parser.add_argument("--from-signals", nargs="?", type=int, const=-1,
                        metavar="DAYS",
                        help="grade what the screens flagged in the last DAYS "
                             "days (default universe.signal_window_days)")
    parser.add_argument("--limit", type=int,
                        help="only the first N of the universe (timing probe)")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore cached entries and re-fetch")
    parser.add_argument("--no-fetch", action="store_true",
                        help="re-render from the cache without any network")
    parser.add_argument("--no-chart", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config()
    started = time.time()

    # The scope decides which files this run may write. Only a full pass touches
    # the shared universe artifacts; everything narrower writes its own, because
    # the filenames are date-stamped and a subset would otherwise replace the
    # day's whole-index table, scatter and page without a word.
    if args.tickers:
        scope = SCOPE_SUBSET
        wanted = [t.upper() for t in args.tickers]
        constituents = _with_sectors(cfg, wanted)
    elif args.from_signals is not None:
        scope = SCOPE_SIGNALS
        days = (int(section(cfg).get("signal_window_days", 7))
                if args.from_signals < 0 else args.from_signals)
        wanted = signal_tickers(cfg, days)
        log_step("UNIVERSE", "ok" if wanted else "none",
                 f"{len(wanted)} ticker(s) flagged by a screen in the last "
                 f"{days} day(s)", cfg=cfg)
        if not wanted:
            # Not a failure: a quiet week is a real outcome, and re-rendering the
            # previous week's page over it would misreport this one.
            print(f"No screen signals in the last {days} days -- nothing to grade.")
            return 0
        constituents = _with_sectors(cfg, wanted)
    else:
        scope = SCOPE_UNIVERSE
        constituents = universe_constituents(cfg)
        if args.limit:
            # A limited run is a subset, and must be scoped like one. It kept
            # SCOPE_UNIVERSE until 2026-08-13, which meant a 30-ticker timing
            # probe both replaced the day's whole-index table/PNG/page and
            # **rebuilt peer_stats.json from 30 names** -- the one file whose
            # whole contract is that only a full pass may narrow it. The
            # `--tickers` path had been fixed for exactly this; `--limit` sat
            # one branch away and had not.
            scope = SCOPE_SUBSET
            constituents = _limited(constituents, args.limit)

    tickers = constituents["ticker"].tolist()
    cache = scan(cfg, tickers, refresh=args.refresh, fetch=not args.no_fetch)

    # Peer statistics are rebuilt only by a **full** pass, because only a full
    # pass has the whole sector to describe. A subset run must never narrow the
    # distributions every other reading is compared against -- that would make
    # the thresholds drift with whatever you last looked at.
    if scope == SCOPE_UNIVERSE:
        try:
            sectors = dict(zip(constituents["ticker"], constituents["sector"]))
            peers.write(peers.build(cache, sectors, cfg), cfg)
            peers.reset_cache()
        except Exception as exc:  # noqa: BLE001 - absent stats fall back to anchors
            log_step("PEERS", "failed", f"{exc} -- absolute anchors stand",
                     cfg=cfg)

    # Render the tickers asked for, not the whole cache -- a scoped run must not
    # silently republish a table built from a months-old universe pass.
    view = {t: cache[t] for t in tickers if t in cache}
    table = build_table(cfg, view, constituents)
    paths = artifact_paths(cfg, scope)
    csv_path = write_table(cfg, table, scope)

    print(f"[{scope}] {summarize(table, cfg)}")
    print(f"Table -> {csv_path}")
    if not table.empty:
        cols = ["ticker", "sector", "reward", "risk", "quadrant", "vetoed"]
        print(table[cols].head(25).to_string(index=False))

    if not args.no_chart and not table.empty:
        # `charts` never reads config, so the two threshold values are merged in
        # here from where they actually live (`quality.quadrant`) rather than the
        # builder reaching across sections for them.
        reward_min, risk_max = quality.quadrant_thresholds(cfg)
        chart_cfg = dict(section(cfg),
                         reward_min=reward_min, risk_max=risk_max)
        try:
            charts.plot_risk_reward(table, chart_cfg, paths["png"],
                                    dpi=int(section(cfg).get("chart_dpi", 120)))
        except Exception as exc:  # noqa: BLE001 - a chart never kills the pass
            log_step("UNIVERSE", "failed", f"chart: {exc}", cfg=cfg)
        try:
            write_html(table, cfg, paths["html"])
            print(f"Interactive -> {paths['html']}")
        except Exception as exc:  # noqa: BLE001
            log_step("UNIVERSE", "failed", f"html: {exc}", cfg=cfg)

    print(f"Done in {time.time() - started:.1f}s.")
    return 0


if __name__ == "__main__":
    enable_utf8_output()
    sys.exit(main())
