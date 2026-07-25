"""The signal contract, end to end: masks -> hits frame -> Discord cards ->
hand-off file -> deep-dive lookup.

Every check is an **invariant**, never a recorded count -- these have to hold at
any thresholds, because `config.json` is retuned constantly. The properties
being pinned are the ones the hits/near-miss unification introduced:

  * `fires_mask` is `signal` for a strict screen, else exactly `signal | partial`
  * the two tiers are disjoint, and `Setup`/`Missing` agree with them
  * one card per signal, partials visibly marked, no near-miss vocabulary left
  * the hand-off is a single list and the deep-dive can resolve either tier

Uses the cached price panel; never downloads. Discord is stubbed -- this test
must never send.
"""

import builtins
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

from _harness import Checks, busiest_day, cached_panel_or_skip, screens

import research_report
import run_scanners
from scanner_common import PARTIAL_COLOR

c = Checks("signal contract")
panel, cfg = cached_panel_or_skip()
truncated, day = busiest_day(panel, cfg)

# --------------------------------------------------------------------------
c.section("mask algebra (per screen, over all history)")
for module, compute, strategy in screens(cfg):
    key = module.CONFIG_KEY
    signals = compute(panel, strategy)
    full = signals["signal"].fillna(False)
    partial = module.partial_mask(panel, signals, strategy).fillna(False)
    fires = module.fires_mask(panel, signals, strategy).fillna(False)

    c.ok(f"{key}: tiers are disjoint",
         not (full & partial).any().any())
    c.ok(f"{key}: fires includes every strict signal",
         (fires >= full).all().all())
    strict = fires.equals(full)
    union = fires.equals(full | partial)
    c.ok(f"{key}: fires is either signal or signal|partial",
         strict or union, "strict" if strict else "signal|partial")

# --------------------------------------------------------------------------
c.section(f"hits frame from find_*  (scan day {day.date()})")
frames = {}
for module, compute, strategy in screens(cfg):
    key = module.CONFIG_KEY
    hits = module.scan(truncated, strategy).hits
    frames[key] = hits
    if hits.empty:
        print(f"  ({key}: nothing fired today)")
        continue

    signals = compute(truncated, strategy)
    full_today = signals["signal"].fillna(False).iloc[-1]
    fires_today = module.fires_mask(truncated, signals, strategy).fillna(False).iloc[-1]

    c.ok(f"{key}: row set == fires_mask on the scan day",
         set(hits.index) == set(fires_today[fires_today].index))
    c.ok(f"{key}: Setup only ever full/partial",
         set(hits["Setup"]) <= {"full", "partial"}, str(set(hits["Setup"])))
    c.ok(f"{key}: Setup=full rows are exactly the strict signals",
         set(hits.index[hits["Setup"] == "full"])
         == set(full_today[full_today].index))
    c.ok(f"{key}: Missing is non-empty iff the row is partial",
         all(bool(hits.at[t, "Missing"]) == (hits.at[t, "Setup"] == "partial")
             for t in hits.index))
    setups = list(hits["Setup"])
    c.ok(f"{key}: full setups sort before partials", setups == sorted(setups))

# --------------------------------------------------------------------------
c.section("Discord cards: one per signal, tier visible, no near-miss wording")
captured = {}
handoff = Path(tempfile.mkdtemp(prefix="test_handoff_")) / "latest_hits.json"
run_cfg = json.loads(json.dumps(cfg))          # deep copy
run_cfg["fundamentals"]["enabled"] = False     # no Yahoo round-trips
run_cfg["charts"]["enabled"] = False           # no PNG rendering
run_cfg["research"]["latest_hits_path"] = str(handoff)

run_scanners.load_config = lambda: run_cfg
run_scanners.get_sp500_tickers = lambda url: list(panel["Close"].columns)
run_scanners.download_price_data = lambda t, period, interval: truncated
run_scanners.send_discord_alert = \
    lambda content, dc, embeds=(), images=(): captured.update(
        content=content, embeds=list(embeds))

_print = builtins.print
builtins.print = lambda *a, **k: None
rc = run_scanners.main()
builtins.print = _print

embeds = captured.get("embeds", [])
n_rows = sum(len(f) for f in frames.values())
n_partial = sum(int((f["Setup"] == "partial").sum()) for f in frames.values()
                if not f.empty)
partial_cards = [e for e in embeds if "(partial)" in e["title"]]
full_cards = [e for e in embeds if "(partial)" not in e["title"]]

c.ok("main() returned 0", rc == 0)
c.ok("one card per signal row", len(embeds) == n_rows,
     f"{len(embeds)} cards vs {n_rows} rows")
c.ok("partial card count matches the frames", len(partial_cards) == n_partial,
     f"{len(partial_cards)} vs {n_partial}")
c.ok("partial cards use the grey side bar",
     all(e["color"] == PARTIAL_COLOR for e in partial_cards))
c.ok("full cards use their screen's own colour",
     all(e["color"] != PARTIAL_COLOR for e in full_cards))
c.ok("partial cards say what is missing",
     all("**Missing:**" in e["description"] for e in partial_cards))
c.ok("full cards carry no Missing line",
     not any("**Missing:**" in e["description"] for e in full_cards))
c.ok("no 'near miss' vocabulary anywhere in the payload",
     not any("near miss" in e["title"].lower()
             or "near-miss" in e["description"].lower() for e in embeds)
     and "near-miss" not in captured.get("content", "").lower())

# --------------------------------------------------------------------------
c.section("hand-off file and deep-dive lookup")
payload = json.loads(handoff.read_text(encoding="utf-8"))
rows = [(t, r) for s in payload["screens"] for t, r in s["hits"].items()]
c.ok("no 'near' key survives in any screen",
     all("near" not in s for s in payload["screens"]))
c.ok("row count matches the frames", len(rows) == n_rows,
     f"{len(rows)} vs {n_rows}")
c.ok("every row carries a Setup tier",
     all(r.get("Setup") in ("full", "partial") for _, r in rows))
by_tier = {}
for t, r in rows:
    by_tier.setdefault(r["Setup"], t)
for tier, ticker in by_tier.items():
    found = research_report.find_ticker(payload, ticker)
    c.ok(f"find_ticker resolves a {tier} setup ({ticker})",
         found is not None and found["kind"] == tier,
         str(found and found["kind"]))
c.ok("find_ticker returns None for a ticker that did not fire",
     research_report.find_ticker(payload, "__NOPE__") is None)

# --------------------------------------------------------------------------
c.section("a day where nothing fires must not crash")
captured.clear()
run_scanners.download_price_data = lambda t, period, interval: panel.iloc[:400]
builtins.print = lambda *a, **k: None
rc2 = run_scanners.main()
builtins.print = _print
c.ok("empty scan day still returns 0", rc2 == 0)
c.ok("empty scan day still sends a summary", "content" in captured)

sys.exit(c.finish())
