"""Sector peer statistics: the distribution a metric is read against.

Every threshold in the `quality` registry is an **absolute** anchor, and several
are only meaningful against a peer group. Measured on the first full universe
pass (2026-08-10), that produced a systematic sector bias rather than a company
judgment:

  * **Utilities were excluded at 83.9%** (26 of 31) and scored the *highest* mean
    risk of any sector, despite being among the least volatile equities there
    are. Six of the seven utilities in the virtual portfolio tripped the identical
    trio -- Altman Z, consecutive negative free cash flow, cash runway. Regulated
    utilities carry heavy debt against rate-based cash flows *by design*.
  * Financials were 42% of the low-risk/high-reward quadrant against 15% of the
    index, largely because banks trade at structurally low P/E and carry high
    accounting ROE.

This module supplies the missing half of the comparison: for a parameter and a
sector, where does this company sit *among its peers*. Three rules:

  * **It reads, it never fetches.** The distribution is built from
    `universe_scan`'s cache, which already holds resolved values for every
    constituent. No network, no statements, no second collection path.
  * **Insufficient evidence means fall back**, never guess. A sector with fewer
    than `min_peers` measured values returns None and the caller uses the
    absolute anchor it always did. That keeps a fresh install, a thin sector and
    a missing cache all behaving exactly as before this module existed.
  * **A percentile is a rank, not a verdict.** Ranking guarantees somebody is
    bottom of every sector, so a pure percentile veto would exclude a fixed share
    of the index forever and stop meaning "visibly falling over". The veto
    therefore uses the percentile as a **second** condition on top of the
    absolute threshold (see `quality._gate_ok`), which can only ever make the
    exclusion quieter.
"""

import json
from pathlib import Path

import pandas as pd

from scanner_common import log_step, output_dir

CONFIG_KEY = "peers"

#: What a sector needs before its distribution is trusted at all. S&P 500 sectors
#: run from 21 (Energy) to 83 (Industrials) names, so 12 keeps every sector usable
#: while refusing to rank a company against three peers.
DEFAULT_MIN_PEERS = 12


def section(cfg: dict) -> dict:
    return (cfg.get("quality") or {}).get(CONFIG_KEY) or {}


def is_enabled(cfg: dict) -> bool:
    return bool(section(cfg).get("enabled", False))


def min_peers(cfg: dict) -> int:
    return int(section(cfg).get("min_peers", DEFAULT_MIN_PEERS))


def veto_percentile(cfg: dict) -> float:
    """How far into its sector's tail a value must sit to confirm a veto.

    0.10 means "also in the worst 10% of its peer group". Applied as a second
    condition on an absolute breach, never on its own.
    """
    return float(section(cfg).get("veto_percentile", 0.10))


def stats_path(cfg: dict) -> Path:
    configured = Path(section(cfg).get("path", "universe/peer_stats.json"))
    return configured if configured.is_absolute() else output_dir() / configured


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------

def build(cache: dict, sectors: dict, cfg: dict) -> dict:
    """Sorted value lists per (parameter, sector), plus the ticker->sector map.

    `cache` is `universe_scan`'s cache (ticker -> entry with `values`), `sectors`
    a ticker -> sector mapping. Only numeric scalars are collected; a series or a
    flag is skipped, because ranking a 0/1 flag says nothing.
    """
    import quality

    buckets: dict[str, dict[str, list]] = {}
    for ticker, entry in (cache or {}).items():
        sector = (sectors.get(ticker) or "").strip()
        if not sector or (entry or {}).get("error"):
            continue
        specs = quality.parameters(cfg, None)
        for key, raw in ((entry or {}).get("values") or {}).items():
            spec = specs.get(key)
            if spec is None:
                continue
            value = quality.scalar(raw, spec)
            if value is None:
                continue
            buckets.setdefault(key, {}).setdefault(sector, []).append(float(value))

    return {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
        "tickers": len(cache or {}),
        "sector_of": {t: s for t, s in sectors.items() if s},
        "distributions": {key: {sector: sorted(vals)
                                for sector, vals in by_sector.items()}
                          for key, by_sector in buckets.items()},
    }


def write(stats: dict, cfg: dict) -> Path:
    path = stats_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(stats), encoding="utf-8")
    tmp.replace(path)                 # atomic: a kill mid-write cannot truncate
    n = sum(len(v) for v in stats.get("distributions", {}).values())
    log_step("PEERS", "ok",
             f"{len(stats.get('distributions', {}))} parameter(s) x {n} "
             f"sector bucket(s) from {stats.get('tickers', 0)} ticker(s)",
             cfg=cfg)
    return path


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

_CACHE: dict = {}


def load(cfg: dict) -> dict:
    """The stats file, memoised per path. Missing or corrupt reads as empty.

    Never raises: without peer stats every caller falls back to the absolute
    anchors, which is exactly the behaviour that predates this module.
    """
    path = stats_path(cfg)
    key = str(path)
    if key in _CACHE:
        return _CACHE[key]
    stats: dict = {}
    try:
        if path.exists() and path.stat().st_size:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                stats = loaded
    except Exception as exc:  # noqa: BLE001 - absence is a supported state
        log_step("PEERS", "warn", f"peer stats unreadable: {exc}", cfg=cfg)
    _CACHE[key] = stats
    return stats


def reset_cache() -> None:
    """Drop the memoised stats. For tests, and after a rebuild in-process."""
    _CACHE.clear()


def sector_of(ticker: str, cfg: dict) -> str:
    return ((load(cfg).get("sector_of") or {}).get((ticker or "").upper()) or "")


def distribution(key: str, sector: str, cfg: dict) -> list | None:
    """The sorted peer values for one parameter in one sector, or None.

    None means "not enough evidence to rank against" and is returned for a
    missing stats file, an unknown sector, an uncollected parameter and a thin
    bucket alike -- every one of which must fall back rather than guess.
    """
    if not sector:
        return None
    values = ((load(cfg).get("distributions") or {}).get(key) or {}).get(sector)
    if not values or len(values) < min_peers(cfg):
        return None
    return values


def percentile(key: str, sector: str, value, cfg: dict) -> float | None:
    """Where `value` sits in its sector, 0.0 (lowest) to 1.0 (highest).

    **Ties take the midpoint of the range they occupy**, which is the standard
    percentile rank and not a refinement. Counting "peers at or below" instead
    hands every member of a tied cluster the *top* of the range, and several of
    these metrics are coarse small integers where that is catastrophic:
    `fcf_negative_years` is capped at the statement window, so 20 of 31 utilities
    hold the identical worst value 4.0. Under at-or-below all twenty ranked 1.00
    and every one of them read as "in the worst 10% of its sector" -- the exact
    sector-wide false positive this module exists to remove. At the midpoint they
    rank 0.68 and none of them does.
    """
    values = distribution(key, sector, cfg)
    if values is None or value is None:
        return None
    import bisect
    target = float(value)
    below = bisect.bisect_left(values, target)
    at_or_below = bisect.bisect_right(values, target)
    return ((below + at_or_below) / 2) / len(values)


def relative_score(key: str, spec: dict, sector: str, value, cfg: dict
                   ) -> float | None:
    """A 0-1 score from the peer rank, oriented by the parameter's own anchors.

    Direction comes from the *ordering* of `good` and `bad`, exactly as
    `quality.normalize` takes it, so an inverted metric needs no special case
    here either. Returns None whenever the rank is unavailable, and the caller
    then uses the absolute anchors.
    """
    anchors = spec.get("score") or {}
    good, bad = anchors.get("good"), anchors.get("bad")
    if good is None or bad is None or good == bad:
        return None
    rank = percentile(key, sector, value, cfg)
    if rank is None:
        return None
    return rank if good > bad else 1.0 - rank


def in_sector_tail(key: str, spec: dict, sector: str, value, cfg: dict
                   ) -> bool | None:
    """Is this value in the worst `veto_percentile` of its sector?

    "Worst" follows the gate, not the score anchors, because a veto's direction
    lives in its `min`/`max`: a `min` gate is breached from below, a `max` gate
    from above. Returns None when the sector cannot be ranked, which the caller
    reads as "no peer opinion" and falls back to the absolute verdict alone.
    """
    rank = percentile(key, sector, value, cfg)
    if rank is None:
        return None
    gate = spec.get("gate") or {}
    cut = veto_percentile(cfg)
    if gate.get("min") is not None:
        return rank <= cut                     # low is bad
    if gate.get("max") is not None:
        return rank >= 1.0 - cut               # high is bad
    return None
