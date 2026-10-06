"""The unified quality engine: the `enabled` flag, the gates, the score.

Fully offline -- every check drives `quality.evaluate` with hand-built values,
so nothing here touches Yahoo or the cached panel.

Every check is an **invariant**, derived from whatever `config.json` currently
says rather than from written-down thresholds; the registry is retuned
constantly and a test pinned to today's numbers would be stale by tomorrow.
What is pinned instead:

  * **`enabled: false` makes a parameter invisible** -- not gated, not scored,
    absent from `Quality Missing` and from the embed fields, and not counted in
    any weight. That is the flag's entire contract, and the reason it exists:
    a company with no dividend has to be able to earn the badge without the
    rule being deleted and the record of it lost.
  * **missing is not failing, and neither is not-evaluated.** A missing value
    fails its gate but is skipped in the score; a group with data but no values
    is neutral 0.5, not 0 (a bank must not be zeroed by rows Yahoo does not
    publish for it); the layer switched off writes no columns at all.
  * gate semantics are strict compares, and `increasing` needs two years
  * the score renormalizes over what actually participated
  * a JSON round trip does not change any verdict -- that is what keeps an
    archived scan readable after the config that produced it has moved on
  * `validate` catches every way a parameter can look configured and do nothing
"""

import copy
import json

from _harness import Checks, config

import quality

c = Checks("quality engine")
cfg = config()

GATED = {k: s for k, s in quality.parameters(cfg, quality.STAGE_FAST).items()
         if s.get("gate") and not s.get("veto")}
VETOES = {k: s for k, s in quality.parameters(cfg, quality.STAGE_FAST).items()
          if s.get("veto")}
SCORED = {k: s for k, s in quality.parameters(cfg).items() if s.get("score")}


def passing_value(gate):
    """A value that satisfies `gate`, derived from the gate's own bounds."""
    lo, hi = gate.get("min"), gate.get("max")
    if lo is not None and hi is not None:
        return (lo + hi) / 2
    if hi is not None:
        return hi / 2 if hi > 0 else hi - 1
    return lo + abs(lo) + 1


def healthy(keys=None):
    """Values passing every gate, as a `{key: value}` for `evaluate`."""
    out = {}
    for key, spec in (keys or GATED).items():
        good = passing_value(spec["gate"])
        if spec["gate"].get("increasing"):
            out[key] = [(2024, good * 0.9), (2025, good)]
        elif spec.get("format", "").endswith("_series"):
            out[key] = [(2024, good * 0.9), (2025, good)]
        else:
            out[key] = good
    return out


# --------------------------------------------------------------------------
c.section("a healthy company passes every enabled gate")

base = healthy()
result = quality.evaluate(base, cfg, quality.STAGE_FAST)
c.ok("the fixture passes", result.passed and not result.failed,
     f"failed: {result.failed}")
c.ok("passed is exactly 'nothing failed'",
     result.passed == (not result.failed))
c.ok("a score is produced", isinstance(result.score, float),
     f"{result.score}")
c.ok("the score is a percentage", 0 <= (result.score or 0) <= 100,
     f"{result.score}")
c.ok("evaluate is idempotent",
     quality.evaluate(base, cfg, quality.STAGE_FAST).as_dict()
     == result.as_dict())

# --------------------------------------------------------------------------
c.section("the veto: an exclusion rule, not a stricter badge")

# The badge and the veto share the gate arithmetic and disagree, deliberately,
# about what a missing value means. This is the single most load-bearing rule
# in the exclusion layer: Yahoo leaves holes in nearly every company's
# statements, so a veto that fired on missing data would exclude most of the
# universe and be worse than no veto at all.
nothing = {k: None for k in quality.parameters(cfg, quality.STAGE_FAST)}
c.ok("a missing value never vetoes",
     quality.veto_failures(nothing, cfg, quality.STAGE_FAST) == [],
     f"{len(VETOES)} veto rule(s) at the fast stage, none tripped on no data")
c.ok("...while the same missing value fails every badge gate",
     set(quality.gate_failures(nothing, cfg, quality.STAGE_FAST)) == set(GATED),
     "unverifiable quality does not earn the badge; unverifiable is not "
     "proof of disaster")

c.ok("a veto parameter never appears in the badge's failed list",
     not (set(VETOES) & set(quality.gate_failures(nothing, cfg,
                                                  quality.STAGE_FAST))),
     "most tickers already fail a badge gate; vetoes must not make it stricter")

if VETOES:
    # Break exactly one veto, using the rule's own bound (compares are strict,
    # so the bound itself trips it) -- no magic numbers, any threshold.
    v_key, v_spec = next(iter(VETOES.items()))
    v_gate = v_spec["gate"]
    tripping = v_gate.get("min", v_gate.get("max"))
    tripped = quality.evaluate({**base, v_key: tripping}, cfg, quality.STAGE_FAST)
    c.ok("a value on the wrong side of the bound trips its veto",
         tripped.vetoed and v_key in tripped.veto_reasons,
         f"{v_key} at its own bound {tripping} -> {tripped.veto_reasons}")
    c.ok("...and does not change the badge",
         tripped.passed == quality.evaluate(base, cfg,
                                            quality.STAGE_FAST).passed)
    c.ok("a clean fixture is not vetoed",
         quality.evaluate(base, cfg, quality.STAGE_FAST).vetoed is False)

# Two conjunctions that exist because the single-leg version fired on healthy
# companies in the 2026-08-09 shakedown. Both are pinned here because the
# failure mode is silent: the veto looks configured and quietly excludes an
# entire business model.
thin = {"currentRatio": 0.54, "quickRatio": 0.48}
burning = {"annual": [{"fcf": -1e9}]}
earning = {"annual": [{"fcf": 2.6e9}]}
c.ok("thin liquidity alone is not distress",
     quality._liquidity_distress(thin, earning) == 0,
     "Marriott 0.54/0.48 and HP 0.79/0.44 both generate billions -- a "
     "negative-working-capital business is financed by its suppliers")
c.ok("thin liquidity WITH cash burn is",
     quality._liquidity_distress(thin, burning) == 1)
c.ok("a bank with neither ratio is not evaluated, not excluded",
     quality._liquidity_distress({}, burning) is None)
c.ok("no cash-flow statement means no verdict either",
     quality._liquidity_distress(thin, {}) is None)
c.ok("the liquidity flag is an int, never a bool",
     isinstance(quality._liquidity_distress(thin, burning), int)
     and not isinstance(quality._liquidity_distress(thin, burning), bool))

# `scalar` rejects bools, so a flag stored as True/False reads as *missing* --
# and a missing value never vetoes. A resolver returning a bool flag would look
# entirely correct in config and silently never fire. `derived._flag` returns
# int for exactly this reason.
c.ok("a bool reads as missing, so every flag must be an int",
     quality.scalar(True) is None and quality.scalar(False) is None
     and quality.scalar(1) == 1.0 and quality.scalar(0) == 0.0)

# --------------------------------------------------------------------------
c.section("`enabled: false` makes a parameter invisible")

# Pick a gate whose value can be removed to break it -- the user's own example
# is `dividendYield` on a company that pays no dividend.
victim = next(iter(GATED))
broken = {**base, victim: None}
with_rule = quality.evaluate(broken, cfg, quality.STAGE_FAST)
c.ok(f"a missing value fails its gate ({victim})",
     with_rule.passed is False and victim in with_rule.failed,
     f"{with_rule.failed}")

off = copy.deepcopy(cfg)
off["quality"]["parameters"][victim]["enabled"] = False
without = quality.evaluate(broken, off, quality.STAGE_FAST)
c.ok("switching it off turns the same company into a pass",
     without.passed is True and not without.failed, f"{without.failed}")
c.ok("...and drops it from Quality Missing entirely",
     victim not in without.failed)
c.ok("...and from the parameter set recorded with the verdict",
     victim in result.parameters and victim not in without.parameters)
c.ok("...and from the values the verdict carries",
     victim in result.values and victim not in without.values)

row = {quality.label_of(k, s): base.get(k)
       for k, s in quality.parameters(cfg, quality.STAGE_FAST).items()}
on_labels = {f["name"] for f in quality.embed_fields(row, cfg)}
off_labels = {f["name"] for f in quality.embed_fields(row, off)}
victim_label = quality.label_of(victim, cfg["quality"]["parameters"][victim])
c.ok("...and from the Discord embed fields when it was displayed",
     (not cfg["quality"]["parameters"][victim].get("display"))
     or (any(n.startswith(victim_label) for n in on_labels)
         and not any(n.startswith(victim_label) for n in off_labels)),
     f"{victim_label}: on={len(on_labels)} off={len(off_labels)} fields")

# Turning every gate off must leave a pass, not an empty-set failure.
none_on = copy.deepcopy(cfg)
for spec in none_on["quality"]["parameters"].values():
    spec["enabled"] = False
c.ok("with every parameter off, nothing can fail",
     quality.evaluate({}, none_on, quality.STAGE_FAST).passed is True)

# --------------------------------------------------------------------------
c.section("the layer switched off is 'not evaluated', not 'failed'")

layer_off = copy.deepcopy(cfg)
layer_off["quality"]["enabled"] = False
result_off = quality.evaluate(broken, layer_off, quality.STAGE_FAST)
c.ok("evaluate reports passed=None", result_off.passed is None)
c.ok("...with no failures and no score",
     not result_off.failed and result_off.score is None)

import pandas as pd  # noqa: E402  (only needed for the frame check)

frame = pd.DataFrame([row], index=pd.Index(["AAA"], name="Ticker"))
annotated = quality.annotate(frame.copy(), layer_off)
c.ok("annotate writes no columns at all when the layer is off",
     "Quality" not in annotated.columns
     and "Quality Missing" not in annotated.columns,
     "absent columns are what downstream reads as 'not evaluated'")
c.ok("annotate does write them when the layer is on",
     {"Quality", "Quality Missing"}
     <= set(quality.annotate(frame.copy(), cfg).columns))
c.ok("a row with no values reports 'not evaluated' rather than all-failed",
     quality.verdict_of({}, cfg) == (None, []))
c.ok("has_values distinguishes the two",
     quality.has_values(row, cfg, quality.STAGE_FAST)
     and not quality.has_values({}, cfg, quality.STAGE_FAST))

# --------------------------------------------------------------------------
c.section("gate semantics")

for key, spec in list(GATED.items())[:6]:
    gate = spec["gate"]
    if gate.get("increasing"):
        good = passing_value(gate)
        c.ok(f"{key}: `increasing` fails on a series that falls",
             key in quality.gate_failures(
                 {**base, key: [(2024, good), (2025, good * 0.5)]},
                 cfg, quality.STAGE_FAST))
        c.ok(f"{key}: `increasing` fails on a single year",
             key in quality.gate_failures({**base, key: [(2025, good)]},
                                          cfg, quality.STAGE_FAST))
    for bound in ("min", "max"):
        if gate.get(bound) is None:
            continue
        c.ok(f"{key}: the {bound} bound itself fails (strict compare)",
             key in quality.gate_failures({**base, key: gate[bound]},
                                          cfg, quality.STAGE_FAST))

c.ok("an empty series is a missing value, not a pass",
     all(k in quality.gate_failures({**base, k: []}, cfg, quality.STAGE_FAST)
         for k, s in GATED.items() if s.get("format", "").endswith("_series")))

# --------------------------------------------------------------------------
c.section("scoring: weights renormalize over what participated")

full_values = {**base, **{k: passing_value(s.get("gate") or {"max": 10})
                          for k, s in SCORED.items() if k not in base}}
score_all, groups_all = quality.score_of(full_values, cfg)
c.ok("every scored group appears in the breakdown",
     set(groups_all) <= set(quality.groups(cfg)),
     f"{sorted(groups_all)}")
c.ok("a group reports how much of it had data",
     all(g["metrics_used"] <= g["metrics_total"] for g in groups_all.values()))

# A group with parameters but no values is neutral, never zero: that is what
# stops a bank being driven to the bottom by rows Yahoo does not publish.
blank = {k: None for k in full_values}
score_blank, groups_blank = quality.score_of(blank, cfg)
c.ok("a group with no values scores a neutral 0.5, not 0",
     all(g["score"] == 0.5 for g in groups_blank.values()),
     f"{ {k: v['score'] for k, v in groups_blank.items()} }")
c.close("...so an all-missing company sits at 50, not 0", score_blank, 50.0,
        tol=0.05)

# A group with no participating parameters at this stage is dropped entirely
# rather than folded in as 0.5 -- at `fast` there is no estimate data to be
# neutral *about*.
_, groups_fast = quality.score_of(base, cfg, quality.STAGE_FAST)
deep_only = {name for name in quality.groups(cfg)
             if not any(s.get("group") == name and s.get("score")
                        for s in quality.parameters(cfg, quality.STAGE_FAST).values())}
c.ok("a group with no parameters at this stage is dropped, not neutralised",
     not (deep_only & set(groups_fast)),
     f"deep-only groups: {sorted(deep_only)}")

disabled_group = copy.deepcopy(cfg)
first_group = next(iter(quality.groups(cfg)))
disabled_group["quality"]["groups"][first_group]["enabled"] = False
score_less, groups_less = quality.score_of(full_values, disabled_group)
c.ok("disabling a group removes it from the breakdown",
     first_group not in groups_less)
c.ok("...and the remaining weights still produce a 0-100 score",
     score_less is None or 0 <= score_less <= 100, f"{score_less}")

# --------------------------------------------------------------------------
c.section("the verdict survives a JSON round trip")

# `_json_safe` turns the [(year, value)] tuples into nested arrays. Grading has
# to be unaffected, or every archived scan becomes unreadable the moment the
# format moves on.
round_tripped = json.loads(json.dumps(
    {quality.label_of(k, s): base.get(k)
     for k, s in quality.parameters(cfg, quality.STAGE_FAST).items()}))
c.ok("a round-tripped row grades identically",
     quality.verdict_of(round_tripped, cfg)
     == (result.passed, result.failed),
     f"{quality.verdict_of(round_tripped, cfg)} vs "
     f"{(result.passed, result.failed)}")
c.ok("a multi-year series still unwraps to its latest year",
     quality.scalar([[2024, 1.0], [2025, 9.0]]) == 9.0)
c.ok("zero_is_missing turns Yahoo's bank artifact into a missing value",
     quality.scalar(0.0, {"zero_is_missing": True}) is None
     and quality.scalar(0.0, {}) == 0.0)
c.ok("a bool is never a number", quality.scalar(True) is None)

# --------------------------------------------------------------------------
c.section("validate catches what fails silently at runtime")


def problems(mutate):
    variant = copy.deepcopy(cfg)
    mutate(variant)
    return quality.validate(variant)


c.ok("the live config is valid", not quality.validate(cfg),
     str(quality.validate(cfg)))


def _set(path, value):
    def apply(variant):
        node = variant
        parts = path.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return apply


any_key = next(iter(quality.parameters(cfg)))
c.ok("an unknown source is caught",
     problems(_set(f"quality.parameters.{any_key}.source", "nope.x")))
c.ok("a typo'd gate keyword is caught",
     problems(_set(f"quality.parameters.{any_key}.gate", {"minimum": 1})))
c.ok("a group that does not exist is caught",
     problems(_set(f"quality.parameters.{any_key}.group", "invented")))
c.ok("an unknown stage is caught",
     problems(_set(f"quality.parameters.{any_key}.stage", "sometimes")))
c.ok("a score missing an anchor is caught",
     problems(_set(f"quality.parameters.{any_key}.score", {"good": 1})))
c.ok("an unknown format is caught",
     problems(_set(f"quality.parameters.{any_key}.format", "sparkline")))
c.ok("an enabled group with no weight is caught",
     problems(_set(f"quality.groups.{first_group}.weight", 0)))


def _inert(variant):
    variant["quality"]["parameters"]["inert"] = {
        "enabled": True, "label": "Inert", "source": "yahoo_info.x",
        "group": first_group, "stage": "fast", "format": "number"}


veto_no_gate = copy.deepcopy(cfg)
veto_no_gate["quality"]["parameters"]["_probe"] = {
    "enabled": True, "source": "yahoo_info.probe", "group": next(iter(quality.groups(cfg))),
    "stage": "fast", "veto": True, "score": {"good": 1, "bad": 0}}
c.ok("a veto with no gate is caught",
     any("veto" in p for p in quality.validate(veto_no_gate)),
     "a score can never exclude anything, so the rule would be decorative")

c.ok("a parameter with neither a gate nor a score is caught",
     problems(_inert), "it would be collected and never used")


# --------------------------------------------------------------------------
# The risk/reward axes -- the quadrant coordinates
# --------------------------------------------------------------------------
# What is pinned here is the *polarity* and the *separation*, never a number.
# A silent sign flip on the risk axis would put the most dangerous companies in
# the buy quadrant, and no other check in the file would notice.

axis_names = {quality.axis_of(g) for g in quality.groups(cfg).values()}
c.ok("every enabled group lands on a known axis",
     axis_names <= set(quality.AXES), f"got {sorted(axis_names)}")
c.ok("both axes have at least one enabled group",
     axis_names == set(quality.AXES),
     "an axis with no groups can only ever report 'not measured'")


def _axis_probe(risk_value: float, reward_value: float) -> dict:
    """Values that drive every risk metric one way and every reward metric the
    other, so the two coordinates must move independently."""
    out = {}
    for key, spec in quality.parameters(cfg, None).items():
        anchors = spec.get("score")
        if not anchors:
            continue
        group = quality.groups(cfg).get(spec.get("group")) or {}
        frac = risk_value if quality.axis_of(group) == quality.AXIS_RISK \
            else reward_value
        # Interpolate in anchor space so direction is handled for us.
        out[key] = anchors["bad"] + frac * (anchors["good"] - anchors["bad"])
    return out


best = quality.axis_scores(_axis_probe(1.0, 1.0), cfg, None)
worst = quality.axis_scores(_axis_probe(0.0, 0.0), cfg, None)
safe_dull = quality.axis_scores(_axis_probe(1.0, 0.0), cfg, None)
risky_rich = quality.axis_scores(_axis_probe(0.0, 1.0), cfg, None)

c.ok("all-good scores maximum reward and minimum risk",
     best["reward"] == 100.0 and best["risk"] == 0.0,
     f"reward={best['reward']} risk={best['risk']}")
c.ok("all-bad scores minimum reward and maximum risk",
     worst["reward"] == 0.0 and worst["risk"] == 100.0,
     f"reward={worst['reward']} risk={worst['risk']}")
c.ok("risk is the complement of safety, not a second copy of it",
     all(abs(a["risk"] - (100 - a["safety"])) < 1e-9
         for a in (best, worst, safe_dull, risky_rich)))
c.ok("the axes are independent -- safe-and-dull is not risky-and-rich",
     safe_dull["reward"] == 0.0 and safe_dull["risk"] == 0.0
     and risky_rich["reward"] == 100.0 and risky_rich["risk"] == 100.0,
     "a blended score cannot tell these two apart, which is why axes exist")

no_values = quality.axis_scores({}, cfg, None)
c.ok("an unmeasurable ticker reports neutral axes, never zero risk",
     no_values["risk"] == 50.0 and no_values["reward"] == 50.0,
     "groups with no values are neutral 0.5; plotting 0 would file an "
     "unknown company in the buy quadrant")

c.ok("the blended score still agrees with score_of",
     quality.score_of(_axis_probe(1.0, 1.0), cfg, None)[0] == 100.0,
     "the axes are additive -- they must not have changed the old number")


# `worst_k` selects the lowest readings, so it can only ever be <= the mean.
# Isolated to a single group on the risk axis: there is more than one now
# (accounting distress and market risk), and a weighted mean across two of them
# would dilute the effect being measured and make the check prove nothing.
_wk = copy.deepcopy(cfg)
_risk_groups = [n for n, g in quality.groups(cfg).items()
                if quality.axis_of(g) == quality.AXIS_RISK]
_risk_group = _risk_groups[0]
for _name in _risk_groups[1:]:
    _wk["quality"]["groups"][_name]["enabled"] = False
_wk["quality"]["groups"][_risk_group]["aggregate"] = {"worst_k": 1}
mixed = _axis_probe(1.0, 1.0)
# Drag exactly one risk metric to its bad anchor.
_one = next(k for k, s in quality.parameters(cfg, None).items()
            if s.get("score") and s.get("group") == _risk_group)
mixed[_one] = quality.parameters(cfg, None)[_one]["score"]["bad"]
# Compared against the *same* single-group config with a mean, so the only
# difference between the two readings is the aggregation.
_mean_one = copy.deepcopy(_wk)
_mean_one["quality"]["groups"][_risk_group]["aggregate"] = "mean"
c.ok("worst_k is never kinder than the mean",
     quality.axis_scores(mixed, _wk, None)["risk"]
     >= quality.axis_scores(mixed, _mean_one, None)["risk"],
     "one catastrophic reading must not be averaged away by benign ones")
c.ok("worst_k:1 keys the axis off the single worst reading",
     quality.axis_scores(mixed, _wk, None)["risk"] == 100.0)

# `worst_k` >= the number of PARTICIPATING metrics has to degrade to the mean,
# not raise and not drop members. This is what makes the setting safe to leave on
# across stages: the same group carries 10 scored metrics at `fast` and 18 at
# `deep`, and a ticker with only two resolvable values must still be gradeable.
_big_k = copy.deepcopy(_mean_one)
_big_k["quality"]["groups"][_risk_group]["aggregate"] = {"worst_k": 999}
c.ok("worst_k larger than the group degrades to the mean of what is there",
     quality.axis_scores(mixed, _big_k, None)["risk"]
     == quality.axis_scores(mixed, _mean_one, None)["risk"],
     "the most pessimistic read available is already the mean of everything")

_two_only = {k: v for k, v in mixed.items() if k == _one}
c.ok("a group with fewer values than k is still graded, not skipped",
     quality.axis_scores(_two_only, _wk, None)["risk"] is not None)

# Enabling `worst_k` makes the ORDER of the readings load-bearing rather than
# just their average, so pin the selection against a hand-computed number. Two
# equally-weighted metrics at 0.25 and 0.75: the mean is 0.50 -> risk 50, and
# worst_k 1 must take the 0.25 -> safety 0.25 -> risk 75.
_pair = [k for k, s in quality.parameters(cfg, None).items()
         if s.get("score") and s.get("group") == _risk_group][:2]
if len(_pair) == 2:
    _probe = {}
    for _k, _frac in zip(_pair, (0.25, 0.75)):
        _a = quality.parameters(cfg, None)[_k]["score"]
        _probe[_k] = _a["bad"] + _frac * (_a["good"] - _a["bad"])
    _only_pair = copy.deepcopy(_wk)
    _mean_pair = copy.deepcopy(_mean_one)
    # Equal weights, so the hand arithmetic above is the whole story.
    for _c in (_only_pair, _mean_pair):
        for _k in _pair:
            _c["quality"]["parameters"][_k]["weight"] = 1.0
    _got_wk = quality.axis_scores(_probe, _only_pair, None)["risk"]
    _got_mean = quality.axis_scores(_probe, _mean_pair, None)["risk"]
    c.ok("worst_k takes the lowest reading, not the average of the two",
         abs(_got_mean - 50.0) < 1e-6 and abs(_got_wk - 75.0) < 1e-6,
         f"mean {_got_mean:.3f} (expect 50), worst_k {_got_wk:.3f} (expect 75)")

c.ok("an unknown axis is caught",
     problems(lambda v: v["quality"]["groups"][first_group].update(
         {"axis": "sideways"})),
     "it would silently fall back to reward and move the group off risk")
c.ok("an unrecognised aggregate is caught",
     problems(lambda v: v["quality"]["groups"][first_group].update(
         {"aggregate": {"worst_k": 0}})),
     "it would silently fall back to the mean")

raise SystemExit(c.finish())
