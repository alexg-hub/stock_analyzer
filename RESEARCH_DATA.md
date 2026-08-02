# Research data-availability matrix — Yahoo + IBKR

Go/no-go inventory for the investment-case layer, produced by probing both
sources on a deliberate spread: **AAPL, MSFT** (mega-cap tech), **JNJ**
(healthcare/pharma), **JPM** (bank — the n/a-tolerance stress case). This is
the collection foundation; synthesis/formatting is a later step.

Legend: ✅ reliably present · ⚠️ present but often `n/a` for some issuers
(sector-dependent, tolerated) · ❌ not available / gated.

## Tier A — Yahoo / yfinance (runs in the nightly Python pipeline)

Collected by `research_collect.collect_yahoo(ticker)` → nested dict. yfinance
1.5.1. Verified: no crashes on any name; the bank degrades to `n/a` cleanly.

| Group | Fields | AAPL | MSFT | JNJ | JPM | Notes |
|---|---|:--:|:--:|:--:|:--:|---|
| profile | company, sector, industry, employees, summary | ✅ | ✅ | ✅ | ✅ | |
| valuation | trailingPE, forwardPE, P/S, P/B, EV/Rev, EV, mktcap | ✅ | ✅ | ✅ | ⚠️ | JPM `EV/EBITDA` n/a (bank) |
| valuation | **pe_percentile_2y** (reconstructed TTM-EPS series) | ✅ 95 | ✅ 3 | ✅ 99 | ✅ 93 | needs ≥4 reported quarters + 2y close |
| valuation | price_percentile_2y | ✅ | ✅ | ✅ | ✅ | from the same 2y close the screens use |
| estimates | earnings_estimate, revenue_estimate (avg/low/high/#/growth) | ✅ | ✅ | ✅ | ✅ | forward consensus, per period 0q/+1q/0y/+1y |
| estimates | eps_trend, eps_revisions | ✅ | ✅ | ✅ | ✅ | the up/down-revision signal |
| estimates | growth_estimates | ✅ | ✅ | ✅ | ✅ | `LTG` (5y) stockTrend usually `null` across all |
| analyst | targets low/mean/high + upside %, recs, recent actions | ✅ | ✅ | ✅ | ✅ | dated upgrades/downgrades w/ firm + target |
| earnings | next date + days-to, last-8-qtr beat/miss & surprise % | ✅ | ✅ | ✅ | ✅ | "reports in N days" guard works (AAPL 6d, MSFT 5d) |
| quality | ROA, grossMargin, current, quick, D/E, netDebt/EBITDA | ✅ | ✅ | ⚠️ | ⚠️ | JNJ: ROA/current/quick n/a · JPM: current/quick/D-E/netDebt n/a, grossMargin `0.0` (bank artifact — treat as n/a in synthesis) |
| ownership | instit %, insider %, top holders, insider net 6m, 2y share Δ | ✅ | ✅ | ✅ | ✅ | share Δ = buyback proxy (AAPL −4.7%, JPM −6.6%) |
| news | title, publisher, published, link (≤8) | ✅ | ✅ | ✅ | ✅ | current, real catalysts (JNJ OTTAVA FDA clearance; MSFT Mistral deal) |

**Yahoo does not provide:** earnings-call transcripts; the qualitative
moat/competitor/product/geography graph (→ IBKR). `LTG` 5-yr growth is
structurally sparse.

## Tier B1 — IBKR over the TWS API (`ibkr.py`, usable in the nightly job)

Added 2026-08-01. `ib_async` against IB Gateway/TWS on localhost, wired into the
`quality` registry as the `ibkr.*` resolver. **Optional by construction** —
retail IBKR has no headless API (OAuth 1.0a is institutional-only), so the
gateway has to be logged in on this box; when it is not, every value resolves
to `n/a` and the run completes normally with one `IBKR skip` line.

| Layer | Call | Status | Notes |
|---|---|:--:|---|
| Ratios | `reqFundamentalData(ReportSnapshot)` | ✅ | Refinitiv ratio block, parsed by `parse_ratios`. Mapped: ROE, ROI, revenue/EPS growth, gross/operating/net margin, D/E, P/E, P/B, yield, payout, market cap, TTM revenue/EPS. Unmapped fields come back under their raw Refinitiv name, so a new parameter is config-only. |
| Statements | `reqFundamentalData(ReportsFinStatements)` | ⚠️ | Available but **not enabled** — Yahoo is already the source of truth for statements and a second one would need reconciling, not merging. |
| Estimates | `reqFundamentalData(RESC)` | ⚠️ | Same: analyst estimates stay Yahoo's job. |
| Market stats | `reqMktData` generic ticks 106, 165 | ⚠️ | 52w hi/lo, average volume, historical and **implied** volatility. Subscription-gated per field; an unsubscribed field arrives NaN → `n/a`. |
| IV **percentile** | — | ❌ | The MCP connector returned one; the TWS API does not. It was a Reflexivity computation, not an IBKR field. Deliberately absent rather than approximated — a percentile needs a stored history. |
| Moat / competitors / themes | — | ❌ | `get_company_connections`, `get_company_themes`, `search_investment_topics` are Reflexivity products on the claude.ai connector with **no public-API equivalent**. Still MCP-only (Tier B2). |
| Account context | — | 🚫 | **Not implemented, at all.** See `ibkr.FORBIDDEN_CALLS`; a test asserts none is called. Same rule as below. |

Sentinel to know about: Refinitiv reports "not reported" as **-99999**. Left
alone it reads as a real, catastrophically bad number in any score that touches
it; `_number` drops it.

## Tier B2 — IBKR qualitative graph (interactive MCP only — never in the nightly job)

Resolve conid first via `search_contracts` → exact-symbol + US-primary row
(`country_code=US`, `STK` section). Resolved cleanly: AAPL 265598, MSFT 272093,
JNJ 8719, JPM 1520593.

| Layer | Tool | Status | Notes |
|---|---|:--:|---|
| Moat / competitive graph | `get_company_connections` (link_info) | ✅ | themes + products (w/ revenue mix) + ranked competitors (how they compete, + conids) + country/region exposure, each with a grounded evidence paragraph citing recent quarters |
| Themes & ranked peers | `get_company_themes` | ✅ | JNJ→Pharma/MedDevices/Oncology/Immunology; JPM→Commercial/Investment Banking/Trading/Capital Markets; peers ranked w/ evidence |
| Theme sizing / discovery | `search_investment_topics` → `get_theme_details` | ✅ | who else is in a trend, ranked by centrality (+ optional ETFs) |
| Market stats & IV | `get_price_snapshot` | ✅ | working: 52w hi/lo range, historical_vol (annualized), IV percentile (13/26/52w), avg_90d_usd_volume, dividend_yield, change, volume |
| Market stats (gated) | `get_price_snapshot` | ❌ | `cumulative_perf_*`, `year_to_date_change`, `prior_close` came back **empty on every name** — market-data-subscription gated; do not depend |
| Options / sentiment | `get_option_parameters` → `get_option_data` → snapshot IV/OI | ✅ | chain + IV/open-interest via the same snapshot path |
| Account context | `get_account_summary` / `get_account_positions` / `get_account_balances` / `get_account_trades` / `get_pa_*` | 🚫 | **Out of scope — do not call.** They work, and they return the user's live book (net-liq, buying power, margin, positions), but the user asked for the real portfolio to stay out of the analysis: a deep-dive grades the *security* and tier 4 grades the signal against a fixed-notional virtual ledger, so what is already held changes neither. `SKILL.md` step 2 states the same prohibition. |

**IBKR does not provide:** financial statements, analyst EPS/revenue estimates,
earnings dates/surprise, or a news feed (→ all Yahoo's job).

## Source-of-truth split (settled)

- Price history / 52-week range → **Yahoo** for the screens (nightly); IBKR cross-check only.
- Statements / estimates / earnings / news → **Yahoo only**.
- Valuation multiples → **Yahoo** (IBKR's are an independent corroboration, not a replacement).
- Moat / competitors / products / geography / themes/peers → **IBKR MCP only**, and only in the optional narrative pass.
- Implied volatility → **IBKR TWS API** (raw); the *percentile* is unavailable from either.

Where a metric exists on both sides, Yahoo stays the source of truth and the
IBKR parameter is a separate registry entry (`ibkr_return_on_equity`, not a
second writer of `roe`). Two sources reconciled silently into one number is a
number nobody can check; two parameters that disagree is information.

## Verdict

Both sources are exhausted and their reliable surfaces are mapped. Nothing
blocks the next step. Yahoo alone covers the quantitative investment case
end-to-end (valuation-in-context, forward estimates + revision trend, analyst,
earnings cadence, balance sheet, ownership/buybacks, news). IBKR adds the
qualitative moat/competitive/thematic graph plus positioning (IV) and account
context — interactive deep-dive only. Only genuinely missing without an external
provider: **earnings-call transcripts**.
