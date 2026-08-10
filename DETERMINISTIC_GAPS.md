# Deterministic gaps: metrics worth adding, and where to get them

Companion to `RESEARCH_DATA.md`, which maps what each source *can* supply. This
maps what the registry is **not yet computing**, ordered by what it costs to get
rather than by how useful it is.

The governing rule: **anything derivable is computed in Python.** A number a model
wrote is a number you cannot check, which is why tier 3's thesis, tier 4's
`conclusion` and the whole verdict are already deterministic. The AI's territory
is section D, and nothing else.

Status legend — ⬜ not started · 🟨 data in hand, no parameter · ✅ done

---

## A. Free right now — the data is already being fetched

Nothing new to source. These are the cheapest wins in the file.

### A1. Statement rows already parsed but never turned into a metric

`derived.ROWS` resolves these and uses them only *inside* composites: `sga`,
`tax`, `receivables`, `ppe`, `depreciation`, `current_assets`,
`current_liabilities`, `long_term_debt`, `retained`, `capex`, `invested_capital`.
No extra I/O — `statement_frames` already has them.

| Metric | Why | Status |
|---|---|---|
| **Piotroski F-score** (9 binary tests) | The best-validated deterministic quality composite there is, and every input is already in hand. Highest value-per-line in this document. | ⬜ |
| Days sales outstanding + trend | Receivables growing faster than sales is the classic revenue-quality warning; `receivables` currently only feeds Beneish's DSRI. | ⬜ |
| Asset turnover + trend | Revenue ÷ assets. Deteriorating turnover is capital being destroyed quietly. | ⬜ |
| SG&A intensity + trend | `sga` only feeds Beneish's SGAI today. | ⬜ |
| Effective tax rate + anomaly | A rate that swings without a law change is an earnings-quality flag. | ⬜ |
| D&A as % of revenue; capex ÷ D&A | Capex below depreciation for years is a business being harvested, not run. | ⬜ |
| Debt ÷ FCF (years to repay) | More intuitive than net-debt/EBITDA and immune to EBITDA games. | ⬜ |
| Current / quick ratio as a multi-year **trend** | Today these are Yahoo `info` snapshots. A trend distinguishes "runs lean" from "deteriorating" — exactly the confusion that made the current anchors punish MSFT and KO. | ⬜ |
| Retained-earnings trend / accumulated deficit | Only feeds Altman's X2 today. | ⬜ |
| Invested-capital growth | Pairs with `incremental_roic` to show whether growth is being bought. | ⬜ |

Add three rows to `ROWS` and these open up too:

| Metric | Needs row | Status |
|---|---|---|
| Inventory turnover / days inventory | `Inventory` | ⬜ |
| Cash conversion cycle (DSO + DIO − DPO) | `Accounts Payable` | ⬜ |
| Goodwill ÷ assets (impairment risk) | `Goodwill` | ⬜ |

### A2. Collected by `research_collect` and then discarded

`_valuation` and `_ownership` already fetch these on every deep pass;
`quality.deep_metrics` simply never maps them. Roughly eight lines plus config.

| Metric | Source field | Why | Status |
|---|---|---|---|
| **FCF yield** | `fcf` ÷ `marketCap` | Both already present. The most robust valuation metric in this document, and entirely absent from a registry whose valuation group leans on `trailingPE`. | 🟨 |
| **EV/EBITDA** | `evToEbitda` | Comparable across capital structures, unlike PE. | 🟨 |
| EV/Revenue | `evToRevenue` | Works on unprofitable names, where PE is undefined. | 🟨 |
| P/B, P/S | `priceToBook`, `priceToSales` | P/B is the one valuation metric that means something for financials. | 🟨 |
| Forward PE | `forwardPE` | Collected, unused. | 🟨 |
| Shareholder yield | `dividendYield` + `shares_change_2y_pct` | Both present; the pair is what actually returns cash. | 🟨 |
| Institutional / insider ownership | `institutions_pct`, `insiders_pct` | Collected, unused. | 🟨 |

### A3. Price-based risk — ✅ done

`price_risk.py`, added 2026-08-10. Volatility (60d/252d), max drawdown (1y/3y),
downside deviation, Ulcer index, beta and **downside** beta, SPY correlation,
return skew, distance from the 52-week high, 12-1 momentum.

Free because the panel is already downloaded in bulk. Worth stating how large
this gap was: the risk axis had **no** volatility, drawdown or beta at all —
the three most directly measurable risks a listed company has. Adding them moved
INTC from risk 24 to 47 and out of the "dull" quadrant into "avoid".

`momentum_12_1` is computed but **not yet consumed** — whichever parameter takes
it must sit on the *reward* axis, not risk. 🟨

---

## B. Free, but a new source has to be wired

### B1. GICS sector — ✅ captured, ✅ used for scoring

`sp500_constituents` now keeps the Sector and Sub-Industry columns that
`get_sp500_tickers` was parsing and throwing away.

Done 2026-08-10: `peers.py` scores any parameter carrying
`sector_relative: true` against its sector's distribution instead of its fixed
anchors, and confirms a `sector_relative` veto only when the value is *also* in
the worst decile of its peer group. Utilities went from 26 of 31 excluded to 5,
sector mean risk 38.7 → 28.7, and Financials' share of the buy quadrant from 42%
to 23% against a 15% index weight.

### B1b. `aggregate: worst_k` on the risk axis — ✅ done

Enabled 2026-08-10 at `worst_k: 3` on both risk groups. Sector-relative scoring is
what unblocked it: a percentile-ranked metric cannot have a miscalibrated absolute
anchor, because it re-centres at 0.5 by construction.

Separation of vetoed from clean names on the risk axis improved from an effect size
of **+0.91 to +1.09**, and the aggregation now automatically ignores the
no-variance metrics that averaging had been letting dilute every reading
(`negative_equity` is a clean 1.00 for all 503 constituents).

Getting there required fixing three anchors, and the *diagnostic* is the reusable
part: **the median normalized reading of every metric on the axis.** One centring
below ~0.25 is marking the whole index bad, and `worst_k` will then hand it the
axis for nearly every company. That test found a genuine implementation defect in
`downside_deviation` (loss-count divisor instead of the total, so it duplicated
volatility) and two absolute anchors that were describing a different era of
balance sheet (`current_ratio` median 1.21 against a `good: 2.5` bar). Full detail
in CLAUDE.md.

Two consequences to carry forward:

- **`worst_k` moves the level of the axis, so quadrant thresholds move with it** —
  `risk_max` 30 → 56, re-anchored at the same selectivity rather than picked by
  eye. Third time thresholds have had to be recalibrated after a scoring change;
  assume it every time.
- **The `fast`/`deep` slice asymmetry is now live and unmeasured.** `risk` holds 10
  scored metrics at `fast` and 18 at `deep`, so worst-3-of-18 is harsher and
  tier 3 reads riskier than the plane for the same company. Safe today only
  because the 8 deep additions are mostly binary `sec_flags` scoring a clean 1.00.
  A deep universe pass would let this be measured rather than argued — see B2.

### B2. SEC XBRL `frames` — the strategic one

```
https://data.sec.gov/api/xbrl/frames/us-gaap/<Concept>/USD/CY2026Q2I.json
```

Returns **one fact for every filer** for one concept and one period, in a single
request. That inverts the cost model: universe fundamentals become ~1 request per
concept instead of 500 requests per concept — against the ~1.5s/ticker the Yahoo
path costs today.

The larger prize is **point-in-time**. Every fact carries `accn`, `filed`, `fy`
and `fp`, so an as-of view is reconstructable. Yahoo serves *current, restated*
figures with no history, which means a fundamentals backtest on Yahoo data has
look-ahead bias — sometimes using numbers that were restated *because* the
company blew up, which would make the distress veto look clairvoyant. This is the
only path to a legitimate historical test of the exclusion thesis.

Plumbing already exists: `sec.py` has the CIK map, a compliant User-Agent and a
`companyfacts` fetcher, and `research.sec.xbrl_concepts` is config-driven (11
concepts today). No API key. Check SEC's current fair-access rate limit before
running a bulk pass — the published number was not verified here. ⬜

### B3. yfinance surfaces present in 1.5.1 and unused

Verified live on this box:

| Surface | What it gives | Status |
|---|---|---|
| `ttm_income_stmt` | **TTM** EBITDA, EBIT, Diluted EPS, tax rate — as of 2026-06-30 for MSFT. The repo uses **annual only**, so every ratio can be up to 12 months stale. | ⬜ |
| `quarterly_balance_sheet` | 6 quarters of Net Debt, Total Debt, Invested Capital, Working Capital, Ordinary Shares Number. Quarter-over-quarter deterioration is currently invisible. | ⬜ |
| `insider_transactions`, `insider_purchases` | Actual Form 4 activity, vs the single aggregate now used. | ⬜ |
| `institutional_holders`, `major_holders` | Ownership concentration and change. | ⬜ |
| `upgrades_downgrades` | Already partly used via `recent_actions`. | 🟨 |
| `shares_full` | True share-count history — better dilution evidence than the 2-year proxy. | ⬜ |
| `earnings_dates`, `growth_estimates` | Cadence and forward growth. | 🟨 |

Note: `valuation_measures` is **not** available as a property in yfinance 1.5.1
(it raises `AttributeError`) — don't plan around it.

### B4. Other free sources

| Source | Gives | Status |
|---|---|---|
| **FINRA short interest** — `api.finra.org/data/group/otcMarket/name/EquityShortInterest`, plus bi-monthly files back to 2014 | Real short interest and days-to-cover for exchange-listed names, instead of Yahoo's stale snapshot. Free for non-commercial use. | ⬜ |
| **FRED** (free key) | HY credit spread, yield-curve slope, financial conditions. A regime overlay — risk thresholds arguably should move with the credit cycle rather than sit fixed. | ⬜ |
| SEC **Form 4** / **13F** direct | Insider and institutional detail, if Yahoo's aggregates prove too coarse. | ⬜ |

---

## C. Semi-deterministic — text and XBRL parsing, still not AI judgment

`sec.fetch_filing_sections` already extracts 10-K sections, so these are regex
and tag lookups.

| Metric | Note | Status |
|---|---|---|
| **`AuditorName`** | A required XBRL tag since 2021, so auditor identity and changes are exact rather than inferred. Strictly better than the current `auditor_change` proxy. | ⬜ |
| **"material weakness"** in ICFR | Unambiguous distress language and a natural sibling to the going-concern scan. **Needs the same negation guard** — the phrase appears in the negative constantly. | ⬜ |
| **Share-based comp ÷ revenue** | XBRL `ShareBasedCompensation`. The single largest quality-of-earnings adjustment for tech. | ⬜ |
| Customer concentration | "no customer accounted for more than 10%". | ⬜ |
| Debt maturity wall | XBRL `LongTermDebtMaturitiesRepaymentsOfPrincipal…`. A refinancing cliff is a real, datable risk. | ⬜ |
| Pension funded status | XBRL `DefinedBenefitPlanFundedStatus…`. | ⬜ |
| Operating lease liabilities | XBRL `OperatingLeaseLiability` — off-balance-sheet leverage until 2019, still easy to miss. | ⬜ |

---

## D. Genuinely needs AI — and belongs *only* in `risk_research`

Everything above is arithmetic. These are not, and no amount of ratio-building
reaches them:

- Competitive position: who is taking share from whom, and whether the moat the
  metrics imply is actually durable.
- Management's capital-allocation record against what they said they would do.
- Regulatory, legal and product-cycle exposure that has not hit the statements.
- Whether a tripped rule is a real problem or an artifact of the sector or an
  accounting convention.
- Whether MD&A tone shifted against last year, and whether a disclosed risk
  factor is boilerplate or newly serious.

`mcp_tools/universe.risk_research` is the only entry point, it runs no model
itself, and its prompt names exactly these questions and forbids restating the
computed numbers as findings.

---

## E. No free source

| Wanted | Situation |
|---|---|
| Credit ratings (S&P/Moody's) | Paid. Proxy with Altman Z + interest coverage, both already present. |
| Short borrow fee / utilization | Paid (Ortex, S3). The genuinely predictive short-side data. |
| **Earnings-call transcripts** | `RESEARCH_DATA.md` already identifies this as the one gap with no source at all. |
| Supply-chain / customer graphs | Was Reflexivity on the old claude.ai IBKR connector; no public-API equivalent. |

---

## Suggested order

1. **A2** — eight lines of `deep_metrics` plus config buys FCF yield and
   EV/EBITDA. Best ratio of value to effort in the document.
2. **A1 Piotroski** — one function, no I/O, a well-validated composite.
3. ~~**B1 sector-relative percentiles**~~ — done, and it unblocked ~~B1b~~
   `worst_k`, also done.
4. **B3 TTM + quarterly** — removes up to 12 months of staleness from every ratio.
5. **B2 XBRL frames** — the only route to a point-in-time backtest, and therefore
   the only way to test the exclusion thesis on history rather than forward.

One lesson worth applying to all of the above: **every metric added to an axis
needs its median normalized reading checked against the index before it is
trusted.** Three of the anchors in this registry were wrong in the same direction
— strict enough to mark the whole index bad — and under a plain mean that is
invisible, because it just shifts the level uniformly. Anchors written from
textbook values rather than from the observed distribution are the single most
common defect found here so far.
