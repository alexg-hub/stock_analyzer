"""Offline -- sector-relative scoring (`peers.py`) and its safety properties.

Why this exists: every threshold in the registry is an absolute anchor, and the
first full universe pass measured what that costs. **Utilities were excluded at
83.9%** (26 of 31) on Altman Z, consecutive negative free cash flow and cash
runway -- all of which are structural for a regulated business financing a rate
base, so the rules had stopped describing the company at all.

Invariants, not recorded output:

  * **Ties take the midpoint of their range.** This is the whole ballgame.
    `fcf_negative_years` is capped at the statement window, so 20 of 31 utilities
    hold the identical worst value; counting "peers at or below" gave every one of
    them rank 1.00 and therefore "worst 10% of sector", reproducing the exact
    false positive the module removes. Midpoint ranks them 0.68 and none trips.
  * **The peer layer can only make the veto QUIETER.** It is a second condition
    on an absolute breach, never a rule of its own, so the vetoed set with peers
    enabled must always be a subset of the set without them. A percentile veto on
    its own would exclude a fixed share of every sector forever and stop meaning
    "visibly falling over".
  * **Thin evidence falls back rather than guessing.** A missing stats file, an
    unknown sector, an uncollected parameter and a bucket under `min_peers` all
    return None, and the caller uses the anchor it always did.
  * **Direction comes from the parameter, never from a hardcoded sign.** A score
    orients by the ordering of `good`/`bad`; a veto tail orients by whether the
    gate is a `min` or a `max`.
"""

import copy
import json
import shutil
import tempfile
from pathlib import Path

from _harness import Checks, config

import peers
import quality

c = Checks("sector-relative scoring")

cfg = config()
tmp = Path(tempfile.mkdtemp(prefix="peers_test_"))
cfg["quality"] = copy.deepcopy(cfg["quality"])
cfg["quality"]["peers"] = {"enabled": True, "path": str(tmp / "peer_stats.json"),
                           "min_peers": 12, "veto_percentile": 0.10}
peers.reset_cache()

# --------------------------------------------------------------------------
c.section("no stats at all: everything falls back")

c.ok("a missing stats file loads as empty", peers.load(cfg) == {})
c.ok("...so no distribution is available",
     peers.distribution("altman_z", "Utilities", cfg) is None)
c.ok("...and no percentile", peers.percentile("altman_z", "Utilities", 1.0, cfg) is None)
c.ok("...and no relative score",
     peers.relative_score("altman_z", {"score": {"good": 4, "bad": 1.8}},
                          "Utilities", 1.0, cfg) is None,
     "the caller then uses the absolute anchors, exactly as before")

(tmp / "peer_stats.json").write_bytes(b"{ not json")
peers.reset_cache()
c.ok("a corrupt stats file also loads as empty, without raising",
     peers.load(cfg) == {})

# --------------------------------------------------------------------------
c.section("ties take the midpoint -- the utilities false positive")

# 31 "utilities": 11 below the cap, 20 tied at the worst value, exactly the shape
# `fcf_negative_years` actually has.
tied = [0.0, 1.0, 1.0, 2.0, 2.0, 2.0, 3.0, 3.0, 3.0, 3.0, 3.0] + [4.0] * 20
stats = {"sector_of": {"AEP": "Utilities", "XEL": "Utilities", "MSFT": "Tech"},
         "distributions": {"fcf_negative_years": {"Utilities": sorted(tied)},
                           "altman_z": {"Utilities": sorted(
                               [0.45 + i * 0.06 for i in range(31)])},
                           "thin_metric": {"Utilities": [1.0, 2.0, 3.0]}}}
(tmp / "peer_stats.json").write_text(json.dumps(stats), encoding="utf-8")
peers.reset_cache()

rank = peers.percentile("fcf_negative_years", "Utilities", 4.0, cfg)
c.ok("a value tied with two thirds of its sector ranks mid-cluster, not top",
     rank is not None and 0.6 < rank < 0.8, f"rank {rank}")
c.ok("...so it is NOT in the worst 10% of its sector",
     peers.in_sector_tail("fcf_negative_years",
                          {"gate": {"max": 3}}, "Utilities", 4.0, cfg) is False,
     "counting peers at-or-below put all twenty of them in the tail")

c.ok("the genuinely worst value in a spread metric IS in the tail",
     peers.in_sector_tail("altman_z", {"gate": {"min": 1.1}},
                          "Utilities", 0.45, cfg) is True)
c.ok("...and the sector median is not",
     peers.in_sector_tail("altman_z", {"gate": {"min": 1.1}},
                          "Utilities", 1.35, cfg) is False)

c.ok("a bucket under min_peers is not ranked at all",
     peers.distribution("thin_metric", "Utilities", cfg) is None,
     "three peers is not a distribution")
c.ok("an unknown sector is not ranked",
     peers.percentile("altman_z", "Nowhere", 1.0, cfg) is None)
c.ok("an empty sector string is not ranked",
     peers.percentile("altman_z", "", 1.0, cfg) is None)

c.ok("the ticker -> sector map reads back",
     peers.sector_of("AEP", cfg) == "Utilities"
     and peers.sector_of("aep", cfg) == "Utilities"
     and peers.sector_of("NOPE", cfg) == "")

# --------------------------------------------------------------------------
c.section("direction is taken from the parameter, never assumed")

rising = {"score": {"good": 4.0, "bad": 1.8}}      # higher is better
falling = {"score": {"good": 1.8, "bad": 4.0}}     # lower is better
low, high = 0.45, 2.25
for spec, name in ((rising, "higher-is-better"), (falling, "lower-is-better")):
    lo = peers.relative_score("altman_z", spec, "Utilities", low, cfg)
    hi = peers.relative_score("altman_z", spec, "Utilities", high, cfg)
    better_at_high = spec["score"]["good"] > spec["score"]["bad"]
    c.ok(f"a {name} metric scores in the right direction",
         (hi > lo) == better_at_high, f"low={lo:.2f} high={hi:.2f}")

c.ok("a min-gate veto reads its tail from below",
     peers.in_sector_tail("altman_z", {"gate": {"min": 1.1}},
                          "Utilities", 0.45, cfg) is True
     and peers.in_sector_tail("altman_z", {"gate": {"min": 1.1}},
                              "Utilities", 2.25, cfg) is False)
c.ok("a max-gate veto reads its tail from above",
     peers.in_sector_tail("altman_z", {"gate": {"max": 1.1}},
                          "Utilities", 2.25, cfg) is True
     and peers.in_sector_tail("altman_z", {"gate": {"max": 1.1}},
                              "Utilities", 0.45, cfg) is False)
c.ok("a gate with neither bound has no tail opinion",
     peers.in_sector_tail("altman_z", {"gate": {}}, "Utilities", 1.0, cfg) is None)

# --------------------------------------------------------------------------
c.section("the peer layer can only make the veto quieter")

# Drive every veto parameter to its failing side, then check the enabled/disabled
# sets against each other. This is the safety property: peers is a *second*
# condition, so it can never add an exclusion.
off = copy.deepcopy(cfg)
off["quality"]["peers"]["enabled"] = False

vetoes = quality.veto_parameters(cfg, None)
worst = {}
for key, spec in vetoes.items():
    gate = spec.get("gate") or {}
    if gate.get("min") is not None:
        worst[key] = float(gate["min"]) - 1.0
    elif gate.get("max") is not None:
        worst[key] = float(gate["max"]) + 1.0

for sector in ("Utilities", "Tech", "", "Nowhere"):
    with_peers = set(quality.veto_failures(worst, cfg, None, sector))
    without = set(quality.veto_failures(worst, off, None, sector))
    c.ok(f"sector {sector!r}: the vetoed set is a subset of the absolute one",
         with_peers <= without,
         f"added {sorted(with_peers - without)}" if with_peers - without
         else f"{len(with_peers)} of {len(without)} survive")

c.ok("with no sector, the peer layer changes nothing at all",
     set(quality.veto_failures(worst, cfg, None, ""))
     == set(quality.veto_failures(worst, off, None, "")),
     "an unknown sector must behave exactly as it did before this module")

# --------------------------------------------------------------------------
c.section("build skips what cannot be ranked")

built = peers.build(
    {"AAA": {"values": {"altman_z": 2.0}},
     "BBB": {"values": {"altman_z": 3.0}},
     "ERR": {"values": {"altman_z": 9.0}, "error": "HTTPError"},
     "NOSEC": {"values": {"altman_z": 5.0}}},
    {"AAA": "Utilities", "BBB": "Utilities", "ERR": "Utilities", "NOSEC": ""},
    cfg)
collected = (built.get("distributions") or {}).get("altman_z", {}).get("Utilities")
c.ok("an errored ticker contributes nothing", 9.0 not in (collected or []))
c.ok("a ticker with no sector contributes nothing", 5.0 not in (collected or []))
c.ok("the good ones are collected and sorted", collected == [2.0, 3.0])
c.ok("the sector map excludes blanks", "NOSEC" not in (built.get("sector_of") or {}))

# --------------------------------------------------------------------------
c.section("every production grading call site passes a sector")

# Threading `sector` through `quality` is only half the job: a caller that omits
# it gets absolute anchors for every `sector_relative` parameter, silently, and
# returns a perfectly plausible number. That is exactly what happened -- tier 3's
# `compute_quant_score` and `risk_report` graded on absolute anchors while the
# plane used peers, so the two disagreed about the same company, and
# `universe_scan.collect_one` stored a veto count (92) that the rendered table
# (59) contradicted. Nothing raised, and no output looked wrong on its own.
#
# So this is a source check rather than a behavioural one: there is no observable
# difference to assert without a peer-stats file and a network fetch, and by the
# time a reader notices two tables disagreeing the number has already been used.
import ast

_SECTOR_AWARE = {"evaluate", "axis_scores", "score_of", "group_scores",
                 "veto_failures"}
_ROOT = Path(__file__).resolve().parent.parent
_offenders = []
_checked = 0
for _name in ("research_report.py", "universe_scan.py", "scanner_common.py",
              "run_scanners.py"):
    _path = _ROOT / _name
    if not _path.exists():
        continue
    for _node in ast.walk(ast.parse(_path.read_text(encoding="utf-8"))):
        if not isinstance(_node, ast.Call):
            continue
        _f = _node.func
        if not (isinstance(_f, ast.Attribute) and _f.attr in _SECTOR_AWARE
                and isinstance(_f.value, ast.Name) and _f.value.id == "quality"):
            continue
        _checked += 1
        # `sector` is the 4th positional parameter on each of these, or a kwarg.
        _has = len(_node.args) >= 4 or any(k.arg == "sector"
                                           for k in _node.keywords)
        if not _has:
            _offenders.append(f"{_name}:{_node.lineno} quality.{_f.attr}")

c.ok("the check found the call sites at all", _checked >= 3,
     f"inspected {_checked} calls -- zero would make this test vacuous")
c.ok("no production grading call omits the sector", not _offenders,
     f"absolute anchors would be used silently at: {_offenders}"
     if _offenders else f"{_checked} call sites all pass it")

shutil.rmtree(tmp, ignore_errors=True)
peers.reset_cache()
raise SystemExit(c.finish())
