"""The schema check for every table a session writes a *judgment* into.

`enrichment.py` and `theme_signals.py` both record categorical fields an agent
chose, and both must refuse any field that could carry a score. This is the one
implementation of that check, so the two tables cannot drift on what a valid
row is.

Strict where everything else fails open, and deliberately so: a missing
measurement is honest, but a wrong categorical value is a cohort of one that
tier 4's `analyze` will faithfully grade. So an invalid row raises rather than
records.
"""

import json
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from scanner_common import read_table


@dataclass(frozen=True)
class JudgmentSchema:
    columns: tuple
    required: tuple
    vocabularies: dict
    list_fields: tuple
    banned: tuple
    banned_reason: str
    counts: dict = field(default_factory=dict)  # count column -> list field
    numeric: tuple = ()

    def validate(self, row: dict) -> list[str]:
        """Everything wrong with a proposed row; empty means it is recordable."""
        problems = []
        for name in self.required:
            value = row.get(name)
            empty = (not value if isinstance(value, (list, tuple))
                     else not str(value or "").strip())
            if empty:
                problems.append(f"{name} is required")

        for name, allowed in self.vocabularies.items():
            value = row.get(name)
            if value not in (None, "") and str(value).lower() not in allowed:
                problems.append(
                    f"{name}={value!r} is not one of {', '.join(allowed)}")

        for name in self.list_fields:
            value = row.get(name)
            if value not in (None, "") and not isinstance(value, (list, tuple)):
                problems.append(
                    f"{name} must be a list, got {type(value).__name__}")

        for name in self.numeric:
            value = row.get(name)
            if value is not None and not isinstance(value, (int, float)):
                problems.append(f"{name} must be a number")

        unknown = set(row) - set(self.columns)
        if unknown:
            problems.append(f"unknown field(s): {', '.join(sorted(unknown))}")

        # Named, not just "unknown": re-adding a field that can move a verdict
        # is the exact regression these tables exist to prevent.
        for name in self.banned:
            if name in row:
                problems.append(f"{name!r} is not allowed -- {self.banned_reason}")
        return problems

    def normalize(self, row: dict) -> dict:
        """A validated row in storage form: vocabularies lowered, lists JSON.

        `agent_date` is stamped rather than asked for: `scan_date` is the bar the
        row is about and cannot say when the judgment was formed.
        """
        out = {}
        for key, value in row.items():
            if key in self.vocabularies and value not in (None, ""):
                value = str(value).lower()
            elif key in self.list_fields:
                value = json.dumps(list(value or []), ensure_ascii=False)
            out[key] = value
        for count, source in self.counts.items():
            out.setdefault(count, len(row.get(source) or []))
        out.setdefault("agent_date", date.today().isoformat())
        out["ticker"] = str(out.get("ticker", "")).upper()
        out["scan_date"] = str(out.get("scan_date", ""))
        return {c: out.get(c) for c in self.columns if c in out}

    def decode_lists(self, row: dict) -> dict:
        """The stored row with its JSON list columns decoded, for display."""
        out = dict(row)
        for name in self.list_fields:
            raw = out.get(name)
            if isinstance(raw, str) and raw:
                try:
                    out[name] = json.loads(raw)
                except json.JSONDecodeError:
                    pass
        return out


def read_one(path, ticker: str, scan_date: str) -> dict:
    """One `(ticker, scan_date)` row as a plain dict, `{}` when there is none.

    `{}` is the answer tier 4 needs for "never recorded", which is a cohort in
    its own right rather than a gap to fill.
    """
    try:
        frame = read_table(path)
    except Exception:  # noqa: BLE001 - an unreadable record is an absent one
        return {}
    if frame.empty or not {"ticker", "scan_date"} <= set(frame.columns):
        return {}
    hit = frame[(frame["ticker"].astype(str).str.upper() == ticker.upper())
                & (frame["scan_date"].astype(str) == str(scan_date))]
    if hit.empty:
        return {}
    return {k: (None if pd.isna(v) else v) for k, v in hit.iloc[-1].to_dict().items()}
