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
import io
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

import _harness
from _harness import Checks, busiest_day, cached_panel_or_skip, screens

import quality
import research_report
import run_scanners
import scanner_common
from scanner_common import (
    COMPANY_COL,
    CONVICTION_COL,
    HISTORY_KEYS,
    PARTIAL_COLOR,
    QUALITY_COL,
    QUALITY_MISSING_COL,
    VERDICT_COL,
    drop_unsettled_bars,
    update_csv_rows,
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
run_cfg["quality"]["enabled"] = False          # no Yahoo round-trips
run_cfg["charts"]["enabled"] = False           # no PNG rendering
# main() now runs tiers 3 and 4 in-process so they can share one message. Both
# reach the network and both write outside the hand-off, and neither is what
# this file tests -- test_portfolio_sim.py owns the ledger and
# test_combined_alert.py owns the verdict pass.
# Belt and braces: `enabled: False` is the switch, but the directory is
# redirected too. Relying on the flag alone is how 114 test positions from
# the cached panel's busiest days (2023-12-13, 2024-11-06) ended up in the
# real output/portfolio/positions.csv on 2026-08-01 -- `output_fingerprint()`
# caught it, but only after the write.
run_cfg["portfolio"] = {**run_cfg.get("portfolio", {}), "enabled": False,
                        "dir": str(sandbox / "portfolio")}
run_cfg["research"].setdefault("auto", {})
run_cfg["research"]["auto"]["enabled"] = False
run_cfg["research"]["latest_hits_path"] = str(handoff)
run_cfg["research"].setdefault("history", {})
run_cfg["research"]["history"].update(enabled=True, dir=str(sandbox / "history"),
                                      csv="signals.csv")
# The step log needs no redirect here: `_harness` pins it at import (before the
# cached panel is even loaded, which already logs) and `config()` redirects the
# config this is deep-copied from, so run_cfg inherits it.

run_scanners.load_config = lambda: run_cfg
run_scanners.get_sp500_tickers = lambda url: list(panel["Close"].columns)
run_scanners.download_price_data = lambda t, period, interval: truncated
run_scanners.send_discord_alert = \
    lambda content, dc, embeds=(), images=(): captured.update(
        content=content, embeds=list(embeds))

_print = builtins.print
builtins.print = lambda *a, **k: None
# Capture the step log's stderr echo alongside the run: the nightly .bat
# redirects 2>&1 into scanner_log.txt, so stderr is where tiers 1+2 now speak.
_log_file = Path(_harness.LOG_DIR) / f"{scanner_common.run_id()}.log"
_log_before = _log_file.read_text(encoding="utf-8") if _log_file.exists() else ""
_err_buf, _stderr = io.StringIO(), sys.stderr
sys.stderr = _err_buf
try:
    rc = run_scanners.main()
finally:
    sys.stderr = _stderr
    builtins.print = _print

scan_log = _log_file.read_text(encoding="utf-8")[len(_log_before):].splitlines()
scan_err = _err_buf.getvalue()


def log_phases(lines) -> list:
    return [ln.split(None, 3)[2] for ln in lines if len(ln.split()) > 2]


def log_lines_for(lines, phase) -> list:
    return [ln for ln in lines if len(ln.split()) > 2
            and ln.split(None, 3)[2] == phase]

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
# Tiers 1+2 write the same step log tier 3 does, so one file per run covers the
# whole nightly chain. Invariants only -- which phases a scan records and where
# they go, never how many signals fired.
c.section("the step log covers tiers 1 and 2")

phases = log_phases(scan_log)
c.ok("a scan records its own end, after everything else",
     phases and phases[-1] == "SCAN" and scan_log[-1].split()[3] == "ok",
     " -> ".join(phases))
c.ok("each SCREEN line names its config key and the scan date",
     all(ln.split()[4] in run_cfg and str(day.date()) in ln
         for ln in log_lines_for(scan_log, "SCREEN")
         if ln.split()[3] == "ok"),
     " | ".join(ln.split(None, 4)[-1] for ln in log_lines_for(scan_log, "SCREEN")))
c.ok("every screen that ran reports its count",
     len(log_lines_for(scan_log, "SCREEN")) >= len(
         [m for m, _ in [(m, None) for m in run_scanners.SCANNERS]
          if run_cfg.get(m.CONFIG_KEY)]),
     f"{len(log_lines_for(scan_log, 'SCREEN'))} SCREEN line(s)")
c.ok("a disabled screen is recorded as off, not silently skipped",
     all(any(f"{k} disabled" in ln for ln in log_lines_for(scan_log, "SCREEN"))
         for k in silenced),
     f"disabled={silenced or 'none'} -- a screen switched off is the likeliest "
     f"reason a night finds nothing")
c.ok("the hand-off and the archive are both recorded",
     log_lines_for(scan_log, "HANDOFF") and log_lines_for(scan_log, "ARCHIVE"))
c.ok("tier 2 records its verdict count even with fundamentals off",
     len(log_lines_for(scan_log, "QUALITY")) == 1,
     "absent quality columns mean 'not evaluated', which the log must say")

# The nightly .bat redirects 2>&1 into scanner_log.txt, and `context` needs
# stdout for its JSON bundle -- so tiers 1+2 must speak on stderr, like tier 3.
# (Checked with a direct call: the run above stubs builtins.print, which is what
# swallows the echo, not the stream choice.)
_echo_err, _stderr = io.StringIO(), sys.stderr
_echo_out, _stdout = io.StringIO(), sys.stdout
sys.stderr, sys.stdout = _echo_err, _echo_out
try:
    scanner_common.log_step("SCREEN", "ok", "a tier-1 step", cfg=run_cfg)
finally:
    sys.stderr, sys.stdout = _stderr, _stdout
c.ok("a tier-1 step echoes to stderr, keeping stdout free for structured output",
     "a tier-1 step" in _echo_err.getvalue() and not _echo_out.getvalue(),
     "run_scanner.bat redirects 2>&1, so scanner_log.txt still captures it")

# The single most valuable line in the file: a bar Yahoo has not settled makes
# every condition NaN, which reads as "no signal" -- a confident zero on data
# that looks complete. It has to be visible, and it has to be a warning.
_unsettled = truncated.copy()
_unsettled.loc[_unsettled.index[-1], "Close"] = float("nan")
_probe = Path(_harness.LOG_DIR) / f"{scanner_common.run_id()}.log"
_before = _probe.read_text(encoding="utf-8")
_kept = drop_unsettled_bars(_unsettled)
_drop_lines = _probe.read_text(encoding="utf-8")[len(_before):].splitlines()
c.ok("dropping an unsettled bar is logged as a warning, naming the bar",
     len(_kept) == len(_unsettled) - 1
     and any(ln.split()[3] == "warn" and str(_unsettled.index[-1].date()) in ln
             for ln in log_lines_for(_drop_lines, "DOWNLOAD")),
     " | ".join(_drop_lines) or "nothing logged")

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
     drop_unsettled_bars(panel).index.equals(panel.index))
c.ok("a fully unsettled bar is dropped",
     drop_unsettled_bars(with_unsettled(panel)).index.equals(panel.index))
c.ok("several unsettled bars are all dropped",
     drop_unsettled_bars(with_unsettled(panel, bars=3)).index.equals(panel.index))
c.ok("a few missing tickers do not discard the bar",
     len(drop_unsettled_bars(
         with_unsettled(panel, missing=max(1, len(all_tickers) // 10)))
     ) == len(panel) + 1,
     "per-ticker download failures are normal and must not lose the day")

unsettled = with_unsettled(panel)
for module, compute, strategy in screens(cfg):
    def last_fires(frame):
        return module.fires_mask(
            frame, compute(frame, strategy), strategy).fillna(False).iloc[-1]

    raw, guarded = last_fires(unsettled), last_fires(drop_unsettled_bars(unsettled))
    c.ok(f"{module.CONFIG_KEY}: unsettled bar would scan as zero, guard restores it",
         int(raw.sum()) == 0 and guarded.equals(last_fires(panel)),
         f"unguarded={int(raw.sum())} guarded={int(guarded.sum())}")

# --------------------------------------------------------------------------
# The same withdrawn bar, one session later. It is no longer the tail, so a
# tail-only guard walks straight past it -- but every compute_* builds its
# baselines with `rolling(window)` at the default `min_periods=window`, so one
# NaN *inside* the window voids the output for the next `window` sessions
# rather than for that day alone. That is the 2026-07-27 night: Yahoo still had
# 2026-07-24 blank for 502 of 503 tickers, Monday sat on top of it, and the
# alert said "nothing today" while suppressing 6 real signals. A bar no longer
# being last is not a bar becoming valid.
c.section("an unsettled interior bar is dropped too")

_INTERIOR_BACK = 3   # well inside every screen's rolling window


def with_interior_unsettled(frame, back=_INTERIOR_BACK, fields=None):
    """`frame` with the bar `back` from the end blanked out for every ticker.

    Blanks **all** OHLCV fields by default, which is the shape actually
    observed on 2026-07-24 (Open/High/Low/Volume withdrawn along with Close,
    not just Close) -- and the shape that voids the breakout screen's
    High-derived baselines as well as the pullback screen's Close-derived SMA.
    """
    out = frame.copy()
    fields = fields or list(dict.fromkeys(frame.columns.get_level_values(0)))
    cols = pd.MultiIndex.from_product([fields, all_tickers])
    out.loc[[out.index[-back]], cols.intersection(frame.columns)] = float("nan")
    return out


poisoned = with_interior_unsettled(truncated)
# What the panel should look like afterwards: the phantom session gone, every
# real one untouched.
without = truncated.drop(index=truncated.index[-_INTERIOR_BACK])

_before = _probe.read_text(encoding="utf-8")
_cleaned = drop_unsettled_bars(poisoned)
_drop_lines = _probe.read_text(encoding="utf-8")[len(_before):].splitlines()
c.ok("the interior bar is dropped and no other bar is",
     _cleaned.index.equals(without.index),
     f"{len(_cleaned)} bars vs {len(without)} expected")
c.ok("dropping an interior bar is logged as a warning, naming the bar",
     any(ln.split()[3] == "warn"
         and str(truncated.index[-_INTERIOR_BACK].date()) in ln
         for ln in log_lines_for(_drop_lines, "DOWNLOAD")),
     " | ".join(_drop_lines) or "nothing logged")
c.ok("a Close-only interior blank is caught as well",
     drop_unsettled_bars(
         with_interior_unsettled(truncated, fields=["Close"])
     ).index.equals(without.index),
     "the guard keys off Close, whatever else Yahoo left behind")
c.ok("a few missing tickers do not discard an interior bar",
     len(drop_unsettled_bars(pd.concat([
         poisoned.iloc[:-_INTERIOR_BACK],
         truncated.iloc[-_INTERIOR_BACK:],
     ]))) == len(truncated),
     "per-ticker gaps mid-panel are normal and must not lose the day")

for module, compute, strategy in screens(cfg):
    def last_fires(frame):
        return module.fires_mask(
            frame, compute(frame, strategy), strategy).fillna(False).iloc[-1]

    raw, guarded = last_fires(poisoned), last_fires(_cleaned)
    # Compared against `without`, not `truncated`: dropping a bar legitimately
    # slides every rolling window one session further back, so "unchanged
    # output" is the wrong bar to hold the guard to. What must hold is that the
    # result is exactly the one a panel that never carried the phantom would
    # have produced.
    c.ok(f"{module.CONFIG_KEY}: an interior NaN voids the window, guard restores it",
         int(raw.sum()) == 0 and guarded.equals(last_fires(without)),
         f"unguarded={int(raw.sum())} guarded={int(guarded.sum())} "
         f"expected={int(last_fires(without).sum())}")

# --------------------------------------------------------------------------
# Tier 2: the quality verdict, the archive, and the deep-dive gate.
#
# The run above deliberately disables fundamentals to stay off the network,
# which leaves the whole second half of the hand-off untested -- so this one
# stubs the collector instead of switching it off. That is what puts
# `_json_safe` under test on the values that actually exercise it (the
# [(year, value)] series, a NaN, numpy scalars) and what lets the badge be
# compared against the recorded verdict.
#
# The fixture is derived from `quality.parameters` rather than written down: a
# gate's own min/max produces the value that satisfies it, and the bound
# *itself* is the value that fails it (the compares are strict). So the fixture
# keeps working at any thresholds, which is the whole point.
c.section("tier 2: quality verdict, archive, deep-dive gate")

gated = {k: s for k, s in quality.parameters(cfg, quality.STAGE_FAST).items()
         if s.get("gate")}
rules = {k: s["gate"] for k, s in gated.items()}


def rule_label(key):
    """The hits-frame column a parameter grades, or None if it names nothing."""
    spec = gated.get(key)
    return quality.label_of(key, spec) if spec else None


unresolvable = [k for k in rules if rule_label(k) is None]
c.ok("every gated parameter resolves to a hits-frame column",
     not unresolvable, f"unresolvable: {unresolvable}" if unresolvable else "")

# Every gate is expressed in the registry's own vocabulary -- a typo'd keyword
# is never applied, so the parameter would look configured and gate nothing.
stray = {f"{k}.{w}" for k, g in rules.items()
         for w in set(g) - set(quality.GATE_KEYS)}
c.ok("no gate uses a keyword the engine does not implement",
     not stray, f"stray: {sorted(stray)}" if stray else "")

resolvable = [k for k in rules if rule_label(k) is not None]


def passing_value(rule):
    lo, hi = rule.get("min"), rule.get("max")
    if lo is not None and hi is not None:
        return (lo + hi) / 2
    if hi is not None:
        return hi / 2 if hi > 0 else hi - 1
    return lo + abs(lo) + 1


def fundamentals_row(break_key=None, nan_key=None):
    """A row passing every gate, optionally breaking exactly one of them."""
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
q_cfg["research"]["auto"]["enabled"] = False    # see run_cfg above
q_cfg["portfolio"] = {**q_cfg.get("portfolio", {}), "enabled": False,
                      "dir": str(hist_dir / "portfolio")}

run_scanners.load_config = lambda: q_cfg
run_scanners.quality.fetch_fast = stub_fundamentals


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
     all(quality.verdict_of(r, q_cfg)[0] == r[QUALITY_COL] for r in graded))
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

badge = quality.badge(q_cfg)
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

# Tier 3 writes its verdict into a row tier 1 created hours earlier, and the
# archive is *rewritten* on every scan. Without the protected-column carry in
# merge_history_csv, re-running a day would silently drop the verdict recorded
# against it -- no error, no warning, just a blank column next morning.
verdict_ticker = str(after_second.iloc[0]["ticker"])
verdict_date = str(after_second.iloc[0]["scan_date"])
n_marked = update_csv_rows(csv_path,
                           {"scan_date": verdict_date, "ticker": verdict_ticker},
                           {VERDICT_COL: "STRONG", CONVICTION_COL: 91})
c.ok("a verdict can be written onto an archived signal row", n_marked >= 1,
     f"{n_marked} row(s) for {verdict_ticker} on {verdict_date}")

rescan_day = next(d for d in busy_days
                  if str(pd.Timestamp(d).date()) == verdict_date)
run_on(rescan_day)
after_verdict = pd.read_csv(csv_path, dtype={"scan_date": str})
marked = after_verdict[(after_verdict["scan_date"] == verdict_date)
                       & (after_verdict["ticker"] == verdict_ticker)]
c.ok("re-scanning that day preserves the verdict",
     len(marked) == n_marked and (marked[VERDICT_COL] == "STRONG").all()
     and (marked[CONVICTION_COL] == 91).all(),
     f"{marked[VERDICT_COL].tolist()} / {marked[CONVICTION_COL].tolist()}")
c.ok("re-scanning still adds no duplicate rows",
     len(after_verdict) == len(after_second),
     f"{len(after_verdict)} vs {len(after_second)}")
c.ok("only the verdicted ticker carries a verdict",
     int(after_verdict[VERDICT_COL].notna().sum()) == n_marked,
     f"{int(after_verdict[VERDICT_COL].notna().sum())} row(s) with a verdict")

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
c.section("the optional narrative pass's prompt")
# `research.auto` governs the *deterministic* verdict run inside the scan;
# `research.narrative` governs this pass. They are separate switches because the
# common case is wanting a graded verdict every night and a written report only
# sometimes -- so auto-prompt must follow the narrative flag, not the auto one.
q_cfg["research"]["auto"].update(enabled=True, gate="all", max_reports=2,
                                 discord_send=False)
q_cfg["research"]["narrative"] = {"enabled": True, "model": "opus"}
research_report.load_config = lambda: q_cfg
prompt = research_report.auto_prompt(q_cfg)
c.ok("auto-prompt invokes the skill", bool(prompt) and prompt.startswith("/deep-dive"))
c.ok("auto-prompt names exactly the capped candidates",
     prompt.splitlines()[0].split()[1:] ==
     list(dict.fromkeys(r["ticker"] for r in every))[:2],
     prompt.splitlines()[0])
c.ok("auto-prompt passes the configured send authorization",
     "send=false" in prompt)
c.ok("the prompt tells the model the verdict already exists",
     "already computed" in prompt and "narrative" in prompt,
     "a pass that re-derives the score would defeat the whole point")
q_cfg["research"]["narrative"]["enabled"] = False
c.ok("auto-prompt declines when the narrative pass is switched off",
     research_report.auto_prompt(q_cfg) is None)
q_cfg["research"]["narrative"]["enabled"] = True
c.ok("...and the deterministic verdict switch does not gate it",
     research_report.auto_prompt(
         {**q_cfg, "research": {**q_cfg["research"],
                                "auto": {**q_cfg["research"]["auto"],
                                         "enabled": False}}}) is not None,
     "the two switches are independent")
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
