"""The unified quality check -- one config-driven engine for tiers 2 and 3.

Everything the pipeline knows about "is this a good company" lives in the
`quality` section of `config.json` and is evaluated here. There used to be two
separate systems that shared no keys, no config shape and no code path:

  * `fundamentals.quality.rules` -- a strict pass/fail gate over Yahoo `info`
    and the annual statements, producing the tier-2 badge; and
  * `research.synthesis.dimensions` -- a weighted 0-100 score over a different
    13 metrics, producing tier 3's quant anchor.

They are one registry now. A **parameter** is one measurable thing about a
company. It declares where its value comes from (`source`), which weighted
bucket it belongs to (`group`), when it is affordable to collect (`stage`), and
optionally a `gate` (pass/fail) and/or a `score` (good/bad anchors). One
evaluation therefore yields **both** outputs at once: the badge is "every
enabled gate passed", the score is the weighted mean of the enabled anchors.

The load-bearing rule is `enabled`:

    "dividendYield": {"enabled": false, ...}

A disabled parameter is **invisible** -- not gated, not scored, not listed in
`Quality Missing`, not rendered as an embed field, and not counted in any
weight. That is the whole point: a company with no dividend should be able to
earn the badge by switching one flag, not by deleting the rule and losing the
record that it ever existed.

Two distinctions that look like details and are not:

  * **Missing is not failing.** A missing value *fails its gate* (unverifiable
    quality does not earn the badge -- unchanged from the old rule engine) but
    is *skipped* in the score. A whole group with no data scores a neutral 0.5
    rather than 0, so a bank with no operating income is not driven to the
    bottom by data Yahoo simply does not publish for it.
  * **Not evaluated is not failed.** With the layer off, `annotate` writes no
    columns at all and `evaluate` reports `passed=None`. Downstream readers
    (`research_report._has_fundamentals`, `ledger._quality_rule_flags`) already
    depend on that difference.

A parameter may also carry `veto: true`, which makes it an **exclusion rule**
rather than a quality gate -- the "identify the losers" half of the thesis. A
veto answers a much narrower question than the badge ("is this company visibly
falling over?" rather than "is this a good company?") and is meant to fire
rarely, so it is kept deliberately apart:

  * a veto parameter is **skipped by `gate_failures`** -- the badge is already
    strict enough that most tickers fail one of its gates, and letting vetoes
    into it would make the ⭐ mean two different things at once;
  * **a missing value never vetoes.** This is the exact inverse of the gate
    rule above, and it is the single most load-bearing line in the layer.
    Unverifiable quality does not earn the badge, but unverifiable is not
    *proof of disaster* -- and Yahoo leaves holes in nearly every company's
    statements. A veto that fired on missing data would exclude most of the
    universe and be worse than no veto at all.

The second rule has a sharp edge worth stating: `scalar` rejects `bool`, so a
flag stored as `True`/`False` reads as *missing* and therefore never vetoes.
Every flag a resolver produces must be an **int 0/1**. `validate` cannot catch
this (it sees config, not values), so `tests/test_quality.py` pins it instead.

This module does the *math*; `collect` does the I/O and is deliberately the
only part that touches the network. `charts.py` keeps the same discipline.
"""

import json
import math
import time
from dataclasses import dataclass, field

import pandas as pd
import yfinance as yf

from scanner_common import (
    COMPANY_COL,
    QUALITY_COL,
    QUALITY_MISSING_COL,
    VETO_COL,
    VETO_REASONS_COL,
    fmt_compact,
    fmt_value,
    log_step,
    stmt_value,
)

CONFIG_KEY = "quality"

STAGE_FAST = "fast"     # yf.Ticker.info + annual statements -- every tier-1 hit
STAGE_DEEP = "deep"     # collect_yahoo / IBKR -- gated candidates only
STAGES = (STAGE_FAST, STAGE_DEEP)

# A gate may only use these. Enforced by `validate` so a typo in config is a
# refusal rather than a rule that silently never fires.
GATE_KEYS = ("min", "max", "increasing")

# The two axes of the risk/reward plane. Every group declares one, and the pair
# is what a quadrant decision reads -- `AXIS_REWARD` is expected return,
# `AXIS_RISK` is the probability of permanent loss. They are deliberately NOT
# blended into each other: a single number cannot say whether a 53 means
# "mid risk, mid reward" or "high reward, high risk", which is precisely the
# distinction a quadrant exists to draw. The blended `score` survives alongside
# them because every recorded conviction is denominated in it.
AXIS_REWARD = "reward"
AXIS_RISK = "risk"
AXES = (AXIS_REWARD, AXIS_RISK)


@dataclass
class QualityResult:
    """One ticker's verdict under the rules in force at evaluation time."""

    passed: bool | None = None          # None = the layer never ran
    failed: list[str] = field(default_factory=list)
    score: float | None = None          # 0-100, None when nothing was scorable
    groups: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)
    parameters: list[str] = field(default_factory=list)   # the enabled keys
    vetoed: bool | None = None          # None = the layer never ran
    veto_reasons: list[str] = field(default_factory=list)
    # The risk/reward coordinates. `risk` is 100 - `safety`, i.e. high = bad,
    # so it reads as a chart axis; None on either means "not measured".
    reward: float | None = None
    safety: float | None = None
    risk: float | None = None

    def as_dict(self) -> dict:
        return {"passed": self.passed, "failed": list(self.failed),
                "score": self.score, "groups": self.groups,
                "values": self.values, "parameters": list(self.parameters),
                "vetoed": self.vetoed, "veto_reasons": list(self.veto_reasons),
                "reward": self.reward, "safety": self.safety,
                "risk": self.risk}


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

def section(cfg: dict) -> dict:
    """The `quality` config section (empty dict when absent)."""
    return cfg.get(CONFIG_KEY) or {}


def is_enabled(cfg: dict) -> bool:
    """Whether the quality layer runs at all."""
    return bool(section(cfg).get("enabled"))


def badge(cfg: dict) -> str:
    return section(cfg).get("badge", "")


def veto_badge(cfg: dict) -> str:
    return section(cfg).get("veto_badge", "")


def veto_tier(cfg: dict) -> str:
    """The tier label a vetoed candidate is forced to, whatever it scored."""
    return section(cfg).get("veto_tier", "AVOID")


def veto_text(reasons: list, cfg: dict) -> str:
    """Veto keys rendered as readable labels for a card or a report.

    Uses each parameter's display label, so the alert says "Altman Z, FCF neg
    yrs" rather than leaking internal keys -- and falls back to the key when a
    rule has since been renamed or deleted, because an archived row can name a
    parameter the registry no longer has.
    """
    specs = parameters(cfg, None, enabled_only=False)
    labels_ = [label_of(k, specs.get(k) or {}) for k in (reasons or [])]
    return ", ".join(labels_) if labels_ else "none"


def parameters(cfg: dict, stage: str | None = None,
               enabled_only: bool = True) -> dict:
    """The parameter registry, filtered.

    `stage=None` means every stage. `enabled_only=False` is for the config
    tooling, which has to show you the switch you are about to flip.
    """
    out = {}
    for key, spec in (section(cfg).get("parameters") or {}).items():
        if not isinstance(spec, dict):
            continue
        if enabled_only and not spec.get("enabled", True):
            continue
        if stage is not None and spec.get("stage", STAGE_FAST) != stage:
            continue
        out[key] = spec
    return out


def groups(cfg: dict, enabled_only: bool = True) -> dict:
    out = {}
    for name, spec in (section(cfg).get("groups") or {}).items():
        if not isinstance(spec, dict):
            continue
        if enabled_only and not spec.get("enabled", True):
            continue
        out[name] = spec
    return out


def label_of(key: str, spec: dict) -> str:
    """The display label -- the DataFrame column and Discord field name.

    Labels are what `signals.csv` and `latest_hits.json` are keyed by, so they
    are part of the on-disk contract; the internal key is what a gate, a score
    and `Quality Missing` refer to. Renaming a label is safe, renaming a key is
    not -- exactly the split the old `_rule_value` maintained.
    """
    return spec.get("label") or key


def labels(cfg: dict, stage: str | None = None) -> dict:
    """key -> display label, for the enabled parameters of `stage`."""
    return {k: label_of(k, s) for k, s in parameters(cfg, stage).items()}


def sources_needed(cfg: dict, stage: str | None = None) -> set:
    """Which resolver prefixes the enabled parameters of `stage` require."""
    out = set()
    for spec in parameters(cfg, stage).values():
        source = spec.get("source") or ""
        if "." in source:
            out.add(source.split(".", 1)[0])
    return out


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------

def _is_num(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _series(value):
    """`[(year, value)]` when the value is a multi-year series, else None.

    Survives a JSON round-trip, where the tuples come back as lists -- which is
    what lets `quality_failures`' successor grade an *archived* scan long after
    the config that produced it has moved on.
    """
    if not isinstance(value, list) or not value:
        return None
    pairs = [p for p in value if isinstance(p, (list, tuple)) and len(p) == 2]
    return pairs or None


def scalar(value, spec: dict | None = None):
    """The comparable number behind a raw value, or None when there isn't one.

    Unwraps a multi-year series to its latest year. `zero_is_missing` exists
    for Yahoo's bank artifact: it reports `grossMargins` as a hard `0.0` rather
    than omitting it, which is numeric enough to score a company at the very
    bottom of a metric that simply does not apply to it.
    """
    series = _series(value)
    if series is not None:
        value = series[-1][1]
    if isinstance(value, bool) or value is None:
        return None
    if not _is_num(value):
        return None
    if spec and spec.get("zero_is_missing") and value == 0:
        return None
    return float(value)


def row_values(row, cfg: dict, stage: str | None = None) -> dict:
    """Read a label-keyed row (or dict, or Series) back as key-keyed values.

    The hits frame, `latest_hits.json` and `signals.csv` all store fundamentals
    under their display labels; every gate and score here is keyed by the
    internal key. This is the one place that bridges the two.
    """
    out = {}
    for key, spec in parameters(cfg, stage).items():
        label = label_of(key, spec)
        value = row.get(label) if hasattr(row, "get") else None
        if isinstance(value, float) and pd.isna(value):
            value = None
        out[key] = value
    return out


def has_values(row, cfg: dict, stage: str | None = None) -> bool:
    """Whether this row carries anything to grade.

    Distinguishes "fundamentals were switched off that night" from "this
    company failed everything". Without it, re-grading an ungraded row would
    invent a verdict the pipeline never reached.
    """
    return any(v is not None for v in row_values(row, cfg, stage).values())


# --------------------------------------------------------------------------
# Gates -- the badge
# --------------------------------------------------------------------------

def _gate_ok(raw, spec: dict, gate: dict) -> bool | None:
    """Does this value satisfy its gate? `None` when there is no value.

    Split out so the badge and the veto can share one definition of the gate
    arithmetic while disagreeing -- deliberately -- about what a missing value
    means. `min`/`max` are **strict** compares; `increasing` needs at least two
    fiscal years with the latest above the previous.
    """
    value = scalar(raw, spec)
    if value is None:
        return None
    ok = True
    if "min" in gate and gate["min"] is not None:
        ok = value > gate["min"]
    if ok and "max" in gate and gate["max"] is not None:
        ok = value < gate["max"]
    if ok and gate.get("increasing"):
        series = _series(raw)
        ok = (series is not None and len(series) >= 2
              and _is_num(series[-1][1]) and _is_num(series[-2][1])
              and series[-1][1] > series[-2][1])
    return ok


def gate_failures(values: dict, cfg: dict, stage: str | None = None) -> list[str]:
    """Which enabled gates this ticker fails, by parameter key.

    Semantics are unchanged from the rule engine this replaces: `min`/`max` are
    **strict** compares, `increasing` needs at least two fiscal years with the
    latest above the previous, and **a missing value fails** -- quality that
    cannot be verified does not earn the badge.

    `veto` parameters are skipped: they are exclusion rules, not quality gates,
    and are answered by `veto_failures` under the opposite missing-value rule.
    """
    failed = []
    for key, spec in parameters(cfg, stage).items():
        gate = spec.get("gate")
        if not gate or spec.get("veto"):
            continue
        if _gate_ok(values.get(key), spec, gate) is not True:
            failed.append(key)
    return failed


# --------------------------------------------------------------------------
# Vetoes -- the exclusion half
# --------------------------------------------------------------------------

def veto_parameters(cfg: dict, stage: str | None = None) -> dict:
    """The enabled `veto: true` parameters that actually carry a gate."""
    return {k: s for k, s in parameters(cfg, stage).items()
            if s.get("veto") and s.get("gate")}


def veto_failures(values: dict, cfg: dict, stage: str | None = None) -> list[str]:
    """Which veto rules this ticker trips, by parameter key. Empty = keep it.

    The gate arithmetic is `gate_failures`', but the missing-value rule is
    **inverted**: a value we could not collect does not veto. Unverifiable
    quality does not earn the badge; unverifiable is not proof of disaster, and
    Yahoo leaves holes in nearly every company's statements. A veto that fired
    on missing data would exclude most of the universe.

    The practical consequence worth remembering: an EDGAR outage, a logged-out
    gateway or a statement Yahoo does not publish can only ever make this layer
    *quieter*, never trigger-happy. It fails open, on purpose.
    """
    return [key for key, spec in veto_parameters(cfg, stage).items()
            if _gate_ok(values.get(key), spec, spec["gate"]) is False]


# --------------------------------------------------------------------------
# Anchors -- the 0-100 score
# --------------------------------------------------------------------------

def normalize(value: float, good: float, bad: float) -> float:
    """Linear map to [0,1]: good->1, bad->0, clamped.

    Direction comes from the *ordering* of good and bad, so an inverted metric
    (`net_debt_to_ebitda` good=0 bad=4) needs no special case.
    """
    if good == bad:
        return 0.5
    return max(0.0, min(1.0, (value - bad) / (good - bad)))


def axis_of(gspec: dict) -> str:
    """Which axis a group scores on. Unmarked groups are reward.

    Defaulting rather than requiring keeps every pre-axis config readable: a
    group nobody has classified yet still contributes to the blended score
    exactly as before, and only the risk axis needs opting into.
    """
    axis = (gspec.get("axis") or AXIS_REWARD)
    return axis if axis in AXES else AXIS_REWARD


def _combine(parts: list, pw: list, gspec: dict) -> float:
    """Reduce one group's normalized metrics to a single 0-1 score.

    `mean` is the default and what every group did before axes existed.
    `{"worst_k": n}` averages the **n lowest** readings instead, which is the
    aggregation the risk axis needs: averaging 19 distress metrics lets one
    catastrophic reading be washed out by eighteen benign ones, so a company
    visibly failing two tests scores almost the same as one failing none. Taking
    the worst few is the same argument that makes a veto a veto rather than a
    deduction -- risk is about the worst thing true of a company, not the
    average thing.

    Fewer than `n` participating metrics is not a problem: it reduces to the
    mean of what there is, which is already the most pessimistic read available.
    """
    agg = gspec.get("aggregate") or "mean"
    if isinstance(agg, dict) and _is_num(agg.get("worst_k")):
        k = max(1, int(agg["worst_k"]))
        order = sorted(range(len(parts)), key=lambda i: parts[i])[:k]
        parts = [parts[i] for i in order]
        pw = [pw[i] for i in order]
    return sum(p * w for p, w in zip(parts, pw)) / sum(pw)


def group_scores(values: dict, cfg: dict, stage: str | None = None) -> dict:
    """Per-group 0-1 scores -- the one pass the blend and both axes share.

    Two kinds of emptiness, deliberately treated differently:

      * a group with **no participating parameters at this stage** is dropped
        entirely -- at `fast` there is no estimate data to be neutral *about*,
        and folding in a 0.5 would drag every score toward the middle for no
        reason;
      * a group that has parameters but no *values* scores a neutral **0.5**,
        which is the rule that keeps banks from being zeroed by statement rows
        Yahoo does not publish for them.
    """
    breakdown = {}
    for name, gspec in groups(cfg).items():
        members = {k: s for k, s in parameters(cfg, stage).items()
                   if s.get("group") == name and s.get("score")}
        if not members:
            continue                      # nothing to be neutral about
        parts, pw = [], []
        for key, spec in members.items():
            value = scalar(values.get(key), spec)
            if value is None:
                continue
            anchors = spec["score"]
            weight = float(spec.get("weight", 1.0) or 0.0)
            if weight <= 0:
                continue
            parts.append(normalize(value, anchors["good"], anchors["bad"]))
            pw.append(weight)
        gscore = _combine(parts, pw, gspec) if parts else 0.5
        breakdown[name] = {"score": round(gscore, 3),
                           "weight": float(gspec.get("weight", 0.0) or 0.0),
                           "axis": axis_of(gspec),
                           "metrics_used": len(parts),
                           "metrics_total": len(members)}
    return breakdown


def _weighted(breakdown: dict, names=None) -> float | None:
    """Weighted mean of `breakdown`'s group scores, renormalized over members.

    Renormalizing is what makes switching a group off, or an axis holding one
    group, behave the same as any other configuration -- no weight has to sum
    to anything in particular.
    """
    rows = [g for n, g in breakdown.items() if names is None or n in names]
    wsum = sum(g["weight"] for g in rows)
    if not wsum:
        return None
    return round(100 * sum(g["weight"] * g["score"] for g in rows) / wsum, 1)


def score_of(values: dict, cfg: dict, stage: str | None = None):
    """The blended 0-100 score and its per-group breakdown.

    Weights renormalize over what actually participated, so switching one
    parameter off never silently reweights the rest.
    """
    breakdown = group_scores(values, cfg, stage)
    return _weighted(breakdown), breakdown


def axis_scores(values: dict, cfg: dict, stage: str | None = None) -> dict:
    """The risk/reward coordinates: `reward`, `safety`, `risk`, 0-100 each.

    **Polarity, stated explicitly because a silent sign flip here would be a
    quiet catastrophe.** Every `normalize` call maps good->1, so the risk
    group's composite is a *safety* reading -- high means healthy. `risk` is
    its complement, so it reads the way a chart axis labelled "risk" must:
    high means dangerous. Both are returned rather than just one, so no caller
    has to remember which direction the underlying metrics ran.

    A `None` axis means no group on it had anything scorable -- "not measured",
    never zero. Plotting a missing axis as 0 would put an unmeasurable company
    in the best quadrant.
    """
    return _axes_from(group_scores(values, cfg, stage))


def _axes_from(breakdown: dict) -> dict:
    """Split an existing group breakdown into axis coordinates.

    Separate from `axis_scores` so `evaluate` can produce the blend and both
    axes from **one** pass over the registry rather than three.
    """
    by_axis = {axis: {n for n, g in breakdown.items() if g["axis"] == axis}
               for axis in AXES}
    reward = _weighted(breakdown, by_axis[AXIS_REWARD])
    safety = _weighted(breakdown, by_axis[AXIS_RISK])
    return {"reward": reward,
            "safety": safety,
            "risk": None if safety is None else round(100 - safety, 1),
            "groups": breakdown}


# --------------------------------------------------------------------------
# One call, both verdicts
# --------------------------------------------------------------------------

def evaluate(values: dict, cfg: dict, stage: str | None = None) -> QualityResult:
    """Grade one ticker: the badge, the score, and everything behind them.

    A no-op returning `passed=None` when the layer is off -- callers must read
    that as "not evaluated", never as a failure.
    """
    if not is_enabled(cfg):
        return QualityResult()
    specs = parameters(cfg, stage)
    failed = gate_failures(values, cfg, stage)
    vetoes = veto_failures(values, cfg, stage)
    breakdown = group_scores(values, cfg, stage)
    axes = _axes_from(breakdown)
    return QualityResult(
        passed=not failed,
        failed=failed,
        score=_weighted(breakdown),
        groups=breakdown,
        values={k: values.get(k) for k in specs},
        parameters=list(specs),
        vetoed=bool(vetoes),
        veto_reasons=vetoes,
        reward=axes["reward"],
        safety=axes["safety"],
        risk=axes["risk"],
    )


def tier_for(conviction: float, cfg: dict) -> str:
    """Map a 0-100 score to a tier label via the configured bands."""
    tiers = sorted(section(cfg).get("tiers") or [], key=lambda t: -t["min"])
    if not tiers:
        return ""
    for band in tiers:
        if conviction >= band["min"]:
            return band["label"]
    return tiers[-1]["label"]


def tier_color(tier: str, cfg: dict, default: int = 0x2A78D6) -> int:
    for band in section(cfg).get("tiers") or []:
        if band.get("label") == tier:
            try:
                return int(str(band.get("color", "")), 16)
            except ValueError:
                return default
    return default


def annotate(hits: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Record the verdict on a hits frame, mirroring the Setup/Missing pair.

    Adds `Quality` (passed every enabled gate?) and `Quality Missing` (the keys
    that failed, `[]` when it passed), plus the exclusion pair `Veto` and
    `Veto Reasons` over the `fast`-stage veto rules. Graded **exactly once per
    scan**, here, so the Discord badge and the tier-3 hand-off read one recorded
    answer and cannot disagree -- the bug this arrangement exists to prevent was
    the badge being computed inside `build_embeds`, which runs *after* the
    hand-off is written.

    Only the `fast` half of the veto set is answerable here; the `deep` rules
    (SEC filing flags, Beneish, the liquidity pair) need collectors that only
    run for gated candidates, and land later as `Deep Veto`.

    A no-op when the layer is off: the columns stay *absent*, which downstream
    readers must treat as "not evaluated". Mutates and returns `hits`.
    """
    if hits.empty or not is_enabled(cfg):
        return hits
    graded = [row_values(row, cfg, STAGE_FAST) for _, row in hits.iterrows()]
    failures = [gate_failures(v, cfg, STAGE_FAST) for v in graded]
    vetoes = [veto_failures(v, cfg, STAGE_FAST) for v in graded]
    hits[QUALITY_COL] = [not f for f in failures]
    hits[QUALITY_MISSING_COL] = pd.Series(failures, index=hits.index, dtype=object)
    hits[VETO_COL] = [bool(v) for v in vetoes]
    hits[VETO_REASONS_COL] = pd.Series(vetoes, index=hits.index, dtype=object)
    return hits


def verdict_of(row, cfg: dict) -> tuple[bool | None, list]:
    """Re-grade a recorded row against the rules in force **now**.

    `config.json` is retuned between scans, and a gate answering with last
    night's bar silently ignores the change until the next scan -- loosening a
    rule and watching the candidate list still report the old failures is
    genuinely confusing. `output/history/` keeps the original verdict, so
    nothing archived is rewritten by this.

    Falls back to the recorded verdict when there is nothing to re-grade, and
    reports `(None, [])` when the layer never ran at all.
    """
    if is_enabled(cfg) and has_values(row, cfg, STAGE_FAST):
        failed = gate_failures(row_values(row, cfg, STAGE_FAST), cfg, STAGE_FAST)
        return (not failed), failed
    recorded = row.get(QUALITY_COL) if hasattr(row, "get") else None
    if recorded is not None and not (isinstance(recorded, float) and pd.isna(recorded)):
        missing = row.get(QUALITY_MISSING_COL)
        if isinstance(missing, str):
            try:
                missing = json.loads(missing)
            except ValueError:
                missing = []
        return bool(recorded), list(missing or [])
    return None, []


def veto_of(row, cfg: dict, stage: str | None = STAGE_FAST) -> tuple[bool | None, list]:
    """Re-grade a recorded row's veto against the rules in force **now**.

    The mirror of `verdict_of`, and for the same reason: thresholds are retuned
    between scans, and a gate answering with last night's bar quietly ignores
    the change. `output/history/` keeps the original, so nothing archived moves.

    Note the recorded fallback reads `Veto Reasons` as the source of truth and
    derives the boolean from it, rather than trusting the `Veto` column: a row
    that round-tripped through CSV carries the reasons as a JSON string and the
    flag as the text "True"/"False", and the list is the thing tier 4 grades.
    """
    if is_enabled(cfg) and has_values(row, cfg, stage):
        reasons = veto_failures(row_values(row, cfg, stage), cfg, stage)
        return bool(reasons), reasons
    recorded = row.get(VETO_REASONS_COL) if hasattr(row, "get") else None
    if isinstance(recorded, str):
        try:
            recorded = json.loads(recorded)
        except ValueError:
            recorded = None
    if isinstance(recorded, list):
        return bool(recorded), list(recorded)
    return None, []


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _fmt_pct(value) -> str:
    return (f"{value:.1f}%" if isinstance(value, (int, float)) and pd.notna(value)
            else "n/a")


def _fmt_series(series, fmt) -> str:
    """'18.1B->19.3B' for a [(fiscal_year, value)] list, oldest -> newest."""
    pairs = _series(series)
    if pairs is None:
        return "n/a"
    return "->".join(fmt(value) for _, value in pairs)


def _year_span(series) -> str:
    """'(FY24->FY25)' label for a multi-year series."""
    pairs = _series(series)
    if pairs is None:
        return ""
    years = [f"FY{int(year) % 100:02d}" for year, _ in pairs]
    return f" ({years[0]}->{years[-1]})" if len(years) > 1 else f" ({years[0]})"


_FORMATTERS = {
    "number": lambda v: fmt_value(v),
    "pct": _fmt_pct,
    "pct_series": lambda v: _fmt_series(v, _fmt_pct),
    "money_series": lambda v: _fmt_series(v, fmt_compact),
}


def format_value(value, spec: dict) -> str:
    """Render one parameter's value for display, n/a-tolerant throughout."""
    kind = spec.get("format", "number")
    formatter = _FORMATTERS.get(kind, _FORMATTERS["number"])
    if kind in ("pct_series", "money_series"):
        return formatter(value)
    return formatter(scalar(value, spec))


def embed_fields(row, cfg: dict) -> list[dict]:
    """The displayable parameters as Discord embed fields (inline = a grid).

    Which parameters appear, in what order and formatted how, is entirely the
    registry's business now -- the ordering used to be hardcoded here, so
    adding a metric meant editing this function instead of the config.
    """
    if not is_enabled(cfg):
        return []
    fields = []
    for key, spec in parameters(cfg).items():
        if not spec.get("display"):
            continue
        label = label_of(key, spec)
        value = row.get(label) if hasattr(row, "get") else None
        name = label + (_year_span(value)
                        if spec.get("format", "").endswith("_series") else "")
        fields.append({"name": name, "value": format_value(value, spec),
                       "inline": True})
    return fields


# --------------------------------------------------------------------------
# Collection -- the only part that touches the network
# --------------------------------------------------------------------------

def _statement_metrics(frames: dict, keys: set, years: int, info: dict) -> dict:
    """Statement-derived metrics for one ticker, keyed by parameter key.

    Multi-year metrics (FCF, margins) come back as `[(fiscal_year, value)]`
    oldest -> newest; ROE/ROIC as scalar percents. Anything Yahoo does not
    publish for this company stays None -> 'n/a'. Banks have no Operating
    Income and negative-equity companies no Debt/Equity; both are genuine, not
    errors, so nothing here raises.

    Takes the frames rather than the ticker so `collect` can fetch the three
    statements once and hand the same ones to `derived.distress_metrics` and
    `derived.moat_metrics`.
    """
    income = frames.get("income", pd.DataFrame())
    cashflow = frames.get("cashflow", pd.DataFrame())
    balance = frames.get("balance", pd.DataFrame())

    # The `years` most recent annual columns, oldest -> newest.
    inc_cols = sorted(income.columns)[-years:] if not income.empty else []
    cf_cols = sorted(cashflow.columns)[-years:] if not cashflow.empty else []

    def yearly(pairs):
        return [(year, value) for year, value in pairs if value is not None]

    row = {}
    if "fcf" in keys:
        row["fcf"] = yearly([(c.year, stmt_value(cashflow, "Free Cash Flow", c))
                             for c in cf_cols])

    def margin(numerator_row):
        out = []
        for c in inc_cols:
            num = stmt_value(income, numerator_row, c)
            rev = stmt_value(income, "Total Revenue", c)
            out.append((c.year,
                        100 * num / rev if num is not None and rev else None))
        return yearly(out)

    # Deliberately NOT the pretax fallback `research_collect._financials` uses:
    # that one only feeds a chart, this one feeds the badge, and relaxing the
    # definition here would silently move the ⭐ for every bank and insurer.
    if "operating_margin" in keys:
        row["operating_margin"] = margin("Operating Income")
    if "profit_margin" in keys:
        row["profit_margin"] = margin("Net Income")

    if "roe" in keys:
        roe = None
        for c in reversed(inc_cols):          # latest year with both rows present
            net = stmt_value(income, "Net Income", c)
            equity = stmt_value(balance, "Stockholders Equity", c)
            if net is not None and equity:
                roe = 100 * net / equity
                break
        if roe is None and isinstance(info.get("returnOnEquity"), (int, float)):
            roe = 100 * info["returnOnEquity"]
        row["roe"] = roe

    if "roic" in keys:
        roic = None
        for c in reversed(inc_cols):
            ebit = stmt_value(income, "EBIT", c)
            tax = stmt_value(income, "Tax Provision", c)
            pretax = stmt_value(income, "Pretax Income", c)
            invested = stmt_value(balance, "Invested Capital", c)
            if None in (ebit, tax, pretax) or not invested or pretax <= 0:
                continue
            roic = 100 * ebit * (1 - tax / pretax) / invested
            break
        row["roic"] = roic

    return row


def _avg(xs):
    xs = [x for x in xs if _is_num(x)]
    return sum(xs) / len(xs) if xs else None


def _revision_net(d):
    if not isinstance(d, dict):
        return None
    up, dn = d.get("upLast30days"), d.get("downLast30days")
    if not (_is_num(up) and _is_num(dn)) or (up + dn) <= 0:
        return None
    return (up - dn) / (up + dn)


def _trend_change(d):
    if not isinstance(d, dict):
        return None
    cur, old = d.get("current"), d.get("90daysAgo")
    if not (_is_num(cur) and _is_num(old)) or old == 0:
        return None
    return (cur - old) / abs(old)


def _beat_rate(hist):
    ss = [h.get("surprise_pct") for h in (hist or [])
          if _is_num(h.get("surprise_pct"))]
    return sum(1 for s in ss if s > 0) / len(ss) if ss else None


def _buy_ratio(recs):
    if not recs:
        return None
    r = recs[0]
    total = sum((r.get(k) or 0)
                for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
    return ((r.get("strongBuy") or 0) + (r.get("buy") or 0)) / total if total else None


def _liquidity_distress(q: dict, financials: dict, current_floor: float = 1.0,
                        quick_floor: float = 0.7) -> int | None:
    """Both liquidity ratios under water **and** the company burning cash.

    `None` when a ratio is missing -- the whole universe of banks and insurers,
    for whom neither is defined. A veto that read that as distress would
    exclude every financial in the index.

    The cash-flow leg is not optional. A current ratio below 1 is the *normal*
    shape of a negative-working-capital business, where suppliers and customers
    finance operations: Marriott (0.54/0.48) and HP (0.79/0.44) both trip the
    ratio pair while generating 2.6bn and 2.8bn of free cash flow a year. Thin
    liquidity is only distress when the cash is actually going out, which is
    the same conjunction `negative_equity_burn` makes for the same reason.
    """
    current, quick = q.get("currentRatio"), q.get("quickRatio")
    if not (_is_num(current) and _is_num(quick)):
        return None
    annual = (financials or {}).get("annual") or []
    fcf = next((r.get("fcf") for r in reversed(annual) if _is_num(r.get("fcf"))),
               None)
    if fcf is None:
        return None
    return int(current < current_floor and quick < quick_floor and fcf < 0)


def deep_metrics(yahoo: dict) -> dict:
    """Flatten a `research_collect.collect_yahoo` bundle to scalar metrics.

    Every `.get(...) or {}` here is load-bearing: `_estimates` sets its keys to
    **None** (not `{}`) when yfinance returns no frame, so a plain
    `.get("eps_revisions", {})` would hand back None and the next `.get` would
    raise -- aborting the whole deep pass for that ticker.
    """
    val = yahoo.get("valuation") or {}
    ana = yahoo.get("analyst") or {}
    est = yahoo.get("estimates") or {}
    earn = yahoo.get("earnings") or {}
    q = yahoo.get("quality") or {}
    own = yahoo.get("ownership") or {}
    growth = est.get("growth") or {}
    targets = ana.get("targets") or {}

    def g(period, key):                      # a growth stockTrend as a percent
        cell = growth.get(period) or {}
        v = cell.get(key)
        return v * 100 if _is_num(v) else None

    hist = earn.get("surprise_history") or []

    # Analyst downgrades in the trailing 90 days. `recent_actions` is already
    # collected on every deep pass and was previously discarded; a cluster of
    # downgrades is one of the few genuinely forward-looking distress signals
    # Yahoo carries.
    cutoff = (pd.Timestamp.today().normalize() - pd.Timedelta(days=90))
    downgrades = 0
    for action in (ana.get("recent_actions") or []):
        if "down" not in str(action.get("action", "")).lower():
            continue
        when = pd.to_datetime(action.get("date"), errors="coerce")
        if pd.notna(when) and when.tz_localize(None) >= cutoff:
            downgrades += 1

    return {
        "pe_percentile_2y": val.get("pe_percentile_2y"),
        "analyst_upside_pct": targets.get("upside_pct"),
        "growth_this_year_pct": g("0y", "stockTrend"),
        "growth_next_year_pct": g("+1y", "stockTrend"),
        "eps_revision_net": _revision_net((est.get("eps_revisions") or {}).get("0y")),
        "eps_trend_change": _trend_change((est.get("eps_trend") or {}).get("0y")),
        "earnings_avg_surprise": _avg([h.get("surprise_pct") for h in hist]),
        "earnings_beat_rate": _beat_rate(hist),
        "return_on_assets": q.get("returnOnAssets_pct"),
        "gross_margin": q.get("grossMargins_pct"),
        "net_debt_to_ebitda": q.get("netDebtToEbitda"),
        "buyback_2y": own.get("shares_change_2y_pct"),
        "analyst_buy_ratio": _buy_ratio(ana.get("recommendations")),
        # Collected by `research_collect` on every deep pass and previously
        # discarded. The two liquidity ratios are the classic distress pair;
        # insider net selling and the downgrade count are the market's own
        # read. None of them cost an extra request.
        "current_ratio": q.get("currentRatio"),
        "quick_ratio": q.get("quickRatio"),
        # Both legs together, because a gate compares one number and the
        # classic liquidity read is a conjunction: a current ratio under 1 is
        # ordinary for a retailer with fast inventory turns, and only becomes a
        # distress signal when the quick ratio is under water too.
        "liquidity_distress": _liquidity_distress(q, yahoo.get("financials")),
        "insider_net_shares_6m": own.get("insider_net_shares_6m"),
        "downgrades_90d": float(downgrades) if ana.get("recent_actions") else None,
    }


def resolve(bundle: dict, cfg: dict, stage: str | None = None) -> dict:
    """Pick each enabled parameter's value out of the collected source payloads.

    Pure -- `bundle` is `{source_prefix: {key: value}}` as produced by
    `collect`. Keeping the lookup separate from the fetching is what makes the
    engine testable offline and what lets the deep path reuse the bundle the
    tier-3 collector already built.
    """
    out = {}
    for key, spec in parameters(cfg, stage).items():
        source = spec.get("source") or ""
        prefix, _, field_name = source.partition(".")
        value = (bundle.get(prefix) or {}).get(field_name or key)
        if isinstance(value, float) and pd.isna(value):
            value = None
        if spec.get("percent") and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            value = value * 100
        out[key] = value
    return out


def collect(ticker: str, cfg: dict, stage: str | None = STAGE_FAST,
            close=None) -> dict:
    """Fetch the source payloads the enabled parameters of `stage` need.

    `stage=None` means every stage, which is what tier 3 passes: it scores the
    whole registry, so it has to collect the whole registry.

    Every source is optional and independently guarded: one that fails logs and
    contributes nothing, which the engine reads as missing values rather than
    as a failure. That tolerance is why a nightly run survives Yahoo dropping a
    statement or IB Gateway being logged out.
    """
    wanted = sources_needed(cfg, stage)
    bundle: dict = {}
    tk = yf.Ticker(ticker)
    statement_users = {"yahoo_stmt", "distress", "moat"}
    info = {}

    if ({"yahoo_info"} | statement_users) & wanted:
        try:
            info = tk.info or {}
        except Exception as exc:  # noqa: BLE001 - a bad ticker must not kill the scan
            log_step("YAHOO", "failed", f"info for {ticker}: {exc}")
            info = {}
        bundle["yahoo_info"] = info
        bundle[COMPANY_COL] = info.get("longName") or info.get("shortName")

    if statement_users & wanted:
        # The three annual statements, fetched **once** and shared. `distress`
        # and `moat` read the same frames `yahoo_stmt` does, so wiring them in
        # costs no additional Yahoo round trips -- which is why both can sit at
        # the `fast` stage and grade every tier-1 hit rather than only the
        # handful of candidates tier 3 reaches.
        import derived
        years = int(section(cfg).get("statement_years", 2))
        frames = derived.statement_frames(tk)
        if "yahoo_stmt" in wanted:
            keys = {k for k, s in parameters(cfg, stage).items()
                    if (s.get("source") or "").startswith("yahoo_stmt.")}
            bundle["yahoo_stmt"] = _statement_metrics(frames, keys, years, info)
        if "distress" in wanted:
            bundle["distress"] = derived.distress_metrics(frames, info, years)
        if "moat" in wanted:
            bundle["moat"] = derived.moat_metrics(
                frames, info, years,
                float(section(cfg).get("moat_roic_hurdle_pct", 12.0)))

    if "sec_flags" in wanted:
        import sec
        bundle["sec_flags"] = sec.flags(ticker, cfg)

    if "yahoo_deep" in wanted:
        import research_collect
        fin_cfg = cfg.get("research", {}).get("financials", {})
        yahoo = research_collect.collect_yahoo(
            ticker, close=close,
            years=fin_cfg.get("years", 4), quarters=fin_cfg.get("quarters", 4))
        bundle["yahoo_deep"] = deep_metrics(yahoo)
        bundle["_yahoo"] = yahoo          # the full bundle, for tier 3's report

    if "ibkr" in wanted:
        import ibkr
        bundle["ibkr"] = ibkr.metrics(ticker, cfg)

    return bundle


def fetch_fast(tickers: list[str], cfg: dict) -> pd.DataFrame:
    """The `fast` parameters for a list of tickers, as a label-keyed frame.

    This is what joins onto each screen's hits, so its columns are the display
    labels the whole downstream contract (`latest_hits.json`, `signals.csv`,
    the embed fields) is keyed by. Only ever runs on the handful of signalling
    tickers, so a plain loop is fine.
    """
    if not is_enabled(cfg) or not tickers:
        return pd.DataFrame()
    specs = parameters(cfg, STAGE_FAST)
    t0 = time.perf_counter()
    rows, failed = {}, []
    for ticker in tickers:
        try:
            bundle = collect(ticker, cfg, STAGE_FAST)
            values = resolve(bundle, cfg, STAGE_FAST)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not kill the alert
            log_step("YAHOO", "failed", f"fundamentals for {ticker}: {exc}")
            failed.append(ticker)
            bundle, values = {}, {}
        row = {COMPANY_COL: bundle.get(COMPANY_COL)}
        for key, spec in specs.items():
            row[label_of(key, spec)] = values.get(key)
        rows[ticker] = row
    log_step("YAHOO", "ok" if not failed else "partial",
             f"fundamentals for {len(rows) - len(failed)}/{len(tickers)} ticker(s)"
             + (f" -- missing {', '.join(failed)}" if failed else ""),
             ms=(time.perf_counter() - t0) * 1000)
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("Ticker")


# --------------------------------------------------------------------------
# Validation -- used by the MCP config tools before any write
# --------------------------------------------------------------------------

RESOLVERS = ("yahoo_info", "yahoo_stmt", "yahoo_deep", "ibkr",
             "distress", "moat", "sec_flags")


def validate(cfg: dict) -> list[str]:
    """Every way the `quality` section can be wrong, as plain sentences.

    Called before a config write, because each of these fails *silently* at
    runtime: an unknown source resolves to None (the parameter reads as missing
    for every company), a typo'd gate key is simply never applied, and a group
    with no weight contributes nothing while still looking configured.
    """
    problems = []
    sect = section(cfg)
    if not sect:
        return problems

    known_groups = set((sect.get("groups") or {}).keys())
    axis_seen = {axis: 0 for axis in AXES}
    for name, spec in (sect.get("groups") or {}).items():
        weight = spec.get("weight")
        enabled = spec.get("enabled", True)
        if enabled and not (isinstance(weight, (int, float)) and weight > 0):
            problems.append(f"quality.groups.{name}: enabled but weight is "
                            f"{weight!r} -- it would contribute nothing")
        # An unknown axis silently falls back to reward, which would move a
        # group off the risk axis without any visible sign -- exactly the class
        # of failure this function exists to convert into a refusal.
        axis = spec.get("axis")
        if axis is not None and axis not in AXES:
            problems.append(f"quality.groups.{name}.axis: {axis!r} is not one "
                            f"of {', '.join(AXES)}")
        elif enabled:
            axis_seen[axis_of(spec)] += 1
        agg = spec.get("aggregate")
        if agg is not None and agg != "mean":
            if not (isinstance(agg, dict) and set(agg) == {"worst_k"}
                    and _is_num(agg.get("worst_k")) and agg["worst_k"] >= 1):
                problems.append(
                    f"quality.groups.{name}.aggregate: expected \"mean\" or "
                    f"{{\"worst_k\": n>=1}}, got {agg!r} -- an unrecognised "
                    f"value would silently fall back to the mean")
    # An axis with no groups yields None, which every caller must read as "not
    # measured". That is correct behaviour but almost never intended, so say so.
    for axis, count in axis_seen.items():
        if not count:
            problems.append(f"quality.groups: no enabled group is on the "
                            f"{axis!r} axis -- that coordinate can only ever "
                            f"come back unmeasured")

    for key, spec in (sect.get("parameters") or {}).items():
        where = f"quality.parameters.{key}"
        if not isinstance(spec, dict):
            problems.append(f"{where}: expected an object")
            continue
        source = spec.get("source") or ""
        prefix = source.split(".", 1)[0]
        if prefix not in RESOLVERS:
            problems.append(f"{where}.source: unknown resolver {prefix!r} "
                            f"-- expected one of {', '.join(RESOLVERS)}")
        stage = spec.get("stage", STAGE_FAST)
        if stage not in STAGES:
            problems.append(f"{where}.stage: {stage!r} is not one of "
                            f"{', '.join(STAGES)}")
        group = spec.get("group")
        if group and group not in known_groups:
            problems.append(f"{where}.group: {group!r} is not a "
                            f"quality.groups entry")
        gate = spec.get("gate")
        if gate:
            if not isinstance(gate, dict):
                problems.append(f"{where}.gate: expected an object")
            else:
                for bad in set(gate) - set(GATE_KEYS):
                    problems.append(f"{where}.gate.{bad}: not a gate keyword "
                                    f"-- expected {', '.join(GATE_KEYS)}")
        anchors = spec.get("score")
        if anchors:
            if not isinstance(anchors, dict):
                problems.append(f"{where}.score: expected an object")
            elif not all(_is_num(anchors.get(k)) for k in ("good", "bad")):
                problems.append(f"{where}.score: needs numeric 'good' and 'bad'")
        if spec.get("enabled", True) and not gate and not anchors:
            problems.append(f"{where}: enabled but has neither a gate nor a "
                            f"score -- it would be collected and never used")
        if spec.get("veto") and not gate:
            problems.append(f"{where}.veto: a veto needs a gate to trip -- "
                            f"a score alone can never exclude anything")
        fmt = spec.get("format", "number")
        if fmt not in _FORMATTERS:
            problems.append(f"{where}.format: {fmt!r} is not one of "
                            f"{', '.join(_FORMATTERS)}")
    return problems
