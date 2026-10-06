"""Industry valuation anomalies -- has an industry's multiple left its own history?

Every other surface here grades a **company**. Tiers 1-3 ask what a ticker did on
the tape and what its own statements say; `peers.py` compares a company to its
sector *today*. None of them can answer the question this module exists for:
money rotated into semiconductors while pharmaceuticals went sideways -- was the
sector that did nothing actually cheap, or just unloved for a reason?

A price gap alone cannot answer that. Over any window the identity

    dlog(Price)  =  dlog(P/E)  +  dlog(EPS)

holds exactly, because P = (P/E) x EPS, and it separates the two things a price
move can mean. An industry whose **earnings grew while its multiple compressed**
has been de-rated -- that is the opportunity. An industry whose multiple fell
*because* its earnings fell has not been de-rated at all, it has been repriced
correctly. The whole module is that decomposition, computed per industry.

Where the history comes from
----------------------------
`research_collect.quarterly_eps` returns reported quarterly EPS indexed by
**announcement date**, and Yahoo serves ~24 quarters. Rolled to TTM and
forward-filled onto a 5y price panel that is a genuine multi-year P/E path --
earnings varying over time, not price wearing P/E's clothes. The forward-fill is
what makes it point-in-time: a figure can only ever propagate forward from the
day it was published.

Forward P/E has no history anywhere. Yahoo serves only today's value and nothing
in this repo has ever stored it, so `forward_pe` here is a **level, not a path**,
and `valuation_history.csv` is what turns it into a series from now on.

Two statistics, two aggregations, deliberately
----------------------------------------------
The **level** (`pe_now`, `pe_z`, `pe_pctile`) is a cross-sectional **median** of
member P/Es: robust to one distorted member, and interpretable -- "pharma trades
at 14x" is a sentence. The **decomposition** is built from equal-weight indices
(`mean` of member ratios), because the identity above is additive in logs and a
median is not: median(a+b) != median(a)+median(b), so a median decomposition
would not add up and the one property worth testing would be untestable.

What this is not
----------------
It is **not a screen**, and is in neither `run_scanners.SCANNERS` nor
`backtest_universe.SCREENS` -- `tests/test_industry_valuation.py` pins both
absences. A full-history mask is in principle computable here (unlike
`theme_screen`), which is a real future option; nothing below forecloses it.

`record` is the opt-in write into `signals.csv`, so tier 4 buys the picks and
grades whether the anomaly ever paid. Unlike `theme_signals` it *may* record
numbers: there the picks are named by a model, so a number it wrote would be
unaccountable, whereas every figure here is computed in Python from a recorded
panel. `Verdict`/`Conviction` still stay out -- tier 3 owns those.
"""

from __future__ import annotations

import json
import math
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from scanner_common import (INDEX_COL, HISTORY_KEYS, VERDICT_COLS, log_step,
                            merge_history_csv, output_dir, signals_csv_path,
                            universe_constituents)

CONFIG_KEY = "industry_valuation"

# The value written into signals.csv's `Setup` column -- distinct from the
# screens' full/partial/none and from theme_screen's "theme".
SETUP_VALUE = "valuation"

# An empty Trigger round-trips through CSV as NaN and `groupby` drops NaN
# silently, which would discard exactly the cohort the column exists to isolate.
TRIGGER_NONE = "none"

FLAG_CHEAP = "cheap"
FLAG_RICH = "rich"
FLAG_NONE = ""

# A bucket that had too few members to stand on its own and was rolled up to its
# sector carries this suffix, so it can never be read as a real sub-industry.
ROLLUP_SUFFIX = " (other)"

BUCKET_COL = "bucket"
HISTORY_TABLE_KEYS = ["scan_date", BUCKET_COL]

TABLE_COLUMNS = [
    "scan_date", BUCKET_COL, "sector", "rolled_up",
    "pe_now", "pe_asof", "pe_median", "pe_pctile", "pe_z",
    "price_chg_pct", "multiple_chg_pct", "earnings_chg_pct",
    "price_dlog", "multiple_dlog", "earnings_dlog",
    "forward_pe", "implied_eps_growth_pct",
    "flag", "members_n", "members_priced_n", "window_members_n",
    "history_days", "membership_asof",
]


# --------------------------------------------------------------------------
# Config and paths
# --------------------------------------------------------------------------

def section(cfg: dict) -> dict:
    return cfg.get(CONFIG_KEY) or {}


def is_enabled(cfg: dict) -> bool:
    return bool(section(cfg).get("enabled", False))


def valuation_dir(cfg: dict, create: bool = True) -> Path:
    """`output/<industry_valuation.dir>`. Bare name in config resolves under
    `output_dir()`; an absolute path still wins, which is how tests redirect."""
    configured = Path(section(cfg).get("dir", "valuation"))
    path = configured if configured.is_absolute() else output_dir() / configured
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def eps_cache_path(cfg: dict) -> Path:
    return valuation_dir(cfg) / section(cfg).get("eps_cache", "eps_cache.pkl")


def history_csv_path(cfg: dict) -> Path:
    return valuation_dir(cfg) / section(cfg).get("history_csv",
                                                 "valuation_history.csv")


def table_path(cfg: dict, scan_date: str) -> Path:
    stem = section(cfg).get("table_csv", "industry_valuation")
    return valuation_dir(cfg) / f"{stem}_{scan_date}.csv"


def chart_path(cfg: dict, scan_date: str) -> Path:
    name = section(cfg).get("chart_path", "industry_valuation.png")
    return valuation_dir(cfg) / f"{Path(name).stem}_{scan_date}{Path(name).suffix}"


# --------------------------------------------------------------------------
# The EPS cache
# --------------------------------------------------------------------------
# Deliberately caches the **quarterly EPS points, not the P/E path**. Price moves
# daily and reported EPS steps quarterly, so re-deriving a path from a fresh
# panel plus cached earnings is instant -- that split is what makes a warm re-run
# take seconds against ~20 minutes cold.

def load_cache(cfg: dict) -> dict:
    """Ticker -> entry. A missing or unreadable cache is an empty one; the
    entries are re-fetchable by definition, so the safe failure is to refetch."""
    path = eps_cache_path(cfg)
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("rb") as fh:
            data = pickle.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - a bad cache is a cold cache
        log_step("VALUATION", "warn", f"cache unreadable, starting cold: {exc}",
                 cfg=cfg)
        return {}


def save_cache(cfg: dict, cache: dict) -> Path:
    path = eps_cache_path(cfg)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(cache, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)               # atomic, so a kill mid-write cannot truncate
    return path


def _age_days(entry: dict) -> float:
    stamp = pd.to_datetime(entry.get("fetched_at"), errors="coerce", utc=True)
    if pd.isna(stamp):
        return float("inf")
    return (pd.Timestamp.now(tz="UTC") - stamp).total_seconds() / 86400.0


def is_stale(entry: dict, cfg: dict) -> bool:
    """An entry that errored is always stale -- a transient Yahoo failure must
    not pin a ticker out of the table for a week."""
    if not entry or entry.get("error"):
        return True
    max_age = float(section(cfg).get("cache_max_age_days", 7))
    return _age_days(entry) > max_age


def fetch_one(ticker: str, cfg: dict, retries: int = 2,
              backoff_s: float = 1.5) -> dict:
    """One ticker's quarterly EPS points plus today's two multiples.

    Both come off a single `yf.Ticker`, so the forward multiple rides along on a
    pass that had to happen anyway. Retries exist for the same reason
    `universe_scan.collect_one` has them: one ticker failing is one missing row,
    but a throttle at ticker 200 of 903 would silently halve the table.
    """
    import research_collect
    import yfinance as yf

    last_error = None
    for attempt in range(retries + 1):
        try:
            tk = yf.Ticker(ticker)
            eps = research_collect.quarterly_eps(tk)
            points = ([[d.isoformat(), float(v)] for d, v in eps.items()]
                      if eps is not None else [])
            try:
                info = tk.info or {}
            except Exception:  # noqa: BLE001 - the multiples are the optional half
                info = {}
            return {
                "fetched_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"),
                "eps": points,
                "forward_pe": _finite(info.get("forwardPE")),
                "trailing_pe": _finite(info.get("trailingPE")),
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - one ticker never kills the pass
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(backoff_s * (attempt + 1))
    log_step("VALUATION", "failed", f"{ticker}: {last_error}", cfg=cfg)
    return {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "eps": [], "forward_pe": None, "trailing_pe": None,
            "error": last_error}


def _finite(value):
    """A float, or None. Rejects bools, NaN and inf -- the same discipline
    `quality.scalar` applies, for the same reason: a plausible wrong number is
    worse than a missing one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def scan_eps(cfg: dict, tickers: list[str], refresh: bool = False,
             fetch: bool = True) -> dict:
    """Fill the EPS cache for `tickers`, returning it.

    The per-N progress line is not cosmetic: `mcp_tools.backtests.run_script`
    kills a child whose log is still empty after 60s, and this loop runs for
    ~20 minutes cold.
    """
    cache = load_cache(cfg)
    every = max(1, int(section(cfg).get("progress_every", 25)))
    delay = float(section(cfg).get("request_delay_s", 0.2))
    retries = int(section(cfg).get("retries", 2))

    todo = [t for t in tickers if refresh or is_stale(cache.get(t, {}), cfg)]
    log_step("VALUATION", "ok",
             f"{len(tickers)} ticker(s); {len(todo)} to fetch, "
             f"{len(tickers) - len(todo)} cached", cfg=cfg)
    if not fetch:
        if todo:
            log_step("VALUATION", "skip",
                     f"--no-fetch: {len(todo)} stale ticker(s) left as they are",
                     cfg=cfg)
        return cache

    started = time.time()
    for i, ticker in enumerate(todo, start=1):
        cache[ticker] = fetch_one(ticker, cfg, retries=retries)
        if i % every == 0 or i == len(todo):
            rate = (time.time() - started) / i
            left = rate * (len(todo) - i)
            log_step("VALUATION", "ok",
                     f"{i}/{len(todo)} fetched ({rate:.2f}s/ticker, "
                     f"~{left/60:.1f}min left)", cfg=cfg)
            save_cache(cfg, cache)      # checkpoint, so a kill loses ~25 tickers
        if delay and i < len(todo):
            time.sleep(delay)

    save_cache(cfg, cache)
    ok = sum(1 for t in todo if not cache.get(t, {}).get("error"))
    log_step("VALUATION", "ok" if ok == len(todo) else "partial",
             f"fetched {ok}/{len(todo)} in {time.time() - started:.0f}s", cfg=cfg)
    return cache


def load_prices(cfg: dict, tickers: list[str]):
    """One bulk close panel. Returns a (days x tickers) frame, or None.

    Deliberately not `backtest_universe.cached_panel`, for the reason
    `universe_scan.load_prices` records: that pickle is keyed to a fixed
    universe. Bulk download already routes through `drop_unsettled_bars` and
    `drop_open_session_bar`, so both guards apply here for free.
    """
    from scanner_common import download_price_data
    period = str(section(cfg).get("price_period", "5y"))
    interval = cfg.get("data", {}).get("download_interval", "1d")
    try:
        panel = download_price_data(sorted(set(tickers)), period, interval)
        closes = panel["Close"]
        log_step("VALUATION", "ok",
                 f"prices for {closes.shape[1]} ticker(s), {closes.shape[0]} bars",
                 cfg=cfg)
        return closes
    except Exception as exc:  # noqa: BLE001
        log_step("VALUATION", "failed", f"price panel: {exc}", cfg=cfg)
        return None


# --------------------------------------------------------------------------
# Bucketing
# --------------------------------------------------------------------------

def assign_buckets(constituents: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Add `bucket` / `rolled_up` to a constituents frame.

    Bucket is the GICS **sub-industry**; anything thinner than `min_members`
    rolls up to its sector. This is the first thing here to group by
    sub_industry -- `peers.py` buckets by sector only.

    The floor is 6 against `peers.min_peers` 12, and the difference is
    deliberate: peers needs resolution for a within-bucket *percentile rank*,
    this needs only a *central tendency*. At 6 the interesting cases survive
    (Semiconductors 26, Biotechnology 18, Pharmaceuticals 9); at 12
    Pharmaceuticals would silently vanish into Health Care. Don't "harmonise"
    them.
    """
    floor = int(section(cfg).get("min_members", 6))
    field = section(cfg).get("bucket_field", "sub_industry")
    fallback = section(cfg).get("fallback_field", "sector")

    out = constituents.copy()
    for col in (field, fallback):
        if col not in out.columns:
            out[col] = ""
    out[field] = out[field].fillna("").astype(str).str.strip()
    out[fallback] = out[fallback].fillna("").astype(str).str.strip()

    counts = out[out[field] != ""][field].value_counts()
    big = set(counts[counts >= floor].index)

    def resolve(row):
        name = row[field]
        if name and name in big:
            return name, False
        sector = row[fallback]
        return (f"{sector}{ROLLUP_SUFFIX}", True) if sector else ("", False)

    resolved = out.apply(resolve, axis=1, result_type="expand")
    out[BUCKET_COL] = resolved[0]
    out["rolled_up"] = resolved[1]
    return out[out[BUCKET_COL] != ""].reset_index(drop=True)


# --------------------------------------------------------------------------
# The paths
# --------------------------------------------------------------------------

def eps_frame(cache: dict, tickers: list[str], index: pd.DatetimeIndex,
              ) -> pd.DataFrame:
    """(days x tickers) daily TTM EPS, forward-filled from announcement dates.

    Goes through `research_collect.ttm_from_quarterly` rather than rolling here,
    so the per-ticker `pe_percentile_2y` and this industry path can never drift
    apart on what "trailing earnings on day d" means.
    """
    import research_collect

    columns = {}
    for ticker in tickers:
        points = (cache.get(ticker) or {}).get("eps") or []
        if len(points) < 4:
            continue
        series = pd.Series(
            [float(v) for _, v in points],
            index=pd.DatetimeIndex([pd.Timestamp(d) for d, _ in points]),
        ).sort_index()
        ttm = research_collect.ttm_from_quarterly(series, index)
        if ttm is not None:
            columns[ticker] = ttm
    if not columns:
        return pd.DataFrame(index=index)
    return pd.DataFrame(columns, index=index)


def pe_frame(closes: pd.DataFrame, eps: pd.DataFrame) -> pd.DataFrame:
    """(days x tickers) P/E. A non-positive or missing EPS yields NaN.

    A loss-making company has **no** P/E and must never read as cheap -- which
    matters most exactly where this module gets used, since much of
    Biotechnology is loss-making and its member count therefore varies over
    time. Same rule as `quality.has_values`: not measurable is not good news.
    """
    shared = [t for t in closes.columns if t in eps.columns]
    if not shared:
        return pd.DataFrame(index=closes.index)
    px = closes[shared].astype(float)
    ep = eps[shared].astype(float)
    ep = ep.where(ep > 0)
    out = px / ep
    return out.where(np.isfinite(out) & (out > 0))


def bucket_median(pe: pd.DataFrame, members: list[str],
                  min_members: int) -> pd.Series:
    """Daily median P/E across `members`, NaN on days with too few priced ones.

    A median of three is not a thin reading of an industry, it is a different
    measurement -- so it is NaN, never a number, and never forward-filled. Same
    rule `price_risk` applies with `min_obs`.
    """
    cols = [t for t in members if t in pe.columns]
    if not cols:
        return pd.Series(dtype=float, index=pe.index)
    sub = pe[cols]
    med = sub.median(axis=1, skipna=True)
    return med.where(sub.notna().sum(axis=1) >= min_members)


def decompose(closes: pd.DataFrame, eps: pd.DataFrame, members: list[str],
              start, end) -> dict:
    """Split an industry's price move into multiple change and earnings change.

    Returns log changes, because the identity

        price_dlog == multiple_dlog + earnings_dlog

    is additive in logs and holds here **exactly, by construction**: each leg is
    the equal-weight mean of member *log* ratios, and the mean is linear, so the
    multiple leg is both their difference and the mean of the members' own log
    multiple changes. That exactness is the property
    `tests/test_industry_valuation.py` pins -- a sign error here is plausible,
    silent, and would invert the module's entire conclusion.

    **Mean of logs, not mean of ratios** -- i.e. a geometric mean, which is the
    right central tendency for a multiplicative quantity and the reason this is
    readable at all. The first full pass used the arithmetic mean of ratios and
    reported `Real Estate (other)` earnings **+178%** and Health Care REITs
    **+93%**: a REIT whose TTM EPS goes 0.02 -> 0.60 contributes a ratio of 30
    and simply owns the average, and REIT earnings are depreciation-driven and
    routinely do that. In logs the same member contributes 3.4 against a typical
    0.1 -- still the largest reading, which is honest, but no longer the only
    one. Both forms satisfy the identity, so the test could not have caught
    this; only looking at the output did.

    Membership is held constant across the window: a name needs a positive price
    and a positive TTM EPS at **both** endpoints, so a company that swung into
    profit mid-window cannot manufacture an earnings collapse on the way in.
    """
    empty = {"price_dlog": None, "multiple_dlog": None, "earnings_dlog": None,
             "members_n": 0}
    cols = [t for t in members if t in closes.columns and t in eps.columns]
    if not cols or start not in closes.index or end not in closes.index:
        return empty

    p0, p1 = closes.loc[start, cols], closes.loc[end, cols]
    e0, e1 = eps.loc[start, cols], eps.loc[end, cols]
    frame = pd.DataFrame({"p0": p0, "p1": p1, "e0": e0, "e1": e1}).astype(float)
    frame = frame[(frame > 0).all(axis=1) & np.isfinite(frame).all(axis=1)]
    if frame.empty:
        return empty

    price_dlog = float(np.log(frame["p1"] / frame["p0"]).mean())
    earnings_dlog = float(np.log(frame["e1"] / frame["e0"]).mean())
    if not (math.isfinite(price_dlog) and math.isfinite(earnings_dlog)):
        return empty

    return {"price_dlog": price_dlog,
            "earnings_dlog": earnings_dlog,
            "multiple_dlog": price_dlog - earnings_dlog,
            "members_n": int(len(frame))}


def zscore(series: pd.Series, lookback: int, min_history: int) -> dict:
    """Where today's multiple sits in its own trailing history.

    On **log** P/E: multiples are ratio-scaled, so a move from 10x to 20x and one
    from 20x to 40x are the same event and a linear z-score would not say so.
    Returns None rather than a number when the history is too thin.
    """
    out = {"pe_now": None, "pe_median": None, "pe_pctile": None, "pe_z": None,
           "history_days": 0, "pe_asof": ""}
    valid = series.dropna()
    if valid.empty:
        return out
    window = valid.iloc[-lookback:] if lookback > 0 else valid
    out["pe_now"] = float(valid.iloc[-1])
    # The date of the last *measurable* reading, which is not always the last
    # bar: a bucket drops below `min_members` the day too few of its members
    # carry a positive EPS, and its median goes NaN. Office REITs did exactly
    # that on the first live pass -- 5 priced against a floor of 6 -- and
    # reported a `pe_now` from whenever it was last measurable. A stale reading
    # has to name its own date rather than masquerade as today's.
    out["pe_asof"] = pd.Timestamp(valid.index[-1]).date().isoformat()
    out["history_days"] = int(len(window))
    if len(window) < max(2, int(min_history)):
        return out

    logs = np.log(window.astype(float))
    now = float(logs.iloc[-1])
    sd = float(logs.std(ddof=0))
    out["pe_median"] = float(window.median())
    out["pe_pctile"] = round(100.0 * float((window < out["pe_now"]).mean()), 1)
    if sd > 0:
        out["pe_z"] = round((now - float(logs.mean())) / sd, 2)
    return out


def flag_of(pe_z, earnings_dlog, multiple_dlog, threshold: float) -> str:
    """Cheap / rich / neither -- a conjunction, never a z-score alone.

    `cheap` needs the multiple historically low **and** earnings growing over the
    window: a multiple that fell *because* earnings fell is not a dislocation, it
    is correct repricing. `rich` needs the multiple historically high **and**
    reached by expanding rather than by an earnings collapse.

    Not symmetric, deliberately. Forcing symmetry ("rich = high z and earnings
    falling") would miss the case this module was built to catch -- an industry
    whose earnings grew strongly *and* whose multiple ran further still.

    Like `peers.in_sector_tail`, the extra conjunct can only ever make the flag
    quieter than the z-score alone.
    """
    if pe_z is None:
        return FLAG_NONE
    if pe_z <= -abs(threshold) and (earnings_dlog or 0) > 0:
        return FLAG_CHEAP
    if pe_z >= abs(threshold) and (multiple_dlog or 0) > 0:
        return FLAG_RICH
    return FLAG_NONE


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------

def build_table(cfg: dict, buckets: pd.DataFrame, closes: pd.DataFrame,
                cache: dict) -> pd.DataFrame:
    """One row per industry bucket, as of the last bar in `closes`.

    Note the `membership_asof` column: bucket membership is **today's**
    membership applied backwards. S&P rewrites membership at every rebalance --
    which is exactly why `INDEX_COL` is recorded on every signal -- so every
    historical path here carries survivorship bias and the column says as of
    when. Same discipline `universe_scan` applies to its `stage` column.
    """
    sec = section(cfg)
    floor = int(sec.get("min_members", 6))
    lookback = int(sec.get("lookback_days", 756))
    min_history = int(sec.get("min_history_days", 252))
    window = int(sec.get("change_window_days", 126))
    threshold = float(sec.get("z_threshold", 1.0))

    if closes is None or closes.empty:
        return pd.DataFrame(columns=TABLE_COLUMNS)

    index = closes.index
    scan_date = pd.Timestamp(index[-1]).date().isoformat()
    tickers = [t for t in buckets["ticker"] if t in closes.columns]
    eps = eps_frame(cache, tickers, index)
    pe = pe_frame(closes, eps)

    end = index[-1]
    start = index[max(0, len(index) - 1 - window)]

    rows = []
    for name, group in buckets.groupby(BUCKET_COL, sort=True):
        members = [t for t in group["ticker"] if t in closes.columns]
        if not members:
            continue
        med = bucket_median(pe, members, floor)
        stats = zscore(med, lookback, min_history)
        parts = decompose(closes, eps, members, start, end)

        fwd = [_finite((cache.get(t) or {}).get("forward_pe")) for t in members]
        fwd = [v for v in fwd if v is not None and v > 0]
        forward_pe = float(np.median(fwd)) if len(fwd) >= floor else None
        implied = (round(100.0 * (stats["pe_now"] / forward_pe - 1.0), 1)
                   if forward_pe and stats["pe_now"] else None)

        priced = int(pe[[t for t in members if t in pe.columns]]
                     .iloc[-1].notna().sum()) if members else 0

        rows.append({
            "scan_date": scan_date,
            BUCKET_COL: name,
            "sector": (group["sector"].mode().iat[0]
                       if not group["sector"].mode().empty else ""),
            "rolled_up": bool(group["rolled_up"].any()),
            "pe_now": _round(stats["pe_now"], 2),
            "pe_asof": stats["pe_asof"],
            "pe_median": _round(stats["pe_median"], 2),
            "pe_pctile": stats["pe_pctile"],
            "pe_z": stats["pe_z"],
            "price_chg_pct": _pct(parts["price_dlog"]),
            "multiple_chg_pct": _pct(parts["multiple_dlog"]),
            "earnings_chg_pct": _pct(parts["earnings_dlog"]),
            # Deliberately NOT rounded: these three are the exact record, and
            # rounding them to 6 places would break the additive identity the
            # column set exists to make checkable from the CSV itself. The
            # `*_chg_pct` columns above are the readable ones.
            "price_dlog": parts["price_dlog"],
            "multiple_dlog": parts["multiple_dlog"],
            "earnings_dlog": parts["earnings_dlog"],
            "forward_pe": _round(forward_pe, 2),
            "implied_eps_growth_pct": implied,
            "flag": flag_of(stats["pe_z"], parts["earnings_dlog"],
                            parts["multiple_dlog"], threshold),
            "members_n": int(len(members)),
            "members_priced_n": priced,
            "window_members_n": parts["members_n"],
            "history_days": stats["history_days"],
            "membership_asof": scan_date,
        })

    table = pd.DataFrame(rows, columns=TABLE_COLUMNS)
    if table.empty:
        return table
    return table.sort_values("multiple_chg_pct",
                             ascending=True).reset_index(drop=True)


def _round(value, digits):
    return None if value is None else round(float(value), digits)


def _pct(dlog):
    """A log change rendered as a percent, for reading. The identity is additive
    in the `*_dlog` columns; these three add only approximately."""
    return None if dlog is None else round(100.0 * (math.exp(dlog) - 1.0), 2)


def write_table(cfg: dict, table: pd.DataFrame) -> Path:
    scan_date = str(table["scan_date"].iat[0])
    path = table_path(cfg, scan_date)
    table.to_csv(path, index=False)
    log_step("VALUATION", "ok", f"{len(table)} bucket(s) -> {path.name}", cfg=cfg)
    return path


def write_history(cfg: dict, table: pd.DataFrame) -> Path:
    """Append this run's rows to the accumulating record.

    This is the only thing that ever makes **forward** P/E a series: Yahoo serves
    one value with no history, so the series can only be built forward from now.
    De-duplicating on (scan_date, bucket) makes a same-day re-run idempotent for
    free -- the same `merge_history_csv` the signal history uses.
    """
    path = history_csv_path(cfg)
    merge_history_csv(path, table.to_dict("records"), HISTORY_TABLE_KEYS)
    log_step("VALUATION", "ok", f"history -> {path.name}", cfg=cfg)
    return path


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def run(cfg: dict, refresh: bool = False, fetch: bool = True,
        limit: int = 0) -> dict:
    """The full pass. Returns `{table, paths, note}`.

    **Only a full pass writes anything.** `--limit` is a timing probe and writes
    no table, no chart and no history row. `universe_scan` learned this the hard
    way: its date-stamped artifacts meant a thirty-ticker probe silently replaced
    the day's whole-index plane with a plane of thirty. A bucket-level table
    built from a subset is meaningless anyway, so the simple rule -- a subset
    prints and writes nothing -- beats reproducing three artifact scopes.
    """
    if not is_enabled(cfg):
        return {"table": pd.DataFrame(columns=TABLE_COLUMNS), "paths": {},
                "note": f"{CONFIG_KEY}.enabled is false -- nothing ran"}

    constituents = universe_constituents(cfg)
    buckets = assign_buckets(constituents, cfg)
    tickers = sorted(set(buckets["ticker"]))
    partial = bool(limit and limit < len(tickers))
    if partial:
        tickers = tickers[:limit]
        buckets = buckets[buckets["ticker"].isin(tickers)]

    log_step("VALUATION", "ok",
             f"{len(tickers)} ticker(s) in {buckets[BUCKET_COL].nunique()} "
             f"bucket(s)" + (" [probe]" if partial else ""), cfg=cfg)

    cache = scan_eps(cfg, tickers, refresh=refresh, fetch=fetch)
    closes = load_prices(cfg, tickers)
    if closes is None or closes.empty:
        return {"table": pd.DataFrame(columns=TABLE_COLUMNS), "paths": {},
                "note": "no price panel -- nothing computed"}

    table = build_table(cfg, buckets, closes, cache)
    if partial:
        return {"table": table, "paths": {},
                "note": (f"probe over {len(tickers)} ticker(s) -- wrote nothing; "
                         "only a full pass may write the table")}

    paths = {}
    if not table.empty:
        paths["table"] = str(write_table(cfg, table))
        paths["history"] = str(write_history(cfg, table))
        chart = _write_chart(cfg, table)
        if chart:
            paths["chart"] = str(chart)
    return {"table": table, "paths": paths, "note": ""}


def _write_chart(cfg: dict, table: pd.DataFrame):
    """Fail-open: a chart is a rendering of a table that is already on disk."""
    try:
        import charts
        scan_date = str(table["scan_date"].iat[0])
        path = chart_path(cfg, scan_date)
        charts.plot_industry_valuation(
            table, cfg.get("charts", {}), path,
            dpi=int(section(cfg).get("chart_dpi", 120)),
            window_days=int(section(cfg).get("change_window_days", 126)))
        return path
    except Exception as exc:  # noqa: BLE001
        log_step("VALUATION", "warn", f"chart: {exc}", cfg=cfg)
        return None


def read_table(cfg: dict, bucket: str = "") -> pd.DataFrame:
    """The most recent table on disk, optionally one bucket. Never raises."""
    stem = section(cfg).get("table_csv", "industry_valuation")
    try:
        files = sorted(valuation_dir(cfg, create=False).glob(f"{stem}_*.csv"))
    except OSError:
        return pd.DataFrame(columns=TABLE_COLUMNS)
    if not files:
        return pd.DataFrame(columns=TABLE_COLUMNS)
    try:
        table = pd.read_csv(files[-1])
    except Exception:  # noqa: BLE001 - unreadable is the same as absent
        return pd.DataFrame(columns=TABLE_COLUMNS)
    if bucket:
        table = table[table[BUCKET_COL].str.lower() == bucket.lower()]
    return table.reset_index(drop=True)


def members_of(cfg: dict, bucket: str) -> list[str]:
    """The tickers currently in a bucket, by the same rule the table used."""
    buckets = assign_buckets(universe_constituents(cfg), cfg)
    hit = buckets[buckets[BUCKET_COL].str.lower() == bucket.lower()]
    return sorted(hit["ticker"].tolist())


# --------------------------------------------------------------------------
# Recording -- the opt-in write into signals.csv
# --------------------------------------------------------------------------

def record(bucket: str, cfg: dict, top: int = 0) -> dict:
    """Write a bucket's members into `signals.csv` so tier 4 grades them.

    Modelled on `theme_signals.record`, and the order of operations is the
    design:

    1. the section switch is a no-op, not a raise;
    2. every pick is **graded before anything is written** -- `scan_ticker`
       returns the payload in exactly the `latest_hits.json` shape, so the row
       carries the badge, the veto, both plane axes and `Index` computed *after*
       the pick, and a name with no bars is refused rather than written into a
       table tier 4 buys from;
    3. the batch is **atomic** -- an industry is a cohort, so half of one on the
       record is misleading rather than partial;
    4. `protect=VERDICT_COLS` so a re-record can never erase a verdict tier 3
       wrote hours later.
    """
    import run_scanners

    if not is_enabled(cfg):
        return {"recorded": False, "picks": 0,
                "note": f"{CONFIG_KEY}.enabled is false -- nothing was written"}

    table = read_table(cfg, bucket)
    if table.empty:
        return {"recorded": False, "picks": 0, "problems": [
            f"no recorded row for {bucket!r} -- run a full scan first"]}
    row = table.iloc[0]

    members = members_of(cfg, bucket)
    cap = int(section(cfg).get("max_picks_per_run", 10))
    limit = min(top or cap, cap)
    if not members:
        return {"recorded": False, "picks": 0,
                "problems": [f"{bucket!r} has no members"]}
    if len(members) > limit:
        members = members[:limit]

    graded, problems = [], []
    for ticker in members:
        try:
            payload = run_scanners.scan_ticker(ticker, cfg)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{ticker}: could not be scanned ({exc})")
            continue
        merged, fired = {}, []
        for screen in payload.get("screens") or []:
            hit = (screen.get("hits") or {}).get(ticker.upper())
            if hit is None:
                continue
            key = screen.get("config_key")
            if key and key != run_scanners.NO_SIGNAL_KEY:
                fired.append(key)
            merged.update(hit)
        scan_date = str(payload.get("scan_date") or "")
        if not merged or not scan_date:
            problems.append(f"{ticker}: refused -- no bars to grade it on")
            continue
        graded.append((ticker, merged, scan_date, ",".join(fired)))

    if problems:
        return {"recorded": False, "picks": 0, "problems": problems,
                "note": "nothing was written -- the batch is atomic"}

    index_map = _index_map(cfg)
    rows = []
    for ticker, merged, scan_date, fired in graded:
        rows.append({
            # Lists as JSON, exactly as `history_rows` writes them: a bare list
            # reaches the CSV as Python repr, which `ledger._as_list` cannot read
            # back, so a vetoed pick would lose its veto on the position.
            **{k: (json.dumps(v) if isinstance(v, list) else v)
               for k, v in merged.items()},
            "scan_date": scan_date,
            "config_key": CONFIG_KEY,
            "ticker": ticker,
            "Setup": SETUP_VALUE,
            "Missing": "",
            "Trigger": fired or TRIGGER_NONE,
            "Industry": row[BUCKET_COL],
            "Industry Flag": row["flag"],
            "Industry PE Z": row["pe_z"],
            "Industry Multiple Chg %": row["multiple_chg_pct"],
            "Industry Earnings Chg %": row["earnings_chg_pct"],
            INDEX_COL: index_map.get(ticker, ""),
        })

    path = signals_csv_path(cfg)
    merge_history_csv(path, rows, HISTORY_KEYS, protect=VERDICT_COLS)
    log_step("VALUATION", "ok",
             f"{row[BUCKET_COL]}: {len(rows)} pick(s) -> {path.name}", cfg=cfg)
    return {"recorded": True, "picks": len(rows), "bucket": row[BUCKET_COL],
            "flag": row["flag"], "tickers": [t for t, _, _, _ in graded],
            "signals_csv": str(path), "note": ""}


def _index_map(cfg: dict) -> dict:
    """ticker -> index_name, for the `Index` column. Fail-open to blank: S&P
    rewrites membership at every rebalance, so this is not reconstructable
    later -- but losing it must not cost the whole record."""
    try:
        frame = universe_constituents(cfg)
        return dict(zip(frame["ticker"], frame["index_name"]))
    except Exception as exc:  # noqa: BLE001
        log_step("VALUATION", "warn", f"index map: {exc}", cfg=cfg)
        return {}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cmd_scan(args: list[str], cfg: dict) -> int:
    refresh = "--refresh" in args
    fetch = "--no-fetch" not in args
    limit = 0
    if "--limit" in args:
        try:
            limit = int(args[args.index("--limit") + 1])
        except (IndexError, ValueError):
            print("usage: scan [--refresh] [--no-fetch] [--limit N]")
            return 2
    result = run(cfg, refresh=refresh, fetch=fetch, limit=limit)
    table = result["table"]
    if table.empty:
        print(result.get("note") or "nothing computed")
        return 1
    cols = [BUCKET_COL, "pe_now", "pe_z", "multiple_chg_pct",
            "earnings_chg_pct", "price_chg_pct", "forward_pe", "flag",
            "members_priced_n"]
    print(table[cols].to_string(index=False))
    if result.get("note"):
        print(f"\n{result['note']}")
    for kind, path in (result.get("paths") or {}).items():
        print(f"{kind}: {path}")
    return 0


def _cmd_show(args: list[str], cfg: dict) -> int:
    if not args:
        print("usage: show <BUCKET>")
        return 2
    bucket = " ".join(args)
    table = read_table(cfg, bucket)
    if table.empty:
        print(f"no recorded row for {bucket!r}")
        return 1
    row = table.iloc[0]
    for col in TABLE_COLUMNS:
        print(f"{col:26s} {row[col]}")
    print("\nmembers: " + ", ".join(members_of(cfg, bucket)))
    return 0


def _cmd_record(args: list[str], cfg: dict) -> int:
    if not args:
        print("usage: record <BUCKET> [--top N]")
        return 2
    top = 0
    if "--top" in args:
        try:
            top = int(args[args.index("--top") + 1])
            args = args[:args.index("--top")]
        except (IndexError, ValueError):
            print("usage: record <BUCKET> [--top N]")
            return 2
    result = record(" ".join(args), cfg, top=top)
    if not result.get("recorded"):
        for problem in result.get("problems") or [result.get("note", "")]:
            print(problem)
        return 1
    print(f"recorded {result['picks']} pick(s) from {result['bucket']} "
          f"({result['flag'] or 'no flag'}): {', '.join(result['tickers'])}")
    print(result["signals_csv"])
    return 0


def main(argv: list[str]) -> int:
    from scanner_common import enable_utf8_output, load_config
    enable_utf8_output()
    cfg = load_config()
    command = argv[0] if argv else ""
    if command == "scan":
        return _cmd_scan(argv[1:], cfg)
    if command == "show":
        return _cmd_show(argv[1:], cfg)
    if command == "record":
        return _cmd_record(argv[1:], cfg)
    print(__doc__)
    print("usage:\n"
          "  python industry_valuation.py scan [--refresh] [--no-fetch] [--limit N]\n"
          "  python industry_valuation.py show <BUCKET>\n"
          "  python industry_valuation.py record <BUCKET> [--top N]")
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
