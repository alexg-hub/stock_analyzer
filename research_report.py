"""
Deep-dive pipeline -- Stage 2 toolkit.

Synthesis is a *skill-driven procedure* (`.claude/skills/deep-dive`), not one
Python function: Claude reasons in-session over the collected data + IBKR Tier-B
(interactive MCP) + live web research to write the investment case and set the
verdict. This module provides the deterministic pieces around that reasoning:

  * list_candidates(hits, cfg) -> tonight's deep-dive candidates, gated by tier 2
  * assemble_context(ticker) -> the data bundle Claude reasons over
      (trigger + Yahoo Tier-A + deterministic quant score + SEC 10-Q/10-K filings)
  * compute_quant_score(yahoo, cfg) -> the config-driven 0-100 anchor
  * report_dir / write_report -> archive the full report under output/
  * post_summary / post_verdict -> deliver to Discord (reuse send_discord_alert)

The verdict = tier + conviction: conviction = clamp(quant + narrative_adj, 0,
100), narrative_adj (bounded by config) is Claude's qualitative adjustment; the
tier comes from config conviction bands.

CLI:
    python research_report.py candidates [--all] [--json]   # who to deep-dive
    python research_report.py auto-prompt                   # the nightly prompt
    python research_report.py context MSFT [JNJ ...]        # the data bundle
"""

import json
import math
import sys
from datetime import date
from pathlib import Path

import sec
from research_collect import collect_yahoo
from scanner_common import (
    COMPANY_COL,
    QUALITY_COL,
    QUALITY_MISSING_COL,
    load_config,
    output_dir,
    quality_enabled,
    quality_failures,
    send_discord_alert,
)

VERDICT_COLOR = 0x2A78D6  # matches the charts' "close" blue


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


def find_ticker(hits: dict, ticker: str) -> dict | None:
    """Locate `ticker` in the hand-off; return {screen, kind, ...} or None.

    `kind` is the row's tier-1 setup tier (`full` or `partial`) -- every screen
    reports one signal list, with partial setups tagged rather than split out.
    `quality` / `quality_missing` are the tier-2 verdict, `None` when the scan
    did not evaluate it.

    A ticker that fired on more than one screen resolves to the first; use
    `list_candidates` when you need every (screen, ticker) pair.
    """
    for screen in hits.get("screens", []):
        row = screen.get("hits", {}).get(ticker)
        if row is not None:
            return {"screen": screen["title"], "config_key": screen["config_key"],
                    "kind": row.get("Setup", "full"),
                    "quality": row.get(QUALITY_COL),
                    "quality_missing": row.get(QUALITY_MISSING_COL),
                    "strategy": screen.get("strategy", {}),
                    "row": row}
    return None


# --------------------------------------------------------------------------
# Tier 2 as a gate: which of tonight's hits are worth a deep dive
# --------------------------------------------------------------------------

GATES = ("all", "quality_pass")


def _row_quality(row: dict, cfg: dict) -> tuple[bool | None, list]:
    """The row's tier-2 verdict, recomputed if the scan predates it.

    Hand-offs written before `annotate_quality` existed carry the raw
    fundamentals but no verdict. `quality_failures` works unchanged on a
    JSON-round-tripped row -- it looks values up by display label and indexes
    the multi-year series positionally, so nested arrays behave exactly like
    the original tuples -- which keeps old archives readable.
    """
    if QUALITY_COL in row:
        return bool(row[QUALITY_COL]), list(row.get(QUALITY_MISSING_COL) or [])
    fund_cfg = cfg.get("fundamentals", {})
    if not quality_enabled(fund_cfg):
        return None, []
    failed = quality_failures(row, fund_cfg)
    return not failed, failed


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

def assemble_context(ticker: str, cfg: dict | None = None) -> dict:
    """Everything deterministic the deep-dive skill needs for one ticker:
    the scan trigger, Yahoo Tier-A data, the quant-score anchor, and the SEC
    10-Q/10-K filings. IBKR Tier-B + live web research are added by Claude in-session.
    """
    cfg = cfg or load_config()
    hits = load_hits(cfg)
    yahoo = collect_yahoo(ticker)
    return {
        "ticker": ticker,
        "scan_date": hits.get("scan_date") or date.today().isoformat(),
        "trigger": find_ticker(hits, ticker),
        "yahoo": yahoo,
        "quant": compute_quant_score(yahoo, cfg),
        "filings": sec.fetch_filing_sections(ticker, cfg),
    }


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
    print(f"Report archived: {out}")
    return out


# --------------------------------------------------------------------------
# Deliver: verdicts to Discord (reuse the existing webhook path)
# --------------------------------------------------------------------------

def _verdict_line(v: dict) -> str:
    comp = f" ({v['company']})" if v.get("company") else ""
    return (f"**{v.get('tier', '?')} {v.get('conviction', '?')}** · "
            f"{v['ticker']}{comp} — {v.get('thesis', '')} _[{v.get('screen', '')}]_")


def post_summary(verdicts: list[dict], cfg: dict, send: bool = False) -> None:
    """One combined Discord message: a ranked verdict line per ticker."""
    vs = sorted(verdicts, key=lambda v: -(v.get("conviction") or 0))
    desc = "\n".join(_verdict_line(v) for v in vs) or "No verdicts."
    desc += f"\n\nFull reports: {report_dir(cfg, create=False)}"
    embed = {"title": "Deep-dive verdicts", "description": desc[:4000], "color": VERDICT_COLOR}
    content = "**Nightly deep-dive summary**"
    if send:
        send_discord_alert(content, cfg["discord"], [embed])
    else:
        print("\n--- Discord summary (dry-run, not sent) ---")
        print(content + "\n" + desc)


def post_verdict(ticker: str, headline: str, short_md: str, cfg: dict,
                 send: bool = False) -> None:
    """Post (or dry-run print) a single-ticker verdict embed."""
    embed = {"title": f"{ticker} -- {headline}", "description": short_md[:4000],
             "color": VERDICT_COLOR}
    content = f"**Deep-dive verdict: {ticker}**"
    if send:
        send_discord_alert(content, cfg["discord"], [embed])
    else:
        print("\n--- Discord verdict (dry-run, not sent) ---")
        print(f"{content}\n[{embed['title']}]\n{embed['description']}")


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
  python research_report.py context TICKER [TICKER ...]   the data bundle for one ticker"""


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

    if len(args) >= 2 and args[0] == "context":
        for t in args[1:]:
            print(json.dumps(assemble_context(t.upper()), indent=2,
                             default=str, ensure_ascii=False))
        return 0

    print(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
