"""The thematic screen's two doors -- and neither of them runs a model.

`theme_research` is the `risk_research` shape applied to identification instead
of grading: it assembles everything deterministic -- the configured themes and
their beneficiary chains, dated sourced events from `newsfeed.py`, what has
already been recorded so a run does not re-record the same chain -- and returns
it with a prompt naming only the part arithmetic cannot do. Which company
benefits from an announcement, through what mechanism, and whether the exposure
is material to its revenue: none of that is derivable from a price panel or a
statement, and no ratio reaches it.

`theme_record` is the way back in, and it is the guard. Everything in this
server hands facts *to* a session; this takes a conclusion back, and refuses to
let it be a number. `theme_signals.validate` rejects `conviction`, `tier`,
`score`, `Verdict`, `Reward`, `Risk` and `price_target` by name, and every
figure that ends up on a theme row is computed by `run_scanners.scan_ticker`
*after* the pick was made -- which is also what proves the ticker is real and
US-listed before it can reach a table tier 4 buys from.

Both properties live in `theme_signals.py` rather than here, so the CLI and
these tools cannot drift -- the same arrangement `mcp_tools/enrichment.py` has
with `enrichment.py`.
"""

from scanner_common import index_map, load_config


def research_impl(theme: str | None = None,
                  lookback_days: int | None = None) -> dict:
    """The deterministic thematic bundle, plus what to do with it.

    Assembles; never concludes. No model runs here.
    """
    import newsfeed
    import theme_signals

    cfg = load_config()
    configured = newsfeed.themes(cfg)
    if theme:
        configured = [t for t in configured if t.get("name") == theme]
        if not configured:
            return {"error": f"no theme named {theme!r}",
                    "known": newsfeed.theme_names(cfg),
                    "fix": "add it to theme_screen.themes in config.json"}

    names = [t["name"] for t in configured]
    days = (lookback_days if lookback_days is not None
            else int(newsfeed.section(cfg).get("lookback_days", 14)))
    events = newsfeed.cluster_events(newsfeed.recent(cfg, names, days=days))

    already = theme_signals.read_recent(cfg, limit=100)
    seen = [{"ticker": r.get("ticker"), "scan_date": r.get("scan_date"),
             "theme": r.get("theme"), "event": r.get("event")}
            for r in already if r.get("theme") in names]

    # Who is telling the SEC about this theme, with a ticker already resolved.
    # One request per theme, fail-open: `sec_fulltext` returns [] on any error,
    # so a slow or unhappy EDGAR costs corroboration and never the bundle.
    #
    # `filing_query` is deliberately separate from the news query and is skipped
    # when absent. EDGAR full-text matches an exact phrase, so the keyword soup
    # that works for a news search ("data center investment announcement")
    # matches nothing at all -- and zero hits reads as "nobody filed about
    # this", which is a wrong answer rather than a missing one.
    filings = []
    for entry in configured:
        query = entry.get("filing_query")
        if not query:
            continue
        for hit in newsfeed.sec_fulltext(query, cfg, forms="8-K", limit=12,
                                         days=days,
                                         item=entry.get("filing_item", "")):
            filings.append({**hit, "theme": entry["name"], "matched": query})

    # Index membership, and it is the one thing that corrects the size bias in
    # a phrase search. Micro-caps mention a theme promotionally far more often
    # than large filers mention one materially, so a bare query buries IRM, NI,
    # CVX, HE and OSK among ARMP/QUCY/ZSQR. Measured 2026-08-14, filtering by
    # 8-K *item code* instead does not fix this -- it surfaced PLD for
    # `datacenter` but collapsed `energy` to one hit and `biopharma` to zero,
    # losing Chevron and Oshkosh with them. So the item stays available per
    # theme and set nowhere, and the ordering does the work: constituents
    # first, then by filing date. Nothing is dropped -- an off-index filer is
    # still a legitimate find, just not the one to read first.
    index_of = index_map(cfg) if filings else {}
    for hit in filings:
        hit["index"] = index_of.get(hit.get("ticker", ""), "")
    filings.sort(key=lambda h: (bool(h["index"]), h.get("filed", "")),
                 reverse=True)

    return {
        "themes": [{"name": t["name"], "chain": list(t.get("chain") or []),
                    "query": t.get("query")} for t in configured],
        "events": events,
        "events_note": (
            "Pulled from the configured news feed by Python, then de-duplicated "
            "into one entry per event and ordered by size, corroboration and "
            "recency -- so the top of this list is where to start. "
            "`sources_n` is how many distinct outlets carried it, which is the "
            "corroboration `min_sources` asks about; `magnitude` is the largest "
            "money amount in the headline, in `magnitude_currency` and NOT "
            "FX-converted. An event with no `magnitude` is not a small event, "
            "it is one nobody put a number on in the headline. "
            "Reddit, StockTwits, X and Facebook are NOT reachable from here "
            "(blocked, 403 and paid-tier respectively) and Google Trends has "
            "no official API, so this is a news-and-filings screen. Say that "
            "plainly rather than implying a social read that did not happen."),
        "filings": filings,
        "filings_note": (
            "8-K filings mentioning the theme query in the same window, from "
            "EDGAR full-text search, with EDGAR's own ticker. A company "
            "telling the SEC about a theme is stronger evidence than a "
            "journalist associating it with one -- but the match is a phrase "
            "hit, not a judgment, so confirm the mechanism before recording. "
            "Ordered index constituents first: micro-caps mention a theme "
            "promotionally far more often than large filers mention one "
            "materially, so an empty `index` means read it more sceptically, "
            "not that it is wrong."),
        "already_recorded": seen,
        "vocabularies": {
            "chain_role": list(theme_signals.CHAIN_ROLES),
            "exposure": list(theme_signals.EXPOSURES),
            "time_horizon": list(theme_signals.HORIZONS),
            "confidence": list(theme_signals.CONFIDENCES),
            "evidence_type": list(theme_signals.EVIDENCE_TYPES),
        },
        "min_sources": int(newsfeed.section(cfg).get("min_sources", 2)),
        "max_picks_per_run": int(
            newsfeed.section(cfg).get("max_picks_per_run", 8)),
        "prompt": (
            "Identify companies that benefit from one of the events above. "
            "You are naming CANDIDATES, not grading them: never state a price "
            "target, a conviction, a tier or a score. Every number on a "
            "recorded pick -- the quality badge, the exclusion veto, both "
            "risk/reward axes -- is computed by the registry after you record, "
            "and `theme_record` rejects those fields by name.\n"
            "Method:\n"
            "  1. Pick ONE concrete, dated event from `events` that has a named "
            "counterparty and a location or a number. Vague sector commentary "
            "is not an event.\n"
            "  2. Walk that theme's `chain` one link at a time. The operator is "
            "usually already priced; the tiers behind it are where the work "
            "pays.\n"
            "  3. For each candidate state the mechanism in ONE falsifiable "
            "sentence -- what specifically it sells into this event.\n"
            "  4. State what would make the thesis wrong, in `risks`.\n"
            "  5. Reject any name whose exposure is immaterial to its revenue. "
            "A mega-cap with a rounding-error segment is the classic thematic "
            "pick that looks right and pays nothing -- that is what `exposure` "
            "records, so be honest with it.\n"
            "Where to look, since none of this is reachable from Python:\n"
            "  * `search_investment_topics` -> `get_theme_details` is the "
            "strongest source here: it returns companies ranked by relevance "
            "with a sourced evidence paragraph each. Query it with SHORT "
            "SINGULAR nouns ('grid', 'semiconductor', 'biotech'); plurals and "
            "multi-word phrases return nothing.\n"
            "  * `get_company_connections` for the supplier and competitor "
            "graph behind a name you already have.\n"
            "  * `search_contracts` to resolve a company name to a tradeable "
            "symbol. The theme graph returns foreign listings (Milan, "
            "Copenhagen, NSE) -- confirm a US listing, or the recorder will "
            "refuse the pick.\n"
            "  * `WebSearch` / `WebFetch` to corroborate. An event with one "
            "source is a rumour; the recorder enforces a minimum.\n"
            "Never `get_account_*` or `get_pa_*` -- the real book is out of "
            "scope for every surface in this project.\n"
            "Cite a source per factual claim. Where you cannot find evidence, "
            "say so rather than inferring. Then record with `theme_record`; "
            "the picks land in signals.csv, get graded by the registry, and "
            "are bought by the virtual portfolio so the screen's record can be "
            "measured against the technical screens."),
        "note": ("No model ran to produce this. The events and lists are "
                 "Python; the prompt is for the session that called this tool."),
    }


def record_impl(theme: str, event: str, picks: list[dict],
                event_date: str = "", deep: bool = False) -> dict:
    """Validate and record a batch of picks. Never raises at the tool boundary.

    A refusal comes back as data so the session can fix the row and call again,
    which is what `mcp_tools/enrichment.record_impl` does for the same reason.
    """
    import theme_signals

    cfg = load_config()
    try:
        return theme_signals.record(theme, event, picks, cfg,
                                    event_date=event_date, deep=deep)
    except ValueError as exc:
        return {"recorded": False, "problems": str(exc).split("; "),
                "note": "nothing was written -- fix the picks and call again"}


def read_impl(ticker: str | None = None, limit: int = 50) -> dict:
    """Recorded theme picks, newest first. Reads one CSV; no network."""
    import theme_signals

    cfg = load_config()
    rows = theme_signals.read_recent(cfg, ticker, limit)
    if not rows:
        return {"picks": [],
                "note": ("nothing recorded yet -- run the theme-screen skill, "
                         "or `theme_research` to see the current events")}
    return {"picks": rows, "count": len(rows)}


def register(mcp) -> None:
    @mcp.tool()
    def theme_research(theme: str | None = None,
                       lookback_days: int | None = None) -> dict:
        """Dated thematic events plus a research brief. Runs no model.

        The identification counterpart to `risk_research`. Returns the
        configured themes with their beneficiary chains, recent sourced news
        events, what has already been recorded (so you do not re-record the
        same chain), and the controlled vocabularies -- then a prompt naming
        only the questions arithmetic cannot answer.

        Reads a cached RSS feed and one CSV; seconds, no Discord. `theme`
        narrows to one configured theme; omit for all.

        This is a news-, filings- and theme-graph-driven screen. Reddit,
        StockTwits, X and Facebook are not reachable and Google Trends has no
        API -- do not imply a social sentiment read you could not perform.
        """
        return research_impl(theme, lookback_days)

    @mcp.tool()
    def theme_record(theme: str, event: str, picks: list[dict],
                     event_date: str = "", deep: bool = False) -> dict:
        """Record thematic picks into signals.csv, where tier 4 will buy them.

        **This writes to the signal history and creates virtual positions.**
        Each pick is scanned first: that downloads real price data, which is
        what proves the ticker exists and is US-listed (the theme graph returns
        Milan, Copenhagen and NSE listings), supplies the `scan_date` from its
        own last settled bar, and computes the quality badge, the exclusion
        veto and both plane axes. You name the company and the mechanism; the
        registry grades it. `conviction`, `tier`, `score`, `Verdict`, `Reward`,
        `Risk` and `price_target` are rejected by name.

        Each entry of `picks` is a dict:
          ticker         required -- a US-listed symbol
          mechanism      required -- ONE falsifiable sentence on what it sells
                         into this event
          sources        required -- a URL per factual claim, at least
                         `min_sources` of them
          chain_role     operator | engineering | equipment | power | utility |
                         transmission | materials | fuel | designer | foundry |
                         developer | cdmo | diagnostics | supplies | prime |
                         subsystem | services
          exposure       pure_play | major | moderate | minor
          time_horizon   announced | near_term | multi_year | speculative
          confidence     high | medium | low
          evidence_type  news | trade_press | filing | theme_graph | social |
                         mixed
          risks          what would make the thesis wrong
          report         filename of the write-up, if you archived one

        `low` confidence and `minor` exposure are legitimate answers -- a thin
        read must be visibly thin. **The batch is atomic**: one invalid pick
        writes nothing, because half a chain on the record is a misleading
        cohort rather than a partial answer. An unknown value is rejected with
        an explanation and nothing is written.

        `deep=True` also runs tier 3's verdict per pick (an EDGAR fetch plus a
        deep Yahoo pass each). Costs ~1.5s per pick otherwise. No Discord.
        """
        return record_impl(theme, event, picks, event_date, deep)

    @mcp.tool()
    def theme_read(ticker: str | None = None, limit: int = 50) -> dict:
        """What the thematic screen has recorded, newest first.

        Reads one CSV -- no network, no grading. Omit `ticker` for everything.
        """
        return read_impl(ticker, limit)
