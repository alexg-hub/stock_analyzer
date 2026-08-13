"""Offline -- the combined dossier: a rendering, never an analysis.

The two halves of this project are recorded in separate tables precisely so that
neither can move the other. This report puts them on one page, which is exactly
the moment that separation could quietly be undone -- so what is pinned here is
that the page *renders* and never *computes*:

  * **It reads recorded files only.** No network, no grading, nothing
    recomputed. A verdict weeks old must render as the verdict that was made,
    not as what today's fundamentals would score -- the same rule
    `marking._tier3_columns` follows.
  * **A missing half says so.** A ticker nobody researched still gets a page
    marked "not enriched", and a ticker with no scan row says that too. Coverage
    is worth seeing, and both are legitimate states rather than gaps to fill.
  * **Scope decides the filename.** The artifacts are date-stamped, so without
    this a one-ticker run and a whole-week run resolve to the same path and one
    silently replaces the other -- the bug `universe_scan.artifact_paths` exists
    to prevent, reproduced here before it could happen again.
  * **The enrichment cannot leak into the graded half.** The strongest check in
    the file: render the same ticker with and without a maximally bearish
    enrichment and the deterministic block must be byte-identical.

Every path is redirected into a temp tree; nothing here touches the real record.
"""

import copy
import json
import tempfile
from pathlib import Path

import pandas as pd

from _harness import Checks, config

import combined_report
import enrichment
import quality

c = Checks("combined report")

TMP = Path(tempfile.mkdtemp(prefix="combined_test_"))
cfg = copy.deepcopy(config())
cfg["enrichment"] = {"enabled": True, "dir": str(TMP / "enrich"),
                     "csv": str(TMP / "enrich" / "e.csv")}
cfg["combined"] = {"dir": str(TMP / "combined")}
cfg.setdefault("research", {}).setdefault("history", {}).update(
    dir=str(TMP), csv=str(TMP / "signals.csv"),
    on_demand_csv=str(TMP / "on_demand.csv"))
cfg["research"]["report_subdir"] = str(TMP / "reports")
cfg.setdefault("portfolio", {})["dir"] = str(TMP / "portfolio")
cfg["portfolio"]["positions_csv"] = str(TMP / "portfolio" / "positions.csv")

DATE = "2026-03-02"

# A recorded signal row, in the shape `archive_scan` writes.
(TMP / "reports").mkdir(parents=True, exist_ok=True)
pd.DataFrame([{
    "scan_date": DATE, "config_key": "pullback_strategy", "ticker": "AAA",
    "screen": "Pullback to rising 150-day SMA", "Setup": "full",
    "Quality": False, "Quality Missing": json.dumps(["operating_margin"]),
    "Veto": False, "Veto Reasons": json.dumps([]),
    "Reward": 71.2, "Risk": 33.4, "Quadrant": "buy",
    "Verdict": "WATCH", "Conviction": 64.0,
}]).to_csv(TMP / "signals.csv", index=False)

(TMP / "reports" / f"AAA_{DATE}_facts.json").write_text(json.dumps({
    "ticker": "AAA", "scan_date": DATE, "company": "Alpha Corp",
    "quant_score": 64.0, "tier": "WATCH", "conviction": 64.0,
    "quant_dimensions": {
        "growth": {"score": 0.81, "metrics_used": 3, "metrics_total": 3},
        "risk": {"score": 0.29, "metrics_used": 10, "metrics_total": 12},
    },
    # `_facts` stores these already flattened to scalars, even for the
    # parameters whose registry format is a *_series -- that mismatch is what
    # the rendering checks below exist for. `cash_runway_quarters` is
    # deliberately absent: "not evaluated" must not read as "failed".
    "quant_metrics": {
        "trailingPE": 21.5,
        "roe": 18.25,
        "operating_margin": 22.0,
        "fcf": 4200000000.0,
        "altman_z": 4.1,
    },
    "quality_missing": ["operating_margin"],
    "source": "signal",
}), encoding="utf-8")


# --------------------------------------------------------------------------
c.section("what it reads, and what it says when a half is missing")

entry = combined_report.collect_one("AAA", cfg)
c.ok("it finds the recorded scan row", entry["scan_date"] == DATE
     and entry["source"] == "signal")
c.ok("it reads the recorded facts rather than recomputing",
     entry["facts"].get("quant_score") == 64.0,
     "recomputing would score today's fundamentals against an old verdict")
c.ok("an unenriched ticker reports an empty enrichment, not a default",
     entry["enrichment"] == {})

body = combined_report.render([entry], cfg, combined_report.SCOPE_TICKER)
c.ok("the graded half is rendered", "WATCH" in body and "64.0/100" in body)
c.ok("the quality failure names its rule", "operating_margin" in body)
c.ok("both plane coordinates and the quadrant appear",
     "71.2" in body and "33.4" in body and "buy" in body)
c.ok("the group breakdown names strongest and weakest",
     "growth" in body and "risk" in body and "13 of 15" in body,
     "used/total is summed across groups")
c.ok("an unenriched ticker is marked, not omitted",
     "not enriched" in body and "absent rather than neutral" in body,
     "coverage is a finding")
c.ok("the disclaimer survives", "not investment advice" in body)

unknown = combined_report.collect_one("ZZZ", cfg)
c.ok("a ticker with no scan record collects nothing", not unknown["scan_row"])
c.ok("...and renders as 'never screened' rather than blank",
     "never been screened" in combined_report.render(
         [unknown], cfg, combined_report.SCOPE_TICKER))


# --------------------------------------------------------------------------
c.section("the parameter values behind the score")

# A group score of 0.29 is not checkable; the value beside its threshold is.
# Everything here is keyed through `quality.label_of` rather than a literal
# label, so retuning a label in config cannot fail these.
specs = quality.parameters(cfg)
by_label = {r[0]: r for r in combined_report._parameter_rows(entry, cfg)}


def prow(key):
    """The rendered row for one registry key: (label, value, gate, verdict)."""
    return by_label.get(quality.label_of(key, specs[key]))


c.ok("every parameter the score used appears with its value",
     all(prow(k) and prow(k)[1] != "n/a"
         for k in ("trailingPE", "roe", "operating_margin", "fcf")))
c.ok("the threshold it was compared against is printed beside it",
     all(str(v) in prow("roe")[2]
         for v in (specs["roe"].get("gate") or {}).values()),
     "a value without its gate is not checkable")

# The two rendering bugs this table shipped with, pinned so they cannot return.
c.ok("a *_series parameter renders its flattened scalar, not n/a",
     prow("operating_margin")[1] != "n/a",
     "_facts stores a scalar; feeding it to the series formatter yielded n/a "
     "for a value that was present and scored")
c.ok("a plain pct parameter keeps its unit",
     "%" in prow("roe")[1],
     "defaulting the scalar-format lookup to 'number' silently stripped it")

c.ok("a parameter with no recorded value reads 'not evaluated', not a failure",
     prow("cash_runway_quarters")[3] == "not evaluated",
     "missing is not failing -- the distinction the whole registry rests on")

# The load-bearing one: pass/fail is read back from the recorded lists, never
# recomputed. Move the value to either extreme and the verdict must not budge,
# because the record -- not today's arithmetic -- is what the page reports.
moved = copy.deepcopy(entry)
verdicts = set()
for value in (-999.0, 999.0):
    moved["facts"]["quant_metrics"]["operating_margin"] = value
    verdicts.add({r[0]: r for r in combined_report._parameter_rows(moved, cfg)}
                 [quality.label_of("operating_margin", specs["operating_margin"])][3])
c.ok("the verdict comes from the recorded result, not from the value",
     verdicts == {prow("operating_margin")[3]} and len(verdicts) == 1,
     "re-deriving pass/fail here would silently re-grade an old verdict")

c.ok("the table reaches the rendered page", "| Parameter | Value | Gate |" in body)

# A deep-stage veto is written by tier 3 hours after the scan row, into its own
# column pair. Reading only the fast pair reported VTR -- excluded on
# dilution_veto and eps_collapse_veto -- as "Exclusion: clean", while the
# parameter table two lines below correctly marked both 🚫.
deep = copy.deepcopy(entry)
deep["scan_row"].update({"Veto": False, "Veto Reasons": json.dumps([]),
                         "Deep Veto": True,
                         "Deep Veto Reasons": json.dumps(["altman_z"])})
deep_body = combined_report.render([deep], cfg, combined_report.SCOPE_TICKER)
c.ok("a deep-stage veto is not reported as clean",
     "excluded" in deep_body and "altman_z" in deep_body,
     "the fast pair alone says False; the exclusion happened at the deep stage")
c.ok("...and the summary agrees with the parameter table",
     ("clean" not in deep_body.split("**Parameters**")[0].split("Exclusion")[1]
      .splitlines()[0]),
     "the headline contradicting its own table is worse than either alone")
c.ok("a multi-ticker report collapses the table instead of inlining it",
     "<details><summary>All parameter values</summary>" in combined_report.render(
         [entry, copy.deepcopy(entry)], cfg, combined_report.SCOPE_SUBSET),
     "73 rows per ticker would bury the comparison a subset report exists for")


# --------------------------------------------------------------------------
c.section("the enrichment cannot leak into the graded half")

# The strongest check here. Render once, add the most negative enrichment the
# vocabulary allows, render again: the graded block must not move by a single
# byte. If it ever does, the separation this whole design rests on has been
# undone by the thing that was meant to display it.
#
# Asserted on `_deterministic_block`, not on the rendered prefix: the report
# *header* legitimately changes, because it counts how many tickers were
# researched. That coverage line is the one place enrichment may show up outside
# its own section, and the second check below pins it to exactly that.
det_before = combined_report._deterministic_block(
    combined_report.collect_one("AAA", cfg))
before = combined_report.render([combined_report.collect_one("AAA", cfg)],
                                cfg, combined_report.SCOPE_TICKER)

enrichment.record({
    "scan_date": DATE, "ticker": "AAA", "stance": "bear",
    "moat_view": "eroding", "social_sentiment": "negative",
    "conviction_note": "The moat is thinner than the ratios imply.",
    "concerns": ["pricing pressure", "churn"], "catalysts": ["Q2 print"],
    "rule_disputes": ["altman_z"],
    "sources": ["https://example.com/a"]}, cfg)

after_entry = combined_report.collect_one("AAA", cfg)
after = combined_report.render([after_entry], cfg, combined_report.SCOPE_TICKER)
c.ok("a maximally bearish enrichment changes the graded block by nothing",
     combined_report._deterministic_block(after_entry) == det_before,
     "the report displays the two halves; it must never join them")

# ...and in the report *header* -- the only text outside a per-ticker section --
# the sole thing it may move is the coverage count.
head = [(a, b) for a, b in zip(before.split("### ")[0].splitlines(),
                               after.split("### ")[0].splitlines()) if a != b]
c.ok("the only header line enrichment may move is the coverage count",
     len(head) == 1 and "researched" in head[0][0],
     str(head) if head else "no difference at all")

c.ok("the enrichment half now renders",
     "bear" in after and "eroding" in after and "negative" in after)
c.ok("its note is quoted verbatim, not summarised",
     "The moat is thinner than the ratios imply." in after)
c.ok("concerns, catalysts and disputed rules are listed",
     "pricing pressure" in after and "Q2 print" in after and "altman_z" in after)
c.ok("sources are linked", "https://example.com/a" in after)
c.ok("the coverage line updates", "1 researched" in after)


# --------------------------------------------------------------------------
c.section("the prose report is inlined for one ticker and linked for many")

enrichment.report_path("AAA", DATE, cfg).write_text(
    "# AAA research\n\nThe long-form case.\n", encoding="utf-8")
one = combined_report.render([combined_report.collect_one("AAA", cfg)],
                             cfg, combined_report.SCOPE_TICKER)
c.ok("a single-ticker dossier inlines the full prose",
     "The long-form case." in one and "<details>" in one,
     "one name is a dossier; you should not need a second file")

pd.DataFrame([{"scan_date": DATE, "config_key": "pullback_strategy",
               "ticker": "BBB", "screen": "S", "Setup": "full"}]).to_csv(
    TMP / "on_demand.csv", index=False)
many = combined_report.render(
    [combined_report.collect_one(t, cfg) for t in ("AAA", "BBB")],
    cfg, combined_report.SCOPE_SUBSET)
c.ok("a multi-ticker report links the prose instead of inlining it",
     "The long-form case." not in many and "AAA_2026-03-02.md" in many,
     "inlining N reports would bury the comparison they exist for")


# --------------------------------------------------------------------------
c.section("scope decides the filename")

# The bug `universe_scan.artifact_paths` exists to prevent: date-stamped
# artifacts, so without a scope in the name a one-ticker run and a whole-week
# run resolve to the same path and one silently replaces the other.
paths = {
    "single": combined_report.report_path(cfg, combined_report.SCOPE_TICKER,
                                          ["AAA"], DATE),
    "subset": combined_report.report_path(cfg, combined_report.SCOPE_SUBSET,
                                          ["AAA", "BBB"], DATE),
    "signals": combined_report.report_path(cfg, combined_report.SCOPE_SIGNALS,
                                           ["AAA"], DATE),
}
c.ok("the three scopes cannot collide", len(set(paths.values())) == 3,
     ", ".join(p.name for p in paths.values()))
c.ok("a single ticker gets its own file",
     paths["single"].name.startswith("AAA_"),
     "so an unrelated run on the same day cannot overwrite a dossier")

result = combined_report.build(["AAA"], cfg)
c.ok("build writes the file and returns its path",
     Path(result["path"]).exists() and result["scope"] == "ticker")
c.ok("it reports which tickers were researched",
     result["researched"] == ["AAA"])
c.ok("build is idempotent -- same inputs, same bytes",
     Path(result["path"]).read_text(encoding="utf-8")
     == Path(combined_report.build(["AAA"], cfg)["path"]).read_text(
         encoding="utf-8"),
     "a rendering of unchanged records must not churn")

empty = combined_report.build([], cfg)
c.ok("no tickers is a reported no-op, not a crash or an empty file",
     empty["path"] is None and "nothing to report" in empty["note"])


# --------------------------------------------------------------------------
c.section("it computes nothing")

import inspect  # noqa: E402

src = inspect.getsource(combined_report)
for banned, why in (("axis_scores", "would re-grade the plane"),
                    ("quality.evaluate", "would re-run the registry"),
                    ("compute_quant_score", "would re-score the verdict"),
                    ("download", "would hit the network")):
    c.ok(f"it never calls {banned} -- that {why}", banned not in src)

raise SystemExit(c.finish())
