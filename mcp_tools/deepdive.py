"""Tier 3: the deep-dive's deterministic half.

The synthesis itself is the `deep-dive` skill -- Claude reasoning, not a
function -- so what lives here is only what feeds it and what records its
verdict.
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

    @mcp.tool()
    def deepdive_complete_log(run_id: str, session_id: str,
                              mode: str = "") -> dict:
        """Finish the step log of a deep-dive run that died before logging out.

        Merges the model's half of the run (from Claude Code's own session
        transcript) into `output/logs/<run_id>.log` and records the run in the
        manifest. Safe to re-run -- the log is rebuilt and the manifest
        de-duplicates on run_id. Both ids are in the 'Deep-dive started' banner
        in `output/deepdive_log.txt`; a run killed mid-flight (Task Scheduler
        result 3221225786) is exactly what this recovers.
        """
        import research_report
        path = research_report.complete_run_log(run_id, session_id,
                                                load_config(), mode)
        return {"run_id": run_id, "log": str(path)}

    @mcp.tool()
    def deepdive_post_verdicts(verdicts: list[dict], send: bool = False) -> dict:
        """Record deep-dive verdicts, and optionally post the Discord cards.

        Each verdict is {ticker, scan_date, tier, conviction, narrative_adj,
        thesis}. Recording always happens -- that is the point; `send` only
        controls the Discord message and is double-gated by
        `research.auto.discord_send` in config, exactly as the CLI is.

        The verdict lands in signals.csv or on_demand_scans_results.csv by
        provenance, decided by the `source` in the ticker's facts JSON.
        """
        import research_report
        cfg = load_config()
        allowed = cfg.get("research", {}).get("auto", {}).get("discord_send", False)
        effective = bool(send and allowed)
        research_report.post_summary(verdicts, cfg, send=effective)
        research_report.record_verdicts(verdicts, cfg)
        return {
            "recorded": [v.get("ticker") for v in verdicts],
            "sent": effective,
            "note": ("" if effective or not send else
                     "research.auto.discord_send is false -- printed, not sent"),
        }
