"""The universe risk/reward plane, and the one door to AI risk research.

Two read-only tools over `universe_scan`'s cache plus a job to refill it, and
`risk_research` -- which despite the name runs **no** model. It assembles
everything deterministic about one ticker and hands it back for the calling
session to reason over.

That shape is deliberate. The repo already carries two `claude -p` invocation
paths (`run_deepdive.bat`, `run_ondemand.bat`) whose IBKR allow-lists have to
stay byte-identical, and under `--permission-mode dontAsk` an un-allowed tool is
refused *silently* -- so a third enumerated list would be a third way to lose a
report section with no error to explain it. A tool that returns a bundle needs no
subprocess, no allow-list and no new silent-failure mode: the session asking the
question already has the tools to answer it.
"""

from scanner_common import load_config

from . import backtests


def _table(cfg: dict, tickers: list[str] | None = None):
    """The risk/reward table straight from the cache. No network, ever."""
    import pandas as pd

    import universe_scan
    cache = universe_scan.load_cache(cfg)
    if not cache:
        return None, pd.DataFrame()
    wanted = [t.upper() for t in tickers] if tickers else list(cache)
    view = {t: cache[t] for t in wanted if t in cache}
    sectors = pd.DataFrame({"ticker": list(view),
                            "sector": "", "sub_industry": ""})
    return cache, universe_scan.build_table(cfg, view, sectors)


def quadrant_impl(quadrant: str | None = None, sector: str | None = None,
                  limit: int = 50, vetoed_only: bool = False) -> dict:
    import quality
    import universe_scan
    cfg = load_config()
    cache, table = _table(cfg)
    if table.empty:
        return {"rows": [], "note": ("no universe cache yet -- run universe_scan "
                                     "first (it takes ~15 minutes)")}

    # Sector lives in the CSV, not the cache, so re-read the latest table when a
    # sector filter is asked for rather than silently ignoring the argument.
    if sector:
        latest = sorted(universe_scan.universe_dir(cfg).glob(
            f"{universe_scan.section(cfg).get('table_csv', 'risk_reward')}_*.csv"))
        if latest:
            import pandas as pd
            table = pd.read_csv(latest[-1])
        table = table[table["sector"].astype(str).str.lower()
                      == sector.lower()]
    if quadrant:
        table = table[table["quadrant"] == quadrant.lower()]
    if vetoed_only:
        table = table[table["vetoed"].fillna(False).astype(bool)]

    counts = (table["quadrant"].value_counts().to_dict()
              if "quadrant" in table.columns else {})
    stage = (table["stage"].dropna().iloc[0]
             if "stage" in table.columns and table["stage"].notna().any()
             else None)
    return {
        "rows": table.head(limit).where(table.notna(), None).to_dict("records"),
        "matched": len(table),
        "counts": counts,
        "stage": stage,
        "thresholds": {
            "reward": quality.quadrant_thresholds(cfg)[0],
            "risk": quality.quadrant_thresholds(cfg)[1],
        },
        "note": ("Thresholds are display-only -- they label quadrants and change "
                 "no recorded score. A `fast` stage means the SEC filing flags "
                 "are absent from the risk axis, so it is not comparable to a "
                 "deep-stage reading."),
    }


def risk_research_impl(ticker: str, peers: int = 6) -> dict:
    """Everything deterministic about one ticker, plus what to do with it.

    Deliberately assembles rather than concludes. `risk_report` already pairs
    every rule with the threshold it was compared against and splits them into
    tripped / clean / unknown; this adds the sector peer group from the universe
    cache, so "ROIC 14%" can be read against the sector rather than an absolute
    anchor, and states plainly which questions the numbers cannot answer.
    """
    import pandas as pd

    import research_report
    import universe_scan
    cfg = load_config()
    tick = ticker.upper()

    report = research_report.risk_report(tick, cfg)

    # Peer context: same sector, from the cache, nearest on reward.
    peer_rows: list[dict] = []
    sector = ""
    stem = universe_scan.section(cfg).get("table_csv", "risk_reward")
    tables = sorted(universe_scan.universe_dir(cfg).glob(f"{stem}_*.csv"))
    if tables:
        frame = pd.read_csv(tables[-1])
        row = frame[frame["ticker"] == tick]
        sector = (row["sector"].iloc[0] if not row.empty
                  and pd.notna(row["sector"].iloc[0]) else "")
        if sector:
            group = frame[(frame["sector"] == sector) & (frame["ticker"] != tick)]
            peer_rows = (group.nlargest(peers, "reward")
                         [["ticker", "company", "reward", "risk", "quadrant",
                           "vetoed"]]
                         .where(group.notna(), None).to_dict("records"))

    return {
        "ticker": tick,
        "company": report.get("company"),
        "sector": sector,
        "deterministic": {
            "reward": report.get("reward_axis"),
            "risk": report.get("risk_axis"),
            "quant_score": report.get("quant_score"),
            "moat_score": report.get("moat_score"),
            "vetoed": report.get("vetoed"),
            "veto_reasons": report.get("veto_reasons"),
            "rules": report.get("rules"),
            "moat_metrics": report.get("moat_metrics"),
            "risk_metrics": report.get("risk_metrics"),
            "worst_risk_readings": report.get("risk_normalized", [])[:5],
        },
        "sector_peers": peer_rows,
        "prompt": (
            f"Research the risk case for {tick}"
            + (f" ({sector})" if sector else "") + ". "
            "Every number above is already computed and checkable -- do not "
            "recompute, re-derive or restate any of them as your own finding, "
            "and do not produce new ratios. Your job is only the part arithmetic "
            "cannot reach:\n"
            "  1. Competitive position -- who is taking share from whom, and is "
            "the moat the metrics imply actually durable?\n"
            "  2. Management's capital-allocation record against what they said "
            "they would do.\n"
            "  3. Regulatory, legal and product-cycle exposure that has not hit "
            "the statements yet.\n"
            "  4. For each tripped rule above: is it a real problem or an "
            "artifact of the sector or an accounting convention? Say which, and "
            "why.\n"
            "  5. What would have to be true for the deterministic read to be "
            "wrong in either direction.\n"
            "Cite sources for every factual claim. Where you cannot find "
            "evidence, say so rather than inferring."),
        "note": ("No model ran to produce this. The numbers are Python; the "
                 "prompt is for the session that called this tool."),
    }


def register(mcp) -> None:
    @mcp.tool()
    def universe_quadrant(quadrant: str | None = None,
                          sector: str | None = None,
                          limit: int = 50,
                          vetoed_only: bool = False) -> dict:
        """The risk/reward plane for the whole index, from the cache.

        `quadrant` is 'buy' (low risk, high reward), 'speculative', 'dull',
        'avoid' or 'unknown'. Reads `universe_scan`'s cache and latest table --
        no network, no scoring. Run `universe_scan` first to fill it.
        """
        return quadrant_impl(quadrant, sector, limit, vetoed_only)

    @mcp.tool()
    def universe_scan(refresh: bool = False, tickers: list[str] | None = None,
                      limit: int | None = None) -> dict:
        """Grade the whole S&P 500 on the risk/reward plane. Long job.

        ~1.6s per ticker over four Yahoo calls each, so a full pass is roughly
        15 minutes; cached tickers are skipped unless `refresh`. Returns a
        job_id -- poll `job_status`. Writes the table, PNG and interactive HTML
        under output/universe/. No Discord.
        """
        args = ["universe_scan.py"]
        if refresh:
            args.append("--refresh")
        if tickers:
            args += ["--tickers", *[t.upper() for t in tickers]]
        if limit:
            args += ["--limit", str(int(limit))]
        return backtests._script_job("universe_scan", args)

    @mcp.tool()
    def risk_research(ticker: str, peers: int = 6) -> dict:
        """The deterministic risk bundle for one ticker, plus a research prompt.

        Runs no model. Returns every rule with the threshold it was compared
        against (tripped / clean / unknown), the moat and risk metrics, the axis
        coordinates and the sector peer group -- then a prompt naming only the
        questions arithmetic cannot answer. Reason from the bundle; never
        recompute what it already contains. Hits Yahoo and SEC, tens of seconds.
        """
        return risk_research_impl(ticker, peers)
