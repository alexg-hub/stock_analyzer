# Where AI sits in this project

**The analyzer never runs a model.** Every number, tier, verdict and sentence
the pipeline produces is computed in Python, and `tests/test_no_model.py` fails
if any code path could start one. A judgment a model wrote inside the score is
a number nobody can check.

AI is used only from a Claude Code session, through two skills, and what it
concludes is recorded **beside** the graded data, never inside it:

| Surface | AI involvement | Where the result goes |
|---|---|---|
| Tier 1: technical screens | none | `signals.csv` |
| Tier 2: quality ⭐ and veto 🚫 | none | the same row |
| Tier 3: the graded verdict | none | `signals.csv` / `on_demand_scans_results.csv` + `_facts.json` |
| Tier 4: the virtual portfolio | none; it **grades** the AI's record like any other attribute | `output/portfolio/` |
| `enrich` skill | researches a graded ticker: competitive position, capital allocation, whether a tripped rule is a sector artifact | `output/enrichment/enrichment.csv` (categorical fields only) + a prose report |
| `theme-screen` skill | names companies that benefit from a dated real-world event | `signals.csv` under `config_key = "theme_screen"`, graded by the registry **after** the pick |

## The guards

- **No field can carry a score.** `agent_records.JudgmentSchema` rejects
  `conviction`, `tier`, `score`, `narrative_adj` (and, for theme picks,
  `Verdict`, `Reward`, `Risk`, `price_target`) by name, and an invalid row
  raises instead of being recorded.
- **The registry grades every theme pick** after it is named, from a fresh
  download. A ticker with no bars is refused, so an invented or foreign symbol
  never reaches a table tier 4 buys from.
- **The direction is one-way:** a session may call the analyzer; the analyzer
  may not call a session.
- **No account surface:** sessions never call IBKR `get_account_*` /
  `get_pa_*`. A security is graded on its own merits.

## Why it is set up this way

A narrative pass once ran nightly and could adjust the conviction by up to ±15
points. Over its whole life it moved nine convictions by at most 5 points and
changed no tier, while making the one score in the system unverifiable. It was
removed on 2026-08-11. The judgment it represented now lives in the enrichment
record, where tier 4 measures whether it predicts anything.

```powershell
python tests/run_all.py             # test_no_model.py is the guard
python enrichment.py show TJX       # what the agent concluded, if anything
python theme_signals.py show        # what the theme screen picked
python -m portfolio_sim analyze     # grades the agent's record against returns
```
