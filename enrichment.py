"""The enrichment record -- what the agent saw, in a form tier 4 can grade.

The analyzer is deterministic end to end: tiers 1-4 compute every number, and
no code path in it invokes a model. What a model *can* still contribute is the
part arithmetic cannot reach -- competitive position, whether a tripped rule is
a sector artifact, what the filings and the tape are saying that the statements
have not caught up with. `DETERMINISTIC_GAPS.md` section D is the standing list.

That contribution used to arrive as `narrative_adj`, a number the model wrote
into the recorded conviction. Measured over its whole life it moved nine
verdicts by at most 5 points against a +/-15 bound and changed **no** tier
(`AI_ROLE.md`), while making the one score in the system unverifiable. So the
coupling is gone and this table replaces it. The difference that matters:

  * **Nothing here can move a score.** There is no numeric adjustment field --
    not clamped, not bounded, absent. A conviction is what the registry
    computed, always.
  * **Everything here is graded.** The agent's judgment is recorded as
    categorical attributes and handed to tier 4 exactly like `qr_*` (which
    quality rule passed) and `vt_*` (which veto tripped), so "did the agent's
    stance predict anything?" becomes a measurement rather than an opinion.

That is the same trade the veto layer already makes: leave it as a label and it
becomes a hypothesis you can watch; fold it into the tier and it becomes a
category nothing can be compared against.

One row per (scan_date, ticker), keyed and rewritten exactly like the on-demand
table, plus a prose report beside it that nothing in the pipeline reads.
"""

import json
from pathlib import Path

from agent_records import JudgmentSchema
from agent_records import read_one as _read_one
from scanner_common import (
    count_csv_rows,
    log_step,
    merge_history_csv,
    on_demand_csv_path,
    output_dir,
    read_table,
    signals_csv_path,
)

CONFIG_KEY = "enrichment"

# Same key as the on-demand table: an enrichment is about a *ticker on a date*,
# not about the screen that happened to surface it. A ticker that fired two
# screens has two `signals.csv` rows and one enrichment.
ENRICH_KEYS = ["scan_date", "ticker"]

# The controlled vocabularies. Every one of these is categorical on purpose --
# tier 4's `analyze` grades a two-group split, and a free-text judgment cannot
# be split. A value outside the vocabulary is rejected rather than recorded:
# silently it would become a one-member cohort that the analysis then dutifully
# "measures", which is how a typo turns into a finding.
STANCES = ("bull", "neutral", "bear")
MOAT_VIEWS = ("widening", "stable", "eroding", "unclear")
SENTIMENTS = ("positive", "mixed", "negative", "thin")

VOCABULARIES = {
    "stance": STANCES,
    "moat_view": MOAT_VIEWS,
    "social_sentiment": SENTIMENTS,
}

# Stored JSON-encoded so they survive the CSV round trip -- the same thing
# `scanner_common.history_rows` does with the per-year statement series.
LIST_FIELDS = ("concerns", "catalysts", "rule_disputes", "sources")

REQUIRED = ("scan_date", "ticker", "stance")

# Canonical column order, so the table stays readable when opened by hand.
COLUMNS = ["scan_date", "ticker", "stance", "moat_view", "social_sentiment",
           "conviction_note", "concerns", "catalysts", "rule_disputes",
           "sources", "sources_n", "concerns_n", "report", "agent_date",
           "model"]

SCHEMA = JudgmentSchema(
    columns=tuple(COLUMNS), required=REQUIRED, vocabularies=VOCABULARIES,
    list_fields=LIST_FIELDS,
    banned=("narrative_adj", "conviction", "tier", "score"),
    banned_reason="enrichment records judgment, it never adjusts a verdict",
    counts={"sources_n": "sources", "concerns_n": "concerns"},
    numeric=("sources_n", "concerns_n"))


# --------------------------------------------------------------------------
# Where it lives
# --------------------------------------------------------------------------

def section(cfg: dict) -> dict:
    return cfg.get(CONFIG_KEY) or {}


def is_enabled(cfg: dict) -> bool:
    return bool(section(cfg).get("enabled", True))


def enrichment_dir(cfg: dict, create: bool = True) -> Path:
    """Where the enrichment record lives (`enrichment.dir` under output/).

    Its own directory rather than a third file in `history/`, for the reason
    `portfolio_dir` gives for the ledger: history records what the *scan* saw
    and is never revised, while this is written by something else entirely,
    days later, and may be rewritten when the agent looks again.
    """
    path = Path(section(cfg).get("dir", "enrichment"))
    if not path.is_absolute():
        path = output_dir(create) / path
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def enrichment_csv_path(cfg: dict, create: bool = True) -> Path:
    """The enrichment table -- one row per (scan_date, ticker).

    An absolute path in config overrides the directory, as everywhere else here.
    Unlike everywhere else here, its parent is created too: `create=True` means
    "this path is about to be written", and an absolute override pointing at a
    directory that does not exist yet otherwise fails deep inside `to_csv` with
    an OSError naming pandas rather than the setting that caused it.
    """
    path = Path(section(cfg).get("csv", "enrichment.csv"))
    if not path.is_absolute():
        return enrichment_dir(cfg, create) / path
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def report_path(ticker: str, scan_date: str, cfg: dict,
                create: bool = True) -> Path:
    """Where the agent's prose for one look belongs."""
    return enrichment_dir(cfg, create) / f"{ticker.upper()}_{scan_date}.md"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate(row: dict) -> list[str]:
    """Everything wrong with a proposed row; empty means it is recordable."""
    return SCHEMA.validate(row)


def normalize(row: dict) -> dict:
    """A validated row in storage form: vocabularies lowered, lists JSON."""
    return SCHEMA.normalize(row)


# --------------------------------------------------------------------------
# Reading and writing
# --------------------------------------------------------------------------

def record(row: dict, cfg: dict) -> Path | None:
    """Write one enrichment row. The only supported way to add one.

    Raises `ValueError` on an invalid row rather than recording it -- unlike
    almost everything else here, which fails open. The difference is that a bad
    value in this table is not a missing measurement, it is a *wrong* one, and
    tier 4 cannot tell the two apart once it is on disk.
    """
    if not is_enabled(cfg):
        log_step("ENRICH", "skip", "enrichment.enabled is false", cfg=cfg)
        return None

    problems = validate(row)
    if problems:
        raise ValueError("; ".join(problems))

    clean = normalize(row)
    ticker, scan_date = clean["ticker"], clean["scan_date"]

    # A row nothing can join to is not an error -- you may enrich a ticker
    # ahead of its next signal -- but it is worth saying out loud, because the
    # silent version is an enrichment that tier 4 never grades.
    match = {"scan_date": scan_date, "ticker": ticker}
    known = (count_csv_rows(signals_csv_path(cfg, create=False), match)
             or count_csv_rows(on_demand_csv_path(cfg, create=False), match))
    if not known:
        log_step("ENRICH", "warn",
                 f"{ticker} {scan_date} matches no recorded scan -- "
                 "nothing will join to it", cfg=cfg)

    path = enrichment_csv_path(cfg)
    frame = merge_history_csv(path, [clean], ENRICH_KEYS)
    log_step("ENRICH", "ok", f"{ticker} {scan_date} -> {path.name}  "
                             f"{len(frame)} row(s)", cfg=cfg)
    return path


def read_one(ticker: str, scan_date: str, cfg: dict) -> dict:
    """One enrichment row as a plain dict, or `{}` when it was never enriched."""
    return _read_one(enrichment_csv_path(cfg, create=False), ticker, scan_date)


def decode_lists(row: dict) -> dict:
    """The stored row with its JSON list columns decoded, for display."""
    return SCHEMA.decode_lists(row)


# --------------------------------------------------------------------------
# CLI -- the agent's only write path
# --------------------------------------------------------------------------

def _cmd_record(args: list[str], cfg: dict) -> int:
    if not args:
        print("usage: python enrichment.py record <row.json>")
        return 2
    payload = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else [payload]
    for row in rows:
        path = record(row, cfg)
        if path is None:
            print("enrichment.enabled is false -- nothing recorded.")
            return 1
        print(f"Recorded {row.get('ticker')} {row.get('scan_date')} "
              f"in {path.name}.")
    return 0


def _cmd_show(args: list[str], cfg: dict) -> int:
    path = enrichment_csv_path(cfg, create=False)
    if not path.exists():
        print(f"No enrichment recorded yet ({path}).")
        return 1
    frame = read_table(path)
    if args:
        frame = frame[frame["ticker"].astype(str).str.upper()
                      == args[0].upper()]
    if frame.empty:
        print("Nothing recorded for that ticker.")
        return 1
    columns = [c for c in ("scan_date", "ticker", "stance", "moat_view",
                           "social_sentiment", "sources_n", "concerns_n")
               if c in frame.columns]
    print(frame[columns].to_string(index=False))
    return 0


def main(argv: list[str]) -> int:
    from scanner_common import enable_utf8_output, load_config

    enable_utf8_output()
    cfg = load_config()
    command = argv[0] if argv else ""
    if command == "record":
        return _cmd_record(argv[1:], cfg)
    if command == "show":
        return _cmd_show(argv[1:], cfg)
    print(__doc__)
    print("usage:\n  python enrichment.py record <row.json>"
          "\n  python enrichment.py show [TICKER]")
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
