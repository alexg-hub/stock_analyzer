"""
Deep-dive pipeline -- Stage 2 driver (plumbing; synthesis deferred).

The nightly scan (run_scanners.py) writes `latest_hits.json`. On demand, an
interactive Claude session drives the deep-dive:

    read latest_hits.json
      -> per ticker: collect_yahoo() [Tier A]  +  IBKR MCP [Tier B, Claude-only]
      -> synthesize verdict + evidence + short/full report   <-- DEFERRED
      -> post the verdict to Discord (reuse send_discord_alert)
      -> archive the full report into the Google Drive sync folder

This module is the mechanical toolkit around that flow. Two parts of the flow
are NOT in Python by nature and are supplied by Claude in-session:
  * the IBKR Tier-B data (the MCP connector is reachable only from the Claude
    session, never this script), and
  * `synthesize()` -- the actual verdict/report writing, a separate design step.

`synthesize()` therefore raises NotImplementedError for now; `build_stub_report`
provides a clearly-labelled placeholder so the plumbing (collect -> Drive ->
Discord) is testable end-to-end today.

Usage (dry-run prints, nothing sent, nothing written unless flagged):
    python research_report.py MSFT
    python research_report.py MSFT --archive        # also write report to Drive
    python research_report.py MSFT --archive --send # also POST verdict to Discord
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from scanner_common import load_config, resolve_drive_dir, send_discord_alert
from research_collect import collect_yahoo

VERDICT_COLOR = 0x2A78D6  # matches the charts' "close" blue


# --------------------------------------------------------------------------
# Hand-off: read what the nightly scan produced
# --------------------------------------------------------------------------

def load_hits(cfg: dict) -> dict:
    """Load latest_hits.json (the Stage-1 hand-off). Empty dict if absent."""
    path = Path(cfg.get("research", {}).get("latest_hits_path", "latest_hits.json"))
    if not path.is_absolute():
        path = Path(__file__).with_name(str(path))
    if not path.exists():
        print(f"(no hand-off file at {path} -- run run_scanners.py first)")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def find_ticker(hits: dict, ticker: str) -> dict | None:
    """Locate `ticker` in the hand-off; return {screen, kind, row} or None."""
    for screen in hits.get("screens", []):
        for kind in ("hits", "near"):
            if ticker in screen.get(kind, {}):
                return {"screen": screen["title"], "config_key": screen["config_key"],
                        "kind": "hit" if kind == "hits" else "near-miss",
                        "strategy": screen.get("strategy", {}),
                        "row": screen[kind][ticker]}
    return None


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
# Deliver: the short verdict to Discord (reuse the existing webhook path)
# --------------------------------------------------------------------------

def post_verdict(ticker: str, headline: str, short_md: str, cfg: dict,
                 send: bool = False) -> None:
    """Post (or dry-run print) the short verdict as a Discord embed."""
    embed = {"title": f"{ticker} -- {headline}", "description": short_md[:4000],
             "color": VERDICT_COLOR}
    content = f"**Deep-dive verdict: {ticker}**"
    if send:
        send_discord_alert(content, cfg["discord"], [embed])
    else:
        print("\n--- Discord verdict (dry-run, not sent) ---")
        print(content)
        print(f"[{embed['title']}]\n{embed['description']}")


# --------------------------------------------------------------------------
# Synthesis -- the deferred design step
# --------------------------------------------------------------------------

def synthesize(ticker: str, yahoo: dict, ibkr: dict | None, context: dict | None) -> dict:
    """Turn collected data into {headline, short_md, full_md, verdict, score}.

    NOT YET DESIGNED. This is the pluggable middle of the flow; until it is
    designed, callers use `build_stub_report` for the plumbing dry-run.
    """
    raise NotImplementedError(
        "synthesis layer is the next design step -- see the plan file")


def build_stub_report(ticker: str, yahoo: dict, context: dict | None) -> tuple[str, str]:
    """A clearly-labelled placeholder (Yahoo-only) so the pipeline is testable
    before `synthesize()` exists. Returns (short_md, full_md)."""
    prof = yahoo.get("profile", {}) or {}
    val = yahoo.get("valuation", {}) or {}
    company = prof.get("company") or ticker
    ctx = ""
    if context:
        ctx = f" ({context['kind']} on {context['screen']})"

    short_md = (
        f"**{company}**{ctx}\n"
        f"P/E {val.get('trailingPE')}, fwd P/E {val.get('forwardPE')}, "
        f"P/E 2y-pct {val.get('pe_percentile_2y')}\n"
        f"_STUB verdict -- synthesis layer not yet implemented._")

    full_md = (
        f"# {ticker} - {company} - deep-dive (STUB)\n\n"
        f"> Placeholder report. The synthesis layer is not yet designed, and the\n"
        f"> IBKR Tier-B graph (connections/themes/positioning) is added by Claude\n"
        f"> in-session, not by this script. This file exists only to validate the\n"
        f"> collect -> Drive -> Discord plumbing.\n\n"
        f"## Scan context\n\n{json.dumps(context, indent=2) if context else 'n/a'}\n\n"
        f"## Yahoo (Tier A) collected data\n\n"
        f"```json\n{json.dumps(yahoo, indent=2, ensure_ascii=False)}\n```\n")
    return short_md, full_md


# --------------------------------------------------------------------------
# CLI -- dry-run driver for the plumbing
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--archive", action="store_true",
                        help="write the (stub) full report into the Drive folder")
    parser.add_argument("--send", action="store_true",
                        help="POST the verdict to the live Discord webhook")
    args = parser.parse_args()
    ticker = args.ticker.upper()

    cfg = load_config()
    hits = load_hits(cfg)
    context = find_ticker(hits, ticker)
    scan_date = hits.get("scan_date") or date.today().isoformat()
    if context:
        print(f"{ticker}: {context['kind']} on '{context['screen']}' (scan {scan_date})")
    else:
        print(f"{ticker}: not in the latest hand-off -- running ad-hoc (scan {scan_date})")

    print("Collecting Yahoo (Tier A)...")
    yahoo = collect_yahoo(ticker)

    short_md, full_md = build_stub_report(ticker, yahoo, context)

    if args.archive:
        write_report_to_drive(ticker, scan_date, full_md, cfg)
    else:
        print("(skipping Drive archive -- pass --archive to write it)")

    post_verdict(ticker, "deep-dive (stub)", short_md, cfg, send=args.send)
    return 0


if __name__ == "__main__":
    sys.exit(main())
