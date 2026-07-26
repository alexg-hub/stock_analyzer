"""The signal contract, end to end: masks -> hits frame -> Discord cards ->
hand-off file -> deep-dive lookup.

Every check is an **invariant**, never a recorded count -- these have to hold at
any thresholds, because `config.json` is retuned constantly. The properties
being pinned are the ones the hits/near-miss unification introduced:

  * `fires_mask` is `signal` for a strict screen, else exactly `signal | partial`
  * the two tiers are disjoint, and `Setup`/`Missing` agree with them
  * one card per signal, partials visibly marked, no near-miss vocabulary left
  * the hand-off is a single list and the deep-dive can resolve either tier
  * an unsettled trailing bar (Yahoo's null-close row) is dropped rather than
    scanned as a silent zero
  * `enabled: false` silences a screen's alert without disturbing any other
  * the tier-2 quality verdict reaches the hand-off intact, and the ⭐ badge is
    that same recorded verdict rather than a second, drifting computation
  * the archive accumulates scans without duplicating a re-run
  * the deep-dive gate ranks and filters candidates, and can still grade a
    hand-off written before the verdict was recorded

Note the split: the mask/hits invariants run over **every configured screen**,
enabled or not (a screen is usually switched off while it is being reworked, so
that is when its contract most needs checking), while the alert and hand-off
expectations are built from the **enabled** ones, mirroring `run_scanners.main`.

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
from scanner_common import (
    COMPANY_COL,
    HISTORY_KEYS,
    PARTIAL_COLOR,
    QUALITY_COL,
    QUALITY_MISSING_COL,
    drop_unsettled_tail,
    quality_check,
)


def _raises(fn) -> bool:
    try:
        fn()
    except SystemExit:
        return True
    return False


c = Checks("signal contract")
panel, cfg = cached_panel_or_skip()
truncated, day = busiest_day(panel, cfg)

# This file runs production `main()` several times. Fingerprint the real
# output directory up front so the last check can prove none of those runs
# leaked into it -- a redirect that covers the hand-off but forgets the
# archive files test scan days among the user's genuine ones.
from scanner_common import output_dir


def output_fingerprint():
    root = output_dir(create=False)
    if not root.exists():
        return set()
    return {(str(p.relative_to(root)), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


OUTPUT_BEFORE = output_fingerprint()

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
# `enabled` gates the nightly alert only, so the alert's expectations come from
# the enabled screens while the invariants above keep covering every configured
# screen -- a screen is normally switched off while it is being reworked, which
# is when its contract most needs to stay verified.
alerted = {k: f for k, f in frames.items() if cfg[k].get("enabled", True)}
silenced = sorted(set(frames) - set(alerted))

# --------------------------------------------------------------------------
c.section("Discord cards: one per signal, tier visible, no near-miss wording")
captured = {}
# EVERY output path main() writes has to be redirected, not just the hand-off:
# `archive_scan` otherwise falls back to the real `output/history/` and files
# the test's historical scan days alongside genuine ones. A test that runs
# production `main()` must leave no trace in output/.
sandbox = Path(tempfile.mkdtemp(prefix="test_handoff_"))
handoff = sandbox / "latest_hits.json"
run_cfg = json.loads(json.dumps(cfg))          # deep copy
run_cfg["fundamentals"]["enabled"] = False     # no Yahoo round-trips
run_cfg["charts"]["enabled"] = False           # no PNG rendering
run_cfg["research"]["latest_hits_path"] = str(handoff)
run_cfg["research"].setdefault("history", {})
run_cfg["research"]["history"].update(enabled=True, dir=str(sandbox / "history"),
                                      csv="signals.csv")

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
n_rows = sum(len(f) for f in alerted.values())
n_partial = sum(int((f["Setup"] == "partial").sum()) for f in alerted.values()
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
suppressed = sum(len(frames[k]) for k in silenced)
c.ok("a disabled screen's signals are held back, every other card kept",
     len(embeds) + suppressed == sum(len(f) for f in frames.values()),
     f"disabled={silenced or 'none'}, {suppressed} row(s) held back")

# --------------------------------------------------------------------------
c.section("hand-off file and deep-dive lookup")
payload = json.loads(handoff.read_text(encoding="utf-8"))
rows = [(t, r) for s in payload["screens"] for t, r in s["hits"].items()]
c.ok("no 'near' key survives in any screen",
     all("near" not in s for s in payload["screens"]))
c.ok("the hand-off lists exactly the enabled screens",
     [s["config_key"] for s in payload["screens"]] == list(alerted),
     f"{[s['config_key'] for s in payload['screens']]}")
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

# --------------------------------------------------------------------------
# Yahoo serves an unsettled session as a normal row with Open/High/Low/Volume
# but a null Close, and can revert a settled bar to that form hours later
# (2026-07-24). Because every condition compares against Close, such a bar
# scans as a confident zero -- so the guard has to be load-bearing, not
# cosmetic, and that is what the last check here asserts.
c.section("an unsettled trailing bar is dropped, never scanned")
all_tickers = list(panel["Close"].columns)


def with_unsettled(frame, bars=1, missing=None):
    """`frame` plus trailing copies of its last row with Close blanked out for
    `missing` tickers (all of them by default)."""
    out = frame
    blank = all_tickers if missing is None else all_tickers[:missing]
    cols = pd.MultiIndex.from_product([["Close"], blank])
    for _ in range(bars):
        row = out.iloc[[-1]].copy()
        row.index = [out.index[-1] + pd.Timedelta(days=1)]
        row.loc[:, cols] = float("nan")
        out = pd.concat([out, row])
    return out


c.ok("a clean panel is returned untouched",
     drop_unsettled_tail(panel).index.equals(panel.index))
c.ok("a fully unsettled bar is dropped",
     drop_unsettled_tail(with_unsettled(panel)).index.equals(panel.index))
c.ok("several unsettled bars are all dropped",
     drop_unsettled_tail(with_unsettled(panel, bars=3)).index.equals(panel.index))
c.ok("a few missing tickers do not discard the bar",
     len(drop_unsettled_tail(
         with_unsettled(panel, missing=max(1, len(all_tickers) // 10)))
     ) == len(panel) + 1,
     "per-ticker download failures are normal and must not lose the day")

unsettled = with_unsettled(panel)
for module, compute, strategy in screens(cfg):
    def last_fires(frame):
        return module.fires_mask(
            frame, compute(frame, strategy), strategy).fillna(False).iloc[-1]

    raw, guarded = last_fires(unsettled), last_fires(drop_unsettled_tail(unsettled))
    c.ok(f"{module.CONFIG_KEY}: unsettled bar would scan as zero, guard restores it",
         int(raw.sum()) == 0 and guarded.equals(last_fires(panel)),
         f"unguarded={int(raw.sum())} guarded={int(guarded.sum())}")

# --------------------------------------------------------------------------
# Tier 2: the quality verdict, the archive, and the deep-dive gate.
#
# The run above deliberately disables fundamentals to stay off the network,
# which leaves the whole second half of the hand-off untested -- so this one
# stubs `fetch_fundamentals` instead of switching it off. That is what puts
# `_json_safe` under test on the values that actually exercise it (the
# [(year, value)] series, a NaN, numpy scalars) and what lets the badge be
# compared against the recorded verdict.
#
# The fixture is derived from `fundamentals.quality.rules` rather than written
# down: a rule's own min/max produces the value that satisfies it, and the
# bound *itself* is the value that fails it (the compares are strict). So the
# fixture keeps working at any thresholds, which is the whole point.
c.section("tier 2: quality verdict, archive, deep-dive gate")

fund_cfg = cfg["fundamentals"]
rules = fund_cfg.get("quality", {}).get("rules", {})


def rule_label(key):
    """The hits-frame column a rule key grades, or None if it names nothing."""
    if key in fund_cfg.get("fields", {}):
        return fund_cfg["fields"][key]
    return fund_cfg.get("statements", {}).get("metrics", {}).get(key)


unresolvable = [k for k in rules if rule_label(k) is None]
c.ok("every quality rule names a configured field or metric",
     not unresolvable, f"unresolvable: {unresolvable}" if unresolvable else "")

resolvable = [k for k in rules if rule_label(k) is not None]


def passing_value(rule):
    lo, hi = rule.get("min"), rule.get("max")
    if lo is not None and hi is not None:
        return (lo + hi) / 2
    if hi is not None:
        return hi / 2 if hi > 0 else hi - 1
    return lo + abs(lo) + 1


def fundamentals_row(break_key=None, nan_key=None):
    """A row passing every rule, optionally breaking exactly one of them."""
    row = {COMPANY_COL: "Test Corp"}
    for key in resolvable:
        rule, label = rules[key], rule_label(key)
        good = passing_value(rule)
        if key == nan_key:
            row[label] = float("nan")
            continue
        # A strict compare means the bound itself fails, and `increasing`
        # fails on a series that falls -- no magic numbers either way.
        broken = key == break_key
        if rule.get("increasing"):
            bound = rule.get("min", rule.get("max", good))
            row[label] = ([(2024, good), (2025, good * 0.5)] if broken
                          else [(2024, good * 0.5 + bound * 0.5), (2025, good)])
        else:
            row[label] = rule.get("min", rule.get("max")) if broken else good
    return row


broken_key = resolvable[0] if resolvable else None
nan_key = resolvable[-1] if resolvable else None
SHAPES = ["pass", "break", "nan"]


def stub_fundamentals(tickers, _cfg):
    """Cycle the three row shapes across the signalling tickers."""
    rows = {}
    for i, ticker in enumerate(tickers):
        shape = SHAPES[i % len(SHAPES)]
        rows[ticker] = fundamentals_row(
            break_key=broken_key if shape == "break" else None,
            nan_key=nan_key if shape == "nan" else None)
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("Ticker")


# Two scan days with signals, so the archive can be tested across dates.
totals = None
for module, compute, strategy in screens(cfg):
    fires = module.fires_mask(panel, compute(panel, strategy), strategy).fillna(False)
    per_day = fires.sum(axis=1)
    totals = per_day if totals is None else totals + per_day
busy_days = list(totals[totals > 0].sort_values(ascending=False).index[:2])

hist_dir = Path(tempfile.mkdtemp(prefix="test_history_"))
q_cfg = json.loads(json.dumps(cfg))
q_cfg["charts"]["enabled"] = False
q_cfg["research"]["latest_hits_path"] = str(handoff)
q_cfg["research"].setdefault("history", {})
q_cfg["research"]["history"].update(enabled=True, dir=str(hist_dir),
                                    csv="signals.csv")
q_cfg["research"].setdefault("auto", {})

run_scanners.load_config = lambda: q_cfg
run_scanners.fetch_fundamentals = stub_fundamentals


def run_on(day):
    """Run the full nightly pipeline as if `day` were the scan day."""
    cut = list(panel.index).index(day) + 1
    run_scanners.download_price_data = lambda t, period, interval: panel.iloc[:cut]
    captured.clear()
    builtins.print = lambda *a, **k: None
    rc = run_scanners.main()
    builtins.print = _print
    return rc, json.loads(handoff.read_text(encoding="utf-8")), captured.get("embeds", [])


rc3, q_payload, q_embeds = run_on(busy_days[0])
q_rows = [(t, r) for s in q_payload["screens"] for t, r in s["hits"].items()]
c.ok("main() returned 0 with fundamentals on", rc3 == 0)
c.ok("the fundamentals-enabled run produced rows", bool(q_rows), f"{len(q_rows)} rows")

graded = [r for _, r in q_rows]
c.ok("every hand-off row carries a Quality verdict",
     all(isinstance(r.get(QUALITY_COL), bool) for r in graded))
c.ok("Quality Missing is a list on every row",
     all(isinstance(r.get(QUALITY_MISSING_COL), list) for r in graded))
c.ok("Quality is true exactly when nothing failed",
     all(r[QUALITY_COL] == (not r[QUALITY_MISSING_COL]) for r in graded))

# The real point of the round trip: the verdict recomputed from the *parsed
# JSON* must match the one recorded before serialization. That can only hold
# if _json_safe preserved the [(year, value)] series and turned NaN into null.
c.ok("the verdict survives the JSON round trip",
     all(quality_check(r, fund_cfg) == r[QUALITY_COL] for r in graded))
series_rows = [v for r in graded for v in r.values()
               if isinstance(v, list) and v and isinstance(v[0], list)]
c.ok("multi-year metrics serialize as [[year, value], ...]",
     all(len(pair) == 2 for s in series_rows for pair in s),
     f"{len(series_rows)} series column(s) seen")
c.ok("a NaN fundamental becomes null and fails its rule",
     any(r.get(rule_label(nan_key)) is None and nan_key in r[QUALITY_MISSING_COL]
         for r in graded) if nan_key else True)
broken_rows = [r for r in graded if r[QUALITY_MISSING_COL] == [broken_key]]
c.ok("breaking one rule fails exactly that rule",
     bool(broken_rows), f"{len(broken_rows)} row(s) failing only {broken_key}")

badge = fund_cfg.get("quality", {}).get("badge", "")
badged = {e["title"].split()[1].split("(")[0] for e in q_embeds
          if badge and e["title"].startswith(badge)}
passing = {t for t, r in q_rows if r[QUALITY_COL]}
c.ok("the badge marks exactly the rows the hand-off recorded as passing",
     badged == passing, f"badged={sorted(badged)} passing={sorted(passing)}")
partial_passing = {t for t, r in q_rows
                   if r.get("Setup") == "partial" and r[QUALITY_COL]}
c.ok("a partial setup is not excluded from the badge",
     partial_passing <= badged,
     f"{len(partial_passing)} partial row(s) passed quality; "
     "the badge grades fundamentals, not setup completeness")

# --------------------------------------------------------------------------
c.section("the archive accumulates across scans")
csv_path = hist_dir / "signals.csv"
after_first = pd.read_csv(csv_path, dtype={"scan_date": str})
c.ok("a dated snapshot is written",
     (hist_dir / f"hits_{q_payload['scan_date']}.json").exists())
c.ok("the CSV holds one row per hand-off row",
     len(after_first) == len(q_rows), f"{len(after_first)} vs {len(q_rows)}")
c.ok("the CSV carries both tiers",
     {"Setup", "Missing", QUALITY_COL, QUALITY_MISSING_COL} <= set(after_first.columns))

run_on(busy_days[0])
after_repeat = pd.read_csv(csv_path, dtype={"scan_date": str})
c.ok("re-running the same scan day adds no duplicate rows",
     len(after_repeat) == len(after_first),
     f"{len(after_repeat)} vs {len(after_first)}")

_, second_payload, _ = run_on(busy_days[1])
after_second = pd.read_csv(csv_path, dtype={"scan_date": str})
second_rows = sum(len(s["hits"]) for s in second_payload["screens"])
c.ok("a second scan day is appended, not overwritten",
     len(after_second) == len(after_first) + second_rows,
     f"{len(after_second)} vs {len(after_first)}+{second_rows}")
c.ok("both scan dates are present",
     set(after_second["scan_date"]) ==
     {q_payload["scan_date"], second_payload["scan_date"]})
c.ok("no (scan_date, screen, ticker) is duplicated",
     not after_second.duplicated(subset=HISTORY_KEYS).any())

# --------------------------------------------------------------------------
c.section("the deep-dive gate")
gate_payload = json.loads(handoff.read_text(encoding="utf-8"))
every = research_report.list_candidates(gate_payload, q_cfg, gate="all")
passers = research_report.list_candidates(gate_payload, q_cfg, gate="quality_pass")
c.ok("gate 'all' keeps every signal",
     len(every) == sum(len(s["hits"]) for s in gate_payload["screens"]))
c.ok("gate 'quality_pass' keeps exactly the passers",
     {r["ticker"] for r in passers} == {r["ticker"] for r in every if r["quality"]})
c.ok("candidates rank quality-pass first, then full before partial",
     every == sorted(every, key=lambda r: (not r["quality"],
                                           r["setup"] != "full", r["ticker"])))
c.ok("a limit takes the top of that ranking, unchanged",
     research_report.list_candidates(gate_payload, q_cfg, gate="all", limit=2)
     == every[:2])
c.ok("an unknown gate is rejected rather than silently ignored",
     _raises(lambda: research_report.list_candidates(gate_payload, q_cfg, gate="nope")))

# A hand-off written before tier 2 existed must still be gradeable, or every
# archived scan becomes unreadable the moment the format moves on.
legacy = json.loads(json.dumps(gate_payload))
for screen in legacy["screens"]:
    for row in screen["hits"].values():
        row.pop(QUALITY_COL, None)
        row.pop(QUALITY_MISSING_COL, None)
c.ok("a hand-off with no recorded verdict is recomputed, not dropped",
     [(r["ticker"], r["quality"]) for r in
      research_report.list_candidates(legacy, q_cfg, gate="all")]
     == [(r["ticker"], r["quality"]) for r in every])

# --------------------------------------------------------------------------
c.section("the nightly auto-prompt")
q_cfg["research"]["auto"].update(enabled=True, gate="all", max_reports=2,
                                 discord_send=False)
research_report.load_config = lambda: q_cfg
prompt = research_report.auto_prompt(q_cfg)
c.ok("auto-prompt invokes the skill", bool(prompt) and prompt.startswith("/deep-dive"))
c.ok("auto-prompt names exactly the capped candidates",
     prompt.splitlines()[0].split()[1:] ==
     list(dict.fromkeys(r["ticker"] for r in every))[:2],
     prompt.splitlines()[0])
c.ok("auto-prompt passes the configured send authorization",
     "send=false" in prompt)
q_cfg["research"]["auto"]["enabled"] = False
c.ok("auto-prompt declines when the nightly run is disabled",
     research_report.auto_prompt(q_cfg) is None)
q_cfg["research"]["auto"].update(enabled=True, gate="quality_pass")
if not passers:
    c.ok("auto-prompt declines when the gate holds everything back",
         research_report.auto_prompt(q_cfg) is None)

c.ok("load_hits reads the same payload the run wrote",
     research_report.load_hits(q_cfg) == gate_payload)

# --------------------------------------------------------------------------
c.section("the suite leaves the real output/ untouched")
touched = sorted(p for p, _ in output_fingerprint() - OUTPUT_BEFORE)
c.ok("no production main() run wrote into output/",
     not touched,
     f"leaked: {touched[:6]}" if touched else "every path redirected to a temp dir")

sys.exit(c.finish())
