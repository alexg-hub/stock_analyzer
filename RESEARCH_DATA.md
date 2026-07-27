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

## Tier B — IBKR (interactive MCP only — never in the nightly job)

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

- Price history / 52-week range → **Yahoo** for the screens (nightly); IBKR snapshot cross-check only.
- Statements / estimates / earnings / news → **Yahoo only**.
- Moat / competitors / products / geography / themes/peers → **IBKR only**.
- Valuation multiples → **Yahoo** (IBKR snapshot doesn't return them).

## Verdict

Both sources are exhausted and their reliable surfaces are mapped. Nothing
blocks the next step. Yahoo alone covers the quantitative investment case
end-to-end (valuation-in-context, forward estimates + revision trend, analyst,
earnings cadence, balance sheet, ownership/buybacks, news). IBKR adds the
qualitative moat/competitive/thematic graph plus positioning (IV) and account
context — interactive deep-dive only. Only genuinely missing without an external
provider: **earnings-call transcripts**.
