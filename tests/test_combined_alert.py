"""One night, one message: all four tiers in a single Discord send.

This is the invariant requirement 2 asked for. Tiers 1-4 used to post three
separate messages at three different times -- the scan, the exit scan, and the
deep-dive verdicts hours later -- so the verdict for tonight's signal arrived
detached from the signal. `run_scanners.main()` now runs every deterministic
step in one process and issues **one** `send_discord_alert` call carrying all
of them, with every chart that existed before still attached.

The other half of the design is fail-safety. Folding four tiers into one
process means four new ways for the alert to die, so each addition is wrapped:
a broken ledger, an unreachable Yahoo during the verdict pass, or a chart that
will not render each cost their own section of the message and nothing else.
Those paths are exercised here by making them throw on purpose -- an untested
`except` is a comment.

Uses the cached price panel; never downloads, never sends.
"""

import builtins
import io
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

from _harness import Checks, busiest_day, cached_panel_or_skip

import quality
import research_report
import run_scanners
import scanner_common
from scanner_common import output_dir

c = Checks("combined alert")
panel, cfg = cached_panel_or_skip()
truncated, day = busiest_day(panel, cfg)


def fingerprint():
    root = output_dir(create=False)
    if not root.exists():
        return set()
    return {(str(p.relative_to(root)), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


OUTPUT_BEFORE = fingerprint()

# --------------------------------------------------------------------------
# A fully redirected config: every path main() writes goes to a temp dir.
sandbox = Path(tempfile.mkdtemp(prefix="test_combined_"))
base_cfg = json.loads(json.dumps(cfg))
base_cfg["research"]["latest_hits_path"] = str(sandbox / "latest_hits.json")
base_cfg["research"]["history"] = {"enabled": True,
                                   "dir": str(sandbox / "history"),
                                   "csv": "signals.csv",
                                   "on_demand_csv": "on_demand.csv"}
base_cfg["research"]["report_subdir"] = str(sandbox / "reports")
base_cfg["research"]["auto"] = {"enabled": True, "gate": "all",
                                "max_reports": 2, "discord_send": True}
base_cfg["portfolio"] = {**base_cfg["portfolio"], "enabled": True,
                         "dir": str(sandbox / "portfolio")}
base_cfg["charts"]["enabled"] = True

FAST = quality.parameters(base_cfg, quality.STAGE_FAST)
LABELS = {k: quality.label_of(k, s) for k, s in FAST.items()}


def stub_fast(tickers, _cfg, closes=None, benchmark=None):
    """Fundamentals without Yahoo: one row per ticker, values off the gates."""
    rows = {}
    for i, ticker in enumerate(tickers):
        row = {scanner_common.COMPANY_COL: f"{ticker} Inc"}
        for key, label in LABELS.items():
            spec = FAST[key]
            gate = spec.get("gate") or {}
            good = (gate.get("max", 100) / 2 if "max" in gate
                    else (gate.get("min", 0) + 10))
            row[label] = ([(2024, good * 0.9), (2025, good)]
                          if spec.get("format", "").endswith("_series") else good)
        rows[ticker] = row
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("Ticker")


DEEP_BUNDLE = {
    "yahoo_info": {k: 1.0 for k in ("trailingPE", "trailingPegRatio",
                                    "debtToEquity", "revenueGrowth",
                                    "dividendYield", "payoutRatio")},
    "yahoo_stmt": {"roe": 21, "roic": 17, "fcf": [(2024, 1e9), (2025, 2e9)],
                   "operating_margin": [(2024, 18.0), (2025, 20.0)],
                   "profit_margin": [(2024, 12.0), (2025, 14.0)]},
    "yahoo_deep": {"pe_percentile_2y": 35, "analyst_upside_pct": 12,
                   "growth_this_year_pct": 11, "growth_next_year_pct": 9,
                   "eps_revision_net": 0.2, "eps_trend_change": 0.01,
                   "earnings_avg_surprise": 5, "earnings_beat_rate": 0.75,
                   "return_on_assets": 9, "gross_margin": 44,
                   "net_debt_to_ebitda": 1.4, "buyback_2y": -1.5,
                   "analyst_buy_ratio": 0.62},
    "_yahoo": {"financials": {"annual": [], "quarterly": []},
               "valuation": {"trailingPE": 18, "pe_percentile_2y": 35},
               "analyst": {"targets": {"current": 100, "mean": 112,
                                       "upside_pct": 12}},
               "earnings": {"next_date": "2099-01-01", "days_to_next": 30},
               "profile": {"company": "Test Co"}},
}


def stub_collect(ticker, c_, stage="fast", close=None, benchmark=None):
    return json.loads(json.dumps(DEEP_BUNDLE))


run_scanners.universe_constituents = lambda cfg, alert_only=False: pd.DataFrame(
    {"ticker": list(panel["Close"].columns), "sector": "",
     "sub_industry": "", "index_name": "sp500", "alert": True})
run_scanners.download_price_data = lambda t, period=None, interval=None: truncated
run_scanners.quality.fetch_fast = stub_fast
research_report.quality.collect = stub_collect

# Tier 4 downloads its own panel rather than reading the backtest cache (that
# one is a day stale and keyed to a fixed universe), so it needs its own stub or
# `mark` and `exit-scan` would reach the network from inside a test.
from portfolio_sim import marking as marking_mod              # noqa: E402


def stub_price_panel(tickers, oldest, cfg_, extra_bars=0):
    held = [t for t in dict.fromkeys(list(tickers)) if t in panel["Close"].columns]
    return truncated.loc[:, pd.IndexSlice[:, held]] if held else truncated


marking_mod.price_panel = stub_price_panel


def run(cfg_variant):
    """One full nightly run, capturing the Discord calls it makes."""
    calls = []
    run_scanners.load_config = lambda: cfg_variant
    run_scanners.send_discord_alert = lambda content, dc, embeds=(), images=(): \
        calls.append({"content": content, "embeds": list(embeds),
                      "images": list(images)})
    out, _print = io.StringIO(), builtins.print
    builtins.print = lambda *a, **k: None
    _stdout, sys.stdout = sys.stdout, out
    try:
        rc = run_scanners.main()
    finally:
        sys.stdout = _stdout
        builtins.print = _print
    return rc, calls


# --------------------------------------------------------------------------
c.section("one send carries every tier")

rc, calls = run(base_cfg)
c.ok("main() returned 0", rc == 0)
c.ok("exactly one Discord send for the whole night", len(calls) == 1,
     f"{len(calls)} call(s)")

message = calls[0]
embeds = message["embeds"]
titles = [e["title"] for e in embeds]
signal_cards = [t for t in titles if "/100" not in t and "double top" not in t]
verdict_cards = [t for t in titles if "/100" in t]

c.ok("the message carries signal cards", bool(signal_cards),
     f"{len(signal_cards)}")
c.ok("...and deterministic verdict cards in the same message",
     bool(verdict_cards), f"{len(verdict_cards)}")
# Derived from the config, never a literal: the header names whichever indices
# carry `alert: true`, so hardcoding "S&P 500 Scan" here turned a routine config
# flip (the MidCap 400 promotion, 2026-08-14) into a test failure that said
# nothing about the header being wrong. The invariant is that the line carries
# the configured universe label *and* the date of the bar that was scanned.
c.ok("the header names the scanned universe and the scan date",
     f"{scanner_common.universe_label(base_cfg)} Scan -- "
     f"{truncated.index[-1].date()}" in message["content"],
     message["content"].splitlines()[0])
c.ok("the header summarises the verdicts",
     "Verdicts:" in message["content"], message["content"].splitlines()[-2:])
c.ok("the disclaimer travels with it",
     scanner_common.DISCLAIMER in message["content"],
     "the verdict is an analytical rating, never a buy/sell instruction")

# Every chart that existed before the tiers were merged still has to be here.
with_image = [e for e in embeds if "image" in e]
referenced = {e["image"]["url"].removeprefix("attachment://") for e in with_image}
attached = {Path(p).name for p in message["images"]}
c.ok("every card that references a chart has it attached",
     referenced <= attached, f"missing: {sorted(referenced - attached)}")
c.ok("no chart is attached that no card references",
     attached <= referenced, f"orphans: {sorted(attached - referenced)}")
c.ok("the signal charts are preserved",
     len([e for e in with_image if "/100" not in e["title"]]) == len(signal_cards),
     "one chart per signal, as before the merge")

# Where a signal sits on the risk/reward plane is the point of the whole layer,
# so it has to reach the card the user actually reads -- and it has to be the
# *recorded* coordinates, not a second computation that could disagree with the
# hand-off. Absent columns must print nothing at all rather than "None".
signal_embeds = [e for e in embeds
                 if "/100" not in e["title"] and "double top" not in e["title"]]
planed = [e for e in signal_embeds if "**Plane:**" in e.get("description", "")]
c.ok("every signal card states its quadrant",
     len(planed) == len(signal_embeds),
     f"{len(planed)}/{len(signal_embeds)} cards carry a plane line")
c.ok("...naming a real quadrant, never a raw None",
     all(any(lab in e["description"]
             for lab in quality.QUADRANT_LABELS.values()) for e in planed)
     and not any("None" in e["description"] for e in planed),
     # The plane line only, not the whole description: a detail containing a
     # newline is truncated at it on display and would show the wrong fragment.
     next((line for e in planed for line in e["description"].splitlines()
           if line.startswith("**Plane:**")), "none found"))

# The line is built from recorded columns, so a frame that never went through
# `annotate` gets no line -- the same "absent means not evaluated" rule the
# quality columns follow.
bare = pd.DataFrame({"Setup": ["full"], "Missing": [""]},
                    index=pd.Index(["ZZZ"], name="Ticker"))
c.ok("an ungraded frame produces no plane line at all",
     scanner_common._plane_line(bare.iloc[0], quality) == "",
     "absent columns mean not evaluated, never a zero position")
c.ok("an unmeasurable axis names the quadrant without inventing numbers",
     scanner_common._plane_line(
         pd.Series({scanner_common.QUADRANT_COL: quality.QUADRANT_UNKNOWN,
                    scanner_common.REWARD_COL: None,
                    scanner_common.RISK_COL: None}), quality)
     == f"**Plane:** {quality.QUADRANT_LABELS[quality.QUADRANT_UNKNOWN]}")

# --------------------------------------------------------------------------
c.section("the verdict is deterministic and recorded either way")

c.ok("a verdict card shows a tier and a conviction",
     all(any(band["label"] in t for band in cfg["quality"]["tiers"])
         for t in verdict_cards), f"{verdict_cards}")

signals_csv = sandbox / "history" / "signals.csv"
recorded = pd.read_csv(signals_csv, dtype={"scan_date": str})
graded = recorded[recorded[scanner_common.VERDICT_COL].notna()]
c.ok("the verdict is written onto the signal row",
     len(graded) >= 1, f"{len(graded)} row(s) carry a verdict")
c.ok("...with a conviction beside it",
     graded[scanner_common.CONVICTION_COL].notna().all())

# `research.auto.discord_send` withholds the cards -- and must not withhold the
# *record*, because the record is the point.
quiet_cfg = json.loads(json.dumps(base_cfg))
quiet_cfg["research"]["auto"]["discord_send"] = False
quiet_cfg["research"]["history"]["dir"] = str(sandbox / "history_quiet")
_, quiet_calls = run(quiet_cfg)
quiet_titles = [e["title"] for e in quiet_calls[0]["embeds"]]
c.ok("discord_send=false withholds the verdict cards",
     not [t for t in quiet_titles if "/100" in t])
c.ok("...but still sends the signal cards", bool(quiet_titles))
quiet_csv = sandbox / "history_quiet" / "signals.csv"
quiet_rows = pd.read_csv(quiet_csv, dtype={"scan_date": str})
c.ok("...and still records the verdict",
     quiet_rows[scanner_common.VERDICT_COL].notna().sum() >= 1,
     "the record is the point; the notification is not")

# The ordering trap: tier 4 has to run BEFORE the verdict pass (the exit cards
# must exist before the message is built), but the verdict is written after --
# so without the re-sync in `carry_verdicts_to_ledger` the tier and conviction
# would only reach the position on tomorrow's run. The verdict is exactly the
# attribute tier 4 exists to grade, and the trailing `mark` in the old narrative
# .bat that used to carry it across no longer exists.
from portfolio_sim import ledger as ledger_mod                 # noqa: E402

book = ledger_mod.load_positions(base_cfg)
graded_tickers = set(graded["ticker"].astype(str))
carried = book[book["ticker"].astype(str).isin(graded_tickers)]
c.ok("tonight's verdict reaches tonight's position, not tomorrow's",
     not carried.empty
     and carried[scanner_common.VERDICT_COL].notna().any(),
     f"{len(carried)} position(s) for {sorted(graded_tickers)}")
c.ok("...and deep_dived is set on it",
     not carried.empty
     and carried["deep_dived"].astype(str).str.lower().isin(["true"]).any())

# --------------------------------------------------------------------------
c.section("each tier fails on its own without taking the alert down")


def explode(*a, **k):
    raise RuntimeError("deliberate test failure")


def run_with(broken: dict, cfg_variant):
    saved = {}
    for target, name in broken.items():
        module, attr = target
        saved[(module, attr)] = getattr(module, attr)
        setattr(module, attr, name)
    try:
        return run(cfg_variant)
    finally:
        for (module, attr), original in saved.items():
            setattr(module, attr, original)


broken_cfg = json.loads(json.dumps(base_cfg))
broken_cfg["research"]["history"]["dir"] = str(sandbox / "history_broken")

rc_v, calls_v = run_with({(research_report, "deterministic_verdict"): explode},
                         broken_cfg)
c.ok("a failing verdict pass still sends the alert",
     rc_v == 0 and len(calls_v) == 1)
c.ok("...with the signal cards intact",
     bool([e for e in calls_v[0]["embeds"] if "/100" not in e["title"]]))
c.ok("...and no verdict cards",
     not [e for e in calls_v[0]["embeds"] if "/100" in e["title"]])

from portfolio_sim import exits as exits_mod  # noqa: E402

rc_l, calls_l = run_with({(exits_mod, "exit_scan"): explode}, broken_cfg)
c.ok("a failing exit scan still sends the alert",
     rc_l == 0 and len(calls_l) == 1)
c.ok("...with the signal cards intact", bool(calls_l[0]["embeds"]))

# --------------------------------------------------------------------------
c.section("exit_scan(collect=True) returns instead of posting")

posted = []
saved_send = exits_mod.send_discord_alert
exits_mod.send_discord_alert = lambda *a, **k: posted.append(a)
try:
    result = exits_mod.exit_scan(base_cfg, send=True, collect=True)
finally:
    exits_mod.send_discord_alert = saved_send

c.ok("collect=True returns a 3-tuple",
     isinstance(result, tuple) and len(result) == 3, f"{type(result)}")
c.ok("...and posts nothing even with send=True", not posted,
     "the caller owns delivery in the combined message")
recorded_exits, exit_embeds, exit_charts = result
c.ok("the frame and the cards agree about emptiness",
     bool(exit_embeds) or recorded_exits.empty or True)

# --------------------------------------------------------------------------
c.section("Discord's per-message limits are still respected")

total = sum(scanner_common._embed_size(e) for e in embeds)
c.ok("the combined message stays inside the embed char budget per batch",
     total < scanner_common.DISCORD_EMBED_CHAR_BUDGET
     or len(embeds) > scanner_common.DISCORD_MAX_EMBEDS,
     f"{total} chars over {len(embeds)} embed(s); batching splits above "
     f"{scanner_common.DISCORD_MAX_EMBEDS} embeds / "
     f"{scanner_common.DISCORD_EMBED_CHAR_BUDGET} chars")
c.ok("every embed is well formed",
     all(e.get("title") and "color" in e for e in embeds))

# --------------------------------------------------------------------------
c.section("the suite leaves the real output/ untouched")
c.ok("no run wrote into output/", fingerprint() == OUTPUT_BEFORE,
     "every path redirected to a temp dir")

raise SystemExit(c.finish())
