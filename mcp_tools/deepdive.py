"""Tier 3: the graded verdict, for a session to read.

The verdict itself is computed inside the nightly scan and recorded there;
nothing here decides anything. These two tools exist so a session can see
*who* was worth researching and *what the analyzer already concluded* before
adding the qualitative half -- which is the `enrich` skill, and which records
to its own table rather than to a verdict. See AI_ROLE.md.
"""

from scanner_common import load_config

from . import jobs


def candidates_impl(gate: str | None = None, limit: int | None = None) -> dict:
    import research_report
    cfg = load_config()
    hits = research_report.load_hits(cfg)
    if not hits:
        return {"candidates": [], "gate": gate,
                "note": "no hand-off file yet -- run the nightly scan first"}
    effective = gate or cfg.get("research", {}).get("auto", {}).get("gate",
                                                                   "quality_pass")
    rows = research_report.list_candidates(hits, cfg, gate=gate, limit=limit)
    everything = research_report.list_candidates(hits, cfg, gate="all")
    return {
        "scan_date": hits.get("scan_date"),
        "gate": effective,
        "candidates": rows,
        "held_back_by_gate": len(everything) - len(rows) if effective != "all" else 0,
        "note": ("The gate is a fundamentals filter, not an override -- a ticker "
                 "you name explicitly is always worth analysing. Pass gate='all' "
                 "to see every tier-1 hit."),
    }


def register(mcp) -> None:
    @mcp.tool()
    def deepdive_candidates(gate: str | None = None,
                            limit: int | None = None) -> dict:
        """Who is worth a deep dive, per the tier-2 quality gate.

        `gate` is 'quality_pass' or 'all'; omitted, it follows
        `research.auto.gate` in config. Ranked quality-pass first, then full
        setups before partial ones. Reads the hand-off only -- no network.
        """
        return candidates_impl(gate, limit)

    @mcp.tool()
    def deepdive_context(ticker: str) -> dict:
        """Assemble the deterministic research bundle for one ticker.

        This is tier 3's anchor: the trigger row, Yahoo fundamentals and
        financial history, a config-driven 0-100 quant score, SEC 10-K/10-Q
        sections, plus a rendered financials chart and a facts JSON already
        written to disk. Reason *on top of* the quant score; never recompute it,
        and never redraw the chart -- read the paths it returns.

        A ticker absent from the nightly hand-off is scanned on demand, so an
        ad-hoc name arrives with a real trigger rather than 'not evaluated'.
        Hits Yahoo and SEC (tens of seconds), so it returns a job_id. No Discord.
        """
        import research_report
        # Runs in-process, so its progress is the server's own step log
        # (CONTEXT/QUANT/SEC/FACTS lines) rather than a job file.
        return jobs.start("deepdive_context", research_report.assemble_context,
                          ticker.upper(), log_path=jobs.server_step_log())
