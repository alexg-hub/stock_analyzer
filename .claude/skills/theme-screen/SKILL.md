---
name: theme-screen
description: Find investment candidates from real-world events rather than from the tape — a data-centre announcement, an outbreak, an AI capex cycle, a defence award — by tracing the beneficiary chain from a dated news event or 8-K through to the companies that actually sell into it, using the deterministic `theme_research` bundle, IBKR's theme and connection graph, EDGAR full-text search and live web search, then recording the picks with `theme_record` so the registry grades them and tier 4 buys them. Use when the user asks for thematic ideas, who benefits from an event or announcement, infrastructure/energy/AI/biopharma/defence plays, or wants to run the theme screen.
---

# The thematic screen

Every other screen here asks *which of the 903 constituents did something on the
tape?* This one asks *which companies benefit from something that happened in the
world?* — the question no rolling window can reach, because the beneficiaries
have done nothing on their own charts yet.

**You name candidates. Python grades them.** That split is the whole design and
it is enforced, not requested: `theme_record` rejects `conviction`, `tier`,
`score`, `Verdict`, `Reward`, `Risk` and `price_target` by name, and every number
that ends up on a recorded row — the ⭐ badge, the 🚫 veto, both plane axes — is
computed by the registry *after* your pick, from a fresh price download. Your
contribution is the causal chain, and it is measured: the picks land in
`signals.csv` under `config_key = "theme_screen"`, tier 4 buys them, and
`portfolio_sim analyze` split on `config_key` eventually says whether this screen
pays. That is the same bargain the `enrich` skill makes — a judgment on the
record is a hypothesis you can measure.

## Which surface

MCP tools from the `stock_analyzer` server, plus the IBKR connector, plus
`WebSearch`/`WebFetch`. There is no CLI path and no headless mode: this runs in a
session, which is what lets it use the tools it needs without an enumerated
allow-list that fails silently when it drifts.

Read anything under `output/` with `Read`/`Glob` — there are deliberately no
file-reading tools.

## What you can and cannot see

Tested 2026-08-14, and **state this honestly in the report rather than implying
a read you could not perform**:

| Reachable | Not reachable |
|---|---|
| Google News RSS (in the bundle, dated and sourced) | Reddit — blocked outright |
| EDGAR full-text search (in the bundle, with tickers) | StockTwits — 403 |
| IBKR theme + connection graph | X/Twitter — paid API only |
| `WebSearch` / `WebFetch` — news and trade press | Facebook — no public search |
| | Google Trends — no official API |

So this is a **news-, filings- and theme-graph-driven screen, not a social
sentiment screen.** `evidence_type: social` exists in the vocabulary but you will
almost never be able to use it truthfully.

## Procedure

1. **The bundle.** `theme_research()` — configured themes with their beneficiary
   chains, news events, recent 8-K filings mentioning the theme (with tickers
   EDGAR already resolved), what has already been recorded, and the controlled
   vocabularies. Runs no model. Narrow with `theme_research("ai_semi")`.
   - `events` are **already de-duplicated and ranked** by size, corroboration
     and recency, so start at the top. `sources_n` is how many distinct outlets
     carried it — that is your corroboration, and `min_sources` is asking about
     it. `magnitude` is the largest figure in the headline, in
     `magnitude_currency` and **not** FX-converted; an event with no
     `magnitude` is not small, it is one nobody put a number on.
   - `filings` are ordered **index constituents first**. An empty `index` means
     read it more sceptically — micro-caps mention a theme promotionally far
     more often than large filers mention one materially — not that it is wrong.
2. **Pick ONE event.** It must be concrete and dated, with a named counterparty
   and a location or a number. *"SK Hynix to invest $38 billion on new memory
   fabs"* is an event. *"AI demand remains strong"* is not. Check
   `already_recorded` first — re-recording the same chain adds nothing.
3. **Walk the chain.** Each theme carries a `chain` template — for `datacenter`:
   operator → engineering → equipment → power → materials. Take it one link at a
   time. **The operator is almost always already priced**; the tiers behind it are
   where the work pays.
4. **Find the names.** `search_investment_topics` → `get_theme_details` is the
   strongest source: companies ranked by relevance, each with a sourced evidence
   paragraph. Query it with **short singular nouns** — `grid`, `semiconductor`,
   `biotech`, `battery`. Plurals and multi-word phrases return nothing. Then
   `get_company_connections` for the supplier and competitor graph behind a name
   you already have.
5. **Resolve to a tradeable symbol.** `search_contracts`. The theme graph returns
   foreign listings — Prysmian on Milan, NKT on Copenhagen, POWERGRID on NSE —
   and `theme_record` will refuse them, correctly. Confirm a US listing.
6. **Corroborate.** `WebSearch`/`WebFetch`. An event with one source is a rumour;
   the recorder enforces a minimum of `min_sources`.
7. **Write the mechanism.** One falsifiable sentence per candidate: what
   specifically this company sells into this event. If you cannot write it without
   hedging, that is the finding — drop the name.
8. **Record.** `theme_record(theme, event, picks)`. The batch is **atomic**: one
   invalid pick writes nothing, because half a chain is a misleading cohort rather
   than a partial answer.
9. **Archive the prose** to `output/themes/<TICKER>_<scan_date>.md` using the
   template below, and pass its filename as `report`. The events you worked from
   are frozen automatically beside it as
   `events_<theme>_<scan_date>.json` — you do not need to copy them into the
   report, and the row records the filename.

## Recording

| Field | Values |
|---|---|
| `ticker` | required — a **US-listed** symbol |
| `mechanism` | required — one falsifiable sentence |
| `sources` | required — a URL per factual claim, at least `min_sources` |
| `chain_role` | operator · engineering · equipment · power · utility · transmission · materials · fuel · designer · foundry · developer · cdmo · diagnostics · supplies · prime · subsystem · services |
| `exposure` | pure_play · major · moderate · minor |
| `time_horizon` | announced · near_term · multi_year · speculative |
| `confidence` | high · medium · low |
| `evidence_type` | news · trade_press · filing · theme_graph · social · mixed |
| `risks` | what would make the thesis wrong |

`exposure` is the field that matters most and the one most often inflated. **A
mega-cap with a rounding-error segment is the classic thematic pick that looks
right and pays nothing** — if the theme touches under a tenth of revenue, that is
`minor`, and `minor` is a legitimate answer. So are `low` confidence and
`speculative`. A thin read must be visibly thin.

An unknown value is **rejected** and nothing is written; fix it and call again.

## Report template

```markdown
# <TICKER> — <theme> — <scan_date>

## The event
<what happened, when, who announced it, how big. Link every claim.>

## The chain
<operator → … → this company. One line per link.>

## Mechanism
<one falsifiable sentence: what this company sells into this event.>

## Exposure
<what share of revenue the theme can plausibly touch, and how you estimated it.>

## What would make this wrong
<the disconfirming evidence you would watch for.>

## Sources
<numbered list of URLs.>

## Not assessed
<what you could not reach — social sentiment and search-trend data are not
available from this surface.>

_Research analysis, not investment advice._
```

## Rules

- **Never state a price target, conviction, tier or score.** They are refused by
  name, and the refusal is the design rather than a limitation.
- **Never recompute** what the registry already computed. If you disagree with a
  veto or a badge, say so in the prose — that is a `rule_disputes` argument for
  the `enrich` skill, not something to encode here.
- **Confirm a US listing before recording.** The theme graph is full of foreign
  lines.
- **Cite a source per factual claim.** Where you cannot find evidence, say so
  rather than inferring. Prefix anything unconfirmed `RUMOR (unverified)`.
- **An event with one source is not an event.**
- **Say plainly that social platforms were not reachable.** Do not imply a
  sentiment read that did not happen.
- **Never `get_account_*` or `get_pa_*`.** The user's real book is out of scope
  for every surface in this project; a security is graded on its own merits.
- Keep the disclaimer on the report.
