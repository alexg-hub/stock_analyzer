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
    sample cannot produce a confident finding;
  * the double-top exit **flags** a position rather than closing it, sells at
    `Open[t+1]` like everything else, and is idempotent -- a second run adds no
    second sell row and re-alerts nothing.

The exit fixtures are built *from the thresholds in force*, never against
hardcoded prices: `_m_series` derives its peak heights and trough depth from
`cfg["exit_strategy"]`, so retuning the section moves the fixture with it
instead of staling the test.
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
from portfolio_sim import analysis, exits, ledger, marking
from portfolio_sim.stats import (
    benjamini_hochberg,
    bootstrap_diff_ci,
    mann_whitney,
    spearman,
)
from scanner_common import (
    exits_csv_path,
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
              findings_csv_path(REAL, create=False),
              exits_csv_path(REAL, create=False)]
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


# `extra_bars` is keyword-only at the call site in exits.py, so the stub has to
# swallow it -- a positional-only stub would pass here and fail there.
marking.price_panel = lambda tickers, oldest, config_, **_kw: PANEL
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
c.section("exit-scan -- the double top, built from the thresholds in force")

EX = copy.deepcopy(cfg["exit_strategy"])
EX.update({"recent_window_days": 5, "prior_window_days": 10})
XDAYS = pd.bdate_range("2026-03-02", periods=34)
BREAK_AT = 30                       # leaves one bar after it, so the sale fills


def _m_series(n, t, ex, peak_diff=None, depth=None):
    """An M whose geometry is derived from `ex`, breaking its neckline at `t`.

    The windows the rule actually uses are, at bar `t`:
        peak 1  = max over [t-recent-prior, t-recent-1]
        peak 2  = max over [t-recent,       t-1]
        trough  = min over [t-recent,       t-1]
    so the second peak *and* the valley both live in the recent window. The
    series is placed against those bounds rather than against a drawing of an
    M, because that is what the code reads.
    """
    recent, prior = int(ex["recent_window_days"]), int(ex["prior_window_days"])
    confirm = float(ex["break_confirm_pct"])
    if peak_diff is None:                       # comfortably "equal" peaks
        peak_diff = float(ex["max_peak_diff_pct"]) / 4
    if depth is None:                           # comfortably deep valley
        depth = float(ex["min_trough_depth_pct"]) * 2
    peak1, peak2 = 100.0, 100.0 * (1 + peak_diff)
    floor_ = min(peak1, peak2) * (1 - depth)

    s = np.full(n, floor_)
    s[t - recent - 2] = peak1                   # inside the prior window
    s[t - recent] = peak2                       # first bar of the recent window
    for k in range(t - recent + 1, t):          # decline into the trough at t-1
        s[k] = peak2 + (floor_ - peak2) * (k - (t - recent)) / (recent - 1)
    s[t:] = floor_ * (1 - confirm) * 0.99       # the break, then flat
    assert prior >= 3, "the prior window must hold peak 1"
    return s


# One ticker per case. Open == High == Low == Close, so a peak is exactly the
# number the fixture put there and the next open is exactly readable.
XSERIES = {
    "DTP": _m_series(len(XDAYS), BREAK_AT, EX),                       # fires
    "PND": _m_series(len(XDAYS), len(XDAYS) - 1, EX),                 # fires last bar
    "WIDE": _m_series(len(XDAYS), BREAK_AT, EX,
                      peak_diff=float(EX["max_peak_diff_pct"]) * 3),  # peaks unequal
    "SHLW": _m_series(len(XDAYS), BREAK_AT, EX,
                      depth=float(EX["min_trough_depth_pct"]) / 3),   # valley too shallow
    "UPP": np.linspace(80.0, 130.0, len(XDAYS)),                      # never fires
    "SPY": np.linspace(100.0, 110.0, len(XDAYS)),
}
xframes = {}
for ticker, series in XSERIES.items():
    for field in ("Open", "Close", "High", "Low", "Adj Close"):
        xframes[(field, ticker)] = series
    xframes[("Volume", ticker)] = np.full(len(XDAYS), 1_000_000.0)
XPANEL = pd.DataFrame(xframes, index=XDAYS)
XPANEL.columns = pd.MultiIndex.from_tuples(XPANEL.columns)

xsig = exits.compute_double_top(XPANEL, EX)["signal"]
c.ok("the M fires exactly once, on the bar that breaks the neckline",
     xsig["DTP"].sum() == 1 and xsig["DTP"].iloc[BREAK_AT],
     f"{int(xsig['DTP'].sum())} signal(s), "
     f"{[str(d.date()) for d in xsig.index[xsig['DTP']]]}")
c.ok("a monotone uptrend never fires", not xsig["UPP"].any())
c.ok("peaks further apart than max_peak_diff_pct do not fire",
     not xsig["WIDE"].any(),
     "two unequal highs are a trend that pulled back, not a double top")
c.ok("a valley shallower than min_trough_depth_pct does not fire",
     not xsig["SHLW"].any(),
     "without it, sideways drift has 'equal peaks' and fires on every wobble")
c.ok("required_history covers both windows",
     exits.required_history(EX) == 1 + EX["recent_window_days"]
     + EX["prior_window_days"])

# A name that stays under its neckline satisfies the break test every night.
loose = {**EX, "alert_only_on_break": False}
c.ok("without the fresh-break guard the same break can repeat",
     exits.compute_double_top(XPANEL, loose)["signal"]["DTP"].sum()
     >= xsig["DTP"].sum(),
     "which is exactly why alert_only_on_break defaults to true")

# --------------------------------------------------------------------------
c.section("exit-scan -- flags the position, sells at Open[t+1], never repeats")

# Its own ledger, and horizons long enough that nothing settles -- an exit rule
# only ever looks at positions still open, so a fixture whose positions all
# closed would test nothing.
xcfg = copy.deepcopy(cfg)
xcfg["portfolio"] = {**cfg["portfolio"], "dir": str(TMP / "xportfolio"),
                     "horizons": [40, 60], "exits_csv": "exits.csv"}
xcfg["research"] = copy.deepcopy(cfg["research"])
xcfg["research"]["history"]["dir"] = str(TMP / "xhistory")
xcfg["exit_strategy"] = EX
xcfg["charts"] = {**cfg.get("charts", {}), "enabled": False}
# Empty webhook -> send_discord_alert prints instead of posting. The send path
# is exercised; the network is not.
xcfg["discord"] = {**cfg.get("discord", {}), "webhook_url": ""}

Path(xcfg["research"]["history"]["dir"]).mkdir(parents=True, exist_ok=True)
pd.DataFrame([{"scan_date": XDAYS[1].date().isoformat(),
               "config_key": "breakout_strategy", "screen": "Breakout",
               "ticker": ticker, "Company": f"{ticker} Inc", "Setup": "full",
               "Missing": "", "Close": float(XSERIES[ticker][1])}
              for ticker in ("DTP", "PND", "WIDE", "SHLW", "UPP")
              ]).to_csv(signals_csv_path(xcfg), index=False)

marking.price_panel = lambda tickers, oldest, config_, **_kw: XPANEL
ledger.sync(xcfg)
xbook = marking.mark(xcfg)
c.ok("the exit fixture's positions are open and priced",
     (xbook["status"].astype(str) == ledger.STATUS_OPEN).all()
     and pd.to_numeric(xbook["entry_price"], errors="coerce").notna().all(),
     str(dict(xbook["status"].astype(str).value_counts())))

xbefore = xbook.copy()
sold = exits.exit_scan(xcfg)
xafter = ledger.load_positions(xcfg)
flagged = xafter[xafter[ledger.EXIT_FLAG_COL].notna()]
c.ok("only the two genuine double tops are flagged",
     set(flagged["ticker"].astype(str)) == {"DTP", "PND"},
     str(sorted(flagged["ticker"].astype(str))))

dtp = flagged[flagged["ticker"] == "DTP"].iloc[0]
c.same_date("the flag names the bar that broke the neckline",
            pd.Timestamp(dtp[ledger.EXIT_FLAG_COL]), XDAYS[BREAK_AT])
c.close("the sale is literally the next bar's open",
        float(dtp["dt_exit_price"]), float(XPANEL["Open"]["DTP"].iloc[BREAK_AT + 1]),
        tol=5e-5)
c.close("the recorded return is exit over entry",
        float(dtp["dt_ret_%"]),
        100 * (float(dtp["dt_exit_price"]) / float(dtp["entry_price"]) - 1),
        tol=1e-3)
c.ok("the pattern's own numbers are recorded with it",
     all(pd.notna(dtp[k]) for k in ("dt_peak1", "dt_peak2", "dt_neckline"))
     and float(dtp["dt_neckline"]) < float(dtp["dt_peak1"]),
     "so a later analysis can grade the pattern, not just its occurrence")

# The whole point of the design: the fixed horizons keep running, so "sold on
# the double top" and "held to the horizon" stay two measurements of one
# position rather than two different populations.
c.ok("the position is flagged, not closed",
     str(dtp["status"]) == ledger.STATUS_OPEN, str(dtp["status"]))
untouched = ["status", "entry_price", "entry_date", "shares"] + \
    [ledger.horizon_cols(h)["ret"] for h in ledger.horizons_of(xcfg["portfolio"])]
c.ok("no entry-side column is disturbed by an exit",
     all(list(xafter[col].astype(str)) == list(xbefore[col].astype(str))
         for col in untouched if col in xbefore.columns),
     "flag-don't-close is the only reason the two exits stay comparable")

# A break on the final bar has no next open to sell into. It is still a signal
# worth announcing -- but a sell record with no exit price is not a sell.
pnd = flagged[flagged["ticker"] == "PND"].iloc[0]
c.ok("a break on the last bar is pending, not a sale at the close",
     str(pnd["dt_status"]) == ledger.EXIT_PENDING, str(pnd["dt_status"]))
c.ok("...and carries no exit price",
     pd.isna(pd.to_numeric(pnd.get("dt_exit_price"), errors="coerce")))

sells = ledger.read_table(exits_csv_path(xcfg))
c.ok("the sell record holds only the settled sale",
     set(sells["ticker"].astype(str)) == {"DTP"}, str(list(sells["ticker"])))
c.ok("the sell record is the narrow schema asked for",
     list(sells.columns) == ["position_id", "ticker", "name", "entry_date",
                             "entry_price", "exit_date", "exit_price", "ret_%"],
     str(list(sells.columns)))
c.ok("it names the company, not just the symbol",
     str(sells["name"].iloc[0]) == "DTP Inc")
c.ok("exit_scan returns what it recorded", len(sold) == len(sells))

# --------------------------------------------------------------------------
c.section("exit-scan -- re-running changes nothing, and sync never erases it")

again = exits.exit_scan(xcfg)
sells2 = ledger.read_table(exits_csv_path(xcfg))
c.ok("a second run records no second sell",
     len(again) == 0 and len(sells2) == len(sells),
     f"{len(sells2)} row(s) both times")
c.ok("...and re-flags nothing",
     len(ledger.load_positions(xcfg)[
         ledger.load_positions(xcfg)[ledger.EXIT_FLAG_COL].notna()])
     == len(flagged),
     "an already-flagged position has had its answer")

# `sync` rewrites the whole ledger from the source table every night. Without
# EXIT_COLS in `protect`, that would erase a recorded exit with no error --
# the same trap the tier-3 verdict carry exists to close.
ledger.sync(xcfg)
resynced = ledger.load_positions(xcfg)
c.ok("a re-sync preserves every recorded exit column",
     all(list(resynced[col].astype(str)) == list(xafter[col].astype(str))
         for col in ledger.EXIT_COLS if col in xafter.columns),
     "EXIT_COLS must stay inside mark_columns()")
c.ok("the exit columns are reserved, so a source table cannot collide with them",
     set(ledger.EXIT_COLS)
     <= ledger.reserved_columns(ledger.horizons_of(xcfg["portfolio"])))

# A closed position has no horizon left to shorten; a pending one has no entry
# price to sell against. Neither is an exit candidate.
mixed = resynced.copy()
mixed["status"] = ledger.STATUS_CLOSED
c.ok("closed positions are never exit candidates", not exits._held(mixed).any())
mixed["status"] = ledger.STATUS_PENDING
c.ok("pending positions are never exit candidates", not exits._held(mixed).any())

off = copy.deepcopy(xcfg)
off["exit_strategy"] = {**EX, "enabled": False}
c.ok("exit_strategy.enabled false is a clean no-op",
     exits.exit_scan(off).empty)

# --------------------------------------------------------------------------
c.section("nothing leaked into the real output/")

c.ok("the ledger written is the redirected one",
     str(positions_csv_path(cfg, create=False)).startswith(str(TMP))
     and positions_csv_path(cfg, create=False).exists())
c.ok("the findings written are the redirected ones",
     str(findings_csv_path(cfg, create=False)).startswith(str(TMP))
     and findings_csv_path(cfg, create=False).exists())
c.ok("the sell record written is the redirected one",
     str(exits_csv_path(xcfg, create=False)).startswith(str(TMP))
     and exits_csv_path(xcfg, create=False).exists())
# A real ledger may legitimately exist already -- what must not happen is this
# run touching it. Fingerprints, not existence.
after_files = [_fingerprint(p) for p in REAL_FILES]
c.ok("the real output/portfolio files are byte-for-byte untouched",
     after_files == REAL_BEFORE,
     f"{[p.name for p in REAL_FILES]}")

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(c.finish())
