# Where AI sits in this pipeline — and what it measurably contributed

Third companion to `RESEARCH_DATA.md` (what each *source* can supply) and
`DETERMINISTIC_GAPS.md` (what the registry is *not yet computing*). This one
answers a different question: **who computes what**.

> **Status: acted on.** This file was written on 2026-08-11 as an analysis of
> where a model sat in the pipeline. Its measurement (§3) is what settled the
> question, and the separation it recommended was implemented the same day.
> The analyzer is now deterministic end to end — `tests/test_no_model.py`
> asserts no code path in it can start a model — and qualitative research is the
> `enrich` skill, which records to `output/enrichment/` where tier 4 grades it.
> §1-4 below describe the system **as it was**, because the measurement is only
> legible against it; §5 records what changed and §7 is where it stands now.

The repo already stated the rule it meant to follow. `DETERMINISTIC_GAPS.md:8-11`:

> **anything derivable is computed in Python.** A number a model wrote is a number
> you cannot check, which is why tier 3's thesis, tier 4's `conclusion` and the
> whole verdict are already deterministic. The AI's territory is section D, and
> nothing else.

The gap this file found was between that rule and the running system: the nightly
chain still spawned `claude -p`, and that pass wrote a number into a scored
column. Everything from here to §5 is what was true before that was closed.

---

## 1. Every place a model ran

*As of 2026-08-11, before the separation.* Two processes spawned one, one skill
drove them, and one number was allowed back.

| Tier | What it decides | AI involvement |
|---|---|---|
| 1 — technical screens | what fired | **none** |
| 2 — quality ⭐ and veto 🚫 | is this a good company / is it visibly falling over | **none** |
| 3 — graded verdict | tier + conviction | deterministic half: **none**. Narrative half: all of it |
| 4 — virtual portfolio | which attribute predicted the return | **none** |

- **`run_deepdive.bat:81-86`** — the nightly narrative pass. `claude -p`, model
  from `research.narrative.model` (`opus`), gated by `research.narrative.enabled`,
  prompt written by `research_report.auto_prompt` (`research_report.py:1334`).
- **`run_ondemand.bat:63-68`** — the same invocation for named tickers, with a
  byte-identical allow-list by design.
- **`mcp_tools/universe.py:83-157` `risk_research_impl`** — assembles a bundle and
  a prompt and **runs no model at all**; the calling session does the reasoning.
  Its stated reason is worth keeping: a third enumerated allow-list would be
  *"a third way to lose a report section with no error to explain it. A tool that
  returns a bundle needs no subprocess, no allow-list and no new silent-failure
  mode."*

Tier 4 deserves its own line, because it is the one that grades everything else.
`portfolio_sim/analysis.py:11-14`:

> Every conclusion sentence is generated in Python from the numbers in its own
> row. That is the same rule tier 3 follows for its chart and its figures: this is
> a measurement, and a measurement narrated by a model is a measurement you cannot
> check.

---

## 2. Step by step: what the model touched vs. what it authored

The procedure is `.claude/skills/deep-dive/SKILL.md`. Eight steps; only the last
three produce anything that persists.

| Step | Line | What the model does | Persists? |
|---|---|---|---|
| 0. pick tickers | `:64` | none — reads `research_report.py candidates` | no |
| 1. deterministic bundle | `:83` | none — consumes `context TICKER` JSON | no |
| 2. **IBKR MCP (Tier B)** | `:97` | moat / competitor / theme evidence | prose only |
| 3. live web research | `:110` | WebSearch + WebFetch, last 30-60 days | prose only |
| 4. filings | `:114` | MD&A, Risk Factors, Item 1 | prose only |
| 5. disaster symptoms | `:120` | interprets `risk TICKER`'s tripped/clean/unknown | prose only |
| 6. grade qualitative dims | `:140` | **produces `narrative_adj`** | **recorded** |
| 7. verdict | `:152` | **produces `conviction` + `tier`** | **recorded** |
| 8. write report + record | `:161` | markdown report; `{ticker, scan_date, tier, conviction, narrative_adj, thesis}` | report archived; the dict **recorded** |

So across the whole procedure, exactly **three model-authored fields reach a
permanent record**: `narrative_adj`, `conviction`/`tier`, and the free-text
`thesis`. Everything numeric on the Discord card is read back from
`<TICKER>_<date>_facts.json`, which `deterministic_verdict`
(`research_report.py:414`) wrote *before* the model ever ran — `:436-437`:
*"The chart and the facts file are written here, before this returns, so a model
that reads the bundle later can never generate either."*

### The four guardrails, and where each is enforced

These matter because they are why the current design is defensible even before
anything changes. Each one replaced an instruction with a mechanism.

| Guardrail | Where | What it stops |
|---|---|---|
| `clamp_narrative_adj` | `research_report.py:890` | The adjustment is clamped to ±`research.synthesis.narrative_adj_max` (**15**), an out-of-range value is logged as a `VERDICT warn` rather than honoured, and conviction is then **recomputed from the recorded quant score** — so a rejected adjustment cannot smuggle a conviction in behind it. Its docstring is explicit that this was previously *"enforced only by the skill file asking the model to respect it -- i.e. by the model's own compliance."* |
| `enforce_veto` | `research_report.py:928` | The model *"can argue the rule is wrong -- in the report, where a human reads it and can retune the threshold -- but not by relabelling this row."* The veto flag and its reasons are written by Python before the model runs; the model cannot edit `facts["veto"]` at all. |
| `deterministic_thesis` | `research_report.py:481` | A one-sentence thesis built in Python from the group breakdown, so a night with no narrative pass still has one on the card. |
| Subcommand-only `Bash` | `run_deepdive.bat:85` | `Bash(python research_report.py *)` is a prefix rule and every segment of a compound command must be allowed, so `python -c`, redirects and scratch scripts are refused — never widen it to `Bash(python *)`, which is arbitrary code execution. |

Add `financials_table_md` (`research_report.py:272`) and the chart: both are
rendered so *"the model pastes a block instead of retyping figures -- one less
place for a digit to drift."*

---

## 3. What the model actually contributed to the record

The pipeline has been running long enough to answer this from data rather than
argument. Sources: `output/history/signals.csv` × `output/reports/*_facts.json` ×
`output/logs/deepdive_runs.csv`.

**The narrative pass has revised a conviction nine times, ever** — all between
2026-07-23 and 2026-07-29:

| Ticker | Date | quant | recorded | `narrative_adj` | tier before → after |
|---|---|---|---|---|---|
| GM | 2026-07-23 | 70.0 | 67 | −3.0 | WATCH → WATCH |
| HLT | 2026-07-23 | 54.1 | 58 | +3.9 | WATCH → WATCH |
| RL | 2026-07-23 | 79.6 | 76 | −3.6 | WATCH → WATCH |
| ABNB | 2026-07-28 | 60.2 | 63 | +2.8 | WATCH → WATCH |
| APH | 2026-07-28 | 87.5 | 86 | −1.5 | STRONG → STRONG |
| BKR | 2026-07-28 | 69.3 | 74 | +4.7 | WATCH → WATCH |
| ADI | 2026-07-29 | 84.3 | 81 | −3.3 | STRONG → STRONG |
| AEE | 2026-07-29 | 54.0 | 59 | +5.0 | WATCH → WATCH |
| AEP | 2026-07-29 | 39.0 | 42 | +3.0 | PASS → PASS |

Three readings, all of them uncomfortable for the narrative pass:

- **The bound has never bound.** Largest adjustment +5.0 against ±15. The clamp
  added at `research_report.py:890` has never had to fire in production.
- **Zero tier changes.** Against the tier table in force (`STRONG ≥80 /
  WATCH ≥45 / PASS ≥0`) every one of the nine lands in the same tier before and
  after. The recorded *label* — which is what the alert shows and what tier 4
  grades — has never once differed from the deterministic one.
- **It has not run successfully since 2026-07-29.** The 20 verdicts recorded from
  2026-07-31 onward (FCX, DDOG, IVZ, NUE, SWK, WSM, ZBRA, CBOE, PLD, ABNB, FAST,
  HPQ, CVX, DD, MAR, ABNB, NTAP, EVRG, HUBB, TJX) equal their quant score
  **exactly**, and no nightly report `.md` has been written since ADI/AEE/AEP on
  2026-07-29. Nobody noticed, because nothing downstream depends on it.

### What it costs, from `output/logs/deepdive_runs.csv`

| run | date | exit | turns | duration | cost |
|---|---|---|---|---|---|
| `4ce5c200` | 07-28 | ok | 64 | 873 s | $5.76 |
| `d2af3752` | 07-29 | ok | 62 | 899 s | $6.33 |
| `710e2c61` | 07-27 | error | 55 | 1013 s | $6.38 |
| `f2306859` | 07-30 | error | 27 | 141 s | $1.75 |
| `4ea48b00` | 08-10 | error | 1 | 0.9 s | $0.00 |

≈ **$6 and ~15 minutes per night** for three tickers, and **3 of 5 logged runs
ended `error`**. Run `4ea48b00` never launched at all — the manifest recorded the
model as `{}` and one turn — and that night's five verdicts (EVRG, HUBB, TJX,
ABNB, NTAP) were still computed, recorded and posted, marked `(deterministic)`.
**The fail-safe is not theoretical; it has been the operating mode for two weeks.**

### The honest conclusion

The narrative pass's measured contribution to the *record* is approximately nil.
Its real product is the prose: **13 archived reports of 2,800-5,300 words**, whose
"Business & moat", "Growth potential" and "Positioning & sentiment" sections are
the only things in this repo built on evidence Python cannot fetch.

That is not an argument that the pass is worthless. It is an argument that it is
a **research artifact for a human, not an input to a score** — and it is currently
wired as both.

---

## 4. How IBKR MCP is used, and its ceiling

IBKR reaches this project by two entirely separate roads that are easy to
conflate.

### 4a. The MCP connector — narrative pass only

`SKILL.md:97-109` names **4** of the 9 allow-listed tools:

| Tool | What it supplies | Which report section |
|---|---|---|
| `search_contracts` | exact symbol + US primary listing → `conid` | (plumbing) |
| `get_company_connections` | moat, ranked competitors *and how they compete*, product/revenue mix, geography — each with a grounded evidence paragraph | `## Business & moat` |
| `get_company_themes` | secular themes, ranked peers | `## Growth potential` |
| `get_price_snapshot` | 52w range, historical vol, **IV percentile**, avg 90d $ volume | `## Snapshot`, `## Positioning & sentiment` |

Confirmed live in the archived reports — e.g. *"IBKR's connection graph anchors
ABNB on Travel Platform (rank 1)…"*, *"IBKR ranks Booking Holdings (BKNG) and
Expedia/Vrbo (EXPE) as the direct global rivals"*. All 13 reports cite it.

**The ceiling is hard and worth restating**: `RESEARCH_DATA.md:53` and
`ibkr.py:30-32` both record that `get_company_connections`,
`get_company_themes` and `search_investment_topics` are **Reflexivity products
with no public-API equivalent**, and that the IV *percentile* is a Reflexivity
computation, not an IBKR field. This is the one capability in the whole system
that cannot be moved into Python — not for lack of effort, but for lack of a
source. Everything else in `DETERMINISTIC_GAPS.md` sections A-C is arithmetic
waiting to be written.

**Account data is out of scope, enforced three ways**: absent from both `.bat`
allow-lists (enumerated, not wildcarded, precisely so `get_account_*` / `get_pa_*`
stay unreachable); absent from `ibkr.py` entirely (`FORBIDDEN_CALLS` plus a test
asserting none is called); and prohibited in `SKILL.md:104-109`. The rationale,
from `CLAUDE.md:1084`: *"A capability that was never written is stronger than an
instruction a model can talk itself past."*

### 4b. The TWS socket client — wired but cold

`ibkr.py` is a separate, fully tested path that has nothing to do with MCP. It is
switched off at two levels: `ibkr.enabled: false`, and all three `ibkr_*` registry
parameters `enabled: false` — so `quality.sources_needed` never asks for the
resolver and `ibkr.metrics` is never called. `ib_async 2.1.0` *is* installed here.

The reason is a subscription, not a defect: `reqFundamentalData` returns **error
10358** because the account lacks the Reuters Worldwide Fundamentals add-on, hence
`reports: []`. What still works is market statistics — 52/26/13-week range, average
volume, historical and implied volatility — which would populate
`ibkr_implied_volatility`. Dormant, not dead; out of scope for this document, but
it is deterministic data currently switched off.

---

## 5. The answer, and what was done

**Yes — and the repo was already most of the way there, by design rather than by
accident.** Tiers 1, 2 and 4 contained no model at all; tier 3's verdict, thesis,
chart and table were Python *specifically* so a model could not write them;
`risk_research` was already the bundle-and-prompt shape the question describes.

The residue was one coupling: `narrative_adj` flowed into a recorded column that
tier 4 then graded. Both sides had a real argument — *keep it*, because tier 4 can
only measure what it records, and a bounded adjustment on the record is a
hypothesis under test rather than an unchecked input; *cut it*, because nine
samples had produced zero tier changes at ~$6 a night with a 60% run-failure
rate, and the pass had been dead for two weeks without consequence.

**Implemented 2026-08-11.** The tie-break was that the *keep it* argument does
not require the adjustment to touch the score — only that the judgment be
recorded and gradeable. So the judgment stayed and the coupling went:

| Before | After |
|---|---|
| `run_deepdive.bat` / `run_ondemand.bat` → `claude -p`, two byte-identical allow-lists | deleted; nothing chains after `run_scanners.py` |
| `narrative_adj` → recorded conviction, clamped at ±15 | no such field; `enrichment.validate` rejects it by name |
| the model's judgment reached tier 4 as one number | it reaches tier 4 as `en_stance`, `en_moat_view`, `en_social_sentiment`, `en_sources_n`, `en_concerns_n` — graded by group split, like `qr_*` and `vt_*` |
| `.claude/skills/deep-dive` | `.claude/skills/enrich`, session-only, no allow-list to drift |
| "deterministic" was a convention | `tests/test_no_model.py` — no production `.py` or `.bat` may invoke `claude -p`, name it as a command, pass `--allowedTools`/`--permission-mode`/`--session-id`, or carry a config key that selects a model |

What did **not** change: `enforce_veto` and `_warn_tier_drift` stayed (they guard
every way a verdict can be recorded, not just a model's), `research.auto.*`
stayed (it gates the *deterministic* verdict run despite the name), and
`prune_run_logs` was re-homed into `run_scanners.main()` — it had been called
only from `log-session`, so the one job that bounded `output/logs/` was the one
job allowed not to run.

The one gap §4 identified was closed at the same time: `risk_research`'s prompt
now names which IBKR tool answers which question, so the sole door to AI judgment
points at the only evidence source that can answer question 1.

## 6. Inconsistencies found while mapping this

All three were resolved by the 2026-08-11 separation; kept here because each one is a failure mode worth recognising again.

1. **`SKILL.md:313-314` asks the model for account data.** The
   `## Positioning & sentiment` report template still reads
   *"IV percentile, analyst distribution, ownership/insider, **account (held?
   size)**."* That contradicts the prohibition at `SKILL.md:104-109`, both `.bat`
   allow-lists, `RESEARCH_DATA.md:82` and `CLAUDE.md:661`, all of which say
   holdings must never appear in a report. Under the unattended run the tools are
   unreachable, so no report has carried it — but an **interactive** deep-dive in a
   session with the IBKR connector has no such barrier, and the template is
   instructing it to ask. Highest-priority item here; it is a one-line deletion.
2. **Five of nine allow-listed IBKR tools are never instructed.**
   `get_theme_details`, `search_investment_topics`, `get_price_history`,
   `get_option_parameters` and `get_option_data` are permitted in both `.bat`
   files but appear nowhere in `SKILL.md`, so in practice they are unused. Either
   instruct them (see §5) or narrow the allow-list — a permission granted and
   never exercised is surface with no benefit. **Resolved**: both allow-lists are gone, so the session's own permissions govern and there is nothing to keep in sync. `risk_research`'s prompt now names the tools worth calling.
3. **`DETERMINISTIC_GAPS.md:219` describes a live dependency in the past tense.**
   It files supply-chain / customer graphs under "No free source" as *"the old
   claude.ai IBKR connector"*, while `SKILL.md` and both `.bat` files treat that
   connector as a current, load-bearing input to every report. **Resolved**: the graph is reached from the `enrich` skill, which is a live dependency of enrichment and of nothing in the analyzer.

---

## 7. Where it stands now

| Tier | AI involvement |
|---|---|
| 1 — technical screens | none |
| 2 — quality ⭐ and veto 🚫 | none |
| 3 — graded verdict | none |
| 4 — virtual portfolio | none — and it grades the agent's record like any other attribute |
| the `enrich` skill | all of it — session-only, records to `output/enrichment/` |
| the `theme-screen` skill | all of it — session-only, records to `signals.csv` under `config_key = "theme_screen"` |

The `theme-screen` skill is the one addition that goes the *other* way, and it is
worth being precise about why it does not breach anything above. Every other
surface here has AI **judging** something the analyzer already found; this one has
AI **finding** something the analyzer cannot — a beneficiary of a dated real-world
event, which no rolling window over OHLCV can reach. The boundary is unchanged
because the direction of the numbers is unchanged: the agent supplies a ticker
and a falsifiable mechanism, and every figure on the recorded row — the ⭐ badge,
the 🚫 veto, both plane axes, and the tier-3 verdict if it is asked for — is
computed by the registry *after* the pick, from a fresh download. `validate`
rejects `conviction`, `tier`, `score`, `Verdict`, `Reward`, `Risk` and
`price_target` by name, and the scan that grades the pick is also what proves the
ticker is real and US-listed before it can reach a table tier 4 buys from.

So the rule survives intact: **a session may call the analyzer; the analyzer may
not call a session.** `theme_signals.py` runs no model and is in neither screen
registry — `tests/test_theme_screen.py` pins both absences, because
`backtest_universe.SCREENS` would enrol it in `test_signal_contract.py`, which
demands a full-history mask a list dated today cannot honestly supply.

```powershell
python tests/run_all.py             # test_no_model.py is the guard
python run_scanners.py --no-send    # all four tiers, no model in the process
python enrichment.py show TJX       # what the agent concluded, if anything
python theme_signals.py show        # what the thematic screen picked
python -m portfolio_sim analyze     # the roadmap names the agent's questions
```

The measurement behind §3 is reproducible from `output/history/signals.csv`
(Verdict / Conviction per `(scan_date, ticker)`, de-duplicated — a two-screen
night has two rows) against `output/reports/<T>_<date>_facts.json`
(`quant_score`). The run manifest it cites, `output/logs/deepdive_runs.csv`, is
no longer written; its five rows were the whole history, not a truncated tail,
so the absence of runs after 2026-07-30 was meaningful rather than rotation.
