"""The enrichment agent's write path.

Everything else in this server hands *deterministic* facts to a session --
`risk_research` assembles a bundle and asks the five questions arithmetic
cannot answer, `deepdive_context` hands over the whole company bundle,
`universe_quadrant` the sector peers. This is the one tool that goes the other
way: what the session concluded, recorded so tier 4 can grade it.

Two properties are load-bearing and both are enforced in `enrichment.py`, not
here, so the CLI and this tool cannot drift:

  * **No field can move a score.** `validate` rejects `conviction`, `tier`,
    `score` and `narrative_adj` by name. The verdict is the registry's, always.
  * **An invalid row raises rather than records.** Everything else in this
    project fails open, because a missing measurement is honest. A *wrong*
    categorical value is not missing -- it is a cohort of one that `analyze`
    will faithfully report on.
"""

from scanner_common import load_config


def record_impl(row: dict) -> dict:
    import enrichment

    cfg = load_config()
    problems = enrichment.validate(row)
    if problems:
        return {"recorded": False, "problems": problems,
                "note": "nothing was written -- fix the row and call again"}
    path = enrichment.record(row, cfg)
    if path is None:
        return {"recorded": False,
                "note": "enrichment.enabled is false in config.json"}
    return {
        "recorded": True,
        "ticker": row.get("ticker"),
        "scan_date": row.get("scan_date"),
        "table": str(path),
        "note": ("Recorded as an attribute, not an adjustment -- the tier and "
                 "conviction are unchanged and cannot be changed from here."),
    }


def read_impl(ticker: str, scan_date: str) -> dict:
    import enrichment

    cfg = load_config()
    row = enrichment.read_one(ticker, scan_date, cfg)
    if not row:
        return {"ticker": ticker.upper(), "scan_date": scan_date,
                "enrichment": None,
                "note": "never enriched -- a legitimate cohort, not a gap"}
    return {"ticker": ticker.upper(), "scan_date": scan_date,
            "enrichment": enrichment.decode_lists(row)}


def register(mcp) -> None:
    @mcp.tool()
    def enrichment_record(scan_date: str, ticker: str, stance: str,
                          moat_view: str | None = None,
                          social_sentiment: str | None = None,
                          conviction_note: str | None = None,
                          concerns: list[str] | None = None,
                          catalysts: list[str] | None = None,
                          rule_disputes: list[str] | None = None,
                          sources: list[str] | None = None,
                          report: str | None = None,
                          model: str | None = None) -> dict:
        """Record what the enrichment pass concluded about one (scan_date, ticker).

        This does **not** change the verdict. Tier 3's tier and conviction are
        computed by the registry and are not editable from here or anywhere
        else -- the point of this table is that a qualitative judgment can be
        *measured against forward returns* instead of quietly moving a score.

        Controlled vocabularies, because tier 4 grades a group split and free
        text cannot be split:
          stance           bull | neutral | bear
          moat_view        widening | stable | eroding | unclear
          social_sentiment positive | mixed | negative | thin

        `thin` and `neutral` are legitimate answers -- a thin read must be
        visibly thin rather than rounded up to a view. `rule_disputes` names
        veto keys you judge to be sector artifacts rather than real problems;
        `sources` should hold a URL per factual claim. An unknown value is
        rejected with an explanation and nothing is written.

        Re-recording the same (scan_date, ticker) replaces that row.
        """
        row = {"scan_date": scan_date, "ticker": ticker, "stance": stance,
               "moat_view": moat_view, "social_sentiment": social_sentiment,
               "conviction_note": conviction_note, "concerns": concerns,
               "catalysts": catalysts, "rule_disputes": rule_disputes,
               "sources": sources, "report": report, "model": model}
        return record_impl({k: v for k, v in row.items() if v is not None})

    @mcp.tool()
    def enrichment_read(ticker: str, scan_date: str) -> dict:
        """What the agent previously concluded about one (scan_date, ticker).

        Returns `enrichment: null` when there is none, which is the answer tier
        4 needs for "never enriched" -- a cohort, not a gap to fill in.
        """
        return read_impl(ticker, scan_date)
