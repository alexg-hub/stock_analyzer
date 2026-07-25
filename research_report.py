"""
Deep-dive pipeline -- Stage 2 toolkit.

Synthesis is a *skill-driven procedure* (`.claude/skills/deep-dive`), not one
Python function: Claude reasons in-session over the collected data + IBKR Tier-B
(interactive MCP) + live web research to write the investment case and set the
verdict. This module provides the deterministic pieces around that reasoning:

  * assemble_context(ticker) -> the data bundle Claude reasons over
      (trigger + Yahoo Tier-A + deterministic quant score + SEC 10-Q/10-K filings)
  * compute_quant_score(yahoo, cfg) -> the config-driven 0-100 anchor
  * report_dir / write_report_to_drive -> archive the full report to Google Drive
  * post_summary / post_verdict -> deliver to Discord (reuse send_discord_alert)

The verdict = tier + conviction: conviction = clamp(quant + narrative_adj, 0,
100), narrative_adj (bounded by config) is Claude's qualitative adjustment; the
tier comes from config conviction bands.

CLI (data bundle for the skill / debugging):
    python research_report.py context MSFT [JNJ ...]
"""

import json
import math
import sys
from datetime import date
from pathlib import Path

import sec
from research_collect import collect_yahoo
from scanner_common import (
    load_config,
    output_dir,
    resolve_drive_dir,
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
        print(f"(no hand-off file at {path} -- run run_scanners.py first)")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def find_ticker(hits: dict, ticker: str) -> dict | None:
    """Locate `ticker` in the hand-off; return {screen, kind, row} or None.

    `kind` is the row's setup tier (`full` or `partial`) -- every screen now
    reports one signal list, with partial setups tagged rather than split out.
    """
    for screen in hits.get("screens", []):
        row = screen.get("hits", {}).get(ticker)
        if row is not None:
            return {"screen": screen["title"], "config_key": screen["config_key"],
                    "kind": row.get("Setup", "full"),
                    "strategy": screen.get("strategy", {}),
                    "row": row}
    return None


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
# Archive: the Google Drive sync folder
# --------------------------------------------------------------------------

def report_dir(cfg: dict, create: bool = True) -> Path:
    """Resolve <Drive sync folder>/<report_subdir>, creating it if asked."""
    research = cfg.get("research", {})
    drive = resolve_drive_dir(research.get("drive_report_dir", ""))
    if drive is None:
        raise FileNotFoundError(
            "Google Drive sync folder not found via "
            f"{research.get('drive_report_dir')!r} -- is Drive for Desktop running?")
    out = drive / research.get("report_subdir", "stock_reports")
    if create:
        out.mkdir(parents=True, exist_ok=True)
    return out


def write_report_to_drive(ticker: str, scan_date: str, markdown: str, cfg: dict) -> Path:
    """Write `<ticker>_<scan_date>.md` into the Drive report folder."""
    out = report_dir(cfg) / f"{ticker}_{scan_date}.md"
    out.write_text(markdown, encoding="utf-8")
    print(f"Report archived to Drive: {out}")
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
    desc += "\n\nFull reports: Google Drive / stock_reports"
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
# CLI -- dump the context bundle for the skill / debugging
# --------------------------------------------------------------------------

def main() -> int:
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "context":
        for t in args[1:]:
            print(json.dumps(assemble_context(t.upper()), indent=2,
                             default=str, ensure_ascii=False))
        return 0
    print("usage: python research_report.py context TICKER [TICKER ...]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
