"""One-shot: prove the unified `quality` section reproduces the old two.

Tier 2's badge used to come from `fundamentals.quality.rules` and tier 3's
score from `research.synthesis.dimensions`. Both now come from
`quality.parameters`. This script does not rewrite anything -- it *checks* the
translation, because the failure mode of getting it wrong is silent: a gate
whose key no longer resolves simply reads as a missing value, which fails, and
every ticker quietly loses the badge.

Two passes:

  * **structural** -- every old rule has an enabled gate with identical
    thresholds, and every old dimension metric has an anchor with identical
    good/bad. Reports anything added, dropped or changed.
  * **behavioural** -- the old rule engine and the new one graded over the same
    synthetic values, including the awkward cases (missing value, single-year
    series, a series that fell).

Delete this file once the old sections come out of `config.json`.

Usage:
    python migrate_config.py            # both passes, exit 1 on a mismatch
    python migrate_config.py --verbose  # also list what matched
"""

import sys

import quality
from scanner_common import enable_utf8_output, load_config

# The old engine, reproduced here rather than imported: it has been deleted
# from scanner_common, and a check that calls the new code twice checks nothing.
# Lifted verbatim from scanner_common.quality_failures as it stood.


def _old_rule_value(row, key, fund_cfg):
    if key in fund_cfg.get("fields", {}):
        return row.get(fund_cfg["fields"][key])
    metrics = fund_cfg.get("statements", {}).get("metrics", {})
    if key in metrics:
        return row.get(metrics[key])
    return None


def old_failures(row, fund_cfg):
    failed = []
    for key, rule in fund_cfg.get("quality", {}).get("rules", {}).items():
        value = _old_rule_value(row, key, fund_cfg)
        series = value if isinstance(value, list) else None
        if series is not None:
            value = series[-1][1] if series else None
        ok = isinstance(value, (int, float)) and value == value   # not NaN
        if ok and "min" in rule:
            ok = value > rule["min"]
        if ok and "max" in rule:
            ok = value < rule["max"]
        if ok and rule.get("increasing"):
            ok = series is not None and len(series) >= 2 and series[-1][1] > series[-2][1]
        if not ok:
            failed.append(key)
    return failed


# --------------------------------------------------------------------------

def check_gates(cfg, verbose=False) -> list[str]:
    """Every old rule -> an enabled gate with the same thresholds."""
    problems = []
    old = (cfg.get("fundamentals", {}).get("quality", {}).get("rules") or {})
    new = {k: s for k, s in quality.parameters(cfg, enabled_only=False).items()
           if s.get("gate")}

    for key, rule in old.items():
        spec = new.get(key)
        if spec is None:
            problems.append(f"gate {key!r}: in fundamentals.quality.rules but "
                            f"has no gate in quality.parameters")
            continue
        if not spec.get("enabled", True):
            problems.append(f"gate {key!r}: present but disabled -- it will no "
                            f"longer affect the badge")
        gate = spec["gate"]
        for field in ("min", "max", "increasing"):
            if rule.get(field) != gate.get(field):
                problems.append(f"gate {key}.{field}: was {rule.get(field)!r}, "
                                f"now {gate.get(field)!r}")
        if verbose and not problems:
            print(f"  ok  gate {key}: {gate}")
    for key in set(new) - set(old):
        problems.append(f"gate {key!r}: new -- it did not gate the badge before")
    return problems


def check_anchors(cfg, verbose=False) -> list[str]:
    """Every old dimension metric -> a score anchor with the same good/bad."""
    problems = []
    dims = (cfg.get("research", {}).get("synthesis", {}).get("dimensions") or {})
    old = {name: anchors
           for d in dims.values() for name, anchors in d["metrics"].items()}
    new = {k: s["score"] for k, s in quality.parameters(cfg, enabled_only=False).items()
           if s.get("score")}

    for name, anchors in old.items():
        spec = new.get(name)
        if spec is None:
            problems.append(f"anchor {name!r}: scored before, has no "
                            f"quality.parameters score now")
            continue
        for field in ("good", "bad"):
            if anchors.get(field) != spec.get(field):
                problems.append(f"anchor {name}.{field}: was "
                                f"{anchors.get(field)!r}, now {spec.get(field)!r}")
        if verbose:
            print(f"  ok  anchor {name}: {spec}")
    return problems


def _cases(cfg):
    """Value dicts covering the cases the two engines could disagree on."""
    keys = list(quality.parameters(cfg, quality.STAGE_FAST))
    healthy = {"trailingPE": 20, "trailingPegRatio": 1.2, "debtToEquity": 40,
               "revenueGrowth": 15, "dividendYield": 1.5, "payoutRatio": 30,
               "roe": 20, "roic": 18,
               "operating_margin": [(2024, 20.0), (2025, 22.0)],
               "profit_margin": [(2024, 15.0), (2025, 16.0)],
               "fcf": [(2024, 1e9), (2025, 2e9)]}
    yield "healthy", healthy
    yield "all missing", {k: None for k in keys}
    yield "no dividend", {**healthy, "dividendYield": None}
    yield "margins falling", {**healthy,
                              "operating_margin": [(2024, 22.0), (2025, 20.0)],
                              "profit_margin": [(2024, 16.0), (2025, 15.0)]}
    yield "single year", {**healthy, "operating_margin": [(2025, 22.0)],
                          "profit_margin": [(2025, 16.0)], "fcf": [(2025, 2e9)]}
    yield "empty series", {**healthy, "fcf": [], "operating_margin": []}
    yield "on the threshold", {**healthy, "trailingPE": 37, "roe": 12}
    yield "expensive", {**healthy, "trailingPE": 90, "debtToEquity": 200}


def check_behaviour(cfg, verbose=False) -> list[str]:
    """Grade the same values both ways; the failing-rule lists must match."""
    problems = []
    fund_cfg = cfg.get("fundamentals", {})
    specs = quality.parameters(cfg, quality.STAGE_FAST)
    for name, values in _cases(cfg):
        row = {quality.label_of(k, s): values.get(k) for k, s in specs.items()}
        was = sorted(old_failures(row, fund_cfg))
        now = sorted(quality.gate_failures(values, cfg, quality.STAGE_FAST))
        if was != now:
            problems.append(f"case {name!r}: old failed {was}, new failed {now}")
        elif verbose:
            print(f"  ok  case {name!r}: {now or 'passes'}")
    return problems


def report_score_change(cfg) -> None:
    """State plainly how the unified score differs from the old quant score.

    It is not meant to be identical: the fast parameters (P/E, margins, ROE ...)
    used to gate the badge and nothing else, and folding them into the score is
    the point of unifying. This prints what joined so the change is a decision
    you can see rather than a number that moved.
    """
    dims = (cfg.get("research", {}).get("synthesis", {}).get("dimensions") or {})
    scored_before = {m for d in dims.values() for m in d["metrics"]}
    scored_now = {k for k, s in quality.parameters(cfg).items() if s.get("score")}
    added = sorted(scored_now - scored_before)
    print("\nScore composition")
    print(f"  dimensions before : {', '.join(sorted(dims)) or 'none'}")
    print(f"  groups now        : {', '.join(sorted(quality.groups(cfg)))}")
    if added:
        print(f"  newly scored      : {', '.join(added)}")
        print("  -> the 0-100 score now includes the tier-2 fundamentals. To "
              "reproduce the old\n     number exactly, disable the groups that "
              "hold only fast parameters\n     (quality.groups.shareholder_returns.enabled = false) "
              "or clear those\n     parameters' `score` blocks.")
    else:
        print("  newly scored      : none -- the score is unchanged")


def main() -> int:
    verbose = "--verbose" in sys.argv[1:]
    cfg = load_config()
    if not cfg.get("fundamentals"):
        print("config.json no longer has a `fundamentals` section -- the "
              "migration is done and this script can be deleted.")
        return 0

    problems = []
    for title, fn in (("Gates (the ⭐ badge)", check_gates),
                      ("Score anchors", check_anchors),
                      ("Behaviour, old engine vs new", check_behaviour)):
        print(f"\n{title}")
        found = fn(cfg, verbose)
        for line in found:
            print(f"  MISMATCH  {line}")
        if not found:
            print("  ok -- identical")
        problems += found

    problems += [f"config invalid: {p}" for p in quality.validate(cfg)]
    report_score_change(cfg)

    print(f"\n{'FAILED: ' + str(len(problems)) + ' mismatch(es)' if problems else 'PASS'}")
    return 1 if problems else 0


if __name__ == "__main__":
    enable_utf8_output()
    sys.exit(main())
