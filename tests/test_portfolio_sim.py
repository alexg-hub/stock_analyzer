"""Tier 4: the ledger, the marking arithmetic and the attribution engine.

Offline and self-contained -- a synthetic panel and synthetic history tables in
a temp directory, so nothing downloads and nothing touches the real `output/`.

Invariants, not recorded numbers. The ones that matter:

  * the entry price is `Open[t+1]` and agrees **cell for cell** with
    `backtest_universe.forward_trades` -- one next-day-open convention in the
    repo, not two that drift;
  * re-running `open` never erases a mark, and never re-freezes the recorded
    quality rule set;
  * a signal whose entry bar has not traded stays `pending` with no price, and
    is absent from every closed-trade statistic;
  * `sufficient_n` and the FDR-adjusted `q_value` gate every claim, so a thin
    sample cannot produce a confident finding.
"""

import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from _harness import Checks, config

import backtest_universe
from portfolio_sim import analysis, ledger, marking
from portfolio_sim.stats import (
    benjamini_hochberg,
    bootstrap_diff_ci,
    mann_whitney,
    spearman,
)
from scanner_common import (
    findings_csv_path,
    positions_csv_path,
    signals_csv_path,
)

c = Checks("portfolio_sim -- ledger, marking, attribution")


def _fingerprint(path: Path):
    """Existence + mtime + size, so a pre-existing real ledger is fine but a
    write into one is not."""
    return (path.exists(),
            path.stat().st_mtime_ns if path.exists() else None,
            path.stat().st_size if path.exists() else None)


REAL = config()
REAL_FILES = [positions_csv_path(REAL, create=False),
              findings_csv_path(REAL, create=False)]
REAL_BEFORE = [_fingerprint(p) for p in REAL_FILES]

TMP = Path(tempfile.mkdtemp(prefix="tier4_"))
HORIZONS = [2, 5]

cfg = copy.deepcopy(config())
cfg["portfolio"] = {"enabled": True, "dir": str(TMP / "portfolio"),
                    "positions_csv": "positions.csv",
                    "findings_csv": "findings.csv", "entry": "next_open",
                    "horizons": HORIZONS, "notional": 10000,
                    "benchmark_ticker": "SPY", "measure_excursions": True,
                    "analysis": {"min_n": 4, "alpha": 0.05,
                                 "bootstrap_iters": 200, "fdr": True,
                                 "keep_dated_findings": False}}
cfg["research"]["history"]["dir"] = str(TMP / "history")
cfg["research"]["report_subdir"] = str(TMP / "reports")

# --------------------------------------------------------------------------
c.section("Fixtures -- a synthetic panel and a synthetic signal history")

DAYS = pd.bdate_range("2026-01-05", periods=30)
TICKERS = ["AAA", "BBB", "SPY"]
rng = np.random.default_rng(11)
frames = {}
for i, ticker in enumerate(TICKERS):
    base = 100 + np.cumsum(rng.normal(0.2 * i, 0.9, len(DAYS)))
    frames[("Open", ticker)] = base
    frames[("Close", ticker)] = base * 1.003
    frames[("High", ticker)] = base * 1.02
    frames[("Low", ticker)] = base * 0.98
    frames[("Adj Close", ticker)] = base * 1.003
    frames[("Volume", ticker)] = 1_000_000.0
PANEL = pd.DataFrame(frames, index=DAYS)
PANEL.columns = pd.MultiIndex.from_tuples(PANEL.columns)

# The last signal sits on the panel's final bar, so its entry bar cannot have
# traded -- that is the `pending` case, deliberately built from the data rather
# than hardcoded.
SIGNAL_DAYS = list(DAYS[2:12]) + [DAYS[-1]]
rows = []
for n, day in enumerate(SIGNAL_DAYS):
    ticker = TICKERS[n % 2]
    passes = n % 2 == 0
    rows.append({
        "scan_date": day.date().isoformat(),
        "generated_at": "2026-01-05T00:00:00+00:00",
        "config_key": "breakout_strategy" if n % 2 else "pullback_strategy",
        "screen": "Breakout" if n % 2 else "Pullback",
        "ticker": ticker,
        "Close": 100.0 + n, "Range %": 20.0 + n, "Vol Ratio": 1.4 + n / 20,
        "Setup": "partial" if n % 3 == 0 else "full",
        "Missing": "range too wide" if n % 3 == 0 else "",
        "Company": f"{ticker} Inc", "P/E": 18.0 + n, "ROE": 20.0 - n,
        "FCF": json.dumps([[2024, 1e9], [2025, 1.1e9 + n * 1e7]]),
        "Quality": passes,
        "Quality Missing": json.dumps([] if passes else ["roe", "debtToEquity"]),
        "Verdict": "STRONG" if n % 4 == 0 else ("WATCH" if n % 4 == 2 else ""),
        "Conviction": 85 - n if n % 2 == 0 else "",
    })
HISTORY = Path(cfg["research"]["history"]["dir"])
HISTORY.mkdir(parents=True, exist_ok=True)
pd.DataFrame(rows).to_csv(signals_csv_path(cfg), index=False)

REPORTS = Path(cfg["research"]["report_subdir"])
REPORTS.mkdir(parents=True, exist_ok=True)
(REPORTS / f"{rows[0]['ticker']}_{rows[0]['scan_date']}_facts.json").write_text(
    json.dumps({"ticker": rows[0]["ticker"], "scan_date": rows[0]["scan_date"],
                "quant_score": 71.0,
                "quant_dimensions": {"valuation": {"score": 0.4},
                                     "growth": {"score": 0.9}},
                "quant_metrics": {"gross_margin": 55.0},
                "upside_pct": 14.0, "source": "signal"}), encoding="utf-8")

# No test may download. If anything reaches for the network the fixture is
# wrong, and a silent 500-ticker fetch is exactly what `tests/CLAUDE.md`
# forbids.
def _no_network(*_args, **_kwargs):
    raise AssertionError("a test tried to download price data")


marking.price_panel = lambda tickers, oldest, config_: PANEL
marking.download_price_data = _no_network

c.ok("panel and history built", len(PANEL) == 30 and len(rows) == 11,
     f"{len(PANEL)} bars, {len(rows)} recorded signals")

# --------------------------------------------------------------------------
c.section("open -- every recorded row becomes a position")

book = ledger.sync(cfg)
c.ok("one position per recorded signal", len(book) == len(rows),
     f"{len(book)} vs {len(rows)}")
c.ok("failing rows are kept, not filtered out",
     set(book["Setup"].astype(str)) == {"full", "partial"}
     and set(book["quality_pass"].astype(str)) == {"True", "False"},
     "the control cohorts are what make attribution possible")
c.ok("every new position starts pending",
     (book["status"].astype(str) == ledger.STATUS_PENDING).all())

# A night on which no screen fires archives an empty row set, and
# `merge_history_csv` writes that as a **zero-byte** signals.csv -- which
# `pd.read_csv` refuses to parse. Caught by the 2026-07-27 nightly shakedown:
# a fresh install whose first night was quiet logged a ledger failure until
# something finally fired.
quiet = TMP / "quiet.csv"
quiet.write_text("", encoding="utf-8")
c.ok("a headerless CSV from a zero-signal night reads as empty, not an error",
     ledger.read_table(quiet).empty)
c.ok("...and so does a file that does not exist at all",
     ledger.read_table(TMP / "never_written.csv").empty)

# --------------------------------------------------------------------------
c.section("open -- the quality rules explode point-in-time")

configured = list(cfg["fundamentals"]["quality"]["rules"])
failing = book[book["quality_pass"].astype(str) == "False"].iloc[0]
passing = book[book["quality_pass"].astype(str) == "True"].iloc[0]
c.ok("a recorded failure reads as failed",
     ledger.as_bool(failing["qr_roe"]) is False
     and ledger.as_bool(failing["qr_debtToEquity"]) is False)
c.ok("a configured rule absent from the failed list reads as passed",
     all(ledger.as_bool(failing[f"qr_{k}"]) is True
         for k in configured if k not in ("roe", "debtToEquity")))
c.ok("a clean row passes every rule",
     all(ledger.as_bool(passing[f"qr_{k}"]) is True for k in configured))
c.ok("the rule set in force is recorded with the position",
     sorted(json.loads(failing[ledger.QUALITY_RULES_COL])) == sorted(configured))

# A row the scan never graded must not read as "failed everything" -- absent
# quality is missing data, not a verdict.
ungraded = pd.DataFrame([{**rows[0], "ticker": "CCC", "Quality": "",
                          "Quality Missing": ""}])
c.ok("an ungraded row gets no per-rule flags at all",
     not any(k.startswith(ledger.QR_PREFIX)
             for k in ledger._position_rows(ungraded, "signal", cfg, {})[0]))

# --------------------------------------------------------------------------
c.section("mark -- the entry is Open[t+1], and it agrees with forward_trades")

book = marking.mark(cfg)
truth = backtest_universe.forward_trades(PANEL, HORIZONS[0], "next_open",
                                         True, delay=0, dates=True)
# The ledger rounds what it stores -- a CSV a human reads should not carry
# fifteen significant figures of a share count. So the tolerances below are the
# rounding unit, not a fudge: anything looser would stop testing the identity.
PRICE_TOL, SHARE_TOL = 5e-5, 5e-7
checked = mismatch = 0
for _, row in book.iterrows():
    day = pd.Timestamp(row["scan_date"])
    ticker = str(row["ticker"])
    want = truth["entry_price"].at[day, ticker]
    got = pd.to_numeric(row["entry_price"], errors="coerce")
    if pd.isna(want):
        continue
    checked += 1
    if pd.isna(got) or abs(float(got) - float(want)) > PRICE_TOL:
        mismatch += 1
c.ok("entry price matches forward_trades cell for cell",
     checked and not mismatch, f"{checked} filled position(s), {mismatch} off")

first = book.iloc[0]
day0 = pd.Timestamp(first["scan_date"])
c.close("entry price is literally the next bar's open",
        float(first["entry_price"]),
        float(PANEL["Open"][str(first["ticker"])].shift(-1).at[day0]),
        tol=PRICE_TOL)
c.close("shares are notional / entry price",
        float(first["shares"]), 10000 / float(first["entry_price"]),
        tol=SHARE_TOL)
c.close("the 2-day return matches the panel",
        float(first[ledger.horizon_cols(2)["ret"]]),
        float(truth["return_pct"].at[day0, str(first["ticker"])]), tol=PRICE_TOL)

# --------------------------------------------------------------------------
c.section("mark -- an untraded entry bar stays pending, not a 0% trade")

last = book[book["scan_date"] == DAYS[-1].date().isoformat()].iloc[0]
c.ok("a signal on the final bar is still pending",
     str(last["status"]) == ledger.STATUS_PENDING, str(last["status"]))
c.ok("and carries no entry price", pd.isna(pd.to_numeric(last["entry_price"],
                                                         errors="coerce")))
for horizon in HORIZONS:
    settled = analysis.closed(book, horizon)
    c.ok(f"pending rows are excluded from the {horizon}d cohort",
         last["position_id"] not in set(settled["position_id"]),
         f"{len(settled)} settled of {len(book)}")

c.ok("tier 3's quant breakdown is carried onto the position it judged",
     "quant_growth" in book.columns
     and pd.to_numeric(book["quant_growth"], errors="coerce").notna().any(),
     "without it, 'which deep-dive check paid' has nothing to grade")

# --------------------------------------------------------------------------
c.section("open again -- refreshes attributes, never erases marks")

before = book.copy()
after = ledger.sync(cfg)
c.ok("no positions are added or lost", len(after) == len(before))
priced = pd.to_numeric(after["entry_price"], errors="coerce").notna().sum()
c.ok("every filled entry price survives the re-sync",
     priced == pd.to_numeric(before["entry_price"], errors="coerce").notna().sum(),
     f"{priced} still priced")
c.ok("returns survive the re-sync",
     pd.to_numeric(after[ledger.horizon_cols(2)["ret"]], errors="coerce").notna().sum()
     == pd.to_numeric(before[ledger.horizon_cols(2)["ret"]], errors="coerce").notna().sum())
c.ok("statuses survive the re-sync",
     list(after["status"].astype(str)) == list(before["status"].astype(str)))

# The frozen rule set must not follow a config retune: a rule invented after a
# signal was recorded was never evaluated against it.
retuned = copy.deepcopy(cfg)
retuned["fundamentals"]["quality"]["rules"]["invented_later"] = {"min": 1}
ledger.sync(retuned)
reloaded = ledger.load_positions(cfg)
c.ok("a rule added after the fact does not appear on an old position",
     "invented_later" not in json.loads(
         reloaded[ledger.QUALITY_RULES_COL].iloc[0]),
     "the recorded rule set is frozen at first sight")

# --------------------------------------------------------------------------
c.section("statistics -- pinned against hand-computed values")

mw = mann_whitney([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
c.close("Mann-Whitney U on fully separated samples", mw["u"], 0.0)
c.close("P(a > b) is 0 when a is entirely below b", mw["prob_superior"], 0.0)
c.close("its z carries the continuity correction", mw["z"], -12.0 / np.sqrt(275 / 12),
        tol=1e-9)

tied = mann_whitney([1, 1, 2], [1, 3, 3])
c.close("U is right with ties (average ranks)", tied["u"], 2.0)
c.close("P(a > b) with ties", tied["prob_superior"], 2 / 9, tol=1e-12)

sp = spearman([1, 2, 3, 4, 5], [2, 1, 4, 3, 5])
c.close("Spearman rho = 1 - 6*sum(d^2)/(n(n^2-1))", sp["rho"], 0.8, tol=1e-9)

q = benjamini_hochberg([0.01, 0.02, 0.03, 0.04, 0.05])
c.ok("BH q-values for the textbook uniform ladder are all alpha",
     all(abs(v - 0.05) < 1e-9 for v in q), str(q))
c.ok("BH is monotone non-decreasing in p",
     all(a <= b + 1e-12 for a, b in zip(q, q[1:])))
holes = benjamini_hochberg([0.01, None, float("nan"), 0.5])
c.ok("BH ignores untestable rows rather than counting them",
     np.isnan(holes[1]) and np.isnan(holes[2]) and abs(holes[0] - 0.02) < 1e-12,
     str(holes))

lo, hi = bootstrap_diff_ci([1, 2, 3, 4, 5], [1, 2, 3, 4, 5], iters=400)
c.ok("a bootstrap CI on identical samples straddles zero", lo <= 0 <= hi,
     f"[{lo:.3f}, {hi:.3f}]")

# --------------------------------------------------------------------------
c.section("analyze -- every question is asked, and none is over-claimed")

findings = analysis.analyze(cfg, baseline=False)
kinds = set(findings["analysis"])
c.ok("the caveats lead the file",
     findings["analysis"].iloc[0] == "CAVEAT" and (findings["analysis"] == "CAVEAT").sum() >= 5,
     "they are the first thing a reader sees")
for kind in ("portfolio", "roadmap", "cohort", "split", "rule_impact",
             "metric_corr", "ranking"):
    c.ok(f"the {kind} section is present", kind in kinds)

# The roadmap is what makes an empty report legible: without it a question with
# no settled trades is simply missing, and a reader cannot tell "not asked"
# from "asked and came back empty".
roadmap = findings[findings["analysis"] == "roadmap"]
c.ok("the roadmap names every question, answerable or not",
     {"which screen paid", "full setup versus partial",
      "did the tier-2 quality badge predict returns"} <= set(roadmap["dimension"])
     and any(d.startswith("quality rule") for d in roadmap["dimension"]),
     f"{len(roadmap)} question(s) tracked")
c.ok("the roadmap states the shortfall in its own conclusion",
     roadmap["conclusion"].str.contains("recorded|position").all())
c.ok("every finding carries a generated conclusion",
     findings["conclusion"].notna().all())
c.ok("the columns are the declared schema",
     list(findings.columns) == analysis.FINDINGS_COLUMNS)

tests = findings[findings["p_value"].notna()]
c.ok("every test gets an FDR-adjusted q", tests["q_value"].notna().all(),
     f"{len(tests)} test(s)")
c.ok("q is never below its own p", (tests["q_value"] >= tests["p_value"] - 1e-12).all(),
     "an adjustment that shrank a p-value would be backwards")
thin = tests[~tests["sufficient_n"].astype(bool)]
c.ok("nothing under min_n is ever called significant",
     not thin["significant"].astype(bool).any(), f"{len(thin)} thin test(s)")
c.ok("a thin finding says so in its own conclusion",
     thin.empty or thin["conclusion"].str.contains("INSUFFICIENT SAMPLE").all())

# Ticker-level attributes must be de-duplicated, or a name that fired on two
# screens votes twice in exactly the cohorts a deep dive touched.
dupes = analysis.dedup_by_ticker(book)
c.ok("ticker-level analysis de-duplicates (scan_date, ticker)",
     len(dupes) == book.drop_duplicates(subset=["scan_date", "ticker"]).shape[0]
     and len(dupes) <= len(book))

quality_split = findings[(findings["analysis"] == "split")
                         & (findings["dimension"] == "quality badge")]
c.ok("the quality badge is graded against its own failures",
     not quality_split.empty
     and set(quality_split["comparison"]) == {"failed"})
rule_dims = set(findings[findings["analysis"] == "rule_impact"]["dimension"])
c.ok("each quality rule that actually split the sample is graded",
     {"qr_roe", "qr_debtToEquity"} <= rule_dims, str(sorted(rule_dims)))

ranked = findings[findings["analysis"] == "ranking"]
c.ok("the ranking says plainly when nothing cleared FDR",
     ranked.empty
     or bool(findings["significant"].fillna(False).any())
     or ranked["conclusion"].str.contains("NOTHING cleared FDR").any(),
     "a ranking that reads like a result on a null sample is the failure mode")

# --------------------------------------------------------------------------
c.section("nothing leaked into the real output/")

c.ok("the ledger written is the redirected one",
     str(positions_csv_path(cfg, create=False)).startswith(str(TMP))
     and positions_csv_path(cfg, create=False).exists())
c.ok("the findings written are the redirected ones",
     str(findings_csv_path(cfg, create=False)).startswith(str(TMP))
     and findings_csv_path(cfg, create=False).exists())
# A real ledger may legitimately exist already -- what must not happen is this
# run touching it. Fingerprints, not existence.
after_files = [_fingerprint(p) for p in REAL_FILES]
c.ok("the real output/portfolio files are byte-for-byte untouched",
     after_files == REAL_BEFORE,
     f"{[p.name for p in REAL_FILES]}")

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(c.finish())
