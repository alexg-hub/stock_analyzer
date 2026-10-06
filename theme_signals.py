"""The thematic screen -- AI names the candidate, Python grades it.

Tiers 1-3 all ask one kind of question: *which of the 903 constituents did
something on the tape?* That is arithmetic, which is why it is deterministic and
why no code path in the analyzer can start a model. A whole class of investable
event is invisible to it. When a hyperscaler announces a data centre, when an
outbreak starts, when an AI capex cycle turns, the beneficiaries -- the grid
engineer, the turbine maker, the CDMO, the semiconductor equipment supplier --
have done nothing on their own charts yet. The chain runs from a dated event
through a company's revenue, and no rolling window finds it.

`DETERMINISTIC_GAPS.md` section D scoped AI to `risk_research`: the *grading*
side, judging a ticker a screen already surfaced. This is the mirror image --
the *identification* side -- and it keeps the same contract, stated in
`AI_ROLE.md`: a session may call the analyzer; the analyzer may not call a
session. Nothing here invokes a model. A session reasons over the bundle
`mcp_tools/themes.py` assembles and calls `record()` with what it concluded.

Three properties hold the boundary, and each fails silently if broken:

  * **The record names a company and a mechanism; it never carries a number.**
    `validate` rejects `conviction`, `tier`, `score`, `Verdict`, `Reward`,
    `Risk` and `price_target` by name. Every figure on a theme row is computed
    by `run_scanners.scan_ticker` *after* the pick is made.
  * **A pick is proved tradeable before it is recorded.** IBKR's theme graph
    happily returns Prysmian (Milan), NKT (Copenhagen) and POWERGRID (NSE), and
    a model can invent a symbol outright. `scan_ticker` downloads real price
    data, so a name with none is refused and nothing is written. That check also
    supplies the `scan_date`, from the ticker's own last settled bar.
  * **An invalid row raises rather than records**, inverting the house
    fail-open rule for the same reason `enrichment.record` inverts it: a wrong
    categorical value is not a missing measurement, it is a cohort of one that
    `portfolio_sim analyze` will faithfully grade.

This screen is deliberately in **neither** screen registry. It is not in
`run_scanners.SCANNERS` (it would run nightly, and `scan(data, strategy)` over a
price panel is meaningless for a news-driven pick), and it is not in
`backtest_universe.SCREENS` -- which is the load-bearing one. That list is what
`tests/_harness.screens()` reads, and `test_signal_contract.py` then demands a
full-history `(days x tickers)` mask that reproduces itself when bars are
dropped, blanked and half-formed. A list dated *today* has no history; faking
one would get backtested as though it were real. `tests/test_theme_screen.py`
pins the absence from both registries, because nothing else would notice.

What it *is*: a third write path into `signals.csv`, alongside the nightly scan
and `on_demand`. `portfolio_sim.ledger.sync` filters by nothing, so tier 4 buys
and grades these positions under `config_key = "theme_screen"` with no change
there at all -- which is the entire point. Whether AI thematic screening pays is
then a measurement (`analyze` split on `config_key`) rather than an argument.
"""

import json
from datetime import date
from pathlib import Path

import pandas as pd

import scanner_common
from agent_records import JudgmentSchema
from agent_records import read_one as _read_one
from newsfeed import is_enabled, section, theme_names, themes_dir
from scanner_common import (
    INDEX_COL,
    TRIGGER_COL,  # noqa: F401 - re-exported for callers of this module
    TRIGGER_NONE,
    log_step,
    merge_history_csv,
    read_table,
)

# The `config_key` every theme row carries. Not a `*_strategy` section: there
# are no thresholds to sweep here, and naming it one would imply `tune_screen`
# could tune it.
CONFIG_KEY = "theme_screen"

# Distinct from full / partial / none, so a theme pick is never mistaken for a
# technical setup in any cohort split.
SETUP_VALUE = "theme"

# The beneficiary tiers a chain can name. Deliberately one flat vocabulary
# across every theme rather than one per theme: `analyze` grades a group split,
# and a role that exists in only one theme is a cohort of one by construction.
CHAIN_ROLES = ("operator", "engineering", "equipment", "power", "utility",
               "transmission", "materials", "fuel", "designer", "foundry",
               "developer", "cdmo", "diagnostics", "supplies", "prime",
               "subsystem", "services")

# How much of the company the theme can actually move. The single most useful
# field here: a mega-cap with a rounding-error exposure is the classic way a
# thematic pick looks right and pays nothing.
EXPOSURES = ("pure_play", "major", "moderate", "minor")

HORIZONS = ("announced", "near_term", "multi_year", "speculative")
CONFIDENCES = ("high", "medium", "low")

# `social` is legal and will almost never be used: Reddit is blocked outright,
# StockTwits 403s, X is paid-tier and Facebook has no public search (tested
# 2026-08-14, see `newsfeed.py`). Kept in the vocabulary so the record is
# already shaped if a source ever opens up, and so a session that *did* reach
# one can say so precisely instead of calling it `mixed`.
EVIDENCE_TYPES = ("news", "trade_press", "filing", "theme_graph", "social",
                  "mixed")

VOCABULARIES = {
    "chain_role": CHAIN_ROLES,
    "exposure": EXPOSURES,
    "time_horizon": HORIZONS,
    "confidence": CONFIDENCES,
    "evidence_type": EVIDENCE_TYPES,
}

LIST_FIELDS = ("sources", "risks")

# `scan_date` is deliberately absent: unlike an enrichment, the caller does not
# supply it. It comes from the ticker's own last settled bar during recording,
# which is also what proves the ticker exists.
REQUIRED = ("ticker", "theme", "event", "mechanism", "sources")

COLUMNS = ["scan_date", "ticker", "theme", "event", "event_date", "mechanism",
           "chain_role", "exposure", "time_horizon", "confidence",
           "evidence_type", "risks", "sources", "sources_n", "risks_n",
           "report", "agent_date", "evidence_file", "evidence_n"]

THEME_KEYS = ["scan_date", "ticker"]

# The compact categoricals that ride along on the `signals.csv` row, so tier 4
# carries them verbatim onto the position and can grade each as a group split.
# The wide narrative (event, mechanism, risks, sources) stays in themes.csv --
# same split `enrichment.py` draws, and for the same reason: `signals.csv` is a
# union of every screen's columns and prose would bloat every row of it.
THEME_COL = "Theme"
ROLE_COL = "Chain Role"
EXPOSURE_COL = "Exposure"
HORIZON_COL = "Horizon"
CONFIDENCE_COL = "Confidence"
EVIDENCE_COL = "Evidence"

SIGNAL_FIELDS = {
    "theme": THEME_COL,
    "chain_role": ROLE_COL,
    "exposure": EXPOSURE_COL,
    "time_horizon": HORIZON_COL,
    "confidence": CONFIDENCE_COL,
    "evidence_type": EVIDENCE_COL,
}

# The fields a caller must never send. Redundant with the unknown-field check
# below, exactly as `enrichment.validate` re-rejects `narrative_adj` by name:
# the generic message says "unknown", and this one says why it will stay that
# way.
BANNED_FIELDS = ("conviction", "tier", "score", "narrative_adj", "Verdict",
                 "Conviction", "Quality", "Reward", "Risk", "Quadrant",
                 "price_target", "target_price")

SCHEMA = JudgmentSchema(
    columns=tuple(COLUMNS), required=REQUIRED, vocabularies=VOCABULARIES,
    list_fields=LIST_FIELDS, banned=BANNED_FIELDS,
    banned_reason=("this screen identifies candidates, it never grades them; "
                   "every number on a theme row is computed by the registry "
                   "after the pick"),
    counts={"sources_n": "sources", "risks_n": "risks"})


# --------------------------------------------------------------------------
# Where it lives
# --------------------------------------------------------------------------

def themes_csv_path(cfg: dict, create: bool = True) -> Path:
    """The theme table -- one row per (scan_date, ticker)."""
    path = Path(section(cfg).get("csv", "themes.csv"))
    if not path.is_absolute():
        return themes_dir(cfg, create) / path
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def report_path(ticker: str, scan_date: str, cfg: dict,
                create: bool = True) -> Path:
    """Where a theme write-up for one pick belongs."""
    return themes_dir(cfg, create) / f"{ticker.upper()}_{scan_date}.md"


def evidence_path(theme: str, scan_date: str, cfg: dict,
                  create: bool = True) -> Path:
    """Where the events a pick was made from are frozen."""
    return themes_dir(cfg, create) / f"events_{theme}_{scan_date}.json"


def snapshot_evidence(theme: str, scan_date: str, cfg: dict) -> tuple[str, int]:
    """Freeze the events behind a pick. Returns `(filename, count)`.

    **The news cache is mutable and this is not.** `newsfeed._save_cache`
    replaces a theme's entry wholesale on every refresh, so re-reading a pick
    from three weeks ago through the cache shows *today's* headlines -- which
    would make the record look auditable without being auditable. This writes
    the events as they stood when the pick was recorded, under a name keyed by
    `(theme, scan_date)`, and the row points at it.

    Fail-open: a snapshot that cannot be written costs the audit trail, never
    the record. It returns `("", 0)` and the row simply says so.
    """
    import newsfeed

    try:
        events = newsfeed.cluster_events(newsfeed.recent(cfg, [theme]))
        if not events:
            # No file rather than an empty one: "" in the row says the trail is
            # missing, while an empty snapshot would claim the pick was made
            # from no evidence at all.
            return "", 0
        path = evidence_path(theme, scan_date, cfg)
        path.write_text(json.dumps(
            {"theme": theme, "scan_date": scan_date,
             "captured_at": date.today().isoformat(), "events": events},
            indent=2, ensure_ascii=False), encoding="utf-8")
        return path.name, len(events)
    except Exception as exc:  # noqa: BLE001 - the record matters more
        log_step("THEME", "warn",
                 f"{theme}: could not snapshot the evidence ({exc})", cfg=cfg)
        return "", 0


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate(row: dict, cfg: dict | None = None) -> list[str]:
    """Everything wrong with a proposed pick; empty means it is recordable.

    The shared schema check, plus two rules only a theme has: corroboration
    (an event with one source is a rumour) and a configured theme name.
    """
    problems = SCHEMA.validate(row)

    minimum = int((section(cfg or {}) or {}).get("min_sources", 2))
    sources = row.get("sources")
    if isinstance(sources, (list, tuple)) and 0 < len(sources) < minimum:
        problems.append(
            f"sources has {len(sources)} entr{'y' if len(sources) == 1 else 'ies'}"
            f", need at least {minimum} -- an event with one source is not an "
            "event, it is a rumour")

    if cfg is not None:
        known = theme_names(cfg)
        theme = str(row.get("theme") or "").strip()
        if theme and known and theme not in known:
            problems.append(
                f"theme={theme!r} is not configured -- one of {', '.join(known)}"
                " (add it to theme_screen.themes in config.json first)")
    return problems


def normalize(row: dict) -> dict:
    """A validated pick in storage form: vocabularies lowered, lists JSON."""
    return SCHEMA.normalize(row)


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------

def record(theme: str, event: str, picks: list[dict], cfg: dict,
           event_date: str = "", deep: bool = False) -> dict:
    """Record a theme's picks. The only supported way to add one.

    **Atomic over the batch**: every pick is validated before any is written,
    and one failure writes nothing. That is `config_edit`'s rule -- the unit of
    validity is the set -- and it matters more here, because a theme is a
    *chain*. Half a chain on the record is not a partial answer, it is a
    misleading cohort: the tiers that happened to validate would look like the
    whole thesis.

    Each pick is then graded by `scan_ticker` (which is also what proves it is
    tradeable), written to `signals.csv` under `config_key="theme_screen"`, and
    its narrative written to `themes.csv` beside it.

    `deep=True` additionally runs tier 3's deterministic verdict and records it.
    Off by default: it costs an EDGAR fetch plus a deep Yahoo pass per pick.
    """
    if not is_enabled(cfg):
        log_step("THEME", "skip", "theme_screen.enabled is false", cfg=cfg)
        return {"recorded": False,
                "note": "theme_screen.enabled is false in config.json"}

    if not picks:
        return {"recorded": False, "problems": ["no picks supplied"]}

    limit = int(section(cfg).get("max_picks_per_run", 8))
    if len(picks) > limit:
        return {"recorded": False,
                "problems": [f"{len(picks)} picks exceeds "
                             f"theme_screen.max_picks_per_run ({limit})"]}

    # -- validate the whole batch first; write nothing if any pick fails ------
    rows, problems = [], []
    for pick in picks:
        row = {**pick, "theme": theme, "event": event}
        if event_date:
            row["event_date"] = event_date
        found = validate(row, cfg)
        if found:
            problems.append(f"{pick.get('ticker', '?')}: {'; '.join(found)}")
        rows.append(row)
    if problems:
        raise ValueError("; ".join(problems))

    # -- grade every pick before writing anything, for the same reason. This is
    #    also the tradeability guard: IBKR's theme graph returns Milan,
    #    Copenhagen and NSE listings, and a symbol with no bars is refused --
    import run_scanners

    scanned, refused = run_scanners.grade_batch(
        [str(row["ticker"]) for row in rows], cfg)
    if refused:
        raise ValueError("refused: " + "; ".join(refused))
    graded = [(dict(row, ticker=ticker, scan_date=scan_date), hit, fired)
              for row, (ticker, hit, scan_date, fired) in zip(rows, scanned)]

    # One snapshot per (theme, scan_date), taken once for the batch: every pick
    # in it was made from the same events, and writing it per ticker would be
    # the same file five times.
    snapshot_date = graded[0][0]["scan_date"]
    evidence_file, evidence_n = snapshot_evidence(theme, snapshot_date, cfg)

    signal_rows, theme_rows, recorded = [], [], []
    for row, hit, trigger in graded:
        clean = normalize({**row, "evidence_file": evidence_file,
                           "evidence_n": evidence_n})
        theme_rows.append(clean)

        signal_row = scanner_common.signal_row(
            hit, clean["scan_date"], CONFIG_KEY, clean["ticker"], SETUP_VALUE,
            trigger, screen=f"AI theme: {theme}",
            **{column: clean.get(field) or ""
               for field, column in SIGNAL_FIELDS.items()})
        signal_rows.append(signal_row)
        recorded.append({"ticker": clean["ticker"],
                         "scan_date": clean["scan_date"],
                         "trigger": trigger or TRIGGER_NONE,
                         "index": signal_row[INDEX_COL] or "off-index"})

    signals_path = scanner_common.record_signal_rows(signal_rows, cfg)
    themes_path = themes_csv_path(cfg)
    merge_history_csv(themes_path, theme_rows, THEME_KEYS)

    log_step("THEME", "ok",
             f"{theme}: {len(recorded)} pick(s) -> {signals_path.name} + "
             f"{themes_path.name}"
             + (f"; {evidence_n} event(s) frozen in {evidence_file}"
                if evidence_file else "; evidence NOT snapshotted"), cfg=cfg)

    hits = {row["ticker"]: hit for row, hit, _ in graded}
    verdicts = _record_verdicts(recorded, hits, theme, cfg) if deep else []

    return {
        "recorded": True,
        "theme": theme,
        "picks": recorded,
        "signals_csv": str(signals_path),
        "themes_csv": str(themes_path),
        "evidence_file": evidence_file,
        "evidence_n": evidence_n,
        "verdicts": verdicts,
        "note": ("Recorded as candidates, not as a grade. Every number on "
                 "these rows -- the badge, the veto, both plane axes -- was "
                 "computed by the registry after the pick and cannot be "
                 "changed from here. Tier 4 will buy them on the next "
                 "`portfolio_sim open`."),
    }


def _record_verdicts(recorded: list[dict], hits: dict, theme: str,
                     cfg: dict) -> list[dict]:
    """Tier 3's deterministic verdict for each pick, routed to `signals.csv`.

    `source=SOURCE_SIGNAL` is the load-bearing argument. `record_verdict` routes
    by the `source` key `_facts` wrote, and a theme pick is absent from
    `latest_hits.json` -- so left to `resolve_trigger` it would be stamped
    `on_demand` and the verdict would land in the *other* table, leaving the
    theme row's `Verdict` blank forever. This screen does write a `signals.csv`
    row, so `signal` is the honest answer, and reusing the existing routing
    beats hand-writing the columns.

    The trigger is the row `record` already graded, read back through
    `find_ticker` exactly as a nightly hit would be, so a pick is scanned once
    rather than once to record and again to grade.

    Fail-open per pick: a verdict is a bonus here, and losing one must never
    cost the record it was meant to annotate.
    """
    import research_report

    closes, benchmark = research_report.price_inputs(
        [e["ticker"] for e in recorded], cfg)
    out = []
    for entry in recorded:
        ticker = entry["ticker"]
        try:
            trigger = research_report.find_ticker(
                {"screens": [{"title": f"AI theme: {theme}",
                              "config_key": CONFIG_KEY,
                              "hits": {ticker: hits[ticker]}}]}, ticker, cfg)
            verdict = research_report.deterministic_verdict(
                ticker, cfg, trigger, entry["scan_date"],
                research_report.SOURCE_SIGNAL,
                close=research_report.close_of(closes, ticker),
                benchmark=benchmark)
            research_report.record_verdict(verdict, cfg)
            out.append({"ticker": ticker, "tier": verdict.get("tier"),
                        "conviction": verdict.get("conviction")})
        except Exception as exc:  # noqa: BLE001 - the record matters more
            log_step("THEME", "warn", f"{ticker}: verdict failed ({exc})",
                     cfg=cfg)
    return out


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def read_one(ticker: str, scan_date: str, cfg: dict) -> dict:
    """One theme row as a plain dict, or `{}` when there is none."""
    return _read_one(themes_csv_path(cfg, create=False), ticker, scan_date)


def read_recent(cfg: dict, ticker: str | None = None,
                limit: int = 50) -> list[dict]:
    """Recorded theme picks, newest first. `[]` when nothing is recorded."""
    path = themes_csv_path(cfg, create=False)
    if not path.exists():
        return []
    try:
        frame = read_table(path)
    except Exception:  # noqa: BLE001
        return []
    if frame.empty:
        return []
    if ticker and "ticker" in frame.columns:
        frame = frame[frame["ticker"].astype(str).str.upper() == ticker.upper()]
    if "scan_date" in frame.columns:
        frame = frame.sort_values("scan_date", ascending=False, kind="stable")
    rows = frame.head(limit).to_dict("records")
    return [decode_lists({k: (None if pd.isna(v) else v)
                          for k, v in row.items()}) for row in rows]


def decode_lists(row: dict) -> dict:
    """The stored row with its JSON list columns decoded, for display."""
    return SCHEMA.decode_lists(row)


# --------------------------------------------------------------------------
# CLI -- the same `record()` the MCP tool calls, so the two cannot drift
# --------------------------------------------------------------------------

def _cmd_record(args: list[str], cfg: dict) -> int:
    if not args:
        print("usage: python theme_signals.py record <picks.json>")
        return 2
    payload = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        print("picks.json must be an object with theme, event and picks.")
        return 2
    try:
        result = record(payload.get("theme", ""), payload.get("event", ""),
                        payload.get("picks") or [], cfg,
                        event_date=payload.get("event_date", ""),
                        deep="--deep" in args)
    except ValueError as exc:
        print(f"Nothing recorded. {exc}")
        return 1
    if not result.get("recorded"):
        print(f"Nothing recorded. {result.get('problems') or result.get('note')}")
        return 1
    for pick in result["picks"]:
        trigger = pick.get("trigger") or "no technical trigger"
        print(f"Recorded {pick['ticker']} {pick['scan_date']}  ({trigger})")
    return 0


def _cmd_show(args: list[str], cfg: dict) -> int:
    rows = read_recent(cfg, args[0] if args else None)
    if not rows:
        print(f"No theme picks recorded yet ({themes_csv_path(cfg, False)}).")
        return 1
    frame = pd.DataFrame(rows)
    columns = [c for c in ("scan_date", "ticker", "theme", "chain_role",
                           "exposure", "confidence", "sources_n")
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
    print("usage:\n  python theme_signals.py record <picks.json> [--deep]"
          "\n  python theme_signals.py show [TICKER]")
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
