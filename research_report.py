"""
Tier 3 -- the graded investment case.

**The verdict is deterministic, full stop.** `deterministic_verdict` collects
the `deep` half of the quality registry, scores it, renders the financials chart
and sets `conviction = score`, `tier = quality.tier_for(score)`. It runs inside the
nightly scan, so tonight's verdict travels in the same Discord message as the
signal that produced it:

  * resolve_trigger(ticker, cfg)  -> the trigger, scanning on demand if the
      nightly scan never surfaced this ticker
  * deterministic_verdict(...)    -> score, tier, conviction, facts file, chart
  * deterministic_thesis(facts)   -> the sentence explaining it, built in Python
  * verdicts_for(tickers, cfg)    -> a batch, recorded to output/history/
  * list_candidates(hits, cfg)    -> who is worth researching, gated by tier 2
  * risk_report(ticker, cfg)      -> every rule beside the threshold it met
  * record_verdicts               -> the permanent record of tier + conviction

There is no model anywhere in this file, and no way to reach one from it. A
model *did* used to revise the conviction here by a bounded `narrative_adj`;
measured over its whole life it moved nine verdicts by at most 5 points and
changed no tier, while making the one score in the system unverifiable. It was
removed on 2026-08-11 -- see AI_ROLE.md for the measurement.

Qualitative research still happens, in the `enrich` skill, from a Claude Code
session. It reads `assemble_context` (these same numbers plus the SEC filings)
and records to `output/enrichment/` where tier 4 grades it. It cannot alter a
verdict, because there is no field here for it to alter.

CLI:
    python research_report.py candidates [--all] [--json]   # who to research
    python research_report.py verdicts [TICKER ...]         # deterministic, now
    python research_report.py risk TICKER [TICKER ...]      # every rule, valued
    python research_report.py scan PGR [RL ...]             # on-demand tiers 1+2
    python research_report.py context MSFT [JNJ ...]        # the data bundle
"""

import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import charts
import peers
import quality
import run_scanners
import sec
from scanner_common import (
    COMPANY_COL,
    CONVICTION_COL,
    DEEP_VETO_COL,
    DEEP_VETO_REASONS_COL,
    ON_DEMAND_KEYS,
    QUALITY_COL,
    QUALITY_MISSING_COL,
    VERDICT_COL,
    VETO_COLOR,
    count_csv_rows,
    enable_utf8_output,
    fmt_compact,
    history_rows,
    load_config,
    log_step,
    merge_history_csv,
    on_demand_csv_path,
    output_dir,
    run_id,
    signals_csv_path,
    stdout_to_stderr,
    step,
    update_csv_rows,
)

VERDICT_COLOR = 0x2A78D6  # matches the charts' "close" blue

# Where a ticker's trigger came from, and therefore which table its verdict is
# recorded in: a nightly signal has a `signals.csv` row to annotate, an on-demand
# look has none and gets its own table.
SOURCE_SIGNAL = "signal"
SOURCE_ON_DEMAND = "on_demand"


# --------------------------------------------------------------------------
# Hand-off: read what the nightly scan produced
# --------------------------------------------------------------------------

def load_hits(cfg: dict) -> dict:
    """Load latest_hits.json (the Stage-1 hand-off). Empty dict if absent."""
    path = Path(cfg.get("research", {}).get("latest_hits_path", "latest_hits.json"))
    if not path.is_absolute():
        path = output_dir() / path
    if not path.exists():
        # stderr, so `candidates --json` stays pipeable.
        print(f"(no hand-off file at {path} -- run run_scanners.py first)",
              file=sys.stderr)
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def find_ticker(hits: dict, ticker: str, cfg: dict | None = None) -> dict | None:
    """Locate `ticker` in the hand-off; return {screen, kind, ...} or None.

    `kind` is the row's tier-1 setup tier (`full` or `partial`) -- every screen
    reports one signal list, with partial setups tagged rather than split out.

    Tier 2 comes back twice, on purpose. `quality` / `quality_missing` are
    graded under the rules in force **now** (when `cfg` is passed), matching
    what the gate and the Discord card use, while `quality_recorded` /
    `quality_missing_recorded` are what the scan itself concluded. They differ
    whenever the thresholds were retuned between scan and report, and a report
    that says so is more useful than one that silently picks a side -- but the
    two must never be *confused*, which is why they are separate keys rather
    than one ambiguous one.

    A ticker that fired on more than one screen resolves to the first; use
    `list_candidates` when you need every (screen, ticker) pair.
    """
    for screen in hits.get("screens", []):
        row = screen.get("hits", {}).get(ticker)
        if row is None:
            continue
        recorded = row.get(QUALITY_COL)
        recorded_missing = row.get(QUALITY_MISSING_COL)
        passed, missing = (_row_quality(row, cfg) if cfg
                           else (recorded, recorded_missing))
        return {"screen": screen["title"], "config_key": screen["config_key"],
                "kind": row.get("Setup", "full"),
                "quality": passed,
                "quality_missing": missing,
                "quality_recorded": recorded,
                "quality_missing_recorded": recorded_missing,
                "strategy": screen.get("strategy", {}),
                "row": row}
    return None


def _trigger_summary(trigger: dict | None, payload: dict) -> str:
    """Both tiers in one log-line fragment: `pullback/full  quality PASS`.

    Reads the *re-graded* quality (what the gate and the card use), so the log
    can never disagree with the report about why a ticker was worth the work.
    """
    if trigger is None:
        return "no trigger"
    screens = len(payload.get("screens", []))
    passed = trigger.get("quality")
    verdict = ("not evaluated" if passed is None
               else "PASS" if passed
               else f"fails {', '.join(trigger.get('quality_missing') or []) or '?'}")
    return (f"{trigger.get('config_key')}/{trigger.get('kind')}  "
            f"{screens} screen(s)  quality {verdict}")


# --------------------------------------------------------------------------
# Tier 2 as a gate: which of tonight's hits are worth a deep dive
# --------------------------------------------------------------------------

GATES = ("all", "quality_pass")


def _row_quality(row: dict, cfg: dict) -> tuple[bool | None, list]:
    """The row's tier-2 verdict under the rules in force **now**.

    A thin wrapper over `quality.verdict_of` kept because the gate, the facts
    bundle and the candidate list all call it and read better for the name.
    It re-grades rather than trusting the recorded verdict on purpose: the
    hand-off is stamped with a scan date but the parameters get retuned between
    scans, and a gate answering with last night's bar would silently ignore a
    threshold change until the next scan. `output/history/` keeps the original,
    so nothing archived is rewritten.
    """
    return quality.verdict_of(row, cfg)


def list_candidates(hits: dict, cfg: dict, gate: str | None = None,
                    limit: int | None = None) -> list[dict]:
    """Tonight's deep-dive candidates, ranked, one entry per (screen, ticker).

    `gate` (default `research.auto.gate`) is a *soft* filter: `all` keeps every
    tier-1 hit and merely marks its tier-2 result, `quality_pass` keeps only the
    hits that passed every quality rule. Ranking is quality-pass first, then
    `full` before `partial`, then ticker -- so a `limit` takes the best
    candidates rather than an arbitrary slice.
    """
    auto_cfg = cfg.get("research", {}).get("auto", {})
    gate = gate or auto_cfg.get("gate", "quality_pass")
    if gate not in GATES:
        raise SystemExit(f"unknown gate {gate!r} -- expected one of {GATES}")

    rows = []
    for screen in hits.get("screens", []):
        for ticker, row in screen.get("hits", {}).items():
            passed, missing = _row_quality(row, cfg)
            rows.append({
                "ticker": ticker,
                "company": row.get(COMPANY_COL),
                "screen": screen.get("title"),
                "config_key": screen.get("config_key"),
                "setup": row.get("Setup", "full"),
                "missing": row.get("Missing", ""),
                "quality": passed,
                "quality_missing": missing,
            })
    rows.sort(key=lambda r: (not r["quality"], r["setup"] != "full", r["ticker"]))
    if gate == "quality_pass":
        rows = [r for r in rows if r["quality"]]
    return rows[:limit] if limit else rows


# --------------------------------------------------------------------------
# Deterministic quant score (the unified quality engine's scoring half)
# --------------------------------------------------------------------------

def compute_quant_score(bundle: dict, cfg: dict, ticker: str = "") -> dict:
    """The config-driven 0-100 anchor, from a `quality.collect` bundle.

    Thin by design: every metric definition, weight and good/bad anchor now
    lives in one place (`config.json`'s `quality` section, evaluated by
    `quality.evaluate`), so the score tier 3 reports and the badge tier 2 stamps
    are two readings of the same registry rather than two systems that happen
    to be about the same companies.

    The output keys are unchanged -- `score`, `dimensions`, `metrics` -- because
    they are written into `<T>_<date>_facts.json` and tier 4 explodes them into
    the `quant_*` / `qm_*` ledger columns.

    `ticker` is only used to look the sector up. Without it every
    `sector_relative` parameter falls back to its absolute anchors, so tier 3
    would grade a company on a different basis than the plane did -- silently,
    and in the direction the peer layer exists to correct.
    """
    values = quality.resolve(bundle, cfg)
    result = quality.evaluate(values, cfg,
                              sector=peers.sector_of(ticker, cfg) if ticker else "")
    return {
        "score": result.score,
        "dimensions": result.groups,
        "metrics": {k: (round(float(v), 3)
                        if isinstance(v, (int, float)) and not isinstance(v, bool)
                        else v)
                    for k, v in ((key, quality.scalar(raw))
                                 for key, raw in result.values.items())},
        "quality_pass": result.passed,
        "quality_missing": result.failed,
        # Graded over every stage, so this is the *whole* veto set -- the
        # `fast` rules the scan already answered plus the `deep` ones only this
        # pass can reach.
        "veto": result.vetoed,
        "veto_reasons": result.veto_reasons,
    }



# --------------------------------------------------------------------------
# The verdict: facts file, chart and table for one ticker
# --------------------------------------------------------------------------

def financials_table_md(fin: dict) -> str:
    """The financial trend as a markdown table, rendered not transcribed.

    The report shows the same numbers as the chart, and a PNG is not
    greppable. Generating the table here means the model pastes a block
    instead of retyping figures -- one less place for a digit to drift.
    """
    annual = (fin or {}).get("annual") or []
    quarterly = (fin or {}).get("quarterly") or []
    periods = annual + quarterly
    if not periods:
        return "_No financial history available._"

    def cell(row, key, kind):
        value = row.get(key)
        if value is None:
            return "n/a"
        return f"{value:.1f}%" if kind == "percent" else fmt_compact(value)

    header = "| Metric | " + " | ".join(r["period"] for r in periods) + " |"
    rule = "|---" * (len(periods) + 1) + "|"
    lines = [header, rule]
    for key, label, kind in charts.FINANCIAL_ROWS:
        lines.append(f"| {label} | "
                     + " | ".join(cell(r, key, kind) for r in periods) + " |")

    kinds = {r.get("margin_kind") for r in periods if r.get("margin_kind")}
    if kinds == {"pretax"}:
        lines.append("")
        lines.append("_Margin is pretax income / revenue: Yahoo reports no "
                     "Operating Income for this issuer (normal for banks and "
                     "insurers)._")
    elif "pretax" in kinds:
        lines.append("")
        lines.append("_Margin basis is mixed across periods (operating where "
                     "reported, pretax otherwise)._")
    return "\n".join(lines)


def _facts(ticker: str, scan_date: str, yahoo: dict, quant: dict,
           trigger: dict | None, chart: Path | None, cfg: dict,
           source: str = SOURCE_SIGNAL) -> dict:
    """The deterministic values the Discord card shows.

    Written to disk so the card, the record and the dossier all read the same
    figures back rather than recomputing them.

    `source` records whether the trigger came from the nightly hand-off or from
    an on-demand scan. It is the routing key for the permanent record, decided
    here because this is the only place that knows the answer.

    The quant *breakdown* is written alongside the aggregate score even though
    no card shows it: tier 4 grades which dimension actually predicted return,
    and a dimension that was never recorded can never be graded. Recomputing it
    later would answer with today's fundamentals rather than the ones the
    verdict was made on, which is exactly the drift `_row_quality` exists to
    avoid -- so it is captured here, once, at the moment of judgment.
    """
    valuation = yahoo.get("valuation") or {}
    targets = (yahoo.get("analyst") or {}).get("targets") or {}
    earnings = yahoo.get("earnings") or {}
    profile = yahoo.get("profile") or {}
    passed, quality_missing = _quality_now(trigger, cfg)
    score = quant.get("score")

    # The exclusion verdict over **every** stage, which is why it is computed
    # here rather than read off the trigger row: the scan could only answer the
    # `fast` rules, and the SEC filing flags, Beneish and the liquidity pair
    # arrive with this pass. The reasons are recorded either way; whether they
    # also override the tier is `quality.veto_enforced`, off by default so an
    # excluded name keeps a comparable score for tier 4 to grade.
    vetoes = quant.get("veto_reasons") or []
    tier = quality.tier_for(score, cfg) if score is not None else None
    if vetoes and quality.veto_enforced(cfg):
        tier = quality.veto_tier(cfg)
    return {
        "ticker": ticker,
        "scan_date": scan_date,
        "company": profile.get("company"),
        "price": targets.get("current"),
        "target_mean": targets.get("mean"),
        "upside_pct": targets.get("upside_pct"),
        "trailing_pe": valuation.get("trailingPE"),
        "pe_percentile_2y": valuation.get("pe_percentile_2y"),
        "next_earnings_date": earnings.get("next_date"),
        "days_to_earnings": earnings.get("days_to_next"),
        "quant_score": score,
        "quant_dimensions": quant.get("dimensions"),
        "quant_metrics": quant.get("metrics"),
        # Conviction *is* the score. Nothing revises it: the agent that adds
        # qualitative research records to its own table and has no field that
        # could reach this one.
        "tier": tier,
        "conviction": score,
        "screen": (trigger or {}).get("screen"),
        "setup": (trigger or {}).get("kind"),
        "quality": passed,
        "quality_missing": quality_missing,
        "veto": bool(vetoes),
        "veto_reasons": vetoes,
        "veto_text": quality.veto_text(vetoes, cfg) if vetoes else None,
        "chart": str(chart) if chart else None,
        "source": source,
    }


def _quality_now(trigger: dict | None, cfg: dict) -> tuple[bool | None, list]:
    """The trigger row's tier-2 verdict, graded like the gate grades it."""
    row = (trigger or {}).get("row")
    return _row_quality(row, cfg) if row else (None, [])


def resolve_trigger(ticker: str, cfg: dict) -> tuple:
    """Find this ticker's trigger, scanning it on demand when the scan missed it.

    Returns `(trigger, scan_date, source, on_demand_payload)`. A ticker the
    nightly scan never surfaced gets tiers 1 and 2 run for it now rather than
    arriving with an empty trigger and a quality verdict of "not evaluated" --
    which is what an ad-hoc deep dive used to look like.
    """
    hits = load_hits(cfg)
    scan_date = hits.get("scan_date") or date.today().isoformat()
    trigger = find_ticker(hits, ticker, cfg)   # re-graded, so it agrees with _facts
    if trigger is not None:
        log_step("HANDOFF", "hit", f"{ticker} {scan_date}  "
                 f"{_trigger_summary(trigger, hits)}", cfg=cfg)
        return trigger, scan_date, SOURCE_SIGNAL, None

    log_step("HANDOFF", "miss", f"{ticker} not in latest_hits.json", cfg=cfg)
    with step("SCAN", cfg=cfg) as s:
        on_demand = run_scanners.scan_ticker(ticker, cfg)
        # The on-demand scan dates itself off its own price data, so the report
        # and its record carry the day actually analysed, not the last nightly.
        scan_date = on_demand["scan_date"]
        trigger = find_ticker(on_demand, ticker, cfg)
        s.detail = f"on-demand {scan_date}  {_trigger_summary(trigger, on_demand)}"
    return trigger, scan_date, SOURCE_ON_DEMAND, on_demand


def price_inputs(tickers: list[str], cfg: dict):
    """`(closes, benchmark)` for a batch of verdicts: one download, SPY included.

    The same call the nightly card makes (`run_scanners.price_history`), so tier
    3 grades `beta` and `downside_beta` on the same basis the card and the plane
    do. Without a benchmark those two never resolve, and without closes every
    ticker fetches its own history -- twice, once per resolver that needs it.
    """
    return run_scanners.price_history(list(tickers), cfg)


def close_of(closes, ticker: str):
    """One ticker's close series out of a `price_inputs` frame, or None."""
    if closes is None or ticker not in getattr(closes, "columns", ()):
        return None
    return closes[ticker]


def deterministic_verdict(ticker: str, cfg: dict, trigger: dict | None,
                          scan_date: str, source: str = SOURCE_SIGNAL,
                          close=None, benchmark=None) -> dict:
    """Everything tier 3 can decide **without a model**, for one ticker.

    Collects **every** stage of the quality registry, scores it, renders the
    financial-trend chart and writes `<T>_<date>_facts.json` -- and sets the
    verdict from the score alone: `conviction = score`, `tier = quality.tier_for(score)`.

    The collection stage must stay `None` (= all stages). `compute_quant_score`
    grades every stage, so collecting only `deep` leaves all eleven `fast`
    parameters -- P/E, PEG, D/E, revenue growth, ROE, ROIC, the margins, FCF --
    resolving to None and silently absent from the score. That was the state of
    every `_facts.json` written before 2026-08-09: `financial_quality`, the
    heaviest group at weight 0.24, ran on 3 of its 8 metrics.

    This is the whole point of the deterministic pipeline. The verdict exists
    the moment the scan finishes, so it goes out in the same Discord message as
    the signal that produced it, and every figure behind it can be recomputed
    from the config that produced it.

    The chart and the facts file are written here, before this returns, so a
    session that reads the bundle later can never generate either.
    """
    fin_cfg = cfg.get("research", {}).get("financials", {})
    bundle = quality.collect(ticker, cfg, None, close=close, benchmark=benchmark)
    yahoo = bundle.get("_yahoo") or {}
    quant = compute_quant_score(bundle, cfg, ticker)
    log_step("QUANT", "ok", f"{ticker} score {quant.get('score')}", cfg=cfg)

    chart = None
    try:
        chart = report_dir(cfg) / f"{ticker}_{scan_date}_financials.png"
        charts.plot_financials(yahoo.get("financials") or {}, ticker, chart,
                               dpi=fin_cfg.get("chart_dpi", 120))
        log_step("CHART", "ok", chart.name, cfg=cfg)
    except Exception as exc:  # noqa: BLE001 - a chart must not sink the deep-dive
        log_step("CHART", "failed", f"{ticker}: {exc}", cfg=cfg)
        chart = None

    facts = _facts(ticker, scan_date, yahoo, quant, trigger, chart, cfg, source)
    facts_path = report_dir(cfg) / f"{ticker}_{scan_date}_facts.json"
    facts_path.write_text(json.dumps(facts, indent=2, ensure_ascii=False),
                          encoding="utf-8")
    log_step("FACTS", "ok", f"{facts_path.name}  source={source}", cfg=cfg)
    log_step("VERDICT", "ok",
             f"{ticker} {facts.get('tier')} {facts.get('conviction')}/100 "
             f"(deterministic)", cfg=cfg)

    return {
        "ticker": ticker,
        "scan_date": scan_date,
        "source": source,
        "trigger": trigger,
        "yahoo": yahoo,
        "quant": quant,
        "tier": facts.get("tier"),
        "conviction": facts.get("conviction"),
        "facts": facts,
        "facts_path": str(facts_path),
        "financials_chart": str(chart) if chart else None,
        "financials_table_md": financials_table_md(yahoo.get("financials") or {}),
    }


def deterministic_thesis(facts: dict) -> str:
    """One sentence describing where the score came from, built in Python.

    Same rule as tier 4's `conclusion` column and tier 3's financials chart: a
    sentence a model wrote is a sentence you cannot check. This one is a
    rendering of the group breakdown -- strongest and weakest, plus how much of
    the registry actually had data -- so it says only what the numbers say.
    """
    # The exclusion leads, because it is the conclusion: no amount of quality
    # elsewhere changes what a tripped veto means for whether you buy this.
    veto_sentence = ""
    if facts.get("veto"):
        veto_sentence = (f"EXCLUDED on {facts.get('veto_text')}"
                         f" -- the score below is recorded for measurement, not"
                         f" as a recommendation. ")

    dims = facts.get("quant_dimensions") or {}
    scored = {k: v for k, v in dims.items()
              if isinstance(v, dict) and v.get("metrics_used")}
    if not scored:
        return veto_sentence + (
            "No parameter in the quality registry returned a value for this "
            "ticker, so the score is not meaningful.")

    ranked = sorted(scored.items(), key=lambda kv: -kv[1]["score"])
    best, worst = ranked[0], ranked[-1]
    used = sum(v["metrics_used"] for v in scored.values())
    total = sum(v["metrics_total"] for v in dims.values() if isinstance(v, dict))

    def phrase(item):
        return f"{item[0].replace('_', ' ')} {item[1]['score']:.2f}"

    sentence = (f"Scores {facts.get('quant_score')}/100 on {used} of {total} "
                f"parameters: strongest {phrase(best)}")
    if len(ranked) > 1:
        sentence += f", weakest {phrase(worst)}"
    sentence += "."

    failed = facts.get("quality_missing") or []
    if facts.get("quality") is False and failed:
        shown = ", ".join(failed[:4]) + ("..." if len(failed) > 4 else "")
        sentence += f" Fails the quality screen on {shown}."
    elif facts.get("quality") is True:
        sentence += " Passes every enabled quality gate."
    return veto_sentence + sentence


def verdicts_for(tickers: list[str], cfg: dict) -> list[dict]:
    """Deterministic verdicts for a list of tickers, recorded as they are made.

    Each entry is the `{ticker, scan_date, tier, conviction, thesis}` shape
    `build_verdict_embeds` and `record_verdicts` consume. One ticker failing is
    logged and skipped -- an alert missing a card is better than an alert that
    never went out.
    """
    closes, benchmark = price_inputs(tickers, cfg)
    out = []
    for ticker in tickers:
        try:
            trigger, scan_date, source, _ = resolve_trigger(ticker, cfg)
            verdict = deterministic_verdict(
                ticker, cfg, trigger, scan_date, source,
                close=close_of(closes, ticker), benchmark=benchmark)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not kill the alert
            log_step("VERDICT", "failed", f"{ticker}: "
                     f"{type(exc).__name__}: {exc}", cfg=cfg)
            continue
        out.append({
            "ticker": ticker,
            "scan_date": verdict["scan_date"],
            "tier": verdict["tier"],
            "conviction": verdict["conviction"],
            "thesis": deterministic_thesis(verdict["facts"]),
        })
    if out:
        record_verdicts(out, cfg)
    return out


def assemble_context(ticker: str, cfg: dict | None = None) -> dict:
    """The deterministic bundle the `enrich` skill reasons over.

    `deterministic_verdict` plus the SEC filings -- i.e. the same numbers the
    nightly alert already posted, with the filing text qualitative research
    needs.
    Keeping the two separate is what lets the nightly run produce a verdict
    without paying for EDGAR, and lets the skill get the filings without
    recomputing the verdict.

    IBKR's qualitative graph and live web research are still added by Claude
    in-session; everything mechanical is already on disk when this returns.
    """
    cfg = cfg or load_config()
    t0 = time.perf_counter()
    log_step("CONTEXT", "start", f"{ticker}  run={run_id()}", cfg=cfg)

    trigger, scan_date, source, on_demand = resolve_trigger(ticker, cfg)
    closes, benchmark = price_inputs([ticker], cfg)
    verdict = deterministic_verdict(ticker, cfg, trigger, scan_date, source,
                                    close=close_of(closes, ticker),
                                    benchmark=benchmark)

    if on_demand is not None:
        record_on_demand(on_demand, ticker, cfg)

    filings = sec.fetch_filing_sections(ticker, cfg)
    bundle = {**verdict, "filings": filings}
    bundle.pop("facts", None)          # it is on disk; the path is in the bundle
    log_step("CONTEXT", "ok", f"{ticker} bundle ready"
             + ("" if filings else "  (no SEC filings)"),
             ms=(time.perf_counter() - t0) * 1000, cfg=cfg)
    return bundle


# --------------------------------------------------------------------------
# Archive: the report folder under output/
# --------------------------------------------------------------------------

def report_dir(cfg: dict, create: bool = True) -> Path:
    """Resolve `output/<report_subdir>`, creating it if asked.

    Reports follow the same rule as every other generated file -- a bare name
    in config resolved against `output_dir()`, an absolute path overriding it.

    **This is the one report directory.** It holds tier 3's recorded
    computation per ticker -- `<T>_<date>_facts.json` and
    `<T>_<date>_financials.png` -- and, since `combined.dir` was pointed here on
    2026-08-12, the `combined_report.py` dossier that renders them. The dossier
    is the only human-readable page and it spans every tier, so a second
    directory bought nothing but a place to look in and not find things.

    There used to be a `write_report` here, writing `<T>_<date>.md` from the
    narrative deep-dive. That pass was removed on 2026-08-11 (see `AI_ROLE.md`)
    and the function outlived it by a day, called from nowhere while 13 stale
    files sat in this directory looking current -- each one headed with a
    `quant N + narrative M` conviction that the scoring no longer produces.
    Don't reintroduce it: the dossier is generated by `combined_report.py`,
    which renders recorded files and computes nothing.
    """
    subdir = Path(cfg.get("research", {}).get("report_subdir", "reports"))
    out = subdir if subdir.is_absolute() else output_dir(create) / subdir
    if create:
        out.mkdir(parents=True, exist_ok=True)
    return out


# --------------------------------------------------------------------------
# Deliver: verdicts to Discord (reuse the existing webhook path)
# --------------------------------------------------------------------------



def _num_or_na(value, fmt="{:.1f}") -> str:
    return fmt.format(value) if isinstance(value, (int, float)) else "n/a"


def _ordinal(n: int) -> str:
    """1st / 2nd / 3rd / 4th -- including the 11-13 exception."""
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def load_facts(ticker: str, scan_date: str, cfg: dict) -> dict:
    """The deterministic facts `assemble_context` wrote for this ticker."""
    path = report_dir(cfg, create=False) / f"{ticker}_{scan_date}_facts.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a stale facts file must not block the post
        print(f"  facts unreadable for {ticker}: {exc}", file=sys.stderr)
        return {}


def _tier_color(tier: str, cfg: dict) -> int:
    """The band colour, except that an excluded card is always the veto red.

    `veto_tier` is deliberately not a `quality.tiers` band: the bands are score
    ranges that `tier_for` walks by `min`, and an exclusion is not a score
    range. So it is coloured here instead of being forced into that list.
    """
    if tier and tier == quality.veto_tier(cfg):
        return VETO_COLOR
    return quality.tier_color(tier, cfg, VERDICT_COLOR)


def _verdict_fields(f: dict, v: dict) -> list[dict]:
    """The six decision fields, inline so Discord lays them out 3-per-row."""
    def field(name, value):
        return {"name": name, "value": value or "n/a", "inline": True}

    quant = _num_or_na(f.get("quant_score"))
    setup = f.get("setup")
    screen = f.get("screen") or "ad-hoc"
    trigger = f"{screen}\n{setup}" if setup else screen

    if f.get("quality") is True:
        quality = "PASS ⭐"
    elif f.get("quality") is False:
        failed = f.get("quality_missing") or []
        quality = f"{len(failed)} rule(s) failed\n" + ", ".join(failed[:4])
    else:
        quality = "not evaluated"

    price, upside = f.get("price"), f.get("upside_pct")
    valuation = _num_or_na(price, "{:,.2f}")
    if isinstance(upside, (int, float)):
        valuation += f"  ({upside:+.0f}% to target)"

    pe = _num_or_na(f.get("trailing_pe"))
    pct = f.get("pe_percentile_2y")
    if isinstance(pct, (int, float)):
        pe += f"  ({_ordinal(round(pct))} pct, 2y)"

    days = f.get("days_to_earnings")
    when = f.get("next_earnings_date") or "n/a"
    if isinstance(days, (int, float)):
        when = f"{when}\nin {int(days)}d"

    fields = [field("Quant", quant), field("Trigger", trigger),
              field("Quality screen", quality), field("Price", valuation),
              field("P/E", pe), field("Next earnings", when)]

    # The exclusion goes first and full width: it is the one thing on the card
    # that changes the decision rather than informing it.
    if f.get("veto"):
        fields.insert(0, {"name": "🚫 Excluded",
                          "value": f.get("veto_text") or "veto",
                          "inline": False})
    return fields


def build_verdict_embeds(verdicts: list[dict], cfg: dict
                         ) -> tuple[list[dict], list[Path]]:
    """One card per ticker, entirely from the recorded facts.

    Everything numeric comes from the ticker's `_facts.json`, so the card can
    never disagree with what the collector actually measured; the verdict dict
    supplies only `tier`, `conviction` and `thesis`, all three deterministic.
    """
    embeds, images = [], []
    for v in sorted(verdicts, key=lambda x: -(x.get("conviction") or 0)):
        ticker = v["ticker"]
        facts = load_facts(ticker, v.get("scan_date") or "", cfg)
        company = v.get("company") or facts.get("company")
        tier = v.get("tier", "?")
        title = f"{tier} {v.get('conviction', '?')}/100 · {ticker}"
        if company:
            title += f" ({company[:44]})"
        embed = {
            "title": title,
            "description": (v.get("thesis") or "")[:1500],
            "color": _tier_color(tier, cfg),
            "fields": _verdict_fields(facts, v),
            "footer": {"text": str(report_dir(cfg, create=False))},
        }
        chart = facts.get("chart")
        if chart and Path(chart).exists():
            embed["image"] = {"url": f"attachment://{Path(chart).name}"}
            images.append(Path(chart))
        embeds.append(embed)
    return embeds, images


# --------------------------------------------------------------------------
# The permanent record: every verdict, in the table that fits its provenance
# --------------------------------------------------------------------------
# Discord is a notification and `output/reports/` is prose; neither accumulates
# into something you can load and study. These two tables do, split by where the
# ticker came from: a nightly signal annotates its existing `signals.csv` row,
# an on-demand look has no such row and gets its own table carrying the ratios
# that make an old verdict interpretable.

# Columns only tier 3 fills in, long after the scan row was written -- so every
# rewrite of the on-demand table has to carry them forward.
ON_DEMAND_VERDICT_COLS = [VERDICT_COL, CONVICTION_COL, "Quant Score", "Price", "Upside %", "P/E Pctile 2y",
                          "Report", "Thesis",
                          DEEP_VETO_COL, DEEP_VETO_REASONS_COL]


def on_demand_row(payload: dict, ticker: str) -> dict | None:
    """One flat row for the on-demand table, from a `scan_ticker` payload.

    Flattened by `scanner_common.history_rows` -- the same function that builds
    `signals.csv` -- so both tables carry identical, config-driven fundamentals
    labels and can be compared column for column.

    A ticker firing more than one screen keeps the *first* screen's row, which
    is the one `find_ticker` resolves to and therefore the trigger the deep dive
    actually reasoned about; `Screens` names all of them.
    """
    rows = [r for r in history_rows(payload) if r.get("ticker") == ticker]
    if not rows:
        return None
    base = dict(rows[0])
    screens = ", ".join(dict.fromkeys(r.get("screen") or "" for r in rows))
    row = {
        "scan_date": base.pop("scan_date", None),
        "run_at": base.pop("generated_at", None),
        "ticker": base.pop("ticker", None),
        COMPANY_COL: base.pop(COMPANY_COL, None),
        "Screens": screens,
    }
    base.pop("config_key", None)   # replaced by Screens
    base.pop("screen", None)
    row.update(base)
    return row


def record_on_demand(payload: dict, ticker: str, cfg: dict) -> Path | None:
    """Archive an on-demand scan's tier-1/tier-2 row, verdict columns blank.

    Written as soon as the scan runs, so a ticker you looked at but never
    deep-dived is still on the record; `record_verdict` fills in the judgment
    later if it comes.

    Skipped when `signals.csv` already covers that (scan_date, ticker) -- you
    can perfectly well scan a ticker the nightly run also caught, and its
    verdict routes to the signal row. Writing here too would leave an orphan
    that can never receive one. The invariant is worth stating: **a
    (scan_date, ticker) belongs to exactly one of the two tables.**
    """
    if not cfg.get("research", {}).get("history", {}).get("enabled", True):
        return None
    row = on_demand_row(payload, ticker)
    if row is None:
        return None
    match = {"scan_date": str(row.get("scan_date")), "ticker": ticker}
    if count_csv_rows(signals_csv_path(cfg, create=False), match):
        log_step("RECORD", "skip",
                 f"{ticker} already in {signals_csv_path(cfg).name} for "
                 f"{match['scan_date']}", cfg=cfg)
        print(f"{ticker} already has a {signals_csv_path(cfg).name} row for "
              f"{match['scan_date']} -- its verdict records there.")
        return None
    path = on_demand_csv_path(cfg)
    frame = merge_history_csv(path, [row], ON_DEMAND_KEYS,
                              protect=ON_DEMAND_VERDICT_COLS)
    log_step("RECORD", "ok", f"{ticker} -> {path.name}  {len(frame)} row(s)",
             cfg=cfg)
    print(f"Recorded {ticker} in {path.name} ({len(frame)} row(s) total).")
    return path


def _verdict_values(verdict: dict, facts: dict, full: bool) -> dict:
    """The columns a recorded verdict sets.

    `signals.csv` gets only the tier and the conviction -- the row beside them
    already holds the signal and the ratios. The on-demand table has no such
    neighbour, so it gets the figures needed to read the verdict a year later.
    """
    values = {VERDICT_COL: verdict.get("tier"),
              CONVICTION_COL: verdict.get("conviction"),
              # The whole veto set as of this pass, including the `deep` rules
              # the scan could not reach. Written under their own column names
              # rather than the scan's, because `merge_history_csv` inherits a
              # protected column from the row already on file -- right for a
              # value only tier 3 produces, and wrong for one the scan itself
              # just computed.
              DEEP_VETO_COL: bool(facts.get("veto")),
              DEEP_VETO_REASONS_COL: json.dumps(facts.get("veto_reasons") or [])}
    if not full:
        return values
    scan_date = verdict.get("scan_date") or ""
    values.update({
        "Quant Score": facts.get("quant_score"),
        "Price": facts.get("price"),
        "Upside %": facts.get("upside_pct"),
        "P/E Pctile 2y": facts.get("pe_percentile_2y"),
        "Report": f"{verdict.get('ticker')}_{scan_date}.md",
        "Thesis": verdict.get("thesis"),
    })
    return values


def enforce_veto(verdict: dict, facts: dict, cfg: dict) -> dict:
    """A recorded verdict on a vetoed ticker keeps the veto tier, whatever it says.

    Nothing in the nightly path can now report a tier that disagrees with the
    exclusion, so on that path this is a tautology. It stays because it is the
    guard for every *other* way a verdict can be recorded -- a hand-written one,
    a re-record after `quality.parameters` was retuned -- and because the rule it
    enforces is the one that must never quietly lapse: a veto is a deterministic
    rule over collected values, and no caller may relabel a row out of one.

    The conviction is left exactly as it stands, so the record still says what
    the company scored and tier 4 can measure what the exclusion cost.

    Gated on `quality.veto_enforced`: with the veto demoted to a label there is
    no forced tier to protect and the recorded tier stands.
    """
    if not facts.get("veto") or not quality.veto_enforced(cfg):
        return verdict
    forced = quality.veto_tier(cfg)
    if verdict.get("tier") != forced:
        log_step("VERDICT", "warn",
                 f"{verdict.get('ticker')} reported {verdict.get('tier')} but "
                 f"is vetoed on {facts.get('veto_text')} -- forcing {forced}",
                 cfg=cfg)
        verdict["tier"] = forced
    return verdict


def record_verdict(verdict: dict, cfg: dict) -> str | None:
    """Route one verdict to its table and write it; return the file name.

    The routing question -- signal or ad-hoc? -- was answered once, by `_facts`,
    at the only place that knew. Here it is only read back. A verdict is never
    written to both tables and never dropped: with no scan row to annotate, the
    on-demand table gains a minimal one rather than losing the judgment.
    """
    if not cfg.get("research", {}).get("history", {}).get("enabled", True):
        return None
    ticker, scan_date = verdict.get("ticker"), verdict.get("scan_date")
    if not ticker or not scan_date:
        print(f"  cannot record a verdict without ticker and scan_date: "
              f"{verdict.get('ticker') or verdict}", file=sys.stderr)
        return None

    facts = load_facts(ticker, scan_date, cfg)
    enforce_veto(verdict, facts, cfg)
    match = {"scan_date": scan_date, "ticker": ticker}

    if facts.get("source") != SOURCE_ON_DEMAND:
        path = signals_csv_path(cfg, create=False)
        updated = update_csv_rows(path, match,
                                  _verdict_values(verdict, facts, full=False))
        if updated:
            log_step("VERDICT", "ok",
                     f"{ticker} {verdict.get('tier')} {verdict.get('conviction')}"
                     f" -> {path.name} ({updated} row(s))", cfg=cfg)
            print(f"  {ticker}: {verdict.get('tier')} "
                  f"{verdict.get('conviction')} -> {path.name} "
                  f"({updated} row(s))")
            return path.name
        # No signal row: either an ad-hoc look whose facts file is missing, or
        # a scan whose rows have since been rewritten. The on-demand table is
        # the honest home for it either way.

    path = on_demand_csv_path(cfg)
    values = _verdict_values(verdict, facts, full=True)
    if not update_csv_rows(path, match, values):
        merge_history_csv(path, [{
            "scan_date": scan_date,
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ticker": ticker,
            COMPANY_COL: facts.get("company"),
            "Screens": facts.get("screen") or "",
            "Setup": facts.get("setup") or "",
            QUALITY_COL: facts.get("quality"),
            QUALITY_MISSING_COL: json.dumps(facts.get("quality_missing") or []),
            **values,
        }], ON_DEMAND_KEYS)
    log_step("VERDICT", "ok",
             f"{ticker} {verdict.get('tier')} {verdict.get('conviction')} "
             f"-> {path.name}", cfg=cfg)
    print(f"  {ticker}: {verdict.get('tier')} {verdict.get('conviction')} "
          f"-> {path.name}")
    return path.name


def record_verdicts(verdicts: list[dict], cfg: dict) -> None:
    """Record every verdict in the batch. Runs whether or not Discord was sent:
    the record is the point, the notification is not."""
    if not verdicts:
        return
    print("Recording verdicts:")
    for verdict in verdicts:
        record_verdict(verdict, cfg)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

USAGE = """usage:
  python research_report.py candidates [--all] [--json]   who is worth researching
  python research_report.py verdicts [TICKER ...]         deterministic verdicts, recorded now
  python research_report.py scan TICKER [TICKER ...]      on-demand tiers 1 + 2 for a ticker
  python research_report.py risk TICKER [TICKER ...]      disaster symptoms + moat, every rule
  python research_report.py context TICKER [TICKER ...]   the data bundle for one ticker

Qualitative research is the `enrich` skill, run from a Claude Code session --
nothing here starts a model. It records to output/enrichment/ (enrichment.py)
and cannot change a verdict."""


def _print_candidates(rows: list[dict], held_back: int, gate: str) -> None:
    """The candidate table, plus what the gate removed -- never silently."""
    if not rows:
        print("No candidates.")
    for r in rows:
        mark = {True: "PASS", False: "fail", None: "n/a "}[r["quality"]]
        why = ""
        if r["quality"] is False and r["quality_missing"]:
            why = "  fails: " + ", ".join(r["quality_missing"])
        company = f" ({r['company']})" if r.get("company") else ""
        print(f"  {mark}  {r['ticker']:<6}{company:<34.34} "
              f"{r['setup']:<8}{r['config_key']}{why}")
    if held_back:
        print(f"\n({held_back} more signal(s) held back by gate '{gate}' -- "
              f"rerun with --all to see them.)")


def risk_report(ticker: str, cfg: dict) -> dict:
    """Every disaster symptom and moat proxy for one ticker, rule by rule.

    Collects the whole registry, so it reaches the `deep` rules the nightly
    scan cannot: the SEC filing flags, Beneish, the liquidity pair. Reports the
    value *and* the rule beside it, because "Altman Z 1.4" only means something
    next to the threshold it was compared against -- and because a veto that
    did not fire for want of data has to be distinguishable from one that
    passed on the merits.
    """
    bundle = quality.collect(ticker, cfg, None)
    values = quality.resolve(bundle, cfg, None)
    # The sector has to be threaded in, or every `sector_relative` rule falls back
    # to its absolute anchor and this report contradicts the plane about the same
    # company -- including its veto, since the peer conjunction that keeps the
    # exclusion quiet is simply skipped when the sector is unknown.
    sector = peers.sector_of(ticker, cfg)
    result = quality.evaluate(values, cfg, None, sector)
    specs = quality.parameters(cfg, None)

    def entry(key: str) -> dict:
        spec = specs.get(key) or {}
        value = quality.scalar(values.get(key), spec)
        return {
            "key": key,
            "label": quality.label_of(key, spec),
            "group": spec.get("group"),
            "stage": spec.get("stage", quality.STAGE_FAST),
            "value": value,
            "gate": spec.get("gate"),
            "veto": bool(spec.get("veto")),
            # Three states, never two: tripped, clean, or unknown for want of a
            # value. A missing value never vetoes, so conflating it with
            # "clean" here would misrepresent how much the layer actually saw.
            "state": ("unknown" if value is None else
                      ("tripped" if key in result.veto_reasons else "clean")),
        }

    vetoes = [entry(k) for k in quality.veto_parameters(cfg, None)]
    moat = {k: quality.scalar(values.get(k), specs[k]) for k in specs
            if specs[k].get("group") == "moat"}
    risk = {k: quality.scalar(values.get(k), specs[k]) for k in specs
            if specs[k].get("group") == "risk" and not specs[k].get("veto")}
    # Each risk metric's own 0-1 reading, worst first. This is what makes the
    # group's aggregation auditable: a `worst_k` axis is only as good as the
    # anchors on the metrics it selects, and a metric every company trips shows
    # up here as a 0.0 sitting at the top of the list.
    #
    # `quality.normalized_of` is the one definition, shared with `group_scores`
    # itself: `worst_k` selects the lowest readings, so a list built from the
    # absolute anchors would name a different "worst three" than the ones
    # actually driving the number beside it.
    normalized = sorted(
        ((k, r) for k in specs
         if specs[k].get("group") == "risk"
         and (r := quality.normalized_of(k, specs[k], values.get(k), cfg,
                                         sector)) is not None),
        key=lambda kv: kv[1])
    return {
        "ticker": ticker,
        "company": bundle.get(COMPANY_COL),
        "vetoed": result.vetoed,
        "veto_reasons": result.veto_reasons,
        "veto_text": quality.veto_text(result.veto_reasons, cfg),
        "rules": vetoes,
        "moat_metrics": moat,
        "risk_metrics": risk,
        "moat_score": (result.groups.get("moat") or {}).get("score"),
        "risk_score": (result.groups.get("risk") or {}).get("score"),
        "quant_score": result.score,
        # The quadrant coordinates. `risk_axis` is high-is-bad; `risk_score`
        # above is the raw group reading, high-is-good -- they are complements,
        # not duplicates, and the names are deliberately not interchangeable.
        "reward_axis": result.reward,
        "risk_axis": result.risk,
        "risk_normalized": normalized,
    }


def _print_risk(report: dict, cfg: dict) -> None:
    """The risk report as text: the verdict, then every rule and why."""
    company = f" ({report['company']})" if report.get("company") else ""
    print(f"\n{report['ticker']}{company}")
    if report["vetoed"]:
        print(f"  EXCLUDED -- {report['veto_text']}")
    else:
        print("  not excluded")
    print(f"  moat {_num_or_na(report['moat_score'], '{:.2f}')}   "
          f"risk {_num_or_na(report['risk_score'], '{:.2f}')}   "
          f"overall {_num_or_na(report['quant_score'])}/100")
    print(f"  QUADRANT: reward {_num_or_na(report.get('reward_axis'))}/100   "
          f"risk {_num_or_na(report.get('risk_axis'))}/100")
    worst = report.get("risk_normalized") or []
    if worst:
        print("  worst risk readings: "
              + ", ".join(f"{k}={v:.2f}" for k, v in worst[:5]))

    for state in ("tripped", "clean", "unknown"):
        rules = [r for r in report["rules"] if r["state"] == state]
        if not rules:
            continue
        print(f"  {state}:")
        for rule in sorted(rules, key=lambda r: r["label"]):
            bound = ", ".join(f"{k} {v}" for k, v in (rule["gate"] or {}).items()
                              if v is not None)
            print(f"    {rule['label']:<22.22} "
                  f"{_num_or_na(rule['value']):>10}   ({bound or 'no gate'})")

    for name, metrics in (("moat", report["moat_metrics"]),
                          ("risk", report["risk_metrics"])):
        shown = {k: v for k, v in metrics.items() if v is not None}
        if shown:
            print(f"  {name}: " + ", ".join(f"{k}={v:.2f}"
                                            for k, v in sorted(shown.items())))


def _print_scan(payload: dict, ticker: str, cfg: dict) -> None:
    """One on-demand scan, readable: what fired, and what tier 2 made of it."""
    print(f"\n{ticker} -- scanned {payload.get('scan_date')}")
    for screen in payload.get("screens", []):
        row = screen.get("hits", {}).get(ticker)
        if row is None:
            continue
        missing = row.get("Missing") or ""
        print(f"  tier 1  {screen['title']}: {row.get('Setup', '?')}"
              + (f" -- {missing}" if missing else ""))

    trigger = find_ticker(payload, ticker, cfg)
    if trigger is None:
        print("  tier 2  no row produced")
        return
    for field in quality.embed_fields(trigger["row"], cfg):
        print(f"          {field['name']}: {field['value']}")
    passed, failed = trigger["quality"], trigger["quality_missing"]
    if passed is None:
        print("  tier 2  not evaluated (quality layer off)")
    elif passed:
        print(f"  tier 2  PASS {quality.badge(cfg)}".rstrip())
    else:
        print(f"  tier 2  fails: {', '.join(failed)}")


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        return 1

    if args[0] == "candidates":
        cfg = load_config()
        hits = load_hits(cfg)
        gate = "all" if "--all" in args else None
        rows = list_candidates(hits, cfg, gate=gate)
        if "--json" in args:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0
        total = len(list_candidates(hits, cfg, gate="all"))
        effective = gate or cfg.get("research", {}).get("auto", {}).get(
            "gate", "quality_pass")
        _print_candidates(rows, total - len(rows), effective)
        return 0

    if args[0] == "verdicts":
        # The deterministic verdict on demand: the same call the nightly scan
        # makes, for named tickers or for tonight's gated candidates. Records
        # to output/history/ and prints; posting is the scan's job.
        cfg = load_config()
        tickers = [a.upper() for a in args[1:] if not a.startswith("--")]
        if not tickers:
            auto = cfg.get("research", {}).get("auto", {})
            tickers = list(dict.fromkeys(
                c["ticker"] for c in list_candidates(
                    load_hits(cfg), cfg, limit=auto.get("max_reports"))))
        if not tickers:
            print("no candidate passed the gate -- nothing to grade")
            return 1
        with stdout_to_stderr():
            rows = verdicts_for(tickers, cfg)
        for row in rows:
            print(f"{row['ticker']:6} {row['tier']:7} {row['conviction']}/100  "
                  f"{row['thesis']}")
        return 0 if rows else 1

    # Every rule beside the value it was compared against, split into tripped /
    # clean / unknown. The `enrich` skill reads the same thing through the MCP
    # `risk_research` tool, which adds the sector peer group.
    if args[0] == "risk":
        cfg = load_config()
        tickers = [a.upper() for a in args[1:] if not a.startswith("-")]
        if not tickers:
            print("usage: risk TICKER [TICKER ...]", file=sys.stderr)
            return 1
        with stdout_to_stderr():
            reports = [risk_report(t, cfg) for t in tickers]
        if "--json" in args:
            print(json.dumps(reports, indent=2, ensure_ascii=False))
            return 0
        for report in reports:
            _print_risk(report, cfg)
        return 0

    # On-demand: tiers 1 + 2 for a ticker you name, whether or not the nightly
    # scan surfaced it. Recorded immediately, so a look you never deep-dive is
    # still on the record.
    if len(args) >= 2 and args[0] == "scan":
        cfg = load_config()
        for name in args[1:]:
            ticker = name.upper()
            with step("SCAN", cfg=cfg) as s:
                payload = run_scanners.scan_ticker(ticker, cfg)
                s.detail = f"{ticker} {payload.get('scan_date')} (on-demand scan)"
            _print_scan(payload, ticker, cfg)
            record_on_demand(payload, ticker, cfg)
        return 0

    # stdout here is *structured* -- the skill parses it. Everything the
    # assembly prints on the way (tiers 1+2's progress lines, the step log) is
    # a diagnostic and belongs on stderr; only the bundle goes to stdout.
    if len(args) >= 2 and args[0] == "context":
        for t in args[1:]:
            with stdout_to_stderr() as out:
                bundle = assemble_context(t.upper())
            print(json.dumps(bundle, indent=2, default=str,
                             ensure_ascii=False), file=out)
        return 0

    print(USAGE)
    return 1


if __name__ == "__main__":
    enable_utf8_output()   # the quality badge is unprintable in a Windows codepage
    sys.exit(main())
