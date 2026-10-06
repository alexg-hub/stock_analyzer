"""A per-ticker pickle cache, filled by a slow, retrying, checkpointed loop.

`universe_scan` (fast-stage fundamentals) and `industry_valuation` (quarterly
EPS) both fetch one ticker at a time across ~900 names. Any one fetch can fail,
so freshness is judged per ticker, an errored entry is always stale, and the
loop checkpoints every N tickers so a kill costs N tickers rather than the run.

The progress line every N tickers is not cosmetic: `mcp_tools.backtests`
kills a child whose log stays empty for 60 s, and a 15-minute loop that logs
only at the end looks exactly like a stalled interpreter.
"""

import pickle
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from scanner_common import log_step


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path, phase: str, cfg: dict) -> dict:
    """Ticker -> entry. Missing or unreadable is empty: never raises, because a
    corrupt pickle must cost the cache, not the run, and every entry can be
    fetched again."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("rb") as fh:
            data = pickle.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - a bad cache is a cold cache
        log_step(phase, "warn", f"cache unreadable, starting cold: {exc}", cfg=cfg)
        return {}


def save(path: Path, cache: dict) -> Path:
    """Write atomically, so a kill mid-write cannot truncate the cache."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(cache, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)
    return path


def is_stale(entry: dict, max_age_days: float, stamp_key: str) -> bool:
    """Whether a ticker needs fetching. An errored entry is always stale, so a
    transient Yahoo failure cannot pin a ticker out of the table for a week."""
    if not entry or entry.get("error"):
        return True
    stamp = pd.to_datetime(entry.get(stamp_key), errors="coerce", utc=True)
    if pd.isna(stamp):
        return True
    age = (pd.Timestamp.now(tz="UTC") - stamp).total_seconds() / 86400.0
    return age > max_age_days


def with_retries(fn, retries: int, backoff_s: float = 1.5):
    """`(result, None)` on success, `(None, error)` after `retries` more tries.

    Retries exist for volume, not because a throttle was observed: one ticker
    failing is one missing row, but a throttle at ticker 200 of 900 would
    silently halve the table.
    """
    last_error = None
    for attempt in range(retries + 1):
        try:
            return fn(), None
        except Exception as exc:  # noqa: BLE001 - one ticker never kills the pass
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(backoff_s * (attempt + 1))
    return None, last_error


def fill(path: Path, tickers: list[str], fetch_one, cfg: dict, *, phase: str,
         section: dict, stamp_key: str, refresh: bool = False,
         fetch: bool = True, prepare=None, default_every: int = 10) -> dict:
    """Bring the cache at `path` up to date for `tickers`; return it.

    `fetch_one(ticker, retries, context)` returns the entry for one ticker.
    `prepare(todo)` runs once before the loop with the tickers actually being
    fetched -- the bulk price download -- and its result is passed to every
    `fetch_one` as `context`. `section` supplies `cache_max_age_days`,
    `progress_every`, `request_delay_s` and `retries`.
    """
    cache = load(path, phase, cfg)
    max_age = float(section.get("cache_max_age_days", 7))
    every = max(1, int(section.get("progress_every", default_every)))
    delay = float(section.get("request_delay_s", 0.2))
    retries = int(section.get("retries", 2))

    todo = [t for t in tickers
            if refresh or is_stale(cache.get(t, {}), max_age, stamp_key)]
    log_step(phase, "ok", f"{len(tickers)} ticker(s); {len(todo)} to fetch, "
             f"{len(tickers) - len(todo)} cached", cfg=cfg)
    if not fetch:
        if todo:
            log_step(phase, "skip", f"--no-fetch: {len(todo)} stale ticker(s) "
                     "left as they are", cfg=cfg)
        return cache

    context = prepare(todo) if (prepare and todo) else None
    started = time.time()
    for i, ticker in enumerate(todo, start=1):
        cache[ticker] = fetch_one(ticker, retries, context)
        if i % every == 0 or i == len(todo):
            rate = (time.time() - started) / i
            left = rate * (len(todo) - i)
            log_step(phase, "ok", f"{i}/{len(todo)} fetched ({rate:.2f}s/ticker, "
                     f"~{left / 60:.1f}min left)", cfg=cfg)
            save(path, cache)
        if delay and i < len(todo):
            time.sleep(delay)

    save(path, cache)
    ok = sum(1 for t in todo if not cache.get(t, {}).get("error"))
    log_step(phase, "ok" if ok == len(todo) else "partial",
             f"fetched {ok}/{len(todo)} in {time.time() - started:.0f}s", cfg=cfg)
    return cache
