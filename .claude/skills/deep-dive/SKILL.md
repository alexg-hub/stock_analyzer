---
name: deep-dive
description: Produce a graded investment-case deep-dive for a scanner signal — a config-driven quant score anchored by Claude's narrative (moat, growth, earnings/management, news & rumors) → a tier + 0-100 conviction verdict, the full report archived under output/reports and a combined verdict summary posted to Discord. Use when the user asks to deep-dive, analyze, or build an investment case for one or more tickers from the nightly scan.
---

# Deep-dive stock analysis

This skill is **tier 3** of the stock_analyzer pipeline. Tier 1 (technical
screens) and tier 2 (a fast fundamentals-quality check) run nightly in
`run_scanners.py` and hand off `output/latest_hits.json`; this skill turns a
signalling ticker into a full investment case.

**The verdict already exists when you start.** This is the most important thing
to know about this skill. `run_scanners.py` computes tier 3's quant score
deterministically inside the nightly scan, sets `conviction = score` and
`tier = tier_for(score)`, records both to `output/history/`, and posts them in
the same Discord message as the signal. Your job is the **narrative half** —
moat, growth runway, earnings/management quality, catalysts and risks — plus a
bounded `narrative_adj` that *revises* that conviction. You are not grading an
ungraded ticker, and you must never re-derive the score.

Everything mechanical is in `research_report.py`, `quality.py` and `sec.py`.
The judgment is yours.

Each row carries **both earlier tiers' verdicts**:

- **Tier 1** — `Setup` is `full` (every condition held) or `partial` (breakout:
  3 of 4; reclaim: 1-2 confirmations failed), with `Missing` naming what failed.
  Treat a partial setup as a weaker technical trigger and say so in the report.
- **Tier 2** — `Quality` is the ⭐ badge decision and `Quality Missing` lists the
  `quality.parameters` keys whose **gate** failed. Absent keys mean the scan did
  not evaluate quality, which is different from failing it. A parameter with
  `enabled: false` is not evaluated at all and will never appear. The gate set
  is deliberately strict, so most S&P 500 names fail at least one.

Tier 2's gates and tier 3's score are now two halves of **one** registry
(`config.json`'s `quality` section) rather than two systems, so a `Quality
Missing` entry and a low group score are often the same fact seen twice — say
so when they are, rather than reporting them as independent evidence.

**Framing (non-negotiable):** this is research analysis, **not investment advice**.
The "verdict" is an analytical rating, never a buy/sell instruction. Put the
disclaimer line (below) in every report and the Discord summary.

**Which surface to use.** The commands below are the CLI, and the unattended
nightly run executes this skill through `claude -p` behind a
`Bash(python research_report.py *)` allow-list — so they must stay as they are.
Interactively you may use the `stock_analyzer` MCP tools instead
(`deepdive_candidates`, `deepdive_context`, `deepdive_post_verdicts`); they call
the same functions. Either is correct; do not mix them within one report.

## Prerequisites

- `output/latest_hits.json` exists (run `python run_scanners.py` first, or work
  ad-hoc). `research_report.load_hits` resolves it via
  `scanner_common.output_dir()`. A ticker that is *not* in it is fine: step 1
  scans it on demand, so an ad-hoc name arrives with a real trigger and a real
  tier-2 verdict rather than "not evaluated".
- IBKR MCP authenticated in this session (Tier B). If not, run `/mcp`.
- SEC filings need only `research.sec.user_agent` (email-bearing, already in config) — no API key.
- Confirm with the user before any **live** Discord send — except in unattended
  mode (below), where config carries that authorization.

## Step 0 — which tickers

If you were given tickers, use them. If you were asked to deep-dive "tonight's
hits" or similar, get the list rather than guessing:

```powershell
python research_report.py candidates          # the configured tier-2 gate
python research_report.py candidates --all    # every tier-1 hit, gate ignored
python research_report.py candidates --json   # same, machine-readable
```

Output is ranked quality-pass first, then `full` before `partial`. The gate
(`research.auto.gate`: `quality_pass` or `all`) is a **fundamentals filter, not
an override** — a ticker the user names explicitly is always analyzed, whatever
tier 2 said about it. If the gate holds everything back, say so and offer
`--all` rather than silently reporting nothing.

## Procedure — per ticker

1. **Deterministic bundle.** `python research_report.py context TICKER` → JSON:
   `trigger` (which screen fired + row), `yahoo` (Tier-A, including a
   `financials` history), `quant` (`score` 0-100 + per-dimension breakdown +
   metrics), `filings` (SEC 10-Q/10-K MD&A / Risk Factors / Business sections +
   curated XBRL, or null), plus three things already written to disk for you:
   `financials_chart` (a rendered PNG path), `financials_table_md` (the same
   numbers as a markdown table) and `facts_path`.
   The quant score is your **anchor** — do not recompute it, reason on top of it.
   `source` says where the trigger came from: `signal` (tonight's hand-off) or
   `on_demand` (tiers 1 and 2 were just run for this ticker because the scan
   never surfaced it). On-demand, `trigger.screen` may be *"No active technical
   signal"* with `Setup: none` — report that plainly as the trigger; it is an
   answer, not a gap. `python research_report.py scan TICKER` runs the same two
   tiers on their own if you want them without the full bundle.
2. **Tier B — IBKR (MCP).** `search_contracts(TICKER)` → the row with exact symbol
   + US primary listing (`country_code=US`, `STK`) → `underlying_contract_id`. Then:
   `get_company_connections(conid, include=["link_info"])` (moat, competitors,
   products/revenue mix, geography — with evidence), `get_company_themes(conid)`
   (ranked peers / secular themes), `get_price_snapshot(conid, [...])` (52w range,
   historical_vol, implied_volatility_percentile, avg_90d_usd_volume, dividend_yield).
   **Never call `get_account_positions`, `get_account_balances`,
   `get_account_summary`, `get_account_trades` or `get_pa_*`.** The user's real
   IBKR book is out of scope: a deep-dive grades the *security*, and tier 4
   grades the signal against a fixed-notional virtual ledger, so what is or is
   not already held changes neither one. Do not mention holdings, position size
   or concentration in the report — flagging "already held" is exactly what is
   being asked for here, and it is not wanted.
3. **Live web research.** `WebSearch` (and `WebFetch` on the best sources) for the
   last ~30-60 days: earnings/guidance, M&A or **rumors**, product news, analyst
   upgrades/downgrades, insider/buyback, litigation/regulatory. **Link each item to
   the technical trigger** ("why it may be moving"). Label rumors (see Rules).
4. **Filings analysis** (`filings`): read the latest 10-Q/10-K **MD&A** for
   management's account of results drivers, margins, liquidity, and forward/guidance
   statements (and changes vs the prior period); **Risk Factors** for the bear case;
   **Business** (Item 1) to corroborate the moat; cross-check the **XBRL** figures
   against Yahoo. If a section is `n/a` or stubby, `WebFetch` that filing's `url` for
   the item. Quote sparingly with attribution.
5. **Grade the qualitative dimensions** (your judgment, and the only thing not
   already in the quant score): moat width/durability, growth runway,
   earnings/management quality, catalysts vs risks. Decide a **narrative
   adjustment** in `[-N, +N]` where
   `N = config research.synthesis.narrative_adj_max`, with a one-line justification
   for each material ± move. An adjustment of **0 is a legitimate answer** — the
   recorded verdict already stands, and moving it needs a reason you can write
   down.
6. **Verdict.** `conviction = clamp(round(quant.score + narrative_adj), 0, 100)`;
   `tier = research_report.tier_for(conviction, cfg)` (config bands STRONG/WATCH/PASS).
   You may override the tier **only** with an explicit written justification.
   Note this *revises* the conviction already recorded and already announced:
   if you move it, the report should say what the narrative saw that the numbers
   did not.
7. **Write the full report** (template below) and archive it: resolve the folder
   with `research_report.report_dir(load_config())` — `output/reports/` — and use
   the **Write tool** to create `<TICKER>_<scan_date>.md` there (Write handles the
   multiline markdown cleanly; `write_report` exists too but Write is simpler for
   prose).
8. Record only your **judgment** for the batch:
   `{ticker, scan_date, tier, conviction, narrative_adj, thesis}`. Every number
   on the Discord card (price, upside, P/E and its percentile, quant score,
   trigger, quality screen, next earnings) is read back from `facts_path` —
   don't retype any of it, and don't worry that you left it out.

## After all tickers

Write the batch's verdicts to `output/reports/<scan_date>_verdicts.json` with the
**Write tool** — a JSON array of
`{ticker, scan_date, tier, conviction, narrative_adj, thesis}` — then deliver them:

```powershell
python research_report.py post-verdicts output/reports/<scan_date>_verdicts.json
python research_report.py post-verdicts output/reports/<scan_date>_verdicts.json --send
```

Without `--send` it prints the cards; with `--send` it posts to the live channel,
and it refuses to send anyway unless `research.auto.discord_send` is true. Use
`--send` only after the user confirms, or immediately in unattended mode where
config has already authorized it.

**The permanent record is automatic** — `post-verdicts` writes each tier and
conviction to `output/history/`, either onto the ticker's `signals.csv` row or
into `on_demand_scans_results.csv`, and it does so with or without `--send`
(the record is the point; the notification is not). You do not write, update, or
even read those files. If it warns that a tier disagrees with the config bands,
that is information for the user, not something to go back and "fix" — say so in
your summary and leave the verdict as you set it.

**So is the step log** — `output/logs/<run_id>.log` gets one line per step of
the run, and your half of it is rendered afterwards from the session transcript
by `log-session`, which the batch file calls when you are done. Never write to
it, and never narrate your steps into a file yourself: the record is built from
what actually happened, not from what you report happened. It also means you can
say "see the run log" instead of listing every source you touched.

## Unattended (nightly) mode

`run_deepdive.bat` runs this skill headlessly after the nightly scan, via a
prompt from `research_report.py auto-prompt` that says so explicitly. In that
mode:

- **Never ask a question** — nobody is there. Every decision comes from config
  or from your own judgment.
- **Keep every shell command a single, un-redirected invocation of a documented
  subcommand.** The run's allow-list is narrow on purpose, and Claude Code
  requires *every* segment of a compound command to be allowed — so `cmd > file`,
  `cmd; echo $?`, `python -c "..."`, a scratch `.py` you wrote, and any
  `PowerShell(...)` will all be **silently refused**, not error. The 2026-07-26
  shakedown lost its verdict post to exactly this. Everything you need is a
  subcommand: `context`, `post-verdicts`, `candidates`. If you find yourself
  wanting a one-liner, you want a subcommand that does not exist yet — say so in
  the report rather than working around the allow-list.
- **The Discord send is pre-authorized** by `research.auto.discord_send`, which
  the prompt passes as `send=<true|false>`. That config flag *is* the user's
  confirmation; do not wait for another one, and do not send when it is false.
- **Tier B normally works** — the IBKR MCP server is registered in Claude Code
  and resolves in a headless session. But if its login has expired, say so in
  **Sources & provenance** and note that the narrative adjustment was made
  without the moat/competitor evidence. A thinner report must be *visibly*
  thinner; never let a missing section pass unremarked.
- Everything else — the template, the citation rules, the disclaimer — is
  unchanged. An unattended report is not a lesser report.

## The quant groups (already scored for you)

The groups are whatever `config.json`'s `quality.groups` currently lists —
today: valuation · growth · estimate_momentum · earnings_quality ·
financial_quality · analyst_sentiment · shareholder_returns. Each scores 0-1,
weighted. Read `quant.dimensions` in the bundle to see *where* the number came
from, and let your narrative explain or challenge it (a low valuation score
means expensive — is the premium justified?).

Two things the breakdown tells you that the aggregate does not:

- `metrics_used` vs `metrics_total` — a group scored on one parameter of four
  is a thin reading, and a report that leans on it should say so.
- A group at exactly **0.50** with `metrics_used: 0` is *not* a mediocre score;
  it is the neutral value used when nothing in that group had data. Never
  narrate it as "average". This is common for banks and insurers, where Yahoo
  publishes no operating income at all.

## Narrative rubric (your qualitative judgment → the adjustment)

- **Moat** (IBKR connections): pricing power, switching costs, network effects,
  scale, brand/IP; is it widening or eroding vs the ranked competitors?
- **Growth runway** (themes + estimates): TAM, reinvestment, secular theme
  membership; is the growth durable and self-funded?
- **Earnings / management** (10-Q/10-K MD&A + surprise history): guidance trajectory,
  execution consistency, candor.
- **Catalysts vs risks** (news/rumors + web): near-term catalysts against a
  concrete bear case (competition, regulation, litigation, cyclicality, leverage).

Move the adjustment **up** for a real qualitative edge the numbers miss; **down**
for red flags (eroding moat, guidance cut, accounting/litigation/regulatory risk,
crowded/expensive positioning). Stay within ±N; the quant score does the heavy lifting.

## Full-report template (`output/reports/<TICKER>_<scan_date>.md`)

```
# TICKER (Company) — deep-dive — <scan_date>

**Verdict: TIER · Conviction NN/100**  (quant NN + narrative ±M)
Trigger: <screen> (<full|partial> setup) · Quality screen: <passed ⭐ | failed: rule, rule>
*Analysis, not investment advice.*

## Snapshot
price · mkt cap · P/E (fwd) + 2y percentile · growth (this/next yr) ·
analyst mean target + upside% · next earnings date · IV percentile

## Thesis
2-4 sentences: the core bull case in plain terms.

## Business & moat
IBKR connections — products/revenue mix, ranked competitors + how they compete,
geographic exposure. Assess moat width/durability.

## Growth potential
Forward estimates + revision trend; secular themes/peers; runway.

## Financial trend
![financials](TICKER_<scan_date>_financials.png)

<paste `financials_table_md` here, unedited>

2-4 sentences reading the trend: what revenue, earnings, margin, cash generation
and leverage have actually done over 4 years and 4 quarters, and where annual and
quarterly disagree (a business rolling over shows it in the quarters first).

## Financial quality
Margins, ROA, balance sheet (net debt/EBITDA), FCF, buybacks; tie to the
financial_quality dimension score. State the **tier-2 quality screen** result and,
if it failed, name the rules from `Quality Missing` and say whether you consider
each one a genuine concern or an artifact of the rule (e.g. a bank with no
operating income can never pass).

## Earnings & estimate momentum
Surprise history, revision trend, next earnings date. **Management commentary**
(latest 10-Q/10-K MD&A): results drivers, margins, liquidity, guidance vs prior.

## Valuation
Multiples vs the stock's own 2y range and vs peers; is the premium/discount earned?

## Catalysts, declarations & rumors
Recent items (dated, sourced), each linked to the trigger; rumors labelled.

## Positioning & sentiment
IV percentile, analyst distribution, ownership/insider, account (held? size).

## Risks / bear case
The concrete ways this is wrong.

## Verdict rationale
How quant + narrative produced the conviction/tier; what would change it. If this
ticker was analyzed despite failing the tier-2 gate, say so and why it was still
worth the work.

## Sources & provenance
Data sources + dates (Yahoo, IBKR/Reflexivity, SEC EDGAR, web links). n/a where missing.
_This is research analysis, not investment advice._
```

## Rules

- **Never generate a chart, and never retype a figure.** The financial-trend PNG
  is rendered by `charts.plot_financials` before you start — embed
  `financials_chart` by filename and paste `financials_table_md` verbatim. Do not
  draw, plot, sketch, or hand-build a chart or table of these numbers; a
  model-made one is a bug, not a fallback. Your job on this data is
  *interpretation*: say what the trend means, not what the values are.
- **Cite everything non-obvious**: a Yahoo field, an IBKR connection's evidence, a
  news URL (with date), or a filing (MD&A / Risk Factors) quote. No unsourced claims.
- **Rumors**: prefix `RUMOR (unverified)`, give the source + date, never state as
  fact, and weight lightly in the adjustment.
- **n/a tolerance**: missing data renders `n/a` — never invent numbers, never hard-fail.
- **Not advice**: keep the disclaimer in every report and the Discord summary; the
  verdict is an analytical rating, not a recommendation to trade.
- **Reproducibility**: the quant score is the fixed spine; your narrative adjustment
  is bounded and justified, so runs are comparable.
