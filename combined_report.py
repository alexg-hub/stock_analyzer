"""One dossier per ticker: what the analyzer computed and what the agent found.

The two halves are recorded in separate tables on purpose -- `signals.csv` and
`<T>_<date>_facts.json` hold what Python computed, `enrichment.csv` holds what a
session concluded, and nothing lets the second move the first. That separation is
the point (see `AI_ROLE.md`), but it leaves no single artifact you can open and
read end to end. This is that artifact.

**It is a rendering, not an analysis.** Every number is copied from a recorded
file and every sentence is generated here in Python; the agent's prose is quoted
verbatim or linked, never summarised. Same rule as `deterministic_thesis` and
tier 4's `conclusion` -- a combined report that *narrated* the two halves would
be a third place a model writes numbers, which is exactly what was removed.

It therefore does **no network I/O and no grading at all**. It reads:

  * `signals.csv` / `on_demand_scans_results.csv` -- the trigger, tier 2, the
    plane coordinates, and the recorded verdict
  * `<TICKER>_<scan_date>_facts.json` -- the quant score, its group breakdown,
    and `quant_metrics`: every parameter value the score was computed from
  * `enrichment.csv` + `<TICKER>_<scan_date>.md` -- the agent's judgment
  * `positions.csv` -- what tier 4 has done with it since

The parameter table is the reason this page can be read on its own. A group
score of 0.34 is not checkable -- P/E 17.96 against a `max 37` gate is. So every
recorded value is printed beside the threshold it was compared against, which is
the same discipline `research_report.risk_report` and the `risk_research` MCP
bundle already follow. The *values* come from the facts file; only the label,
group, gate and number format are resolved from `quality.parameters`, which is
config lookup rather than grading -- pass/fail is read back from the recorded
`quality_missing` and veto reason lists, never recomputed. A parameter the
registry knows but the scan never resolved prints `n/a — not evaluated`, because
that is a different statement from "failed" and the distinction is load-bearing
everywhere else in this project.

A missing half is reported as missing rather than filled in. A ticker nobody
enriched still gets a page, saying so: coverage is itself worth seeing, and
"not researched" is a legitimate state, not a gap to paper over.

    python combined_report.py TJX GOOG
    python combined_report.py --from-signals 7
"""

import json
from pathlib import Path

import pandas as pd

import enrichment
import quality
from scanner_common import (
    CONVICTION_COL,
    DEEP_VETO_COL,
    DEEP_VETO_REASONS_COL,
    QUADRANT_COL,
    QUALITY_COL,
    QUALITY_MISSING_COL,
    REWARD_COL,
    RISK_COL,
    VERDICT_COL,
    VETO_COL,
    VETO_REASONS_COL,
    log_step,
    on_demand_csv_path,
    output_dir,
    signals_csv_path,
)

CONFIG_KEY = "combined"

# Scope decides the filename, for the reason `universe_scan.artifact_paths`
# spells out: the artifacts are date-stamped, so without this a two-ticker run
# and a whole-week run resolve to the same path and one silently replaces the
# other. A single ticker gets its own file so a dossier is never overwritten by
# an unrelated run on the same day.
SCOPE_TICKER = "ticker"     # exactly one named ticker
SCOPE_SUBSET = "subset"     # several named tickers
SCOPE_SIGNALS = "signals"   # everything the screens flagged in a window


def section(cfg: dict) -> dict:
    return cfg.get(CONFIG_KEY) or {}


def combined_dir(cfg: dict, create: bool = True) -> Path:
    """`output/<combined.dir>` -- its own directory, like the ledger's.

    A dossier belongs to no single tier: it spans the scan record, the verdict,
    the agent's judgment and the ledger. Same bare-filename-in-config convention
    as everything else, so an absolute path still overrides and tests redirect.
    """
    path = Path(section(cfg).get("dir", "combined"))
    if not path.is_absolute():
        path = output_dir(create) / path
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def report_path(cfg: dict, scope: str, tickers: list[str],
                stamp: str | None = None) -> Path:
    stamp = stamp or pd.Timestamp.today().date().isoformat()
    if scope == SCOPE_TICKER and tickers:
        name = f"{tickers[0]}_{stamp}_combined.md"
    else:
        name = f"{scope}_combined_{stamp}.md"
    return combined_dir(cfg) / name


# --------------------------------------------------------------------------
# Reading the record
# --------------------------------------------------------------------------

def _table(path: Path) -> pd.DataFrame:
    """One history table, tolerant of the headerless file a quiet night writes."""
    from portfolio_sim.ledger import read_table
    return read_table(path)


def _clean(value):
    """A CSV value as something JSON-safe, or None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _latest_row(ticker: str, cfg: dict) -> tuple[dict, str]:
    """The most recent recorded scan row for `ticker`, and which table it came from.

    Both tables are searched because a `(scan_date, ticker)` belongs to exactly
    one of them -- a nightly signal annotates its `signals.csv` row, an ad-hoc
    look has none and gets the on-demand table. Newest wins, so a ticker that
    signalled last week and was looked at yesterday reports yesterday.
    """
    best, source = {}, ""
    for path, label in ((signals_csv_path(cfg, create=False), "signal"),
                        (on_demand_csv_path(cfg, create=False), "on_demand")):
        frame = _table(path)
        if frame.empty or "ticker" not in frame.columns:
            continue
        hit = frame[frame["ticker"].astype(str).str.upper() == ticker.upper()]
        if hit.empty:
            continue
        hit = hit.assign(_d=pd.to_datetime(hit["scan_date"], errors="coerce"))
        row = hit.sort_values("_d").iloc[-1].to_dict()
        row.pop("_d", None)
        if not best or str(row.get("scan_date", "")) >= str(best.get("scan_date", "")):
            best, source = row, label
    return best, source


def _facts(ticker: str, scan_date: str, cfg: dict) -> dict:
    """The recorded facts file, or `{}`. Read, never recomputed.

    Recomputing would score *today's* fundamentals against a verdict reached
    weeks ago -- the same reason `marking._tier3_columns` reads rather than
    re-grades.
    """
    from research_report import load_facts
    return load_facts(ticker, scan_date, cfg) or {}


def _position(ticker: str, scan_date: str, cfg: dict) -> dict:
    """What tier 4 has done with this signal, or `{}` if it never bought it."""
    from scanner_common import positions_csv_path
    frame = _table(positions_csv_path(cfg, create=False))
    if frame.empty or "ticker" not in frame.columns:
        return {}
    hit = frame[(frame["ticker"].astype(str).str.upper() == ticker.upper())
                & (frame["scan_date"].astype(str) == str(scan_date))]
    if hit.empty:
        return {}
    return {k: _clean(v) for k, v in hit.iloc[-1].to_dict().items()}


def collect_one(ticker: str, cfg: dict) -> dict:
    """Every recorded half for one ticker, assembled and not interpreted."""
    ticker = ticker.upper()
    row, source = _latest_row(ticker, cfg)
    scan_date = str(row.get("scan_date", "")) if row else ""
    enriched = enrichment.read_one(ticker, scan_date, cfg) if scan_date else {}
    prose = (enrichment.report_path(ticker, scan_date, cfg, create=False)
             if scan_date else None)
    return {
        "ticker": ticker,
        "scan_date": scan_date,
        "source": source,
        "scan_row": {k: _clean(v) for k, v in (row or {}).items()},
        "facts": _facts(ticker, scan_date, cfg) if scan_date else {},
        "enrichment": enrichment.decode_lists(enriched) if enriched else {},
        "enrichment_report": str(prose) if prose and prose.exists() else None,
        "position": _position(ticker, scan_date, cfg) if scan_date else {},
    }


# --------------------------------------------------------------------------
# Rendering -- every line built here, in Python
# --------------------------------------------------------------------------

def _num(value, fmt="{:.1f}", dash="n/a"):
    try:
        return fmt.format(float(value))
    except (TypeError, ValueError):
        return dash


def _listed(value) -> list:
    """A stored list column back as a list, whether JSON or already parsed."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [value]
        except json.JSONDecodeError:
            return [p.strip() for p in value.split(",") if p.strip()]
    return []


def _deterministic_block(entry: dict) -> list[str]:
    row, facts = entry["scan_row"], entry["facts"]
    out = [f"### {entry['ticker']}"
           + (f" ({facts.get('company')})" if facts.get("company") else "")]

    if not row:
        out += ["", "_No recorded scan. This ticker has never been screened or "
                    "looked at on demand, so there is nothing to combine._", ""]
        return out

    tier = row.get(VERDICT_COL) or facts.get("tier")
    conv = row.get(CONVICTION_COL)
    conv = conv if conv is not None else facts.get("conviction")
    verdict = (f"**{tier} · {_num(conv, '{:.1f}')}/100**" if tier
               else "_not yet graded_")
    trigger = row.get("screen") or row.get("Screens") or "ad-hoc look"
    setup = row.get("Setup")
    out += ["", f"{verdict} — {trigger}"
                + (f" ({setup} setup)" if setup and setup != "none" else ""),
            f"Recorded {entry['scan_date']} as a {entry['source'] or 'n/a'} row."]

    badge = row.get(QUALITY_COL)
    missing = _listed(row.get(QUALITY_MISSING_COL))
    if badge is True or str(badge).lower() == "true":
        quality = "passed ⭐"
    elif badge is None:
        quality = "not evaluated"
    else:
        quality = (f"failed {len(missing)} rule(s)"
                   + (f": {', '.join(missing)}" if missing else ""))

    # Both pairs, because they are written at different times and either alone
    # is a wrong answer. `Veto` comes from the scan; `Deep Veto` is written
    # hours later by tier 3 over the `deep` stage, which is where
    # dilution_veto, eps_collapse_veto and every SEC filing flag live. Reading
    # only the fast pair reported a name excluded on two deep rules as "clean",
    # while the parameter table below it correctly showed both as 🚫.
    flags = [row.get(VETO_COL), row.get(DEEP_VETO_COL)]
    reasons = list(dict.fromkeys(_listed(row.get(VETO_REASONS_COL))
                                 + _listed(row.get(DEEP_VETO_REASONS_COL))))
    if any(str(f).lower() == "true" for f in flags):
        exclusion = "🚫 excluded" + (f" — {', '.join(reasons)}" if reasons else "")
    elif any(f is not None for f in flags):
        exclusion = "clean"
    else:
        exclusion = "not evaluated"

    out += ["",
            f"- **Quality gate**: {quality}",
            f"- **Exclusion**: {exclusion}",
            f"- **Plane**: reward {_num(row.get(REWARD_COL))} / "
            f"risk {_num(row.get(RISK_COL))}"
            + (f" — *{row.get(QUADRANT_COL)}*" if row.get(QUADRANT_COL) else "")]

    dims = facts.get("quant_dimensions") or {}
    scored = {k: v.get("score") for k, v in dims.items()
              if isinstance(v, dict) and v.get("score") is not None}
    if scored:
        best = max(scored, key=scored.get)
        worst = min(scored, key=scored.get)
        used = sum(v.get("metrics_used", 0) for v in dims.values()
                   if isinstance(v, dict))
        total = sum(v.get("metrics_total", 0) for v in dims.values()
                    if isinstance(v, dict))
        out.append(f"- **Score breakdown**: {used} of {total} parameters; "
                   f"strongest {best.replace('_', ' ')} "
                   f"{scored[best]:.2f}, weakest {worst.replace('_', ' ')} "
                   f"{scored[worst]:.2f}")
    if facts.get("chart"):
        out.append(f"- **Financials chart**: `{facts['chart']}`")
    return out


def _gate_text(gate: dict | None) -> str:
    """The threshold a parameter was compared against, as the config states it."""
    if not gate:
        return "—"
    parts = []
    for key in ("min", "max"):
        if gate.get(key) is not None:
            parts.append(f"{key} {_clean(gate[key])}")
    if gate.get("increasing"):
        parts.append("increasing")
    return ", ".join(parts) or "—"


def _parameter_rows(entry: dict, cfg: dict) -> list[tuple]:
    """One row per registry parameter: label, value, gate, verdict, group.

    Values are read from the recorded `quant_metrics`; the label, gate and
    number format come from the registry. Nothing is graded here -- the verdict
    column is reconstructed from the gate-failure and veto-reason lists the scan
    already recorded, so a parameter reads exactly as it read on the night.
    """
    facts, row = entry["facts"], entry["scan_row"]
    values = facts.get("quant_metrics") or {}
    if not values:
        return []

    failed = set(_listed(facts.get("quality_missing"))
                 or _listed(row.get(QUALITY_MISSING_COL)))
    vetoed = set(_listed(facts.get("veto_reasons"))
                 + _listed(row.get(VETO_REASONS_COL))
                 + _listed(row.get(DEEP_VETO_REASONS_COL)))

    rows = []
    for key, spec in quality.parameters(cfg).items():
        present = key in values and values[key] is not None
        value = quality.format_scalar(values[key], spec) if present else "n/a"
        gate = spec.get("gate")
        if not present:
            verdict = "not evaluated"
        elif key in vetoed:
            verdict = "🚫 veto"
        elif spec.get("veto"):
            verdict = "clean"
        elif key in failed:
            verdict = "✗ fail"
        elif gate:
            verdict = "ok"
        else:
            verdict = "—"
        rows.append((quality.label_of(key, spec), value, _gate_text(gate),
                     verdict, spec.get("group") or "—"))
    return rows


def _parameters_block(entry: dict, cfg: dict, inline: bool) -> list[str]:
    """Every recorded parameter beside the threshold it was compared against.

    Grouped in registry order, which is also the order the Discord card uses, so
    a reader moving between the two sees the same sequence.
    """
    rows = _parameter_rows(entry, cfg)
    if not rows:
        return ["", "_No parameter values recorded for this scan, so there is "
                    "nothing to show beneath the score._"]

    body = []
    for group in dict.fromkeys(r[4] for r in rows):
        body += ["", f"**{group.replace('_', ' ')}**", "",
                 "| Parameter | Value | Gate | |", "|---|---|---|---|"]
        body += [f"| {label} | {value} | {gate} | {verdict} |"
                 for label, value, gate, verdict, g in rows if g == group]

    # Deliberately phrased against *today's* registry rather than the scan's.
    # The score breakdown above counts what was scored on the night; this counts
    # what the registry asks for now, and the two differ whenever a parameter has
    # been added since. Saying so is more useful than hiding it -- a large gap is
    # the signal that a recorded verdict predates the current shape and should
    # not be compared with a fresh one.
    evaluated = sum(1 for r in rows if r[3] != "not evaluated")
    head = (f"**Parameters** — {evaluated} of the {len(rows)} parameters in "
            f"today's registry carry a value on this scan")
    if inline:
        return ["", head] + body
    return ["", head, "",
            "<details><summary>All parameter values</summary>"] + body + [
            "", "</details>"]


def _enrichment_block(entry: dict, inline_prose: bool) -> list[str]:
    e = entry["enrichment"]
    if not e:
        return ["", "**Research:** not enriched. No session has researched this "
                    "ticker for this date, so the qualitative half is absent "
                    "rather than neutral.", ""]

    out = ["", f"**Research** — stance **{e.get('stance', 'n/a')}**, "
               f"moat {e.get('moat_view') or 'n/a'}, "
               f"social {e.get('social_sentiment') or 'n/a'} "
               f"({int(e.get('sources_n') or 0)} source(s), "
               f"recorded {e.get('agent_date') or 'n/a'})"]
    if e.get("conviction_note"):
        out += ["", f"> {e['conviction_note']}", ""]

    for label, key in (("Concerns", "concerns"), ("Catalysts", "catalysts"),
                       ("Disputed rules", "rule_disputes")):
        items = _listed(e.get(key))
        if items:
            out.append(f"- **{label}**: {', '.join(str(i) for i in items)}")

    sources = _listed(e.get("sources"))
    if sources:
        out.append("- **Sources**: " + ", ".join(f"<{s}>" for s in sources))

    path = entry.get("enrichment_report")
    if path and inline_prose:
        try:
            body = Path(path).read_text(encoding="utf-8").strip()
            out += ["", "<details><summary>Full research report</summary>", "",
                    body, "", "</details>"]
        except OSError:
            out.append(f"- **Report**: `{path}` (unreadable)")
    elif path:
        out.append(f"- **Report**: `{path}`")
    return out


def _position_block(entry: dict) -> list[str]:
    p = entry["position"]
    if not p:
        return ["", "**Position:** none on the virtual book.", ""]

    bits = [f"status **{p.get('status')}**"]
    if p.get("entry_price") is not None:
        bits.append(f"entered {p.get('entry_date')} at {_num(p['entry_price'], '{:.2f}')}")
    if p.get("open_ret_%") is not None:
        bits.append(f"marked {_num(p['open_ret_%'], '{:+.2f}')}% "
                    f"after {_num(p.get('days_held'), '{:.0f}')} days")

    out = ["", "**Position:** " + ", ".join(bits)]
    settled = [(c, p[c]) for c in sorted(p) if c.startswith("ret_")
               and c.endswith("d_%") and p.get(c) is not None]
    if settled:
        out.append("- **Settled**: "
                   + ", ".join(f"{c[4:-3]}d {_num(v, '{:+.2f}')}%"
                               for c, v in settled))
    if p.get("dt_signal_date"):
        out.append(f"- **Double top**: flagged {p['dt_signal_date']} "
                   f"({p.get('dt_status')})")
    return out


def render(entries: list[dict], cfg: dict, scope: str) -> str:
    """The whole report as markdown. Deterministic: same inputs, same bytes."""
    stamp = pd.Timestamp.today().date().isoformat()
    enriched = sum(1 for e in entries if e["enrichment"])
    graded = sum(1 for e in entries if e["scan_row"])

    lines = [f"# Combined report — {scope} — {stamp}", "",
             f"{len(entries)} ticker(s): {graded} with a recorded scan, "
             f"{enriched} researched.", "",
             "Every figure below is copied from a recorded file and every "
             "sentence is generated in Python; the research prose is quoted "
             "verbatim. Nothing here is recomputed, and the research half "
             "cannot and did not affect the graded half — see `AI_ROLE.md`.", "",
             "_Research analysis, not investment advice._", "", "---"]

    inline = len(entries) == 1
    for entry in entries:
        lines += _deterministic_block(entry)
        lines += _parameters_block(entry, cfg, inline)
        lines += _enrichment_block(entry, inline)
        lines += _position_block(entry)
        lines += ["", "---"]

    if not enriched:
        lines += ["", "None of these tickers has been researched. Run "
                      "`/enrich <TICKER>` in a Claude Code session to add the "
                      "qualitative half; it records to `output/enrichment/` and "
                      "cannot change any figure above."]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Building one
# --------------------------------------------------------------------------

def build(tickers: list[str] | None, cfg: dict, from_signals: int | None = None
          ) -> dict:
    """Collect, render and write. Returns the path and a compact summary."""
    import universe_scan

    if from_signals:
        tickers = universe_scan.signal_tickers(cfg, from_signals)
        scope = SCOPE_SIGNALS
    else:
        tickers = [t.upper() for t in (tickers or [])]
        scope = SCOPE_TICKER if len(tickers) == 1 else SCOPE_SUBSET

    if not tickers:
        log_step("COMBINE", "none", "no tickers to report on", cfg=cfg)
        return {"path": None, "tickers": [], "scope": scope,
                "note": ("nothing to report -- name a ticker, or widen "
                         "--from-signals" if from_signals else
                         "nothing to report -- name at least one ticker")}

    entries = [collect_one(t, cfg) for t in tickers]
    path = report_path(cfg, scope, tickers)
    path.write_text(render(entries, cfg, scope), encoding="utf-8")

    enriched = [e["ticker"] for e in entries if e["enrichment"]]
    missing = [e["ticker"] for e in entries if not e["scan_row"]]
    log_step("COMBINE", "ok", f"{len(entries)} ticker(s), {len(enriched)} "
                              f"researched -> {path.name}", cfg=cfg)
    return {
        "path": str(path),
        "scope": scope,
        "tickers": [e["ticker"] for e in entries],
        "researched": enriched,
        "no_scan_record": missing,
        "note": ("Read the file for the full dossier. Tickers with no research "
                 "are included and marked -- coverage is worth seeing."),
    }


def main(argv: list[str]) -> int:
    from scanner_common import enable_utf8_output, load_config

    enable_utf8_output()
    cfg = load_config()
    days = None
    if "--from-signals" in argv:
        i = argv.index("--from-signals")
        tail = argv[i + 1:i + 2]
        days = int(tail[0]) if tail and tail[0].isdigit() else int(
            (cfg.get("universe") or {}).get("signal_window_days", 7))
        argv = argv[:i] + argv[i + (2 if tail and tail[0].isdigit() else 1):]

    names = [a for a in argv if not a.startswith("-")]
    if not names and days is None:
        print(__doc__)
        return 2
    result = build(names, cfg, from_signals=days)
    if not result["path"]:
        print(result["note"])
        return 1
    print(f"{len(result['tickers'])} ticker(s), "
          f"{len(result['researched'])} researched -> {result['path']}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
