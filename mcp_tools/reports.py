"""The combined dossier: the graded half and the researched half on one page."""

from scanner_common import load_config


def register(mcp) -> None:
    @mcp.tool()
    def combined_report(tickers: list[str] | None = None,
                        from_signals: int | None = None) -> dict:
        """One markdown dossier per ticker: the graded half and the researched half.

        The two live in separate tables so that neither can move the other; this
        renders them side by side without joining them in any computational
        sense. Per ticker: the trigger and setup, the ⭐ quality result, the 🚫
        exclusion, both plane coordinates and the quadrant, the recorded tier and
        conviction with its group breakdown, then the agent's stance / moat view /
        social read with its concerns, catalysts, disputed rules and sources, then
        what tier 4 has done with the position since.

        `tickers` names them explicitly; `from_signals=N` sweeps everything the
        screens flagged in the last N days instead. Scope decides the filename, so
        a two-ticker run can never overwrite a whole-week one.

        **Reads recorded files only** -- no network, no grading, nothing
        recomputed, and every sentence generated in Python. A ticker nobody has
        researched is still included and marked as such: coverage is worth
        seeing, and "not researched" is a state rather than a gap.

        Returns the path; open it with `Read`.
        """
        import combined_report as builder

        return builder.build(tickers, load_config(), from_signals=from_signals)
