"""What were the signals worth, and which recorded attribute predicted it?

One tidy CSV, one row per finding. The questions it exists to answer:

* what did the whole book return, and did it beat simply owning the benchmark?
* which screen paid, and did a `full` setup beat a `partial` one?
* did the tier-2 quality badge predict anything -- and *which rule* did the work?
* did the tier-3 deep dive add anything beyond the screen, and which of its
  quant dimensions carried it?

Every conclusion sentence is generated in Python from the numbers in its own
row. That is the same rule tier 3 follows for its chart and its figures: this
is a measurement, and a measurement narrated by a model is a measurement you
cannot check.

Two structural cautions are enforced rather than documented:

* **Ticker-level attributes are de-duplicated first.** A ticker that fired on
  two screens has two ledger rows and one set of fundamentals, so grouping by
  quality or by verdict without de-duplicating counts those names twice.
* **Significance is keyed off the FDR-adjusted q-value.** This file runs dozens
  of tests against one thin sample; ranking by raw p would reliably crown noise.
"""

import json
from datetime import date

import numpy as np
import pandas as pd

from backtest_universe import describe
from scanner_common import findings_csv_path, portfolio_dir, step

from .ledger import (
    IDENTITY_COLS,
    ON_DEMAND_KEY,
    QR_PREFIX,
    QUALITY_RULES_COL,
    VETO_RULES_COL,
    VT_PREFIX,
    as_bool,
    horizon_cols,
    horizons_of,
    load_positions,
    mark_columns,
    text_of,
)
from .marking import QUANT_PREFIX
from .stats import benjamini_hochberg, bootstrap_diff_ci, mann_whitney, spearman

FINDINGS_COLUMNS = [
    "analysis", "dimension", "cohort", "comparison", "horizon_days",
    "n", "n_other", "n_dates",
    "mean_ret_%", "median_ret_%", "win_rate_%", "std_%", "p10_%", "p90_%",
    "mean_excess_vs_bench_%", "mean_mfe_%", "mean_mae_%",
    "total_notional", "total_pnl",
    "effect", "effect_units", "prob_superior", "ci_lo", "ci_hi",
    "p_value", "q_value", "rank", "sufficient_n", "significant", "conclusion",
]

# Price levels and ledger bookkeeping: correlating "what did it cost" with
# "what did it return" is noise that would crowd the ranking.
# Tier 3's own scores. Graded as `dimension_impact` (correlation plus a
# tercile split) rather than lumped in with the recorded fundamentals, because
# "which deep-dive check was worth anything" is a question in its own right.
DIMENSION_COLS = {"quant_score", "Conviction"}

NOT_PREDICTORS = {"Close", "SMA", "Range High", "Price", "last_close",
                  "entry_price", "generated_at", "run_at", "opened_at",
                  "scan_date", "Report", "Thesis", "Company", "screen",
                  "Screens", "Missing", "Quality Missing", QUALITY_RULES_COL,
                  # Binary flags. A two-group rank test is the right question
                  # for these and they already get one; correlating a 0/1
                  # against the return would file the same finding twice under
                  # a weaker test and give it two votes in the FDR family.
                  "Quality", "quality_pass", "deep_dived", "vetoed",
                  "Veto", "Deep Veto", "Veto Reasons", "Deep Veto Reasons",
                  VETO_RULES_COL,
                  # The on-demand table's own copy of the quant score. The
                  # facts-derived `quant_score` is the same number and exists
                  # for signal rows too, so grading both would enter one score
                  # in the FDR family twice under two names. (`Upside %` and
                  # `P/E Pctile 2y` are NOT excluded -- the tier-3 refresh
                  # writes those into the same column the on-demand table uses,
                  # so there is only ever one of each, and both are real
                  # predictors worth grading.)
                  "Quant Score"}

CAVEATS = [
    "Paper trades, not fills: no commission, no slippage, no dividends, and "
    "every signal is bought at the next open regardless of size or liquidity.",
    "Trades overlap and cluster -- signals arrive in bunches on the same few "
    "days, so they are not independent samples and every p-value here is "
    "optimistic.",
    "Survivorship: the screens run over today's index membership, and a name's "
    "index is recorded as of the signal, not of the trade.",
    "Dozens of tests share one sample. Read q_value (FDR-adjusted), not "
    "p_value; 'significant' is keyed off q.",
    "Pending and open positions are recorded but excluded from every "
    "closed-trade statistic, per horizon.",
    "Per-rule quality flags are exploded from the failed-rule list recorded "
    "that night, against the rule set recorded with the position -- so "
    "retuning quality.parameters does not rewrite past findings.",
    "This is measurement of recorded signals, not investment advice.",
]


# --------------------------------------------------------------------------
# Shaping the ledger
# --------------------------------------------------------------------------

def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _latest_of_series(value):
    """The most recent value of a `[[year, value], ...]` cell.

    The multi-year statement metrics (FCF, OpM, PM) are stored as those pairs.
    The quality rules already compare the latest year, so an attribution over
    the same number keeps the two consistent.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("["):
            return np.nan
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return np.nan
    if isinstance(value, list) and value:
        last = value[-1]
        if isinstance(last, (list, tuple)) and len(last) == 2:
            return pd.to_numeric(last[1], errors="coerce")
    return np.nan


def _predictor(frame: pd.DataFrame, column: str) -> pd.Series:
    """One attribute column as numbers, unwrapping the year/value series."""
    values = _numeric(frame[column])
    if values.notna().sum() == 0:
        values = frame[column].map(_latest_of_series)
    return values


def dedup_by_ticker(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per (scan_date, ticker) -- the grain of every ticker-level
    attribute. A name that fired on two screens carries one set of
    fundamentals and one verdict; counting it twice would inflate exactly the
    cohorts a deep dive was most likely to touch."""
    if frame.empty or "ticker" not in frame.columns:
        return frame
    return frame.drop_duplicates(subset=["scan_date", "ticker"], keep="first")


def closed(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Positions with a settled return at this horizon.

    Per horizon, not globally: a position three weeks old has a real 10-day
    result and no 60-day one, and discarding the former would throw away the
    only evidence the ledger has for months.
    """
    col = horizon_cols(horizon)["ret"]
    if frame.empty or col not in frame.columns:
        return frame.iloc[0:0]
    return frame[_numeric(frame[col]).notna()]


# --------------------------------------------------------------------------
# Finding rows
# --------------------------------------------------------------------------

def _blank() -> dict:
    return {c: None for c in FINDINGS_COLUMNS}


def _stats_block(frame: pd.DataFrame, horizon: int) -> dict:
    """Descriptive statistics for one cohort, via the backtest's `describe`.

    Shared on purpose: a ledger mean and a `backtest_universe` mean are then
    literally the same computation over the same convention.
    """
    cols = horizon_cols(horizon)
    returns = _numeric(frame[cols["ret"]]) if cols["ret"] in frame else pd.Series(dtype=float)
    mfe = _numeric(frame[cols["mfe"]]) if cols["mfe"] in frame else None
    mae = _numeric(frame[cols["mae"]]) if cols["mae"] in frame else None
    stats = describe(returns, n_signals=len(frame),
                     n_dates=frame["scan_date"].nunique() if "scan_date" in frame else 0,
                     mfe=mfe, mae=mae)
    out = {
        "n": int(stats["evaluable"]),
        "n_dates": int(stats["distinct_dates"]),
        "mean_ret_%": stats["mean_%"], "median_ret_%": stats["median_%"],
        "win_rate_%": stats["win_rate_%"], "std_%": stats["std"],
        "p10_%": stats["p10"], "p90_%": stats["p90"],
        "mean_mfe_%": stats.get("mean_MFE_%"), "mean_mae_%": stats.get("mean_MAE_%"),
    }
    if cols["excess"] in frame:
        out["mean_excess_vs_bench_%"] = _numeric(frame[cols["excess"]]).mean()
    return out


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _pct(value, digits: int = 2) -> str:
    value = _num(value)
    return "n/a" if value is None else f"{value:+.{digits}f}%"


def _pp(value, digits: int = 2) -> str:
    """A difference of two percentages, in percentage points."""
    value = _num(value)
    return "n/a" if value is None else f"{value:+.{digits}f}pp"


def _rate(value) -> str:
    value = _num(value)
    return "n/a" if value is None else f"{value:.0f}%"


def _round(row: dict) -> dict:
    for key, value in row.items():
        if isinstance(value, (float, np.floating)):
            row[key] = None if not np.isfinite(value) else round(float(value), 4)
    return row


# --------------------------------------------------------------------------
# The analyses
# --------------------------------------------------------------------------

def _caveat_rows(positions: pd.DataFrame, horizons: list[int],
                 min_n: int) -> list[dict]:
    rows = []
    counts = ", ".join(f"{h}d n={len(closed(positions, h))}" for h in horizons)
    lead = (f"SAMPLE: {len(positions)} position(s) on file; settled returns: "
            f"{counts}. Findings need n>={min_n} per cohort to be called "
            f"significant; thinner ones are still reported, marked "
            f"sufficient_n=False.")
    for text in [lead] + CAVEATS:
        row = _blank()
        row.update({"analysis": "CAVEAT", "dimension": "read me",
                    "sufficient_n": False, "significant": False,
                    "conclusion": text})
        rows.append(row)
    return rows


def _portfolio_rows(positions: pd.DataFrame, horizons: list[int]) -> list[dict]:
    rows = []
    for horizon in horizons:
        book = closed(positions, horizon)
        if book.empty:
            continue
        row = _blank()
        row.update({"analysis": "portfolio", "dimension": "ALL positions",
                    "cohort": "ALL", "horizon_days": horizon})
        row.update(_stats_block(book, horizon))
        notional = _numeric(book["notional"]) if "notional" in book else pd.Series(dtype=float)
        returns = _numeric(book[horizon_cols(horizon)["ret"]])
        row["total_notional"] = notional.sum()
        row["total_pnl"] = float((notional * returns / 100).sum())
        row["conclusion"] = (
            f"{row['n']} closed position(s) held {horizon} trading days: mean "
            f"{_pct(row['mean_ret_%'])}, median {_pct(row['median_ret_%'])}, "
            f"{_rate(row['win_rate_%'])} winners, "
            f"{_pct(row.get('mean_excess_vs_bench_%'))} versus the benchmark "
            f"over the same bars; P&L {row['total_pnl']:+,.0f} on "
            f"{row['total_notional']:,.0f} deployed.")
        rows.append(_round(row))

    # The book as it stands right now -- the only number that exists before any
    # horizon has settled, and so the one worth reporting on night one.
    if "open_ret_%" in positions.columns:
        live = _numeric(positions["open_ret_%"]).dropna()
        if len(live):
            row = _blank()
            row.update({"analysis": "portfolio", "dimension": "ALL positions",
                        "cohort": "unrealized (mark to last close)",
                        "n": len(live), "mean_ret_%": live.mean(),
                        "median_ret_%": live.median(),
                        "win_rate_%": 100 * (live > 0).mean(),
                        "conclusion": (
                            f"{len(live)} filled position(s) marked to the last "
                            f"settled close: mean {_pct(live.mean())}, "
                            f"{_rate(100 * (live > 0).mean())} in profit. "
                            f"Unrealized and horizon-free -- not comparable to "
                            f"the fixed-horizon rows above.")})
            rows.append(_round(row))
    return rows


def _roadmap_rows(positions: pd.DataFrame, by_ticker: pd.DataFrame,
                  horizons: list[int], cfg_an: dict) -> list[dict]:
    """Every question this file exists to answer, and how far off each one is.

    A cohort with no settled returns produces no statistics, so without this
    the whole report would simply *omit* the question -- and a reader cannot
    tell an omitted question from one that came back empty. Reporting the
    shortfall keeps the shape of the analysis visible from night one, and stays
    useful later by showing which question is closest to answerable.
    """
    min_n = int(cfg_an.get("min_n", 20))
    deepest = max(horizons)
    rows = []

    def nothing_yet(label: str, note: str) -> None:
        """A question with no data at all, still named.

        The early return below is right for an attribute that simply is not on
        this ledger, but wrong for one nothing has *started* recording: the
        reader would see no row and could not tell the question from one that
        was never asked. Callers that know the column is new pass `always=True`.
        """
        row = _blank()
        row.update({
            "analysis": "roadmap", "dimension": label, "cohort": "none yet",
            "horizon_days": deepest, "n": 0, "sufficient_n": False,
            "significant": False,
            "conclusion": f"{label}: {note}",
        })
        rows.append(row)

    def question(label: str, frame: pd.DataFrame, column: str,
                 always: bool = False) -> None:
        if column not in frame.columns:
            if always:
                nothing_yet(label, "nothing recorded yet -- no position "
                                   "carries this attribute.")
            return
        groups = frame[column].map(text_of)
        recorded = {k: int(v) for k, v in groups.value_counts().items() if k}
        if len(recorded) < 2:
            if always:
                nothing_yet(label, "recorded on "
                                   f"{sum(recorded.values())} position(s) but "
                                   "only one group so far -- a split needs two.")
            return
        settled = closed(frame, deepest)
        have = ({k: int(v) for k, v in groups.loc[settled.index].value_counts().items() if k}
                if not settled.empty else {})
        short = {k: min_n - have.get(k, 0) for k in recorded
                 if have.get(k, 0) < min_n}
        row = _blank()
        row.update({
            "analysis": "roadmap", "dimension": label,
            "cohort": ", ".join(f"{k}={v}" for k, v in sorted(recorded.items())),
            "horizon_days": deepest, "n": sum(have.values()),
            "sufficient_n": not short, "significant": False,
            "conclusion": (
                f"{label}: recorded "
                + ", ".join(f"{k} x{v}" for k, v in sorted(recorded.items()))
                + f"; settled at {deepest}d "
                + (", ".join(f"{k} x{v}" for k, v in sorted(have.items()))
                   if have else "none yet")
                + ("." if not short else
                   "; still needs " + ", ".join(f"{v} more {k}"
                                                for k, v in sorted(short.items()))
                   + f" to reach n>={min_n} per side.")),
        })
        rows.append(row)

    question("which screen paid", positions, "config_key")
    question("full setup versus partial", positions, "Setup")
    question("did the tier-2 quality badge predict returns", by_ticker,
             "quality_pass")
    question("did a tier-3 deep dive predict returns", by_ticker, "deep_dived")
    question("which tier-3 verdict predicted returns", by_ticker, "Verdict")
    question("did excluding the vetoed names beat keeping them", by_ticker,
             "vetoed")
    for column in sorted(c for c in by_ticker.columns if c.startswith(QR_PREFIX)):
        question(f"quality rule {column[len(QR_PREFIX):]}", by_ticker, column)
    for column in sorted(c for c in by_ticker.columns if c.startswith(VT_PREFIX)):
        question(f"veto rule {column[len(VT_PREFIX):]}", by_ticker, column)

    # The enrichment agent. Listed even before a single position carries one,
    # because that is what this section is for: without it "did the agent's
    # stance predict anything" would simply be *missing* from the report, and
    # a reader could not tell that from asked-and-came-back-empty.
    question("did the agent's stance predict returns", by_ticker, "en_stance",
             always=True)
    question("did the agent's moat view predict returns", by_ticker,
             "en_moat_view", always=True)
    question("did the agent's social read predict returns", by_ticker,
             "en_social_sentiment", always=True)

    # The tier-3 dimensions are continuous, so the bar is "how many positions
    # carry a score", not a group split.
    for column in sorted(c for c in by_ticker.columns
                         if c.startswith(QUANT_PREFIX) or c in DIMENSION_COLS):
        settled = closed(by_ticker, deepest)
        have = int(_numeric(settled[column]).notna().sum()) if not settled.empty else 0
        recorded = int(_numeric(by_ticker[column]).notna().sum())
        row = _blank()
        row.update({
            "analysis": "roadmap", "dimension": f"deep-dive score {column}",
            "cohort": f"scored x{recorded}", "horizon_days": deepest, "n": have,
            "sufficient_n": have >= min_n, "significant": False,
            "conclusion": (
                f"deep-dive score {column}: {recorded} position(s) carry it, "
                f"{have} with a settled {deepest}d return"
                + ("." if have >= min_n else
                   f"; needs {min_n - have} more to be gradeable.")),
        })
        rows.append(row)

    if not any(r["analysis"] == "roadmap" and r["sufficient_n"] for r in rows):
        head = _blank()
        head.update({
            "analysis": "roadmap", "dimension": "READ ME", "rank": 0,
            "sufficient_n": False, "significant": False,
            "conclusion": (
                "No question below has enough settled trades yet. The ledger "
                "accumulates one scan at a time, so this section is the report "
                "until it does -- it lists every question the analysis will "
                "answer and what each one is still short of."),
        })
        rows.insert(0, head)
    return rows


def _cohort_rows(frame: pd.DataFrame, dimension: str, horizons: list[int],
                 label: str | None = None) -> list[dict]:
    rows = []
    if dimension not in frame.columns:
        return rows
    groups = frame[dimension].map(text_of)
    for horizon in horizons:
        for value in sorted(v for v in groups.unique() if v):
            cohort = closed(frame[groups == value], horizon)
            if cohort.empty:
                continue
            row = _blank()
            row.update({"analysis": "cohort", "dimension": label or dimension,
                        "cohort": value, "horizon_days": horizon})
            row.update(_stats_block(cohort, horizon))
            row["conclusion"] = (
                f"{label or dimension} = {value} at {horizon}d: n={row['n']}, "
                f"mean {_pct(row['mean_ret_%'])}, median "
                f"{_pct(row['median_ret_%'])}, {_rate(row['win_rate_%'])} winners.")
            rows.append(_round(row))
    return rows


def _split_row(a: pd.DataFrame, b: pd.DataFrame, horizon: int, dimension: str,
               label_a: str, label_b: str, analysis: str, cfg_an: dict,
               phrasing: str) -> dict | None:
    """One two-group comparison: does this attribute separate the returns?"""
    col = horizon_cols(horizon)["ret"]
    if col not in a.columns or col not in b.columns:
        return None
    va, vb = _numeric(a[col]).dropna(), _numeric(b[col]).dropna()
    if va.empty or vb.empty:
        return None

    test = mann_whitney(va, vb)
    lo, hi = bootstrap_diff_ci(va, vb, iters=int(cfg_an.get("bootstrap_iters", 2000)),
                               alpha=float(cfg_an.get("alpha", 0.05)))
    row = _blank()
    row.update({"analysis": analysis, "dimension": dimension, "cohort": label_a,
                "comparison": label_b, "horizon_days": horizon})
    row.update(_stats_block(a, horizon))
    row.update({
        "n_other": len(vb),
        "effect": float(va.mean() - vb.mean()),
        "effect_units": "percentage points of mean return",
        "prob_superior": test["prob_superior"],
        "ci_lo": lo, "ci_hi": hi, "p_value": test["p"],
    })
    min_n = int(cfg_an.get("min_n", 20))
    row["sufficient_n"] = bool(len(va) >= min_n and len(vb) >= min_n)
    row["conclusion"] = (
        f"{phrasing} at {horizon}d: {_pp(row['effect'])} of mean return "
        f"(n={len(va)} vs {len(vb)}"
        + (f", P(better)={test['prob_superior']:.2f}"
           if np.isfinite(test["prob_superior"]) else "")
        + (f", 95% CI {lo:+.2f} to {hi:+.2f}pp" if np.isfinite(lo) else "")
        + ").")
    return _round(row)


def _binary_split_rows(frame: pd.DataFrame, dimension: str, horizons: list[int],
                       cfg_an: dict, analysis: str = "split",
                       label: str | None = None,
                       yes: str = "True", no: str = "False",
                       invert: bool = False) -> list[dict]:
    """A True/False column as one two-group comparison.

    `invert` swaps which side leads, for a flag whose True means the *bad*
    outcome. `vetoed` is the case: reporting "clean minus excluded" makes a
    positive effect mean the exclusion earned its keep, which is the direction
    the thesis is stated in and the direction a reader will assume.
    """
    rows = []
    if dimension not in frame.columns:
        return rows
    flags = frame[dimension].map(as_bool)
    a, b = frame[flags.eq(True)], frame[flags.eq(False)]
    if invert:
        a, b = b, a
    if a.empty or b.empty:
        return rows
    name = label or dimension
    for horizon in horizons:
        row = _split_row(closed(a, horizon), closed(b, horizon), horizon, name,
                         yes, no, analysis, cfg_an,
                         f"{name}={yes} minus {name}={no}")
        if row:
            rows.append(row)
    return rows


def _category_split_rows(frame: pd.DataFrame, dimension: str,
                         horizons: list[int], cfg_an: dict,
                         label: str | None = None) -> list[dict]:
    """Each category of a column against the rest of it.

    A two-category column gets **one** comparison, not two: "A vs not-A" and
    "B vs not-B" are the same test with the sign flipped, and emitting both
    would double this column's weight in the FDR family and put the same
    finding in the ranking twice under two names.
    """
    rows = []
    if dimension not in frame.columns:
        return rows
    groups = frame[dimension].map(text_of)
    values = sorted(v for v in groups.unique() if v)
    if len(values) < 2:
        return rows
    name = label or dimension
    heads = values[:1] if len(values) == 2 else values
    for value in heads:
        others = f"not {value}" if len(values) > 2 else values[1]
        a, b = frame[groups == value], frame[groups.ne(value) & groups.ne("")]
        if a.empty or b.empty:
            continue
        for horizon in horizons:
            row = _split_row(closed(a, horizon), closed(b, horizon), horizon,
                             name, value, others, "split", cfg_an,
                             f"{name} {value} minus {others}")
            if row:
                rows.append(row)
    return rows


def _rule_rows(frame: pd.DataFrame, horizons: list[int], cfg_an: dict,
               prefix: str = QR_PREFIX, a_label: str = "passed",
               b_label: str = "failed",
               phrase: str = "passing the {rule} quality rule was worth"
               ) -> list[dict]:
    """Per rule: was the group flagged True worth more than the group flagged False?

    Parameterised over the prefix because the ledger carries two families of
    per-rule flags with **opposite polarity**: `qr_` is True when a quality rule
    passed, `vt_` is True when a veto tripped. Sharing the split arithmetic but
    naming the groups separately keeps each conclusion sentence honest.
    """
    rows = []
    for column in sorted(c for c in frame.columns if c.startswith(prefix)):
        rule = column[len(prefix):]
        flags = frame[column].map(as_bool)
        a, b = frame[flags.eq(True)], frame[flags.eq(False)]
        if a.empty or b.empty:
            continue
        for horizon in horizons:
            row = _split_row(closed(a, horizon), closed(b, horizon), horizon,
                             column, a_label, b_label, "rule_impact", cfg_an,
                             phrase.format(rule=rule))
            if row:
                rows.append(row)
    return rows


def _corr_rows(frame: pd.DataFrame, columns: list[str], horizons: list[int],
               cfg_an: dict, analysis: str) -> list[dict]:
    """Rank correlation of an attribute against the realized return."""
    rows = []
    min_n = int(cfg_an.get("min_n", 20))
    for column in columns:
        for horizon in horizons:
            cohort = closed(frame, horizon)
            if cohort.empty or column not in cohort.columns:
                continue
            x = _predictor(cohort, column)
            y = _numeric(cohort[horizon_cols(horizon)["ret"]])
            test = spearman(x, y)
            if not test["n"]:
                continue
            row = _blank()
            row.update({"analysis": analysis, "dimension": column,
                        "cohort": "ALL", "comparison": "rank correlation",
                        "horizon_days": horizon, "n": test["n"],
                        "effect": test["rho"], "effect_units": "Spearman rho",
                        "p_value": test["p"],
                        "sufficient_n": bool(test["n"] >= min_n)})
            direction = ("higher" if (test["rho"] or 0) > 0 else "lower")
            row["conclusion"] = (
                f"{column} vs {horizon}d return: rho="
                + (f"{test['rho']:+.3f}" if np.isfinite(test["rho"]) else "n/a")
                + f" over n={test['n']} -- "
                + (f"{direction} values went with better returns."
                   if np.isfinite(test["rho"]) and abs(test["rho"]) > 0.05
                   else "no monotonic relationship worth naming."))
            rows.append(_round(row))
    return rows


def _tercile_rows(frame: pd.DataFrame, columns: list[str], horizons: list[int],
                  cfg_an: dict) -> list[dict]:
    """Top third against bottom third of a continuous score.

    Reported next to the correlation because they fail differently: rho is
    blind to a threshold effect, and a tercile split is blind to a monotone
    trend that never separates the extremes.
    """
    rows = []
    for column in columns:
        for horizon in horizons:
            cohort = closed(frame, horizon)
            values = _predictor(cohort, column) if column in cohort.columns else pd.Series(dtype=float)
            values = values.dropna()
            if len(values) < 6 or values.nunique() < 3:
                continue
            lo, hi = values.quantile(1 / 3), values.quantile(2 / 3)
            top, bottom = cohort.loc[values[values >= hi].index], cohort.loc[values[values <= lo].index]
            if top.empty or bottom.empty:
                continue
            row = _split_row(top, bottom, horizon, column, "top third",
                             "bottom third", "dimension_impact", cfg_an,
                             f"the top third by {column} minus the bottom third")
            if row:
                rows.append(row)
    return rows


def _ranking_rows(rows: list[dict], cfg_an: dict) -> list[dict]:
    """The direct answer to "what mattered most", ranked and honest about it."""
    testable = [r for r in rows
                if r["analysis"] in ("split", "rule_impact", "dimension_impact",
                                     "metric_corr")
                and r.get("q_value") is not None and np.isfinite(r["q_value"])]
    if not testable:
        return []
    survivors = [r for r in testable if r.get("significant")]
    pool = survivors or testable
    # q first, then the raw p. Effect size is only the last tiebreak because
    # this list mixes units -- percentage points of return against a Spearman
    # rho -- and ordering 3.2pp above 0.31 rho would be comparing nothing.
    pool = sorted(pool, key=lambda r: (r.get("q_value") or 1.0,
                                       r.get("p_value") or 1.0,
                                       -abs(r.get("effect") or 0.0)))
    out = []
    for position, source in enumerate(pool[:15], start=1):
        row = _blank()
        row.update({k: source.get(k) for k in
                    ("dimension", "cohort", "comparison", "horizon_days", "n",
                     "n_other", "effect", "effect_units", "prob_superior",
                     "p_value", "q_value", "sufficient_n", "significant")})
        row.update({"analysis": "ranking", "rank": position})
        row["conclusion"] = (
            f"#{position} {'by FDR-adjusted significance' if survivors else 'candidate (NOTHING cleared FDR yet)'}: "
            f"{source.get('dimension')} ({source.get('cohort')} vs "
            f"{source.get('comparison')}) at {source.get('horizon_days')}d, "
            f"effect {source.get('effect')} {source.get('effect_units')}, "
            f"q={source.get('q_value')}."
            + ("" if survivors else " Treat as a lead to re-test, not a result.")
            # The ranking is the section a reader trusts most, so it repeats
            # the sample warning rather than inheriting it from a row above.
            + ("" if source.get("sufficient_n") else
               f" INSUFFICIENT SAMPLE -- needs n>="
               f"{int(cfg_an.get('min_n', 20))} per side."))
        out.append(row)
    if not survivors:
        row = _blank()
        row.update({"analysis": "ranking", "dimension": "VERDICT", "rank": 0,
                    "sufficient_n": False, "significant": False,
                    "conclusion": (
                        "No attribute separates returns at the configured FDR "
                        "yet. With this sample that is the expected answer, not "
                        "a null result -- the ranking below lists the leads in "
                        "q order so they can be re-tested as the ledger grows.")})
        out.insert(0, row)
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _predictor_columns(frame: pd.DataFrame, horizons: list[int]) -> list[str]:
    """Every recorded attribute worth correlating against the return."""
    reserved = set(IDENTITY_COLS) | set(mark_columns(horizons)) | NOT_PREDICTORS
    out = []
    for column in frame.columns:
        if column in reserved or column.startswith(QR_PREFIX) \
                or column.startswith(VT_PREFIX):
            continue
        if column.startswith(QUANT_PREFIX) or column in DIMENSION_COLS:
            continue          # graded as dimension_impact instead
        values = _predictor(frame, column)
        if values.notna().sum() >= 3 and values.nunique() > 1:
            out.append(column)
    return sorted(out)


def analyze(cfg: dict, baseline: bool = True) -> pd.DataFrame:
    """Grade every recorded attribute against realized return; write the CSV."""
    with step("ANALYZE", cfg=cfg) as s:
        port_cfg = cfg.get("portfolio", {})
        cfg_an = port_cfg.get("analysis", {})
        horizons = horizons_of(port_cfg)
        positions = load_positions(cfg)
        if positions.empty:
            s.status = "warn"
            s.detail = "ledger is empty -- nothing to analyze"
            return pd.DataFrame(columns=FINDINGS_COLUMNS)

        by_ticker = dedup_by_ticker(positions)
        # An ad-hoc look has no technical trigger, so it is not a competitor in
        # "which screen paid" -- entering it there would put a non-screen in a
        # screen comparison. It still appears in the `source` cohort, which is
        # the question it actually answers.
        screened = positions[positions["config_key"].map(text_of)
                             != ON_DEMAND_KEY]

        rows = _caveat_rows(positions, horizons, int(cfg_an.get("min_n", 20)))
        rows += _portfolio_rows(positions, horizons)
        rows += _roadmap_rows(screened, by_ticker, horizons, cfg_an)

        # -- tier 1: the screens (every row; a two-screen ticker is two signals)
        rows += _cohort_rows(screened, "config_key", horizons, "screen")
        rows += _cohort_rows(screened, "Setup", horizons)
        rows += _cohort_rows(positions, "source", horizons)
        rows += _category_split_rows(screened, "config_key", horizons, cfg_an,
                                     "screen")
        if "Setup" in screened.columns:
            tiers = screened["Setup"].map(text_of)
            for horizon in horizons:
                row = _split_row(closed(screened[tiers == "full"], horizon),
                                 closed(screened[tiers == "partial"], horizon),
                                 horizon, "Setup", "full", "partial", "split",
                                 cfg_an, "a full setup minus a partial one")
                if row:
                    rows.append(row)

        # -- tier 2 and tier 3: ticker-level, so de-duplicated first
        rows += _cohort_rows(by_ticker, "quality_pass", horizons, "quality badge")
        rows += _cohort_rows(by_ticker, "Verdict", horizons, "tier-3 verdict")
        rows += _binary_split_rows(by_ticker, "quality_pass", horizons, cfg_an,
                                   label="quality badge",
                                   yes="passed", no="failed")
        rows += _binary_split_rows(by_ticker, "deep_dived", horizons, cfg_an,
                                   label="deep-dived", yes="yes", no="no")
        rows += _category_split_rows(by_ticker, "Verdict", horizons, cfg_an,
                                     "tier-3 verdict")
        rows += _rule_rows(by_ticker, horizons, cfg_an)

        # -- the exclusion thesis: was excluding the losers worth anything? --
        # This is the only test in the file that grades the veto layer, and it
        # is the whole reason vetoed signals are still bought and tracked.
        rows += _cohort_rows(by_ticker, "vetoed", horizons, "veto")
        rows += _binary_split_rows(by_ticker, "vetoed", horizons, cfg_an,
                                   label="veto", yes="clean", no="excluded",
                                   invert=True)
        rows += _rule_rows(by_ticker, horizons, cfg_an, prefix=VT_PREFIX,
                           a_label="tripped", b_label="clean",
                           phrase="tripping the {rule} veto was worth")

        # -- the enrichment agent: was its judgment worth anything? --
        # The agent cannot move a score -- there is no adjustment field -- so
        # recording its stance and grading it here is the only way to find out
        # whether qualitative research adds anything to the quant read. Same
        # treatment the tier-3 verdict gets, for the same reason: a categorical
        # judgment is graded by splitting on it, never by correlating it.
        # `en_sources_n` / `en_concerns_n` need no wiring -- they are ordinary
        # numeric attributes and `_predictor_columns` picks them up.
        for column, label in (("en_stance", "agent stance"),
                              ("en_moat_view", "agent moat view"),
                              ("en_social_sentiment", "agent social read")):
            rows += _cohort_rows(by_ticker, column, horizons, label)
            rows += _category_split_rows(by_ticker, column, horizons, cfg_an,
                                         label)

        quant_cols = [c for c in by_ticker.columns
                      if c.startswith(QUANT_PREFIX) or c in DIMENSION_COLS]
        rows += _corr_rows(by_ticker, sorted(quant_cols), horizons, cfg_an,
                           "dimension_impact")
        rows += _tercile_rows(by_ticker, sorted(quant_cols), horizons, cfg_an)
        rows += _corr_rows(by_ticker,
                           _predictor_columns(by_ticker, horizons),
                           horizons, cfg_an, "metric_corr")

        if baseline:
            rows += _baseline_rows(positions, horizons, cfg)

        # One FDR family over every test in the file: they all share the sample.
        alpha = float(cfg_an.get("alpha", 0.05))
        use_fdr = bool(cfg_an.get("fdr", True))
        q_values = benjamini_hochberg([r.get("p_value") for r in rows])
        for row, q in zip(rows, q_values):
            if row.get("p_value") is None:
                continue
            row["q_value"] = None if not np.isfinite(q) else round(q, 6)
            decisive = row["q_value"] if use_fdr else row["p_value"]
            row["significant"] = bool(row.get("sufficient_n")
                                      and decisive is not None
                                      and decisive <= alpha)
            if not row.get("sufficient_n"):
                row["conclusion"] = (
                    f"{row['conclusion']} INSUFFICIENT SAMPLE -- needs n>="
                    f"{int(cfg_an.get('min_n', 20))} per side; reported so the "
                    f"question is visible, not because it is answered.")
            elif not row["significant"]:
                row["conclusion"] = f"{row['conclusion']} Not significant at FDR {alpha}."
            else:
                row["conclusion"] = f"{row['conclusion']} SIGNIFICANT at FDR {alpha}."

        rows += _ranking_rows(rows, cfg_an)

        findings = pd.DataFrame(rows, columns=FINDINGS_COLUMNS)
        path = findings_csv_path(cfg)
        findings.to_csv(path, index=False, encoding="utf-8")
        if cfg_an.get("keep_dated_findings", True):
            dated = portfolio_dir(cfg) / f"findings_{date.today().isoformat()}.csv"
            findings.to_csv(dated, index=False, encoding="utf-8")
        tested = int(findings["p_value"].notna().sum())
        s.detail = (f"{len(findings)} finding(s), {tested} test(s), "
                    f"{int(findings['significant'].fillna(False).sum())} significant "
                    f"-> {path.name}")
        return findings


def _baseline_rows(positions: pd.DataFrame, horizons: list[int],
                   cfg: dict) -> list[dict]:
    """The random-entry bar, borrowed from the universe backtest.

    `backtest_universe.baseline_stats` over its cached panel is "what any
    stock-day returned over the same horizon". A screen that matches it added
    nothing; the ledger's own benchmark column answers a different question
    (versus the index), so both are worth having. Silently skipped when the
    cache is absent -- it is a reference point, never a dependency.
    """
    try:
        import backtest_universe as bu
        path = bu.cache_path(cfg["backtest"])
        if not path.exists():
            return []
        from scanner_common import drop_unsettled_bars
        panel = drop_unsettled_bars(pd.read_pickle(path))
        benchmark = cfg["backtest"].get("benchmark_ticker", "SPY")
        universe = [t for t in panel["Close"].columns if t != benchmark]
        start = panel.index[-1] - pd.DateOffset(years=int(cfg["backtest"].get("years", 3)))
    except Exception as exc:                       # noqa: BLE001
        print(f"  baseline skipped: {exc}")
        return []

    rows = []
    for horizon in horizons:
        try:
            trades = bu.forward_trades(panel, horizon,
                                       cfg["portfolio"].get("entry", "next_open"),
                                       False, delay=0, dates=False)
            base = bu.baseline_stats(trades, start, universe, False)
        except Exception as exc:                   # noqa: BLE001
            print(f"  baseline skipped at {horizon}d: {exc}")
            continue
        book = closed(positions, horizon)
        row = _blank()
        row.update({"analysis": "baseline", "dimension": "random entry",
                    "cohort": f"every stock-day, {horizon}d",
                    "horizon_days": horizon, "n": int(base["evaluable"]),
                    "mean_ret_%": base["mean_%"], "median_ret_%": base["median_%"],
                    "win_rate_%": base["win_rate_%"], "std_%": base["std"],
                    "p10_%": base["p10"], "p90_%": base["p90"],
                    "sufficient_n": True, "significant": False})
        if not book.empty:
            mean = _numeric(book[horizon_cols(horizon)["ret"]]).mean()
            row["effect"] = float(mean - base["mean_%"])
            row["effect_units"] = "percentage points versus random entry"
        row["conclusion"] = (
            f"Random entry over {horizon} trading days across the cached "
            f"universe returned {_pct(base['mean_%'])} on average "
            f"({base['win_rate_%']:.0f}% winners). That is the bar any screen "
            f"has to clear"
            + (f"; the ledger is {_pp(row['effect'])} against it."
               if row.get("effect") is not None else "."))
        rows.append(_round(row))
    return rows
