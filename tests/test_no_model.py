"""Offline -- the analyzer cannot invoke a model.

This is the test that makes "deterministic" a property of the code rather than a
claim in a docstring. Everything `run_scanners.py` produces -- the screens, the
quality verdict, the veto, the 0-100 score, the tier, both axes, the ledger and
the exit scan -- is computed in Python, so a re-run reproduces it and every
figure on a card can be traced to the config that produced it.

That held by convention until 2026-08-11, when tier 3 also ran a headless
`claude -p` pass that wrote a bounded `narrative_adj` into the recorded
conviction. Measured over its whole life it moved nine verdicts by at most 5
points, changed no tier, and cost ~$6 a night with 3 of 5 logged runs ending in
error -- while making the one score in the system unverifiable (`AI_ROLE.md`).
It was removed, and this file is what stops it coming back by accident.

Qualitative research still happens; it is the `enrich` skill, invoked from a
Claude Code session, and it records to its own table where tier 4 grades it.
The distinction this file defends is not "no AI" -- it is **no AI inside the
measurement**. A session may call the analyzer; the analyzer may not call a
session.

Nothing here is a style check. Each pattern below is a way a model could re-enter
the pipeline *silently*: a subprocess whose failure is a missing report rather
than an exception, a config key that turns one on, a column that carries a
model's number into a scored table.
"""

import ast
import json
import re
from pathlib import Path

from _harness import Checks

import enrichment
import scanner_common

c = Checks("no model in the analyzer")

ROOT = Path(__file__).resolve().parent.parent

# `.claude/` is the agent's own territory -- skills, settings and the MCP
# registration are *supposed* to describe model work. Everything else in the
# repo is the analyzer.
AGENT_DIRS = {".claude"}
SKIP_DIRS = {"output", "__pycache__", ".git", "tests"} | AGENT_DIRS


def production_files(suffixes: set[str]) -> list[Path]:
    out = []
    for path in ROOT.rglob("*"):
        if path.suffix not in suffixes or not path.is_file():
            continue
        if set(path.relative_to(ROOT).parts) & SKIP_DIRS:
            continue
        out.append(path)
    return out


PY_FILES = production_files({".py"})
BAT_FILES = production_files({".bat"})

c.ok("the scan found the production tree at all",
     len(PY_FILES) >= 15 and BAT_FILES,
     f"{len(PY_FILES)} .py, {len(BAT_FILES)} .bat -- zero would make this "
     "whole file vacuous")


# --------------------------------------------------------------------------
c.section("no code path starts a model")

# `claude -p` is the headless invocation. A .bat that carries one is a nightly
# job that reaches a model; a .py that carries one is worse, because it would
# sit inside the measurement itself.
INVOCATION = re.compile(r"claude(\.exe)?\s+(-p\b|--print\b)")
offenders = [f"{p.relative_to(ROOT)}" for p in PY_FILES + BAT_FILES
             if INVOCATION.search(p.read_text(encoding="utf-8", errors="replace"))]
c.ok("no production file invokes `claude -p`", not offenders, str(offenders))

# The allow-list flags only exist to hand a model its permissions. Their
# presence anywhere in the analyzer means a headless run came back.
for flag in ("--allowedTools", "--permission-mode", "--session-id"):
    hits = [str(p.relative_to(ROOT)) for p in PY_FILES + BAT_FILES
            if flag in p.read_text(encoding="utf-8", errors="replace")]
    c.ok(f"no production file passes {flag}", not hits, str(hits))

# A subprocess is the quiet way back in: `subprocess.run(["claude", ...])`
# raises nothing a caller would notice, so the symptom would be a missing
# section rather than an error.
spawns = []
for path in PY_FILES:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node.value.strip().lower() in ("claude", "claude.exe"):
            spawns.append(f"{path.relative_to(ROOT)}:{node.lineno}")
c.ok("no production module names `claude` as a command", not spawns, str(spawns))


# --------------------------------------------------------------------------
c.section("no config key can switch one on")

# The committed file, not `config()`: the harness deliberately overrides
# `research.logging` to redirect the step log, so asking the fixture what keys
# exist would be asking the wrong object.
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
research = cfg.get("research", {})
c.ok("research.narrative is gone", "narrative" not in research,
     "the switch that gated the headless pass")
c.ok("research.synthesis.narrative_adj_max is gone",
     "narrative_adj_max" not in research.get("synthesis", {}),
     "a bound is only needed for a number a model writes")
c.ok("the run manifest key is gone",
     "manifest" not in research.get("logging", {}),
     "deepdive_runs.csv recorded model runs; there are none")

flat = str(cfg)
c.ok("no config value mentions a model name",
     not re.search(r"\b(opus|sonnet|haiku|gpt-|claude-)\b", flat, re.I),
     "a model name in config is a model being chosen")


# --------------------------------------------------------------------------
c.section("no model-authored number reaches a scored column")

c.ok("`Narrative Adj` is not a recorded verdict column",
     "Narrative Adj" not in scanner_common.VERDICT_COLS)

import research_report  # noqa: E402 - after the config checks, deliberately

c.ok("the on-demand table no longer carries a narrative adjustment",
     "Narrative Adj" not in research_report.ON_DEMAND_VERDICT_COLS,
     str(research_report.ON_DEMAND_VERDICT_COLS))
c.ok("the deterministic verdict emits no adjustment field",
     "narrative_adj" not in str(research_report.verdicts_for.__doc__ or "")
     and not hasattr(research_report, "clamp_narrative_adj"),
     "a bound that has to be clamped implies a number that has to be trusted")

from portfolio_sim import analysis  # noqa: E402

c.ok("tier 4 grades no model-authored dimension",
     "Narrative Adj" not in analysis.DIMENSION_COLS,
     str(sorted(analysis.DIMENSION_COLS)))

# The replacement, and the property that makes it safe: the agent's record has
# no field that could move a score, enforced by name rather than by convention.
c.ok("the enrichment schema contains no score",
     not ({"conviction", "tier", "score", "narrative_adj"}
          & set(enrichment.COLUMNS)),
     "the agent records judgment; it cannot adjust a verdict")
for banned in ("narrative_adj", "conviction", "tier", "score"):
    c.ok(f"an enrichment row carrying {banned!r} is rejected",
         any(banned in p for p in enrichment.validate(
             {"scan_date": "2026-01-01", "ticker": "AAA", "stance": "bull",
              banned: 10})))


# --------------------------------------------------------------------------
c.section("the entry points that remain")

c.ok("run_scanner.bat runs exactly one python command",
     sum(1 for line in (ROOT / "run_scanner.bat").read_text(
         encoding="utf-8", errors="replace").splitlines()
         if "python.exe" in line and not line.strip().startswith("rem")) == 2,
     "the run-id mint and the scan itself -- nothing chained after")
c.ok("the deep-dive skill is gone",
     not (ROOT / ".claude" / "skills" / "deep-dive").exists(),
     "replaced by .claude/skills/enrich")
c.ok("the enrich skill is present",
     (ROOT / ".claude" / "skills" / "enrich" / "SKILL.md").exists())

raise SystemExit(c.finish())
