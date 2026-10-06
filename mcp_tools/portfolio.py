"""Tier 4: the virtual portfolio."""

import math

from scanner_common import load_config

from . import jobs


def _clean(value):
    """CSV round-trips leave NaN floats around; JSON has no way to say NaN."""
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def status_impl() -> dict:
    from portfolio_sim import analysis

    status = analysis.book_status(load_config())
    if not status["positions"]:
        status["note"] = "ledger is empty -- run portfolio_open first"
    return status

def positions_impl(status: str | None = None, ticker: str | None = None,
                   limit: int = 100) -> dict:
    """The ledger as records, projected down from its 60+ columns.

    Kept as a tool -- unlike the report/log readers, which Read and Glob do
    better -- precisely because the raw CSV is too wide to read usefully.
    """
    from portfolio_sim import ledger

    cfg = load_config()
    frame = ledger.load_positions(cfg)
    if frame.empty:
        return {"positions": [], "total": 0,
                "note": "ledger is empty -- run portfolio_open first"}

    if status:
        frame = frame[frame["status"].astype(str) == status]
    if ticker:
        frame = frame[frame["ticker"].astype(str) == ticker.upper()]

    horizons = ledger.horizons_of(cfg.get("portfolio", {}))
    columns = ["position_id", "scan_date", "ticker", "config_key", "source",
               "status", "entry_date", "entry_price", "last_close", "days_held"]
    columns += [f"ret_{h}d_%" for h in horizons]
    columns += [c for c in (ledger.EXIT_FLAG_COL, "dt_status", "dt_ret_%")
                if c in frame.columns]
    present = [c for c in columns if c in frame.columns]

    total = len(frame)
    rows = [{k: _clean(v) for k, v in row.items()}
            for row in frame[present].head(limit).to_dict("records")]
    return {"positions": rows, "total": total, "returned": len(rows),
            "columns_omitted": sorted(set(frame.columns) - set(present))}


def register(mcp) -> None:
    @mcp.tool()
    def portfolio_status() -> dict:
        """What is on the virtual book: counts, date range, settled returns.

        Also reports whether enough returns have accumulated for attribution
        (`analyze`) to make claims yet. No network, no Discord.
        """
        return status_impl()

    @mcp.tool()
    def portfolio_positions(status: str | None = None, ticker: str | None = None,
                            limit: int = 100) -> dict:
        """The virtual positions, filtered and projected to the useful columns.

        `status` is typically 'open' or 'pending'. Names the columns it dropped
        so you can Read `output/portfolio/positions.csv` if you need them.
        """
        return positions_impl(status, ticker, limit)

    @mcp.tool()
    def portfolio_open() -> dict:
        """Turn recorded signals into virtual positions.

        Idempotent and offline: a new signal becomes a `pending` row with no
        entry price, because the nightly scan fires after the close and the
        next open is most of a day away. `portfolio_mark` fills it in later.
        """
        from portfolio_sim import ledger
        cfg = load_config()
        frame = ledger.sync(cfg)
        return {"positions": len(frame)}

    @mcp.tool()
    def portfolio_mark() -> dict:
        """Re-sync the ledger, fill pending entries, mark every horizon.

        Downloads current prices for the held tickers, so it returns a job_id.
        Re-syncs first, which is what lets a tier-3 verdict written hours after
        the signal row land on the position it belongs to. Never touches
        Discord. Fails soft by design: a broken ledger must not take down
        anything else.
        """
        def _mark():
            from portfolio_sim import ledger, marking
            cfg = load_config()
            ledger.sync(cfg)
            frame = marking.mark(cfg)
            counts = (frame["status"].astype(str).value_counts().to_dict()
                      if "status" in frame else {})
            return {"positions": len(frame),
                    "by_status": {str(k): int(v) for k, v in counts.items()}}

        # In-process: progress is the server's step log (MARK lines).
        return jobs.start("portfolio_mark", _mark,
                          log_path=jobs.server_step_log())

    @mcp.tool()
    def portfolio_analyze(baseline: bool = True) -> dict:
        """Grade which recorded attribute actually predicted the return.

        This is the only thing in the project that ever checks whether tiers
        1-3 were right. Returns the generated conclusion sentences and the
        findings path. Read the CAVEAT rows first, and trust `q_value`
        (Benjamini-Hochberg) over raw p -- it runs dozens of tests on a thin
        sample. Rows with `sufficient_n=False` are still written and say so.

        `baseline=False` skips the cached-panel random-entry comparison.
        """
        def _analyze():
            from portfolio_sim import analysis
            from scanner_common import findings_csv_path
            cfg = load_config()
            frame = analysis.analyze(cfg, baseline=baseline)
            headlines = ([str(x) for x in frame["conclusion"].tolist()[:12]]
                         if "conclusion" in frame else [])
            return {"findings": len(frame),
                    "path": str(findings_csv_path(cfg)),
                    "headlines": headlines}

        # In-process: progress is the server's step log (ANALYZE lines).
        return jobs.start("portfolio_analyze", _analyze,
                          log_path=jobs.server_step_log())

    @mcp.tool()
    def portfolio_exit_scan(send: bool = False) -> dict:
        """Scan the book for double tops, flag them, record the sells.

        The only tier-4 command that can reach Discord, hence `send=False` by
        default. It *flags* rather than closes: `status` and every `ret_*d_%`
        keep running, so the fixed horizon and the signal exit stay two
        measurements of the same position -- which is the only way to ask later
        which one you should have taken.

        Idempotent: a position already carrying a double-top flag is skipped,
        so a re-run adds no second sell and re-alerts nothing.
        """
        from portfolio_sim import exits
        cfg = load_config()
        recorded = exits.exit_scan(cfg, send=send)
        return {"sells_recorded": len(recorded),
                "sent": bool(send),
                "rows": recorded.to_dict("records") if len(recorded) else []}
