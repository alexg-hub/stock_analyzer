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
import research_collect
import research_report
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
c.section("tier 2 is untouched by the pretax fallback")

stmt_cfg = {"enabled": True, "years": 4,
            "metrics": {"operating_margin": "OpM", "profit_margin": "PM",
                        "fcf": "FCF", "roe": "ROE", "roic": "ROIC"}}
tier2_bank = scanner_common._statement_metrics(financial_sector(), stmt_cfg, {})
c.ok("_statement_metrics still yields no operating margin for a bank",
     not tier2_bank.get("OpM"),
     "the quality rules must keep their strict Operating-Income definition")
tier2_ind = scanner_common._statement_metrics(industrial(), stmt_cfg, {})
c.ok("_statement_metrics still computes it where the row exists",
     bool(tier2_ind.get("OpM")), f"{tier2_ind.get('OpM')}")

# --------------------------------------------------------------------------
c.section("the chart renders for every shape of input")

tmp = Path(tempfile.mkdtemp(prefix="test_charts_"))


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

pe_label = cfg["fundamentals"]["fields"]["trailingPE"]
strict = json.loads(json.dumps(cfg))
strict["fundamentals"]["quality"]["rules"] = {"trailingPE": {"max": 10}}
loose = json.loads(json.dumps(cfg))
loose["fundamentals"]["quality"]["rules"] = {"trailingPE": {"max": 100}}

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
