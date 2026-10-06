"""Offline -- the thematic screen: AI names the candidate, Python grades it.

This is the first surface here where a model chooses *what to look at* rather
than judging something a screen already found, so the boundary needs pinning
from both sides. Every check below defends one of five properties, and every one
of them fails silently if it breaks:

  * **The record carries no number.** `validate` rejects `conviction`, `tier`,
    `score`, `Verdict`, `Reward`, `Risk` and `price_target` by name. Everything
    numeric on a theme row is computed by the registry after the pick.
  * **A pick is proved tradeable before it is recorded.** The theme graph returns
    Milan, Copenhagen and NSE listings, and a model can invent a symbol outright;
    a name with no bars must be refused, not written into a table tier 4 buys
    from.
  * **The batch is atomic.** A theme is a chain, and half a chain on the record
    is a misleading cohort rather than a partial answer.
  * **It is in NEITHER screen registry.** `backtest_universe.SCREENS` is the
    load-bearing one: `_harness.screens()` reads it, and `test_signal_contract`
    then demands a full-history mask that reproduces itself under bar-drop
    perturbation. A list dated today cannot supply one, and a faked one would be
    backtested as though it were real.
  * **A re-record cannot erase a verdict.** Same `protect=VERDICT_COLS` trap the
    tier-3 carry already has a test for.

Nothing here downloads: `scan_ticker` and the index lookup are both stubbed, and
every path is redirected into a temp directory.
"""

import copy
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from _harness import Checks, config

import newsfeed
import run_scanners
import scanner_common
import theme_signals
from portfolio_sim import ledger

c = Checks("thematic screen")

TMP = Path(tempfile.mkdtemp(prefix="theme_test_"))
cfg = copy.deepcopy(config())
cfg["theme_screen"] = {
    "enabled": True, "dir": str(TMP), "csv": str(TMP / "themes.csv"),
    "min_sources": 2, "max_picks_per_run": 8, "lookback_days": 14,
    "themes": [{"name": "datacenter", "query": "data center investment",
                "chain": ["operator", "engineering", "equipment"]},
               {"name": "biopharma", "query": "outbreak vaccine",
                "chain": ["developer", "cdmo"]}],
}
cfg.setdefault("research", {}).setdefault("history", {}).update(
    dir=str(TMP), csv=str(TMP / "signals.csv"),
    on_demand_csv=str(TMP / "on_demand.csv"))
cfg.setdefault("portfolio", {}).update(
    enabled=True, dir=str(TMP), positions_csv=str(TMP / "positions.csv"))

# Seed the news cache so `snapshot_evidence` exercises its real path without
# downloading. `_is_fresh` keys off `fetched_at`, so a current stamp means
# `fetch` never reaches the network.
_NOW = datetime.now(timezone.utc)
_SAME = "Operator's Louisiana data center investment to reach $50 billion"
(TMP / "news_cache.json").write_text(json.dumps({"datacenter": {
    "fetched_at": _NOW.isoformat(), "query": "data center investment",
    "items": [
        {"theme": "datacenter", "title": f"{_SAME} - CNBC", "link": "1",
         "published": (_NOW - timedelta(days=1)).isoformat(), "source": "CNBC"},
        {"theme": "datacenter", "title": f"{_SAME} - Reuters", "link": "2",
         "published": (_NOW - timedelta(days=1)).isoformat(),
         "source": "Reuters"},
        {"theme": "datacenter", "title": "Province unveils data centre "
                                         "framework - CBC", "link": "3",
         "published": (_NOW - timedelta(days=2)).isoformat(), "source": "CBC"},
    ]}}), encoding="utf-8")

PICK = {"ticker": "pwr", "mechanism": "builds the transmission interconnect",
        "sources": ["https://example.com/a", "https://example.com/b"],
        "chain_role": "Engineering", "exposure": "Major",
        "time_horizon": "multi_year", "confidence": "medium",
        "evidence_type": "theme_graph", "risks": ["permitting delays"]}
ROW = {**PICK, "theme": "datacenter", "event": "operator announces a campus"}


# --------------------------------------------------------------------------
# A stubbed tier 1+2, so nothing downloads. Shaped exactly like the payload
# `run_scanners.scan_ticker` returns, including the on_demand fallback screen
# a ticker gets when no technical screen fires.
# --------------------------------------------------------------------------

KNOWN = {"PWR": "2026-01-05", "GEV": "2026-01-05", "HUBB": "2026-01-05"}
GRADED = {"Close": 100.0, "Company": "Stub Corp", "Quality": False,
          "Quality Missing": ["trailingPE"], "Veto": False, "Veto Reasons": [],
          "Reward": 61.0, "Risk": 44.0, "Quadrant": "buy"}


def fake_scan_ticker(ticker: str, _cfg: dict, **_) -> dict:
    ticker = ticker.upper()
    if ticker not in KNOWN:
        raise ValueError(f"no price data for {ticker}")
    screens = [{"config_key": run_scanners.NO_SIGNAL_KEY,
                "title": run_scanners.NO_SIGNAL_TITLE, "strategy": {},
                "hits": {ticker: dict(GRADED, Setup="none", Missing="")}}]
    if ticker == "GEV":   # also fired a real screen -- the Trigger case
        screens.insert(0, {"config_key": "breakout_strategy",
                           "title": "Breakout", "strategy": {},
                           "hits": {ticker: dict(GRADED, Setup="full",
                                                 Missing="", **{"Vol Ratio": 1.4})}})
    return {"scan_date": KNOWN[ticker], "generated_at": "2026-01-05T00:00:00Z",
            "screens": screens}


run_scanners.scan_ticker = fake_scan_ticker
scanner_common.universe_constituents = lambda _cfg: pd.DataFrame(
    {"ticker": ["PWR", "HUBB"], "index_name": ["sp500", "sp400"]})


def signals_frame():
    path = scanner_common.signals_csv_path(cfg, create=False)
    return (pd.read_csv(path, dtype={"scan_date": str}) if path.exists()
            else pd.DataFrame())


# --------------------------------------------------------------------------
c.section("the record names a company; it never carries a number")

for banned in ("conviction", "tier", "score", "Verdict", "Reward", "Risk",
               "price_target"):
    problems = theme_signals.validate({**ROW, banned: 80}, cfg)
    c.ok(f"a {banned!r} field is rejected outright",
         any(banned in p for p in problems),
         "the screen identifies candidates; the registry grades them")

c.ok("no column in the schema is a score",
     not ({"conviction", "tier", "score", "Verdict", "Reward", "Risk",
           "Quadrant", "price_target"} & set(theme_signals.COLUMNS)),
     str(theme_signals.COLUMNS))


# --------------------------------------------------------------------------
c.section("an invalid pick is refused, and nothing is written")

c.ok("an unknown chain_role is rejected",
     any("chain_role" in p
         for p in theme_signals.validate({**ROW, "chain_role": "middleman"}, cfg)),
     "silently it becomes a one-member cohort the analysis then grades")
c.ok("an unknown exposure is rejected",
     any("exposure" in p
         for p in theme_signals.validate({**ROW, "exposure": "huge"}, cfg)))
c.ok("an unconfigured theme is rejected",
     any("theme" in p
         for p in theme_signals.validate({**ROW, "theme": "crypto"}, cfg)),
     "a theme nobody configured is a cohort of one")
c.ok("a single source is rejected",
     any("rumour" in p
         for p in theme_signals.validate({**ROW, "sources": ["a"]}, cfg)),
     "an event with one source is not an event")
c.ok("a missing mechanism is rejected",
     any("mechanism" in p for p in theme_signals.validate(
         {k: v for k, v in ROW.items() if k != "mechanism"}, cfg)),
     "a pick with no stated mechanism cannot be falsified")
c.ok("a list field given a bare string is rejected",
     any("risks" in p
         for p in theme_signals.validate({**ROW, "risks": "permitting"}, cfg)))
c.ok("an unknown field is rejected",
     any("unknown" in p for p in theme_signals.validate({**ROW, "target": 2}, cfg)),
     "a typo must not become a column nothing reads")
c.ok("a valid pick has no problems", theme_signals.validate(ROW, cfg) == [])

raised = False
try:
    theme_signals.record("datacenter", "an event",
                         [{**PICK, "confidence": "certain"}], cfg)
except ValueError:
    raised = True
c.ok("record() raises on an invalid pick rather than writing it", raised)
c.ok("...and wrote no signal row", signals_frame().empty)


# --------------------------------------------------------------------------
c.section("a pick is proved tradeable before it is recorded")

raised = False
try:
    theme_signals.record("datacenter", "an event",
                         [{**PICK, "ticker": "ZZZZ"}], cfg)
except ValueError as exc:
    raised = "refused" in str(exc) or "could not be scanned" in str(exc)
c.ok("a symbol with no price data is refused", raised,
     "the theme graph returns Milan/Copenhagen/NSE lines, and a model can "
     "invent a symbol outright")
c.ok("...and wrote nothing", signals_frame().empty)


# --------------------------------------------------------------------------
c.section("the batch is atomic -- half a chain is a misleading cohort")

raised = False
try:
    theme_signals.record("datacenter", "an event",
                         [PICK, {**PICK, "ticker": "GEV", "exposure": "vast"}],
                         cfg)
except ValueError:
    raised = True
c.ok("one invalid pick in a batch raises", raised)
c.ok("...and the VALID pick in that batch was not written either",
     signals_frame().empty,
     "the unit of validity is the set, as for config_edit")

raised = False
try:
    theme_signals.record("datacenter", "an event",
                         [PICK, {**PICK, "ticker": "ZZZZ"}], cfg)
except ValueError:
    raised = True
c.ok("one unscannable pick in a batch raises", raised)
c.ok("...and nothing was written", signals_frame().empty,
     "grading happens for the whole batch before any row is written")


# --------------------------------------------------------------------------
c.section("the round trip")

result = theme_signals.record(
    "datacenter", "operator announces a campus",
    [PICK, {**PICK, "ticker": "GEV", "chain_role": "equipment"}], cfg,
    event_date="2026-01-02")
c.ok("record() reports what it wrote", result.get("recorded")
     and len(result.get("picks", [])) == 2, str(result.get("picks")))

frame = signals_frame()
c.ok("both picks reached signals.csv", len(frame) == 2, f"{len(frame)} row(s)")
c.ok("every row carries config_key='theme_screen'",
     set(frame["config_key"]) == {theme_signals.CONFIG_KEY},
     "so every analysis that groups by screen separates them automatically")
c.ok("Setup is the theme value, never full/partial",
     set(frame["Setup"]) == {theme_signals.SETUP_VALUE})
c.ok("the scan_date comes from the ticker's own bar, not from the caller",
     set(frame["scan_date"]) == {"2026-01-05"},
     "the caller never supplies it -- that is what proving the ticker buys")

pwr = frame[frame["ticker"] == "PWR"].iloc[0]
c.ok("the registry's grade is carried onto the row",
     pwr["Quadrant"] == "buy" and float(pwr["Reward"]) == 61.0,
     "computed after the pick, by tier 2")
c.ok("the categorical judgment rides along for tier 4 to grade",
     pwr[theme_signals.ROLE_COL] == "engineering"
     and pwr[theme_signals.EXPOSURE_COL] == "major"
     and pwr[theme_signals.THEME_COL] == "datacenter",
     "and is normalized to lower case")
c.ok("index membership is recorded, blank when off-index",
     pwr[scanner_common.INDEX_COL] == "sp500",
     "S&P rewrites membership at every rebalance, so it is not "
     "reconstructable later")

gev = frame[frame["ticker"] == "GEV"].iloc[0]
c.ok("a pick that ALSO fired a screen records which one",
     gev[theme_signals.TRIGGER_COL] == "breakout_strategy",
     "the split for 'did the AI find it before or after the tape did'")
c.ok("a pick with no technical trigger names that explicitly",
     pwr[theme_signals.TRIGGER_COL] == theme_signals.TRIGGER_NONE,
     "not '' -- that round-trips as NaN and groupby would silently drop the "
     "very cohort this column exists to isolate")

stored = theme_signals.read_one("PWR", "2026-01-05", cfg)
c.ok("the narrative lands in themes.csv, not in signals.csv",
     stored.get("mechanism") == PICK["mechanism"]
     and "mechanism" not in frame.columns,
     "signals.csv is a union of every screen's columns; prose would bloat it")
c.ok("counts are derived, not asked for",
     stored.get("sources_n") == 2 and stored.get("risks_n") == 1)
c.ok("list fields survive the CSV round trip",
     theme_signals.decode_lists(stored)["risks"] == ["permitting delays"])
c.ok("the ticker is normalized to upper case", stored.get("ticker") == "PWR")


# --------------------------------------------------------------------------
c.section("tier 4 picks them up with no change to the ledger")

ledger.sync(cfg)
positions = pd.read_csv(TMP / "positions.csv", dtype={"scan_date": str})
theme_rows = positions[positions["config_key"] == theme_signals.CONFIG_KEY]
c.ok("every recorded pick becomes exactly one position", len(theme_rows) == 2,
     f"{len(theme_rows)} position(s)")
c.ok("the position carries the theme attributes verbatim",
     set(theme_rows[theme_signals.ROLE_COL]) == {"engineering", "equipment"},
     "an attribute that was not carried can never be graded")
c.ok("the position is keyed by the screen, so cohorts stay separable",
     all(str(p).endswith(theme_signals.CONFIG_KEY)
         for p in theme_rows["position_id"]),
     str(list(theme_rows["position_id"])))


# --------------------------------------------------------------------------
c.section("a re-record is idempotent and cannot erase a verdict")

scanner_common.update_csv_rows(
    scanner_common.signals_csv_path(cfg, create=False),
    {"ticker": "PWR", "scan_date": "2026-01-05"},
    {"Verdict": "WATCH", "Conviction": 67.0})

theme_signals.record("datacenter", "operator announces a campus",
                     [{**PICK, "confidence": "high"}], cfg)
frame = signals_frame()
c.ok("re-recording the same (scan_date, config_key, ticker) replaces the row",
     len(frame) == 2, f"{len(frame)} row(s)")
again = frame[frame["ticker"] == "PWR"].iloc[0]
c.ok("...carrying the new judgment",
     again[theme_signals.CONFIDENCE_COL] == "high")
c.ok("...and the tier-3 verdict survives the rewrite",
     again["Verdict"] == "WATCH" and float(again["Conviction"]) == 67.0,
     "protect=VERDICT_COLS, the same trap the tier-3 carry has a test for")

c.ok("record() never writes a verdict column itself",
     not ({"Verdict", "Conviction"} & set(theme_signals.COLUMNS)),
     "the verdict is tier 3's, written later or not at all")


# --------------------------------------------------------------------------
c.section("it is in NEITHER screen registry")

import backtest_universe

c.ok("not in run_scanners.SCANNERS",
     theme_signals.CONFIG_KEY not in
     [getattr(m, "CONFIG_KEY", None) for m in run_scanners.SCANNERS],
     "it would run nightly, and scan(data, strategy) is meaningless for a "
     "news-driven pick")
c.ok("not in backtest_universe.SCREENS",
     theme_signals.CONFIG_KEY not in
     [getattr(m, "CONFIG_KEY", None) for m in backtest_universe.SCREENS],
     "that list is what _harness.screens() reads, and test_signal_contract "
     "then demands a full-history mask a list dated today cannot supply")
c.ok("not in backtest.screens",
     theme_signals.CONFIG_KEY not in
     (config().get("backtest", {}).get("screens") or []),
     "forward_trades would score a news pick as a technical signal")
c.ok("the module exposes no compute_/fires_mask/partial_mask",
     not any(hasattr(theme_signals, n) for n in
             ("compute_signals", "fires_mask", "partial_mask",
              "required_history")),
     "having them would invite exactly the registration this test forbids")


# --------------------------------------------------------------------------
c.section("the news feed fails open")

c.ok("an RSS document parses to dated, sourced items",
     newsfeed.parse_rss(
         '<rss><channel><item><title>T</title><link>http://x</link>'
         '<pubDate>Fri, 14 Aug 2026 12:20:57 GMT</pubDate>'
         '<source url="http://s">Src</source></item></channel></rss>',
         "datacenter") ==
     [{"theme": "datacenter", "title": "T", "link": "http://x",
       "published": "2026-08-14T12:20:57+00:00", "source": "Src"}])
c.ok("a malformed feed yields no items rather than raising",
     newsfeed.parse_rss("<not xml", "datacenter") == [])

broken = copy.deepcopy(cfg)
broken["theme_screen"]["news_feed"] = "https://127.0.0.1:9/{query}"
broken["theme_screen"]["news_cache_hours"] = 0   # force a refetch attempt
c.ok("an unreachable feed serves the stale cache rather than raising",
     len(newsfeed.fetch("datacenter", broken)) == 3,
     "a week-old event is still an event; a dead feed costs freshness, "
     "not the theme")

cold = copy.deepcopy(broken)
cold["theme_screen"]["dir"] = str(TMP / "cold")
cold["theme_screen"]["csv"] = str(TMP / "cold" / "themes.csv")
c.ok("...and returns [] when there is no cache to fall back on",
     newsfeed.fetch("datacenter", cold) == [],
     "a dead feed costs that theme's events and nothing else")
c.ok("a theme with no configured query is skipped, not fetched",
     newsfeed.fetch("nonesuch", cfg) == [])


# --------------------------------------------------------------------------
c.section("events are ranked by size and corroboration, not by date")

c.ok("a headline's money amount is extracted",
     newsfeed.magnitude("Meta's data center investment to reach $50 billion")
     == {"magnitude": 50e9, "magnitude_currency": "$",
         "magnitude_text": "$50 billion"})
c.ok("the LARGEST amount in a headline wins",
     newsfeed.magnitude("a $2 million grant beside a $13 billion plant"
                        )["magnitude"] == 13e9)
c.ok("a foreign currency keeps its unit and is NOT converted",
     newsfeed.magnitude("Meta to build C$13 billion Alberta data center")
     .get("magnitude_currency") == "C$",
     "there is no rate source here; a wrong number that looks right is worse "
     "than no number")
c.ok("a headline with no amount yields {}",
     newsfeed.magnitude("Ontario unveils a data centre framework") == {})

SAME = "Meta's Louisiana data center investment to reach $50 billion"
items = [
    {"theme": "t", "title": f"{SAME} - CNBC", "link": "1",
     "published": "2026-07-13T00:00:00+00:00", "source": "CNBC"},
    {"theme": "t", "title": f"{SAME} - Reuters", "link": "2",
     "published": "2026-07-13T01:00:00+00:00", "source": "Reuters"},
    {"theme": "t", "title": "Ontario unveils new data centre framework - CBC",
     "link": "3", "published": "2026-08-14T00:00:00+00:00", "source": "CBC"},
]
clusters = newsfeed.cluster_events(items)
c.ok("two outlets on one story collapse to one event", len(clusters) == 2,
     f"{len(clusters)} cluster(s)")
c.ok("...carrying the corroboration count the min_sources rule asks about",
     clusters[0]["sources_n"] == 2
     and clusters[0]["sources"] == ["CNBC", "Reuters"])
c.ok("the sized event outranks the more recent unsized one",
     clusters[0]["magnitude"] == 50e9 and "Ontario" in clusters[1]["title"],
     "date order would have buried a $50bn commitment under an announcement")
c.ok("the publisher suffix is stripped from the headline",
     not clusters[0]["title"].endswith("CNBC"))
c.ok("distinct events are NOT merged",
     clusters[1]["sources_n"] == 1,
     "over-merging would hide an event AND inflate another's corroboration")
c.ok("clustering an empty feed yields nothing", newsfeed.cluster_events([]) == [])

# The route that plain wording overlap misses: outlets rewrite a headline far
# enough that it shares under a third of its words, but they quote the same
# figure. Measured on live data -- three reports of one $38bn fab commitment
# stayed three separate "events" until this was added.
reworded = [
    {"theme": "t", "source": "A", "link": "1", "published": "2026-08-07T02:00:00+00:00",
     "title": "SK Hynix Announces $38.4 Billion Investment Plan to Accelerate AI Chip Expansion"},
    {"theme": "t", "source": "B", "link": "2", "published": "2026-08-07T01:00:00+00:00",
     "title": "SK Hynix to invest $38 billion building new memory chip plants as demand soars"},
]
merged = newsfeed.cluster_events(reworded)
c.ok("a rewritten headline quoting the same figure still merges",
     len(merged) == 1 and merged[0]["sources_n"] == 2,
     f"{len(merged)} cluster(s) -- $38bn vs $38.4bn is one announcement")
c.ok("...and the cluster keeps the fuller figure",
     merged[0]["magnitude"] == 38.4e9)

c.ok("the same figure in a DIFFERENT currency does not merge",
     len(newsfeed.cluster_events([
         {"theme": "t", "source": "A", "link": "1", "published": "2026-08-07T00:00:00+00:00",
          "title": "Operator to build $13 billion Texas data center"},
         {"theme": "t", "source": "B", "link": "2", "published": "2026-08-07T00:00:00+00:00",
          "title": "Operator to build C$13 billion Alberta data center"}])) == 2,
     "nothing here converts between currencies, so they are different numbers")

c.ok("unrelated stories quoting the same round number stay separate",
     len(newsfeed.cluster_events([
         {"theme": "t", "source": "A", "link": "1", "published": "2026-08-07T00:00:00+00:00",
          "title": "Operator launches $1 billion community fund in Louisiana"},
         {"theme": "t", "source": "B", "link": "2", "published": "2026-08-07T00:00:00+00:00",
          "title": "Regulator fines a utility $1 billion over wildfire claims"}])) == 2,
     "the amount route still demands real wording overlap")

c.ok("US and UK spellings tokenize the same",
     newsfeed._tokens("data centre framework")
     == newsfeed._tokens("data center framework"),
     "one Ontario announcement was filed both ways on the same day")


# --------------------------------------------------------------------------
c.section("the evidence behind a pick is frozen, not left in a mutable cache")

row = theme_signals.read_one("PWR", "2026-01-05", cfg)
name = row.get("evidence_file") or ""
c.ok("the recorded row names an evidence snapshot", bool(name), str(name))
frozen = json.loads((TMP / name).read_text(encoding="utf-8")) if name else {}
c.ok("...which exists and is keyed to the pick",
     frozen.get("theme") == "datacenter"
     and frozen.get("scan_date") == row.get("scan_date"),
     "the news cache is overwritten on every refresh; this file is not")
c.ok("...and holds the clustered events, not the raw feed",
     len(frozen.get("events", [])) == 2
     and frozen["events"][0]["sources_n"] == 2,
     "3 seeded headlines -> 2 events, the first carried by two outlets")
c.ok("...with the count recorded on the row too",
     row.get("evidence_n") == len(frozen.get("events", [])))

c.ok("a theme with no events writes no snapshot rather than an empty one",
     theme_signals.snapshot_evidence("nonesuch", "2026-01-05", cfg) == ("", 0),
     "an empty file would claim the pick was made from no evidence at all")


# --------------------------------------------------------------------------
c.section("--deep grades each pick from the row it just recorded")

# `record` already ran tiers 1+2 for every pick. The verdict pass used to look
# the ticker up through `resolve_trigger`, miss `latest_hits.json` and scan it
# all over again -- two downloads and two Yahoo passes per pick.
import research_report  # noqa: E402

scans, triggers = [], {}
_fake, _saved = run_scanners.scan_ticker, (
    research_report.price_inputs, research_report.deterministic_verdict,
    research_report.record_verdict)


def _counting_scan(ticker, cfg_, **kw):
    scans.append(ticker.upper())
    return _fake(ticker, cfg_, **kw)


def _verdict(ticker, cfg_, trigger, scan_date, source, close=None, benchmark=None):
    triggers[ticker] = trigger
    return {"ticker": ticker, "scan_date": scan_date, "tier": "WATCH",
            "conviction": 50}


run_scanners.scan_ticker = _counting_scan
research_report.price_inputs = lambda tickers, cfg_: (None, None)
research_report.deterministic_verdict = _verdict
research_report.record_verdict = lambda verdict, cfg_: None
try:
    deep = theme_signals.record("datacenter", "operator announces a campus",
                                [PICK], cfg, deep=True)
finally:
    run_scanners.scan_ticker = _fake
    (research_report.price_inputs, research_report.deterministic_verdict,
     research_report.record_verdict) = _saved

c.ok("each pick is scanned exactly once", scans == ["PWR"], f"{scans}")
c.ok("the verdict is graded from the recorded row",
     (triggers.get("PWR") or {}).get("row", {}).get("Quadrant") == "buy"
     and len(deep.get("verdicts", [])) == 1, f"{triggers.get('PWR')}")


# --------------------------------------------------------------------------
c.section("the section switch is a full no-op")

off = copy.deepcopy(cfg)
off["theme_screen"]["enabled"] = False
c.ok("record() writes nothing when the screen is off",
     theme_signals.record("datacenter", "e", [PICK], off).get("recorded")
     is False)
c.ok("over-long batches are refused before anything is scanned",
     theme_signals.record("datacenter", "e", [PICK] * 99, cfg)
     .get("recorded") is False,
     "max_picks_per_run")

raise SystemExit(c.finish())
