"""
Deep-dive pipeline -- Stage 2 toolkit.

Synthesis is a *skill-driven procedure* (`.claude/skills/deep-dive`), not one
Python function: Claude reasons in-session over the collected data + IBKR Tier-B
(interactive MCP) + live web research to write the investment case and set the
verdict. This module provides the deterministic pieces around that reasoning:

  * list_candidates(hits, cfg) -> tonight's deep-dive candidates, gated by tier 2
  * assemble_context(ticker) -> the data bundle Claude reasons over
      (trigger + Yahoo Tier-A + deterministic quant score + SEC 10-Q/10-K filings),
      running tiers 1+2 on demand for a ticker the nightly scan never surfaced
  * compute_quant_score(yahoo, cfg) -> the config-driven 0-100 anchor
  * report_dir / write_report -> archive the full report under output/
  * post_summary / post_verdict -> deliver to Discord (reuse send_discord_alert)
  * record_verdicts -> the permanent record of tier + conviction

The verdict = tier + conviction: conviction = clamp(quant + narrative_adj, 0,
100), narrative_adj (bounded by config) is Claude's qualitative adjustment; the
tier comes from config conviction bands.

CLI:
    python research_report.py candidates [--all] [--json]   # who to deep-dive
    python research_report.py auto-prompt                   # the nightly prompt
    python research_report.py scan PGR [RL ...]             # on-demand tiers 1+2
    python research_report.py context MSFT [JNJ ...]        # the data bundle
"""

import json
import math
import re
import sys
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import charts
import run_scanners
import sec
from research_collect import collect_yahoo
from scanner_common import (
    COMPANY_COL,
    CONVICTION_COL,
    ON_DEMAND_KEYS,
    QUALITY_COL,
    QUALITY_MISSING_COL,
    RUN_KEYS,
    VERDICT_COL,
    count_csv_rows,
    enable_utf8_output,
    fmt_bytes,
    fmt_compact,
    format_step,
    fundamentals_fields,
    history_rows,
    load_config,
    log_step,
    manifest_csv_path,
    merge_history_csv,
    new_run_id,
    on_demand_csv_path,
    output_dir,
    prune_run_logs,
    quality_enabled,
    quality_failures,
    run_id,
    run_log_path,
    run_result_path,
    send_discord_alert,
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
        # stderr, so `candidates --json` and `auto-prompt` stay pipeable.
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
        quality, missing = (_row_quality(row, cfg) if cfg
                            else (recorded, recorded_missing))
        return {"screen": screen["title"], "config_key": screen["config_key"],
                "kind": row.get("Setup", "full"),
                "quality": quality,
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
    quality = trigger.get("quality")
    verdict = ("not evaluated" if quality is None
               else "PASS" if quality
               else f"fails {', '.join(trigger.get('quality_missing') or []) or '?'}")
    return (f"{trigger.get('config_key')}/{trigger.get('kind')}  "
            f"{screens} screen(s)  quality {verdict}")


# --------------------------------------------------------------------------
# Tier 2 as a gate: which of tonight's hits are worth a deep dive
# --------------------------------------------------------------------------

GATES = ("all", "quality_pass")


def _row_quality(row: dict, cfg: dict) -> tuple[bool | None, list]:
    """The row's tier-2 verdict under the rules in force **now**.

    Deliberately re-evaluates rather than trusting the verdict the scan
    recorded. The hand-off is stamped with a scan date but the rules get
    retuned between scans, and a gate that answered with last night's bar
    would silently ignore a threshold change until the next scan -- the exact
    confusion of loosening a rule and watching `candidates` report the old
    answer. The archived snapshot in `output/history/` keeps the original
    verdict, so nothing historical is rewritten.

    `quality_failures` works unchanged on a JSON-round-tripped row -- it looks
    values up by display label and indexes the multi-year series positionally,
    so nested arrays behave exactly like the original tuples. That is also
    what lets a hand-off written before the verdict existed still be graded.
    """
    fund_cfg = cfg.get("fundamentals", {})
    if quality_enabled(fund_cfg) and _has_fundamentals(row, fund_cfg):
        failed = quality_failures(row, fund_cfg)
        return not failed, failed
    if QUALITY_COL in row:      # fundamentals gone or disabled -- trust the record
        return bool(row[QUALITY_COL]), list(row.get(QUALITY_MISSING_COL) or [])
    return None, []


def _has_fundamentals(row: dict, fund_cfg: dict) -> bool:
    """Whether the row still carries the values the rules grade.

    Without this, a row scanned with fundamentals switched off would be
    re-graded as failing everything (a missing value fails its rule) instead
    of reporting honestly that quality was never evaluated.
    """
    labels = list((fund_cfg.get("fields") or {}).values())
    labels += list((fund_cfg.get("statements", {}).get("metrics") or {}).values())
    return any(row.get(label) is not None for label in labels)


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
            quality, missing = _row_quality(row, cfg)
            rows.append({
                "ticker": ticker,
                "company": row.get(COMPANY_COL),
                "screen": screen.get("title"),
                "config_key": screen.get("config_key"),
                "setup": row.get("Setup", "full"),
                "missing": row.get("Missing", ""),
                "quality": quality,
                "quality_missing": missing,
            })
    rows.sort(key=lambda r: (not r["quality"], r["setup"] != "full", r["ticker"]))
    if gate == "quality_pass":
        rows = [r for r in rows if r["quality"]]
    return rows[:limit] if limit else rows


# --------------------------------------------------------------------------
# Deterministic quant score (config-driven anchor)
# --------------------------------------------------------------------------

def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _norm(v: float, good: float, bad: float) -> float:
    """Linear map to [0,1]: good->1, bad->0 (works either direction via good/bad
    ordering), clamped."""
    if good == bad:
        return 0.5
    return max(0.0, min(1.0, (v - bad) / (good - bad)))


def _avg(xs):
    xs = [x for x in xs if _is_num(x)]
    return sum(xs) / len(xs) if xs else None


def _revision_net(d):
    if not isinstance(d, dict):
        return None
    up, dn = d.get("upLast30days"), d.get("downLast30days")
    if not (_is_num(up) and _is_num(dn)) or (up + dn) <= 0:
        return None
    return (up - dn) / (up + dn)


def _trend_change(d):
    if not isinstance(d, dict):
        return None
    cur, old = d.get("current"), d.get("90daysAgo")
    if not (_is_num(cur) and _is_num(old)) or old == 0:
        return None
    return (cur - old) / abs(old)


def _beat_rate(hist):
    ss = [h.get("surprise_pct") for h in (hist or []) if _is_num(h.get("surprise_pct"))]
    return sum(1 for s in ss if s > 0) / len(ss) if ss else None


def _buy_ratio(recs):
    if not recs:
        return None
    r = recs[0]
    tot = sum((r.get(k) or 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
    return ((r.get("strongBuy") or 0) + (r.get("buy") or 0)) / tot if tot else None


def _extract_metrics(yahoo: dict) -> dict:
    """Flatten collect_yahoo output to the scalar metrics the config scores."""
    val = yahoo.get("valuation") or {}
    ana = yahoo.get("analyst") or {}
    est = yahoo.get("estimates") or {}
    earn = yahoo.get("earnings") or {}
    q = yahoo.get("quality") or {}
    own = yahoo.get("ownership") or {}
    growth = est.get("growth") or {}
    targets = ana.get("targets") or {}

    def g(period, key):  # a growth stockTrend as a percent
        cell = growth.get(period) or {}
        v = cell.get(key)
        return v * 100 if _is_num(v) else None

    hist = earn.get("surprise_history") or []
    return {
        "pe_percentile_2y": val.get("pe_percentile_2y"),
        "analyst_upside_pct": targets.get("upside_pct"),
        "growth_this_year_pct": g("0y", "stockTrend"),
        "growth_next_year_pct": g("+1y", "stockTrend"),
        "eps_revision_net": _revision_net(est.get("eps_revisions", {}).get("0y")),
        "eps_trend_change": _trend_change(est.get("eps_trend", {}).get("0y")),
        "earnings_avg_surprise": _avg([h.get("surprise_pct") for h in hist]),
        "earnings_beat_rate": _beat_rate(hist),
        "return_on_assets": q.get("returnOnAssets_pct"),
        "gross_margin": q.get("grossMargins_pct"),
        "net_debt_to_ebitda": q.get("netDebtToEbitda"),
        "buyback_2y": own.get("shares_change_2y_pct"),
        "analyst_buy_ratio": _buy_ratio(ana.get("recommendations")),
    }


def compute_quant_score(yahoo: dict, cfg: dict) -> dict:
    """Config-driven 0-100 anchor. Each dimension = mean of its available
    metrics' normalized scores (missing metric skipped; a dimension with no
    data -> neutral 0.5, so banks aren't unfairly zeroed)."""
    syn = cfg["research"]["synthesis"]
    m = _extract_metrics(yahoo)
    dims, total = {}, 0.0
    for dim, dcfg in syn["dimensions"].items():
        parts = [_norm(m[name], b["good"], b["bad"])
                 for name, b in dcfg["metrics"].items() if _is_num(m.get(name))]
        ds = sum(parts) / len(parts) if parts else 0.5
        dims[dim] = {"score": round(ds, 3), "weight": dcfg["weight"],
                     "metrics_used": len(parts), "metrics_total": len(dcfg["metrics"])}
        total += dcfg["weight"] * ds
    # weighted mean -> robust to weights that don't sum to exactly 1.0
    wsum = sum(d["weight"] for d in syn["dimensions"].values()) or 1.0
    return {
        "score": round(100 * total / wsum, 1),
        "dimensions": dims,
        "metrics": {k: (round(float(v), 3) if _is_num(v) else None) for k, v in m.items()},
    }


def tier_for(conviction: float, cfg: dict) -> str:
    """Map a 0-100 conviction to a tier label via config bands."""
    tiers = sorted(cfg["research"]["synthesis"]["tiers"], key=lambda t: -t["min"])
    for t in tiers:
        if conviction >= t["min"]:
            return t["label"]
    return tiers[-1]["label"]


# --------------------------------------------------------------------------
# The context bundle Claude reasons over
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

    Written to disk so `post_summary` reads them back rather than the model
    retyping them: every figure on the notification comes from the collector,
    and the model contributes only tier, conviction, the adjustment and the
    thesis.

    `source` records whether the trigger came from the nightly hand-off or from
    an on-demand scan. It is the routing key for the permanent record, decided
    here because this is the only place that knows the answer.
    """
    valuation = yahoo.get("valuation") or {}
    targets = (yahoo.get("analyst") or {}).get("targets") or {}
    earnings = yahoo.get("earnings") or {}
    profile = yahoo.get("profile") or {}
    quality, quality_missing = _quality_now(trigger, cfg)
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
        "quant_score": quant.get("score"),
        "screen": (trigger or {}).get("screen"),
        "setup": (trigger or {}).get("kind"),
        "quality": quality,
        "quality_missing": quality_missing,
        "chart": str(chart) if chart else None,
        "source": source,
    }


def _quality_now(trigger: dict | None, cfg: dict) -> tuple[bool | None, list]:
    """The trigger row's tier-2 verdict, graded like the gate grades it."""
    row = (trigger or {}).get("row")
    return _row_quality(row, cfg) if row else (None, [])


def assemble_context(ticker: str, cfg: dict | None = None) -> dict:
    """Everything deterministic the deep-dive skill needs for one ticker:
    the scan trigger, Yahoo Tier-A data, the quant-score anchor, the SEC
    10-Q/10-K filings, and the rendered financial-trend chart + facts file.
    IBKR Tier-B + live web research are added by Claude in-session.

    The chart and the facts file are written **here**, before this function
    returns, precisely so the model never generates either. Tier 3 is
    skill-driven, and without that the chart would be improvised and the
    figures retyped.

    A ticker the nightly scan never surfaced gets tiers 1 and 2 run for it now
    (`run_scanners.scan_ticker`) rather than arriving with an empty trigger and
    a quality verdict of "not evaluated" -- which is what an ad-hoc deep dive
    used to look like.
    """
    cfg = cfg or load_config()
    fin_cfg = cfg.get("research", {}).get("financials", {})
    t0 = time.perf_counter()
    log_step("CONTEXT", "start", f"{ticker}  run={run_id()}", cfg=cfg)

    hits = load_hits(cfg)
    scan_date = hits.get("scan_date") or date.today().isoformat()
    trigger = find_ticker(hits, ticker, cfg)   # re-graded, so it agrees with _facts

    source, on_demand = SOURCE_SIGNAL, None
    if trigger is None:
        log_step("HANDOFF", "miss", f"{ticker} not in latest_hits.json", cfg=cfg)
        source = SOURCE_ON_DEMAND
        with step("SCAN", cfg=cfg) as s:
            on_demand = run_scanners.scan_ticker(ticker, cfg)
            # The on-demand scan dates itself off its own price data, so the
            # report and its record carry the day actually analysed, not the
            # last nightly.
            scan_date = on_demand["scan_date"]
            trigger = find_ticker(on_demand, ticker, cfg)
            s.detail = (f"on-demand {scan_date}  "
                        f"{_trigger_summary(trigger, on_demand)}")
    else:
        log_step("HANDOFF", "hit", f"{ticker} {scan_date}  "
                 f"{_trigger_summary(trigger, hits)}", cfg=cfg)

    yahoo = collect_yahoo(ticker,
                          years=fin_cfg.get("years", 4),
                          quarters=fin_cfg.get("quarters", 4))
    quant = compute_quant_score(yahoo, cfg)
    log_step("QUANT", "ok", f"score {quant.get('score')}", cfg=cfg)

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

    if on_demand is not None:
        record_on_demand(on_demand, ticker, cfg)

    filings = sec.fetch_filing_sections(ticker, cfg)
    bundle = {
        "ticker": ticker,
        "scan_date": scan_date,
        "source": source,
        "trigger": trigger,
        "yahoo": yahoo,
        "quant": quant,
        "filings": filings,
        "financials_chart": str(chart) if chart else None,
        "financials_table_md": financials_table_md(yahoo.get("financials") or {}),
        "facts_path": str(facts_path),
    }
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
    """
    subdir = Path(cfg.get("research", {}).get("report_subdir", "reports"))
    out = subdir if subdir.is_absolute() else output_dir(create) / subdir
    if create:
        out.mkdir(parents=True, exist_ok=True)
    return out


def write_report(ticker: str, scan_date: str, markdown: str, cfg: dict) -> Path:
    """Write `<ticker>_<scan_date>.md` into the report folder."""
    out = report_dir(cfg) / f"{ticker}_{scan_date}.md"
    out.write_text(markdown, encoding="utf-8")
    log_step("REPORT", "ok", f"{out.name}  {fmt_bytes(len(markdown.encode()))}",
             cfg=cfg)
    print(f"Report archived: {out}")
    return out


# --------------------------------------------------------------------------
# Deliver: verdicts to Discord (reuse the existing webhook path)
# --------------------------------------------------------------------------

DISCLAIMER = "_Research analysis, not investment advice._"


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
    for band in cfg.get("research", {}).get("synthesis", {}).get("tiers", []):
        if band.get("label") == tier and band.get("color"):
            return int(str(band["color"]), 16)
    return VERDICT_COLOR


def _verdict_fields(f: dict, v: dict) -> list[dict]:
    """The six decision fields, inline so Discord lays them out 3-per-row."""
    def field(name, value):
        return {"name": name, "value": value or "n/a", "inline": True}

    adj = v.get("narrative_adj")
    quant = _num_or_na(f.get("quant_score"))
    if isinstance(adj, (int, float)):
        quant += f"  ({adj:+g} narrative)"

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

    return [field("Quant", quant), field("Trigger", trigger),
            field("Quality screen", quality), field("Price", valuation),
            field("P/E", pe), field("Next earnings", when)]


def build_verdict_embeds(verdicts: list[dict], cfg: dict
                         ) -> tuple[list[dict], list[Path]]:
    """One card per ticker: the model's judgment plus the recorded facts.

    Everything numeric comes from the ticker's `_facts.json`, so the card can
    never disagree with what the collector actually measured; the verdict dict
    supplies only `tier`, `conviction`, `narrative_adj` and `thesis`.
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


def post_summary(verdicts: list[dict], cfg: dict, send: bool = False) -> None:
    """The nightly deep-dive message: one card per ticker, chart attached.

    `send_discord_alert` batches these under Discord's 10-embed / 10-file /
    ~5500-char caps, so this scales from one verdict to five without tuning.
    """
    embeds, images = build_verdict_embeds(verdicts, cfg)
    scan_date = next((v.get("scan_date") for v in verdicts if v.get("scan_date")), "")
    header = f"**Deep-dive verdicts{f' -- {scan_date}' if scan_date else ''}** "
    header += f"({len(embeds)} report(s))\n{DISCLAIMER}"
    tickers = ", ".join(str(v.get("ticker")) for v in verdicts if v.get("ticker"))
    if send:
        send_discord_alert(header, cfg["discord"], embeds, images)
        log_step("DISCORD", "sent",
                 f"{tickers}  {len(embeds)} card(s), {len(images)} chart(s)",
                 cfg=cfg)
    else:
        log_step("DISCORD", "dry-run",
                 f"{tickers}  {len(embeds)} card(s), {len(images)} chart(s)",
                 cfg=cfg)
        print("\n--- Discord summary (dry-run, not sent) ---")
        print(header)
        for e in embeds:
            print(f"\n[{e['title']}]  color=0x{e['color']:06X}")
            print(e["description"])
            for f in e["fields"]:
                print(f"  - {f['name']}: {f['value']}")
            if "image" in e:
                print(f"  image: {e['image']['url']}")


def post_verdict(ticker: str, headline: str, short_md: str, cfg: dict,
                 send: bool = False) -> None:
    """Post (or dry-run print) a single verdict, using the same card shape."""
    post_summary([{"ticker": ticker, "tier": headline, "thesis": short_md}],
                 cfg, send=send)


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
ON_DEMAND_VERDICT_COLS = [VERDICT_COL, CONVICTION_COL, "Narrative Adj",
                          "Quant Score", "Price", "Upside %", "P/E Pctile 2y",
                          "Report", "Thesis"]


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
              CONVICTION_COL: verdict.get("conviction")}
    if not full:
        return values
    scan_date = verdict.get("scan_date") or ""
    values.update({
        "Narrative Adj": verdict.get("narrative_adj"),
        "Quant Score": facts.get("quant_score"),
        "Price": facts.get("price"),
        "Upside %": facts.get("upside_pct"),
        "P/E Pctile 2y": facts.get("pe_percentile_2y"),
        "Report": f"{verdict.get('ticker')}_{scan_date}.md",
        "Thesis": verdict.get("thesis"),
    })
    return values


def _warn_tier_drift(verdict: dict, cfg: dict) -> None:
    """Warn when a recorded tier and the config bands disagree.

    Not an error and not a correction: the bands get retuned between a run and
    its record, and the label the analysis actually reasoned under is the one
    worth keeping. But a study that groups by tier deserves to know the label
    and the bands have parted company.
    """
    tier, conviction = verdict.get("tier"), verdict.get("conviction")
    if not tier or not isinstance(conviction, (int, float)):
        return
    band = tier_for(conviction, cfg)
    if band != tier:
        log_step("VERDICT", "warn",
                 f"{verdict.get('ticker')} recorded {tier} at {conviction}, "
                 f"config bands say {band}", cfg=cfg)
        print(f"  WARNING: {verdict.get('ticker')} recorded as {tier} at "
              f"conviction {conviction}, but the current config bands put "
              f"{conviction} in {band}. Keeping {tier} as stated.",
              file=sys.stderr)


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
    _warn_tier_drift(verdict, cfg)

    facts = load_facts(ticker, scan_date, cfg)
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
# The run log's other half: what the *model* did
# --------------------------------------------------------------------------
# The Python half of a deep-dive logs itself (above). The rest -- the web
# research, the IBKR calls, the report write -- happens inside a headless
# `claude` run, and Claude Code already records every bit of it in its session
# transcript. So none of this collects anything: it reads a record that exists
# and renders one short line per tool call, then merges the two halves into the
# single timeline you actually read.
#
# Why render rather than archive: the transcript is ~1 MB per run of message
# bodies and tool output. The question a log has to answer is "which steps ran,
# when, and did they work", and that is a few dozen lines.

def transcript_path(session_id: str) -> Path | None:
    """Locate a session transcript by id.

    Globbed rather than composed from the cwd: Claude Code derives the folder
    name by substituting the project path, and a session id is a UUID -- unique
    across every project -- so a glob keeps working if that mapping ever
    changes.
    """
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return None
    return next(iter(sorted(base.glob(f"*/{session_id}.jsonl"))), None)


def _local_stamp(iso: str) -> datetime | None:
    """Transcript stamps are UTC (`...Z`); the step log is local wall-clock.

    Converting is not cosmetic -- unconverted, every model step would sort
    hours away from the Python steps it actually interleaves with.
    """
    try:
        return (datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
                .astimezone().replace(tzinfo=None))
    except (TypeError, ValueError):
        return None


def _short(text, limit: int = 90) -> str:
    # ASCII "..." rather than a real ellipsis: this line is printed to a Windows
    # console whose codepage is cp1255 here, and that is the same class of bug
    # `enable_utf8_output` exists for. A log must never be the thing that raises.
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _short_url(url: str, limit: int = 60) -> str:
    """Drop the scheme and trim the middle -- host and path tail is the useful part."""
    trimmed = re.sub(r"^https?://(www\.)?", "", str(url or ""))
    if len(trimmed) <= limit:
        return trimmed
    return f"{trimmed[:limit // 2]}...{trimmed[-(limit // 2 - 3):]}"


# Every Bash call the skill makes starts by cd-ing into the project, which is
# the same 40 characters on every line and tells you nothing.
_CD_PREFIX = re.compile(r'^cd\s+(".*?"|\S+)\s*&&\s*')


# Tool name -> the phase column it logs under. Anything unlisted falls back to
# the tool's own name, so a new tool shows up rather than silently vanishing.
TOOL_PHASE = {"Bash": "BASH", "Read": "READ", "Write": "WRITE", "Edit": "EDIT",
              "Glob": "GREP", "Grep": "GREP", "WebSearch": "SEARCH",
              "WebFetch": "FETCH", "Task": "AGENT", "Skill": "SKILL"}

# Plumbing the model does to reach its tools -- real calls, no research value.
TOOL_SKIP = {"ToolSearch", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList"}


def _tool_detail(name: str, tool_input: dict, result) -> str:
    """The short description column for one tool call."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    res = result if isinstance(result, dict) else {}
    if name == "Bash":
        return _short(_CD_PREFIX.sub("", " ".join(
            str(tool_input.get("command") or "").split())))
    if name in ("Read", "Write", "Edit"):
        path = Path(str(tool_input.get("file_path", ""))).name
        body = tool_input.get("content")
        size = f"  {fmt_bytes(len(body.encode()))}" if isinstance(body, str) else ""
        return f"{path}{size}"
    if name in ("Glob", "Grep"):
        return _short(tool_input.get("pattern"), 60)
    if name == "WebSearch":
        hits = res.get("results")
        n = sum(len(r.get("content", [])) for r in hits
                if isinstance(r, dict)) if isinstance(hits, list) else None
        return (f'"{_short(tool_input.get("query"), 55)}"'
                + (f"  {n} hits" if n else ""))
    if name == "WebFetch":
        return (f"{_short_url(tool_input.get('url'))}  "
                f"{res.get('code', '?')}  {fmt_bytes(res.get('bytes'))}")
    if name.startswith("mcp__"):
        return _short(name.rsplit("__", 1)[-1], 60)
    return _short(next((str(v) for v in tool_input.values() if v), ""), 60)


def render_session(session_id: str) -> tuple[list[str], dict]:
    """One log line per tool call in a session, plus a small tally.

    Degrades rather than fails: an unreadable or restructured transcript costs
    the model half of the timeline, never the run or the Python half.
    """
    tally = {"steps": 0, "searches": 0, "fetches": 0, "denials": 0, "errors": 0}
    path = transcript_path(session_id)
    if path is None:
        return [], tally

    calls, lines = {}, []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                when = _local_stamp(entry.get("timestamp"))
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        calls[block.get("id")] = {
                            "name": block.get("name") or "?",
                            "input": block.get("input") or {}, "at": when}
                    elif block.get("type") == "tool_result":
                        call = calls.get(block.get("tool_use_id"))
                        if call is None:
                            continue
                        call["done"] = when
                        call["error"] = bool(block.get("is_error"))
                        call["denied"] = entry.get("toolDenialKind")
                        call["result"] = entry.get("toolUseResult")
    except OSError:
        return [], tally

    for call in calls.values():
        name = call["name"]
        if name in TOOL_SKIP or call["at"] is None:
            continue
        status = ("DENIED" if call.get("denied")
                  else "error" if call.get("error") else "ok")
        ms = None
        if call.get("done"):
            ms = (call["done"] - call["at"]).total_seconds() * 1000
        detail = _tool_detail(name, call["input"], call.get("result"))
        if call.get("denied"):
            detail = f"{detail} ({call['denied']})"
        phase = TOOL_PHASE.get(name, "MCP" if name.startswith("mcp__")
                               else name.upper())
        lines.append(format_step(phase, status, detail, ms, when=call["at"]))
        tally["steps"] += 1
        tally["searches"] += name == "WebSearch"
        tally["fetches"] += name == "WebFetch"
        tally["denials"] += bool(call.get("denied"))
        tally["errors"] += bool(call.get("error"))
    return lines, tally


def _read_result(rid: str, cfg: dict) -> dict:
    """The headless run's `--output-format json` blob, or {} if it never landed.

    Missing is a real state, not an error: a run killed mid-flight (result
    3221225786 -- a PC shutdown, which the nightly chain sees) never writes it,
    and the log for that run should still be completed.
    """
    path = run_result_path(cfg, rid, create=False)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _log_field(lines: list[str], phase: str) -> list[str]:
    """Pull the detail column off every line of one phase (for the manifest).

    A line is `<date> <time>  <phase> <status> <detail>`, and the stamp holds a
    space, so the detail is the 5th field -- splitting any shallower hands back
    the status glued to the front of it.
    """
    out = []
    for line in lines:
        parts = line.split(None, 4)
        if len(parts) >= 4 and parts[2] == phase:
            out.append(parts[4] if len(parts) > 4 else "")
    return out


def complete_run_log(rid: str, session_id: str, cfg: dict,
                     mode: str = "") -> Path:
    """Merge the model's steps into the run log, close it out, record the run.

    Safe to re-run: the log is rebuilt from the Python lines already on disk
    plus a fresh render, and the manifest de-duplicates on `run_id`.
    """
    log = run_log_path(cfg, rid)
    existing = (log.read_text(encoding="utf-8").splitlines()
                if log.exists() else [])
    # Drop any END from a previous pass so re-running cannot stack them up.
    own = [ln for ln in existing if ln and " END " not in ln]
    rendered, tally = render_session(session_id)

    # Both halves are `TS_FMT`-prefixed and the model's are now local, so a
    # plain lexicographic sort is a chronological one.
    merged = sorted(own + rendered)

    result = _read_result(rid, cfg)
    denials = len(result.get("permission_denials") or []) or tally["denials"]
    duration_s = (result.get("duration_ms") or 0) / 1000
    exit_code = ("killed" if not result
                 else "error" if result.get("is_error") else "ok")
    end = format_step(
        "END", exit_code,
        f"{result.get('num_turns', '?')} turns  {duration_s:.0f}s  "
        f"${result.get('total_cost_usd', 0):.2f}  {tally['steps']} model step(s)"
        + (f"  {denials} DENIAL(S)" if denials else "")
        + (f"  {tally['errors']} tool error(s)" if tally["errors"] else "")
        + ("" if rendered else "  (no transcript found)"))
    log.write_text("\n".join(merged + [end]) + "\n", encoding="utf-8")

    reports = _log_field(merged, "REPORT")
    verdicts = _log_field(merged, "VERDICT")
    tickers = sorted({d.split()[0] for d in _log_field(merged, "CONTEXT")
                      if d and d.split()[0].isupper()})
    merge_history_csv(manifest_csv_path(cfg), [{
        "run_id": rid,
        "started": merged[0][:19] if merged else "",
        "finished": end[:19],
        "mode": mode,
        "tickers": " ".join(tickers),
        "model": result.get("modelUsage") and next(iter(result["modelUsage"]), ""),
        "session_id": session_id,
        "exit_code": exit_code,
        "turns": result.get("num_turns"),
        "duration_s": round(duration_s, 1),
        "cost_usd": result.get("total_cost_usd"),
        "web_searches": tally["searches"],
        "web_fetches": tally["fetches"],
        "denials": denials,
        "errors": tally["errors"],
        "reports": "; ".join(r.split()[0] for r in reports if r),
        "verdicts": "; ".join(_short(v, 40) for v in verdicts if v),
    }], RUN_KEYS)
    prune_run_logs(cfg)
    return log


# --------------------------------------------------------------------------
# The nightly unattended prompt
# --------------------------------------------------------------------------

AUTO_PROMPT = """/deep-dive {tickers}

Unattended nightly run for the {scan_date} scan -- nobody is watching, so do not
ask questions; follow the skill's "Unattended (nightly) mode" section.
Gate: {gate} ({n} of {total} of tonight's signals).
Post the combined Discord summary at the end with send={send}.
"""


def auto_prompt(cfg: dict) -> str | None:
    """The prompt for the nightly headless deep-dive, or None if it should not run.

    Returning None (rather than an empty prompt) is what lets `run_deepdive.bat`
    stay free of config logic: no candidates or `auto.enabled: false` simply
    exits non-zero and the batch skips the Claude invocation entirely.
    """
    auto_cfg = cfg.get("research", {}).get("auto", {})
    if not auto_cfg.get("enabled", False):
        return None
    hits = load_hits(cfg)
    if not hits:
        return None
    gate = auto_cfg.get("gate", "quality_pass")
    everything = list_candidates(hits, cfg, gate="all")
    chosen = list_candidates(hits, cfg, gate=gate,
                             limit=auto_cfg.get("max_reports"))
    if not chosen:
        return None
    return AUTO_PROMPT.format(
        tickers=" ".join(dict.fromkeys(c["ticker"] for c in chosen)),
        scan_date=hits.get("scan_date", "latest"),
        gate=gate, n=len(chosen), total=len(everything),
        send=str(bool(auto_cfg.get("discord_send", False))).lower())


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

USAGE = """usage:
  python research_report.py candidates [--all] [--json]   who to deep-dive tonight
  python research_report.py auto-prompt                   the nightly prompt (exit 1 if none)
  python research_report.py auto-model                    the model the nightly run should use
  python research_report.py scan TICKER [TICKER ...]      on-demand tiers 1 + 2 for a ticker
  python research_report.py post-verdicts F.json [--send] deliver the batch's verdict cards
  python research_report.py context TICKER [TICKER ...]   the data bundle for one ticker
  python research_report.py run-id                        mint "<run_id> <session_uuid>"
  python research_report.py log-session ID UUID [--mode M]  merge the model's steps into the run log"""


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
    fund_cfg = cfg["fundamentals"]
    for field in fundamentals_fields(trigger["row"], fund_cfg):
        print(f"          {field['name']}: {field['value']}")
    quality, failed = trigger["quality"], trigger["quality_missing"]
    if quality is None:
        print("  tier 2  not evaluated (quality layer off)")
    elif quality:
        badge = fund_cfg.get("quality", {}).get("badge", "")
        print(f"  tier 2  PASS {badge}".rstrip())
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

    if args[0] == "auto-prompt":
        prompt = auto_prompt(load_config())
        if prompt is None:
            return 1
        print(prompt)
        return 0

    # A subcommand rather than an inline `python -c` in the batch file: cmd's
    # `for /f` mangles a quoted interpreter path inside backticks, so the model
    # is handed over through a file instead.
    if args[0] == "auto-model":
        auto_cfg = load_config().get("research", {}).get("auto", {})
        print(auto_cfg.get("model", "opus"))
        return 0

    # Everything the .bat needs to start a run, on one line for `for /f` to
    # split: the run id, the session UUID, and where to put the result JSON.
    # The session UUID is passed to `claude --session-id`, which is what makes
    # the transcript findable *before* the run starts -- so even a run killed
    # mid-flight can have its log completed afterwards. The path is resolved
    # here rather than hardcoded in cmd so `research.logging.dir` stays a real
    # setting: hardcode it there and moving the directory silently orphans
    # every result file from the run log it belongs to.
    # `run_id()`, not `new_run_id()`: run_scanner.bat mints and exports the id
    # before chaining here, so the whole nightly chain -- tiers 1, 2 and 3 --
    # lands in one log for the night. Standalone (env unset) this still mints
    # its own, so an ad-hoc deep dive gets its own file.
    if args[0] == "run-id":
        rid = run_id()
        print(f"{rid} {uuid.uuid4()} {run_result_path(load_config(), rid)}")
        return 0

    # Closes out a run: merges the model's steps into the run log, writes the
    # END line, appends the manifest row. Separate from the run itself so it
    # can be re-run by hand over a run that died before reaching it.
    if args[0] == "log-session":
        if len(args) < 3:
            print("usage: log-session <run_id> <session_id> [--mode M]",
                  file=sys.stderr)
            return 1
        cfg = load_config()
        mode = args[args.index("--mode") + 1] if "--mode" in args else ""
        log = complete_run_log(args[1], args[2], cfg, mode)
        print(f"Run log: {log}")
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

    # A first-class subcommand rather than leaving the skill to reach for
    # `python -c`: the unattended run's allow-list is deliberately narrow, and
    # Claude Code requires *every* segment of a compound command to be allowed,
    # so an inline one-liner (or one with a `; echo` tail) gets refused. The
    # 2026-07-26 shakedown wrote a full report and then silently failed to post
    # the verdict for exactly that reason.
    if args[0] == "post-verdicts":
        if len(args) < 2:
            print("usage: post-verdicts <verdicts.json> [--send]", file=sys.stderr)
            return 1
        cfg = load_config()
        verdicts = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        if isinstance(verdicts, dict):
            verdicts = [verdicts]
        authorized = cfg.get("research", {}).get("auto", {}).get("discord_send", False)
        send = "--send" in args and authorized
        if "--send" in args and not authorized:
            print("research.auto.discord_send is false -- printing instead of sending.",
                  file=sys.stderr)
        post_summary(verdicts, cfg, send=send)
        record_verdicts(verdicts, cfg)
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
