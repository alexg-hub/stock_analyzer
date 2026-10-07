"""The universe risk/reward plane, and the one door to AI risk research.

Two read-only tools over `universe_scan`'s cache plus a job to refill it, and
`risk_research` -- which despite the name runs **no** model. It assembles
everything deterministic about one ticker and hands it back for the calling
session to reason over.

That shape is deliberate, and it is now the *only* shape. The repo used to carry
two headless invocation paths whose tool allow-lists had to stay byte-identical,
and an un-allowed tool was refused *silently* -- so a missing report section had
no error to explain it. Both are gone (see AI_ROLE.md); a tool that returns a
bundle needs no subprocess, no allow-list and no new silent-failure mode,
because the session asking the question already has the tools to answer it.

The reasoning built on this bundle belongs to the `enrich` skill, which records
its conclusion through `enrichment_record` -- an attribute tier 4 grades, never
an adjustment to the score assembled here.
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
    import peers

    # Regrading needs each ticker's sector, or every `sector_relative`
    # parameter silently falls back to absolute anchors and the quadrants
    # disagree with the published plane (294 of 903 did). Offline sources only:
    # the last whole-index table for sub-industry and index, peers for sector.
    meta = pd.DataFrame(columns=["ticker", "sector", "sub_industry", "index_name"])
    latest = sorted(universe_scan.universe_dir(cfg).glob(
        f"{universe_scan.section(cfg).get('table_csv', 'risk_reward')}_*.csv"))
    if latest:
        table = pd.read_csv(latest[-1])
        meta = table[[c for c in meta.columns if c in table.columns]]

    # The whole-index view is the current membership: the cache also keeps
    # tickers that have since left the index, frozen at their last fetch.
    if tickers:
        wanted = [t.upper() for t in tickers]
    elif not meta.empty:
        wanted = meta["ticker"].tolist()
    else:
        wanted = list(cache)
    view = {t: cache[t] for t in wanted if t in cache}
    sectors = (pd.DataFrame({"ticker": list(view)})
               .merge(meta, on="ticker", how="left")
               .reindex(columns=["ticker", "sector", "sub_industry", "index_name"])
               .fillna(""))
    sectors["sector"] = [s or peers.sector_of(t, cfg)
                         for t, s in zip(sectors["ticker"], sectors["sector"])]
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

    if sector:
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
        "rows": table.head(limit).astype(object).where(table.notna(), None)
                .to_dict("records"),
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
                         .astype(object).where(group.notna(), None).to_dict("records"))

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
            "Where to look, since none of it is reachable from Python: IBKR's "
            "`get_company_connections` for ranked competitors, revenue mix and "
            "geography (questions 1 and 5); `get_company_themes` / "
            "`get_theme_details` for secular exposure (question 3); "
            "`get_option_data` for the market's implied move around a catalyst. "
            "Never `get_account_*` or `get_pa_*` -- the real book is out of "
            "scope. Then the 10-K's Item 1 and Risk Factors, and web search for "
            "the last 30-60 days.\n"
            "Cite sources for every factual claim. Where you cannot find "
            "evidence, say so rather than inferring. Record the conclusion with "
            "`enrichment_record` -- it is graded against forward returns, and it "
            "cannot and must not change the verdict above."),
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
                      from_signals: int | None = None,
                      limit: int | None = None) -> dict:
        """Grade tickers on the risk/reward plane. Long job.

        Three scopes, and each writes its **own** files so none can overwrite
        another: no arguments grades every constituent (~1.5s per ticker over
        four Yahoo calls, so ~13 minutes) into `risk_reward_*`; `tickers=[...]`
        grades just those into `subset_plane_*`; `from_signals=N` grades what the
        screens flagged in the last N days into `signals_plane_*`.

        Cached tickers are skipped unless `refresh`. Returns a job_id -- poll
        `job_status`. No Discord.
        """
        args = ["universe_scan.py"]
        if refresh:
            args.append("--refresh")
        if tickers:
            args += ["--tickers", *[t.upper() for t in tickers]]
        elif from_signals is not None:
            args += ["--from-signals", str(int(from_signals))]
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
