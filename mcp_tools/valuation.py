"""Industry valuation anomalies, driven from a Claude Code session.

Three doors onto `industry_valuation.py`: run the pass, read what it found, and
record a bucket's members so tier 4 grades them. Every guard lives in
`industry_valuation.py` itself -- the same arrangement `mcp_tools/themes.py` has
with `theme_signals.py`, so the CLI and these tools cannot drift.

Nothing here runs a model and nothing here reaches Discord. The pass is entirely
deterministic: it reconstructs each company's P/E path from announcement-dated
quarterly EPS, aggregates to GICS sub-industry, and splits every industry's price
move into the part the market repriced and the part the companies earned.
"""

from scanner_common import load_config


def scan_impl(refresh: bool = False, no_fetch: bool = False,
              limit: int = 0) -> dict:
    """Submit the full pass as a background job."""
    from . import backtests
    args = ["industry_valuation.py", "scan"]
    if refresh:
        args.append("--refresh")
    if no_fetch:
        args.append("--no-fetch")
    if limit:
        args += ["--limit", str(int(limit))]
    return backtests._script_job("industry_valuation", args)


def read_impl(bucket: str | None = None, flagged_only: bool = False) -> dict:
    """The most recent table on disk. No network, no grading, nothing recomputed."""
    import industry_valuation as iv

    cfg = load_config()
    table = iv.read_table(cfg, bucket or "")
    if table.empty:
        return {"buckets": [], "rows": 0,
                "note": ("nothing recorded yet -- run `valuation_scan` first"
                         if not bucket else
                         f"no recorded row for {bucket!r}")}
    if flagged_only:
        table = table[table["flag"].fillna("") != ""]

    return {
        "scan_date": str(table["scan_date"].iat[0]) if len(table) else "",
        "rows": int(len(table)),
        "buckets": table.where(table.notna(), None).to_dict("records"),
        "flagged": table[table["flag"].fillna("") != ""]["bucket"].tolist(),
        "reading_note":
            "price = multiple x earnings. The `*_dlog` columns are log changes "
            "and add exactly (price_dlog == multiple_dlog + earnings_dlog); the "
            "`*_chg_pct` columns are the same figures as percentages and so add "
            "only approximately. `cheap` means the multiple is historically low "
            "AND earnings grew over the window -- a multiple that fell because "
            "earnings fell is correct repricing, not a dislocation.",
        "caveat":
            "Bucket membership is today's membership applied backwards (see "
            "`membership_asof`), so every historical path is survivorship-"
            "biased. `forward_pe` is a level only -- Yahoo serves no history "
            "for it, and valuation_history.csv is what builds one from now on.",
        "note": "",
    }


def record_impl(bucket: str, top: int = 0) -> dict:
    """Record a bucket's members as signals. Never raises at the tool boundary."""
    import industry_valuation as iv

    try:
        return iv.record(bucket, load_config(), top=top)
    except Exception as exc:  # noqa: BLE001
        return {"recorded": False, "picks": 0,
                "problems": str(exc).split("; "),
                "note": "nothing was written -- fix the request and call again"}


def register(mcp) -> None:
    @mcp.tool()
    def valuation_scan(refresh: bool = False, no_fetch: bool = False,
                       limit: int = 0) -> dict:
        """Run the industry valuation pass: which industries' multiples have left
        their own history, and whether earnings moved with them.

        Reconstructs a ~5-year daily P/E path per company from announcement-dated
        quarterly EPS, aggregates to GICS sub-industry (thin buckets roll up to
        their sector), and decomposes each industry's price move into multiple
        change and earnings change.

        Returns a job handle immediately -- poll with `job_status`. Costs ~20
        minutes on a cold cache (one earnings-history fetch per constituent) and
        seconds warm, because the cache holds the quarterly EPS points and the
        daily path is rebuilt from a fresh price panel for free. No Discord.

        refresh   -- ignore cached earnings history and refetch every ticker.
        no_fetch  -- rebuild from the cache only; no network at all.
        limit     -- timing probe over the first N tickers. Writes NOTHING:
                     only a full pass may write the table, the chart or the
                     history row.
        """
        return scan_impl(refresh=refresh, no_fetch=no_fetch, limit=limit)

    @mcp.tool()
    def valuation_read(bucket: str | None = None,
                       flagged_only: bool = False) -> dict:
        """Read the most recent industry valuation table. Cache only -- no
        network, nothing recomputed, instant.

        Each row carries the industry's current median P/E, where that sits in
        its own trailing history (`pe_z`, `pe_pctile`), the decomposition of its
        recent price move, today's median forward P/E, and a `cheap`/`rich` flag.

        bucket        -- one industry by name, e.g. "Semiconductors". Omit for all.
        flagged_only  -- only the industries that tripped a flag.
        """
        return read_impl(bucket=bucket, flagged_only=flagged_only)

    @mcp.tool()
    def valuation_record(bucket: str, top: int = 0) -> dict:
        """Record an industry's members into signals.csv so tier 4 buys and
        grades them -- the only way to learn whether the anomaly ever paid.

        Every pick is graded by `run_scanners.scan_ticker` BEFORE anything is
        written, so each row carries the quality badge, the veto, both plane
        axes and its index membership, computed after the pick. A name with no
        bars is refused rather than written into a table tier 4 buys from, and
        the batch is atomic: one bad name writes nothing, because half an
        industry on the record is a misleading cohort rather than a partial
        answer.

        Costs ~1.5s per pick. No Discord. Requires a recorded table, so run
        `valuation_scan` first.

        bucket -- the industry name exactly as `valuation_read` reports it.
        top    -- record at most this many members (capped by max_picks_per_run).
        """
        return record_impl(bucket=bucket, top=top)
