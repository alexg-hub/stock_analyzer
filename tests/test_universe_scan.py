"""Offline -- the risk/reward plane's scoping, cache and quadrant rules.

No network: `quality.collect` is stubbed, so what is exercised is the plumbing
around it -- which is where the bugs were.

Invariants, not recorded output:

  * **A scoped run cannot touch the shared artifacts.** The filenames are
    date-stamped, so a ten-ticker run used to resolve to exactly the same paths
    as the 503-ticker pass and replaced the day's whole-index table, scatter and
    page without an error. This is the check that would have caught it.
  * **Cache freshness is per ticker.** One stale or failed name must not force
    499 good ones to be refetched, and a failure must not pin a ticker out of the
    table until the age threshold expires.
  * **`--from-signals` reads what the screens actually flagged**, de-duplicated
    across screens and nights, and a quiet window yields nothing rather than
    re-rendering a previous window's page over the current one.
  * **An unmeasurable axis is never a position.** `None` must land in `unknown`,
    never in the buy quadrant -- the failure mode where a company nobody could
    grade gets filed as a buy.
"""

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from _harness import Checks, config

import quality
import scanner_common
import universe_scan

c = Checks("universe plane")

cfg = config()
tmp = Path(tempfile.mkdtemp(prefix="universe_test_"))
cfg["universe"] = dict(cfg.get("universe") or {})
cfg["universe"]["dir"] = str(tmp)
cfg["universe"]["request_delay_s"] = 0
cfg["universe"]["progress_every"] = 100

# --------------------------------------------------------------------------
c.section("scope decides which files a run may write")

paths = {scope: universe_scan.artifact_paths(cfg, scope)
         for scope in (universe_scan.SCOPE_UNIVERSE,
                       universe_scan.SCOPE_SIGNALS,
                       universe_scan.SCOPE_SUBSET)}

for kind in ("csv", "png", "html"):
    names = {scope: p[kind].name for scope, p in paths.items()}
    c.ok(f"the three scopes write different {kind} names",
         len(set(names.values())) == 3, json.dumps(names))

c.ok("the universe scope keeps the configured filenames",
     paths[universe_scan.SCOPE_UNIVERSE]["png"].name
     == cfg["universe"].get("chart_path", "risk_reward.png"),
     "the MCP tools glob it and the published page is built from it")

c.ok("every scope writes inside the universe directory",
     all(p[k].parent == universe_scan.universe_dir(cfg)
         for p in paths.values() for k in ("csv", "png", "html")))

# The regression itself, stated as the thing that used to happen.
c.ok("a subset run cannot resolve to the universe table",
     paths[universe_scan.SCOPE_SUBSET]["csv"]
     != paths[universe_scan.SCOPE_UNIVERSE]["csv"],
     "same-day subset and full runs shared this path and the subset won")

# --------------------------------------------------------------------------
c.section("the cache is fresh or stale per ticker, never wholesale")

now = datetime.now(timezone.utc)


def entry(age_days=0.0, error=None, reward=70.0, risk=10.0):
    return {"collected_at": (now - timedelta(days=age_days)).isoformat(),
            "stage": "fast", "values": {}, "groups": {},
            "reward": reward, "risk": risk,
            # None, not 100 - None: an unmeasured axis has no complement either,
            # and computing one here is the same trap `_one_minus` closed in
            # derived.py.
            "safety": None if risk is None else 100 - risk,
            "vetoed": False, "veto_reasons": [], "error": error}


max_age = float(cfg["universe"].get("cache_max_age_days", 7))
c.ok("a fresh entry is not stale", not universe_scan.is_stale(entry(0.5), cfg))
c.ok("an entry past the age limit is stale",
     universe_scan.is_stale(entry(max_age + 1), cfg))
c.ok("an entry that errored is ALWAYS stale, whatever its age",
     universe_scan.is_stale(entry(0.0, error="boom"), cfg),
     "a transient failure must not pin a ticker out for a week")
c.ok("a missing entry is stale", universe_scan.is_stale({}, cfg))

universe_scan.save_cache(cfg, {"AAA": entry(0.1), "BBB": entry(99.0)})
roundtrip = universe_scan.load_cache(cfg)
c.ok("the cache round-trips", set(roundtrip) == {"AAA", "BBB"})
c.ok("...and only the stale member reads as stale",
     not universe_scan.is_stale(roundtrip["AAA"], cfg)
     and universe_scan.is_stale(roundtrip["BBB"], cfg))

bad = universe_scan.cache_path(cfg)
bad.write_bytes(b"not a pickle at all")
c.ok("an unreadable cache is a cold cache, not a crash",
     universe_scan.load_cache(cfg) == {})

# --------------------------------------------------------------------------
c.section("--from-signals reads what the screens flagged")

hist = tmp / "history"
hist.mkdir(exist_ok=True)
sig_cfg = json.loads(json.dumps(cfg))
sig_cfg["research"] = dict(sig_cfg.get("research") or {})
sig_cfg["research"]["history"] = {"enabled": True, "dir": str(hist),
                                  "csv": "signals.csv",
                                  "on_demand_csv": "on_demand.csv"}
sig_cfg["universe"] = cfg["universe"]

today = pd.Timestamp.today().normalize()


def write_signals(rows):
    pd.DataFrame(rows).to_csv(hist / "signals.csv", index=False)


write_signals([
    {"scan_date": (today - pd.Timedelta(days=1)).date().isoformat(),
     "config_key": "breakout_strategy", "ticker": "NEW1"},
    {"scan_date": (today - pd.Timedelta(days=1)).date().isoformat(),
     "config_key": "pullback_strategy", "ticker": "NEW1"},   # two screens
    {"scan_date": (today - pd.Timedelta(days=3)).date().isoformat(),
     "config_key": "breakout_strategy", "ticker": "NEW2"},
    {"scan_date": (today - pd.Timedelta(days=40)).date().isoformat(),
     "config_key": "breakout_strategy", "ticker": "OLD1"},
])

picked = universe_scan.signal_tickers(sig_cfg, 7)
c.ok("only tickers inside the window are picked",
     picked == ["NEW1", "NEW2"], f"{picked}")
c.ok("a ticker that fired on two screens appears once",
     picked.count("NEW1") == 1)
c.ok("newest first, so a truncated run grades the freshest",
     picked[0] == "NEW1")
c.ok("a wider window reaches further back",
     "OLD1" in universe_scan.signal_tickers(sig_cfg, 60))

write_signals([])
c.ok("a quiet window yields nothing rather than raising",
     universe_scan.signal_tickers(sig_cfg, 7) == [],
     "and main() returns 0 without rewriting last window's page")
(hist / "signals.csv").write_bytes(b"")
c.ok("a zero-byte signals.csv is 'nothing recorded yet'",
     universe_scan.signal_tickers(sig_cfg, 7) == [],
     "a night with no signals writes exactly that file")

# --------------------------------------------------------------------------
c.section("quadrants, and the axis that could not be measured")

reward_min, risk_max = quality.quadrant_thresholds(cfg)
cases = [
    (reward_min + 5, risk_max - 5, quality.QUADRANT_BUY),
    (reward_min + 5, risk_max + 5, quality.QUADRANT_SPECULATIVE),
    (reward_min - 5, risk_max - 5, quality.QUADRANT_DULL),
    (reward_min - 5, risk_max + 5, quality.QUADRANT_AVOID),
    (reward_min, risk_max, quality.QUADRANT_BUY),          # inclusive bounds
]
for reward, risk, expected in cases:
    got = quality.quadrant_of(reward, risk, cfg)
    c.ok(f"reward {reward:g} / risk {risk:g} -> {expected}", got == expected,
         f"got {got}")

for reward, risk in ((None, 10.0), (70.0, None), (None, None)):
    got = quality.quadrant_of(reward, risk, cfg)
    c.ok(f"reward={reward} risk={risk} is unknown, never buy",
         got == quality.QUADRANT_UNKNOWN, f"got {got}")

# The table builder has to carry that through rather than dropping the row.
built = universe_scan.build_table(
    cfg,
    {"MEAS": entry(reward=70.0, risk=10.0),
     "UNMEAS": entry(reward=None, risk=None)},
    pd.DataFrame({"ticker": ["MEAS", "UNMEAS"], "sector": ["Tech", "Tech"],
                  "sub_industry": ["", ""]}))
c.ok("an unmeasurable ticker is still a row", len(built) == 2)
c.ok("...marked unknown, and sorted after the real positions",
     built.iloc[-1]["ticker"] == "UNMEAS"
     and built.iloc[-1]["quadrant"] == quality.QUADRANT_UNKNOWN,
     built[["ticker", "quadrant"]].to_dict("records").__str__())
c.ok("a failed ticker yields a row with its error, not a missing row",
     len(universe_scan.build_table(
         cfg, {"BOOM": entry(error="HTTPError")},
         pd.DataFrame({"ticker": ["BOOM"], "sector": [""],
                       "sub_industry": [""]}))) == 1)

# --------------------------------------------------------------------------
c.section("the renderers survive every shape")

for name, frame in (("empty", universe_scan.build_table(cfg, {}, pd.DataFrame())),
                    ("one row", built.head(1)),
                    ("all unmeasurable", built[built["reward"].isna()])):
    html = tmp / f"probe_{name.replace(' ', '_')}.html"
    try:
        universe_scan.write_html(frame, cfg, html)
        ok = html.exists() and html.stat().st_size > 0
        detail = f"{html.stat().st_size} bytes"
    except Exception as exc:                              # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    c.ok(f"the page renders for a {name} table", ok, detail)

c.ok("the payload is JSON with no NaN, which no browser can parse",
     "NaN" not in universe_scan.html_payload(built, cfg)[0])

# --------------------------------------------------------------------------
c.section("many indices, one universe -- and `alert` gates only the alert")

# Stubbed at the parse boundary, so everything above it -- the source list, the
# concatenation, the de-duplication and the gate -- is the real code.
INDEX_TABLES = {
    "big://ok": pd.DataFrame({"ticker": ["AAA", "BBB", "DUP"],
                              "sector": ["Tech", "Tech", "Energy"],
                              "sub_industry": ["", "", ""]}),
    "mid://ok": pd.DataFrame({"ticker": ["CCC", "DUP"],
                              "sector": ["Health", "Energy"],
                              "sub_industry": ["", ""]}),
}

def _index_table(url):
    if url not in INDEX_TABLES:
        raise RuntimeError(f"404 {url}")
    return INDEX_TABLES[url].copy()


scanner_common._index_table = _index_table


def sources_cfg(*entries):
    return {"data": {"universe_sources": [
        dict(zip(("name", "url", "alert"), e)) for e in entries]}}


two = sources_cfg(("big", "big://ok", True), ("mid", "mid://ok", False))
alerted = scanner_common.universe_tickers(two, alert_only=True)
graded = scanner_common.universe_tickers(two)

# The invariant the whole measure-first rollout rests on: a source held back
# from the alert must still be graded by everything that measures, or holding
# it back would mean never learning whether it was worth alerting.
c.ok("a source with alert:false never reaches the nightly alert",
     "CCC" not in alerted, f"alerted={alerted}")
c.ok("...but is still graded by the plane, the peers and the backtest",
     "CCC" in graded, f"graded={graded}")
c.ok("an alerting source is in both", "AAA" in alerted and "AAA" in graded)

frame = scanner_common.universe_constituents(two)
dup = frame[frame["ticker"] == "DUP"]
c.ok("a ticker listed by two indices appears exactly once", len(dup) == 1,
     f"{len(dup)} row(s)")
c.ok("...and keeps the first source's index_name",
     not dup.empty and dup.iloc[0]["index_name"] == "big",
     "a duplicate would be downloaded, screened and peer-ranked twice")

# Fail-open, per source: losing the mid-caps must never cost the S&P 500 scan.
half = scanner_common.universe_constituents(
    sources_cfg(("dead", "gone://404", True), ("big", "big://ok", True)))
c.ok("an unreadable index is skipped and the others still run",
     set(half["ticker"]) == {"AAA", "BBB", "DUP"}, f"{list(half['ticker'])}")

none = scanner_common.universe_constituents(sources_cfg(("dead", "x://404", True)))
c.ok("every source failing yields an empty frame rather than an exception",
     none.empty and list(none.columns)[:2] == ["ticker", "sector"],
     "the download that follows raises, which is loud and honest")

legacy = scanner_common.universe_constituents({"data": {"sp500_source_url": "big://ok"}})
c.ok("the older single-URL config still resolves, and alerts",
     len(legacy) == 3 and bool(legacy["alert"].all()),
     "an archived config must keep working")

# `--limit` is a timing probe; reading it off the top of the concatenation
# would time the first index only and say nothing about the second.
probe = universe_scan._limited(frame, 2)
c.ok("--limit samples across every index, not off the top",
     probe["index_name"].nunique() == 2, f"{list(probe['index_name'])}")
c.ok("--limit never returns more than asked", len(probe) == 2, f"{len(probe)}")

# A limited run is a subset and must be scoped like one, or the timing probe
# republishes the day's whole-index plane -- and rebuilds peer_stats.json,
# which only a full pass may ever narrow -- from thirty names.
src = Path(universe_scan.__file__).read_text(encoding="utf-8")
_, _, after_limit = src.partition("if args.limit:")
c.ok("a --limit run is scoped SCOPE_SUBSET, not SCOPE_UNIVERSE",
     "scope = SCOPE_SUBSET" in after_limit.split("tickers = constituents")[0],
     "it kept the universe scope until 2026-08-13")

shutil.rmtree(tmp, ignore_errors=True)
raise SystemExit(c.finish())
