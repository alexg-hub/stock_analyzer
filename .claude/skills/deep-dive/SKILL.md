---
name: deep-dive
description: Produce a graded investment-case deep-dive for a scanner hit/near-miss — a config-driven quant score anchored by Claude's narrative (moat, growth, earnings/management, news & rumors) → a tier + 0-100 conviction verdict, the full report archived to Google Drive and a combined verdict summary posted to Discord. Use when the user asks to deep-dive, analyze, or build an investment case for one or more tickers from the nightly scan.
---

# Deep-dive stock analysis

This skill is the **synthesis layer** of the stock_analyzer pipeline. The nightly
scan (`run_scanners.py`) writes `latest_hits.json`; this skill turns a hit/near
ticker into a full investment case. Synthesis is **your reasoning**, anchored by
a deterministic quant score. Everything mechanical is in `research_report.py` and
`sec.py`; the judgment is yours.

**Framing (non-negotiable):** this is research analysis, **not investment advice**.
The "verdict" is an analytical rating, never a buy/sell instruction. Put the
disclaimer line (below) in every report and the Discord summary.

## Prerequisites

- `latest_hits.json` exists (run `python run_scanners.py` first, or work ad-hoc).
- IBKR MCP authenticated in this session (Tier B). If not, run `/mcp`.
- SEC filings need only `research.sec.user_agent` (email-bearing, already in config) — no API key.
- Confirm with the user before any **live** Discord send.

## Procedure — per ticker

1. **Deterministic bundle.** `python research_report.py context TICKER` → JSON:
   `trigger` (which screen fired + row), `yahoo` (Tier-A), `quant`
   (`score` 0-100 + per-dimension breakdown + metrics), `filings` (SEC 10-Q/10-K
   MD&A / Risk Factors / Business sections + curated XBRL, or null).
   The quant score is your **anchor** — do not recompute it, reason on top of it.
2. **Tier B — IBKR (MCP).** `search_contracts(TICKER)` → the row with exact symbol
   + US primary listing (`country_code=US`, `STK`) → `underlying_contract_id`. Then:
   `get_company_connections(conid, include=["link_info"])` (moat, competitors,
   products/revenue mix, geography — with evidence), `get_company_themes(conid)`
   (ranked peers / secular themes), `get_price_snapshot(conid, [...])` (52w range,
   historical_vol, implied_volatility_percentile, avg_90d_usd_volume, dividend_yield),
   and — if relevant — `get_account_positions`/`get_account_summary` to flag
   "already held" / size vs portfolio.
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
5. **Grade the qualitative dimensions** (your judgment, not in the quant score):
   moat width/durability, growth runway, earnings/management quality, catalysts vs
   risks. Decide a **narrative adjustment** in `[-N, +N]` where
   `N = config research.synthesis.narrative_adj_max`, with a one-line justification
   for each material ± move.
6. **Verdict.** `conviction = clamp(round(quant.score + narrative_adj), 0, 100)`;
   `tier = research_report.tier_for(conviction, cfg)` (config bands STRONG/WATCH/PASS).
   You may override the tier **only** with an explicit written justification.
7. **Write the full report** (template below) and archive it: resolve the folder
   with `research_report.report_dir(load_config())` and use the **Write tool** to
   create `<TICKER>_<scan_date>.md` there (Write handles the multiline markdown
   cleanly; `write_report_to_drive` exists too but Write is simpler for prose).
8. Record `{ticker, company, tier, conviction, thesis (one line), screen}` for the batch.

## After all tickers

Deliver one **combined** Discord message:
`research_report.post_summary(verdicts, cfg, send=False)` to dry-run;
`send=True` **only after the user confirms** (it posts to the live channel).

## The quant dimensions (already scored for you)

valuation · growth · estimate_momentum · earnings_quality · financial_quality ·
analyst_sentiment — each 0-1, weighted (see `config.research.synthesis`). Read the
breakdown to see *where* the number comes from, and let your narrative explain or
challenge it (e.g. a low valuation score = expensive; is the premium justified?).

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

## Full-report template (Drive markdown)

```
# TICKER (Company) — deep-dive — <scan_date>

**Verdict: TIER · Conviction NN/100**  (quant NN + narrative ±M)
Trigger: <screen> (<hit|near-miss>)  ·  *Analysis, not investment advice.*

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

## Financial quality
Margins, ROA, balance sheet (net debt/EBITDA), FCF, buybacks; tie to the
financial_quality dimension score.

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
How quant + narrative produced the conviction/tier; what would change it.

## Sources & provenance
Data sources + dates (Yahoo, IBKR/Reflexivity, SEC EDGAR, web links). n/a where missing.
_This is research analysis, not investment advice._
```

## Rules

- **Cite everything non-obvious**: a Yahoo field, an IBKR connection's evidence, a
  news URL (with date), or a filing (MD&A / Risk Factors) quote. No unsourced claims.
- **Rumors**: prefix `RUMOR (unverified)`, give the source + date, never state as
  fact, and weight lightly in the adjustment.
- **n/a tolerance**: missing data renders `n/a` — never invent numbers, never hard-fail.
- **Not advice**: keep the disclaimer in every report and the Discord summary; the
  verdict is an analytical rating, not a recommendation to trade.
- **Reproducibility**: the quant score is the fixed spine; your narrative adjustment
  is bounded and justified, so runs are comparable.
