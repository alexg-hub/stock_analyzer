"""Tier 1 + 2: what the scan found, and scanning a named ticker on demand."""

from scanner_common import load_config


def _hits_payload() -> dict:
    import research_report
    return research_report.load_hits(load_config())


def scan_status_impl() -> dict:
    """Summarise `latest_hits.json` without dumping every row."""
    payload = _hits_payload()
    if not payload:
        return {"scan_date": None, "screens": [], "total": 0,
                "note": "no hand-off file yet -- run the nightly scan first"}
    screens = []
    total = 0
    for screen in payload.get("screens", []):
        hits = screen.get("hits", {}) or {}
        setups = [row.get("Setup", "full") for row in hits.values()]
        screens.append({
            "config_key": screen.get("config_key"),
            "title": screen.get("title"),
            "signals": len(hits),
            "full": sum(1 for s in setups if s == "full"),
            "partial": sum(1 for s in setups if s == "partial"),
            "tickers": sorted(hits),
        })
        total += len(hits)
    return {"scan_date": payload.get("scan_date"), "total": total, "screens": screens}


def register(mcp) -> None:
    @mcp.tool()
    def scan_status() -> dict:
        """Summarise the latest nightly scan: date, per-screen counts, tickers.

        Reads the `output/latest_hits.json` hand-off only -- no network, no
        Discord. Use `latest_hits.json` itself via Read for the full rows.
        """
        return scan_status_impl()

    @mcp.tool()
    def scan_tickers(tickers: list[str]) -> dict:
        """Run tiers 1 and 2 for named tickers, whether or not they signalled.

        This is the on-demand path: each ticker is downloaded and screened, and
        tier 2 is graded even when no screen fires (`Setup: "none"`), because
        the quality check is usually the point of asking. Touches the network;
        never touches Discord. Records a row in the on-demand history CSV.
        """
        import run_scanners
        cfg = load_config()
        out = {}
        for raw in tickers:
            ticker = str(raw).strip().upper()
            if not ticker:
                continue
            out[ticker] = run_scanners.scan_ticker(ticker, cfg)
        return {"scanned": sorted(out), "payloads": out}

    @mcp.tool()
    def run_nightly_scan(send: bool = False) -> dict:
        """Run the full nightly scan over the alerting universe (tiers 1 and 2).

        Minutes of Yahoo traffic, so it returns a job_id. **With `send=True`
        this posts to the user's real Discord channel** -- the default prints
        the cards instead.

        Runs as a subprocess of `run_scanners.py`, so the path is byte-identical
        to what Task Scheduler runs nightly. It writes `latest_hits.json` and
        archives the signals either way; only the Discord post is suppressed.
        """
        from .backtests import _script_job
        args = ["run_scanners.py"] + ([] if send else ["--no-send"])
        return _script_job("nightly_scan", args)
