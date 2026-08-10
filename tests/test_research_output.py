"""Tier 3's *output* layer: the financials collector, the chart, the markdown
table and the Discord verdict cards.

Every check is an **invariant** -- nothing here asserts a recorded figure, since
the collector reads live Yahoo data that changes daily. What is pinned instead:

  * the margin falls back to pretax exactly when Operating Income is absent,
    and says which basis it used
  * an empty period never consumes one of the N slots
  * the chart renders for a complete series, an all-missing one, a single
    period, and negative values -- a chart must never raise
  * tier 2 is untouched by the pretax fallback: `_statement_metrics` still uses
    Operating Income only, so the badge and the deep-dive gate cannot drift
  * a verdict card binds its chart as an attachment and stays inside Discord's
    embed budget, and its figures come from the facts file rather than the
    caller

Fully offline: the collector is driven with hand-built statement frames, so no
test here touches the network or Discord.
"""

import io
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

from _harness import Checks

import charts
import derived
import research_collect
import research_report
import run_scanners
import quality
import scanner_common

c = Checks("tier-3 output")

# --------------------------------------------------------------------------
# A fake yf.Ticker exposing only the six statement frames the collector reads.

PERIODS = [pd.Timestamp(f"202{y}-12-31") for y in (2, 3, 4, 5)]
QUARTERS = [pd.Timestamp(d) for d in
            ("2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31")]


def frame(rows: dict, cols) -> pd.DataFrame:
    """A statement frame in yfinance's layout: row labels x period columns."""
    return pd.DataFrame({col: [rows[r][i] for r in rows]
                         for i, col in enumerate(cols)},
                        index=list(rows))


class FakeTicker:
    ticker = "FAKE"

    def __init__(self, income, cash, balance, q_income=None):
        self.income_stmt = income
        self.cash_flow = cash
        self.balance_sheet = balance
        self.quarterly_income_stmt = q_income if q_income is not None else income
        self.quarterly_cash_flow = cash
        self.quarterly_balance_sheet = balance


def industrial(cols=PERIODS):
    n = len(cols)
    return FakeTicker(
        income=frame({"Total Revenue": [100e9] * n,
                      "Operating Income": [25e9] * n,
                      "Pretax Income": [24e9] * n,
                      "Net Income": [20e9] * n}, cols),
        cash=frame({"Free Cash Flow": [18e9] * n}, cols),
        balance=frame({"Total Debt": [50e9] * n,
                       "Stockholders Equity": [200e9] * n}, cols))


def financial_sector(cols=PERIODS):
    """A bank/insurer: no Operating Income row at all, negative FCF."""
    n = len(cols)
    return FakeTicker(
        income=frame({"Total Revenue": [80e9] * n,
                      "Pretax Income": [32e9] * n,
                      "Net Income": [25e9] * n}, cols),
        cash=frame({"Free Cash Flow": [-40e9] * n}, cols),
        balance=frame({"Total Debt": [300e9] * n,
                       "Stockholders Equity": [220e9] * n}, cols))


# --------------------------------------------------------------------------
c.section("the financials collector")

fin = research_collect._financials(industrial(), years=4, quarters=4)
c.ok("annual returns the requested number of periods", len(fin["annual"]) == 4,
     f"{len(fin['annual'])}")
c.ok("periods run oldest -> newest",
     [r["end"] for r in fin["annual"]] == sorted(r["end"] for r in fin["annual"]))
c.ok("every metric is populated when every row exists",
     all(r[k] is not None for r in fin["annual"]
         for k in ("revenue", "earnings", "margin_pct", "fcf", "debt_to_equity")))
c.ok("operating margin is used when Operating Income is reported",
     {r["margin_kind"] for r in fin["annual"]} == {"operating"})
c.close("operating margin is Operating Income / Revenue",
        fin["annual"][-1]["margin_pct"], 25.0, tol=1e-6)
c.close("debt/equity is Total Debt / Equity as a percent",
        fin["annual"][-1]["debt_to_equity"], 25.0, tol=1e-6)

bank = research_collect._financials(financial_sector(), years=4, quarters=4)
c.ok("margin falls back to pretax when Operating Income is absent",
     {r["margin_kind"] for r in bank["annual"]} == {"pretax"})
c.close("the fallback is Pretax Income / Revenue",
        bank["annual"][-1]["margin_pct"], 40.0, tol=1e-6)
c.ok("a negative free cash flow is kept, not dropped",
     all(r["fcf"] < 0 for r in bank["annual"]),
     "a bank's negative FCF is genuine, not an error")

# Yahoo can publish a newest column with no revenue yet (seen on JPM's
# quarterlies). Filtering has to happen before the slice or that empty period
# costs a real one.
cols = PERIODS + [pd.Timestamp("2026-12-31")]
partial = industrial(cols)
partial.income_stmt.loc["Total Revenue", cols[-1]] = float("nan")
trimmed = research_collect._financials(partial, years=4, quarters=4)
c.ok("an empty trailing period does not consume a slot",
     len(trimmed["annual"]) == 4
     and all(r["revenue"] is not None for r in trimmed["annual"]),
     f"{[r['period'] for r in trimmed['annual']]}")

empty = research_collect._financials(
    FakeTicker(pd.DataFrame(), pd.DataFrame(), pd.DataFrame()))
c.ok("no statements at all yields empty lists, not an exception",
     empty == {"annual": [], "quarterly": []})

# --------------------------------------------------------------------------
c.section("share-count change is restated across stock splits")

# `get_shares_full` reports raw counts, so a 2-for-1 reads as 100% dilution --
# measured on Fastenal's 2025 split, which vetoed a company that was actually
# buying stock back. Splits cluster in exactly the names a breakout screener
# surfaces, so this has to survive.
SPLIT_DAY = pd.Timestamp("2025-06-01")


class SplitTicker:
    def __init__(self, series, splits):
        self._series, self.splits = series, splits

    def get_shares_full(self, start=None):
        return self._series


shares = pd.Series([100.0, 100.0, 200.0, 199.0],
                   index=pd.to_datetime(["2024-06-01", "2025-05-01",
                                         "2025-06-02", "2026-01-01"]))
split = pd.Series([2.0], index=pd.to_datetime([SPLIT_DAY]))
adjusted = research_collect._ownership(SplitTicker(shares, split), {})
c.close("a 2-for-1 split is divided out, not read as 100% dilution",
        adjusted["shares_change_2y_pct"], -0.5, tol=1e-6)

no_split = research_collect._ownership(
    SplitTicker(shares, pd.Series(dtype=float)), {})
c.close("with no split the raw comparison is unchanged",
        no_split["shares_change_2y_pct"], 99.0, tol=1e-6)

c.close("the split factor compounds over several splits",
        research_collect._split_factor(
            SplitTicker(shares, pd.Series(
                [2.0, 3.0], index=pd.to_datetime(["2025-06-01", "2025-09-01"]))),
            pd.Timestamp("2024-01-01")), 6.0, tol=1e-9)
c.close("splits before the window are ignored",
        research_collect._split_factor(SplitTicker(shares, split),
                                       pd.Timestamp("2025-07-01")), 1.0, tol=1e-9)
c.ok("a ticker with no split history is tolerated",
     research_collect._split_factor(SplitTicker(shares, None),
                                    pd.Timestamp("2024-01-01")) == 1.0)

# --------------------------------------------------------------------------
c.section("tier 2 is untouched by the pretax fallback")

stmt_keys = {"operating_margin", "profit_margin", "fcf", "roe", "roic"}
tier2_bank = quality._statement_metrics(
    derived.statement_frames(financial_sector()), stmt_keys, 4, {})
c.ok("_statement_metrics still yields no operating margin for a bank",
     not tier2_bank.get("operating_margin"),
     "the quality gates must keep their strict Operating-Income definition")
tier2_ind = quality._statement_metrics(
    derived.statement_frames(industrial()), stmt_keys, 4, {})
c.ok("_statement_metrics still computes it where the row exists",
     bool(tier2_ind.get("operating_margin")),
     f"{tier2_ind.get('operating_margin')}")

# --------------------------------------------------------------------------
c.section("the chart renders for every shape of input")

tmp = Path(tempfile.mkdtemp(prefix="test_charts_"))

# Redirect the step log before anything under test can emit one. The logger
# falls back to the real config when no cfg is passed (sec.py and
# research_collect.py log that way), so without this a test would write into
# the real output/logs -- which is exactly what output_fingerprint() forbids.
logs = tmp / "logs"
scanner_common.configure_logging(
    {"research": {"logging": {"enabled": True, "dir": str(logs),
                              "manifest": "runs.csv", "keep_runs": 50}}},
    rid="testrun")


def renders(name, payload) -> bool:
    out = tmp / f"{name}.png"
    try:
        charts.plot_financials(payload, name, out, dpi=60)
    except Exception as exc:  # noqa: BLE001 - that is exactly what we're testing
        print(f"    raised: {exc}")
        return False
    return out.exists() and out.stat().st_size > 0


c.ok("a complete series renders", renders("complete", fin))
c.ok("a bank (pretax margin, negative FCF) renders", renders("bank", bank))
c.ok("a single period renders",
     renders("single", {"annual": fin["annual"][-1:], "quarterly": []}))
c.ok("an all-missing series renders the n/a path rather than raising",
     renders("missing", {"annual": [{"period": "FY25", "revenue": None,
                                     "earnings": None, "margin_pct": None,
                                     "margin_kind": None, "fcf": None,
                                     "debt_to_equity": None}],
                         "quarterly": []}))
c.ok("no data at all still renders", renders("nothing", {}))

c.ok("the margin panel names its basis",
     charts._margin_label(bank["annual"]).startswith("Pretax")
     and charts._margin_label(fin["annual"]) == "Operating margin")
c.ok("no data implies no basis",
     charts._margin_label([]) == "Margin",
     "'mixed basis' would imply a computation that never happened")

# --------------------------------------------------------------------------
c.section("the markdown table")

table = research_report.financials_table_md(fin)
c.ok("one row per charted metric",
     all(f"| {label} |" in table for _, label, _ in charts.FINANCIAL_ROWS))
c.ok("one column per period",
     table.splitlines()[0].count("|") == len(fin["annual"]) + len(fin["quarterly"]) + 2)
c.ok("an operating-margin issuer gets no pretax footnote",
     "pretax income / revenue" not in table.lower())
c.ok("a pretax issuer is told so in a footnote",
     "pretax income / revenue" in research_report.financials_table_md(bank).lower())
missing_table = research_report.financials_table_md(
    {"annual": [{"period": "FY25", "revenue": None, "earnings": None,
                 "margin_pct": None, "margin_kind": None, "fcf": None,
                 "debt_to_equity": None}], "quarterly": []})
c.ok("a missing metric renders n/a", missing_table.count("n/a") == 5)
c.ok("no history at all is stated, not blank",
     "No financial history" in research_report.financials_table_md({}))

# --------------------------------------------------------------------------
c.section("Discord verdict cards")

cfg = json.loads(json.dumps(scanner_common.load_config()))
reports = tmp / "reports"
reports.mkdir()
cfg["research"]["report_subdir"] = str(reports)

chart_png = reports / "AAA_2026-01-01_financials.png"
charts.plot_financials(fin, "AAA", chart_png, dpi=60)
(reports / "AAA_2026-01-01_facts.json").write_text(json.dumps({
    "ticker": "AAA", "company": "Alpha Corp", "price": 100.0,
    "upside_pct": 12.5, "trailing_pe": 20.0, "pe_percentile_2y": 30.0,
    "next_earnings_date": "2026-02-01", "days_to_earnings": 31,
    "quant_score": 62.0, "screen": "Breakout", "setup": "full",
    "quality": False, "quality_missing": ["trailingPE", "debtToEquity"],
    "chart": str(chart_png),
}), encoding="utf-8")

verdict = {"ticker": "AAA", "scan_date": "2026-01-01", "tier": "WATCH",
           "conviction": 59, "narrative_adj": -3, "thesis": "A thesis."}
embeds, images = research_report.build_verdict_embeds([verdict], cfg)

c.ok("one card per verdict", len(embeds) == 1)
c.ok("the card carries six decision fields", len(embeds[0]["fields"]) == 6,
     f"{[f['name'] for f in embeds[0]['fields']]}")
c.ok("the chart is bound as an attachment and its file is passed",
     embeds[0].get("image", {}).get("url") == f"attachment://{chart_png.name}"
     and images == [chart_png],
     "an image referenced without a matching path uploads nothing")

blob = json.dumps(embeds[0])
c.ok("figures come from the facts file, not the caller",
     "62.0" in blob and "12" in blob and "20.0" in blob)
c.ok("the tier-2 result is shown, with the failing rules named",
     "trailingPE" in blob and "debtToEquity" in blob)
c.ok("the narrative adjustment is shown next to the quant score",
     "-3" in blob)
c.ok("the tier drives the side-bar colour",
     embeds[0]["color"] == research_report._tier_color("WATCH", cfg)
     and research_report._tier_color("STRONG", cfg)
     != research_report._tier_color("PASS", cfg))

# The budget is the reason cards can scale to a full night's cap.
many = [dict(verdict, conviction=50 + i) for i in range(5)]
big, _ = research_report.build_verdict_embeds(many, cfg)
total = sum(scanner_common._embed_size(e) for e in big)
c.ok("a full night of cards fits one Discord message",
     total < scanner_common.DISCORD_EMBED_CHAR_BUDGET,
     f"{total} chars for {len(big)} cards vs budget "
     f"{scanner_common.DISCORD_EMBED_CHAR_BUDGET}")

c.ok("a ticker with no facts file still produces a card",
     len(research_report.build_verdict_embeds(
         [{"ticker": "ZZZ", "scan_date": "2026-01-01", "tier": "PASS",
           "conviction": 10, "thesis": "t"}], cfg)[0]) == 1,
     "an ad-hoc verdict must not need a prior context run")

c.ok("percentiles read as ordinals",
     [research_report._ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 54)]
     == ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "54th"])

# --------------------------------------------------------------------------
# Retuning the rules has to take effect immediately. A gate answering with the
# verdict the scan recorded would ignore a threshold change until the next
# scan, which is exactly the confusion of loosening a rule and watching
# `candidates` still report the old failures.
c.section("tier 2 is re-graded against the rules in force now")

pe_label = quality.label_of("trailingPE",
                            cfg["quality"]["parameters"]["trailingPE"])


def only_pe_gate(max_pe: float) -> dict:
    """A config whose sole gate is trailingPE, so the assertion is unambiguous.

    Built by *disabling* the other parameters rather than deleting them -- that
    is the switch this whole section exists to exercise, and it also proves a
    disabled parameter drops out of `Quality Missing`.
    """
    variant = json.loads(json.dumps(cfg))
    for key, spec in variant["quality"]["parameters"].items():
        spec["enabled"] = key == "trailingPE"
    variant["quality"]["parameters"]["trailingPE"]["gate"] = {"max": max_pe}
    return variant


strict, loose = only_pe_gate(10), only_pe_gate(100)

# The row carries a *stale* passing verdict recorded under some older bar.
stale = {"screens": [{"config_key": "s", "title": "S", "strategy": {},
                      "hits": {"AAA": {"Setup": "full", "Missing": "",
                                       pe_label: 50.0,
                                       scanner_common.QUALITY_COL: True,
                                       scanner_common.QUALITY_MISSING_COL: []}}}]}
under_strict = research_report.list_candidates(stale, strict, gate="all")[0]
under_loose = research_report.list_candidates(stale, loose, gate="all")[0]
c.ok("a tightened rule overrides a recorded pass",
     under_strict["quality"] is False
     and under_strict["quality_missing"] == ["trailingPE"],
     f"{under_strict['quality']} {under_strict['quality_missing']}")
c.ok("a loosened rule still passes", under_loose["quality"] is True)
c.ok("the gate follows the fresh verdict, not the recorded one",
     research_report.list_candidates(stale, strict, gate="quality_pass") == []
     and len(research_report.list_candidates(stale, loose, gate="quality_pass")) == 1)

# A scan that never evaluated quality has nothing to re-grade -- saying
# "failed everything" there would be a lie about missing data.
blank = {"screens": [{"config_key": "s", "title": "S", "strategy": {},
                      "hits": {"AAA": {"Setup": "full", "Missing": ""}}}]}
c.ok("a row with no fundamentals reports 'not evaluated', not all-failed",
     research_report.list_candidates(blank, strict, gate="all")[0]["quality"] is None)
c.ok("the card and the gate grade identically",
     research_report._quality_now({"row": stale["screens"][0]["hits"]["AAA"]}, strict)
     == (under_strict["quality"], under_strict["quality_missing"]),
     "_facts and list_candidates must not diverge")

# The 2026-07-26 production shakedown caught the bundle contradicting itself:
# `trigger` carried the recorded verdict while the card and gate carried the
# re-graded one. Both are worth reporting -- but under distinct keys.
trig = research_report.find_ticker(stale, "AAA", strict)
c.ok("the trigger block agrees with the card",
     (trig["quality"], trig["quality_missing"])
     == (under_strict["quality"], under_strict["quality_missing"]),
     f"trigger={trig['quality']} card={under_strict['quality']}")
c.ok("the scan's own verdict is still available separately",
     trig["quality_recorded"] is True and trig["quality"] is False,
     "a report can state that the thresholds moved between scan and write-up")
c.ok("without cfg, find_ticker reports what the scan recorded",
     research_report.find_ticker(stale, "AAA")["quality"] is True)

# --------------------------------------------------------------------------
# Asking about a ticker by name is mostly a tier-2 question, so a scan that
# fires nothing still has to produce a graded row -- an ad-hoc deep dive used
# to arrive with an empty trigger and quality "not evaluated".
c.section("on-demand scan: tiers 1 and 2 for a named ticker")

TICK = "AAA"
history = tmp / "history"
od_cfg = json.loads(json.dumps(cfg))
od_cfg["research"]["history"] = {"enabled": True, "dir": str(history),
                                 "csv": "signals.csv",
                                 "on_demand_csv": "on_demand.csv"}
od_cfg["research"]["logging"] = {"enabled": True, "dir": str(logs),
                                 "manifest": "runs.csv", "keep_runs": 50}
# One gate the stub satisfies, so the verdict is deterministic whatever the
# real registry has been retuned to since. Every other parameter is switched
# off rather than deleted -- the same flag a user flips to stop a rule mattering.
od_cfg["quality"]["enabled"] = True
od_cfg["quality"]["badge"] = "*"
for _key, _spec in od_cfg["quality"]["parameters"].items():
    _spec["enabled"] = _key == "trailingPE"
od_cfg["quality"]["parameters"]["trailingPE"]["gate"] = {"max": 20}

# 800 sessions of a gentle, uniformly red decline. No screen can fire on it at
# any thresholds -- nothing consolidates, no SMA rises, no close crosses up
# through one -- which is precisely the case tier 2 still has to answer.
_n = 800
_idx = pd.bdate_range("2023-01-02", periods=_n)
_close = pd.Series([200.0 * 0.9985 ** i for i in range(_n)], index=_idx)
falling = pd.DataFrame({("Open", TICK): _close / 0.999,
                        ("High", TICK): _close / 0.997,
                        ("Low", TICK): _close * 0.997,
                        ("Close", TICK): _close,
                        ("Adj Close", TICK): _close,
                        ("Volume", TICK): 1_000_000.0})
falling.columns = pd.MultiIndex.from_tuples(falling.columns)


def stub_fundamentals(tickers, _cfg, closes=None, benchmark=None):
    return pd.DataFrame({pe_label: [12.0], scanner_common.COMPANY_COL: ["Alpha Corp"]},
                        index=list(tickers)).rename_axis("Ticker")


run_scanners.download_price_data = lambda t, period, interval: falling
run_scanners.quality.fetch_fast = stub_fundamentals

_out = io.StringIO()
_stdout, sys.stdout = sys.stdout, _out
try:
    payload = run_scanners.scan_ticker(TICK, od_cfg)
finally:
    sys.stdout = _stdout

scan_date = payload["scan_date"]
c.ok("the on-demand scan dates itself off its own price data",
     scan_date == str(_idx[-1].date()), f"{scan_date} vs {_idx[-1].date()}")

# The whole reuse rests on this: the payload is shaped like latest_hits.json,
# so find_ticker, the tier-2 re-grade and _facts consume it unchanged.
trigger = research_report.find_ticker(payload, TICK, od_cfg)
c.ok("find_ticker consumes an on-demand payload unchanged", trigger is not None)
c.ok("no signal is reported as a setup, not as a missing trigger",
     trigger["kind"] == "none"
     and trigger["screen"] == run_scanners.NO_SIGNAL_TITLE,
     f"{trigger['screen']}: {trigger['kind']}")
c.ok("tier 2 is graded even though no screen fired",
     trigger["quality"] is True and trigger["quality_missing"] == [],
     f"{trigger['quality']} {trigger['quality_missing']}")
c.ok("the gate reads it like any other row",
     len(research_report.list_candidates(payload, od_cfg, gate="quality_pass")) == 1)

# --------------------------------------------------------------------------
# Two tables, split by provenance: a nightly signal annotates the row it
# already has, an ad-hoc look has none and would otherwise be dropped.
c.section("the verdict record: one table per provenance")

od_csv = history / "on_demand.csv"
sig_csv = history / "signals.csv"


def _quiet(fn, *args):
    buf, keep = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        return fn(*args)
    finally:
        sys.stdout = keep


_quiet(research_report.record_on_demand, payload, TICK, od_cfg)
recorded = pd.read_csv(od_csv, dtype={"scan_date": str})
c.ok("the scan is recorded before any verdict exists", len(recorded) == 1)
c.ok("the record carries the ratios tier 2 graded",
     pe_label in recorded.columns
     and scanner_common.QUALITY_COL in recorded.columns
     and recorded.loc[0, "Setup"] == "none",
     f"{[col for col in recorded.columns][:8]}")

_quiet(research_report.record_on_demand, payload, TICK, od_cfg)
c.ok("re-scanning the same day updates rather than duplicates",
     len(pd.read_csv(od_csv)) == 1)


def _facts_file(ticker, source, **extra):
    (reports / f"{ticker}_{scan_date}_facts.json").write_text(
        json.dumps({"ticker": ticker, "scan_date": scan_date, "source": source,
                    "quant_score": 62.0, "price": 100.0, **extra}),
        encoding="utf-8")


_facts_file(TICK, research_report.SOURCE_ON_DEMAND)
od_verdict = {"ticker": TICK, "scan_date": scan_date, "tier": "WATCH",
              "conviction": 59, "narrative_adj": -3, "thesis": "A thesis."}
c.ok("an on-demand verdict lands in the on-demand table",
     _quiet(research_report.record_verdict, od_verdict, od_cfg) == od_csv.name)

with_verdict = pd.read_csv(od_csv, dtype={"scan_date": str})
c.ok("the verdict updates the scan's row rather than adding one",
     len(with_verdict) == 1
     and with_verdict.loc[0, scanner_common.VERDICT_COL] == "WATCH"
     and with_verdict.loc[0, scanner_common.CONVICTION_COL] == 59)
c.ok("an on-demand row keeps the figures that make it readable later",
     {"Narrative Adj", "Quant Score", "Report", "Thesis"} <= set(with_verdict.columns))

# The same clobber the signal archive has: re-scanning rewrites the row.
_quiet(research_report.record_on_demand, payload, TICK, od_cfg)
c.ok("re-scanning after a verdict preserves it",
     pd.read_csv(od_csv).loc[0, scanner_common.VERDICT_COL] == "WATCH")

# A signal ticker routes the other way -- and never to both tables.
SIG = "BBB"
scanner_common.merge_history_csv(
    sig_csv, [{"scan_date": scan_date, "config_key": "breakout_strategy",
               "ticker": SIG, "Setup": "full"}],
    scanner_common.HISTORY_KEYS)
_facts_file(SIG, research_report.SOURCE_SIGNAL)
sig_verdict = {"ticker": SIG, "scan_date": scan_date, "tier": "PASS",
               "conviction": 20, "narrative_adj": 0, "thesis": "Nope."}
c.ok("a signal verdict lands on its signals.csv row",
     _quiet(research_report.record_verdict, sig_verdict, od_cfg) == sig_csv.name)
signals = pd.read_csv(sig_csv, dtype={"scan_date": str})
on_demand = pd.read_csv(od_csv, dtype={"scan_date": str})
c.ok("signals.csv gained exactly the two columns asked for",
     signals.loc[0, scanner_common.VERDICT_COL] == "PASS"
     and signals.loc[0, scanner_common.CONVICTION_COL] == 20
     and "Thesis" not in signals.columns)
c.ok("a (scan_date, ticker) is recorded in exactly one table",
     SIG not in set(on_demand["ticker"]) and TICK not in set(signals["ticker"]),
     f"on-demand={sorted(on_demand['ticker'])} signals={sorted(signals['ticker'])}")

# Scanning a ticker the nightly run also caught must not create an orphan row
# in the other table -- its verdict routes to the signal row it already has.
sig_payload = json.loads(json.dumps(payload))
sig_payload["screens"][0]["hits"] = {SIG: payload["screens"][0]["hits"][TICK]}
c.ok("scanning a ticker that already has a signal row records nothing new",
     _quiet(research_report.record_on_demand, sig_payload, SIG, od_cfg) is None
     and SIG not in set(pd.read_csv(od_csv)["ticker"]))

# A verdict for a ticker never scanned still has to survive somewhere.
_facts_file("CCC", research_report.SOURCE_ON_DEMAND)
_quiet(research_report.record_verdict,
       {"ticker": "CCC", "scan_date": scan_date, "tier": "PASS",
        "conviction": 10, "thesis": "x"}, od_cfg)
c.ok("a verdict with no scan row is still recorded, not dropped",
     "CCC" in set(pd.read_csv(od_csv, dtype={"scan_date": str})["ticker"]))

# The tier the model set and the bands in force can part company, because the
# bands get retuned between the run and the record.
bands = od_cfg["research"]["synthesis"]["tiers"]
top = max(band["min"] for band in bands)
drifted = {"ticker": TICK, "scan_date": scan_date, "tier": "STRONG",
           "conviction": max(top - 10, 0), "thesis": "x"}
buf, _stderr = io.StringIO(), sys.stderr
sys.stderr = buf
try:
    _quiet(research_report.record_verdict, drifted, od_cfg)
finally:
    sys.stderr = _stderr
c.ok("a tier that disagrees with the config bands is flagged",
     "WARNING" in buf.getvalue()
     and research_report.tier_for(drifted["conviction"], od_cfg) in buf.getvalue(),
     buf.getvalue().strip()[:90] or "no warning emitted")
c.ok("the flagged verdict is still recorded as the model set it",
     pd.read_csv(od_csv, dtype={"scan_date": str}).set_index("ticker")
     .loc[TICK, scanner_common.VERDICT_COL] == "STRONG",
     "a drift warning informs; it never rewrites the judgment")

# --------------------------------------------------------------------------
# The model contributes exactly one number to tier 3, and until now nothing in
# Python held it to its documented bound -- `narrative_adj_max` was enforced by
# the skill file asking nicely. Everything else about the verdict is
# deterministic precisely so a model cannot originate one.
limit = od_cfg["research"]["synthesis"]["narrative_adj_max"]
_facts_file("DDD", research_report.SOURCE_ON_DEMAND)
runaway = {"ticker": "DDD", "scan_date": scan_date, "tier": "STRONG",
           "conviction": 99, "narrative_adj": limit + 40, "thesis": "x"}
buf, _stderr = io.StringIO(), sys.stderr
sys.stderr = buf
try:
    _quiet(research_report.record_verdict, runaway, od_cfg)
finally:
    sys.stderr = _stderr
c.ok("an out-of-range narrative adjustment is clamped, not honoured",
     runaway["narrative_adj"] == limit, f"-> {runaway['narrative_adj']}")
c.ok("...the conviction is rebuilt from the recorded quant score",
     runaway["conviction"] == round(62.0 + limit),
     f"{runaway['conviction']} vs {round(62.0 + limit)}")
c.ok("...the tier follows the rebuilt conviction",
     runaway["tier"] == research_report.tier_for(runaway["conviction"], od_cfg))
c.ok("...and the clamp is visible, never silent", "WARNING" in buf.getvalue(),
     buf.getvalue().strip()[:90] or "no warning emitted")

adj_ok = {"ticker": "DDD", "scan_date": scan_date, "tier": "PASS",
          "conviction": 40, "narrative_adj": limit, "thesis": "x"}
_quiet(research_report.record_verdict, adj_ok, od_cfg)
c.ok("an adjustment exactly at the bound is left alone",
     adj_ok["narrative_adj"] == limit and adj_ok["conviction"] == 40,
     "the bound is inclusive; clamping it would move a legal verdict")

# A veto is a deterministic rule over collected values, so whether it overrides
# the tier is a config decision (`quality.veto_enforced`) -- but in neither mode
# may the narrative pass talk the pipeline out of the exclusion *record*. Both
# modes are pinned, because each one silently breaks a different thing: enforcing
# always makes excluded names incomparable for tier 4, and enforcing never would
# let a "STRONG" label stand on a company the rules disqualified.
_facts_file("EEE", research_report.SOURCE_ON_DEMAND, veto=True,
            veto_reasons=["altman_z"], veto_text="Altman Z")

enforced = json.loads(json.dumps(od_cfg))
enforced["quality"]["veto_enforced"] = True
c.ok("veto_enforced is off by default",
     not quality.veto_enforced(od_cfg),
     "a gate would delete the evidence the exclusion thesis needs")

vetoed = {"ticker": "EEE", "scan_date": scan_date, "tier": "STRONG",
          "conviction": 88, "narrative_adj": 0, "thesis": "x"}
_quiet(research_report.record_verdict, vetoed, enforced)
c.ok("enforced: a vetoed ticker keeps the veto tier whatever it claimed",
     vetoed["tier"] == quality.veto_tier(enforced),
     f"reported STRONG -> recorded {vetoed['tier']}")
c.ok("...but the conviction it scored is left on the record",
     vetoed["conviction"] == 88,
     "tier 4 needs the number to measure what the exclusion cost")

labelled = {"ticker": "EEE", "scan_date": scan_date, "tier": "STRONG",
            "conviction": 88, "narrative_adj": 0, "thesis": "x"}
_quiet(research_report.record_verdict, labelled, od_cfg)
c.ok("as a label: the tier stands, so the row stays comparable",
     labelled["tier"] == "STRONG" and labelled["conviction"] == 88,
     f"recorded {labelled['tier']}")

off = json.loads(json.dumps(od_cfg))
off["research"]["history"]["enabled"] = False
c.ok("history.enabled false disables both tables",
     _quiet(research_report.record_verdict, od_verdict, off) is None
     and _quiet(research_report.record_on_demand, payload, TICK, off) is None)

# --------------------------------------------------------------------------
# A deep-dive spans three processes and used to leave three lines in
# deepdive_log.txt. What is pinned here is the shape of the record, never its
# content: which phases appear, that a failure is logged *and* re-raised, and
# that the model's half merges into the same timeline.
c.section("the step log")

log_cfg = json.loads(json.dumps(od_cfg))
log_dir = tmp / "steplog"
log_cfg["research"]["logging"] = {"enabled": True, "dir": str(log_dir),
                                  "manifest": "runs.csv", "keep_runs": 50}
log_file = log_dir / "runA.log"
scanner_common.configure_logging(rid="runA")


def log_lines() -> list[str]:
    return (log_file.read_text(encoding="utf-8").splitlines()
            if log_file.exists() else [])


def phases() -> list[str]:
    return [ln.split(None, 3)[2] for ln in log_lines() if len(ln.split()) > 2]


_err = io.StringIO()
_stderr, sys.stderr = sys.stderr, _err
try:
    scanner_common.log_step("START", "ok", "AAA on-demand", cfg=log_cfg)
    with scanner_common.step("YAHOO", cfg=log_cfg) as s:
        s.detail = "9/9 groups"
    raised = False
    try:
        with scanner_common.step("SEC", "10-K", cfg=log_cfg):
            raise RuntimeError("boom")
    except RuntimeError:
        raised = True
finally:
    sys.stderr = _stderr

c.ok("a step is one line: timestamp, phase, status, description",
     len(log_lines()) == 3 and phases() == ["START", "YAHOO", "SEC"],
     " | ".join(log_lines()))
c.ok("the timestamp carries the date, so a run crossing midnight still sorts",
     all(scanner_common.datetime.strptime(ln[:19], scanner_common.TS_FMT)
         for ln in log_lines()))
c.ok("a failed step is recorded and the exception still propagates",
     raised and log_lines()[2].split()[3] == "failed"
     and "boom" in log_lines()[2],
     log_lines()[2])
c.ok("the log echoes to stderr, never stdout",
     _err.getvalue().count("\n") == 3,
     "context prints its JSON bundle to stdout -- a log line there corrupts it")

# `context` assembles its bundle by calling straight through tiers 1 and 2,
# whose progress lines go to stdout because for run_scanners.py stdout IS the
# log. Reached through `context` they land in front of the JSON the skill
# parses -- and the most valuable of them, the unsettled-bar WARNING, is
# precisely the one that breaks it. Pinned with a callee that prints the way
# the real ones do.
_out, _keep = io.StringIO(), sys.stdout
sys.stdout = _out
try:
    with scanner_common.stdout_to_stderr() as real_stdout:
        print("Downloading 2y of 1d data for 1 tickers...")
        print("WARNING: 2026-07-24 has no settled close -- dropping it.")
        scanner_common.log_step(          # the step log writes here too
            "SCAN", "ok", "noise",
            cfg={"research": {"logging": {"enabled": False}}})
    print(json.dumps({"ticker": "AAA"}), file=real_stdout)
finally:
    sys.stdout = _keep

c.ok("a callee printing to stdout cannot corrupt the bundle",
     json.loads(_out.getvalue())["ticker"] == "AAA",
     repr(_out.getvalue()[:60]))
c.ok("and stdout is restored afterwards", sys.stdout is _keep)

# The whole point of a logger that cannot take down a deep-dive.
c.ok("an unwritable log destination is survived, not raised",
     scanner_common.log_step(
         "X", "ok", "d", cfg={"research": {"logging": {"dir": str(log_file)}}},
         echo=False) is None)

off_log = json.loads(json.dumps(log_cfg))
off_log["research"]["logging"]["enabled"] = False
before = len(log_lines())
scanner_common.log_step("NOPE", "ok", "silent", cfg=off_log, echo=False)
c.ok("logging.enabled false writes nothing", len(log_lines()) == before)

# --------------------------------------------------------------------------
# The model's half. A transcript in Claude Code's own format, hand-built so no
# real session is needed: one Bash call, one WebFetch, one refused tool.
# `transcript_path` globs ~/.claude/projects/*/<session>.jsonl, so pointing
# Path.home at tmp is the whole redirection -- no real session is touched.
transcript_dir = tmp / ".claude" / "projects" / "proj"
transcript_dir.mkdir(parents=True)
session = "11111111-2222-3333-4444-555555555555"


def _msg(role, blocks, when, **extra):
    return json.dumps({"type": role, "timestamp": when,
                       "message": {"content": blocks}, **extra})


(transcript_dir / f"{session}.jsonl").write_text("\n".join([
    _msg("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash",
                        "input": {"command": 'cd "C:\\proj" && python research_report.py context AAA'}}],
         "2026-01-02T03:04:05.000Z"),
    _msg("user", [{"type": "tool_result", "tool_use_id": "t1"}],
         "2026-01-02T03:04:07.000Z", toolUseResult={"stdout": "ok"}),
    _msg("assistant", [{"type": "tool_use", "id": "t2", "name": "WebFetch",
                        "input": {"url": "https://www.example.com/news"}}],
         "2026-01-02T03:05:00.000Z"),
    _msg("user", [{"type": "tool_result", "tool_use_id": "t2"}],
         "2026-01-02T03:05:04.000Z", toolUseResult={"code": 200, "bytes": 2048}),
    _msg("assistant", [{"type": "tool_use", "id": "t3", "name": "Bash",
                        "input": {"command": "python -c 'print(1)'"}}],
         "2026-01-02T03:06:00.000Z"),
    _msg("user", [{"type": "tool_result", "tool_use_id": "t3", "is_error": True}],
         "2026-01-02T03:06:01.000Z", toolUseResult="Error",
         toolDenialKind="permission-rule"),
    # Plumbing the model needs but nobody wants in a log.
    _msg("assistant", [{"type": "tool_use", "id": "t4", "name": "ToolSearch",
                        "input": {"query": "select:WebFetch"}}],
         "2026-01-02T03:06:30.000Z"),
]), encoding="utf-8")

_home = Path.home
Path.home = staticmethod(lambda: tmp)
try:
    rendered, tally = research_report.render_session(session)
finally:
    Path.home = _home

c.ok("one line per tool call, plumbing left out",
     len(rendered) == 3 and tally["steps"] == 3,
     " | ".join(rendered))
c.ok("a refused tool is flagged, not silently absent",
     any(" DENIED " in ln and "permission-rule" in ln for ln in rendered)
     and tally["denials"] == 1,
     "the MU run exited 0 with a denial in it and nothing surfaced it")
c.ok("a fetch records its status and size",
     any("FETCH" in ln and "200" in ln and "2.0 KB" in ln for ln in rendered),
     " | ".join(ln for ln in rendered if "FETCH" in ln))
c.ok("the noisy cd prefix is stripped from a command",
     any("BASH" in ln and ln.rstrip().endswith("(2.0s)")
         and "research_report.py context AAA" in ln and "cd " not in ln
         for ln in rendered),
     " | ".join(ln for ln in rendered if "BASH" in ln))
c.ok("an unknown session degrades to no lines rather than raising",
     research_report.render_session("no-such-session") == ([], {
         "steps": 0, "searches": 0, "fetches": 0, "denials": 0, "errors": 0}))

# Transcript stamps are UTC and the Python half is local wall-clock; without
# the conversion every model step sorts hours away from the steps it belongs
# between, which would make the merged timeline actively misleading.
utc_hour = int(rendered[0][11:13])
c.ok("transcript timestamps are converted to local time before merging",
     utc_hour == (scanner_common.datetime(2026, 1, 2, 3, 4, 5,
                                          tzinfo=scanner_common.timezone.utc)
                  .astimezone().hour),
     rendered[0][:19])

# --------------------------------------------------------------------------
Path.home = staticmethod(lambda: tmp)
try:
    _quiet(research_report.complete_run_log, "runA", session, log_cfg, "on_demand")
    merged = log_lines()
    # END is appended after the sort and stays last even when it ties with the
    # final step's second, so the ordering claim is about the steps.
    steps = merged[:-1]
    c.ok("the model's steps merge into the Python half, in time order",
         steps == sorted(steps) and len(merged) == 3 + 3 + 1,
         f"{len(merged)} lines: {[ln.split()[2] for ln in merged]}")
    c.ok("the run is closed with exactly one END line",
         sum(" END " in ln for ln in merged) == 1 and " END " in merged[-1],
         merged[-1])
    c.ok("a missing result file is reported as killed, not crashed",
         "killed" in merged[-1],
         "a run cut short by a shutdown still gets its log completed")

    runs = pd.read_csv(log_dir / "runs.csv")
    c.ok("the run lands one row in the manifest",
         len(runs) == 1 and runs.loc[0, "run_id"] == "runA"
         and runs.loc[0, "denials"] == 1 and runs.loc[0, "web_fetches"] == 1,
         f"{runs.iloc[0].to_dict()}")

    _quiet(research_report.complete_run_log, "runA", session, log_cfg, "on_demand")
    c.ok("re-running log-session updates the row rather than adding one",
         len(pd.read_csv(log_dir / "runs.csv")) == 1)
    c.ok("and does not stack a second END onto the log",
         sum(" END " in ln for ln in log_lines()) == 1)
finally:
    Path.home = _home
    scanner_common.configure_logging(rid="testrun")

# --------------------------------------------------------------------------
# The dry-run print carries the quality badge, and Windows picks the locale
# codepage for a redirected stream -- cp1255 here, which has no mapping for
# U+2B50. That raised UnicodeEncodeError in exactly the `discord_send: false`
# configuration used to shake the nightly run down, so the badge has to
# survive a narrow codepage.
c.section("output encoding survives a non-UTF-8 console")

badge = cfg["fundamentals"]["quality"]["badge"]
fields = research_report._verdict_fields(
    {"quality": True, "quant_score": 50.0}, {"ticker": "AAA"})
rendered = " ".join(f["value"] for f in fields)
c.ok("a passing card renders the badge", badge in rendered, rendered[:60])

buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1255", errors="strict")
try:
    buf.write(rendered)
    narrow_ok = True
except UnicodeEncodeError:
    narrow_ok = False
c.ok("the badge is genuinely unencodable in the console codepage",
     not narrow_ok,
     "if this ever passes the guard below has stopped testing anything")

# The veto badge is the same trap on the exclusion side: it reaches stdout
# through the same cards and the same redirected `.bat` output.
veto_fields = research_report._verdict_fields(
    {"quality": True, "quant_score": 50.0, "veto": True,
     "veto_text": "Altman Z"}, {"ticker": "AAA"})
veto_rendered = " ".join(f["name"] + f["value"] for f in veto_fields)
c.ok("an excluded card names the rules it tripped",
     "Altman Z" in veto_rendered, veto_rendered[:60])
narrow = io.TextIOWrapper(io.BytesIO(), encoding="cp1255", errors="strict")
try:
    narrow.write(veto_rendered)
    veto_narrow_ok = True
except UnicodeEncodeError:
    veto_narrow_ok = False
c.ok("the veto badge is unencodable in that codepage too", not veto_narrow_ok,
     "the exclusion side reaches stdout through the same redirected .bat")

safe = io.TextIOWrapper(io.BytesIO(), encoding="cp1255", errors="strict")
_stdout = sys.stdout
try:
    sys.stdout = safe
    scanner_common.enable_utf8_output()
    sys.stdout.write(rendered)
    guarded_ok = True
except UnicodeEncodeError:
    guarded_ok = False
finally:
    sys.stdout = _stdout
c.ok("enable_utf8_output makes it printable anyway", guarded_ok)

sys.exit(c.finish())
