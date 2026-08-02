"""The ledger: recorded signals -> virtual positions.

One row per `(scan_date, ticker, config_key)` -- the same grain as
`signals.csv`, so a ticker that fired on two screens is two positions and any
analysis grouping by ticker must de-duplicate first, exactly as it must for the
history table.

The row carries the **entire** source row verbatim. That is the whole point:
the analysis this feeds asks which recorded attribute predicted the return, and
an attribute that was not carried can never be graded. The fundamentals columns
are config-driven display labels, so they are copied by name, never by
position, and the schema is allowed to grow.

`sync` is deliberately re-runnable over the whole source table rather than
incremental. It is a handful of rows a night, it repairs anything an
interrupted run left half-written, and it lets a tier-3 verdict recorded hours
after tier 1 wrote the row reach the ledger on the very next `mark`.
"""

import json
from datetime import datetime, timezone

import pandas as pd

from scanner_common import (
    QUALITY_COL,
    QUALITY_MISSING_COL,
    VERDICT_COL,
    merge_history_csv,
    on_demand_csv_path,
    positions_csv_path,
    signals_csv_path,
    step,
)

# Same grain as signals.csv. `scan_date` (not "signal_date") because that is
# what the rest of the pipeline calls this date -- the history tables, the
# facts files and the report filenames all key on it, and a second name for
# one thing is how joins start going wrong.
POSITION_KEYS = ["scan_date", "ticker", "config_key"]

# An ad-hoc look has no screen, so it needs a stand-in to key on. It is also
# the flag the analysis segments by: these rows had no technical trigger at
# all, which makes them a control group for the screens rather than a peer.
ON_DEMAND_KEY = "on_demand"

STATUS_PENDING = "pending"   # bought in principle; the entry bar has not traded
STATUS_OPEN = "open"         # filled, still inside the longest horizon
STATUS_CLOSED = "closed"     # every horizon has an exit bar

# Recorded at first sight and never revised: which quality rules were in force
# when this signal was graded. Without it, a rule added to config later would
# read as "passed" on every position recorded before it existed.
QUALITY_RULES_COL = "Quality Rules"

QR_PREFIX = "qr_"


def horizon_cols(horizon: int) -> dict[str, str]:
    """The column names one holding period contributes to the ledger."""
    return {
        "ret": f"ret_{horizon}d_%",
        "exit_date": f"exit_date_{horizon}d",
        "exit_price": f"exit_price_{horizon}d",
        "mfe": f"mfe_{horizon}d_%",
        "mae": f"mae_{horizon}d_%",
        "bench": f"bench_ret_{horizon}d_%",
        "excess": f"excess_{horizon}d_%",
    }


# Everything only `mark` ever writes. Passed as `protect=` so re-running
# `sync` refreshes the scan's attributes without erasing the prices -- the same
# contract that stops a re-scan erasing a tier-3 verdict in signals.csv.
#
# `status` is deliberately NOT in here. `protect` inherits from the row already
# on file, and a brand-new position has no such row, so a protected `status`
# would land as null instead of `pending`. It is carried explicitly instead.
BASE_MARK_COLS = ["entry_date", "entry_price", "shares", "notional",
                  "mark_date", "last_close", "open_ret_%", "days_held"]

# Everything only `exit-scan` ever writes: the double-top exit rule's verdict
# on this position. Recorded here rather than in a table of its own because the
# analysis asks which *entry* attribute predicted the return and this is the
# competing exit -- it has to sit on the same row to be compared with the fixed
# horizons.
#
# The position is flagged, never closed: `status` and every `ret_*d_%` keep
# running, so "sold on the double top" and "held to the horizon" stay two
# measurements of the same position rather than two different populations.
EXIT_COLS = ["dt_signal_date", "dt_exit_date", "dt_exit_price", "dt_status",
             "dt_ret_%", "dt_peak1", "dt_peak2", "dt_neckline"]

# `dt_signal_date` is what makes the scan idempotent: a position that already
# carries one is never re-examined, so a re-run adds no second sell row and
# re-alerts nothing.
EXIT_FLAG_COL = "dt_signal_date"

EXIT_PENDING = "pending"     # the neckline broke; the exit bar has not traded
EXIT_FILLED = "filled"       # sold at the next open

# Columns the ledger owns, so a source table can never overwrite them by
# happening to use the same label.
IDENTITY_COLS = ["position_id", "scan_date", "ticker", "config_key", "source",
                 "opened_at", "entry_rule", "status"]


def mark_columns(horizons: list[int]) -> list[str]:
    """Every column a *later* writer fills in, passed to `merge_history_csv`
    as `protect=`.

    `EXIT_COLS` belongs here for exactly the reason the prices do: `sync` runs
    again every night over the whole source table, and without the carry a
    re-sync would erase a recorded exit with no error at all.
    """
    cols = list(BASE_MARK_COLS) + list(EXIT_COLS)
    for h in horizons:
        cols.extend(horizon_cols(h).values())
    return cols


def reserved_columns(horizons: list[int]) -> set[str]:
    return set(IDENTITY_COLS) | set(mark_columns(horizons))


def horizons_of(port_cfg: dict) -> list[int]:
    return sorted(int(h) for h in port_cfg.get("horizons", [10, 30, 60]))


# --------------------------------------------------------------------------
# Reading the source tables
# --------------------------------------------------------------------------

def read_table(path) -> pd.DataFrame:
    """One CSV as a frame, or an empty frame when there is nothing to read.

    Tolerant of a **headerless** file, not just a missing one: a night on which
    no screen fires archives an empty row set, and `merge_history_csv` writes
    that as a zero-byte `signals.csv`. Reading it raises `EmptyDataError`, so a
    fresh install whose first night is quiet would otherwise log a ledger
    failure every night until something finally fired.
    """
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        frame = pd.read_csv(path, dtype={"scan_date": str})
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    return frame.dropna(how="all")


def _as_list(value) -> list | None:
    """A cell that holds a JSON list, whether it came from CSV or a payload.

    `None` means "not recorded", which is different from "recorded as empty" --
    an empty failed-rule list is a ticker that passed every rule.
    """
    if isinstance(value, list):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def text_of(value) -> str:  # noqa: D401
    """A cell as trimmed text, with every flavour of missing collapsing to "".

    `bool(float("nan"))` is True, so a plain truth test on a CSV cell reports
    an absent verdict as a recorded one.
    """
    if value is None:
        return ""
    if not isinstance(value, (list, dict, tuple)) and pd.isna(value):
        return ""
    return str(value).strip()


def as_bool(value):
    """The CSV round trip turns True into the string "True"."""
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().lower()
    if text in ("true", "1"):
        return True
    if text in ("false", "0"):
        return False
    return None


def _quality_rule_flags(row: dict, rules: list[str]) -> dict:
    """Per-rule pass/fail, exploded from the recorded failed-rule list.

    True = passed, False = failed, absent = not evaluated. The distinction
    matters: the scan skips the quality layer entirely when fundamentals are
    off, and scoring that as "failed every rule" would invent a verdict the
    pipeline never reached -- the same reason `_row_quality` guards on
    `_has_fundamentals`.
    """
    failed = _as_list(row.get(QUALITY_MISSING_COL))
    if failed is None:
        return {}
    # A rule recorded as failed is included even if config has since dropped
    # it: the record is what happened, not what the rules look like today.
    keys = list(dict.fromkeys(list(rules) + [k for k in failed if isinstance(k, str)]))
    return {f"{QR_PREFIX}{key}": key not in failed for key in keys}


# --------------------------------------------------------------------------
# Building position rows
# --------------------------------------------------------------------------

def _position_rows(frame: pd.DataFrame, source: str, cfg: dict,
                   frozen: dict) -> list[dict]:
    """Turn one source table into ledger rows."""
    if frame.empty:
        return []
    # The gate-bearing parameters as configured *now*; a position already on
    # file keeps the set frozen with it (below), so retuning the registry can
    # never rewrite a past finding.
    configured = [k for k, spec in (cfg.get("quality", {})
                                    .get("parameters") or {}).items()
                  if spec.get("enabled", True) and spec.get("gate")]
    horizons = horizons_of(cfg.get("portfolio", {}))
    opened_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry_rule = cfg.get("portfolio", {}).get("entry", "next_open")

    rows = []
    for record in frame.to_dict("records"):
        ticker = record.get("ticker")
        scan_date = record.get("scan_date")
        if not ticker or not scan_date or pd.isna(ticker) or pd.isna(scan_date):
            continue
        config_key = record.get("config_key") or ON_DEMAND_KEY
        if pd.isna(config_key):
            config_key = ON_DEMAND_KEY
        key = (str(scan_date), str(ticker), str(config_key))

        # The rule set in force when this signal was graded: whatever was
        # recorded the first time we saw it, else what config says now.
        rules = _as_list(frozen.get(key, {}).get(QUALITY_RULES_COL)) or configured

        was = frozen.get(key, {})
        row = {
            "position_id": f"{scan_date}_{ticker}_{config_key}",
            "scan_date": str(scan_date),
            "ticker": str(ticker),
            "config_key": str(config_key),
            "source": source,
            "opened_at": was.get("opened_at") or opened_at,
            "entry_rule": entry_rule,
            # Carried rather than protected -- see BASE_MARK_COLS.
            "status": text_of(was.get("status")) or STATUS_PENDING,
        }
        # Placeholders so a first-ever write lands in a readable column order;
        # `protect` hands the real values back on every later merge.
        for column in mark_columns(horizons):
            row[column] = pd.NA

        # The source row verbatim -- every attribute the analysis may want to
        # grade, under the config-driven labels the scan wrote them with.
        reserved = reserved_columns(horizons)
        for column, value in record.items():
            if column in POSITION_KEYS or column in reserved:
                continue
            row[column] = json.dumps(value) if isinstance(value, list) else value

        row[QUALITY_RULES_COL] = json.dumps(rules)
        row.update(_quality_rule_flags(record, rules))
        row["quality_pass"] = as_bool(record.get(QUALITY_COL))
        row["deep_dived"] = bool(text_of(record.get(VERDICT_COL)))
        rows.append(row)
    return rows


def load_positions(cfg: dict) -> pd.DataFrame:
    """The ledger as it stands. Empty frame when nothing has been opened."""
    return read_table(positions_csv_path(cfg, create=False))


def _frozen_by_key(positions: pd.DataFrame) -> dict:
    """Values carried from the row already on file, keyed by position."""
    if positions.empty:
        return {}
    wanted = [c for c in (QUALITY_RULES_COL, "opened_at", "status")
              if c in positions.columns]
    if not wanted or not set(POSITION_KEYS) <= set(positions.columns):
        return {}
    out = {}
    for record in positions.to_dict("records"):
        key = tuple(str(record.get(k)) for k in POSITION_KEYS)
        out[key] = {c: record.get(c) for c in wanted}
    return out


def sync(cfg: dict) -> pd.DataFrame:
    """Bring the ledger up to date with both history tables.

    Every recorded row becomes a position -- `full` and `partial` setups,
    quality passes and failures, deep-dived and not. The rows that failed a
    check are the control group; drop them and "did the quality screen help?"
    has nothing left to compare against.
    """
    with step("LEDGER", cfg=cfg) as s:
        positions = load_positions(cfg)
        frozen = _frozen_by_key(positions)

        rows = _position_rows(read_table(signals_csv_path(cfg, create=False)),
                              "signal", cfg, frozen)
        rows += _position_rows(read_table(on_demand_csv_path(cfg, create=False)),
                               ON_DEMAND_KEY, cfg, frozen)
        if not rows:
            s.status = "warn"
            s.detail = "no recorded signals to open"
            return positions

        before = len(positions)
        frame = merge_history_csv(
            positions_csv_path(cfg), rows, POSITION_KEYS,
            protect=mark_columns(horizons_of(cfg.get("portfolio", {}))))
        s.detail = (f"{len(rows)} recorded row(s); ledger {before} -> "
                    f"{len(frame)} position(s)")
        return frame
