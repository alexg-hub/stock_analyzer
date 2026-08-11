---
name: enrich
description: Research one or more tickers the analyzer has already graded — the part arithmetic cannot reach (competitive position, capital allocation, un-filed regulatory and product-cycle risk, whether a tripped rule is a sector artifact, what filings and the tape are saying) — using the deterministic MCP bundles, IBKR's connection graph, SEC filings and live web/social search, then record a graded, categorical judgment with `enrichment_record` and archive the prose. Use when the user asks to enrich, research, deep-dive or build a qualitative case for a scanner signal or a named ticker.
---

# Enrichment

The analyzer is deterministic and finished before you start. Tiers 1-4 have
already computed the screen, the ⭐ quality verdict, the 🚫 veto, the 0-100 quant
score, the tier, both risk/reward axes and the ledger position. **You are not
grading an ungraded ticker, and there is nothing here you can recompute.**

What you add is the territory `DETERMINISTIC_GAPS.md` section D reserves:
competitive position and whether the moat the metrics imply is durable;
management's capital-allocation record against what they said; regulatory, legal
and product-cycle exposure that has not hit the statements; whether a tripped
rule is a real problem or an artifact of the sector; and what would have to be
true for the deterministic read to be wrong.

Your judgment is **recorded as an attribute and graded by tier 4** — never folded
into a score. There is no adjustment field, and `enrichment.validate` rejects
`conviction`, `tier`, `score` and `narrative_adj` by name. That is deliberate: a
judgment on the record is a hypothesis tier 4 can measure against forward
returns, while a judgment inside the score is a number nobody can check. The
predecessor of this skill moved nine convictions by at most 5 points, changed no
tier, and made the one score in the system unverifiable — see `AI_ROLE.md`.

## Which surface

MCP tools from the `stock_analyzer` server, plus the IBKR connector, plus
`WebSearch`/`WebFetch`. There is no CLI path and no headless mode: this skill
runs in a session, which is what lets it use the tools it needs without an
enumerated allow-list that fails silently when it drifts.

Read reports, logs and CSVs under `output/` with `Read`/`Glob` — there are
deliberately no file-reading tools.

## Procedure — per ticker

1. **The deterministic read.** `risk_research(TICKER)` — every rule beside the
   threshold it was compared against, split tripped / clean / unknown; both axis
   coordinates; the moat and risk metrics; the sector peer group; and a prompt
   naming the five questions arithmetic cannot answer. Start here. It runs no
   model and recomputes nothing.
2. **The company bundle.** `deepdive_context(TICKER)` — the trigger row, the
   quant score and its per-group breakdown, Yahoo fundamentals and 4y/4q
   financial history, SEC 10-K/10-Q sections, a rendered financials PNG and a
   facts JSON already on disk. Two fields in it are easy to miss and are free:
   `yahoo["news"]` (8 dated headlines with links) and
   `yahoo["analyst"]["recent_actions"]` (the last 6 upgrades/downgrades).
3. **Peers.** `universe_quadrant(sector=…)` for who else sits where on the plane.
   A metric that looks alarming in absolute terms is often structural for the
   sector — that is the whole reason `peers.py` exists, and it is the most common
   real finding in step 5.
4. **IBKR — the moat graph.** `search_contracts(TICKER)` → the exact-symbol US
   primary row → `underlying_contract_id`. Then `get_company_connections(conid,
   include=["link_info"])` for products/revenue mix, ranked competitors and *how*
   they compete, and geography, each with an evidence paragraph;
   `get_company_themes(conid)` and `get_theme_details` for secular exposure and
   ranked peers; `get_option_data` for the implied move if a catalyst is near.
   This graph is a Reflexivity product with **no public-API equivalent** — it is
   the one input Python cannot ever replace, and it is what `moat_view` is for.
5. **Filings.** The MD&A, Risk Factors and Item 1 sections in the bundle. Has the
   tone shifted against last year? Is a disclosed risk factor boilerplate or
   newly serious? Does a filing corroborate a tripped rule or explain it away?
6. **Web and social.** `WebSearch`/`WebFetch` for the last ~30-60 days:
   earnings and guidance, M&A, product news, litigation and regulatory action,
   insider and buyback activity. For the social read, search Reddit / StockTwits
   / X through the same tools — there is no platform API here and no credential
   to use one. Treat social as weak evidence about *attention*, not about facts.
7. **Write the report** to `output/enrichment/<TICKER>_<scan_date>.md` (template
   below), then **record the judgment** with `enrichment_record`.

## Recording

`enrichment_record(scan_date, ticker, stance, …)`. Controlled vocabularies,
because tier 4 grades a group split and free text cannot be split:

| Field | Values |
|---|---|
| `stance` | `bull` · `neutral` · `bear` |
| `moat_view` | `widening` · `stable` · `eroding` · `unclear` |
| `social_sentiment` | `positive` · `mixed` · `negative` · `thin` |

Plus `concerns`, `catalysts`, `sources` (a URL per factual claim), and
`rule_disputes` — the veto or gate keys you judge to be sector artifacts rather
than real problems. That last one is directly testable: if the disputes cluster
on the same metric across a sector, the anchor is wrong and `peers.py` should be
handling it.

An unknown value is **rejected** and nothing is written; fix it and call again.
Re-recording the same `(scan_date, ticker)` replaces that row.

## Report template (`output/enrichment/<TICKER>_<scan_date>.md`)

```
# TICKER (Company) — enrichment — <scan_date>

Deterministic read: TIER · quant NN/100 · reward NN / risk NN (<quadrant>)
Trigger: <screen> (<full|partial>) · Quality: <passed ⭐ | failed: rule, rule>
Exclusion: <clean | vetoed: reason, reason>
*Research analysis, not investment advice. The verdict above is the analyzer's
and is not changed by anything below.*

## Read
2-4 sentences: what the numbers say, in plain terms, before you add anything.

## Business & moat
IBKR connections — products/revenue mix, ranked competitors and how they compete,
geographic exposure. Is the moat widening, stable or eroding, and on what evidence?

## Growth and catalysts
Forward estimates and revision trend, secular theme membership, dated catalysts.

## Filings
What MD&A and Risk Factors say that the ratios do not. Tone shift vs last year.

## The tripped rules
For each: is it a real problem, or an artifact of the sector or an accounting
convention? Name the ones you dispute and why — these become `rule_disputes`.
Name the **unknown** ones and say what the missing value would have told you.

## News and attention
Dated, sourced items from the last 30-60 days. Social read last and labelled.

## Bear case
The concrete ways the deterministic read is wrong. What would have to be true.

## Sources
Every source with a date. n/a where missing.
```

## Rules

- **Never recompute what Python computed, and never produce a new ratio.** The
  bundle's numbers are checkable; yours would not be. Your job on this data is
  interpretation — what the trend means, not what the values are.
- **Never generate a chart or retype a figure.** The financials PNG is rendered
  before you start; embed it by filename and paste `financials_table_md`
  verbatim. A model-made chart is a bug, not a fallback.
- **Record only through `enrichment_record`.** Never write `signals.csv`,
  `on_demand_scans_results.csv`, `positions.csv` or any `_facts.json` — those are
  the analyzer's, and you do not write, update or even edit them.
- **You cannot change a tier or a conviction, and must not describe one as
  changed.** You may argue in the report that a rule is miscalibrated — that is
  useful, a human reads it and can retune the threshold. Arguing with it in a
  recorded field is not possible; there is no such field.
- **Never call `get_account_*` or `get_pa_*`.** The user's real IBKR book is out
  of scope: this grades the *security*, and tier 4 grades the signal against a
  fixed-notional virtual ledger, so what is already held changes neither. Do not
  mention holdings, position size or concentration anywhere in a report.
- **Cite everything non-obvious** — a Yahoo field, an IBKR connection's evidence,
  a news URL with its date, a filing quote. No unsourced claims.
- **Rumors and social**: prefix `RUMOR (unverified)`, give source and date, never
  state as fact. Social sentiment is evidence about attention, not about the
  business.
- **A thin read must be visibly thin.** `social_sentiment: thin`, `stance:
  neutral`, an empty `concerns` list and "IBKR did not resolve this symbol" are
  all legitimate, recordable answers. Never round a thin read up to a view, and
  say in the report which sections came back empty.
- **Not advice**: keep the disclaimer in every report.
