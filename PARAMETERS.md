# PARAMETERS.md — the tunable surface

Every parameter in the system: where its value comes from, who computes it,
which stage it belongs to, and whether it is actually working.

Built as the baseline for the validation / bug-fixing / tuning phase. Generated
from `config.json` and measured against the **full 503-ticker universe pass of
2026-08-11** (`output/universe/fundamentals_cache.pkl`, 0 errored tickers).
Regenerate the measurements with the commands in [Reproducing](#reproducing).

**Totals:** 77 quality parameters (43 fast / 34 deep · 73 enabled / 4 off ·
17 veto · 30 sector-relative) · 50 screen and exit thresholds · 139
operational settings.

---

## 1. Deterministic vs AI

**All 77 quality parameters are deterministic Python.** So are all 50 screen
thresholds, the tier-3 score, the tier-3 tier, and every sentence in the tier-4
findings report. Nothing a model writes can move a gate, a veto, an axis
coordinate or a recorded return.

Since 2026-08-11 the model contributes **nothing to the analyzer at all** — no
code path in it can start one, and `tests/test_no_model.py` fails if that
changes. It used to contribute four fields in an optional narrative pass, of
which one (`narrative_adj`, bounded at ±15) was numeric; over its whole life
that number moved nine convictions by at most 5 points and changed no tier, so
it was removed rather than kept as a clamped exception (`AI_ROLE.md`).

What a model contributes now is a **separate, graded record**. The `enrich`
skill (`.claude/skills/enrich/SKILL.md`, session-only) writes:

| Field | Kind | Bound |
|---|---|---|
| `stance` | categorical | `bull` / `neutral` / `bear` — rejected if anything else |
| `moat_view` | categorical | `widening` / `stable` / `eroding` / `unclear` |
| `social_sentiment` | categorical | `positive` / `mixed` / `negative` / `thin` |
| `concerns`, `catalysts`, `rule_disputes`, `sources` | lists | free text, JSON-encoded |
| *(no numeric field)* | — | `enrichment.validate` rejects `conviction`, `tier`, `score` and `narrative_adj` **by name** |

Everything numeric on the Discord card is read back from
`<TICKER>_<date>_facts.json`, which Python wrote. The thesis is
`research_report.deterministic_thesis` — a rendering of the group breakdown, not
a generated sentence. The agent may argue a rule is miscalibrated *in its
report*, where a human reads it and can retune the threshold, and
`rule_disputes` makes that argument countable across a sector; it cannot
relabel the row.

`research_report.deterministic_verdict` runs **inside the nightly scan**, so the
verdict exists before anything is posted.

---

## 2. Where each stage runs

| Stage | Entry point | Parameters graded | Network |
|---|---|---|---|
| Tier 1 — screens | `run_scanners.py` → `SCANNERS` registry | 50 screen thresholds | one bulk `yf.download` |
| Tier 2 — `fast` | `quality.annotate` in `run_scanners.main()` | **43** fast, over tier-1 hits | `info` + statements + cached closes |
| Tier 3 — all stages | `research_report.deterministic_verdict` (`stage=None`) | **77** — fast *and* deep | + `collect_yahoo`, EDGAR |
| Enrichment | `enrich` skill, in a session | none — a graded categorical record | web, IBKR MCP |
| Combined dossier | `combined_report.py` / MCP `combined_report` | none — renders recorded files | **none** |
| The plane | `universe_scan.py` | **43** fast, over all 503 | ~13 min, 4 calls/ticker |
| Tier 4 — ledger | `portfolio_sim/` | grades recorded attributes | held tickers + SPY |

`sources_needed(cfg, stage)` derives the fetch set from the registry, so the
stage split is entirely config-driven — nothing in Python hardcodes which
resolver is cheap. `fast` needs `{yahoo_info, yahoo_stmt, distress, moat,
price_risk}`; `deep` adds `{yahoo_deep, sec_flags}`.

Two consequences worth remembering:

- The plane and tier 2 read the **same 43 fast parameters**, which is what makes
  a signal card's coordinates comparable to the whole-index chart.
- `worst_k` is per group and therefore per *stage*: the `risk` group carries 13
  scored metrics at fast and 31 at deep, so a deep-graded company reads riskier
  than the same company on the plane.

---

## 3. Resolvers

| Prefix | Produces | Implementation | Cost |
|---|---|---|---|
| `yahoo_info` | raw `yf.Ticker.info` (~150 fields, **6 consumed**) | inline in `quality.collect` | 1 call, shared |
| `yahoo_stmt` | 5 keys — FCF, OpM, PM, ROE, ROIC | `quality._statement_metrics` | shares one `statement_frames` fetch |
| `distress` | 13 keys | `derived.distress_metrics` | free — same frames |
| `moat` | 10 keys | `derived.moat_metrics` | free — same frames |
| `price_risk` | 12 keys (**9 consumed**) | `price_risk.metrics` | free — caller passes closes |
| `yahoo_deep` | 18 keys, all consumed | `quality.deep_metrics` over `research_collect.collect_yahoo` | **expensive** — ~9 endpoints |
| `sec_flags` | 10 keys | `sec.flags` | EDGAR, deep only |
| `ibkr` | no fixed set | `ibkr.metrics` | local gateway — **dormant** |

Sharing one `statement_frames` call across `yahoo_stmt`, `distress` and `moat`
is why 23 distress and moat metrics grade every tier-1 hit at zero extra network
cost. Every source is independently `try`/`except`'d; missing → gate fails,
score skips, **veto never fires**.

**Verified:** all 8 config prefixes have implementations, and no parameter names
a field its resolver does not produce — zero broken sources across all 77.

---

## 4. Quality registry — 77 parameters

Column notes:

- **Coverage** — how many of the 503 constituents resolved to a usable value in
  the 2026-08-11 pass. Deep parameters are marked n/a¹: they are never collected
  at universe scale, so there is no 503-wide reading. Measured separately on the
  5 post-2026-08-09 `_facts.json` snapshots, all 30 enabled deep parameters
  resolved — a thin but clean sample. Two parameters are marked "applicable"²:
  they are **undefined for part of the index by design**, so their count is
  against the population the metric applies to, not against 503. See §7.1.
- **Med.norm** — the median *normalized* (0–1, good→1) reading across the index.
  This is the calibration diagnostic: a metric near **0.50** discriminates well,
  under **0.25** marks the whole index bad, over **0.85** marks it all fine. It
  matters most under `worst_k`, where a miscalibrated anchor takes over the axis.
  Sector-relative metrics sit at exactly 0.50 by construction — that is the
  percentile path working, not a coincidence.
- **Gate** feeds the ⭐ badge; **Veto** is the separate exclusion layer
  (`quality.veto_enforced: false`, so it labels rather than gates).

#### `valuation` — reward axis, weight 0.16, mean (4 parameters)

| Parameter | Source | Stage | Gate | Score good/bad | Veto | Sec-rel | Coverage /503 | Med.norm | Status |
|---|---|---|---|---|---|---|---|---|---|
| `trailingPE` | `yahoo_info.trailingPE` | fast | ≤ 37 | 12 / 45 |  | ✓ | 476/503 (95%) | 0.50 | ok |
| `trailingPegRatio` | `yahoo_info.trailingPegRatio` | fast | ≤ 2.1 | 0.8 / 3.0 |  |  | 440/503 (87%) | 0.58 | ok |
| `analyst_upside_pct` | `yahoo_deep.analyst_upside_pct` | deep | — | 25 / -10 |  |  | n/a¹ | — | ok |
| `pe_percentile_2y` | `yahoo_deep.pe_percentile_2y` | deep | — | 10 / 90 |  |  | n/a¹ | — | ok |

#### `growth` — reward axis, weight 0.16, mean (4 parameters)

| Parameter | Source | Stage | Gate | Score good/bad | Veto | Sec-rel | Coverage /503 | Med.norm | Status |
|---|---|---|---|---|---|---|---|---|---|
| `revenueGrowth` | `yahoo_info.revenueGrowth` | fast | ≥ 10 | 20 / 0 |  | ✓ | 502/503 (100%) | 0.50 | ok |
| `growth_next_year_pct` | `yahoo_deep.growth_next_year_pct` | deep | — | 15 / 0 |  |  | n/a¹ | — | ok |
| `growth_this_year_pct` | `yahoo_deep.growth_this_year_pct` | deep | — | 20 / 0 |  |  | n/a¹ | — | ok |
| ~~`ibkr_revenue_growth_rate`~~ | `ibkr.revenue_growth_rate` | deep | — | 20 / 0 |  |  | n/a¹ | — | DORMANT — needs the Refinitiv add-on this account lacks |

#### `estimate_momentum` — reward axis, weight 0.13, mean (2 parameters)

| Parameter | Source | Stage | Gate | Score good/bad | Veto | Sec-rel | Coverage /503 | Med.norm | Status |
|---|---|---|---|---|---|---|---|---|---|
| `eps_revision_net` | `yahoo_deep.eps_revision_net` | deep | — | 0.6 / -0.4 |  |  | n/a¹ | — | ok |
| `eps_trend_change` | `yahoo_deep.eps_trend_change` | deep | — | 0.05 / -0.05 |  |  | n/a¹ | — | ok |

#### `earnings_quality` — reward axis, weight 0.1, mean (2 parameters)

| Parameter | Source | Stage | Gate | Score good/bad | Veto | Sec-rel | Coverage /503 | Med.norm | Status |
|---|---|---|---|---|---|---|---|---|---|
| `earnings_avg_surprise` | `yahoo_deep.earnings_avg_surprise` | deep | — | 8 / -2 |  |  | n/a¹ | — | ok |
| `earnings_beat_rate` | `yahoo_deep.earnings_beat_rate` | deep | — | 1.0 / 0.5 |  |  | n/a¹ | — | ok |

#### `financial_quality` — reward axis, weight 0.24, mean (10 parameters)

| Parameter | Source | Stage | Gate | Score good/bad | Veto | Sec-rel | Coverage /503 | Med.norm | Status |
|---|---|---|---|---|---|---|---|---|---|
| `debtToEquity` | `yahoo_info.debtToEquity` | fast | ≤ 85 | 20 / 150 |  | ✓ | 449/503 (89%) | 0.50 | ok |
| `fcf` | `yahoo_stmt.fcf` | fast | ≥ 0, increasing | — |  |  | 503/503 (100%) | — | ok |
| `operating_margin` | `yahoo_stmt.operating_margin` | fast | ≥ 10, increasing | 30 / 5 |  | ✓ | 457/503 (91%) | 0.50 | ok |
| `profit_margin` | `yahoo_stmt.profit_margin` | fast | ≥ 10, increasing | 20 / 3 |  | ✓ | 503/503 (100%) | 0.50 | ok |
| `roe` | `yahoo_stmt.roe` | fast | ≥ 12 | 25 / 5 |  | ✓ | 503/503 (100%) | 0.50 | ok |
| `roic` | `yahoo_stmt.roic` | fast | ≥ 12 | 20 / 4 |  | ✓ | 470/503 (93%) | 0.50 | ok |
| `gross_margin` | `yahoo_deep.gross_margin` | deep | — | 60 / 20 |  | ✓ | n/a¹ | — | `zero_is_missing` — Yahoo returns a hard 0.0 for banks |
| ~~`ibkr_return_on_equity`~~ | `ibkr.return_on_equity` | deep | — | 25 / 5 |  |  | n/a¹ | — | DORMANT — needs the Refinitiv add-on this account lacks |
| `net_debt_to_ebitda` | `yahoo_deep.net_debt_to_ebitda` | deep | — | 0 / 4 |  |  | n/a¹ | — | ok |
| `return_on_assets` | `yahoo_deep.return_on_assets` | deep | — | 15 / 2 |  | ✓ | n/a¹ | — | ok |

#### `analyst_sentiment` — reward axis, weight 0.11, mean (2 parameters)

| Parameter | Source | Stage | Gate | Score good/bad | Veto | Sec-rel | Coverage /503 | Med.norm | Status |
|---|---|---|---|---|---|---|---|---|---|
| `analyst_buy_ratio` | `yahoo_deep.analyst_buy_ratio` | deep | — | 0.8 / 0.3 |  |  | n/a¹ | — | ok |
| ~~`ibkr_implied_volatility`~~ | `ibkr.implied_volatility` | deep | — | 0.2 / 0.8 |  |  | n/a¹ | — | DORMANT — needs `market_data: true` and a live gateway |

#### `shareholder_returns` — reward axis, weight 0.1, mean (3 parameters)

| Parameter | Source | Stage | Gate | Score good/bad | Veto | Sec-rel | Coverage /503 | Med.norm | Status |
|---|---|---|---|---|---|---|---|---|---|
| `dividendYield` | `yahoo_info.dividendYield` | fast | — | 3 / 0 |  | ✓ | 405/503 (81%) | 0.50 | ok |
| `payoutRatio` | `yahoo_info.payoutRatio` | fast | ≤ 60 | 30 / 80 |  | ✓ | 502/503 (100%) | 0.50 | ok |
| `buyback_2y` | `yahoo_deep.buyback_2y` | deep | — | -5 / 3 |  |  | n/a¹ | — | ok |

#### `moat` — reward axis, weight 0.14, mean (10 parameters)

| Parameter | Source | Stage | Gate | Score good/bad | Veto | Sec-rel | Coverage /503 | Med.norm | Status |
|---|---|---|---|---|---|---|---|---|---|
| `capex_intensity` | `moat.capex_intensity` | fast | — | 3 / 15 |  | ✓ | 481/503 (96%) | 0.50 | ok |
| `fcf_conversion` | `moat.fcf_conversion` | fast | — | 1.0 / 0.4 |  | ✓ | 501/503 (100%) | 0.50 | ok |
| `fcf_margin` | `moat.fcf_margin` | fast | — | 20 / 2 |  | ✓ | 503/503 (100%) | 0.50 | ok |
| `gross_margin_slope` | `moat.gross_margin_slope` | fast | — | 1.0 / -1.5 |  | ✓ | 452/503 (90%) | 0.50 | ok |
| `incremental_roic` | `moat.incremental_roic` | fast | — | 20 / 3 |  | ✓ | 109 applicable² | 0.52 | conditional; small-denominator guard live since 2026-08-11 (needs ≥5% base growth) — range now −352 … +259 |
| `operating_margin_stability` | `moat.operating_margin_stability` | fast | — | 8 / 1 |  | ✓ | 477/503 (95%) | 0.50 | ok |
| `revenue_cagr_5y` | `moat.revenue_cagr_5y` | fast | — | 12 / 0 |  | ✓ | 503/503 (100%) | 0.50 | ok |
| `revenue_growth_years` | `moat.revenue_growth_years` | fast | — | 4 / 1 |  | ✓ | 503/503 (100%) | 0.66 | ok |
| `roic_stability` | `moat.roic_stability` | fast | — | 6 / 1 |  | ✓ | 460/503 (91%) | 0.50 | ok |
| `roic_years_above` | `moat.roic_years_above` | fast | — | 5 / 1 |  | ✓ | 470/503 (93%) | 0.45 | ok |

#### `risk` — risk axis, weight 0.1, `worst_k: 3` (31 parameters)

| Parameter | Source | Stage | Gate | Score good/bad | Veto | Sec-rel | Coverage /503 | Med.norm | Status |
|---|---|---|---|---|---|---|---|---|---|
| `accruals_ratio` | `distress.accruals_ratio` | fast | — | -0.05 / 0.1 |  |  | 501/503 (100%) | 0.91 | **no discrimination** (med 0.91) |
| `altman_z` | `distress.altman_z` | fast | ≥ 1.1 | 4 / 1.8 | ✓ | ✓ | 447/503 (89%) | 0.50 | ok |
| `beneish_m` | `distress.beneish_m` | fast | ≤ -0.5 | -3 / -1.78 | ✓ |  | 385/503 (77%) | 0.64 | veto rule; 24% of the index unverifiable (fails open by design) |
| `cash_runway_quarters` | `distress.cash_runway_quarters` | fast | ≥ 4 | — | ✓ | ✓ | 50 applicable² | — | conditional — all 50 cash burners resolve; peers quiet 21 of 28 breaches (Utilities) |
| `earnings_cash_divergence_years` | `distress.earnings_cash_divergence_years` | fast | ≤ 3 | — | ✓ | ✓ | 503/503 (100%) | — | ok |
| `fcf_negative_years` | `distress.fcf_negative_years` | fast | ≤ 3 | 0 / 3 | ✓ | ✓ | 503/503 (100%) | 0.57 | ok |
| `interest_coverage` | `distress.interest_coverage` | fast | ≥ 1 | 12 / 2 | ✓ | ✓ | 466/503 (93%) | 0.50 | ok |
| `negative_equity` | `distress.negative_equity` | fast | — | 0 / 1 |  |  | 503/503 (100%) | 1.00 | INERT — reads a clean 1.00 for all 503; never enters a worst-3 slice |
| `negative_equity_burn` | `distress.negative_equity_burn` | fast | ≤ 1 | — | ✓ |  | 503/503 (100%) | — | ok |
| `short_interest_change_pct` | `distress.short_interest_change_pct` | fast | — | -10 / 25 |  |  | 501/503 (100%) | 0.76 | ok |
| `short_percent_float` | `distress.short_percent_float` | fast | ≤ 20 | 1 / 15 | ✓ |  | 498/503 (99%) | 0.81 | ok |
| `short_ratio` | `distress.short_ratio` | fast | — | 2 / 10 |  |  | 502/503 (100%) | 0.87 | **no discrimination** (med 0.87) |
| `short_term_debt_to_cash` | `distress.short_term_debt_to_cash` | fast | — | 0.2 / 2 |  | ✓ | 442/503 (88%) | 0.50 | ok |
| `auditor_change` | `sec_flags.auditor_change` | deep | — | 0 / 1 |  |  | n/a¹ | — | ok |
| `bankruptcy_filing` | `sec_flags.bankruptcy_filing` | deep | ≤ 1 | — | ✓ |  | n/a¹ | — | ok |
| `current_ratio` | `yahoo_deep.current_ratio` | deep | — | 1.8 / 0.6 |  | ✓ | n/a¹ | — | re-anchored 1.8/0.6 on 2026-08-10 after owning worst-k slots |
| `debt_acceleration` | `sec_flags.debt_acceleration` | deep | — | 0 / 1 |  |  | n/a¹ | — | ok |
| `delisting_notice` | `sec_flags.delisting_notice` | deep | ≤ 1 | — | ✓ |  | n/a¹ | — | ok |
| `dilution_veto` | `yahoo_deep.buyback_2y` | deep | ≤ 10 | — | ✓ |  | n/a¹ | — | shares `yahoo_deep.buyback_2y` with the scored parameter |
| `downgrades_90d` | `yahoo_deep.downgrades_90d` | deep | — | 0 / 3 |  |  | n/a¹ | — | ok |
| `eps_collapse_veto` | `yahoo_deep.eps_revision_net` | deep | ≥ -0.5 | — | ✓ |  | n/a¹ | — | shares `yahoo_deep.eps_revision_net` with the scored parameter |
| `going_concern` | `sec_flags.going_concern` | deep | ≤ 1 | — | ✓ |  | n/a¹ | — | ok |
| `insider_net_shares_6m` | `yahoo_deep.insider_net_shares_6m` | deep | — | 0 / -500000 |  |  | n/a¹ | — | ok |
| `late_filing` | `sec_flags.late_filing` | deep | ≤ 1 | — | ✓ |  | n/a¹ | — | ok |
| `liquidity_distress` | `yahoo_deep.liquidity_distress` | deep | ≤ 1 | — | ✓ |  | n/a¹ | — | ok |
| `net_debt_to_ebitda_veto` | `yahoo_deep.net_debt_to_ebitda` | deep | ≤ 6 | — | ✓ | ✓ | n/a¹ | — | shares `yahoo_deep.net_debt_to_ebitda` with the scored parameter |
| ~~`officer_departure`~~ | `sec_flags.officer_departure` | deep | — | 0 / 1 |  |  | n/a¹ | — | OFF — 8-K 5.02 covers routine director elections; 100% base rate carried no information |
| `quick_ratio` | `yahoo_deep.quick_ratio` | deep | — | 1.3 / 0.3 |  | ✓ | n/a¹ | — | re-anchored 1.3/0.3 on 2026-08-10 after owning worst-k slots |
| `restatement` | `sec_flags.restatement` | deep | ≤ 1 | — | ✓ |  | n/a¹ | — | ok |
| `shelf_registration` | `sec_flags.shelf_registration` | deep | — | 0 / 1 |  |  | n/a¹ | — | reads 0 → clean 1.00; never enters a worst-k slice. Do not disable on the old suspicion |
| `unregistered_sale` | `sec_flags.unregistered_sale` | deep | — | 0 / 1 |  |  | n/a¹ | — | ok |

#### `market_risk` — risk axis, weight 0.08, `worst_k: 3` (9 parameters)

| Parameter | Source | Stage | Gate | Score good/bad | Veto | Sec-rel | Coverage /503 | Med.norm | Status |
|---|---|---|---|---|---|---|---|---|---|
| `beta` | `price_risk.beta` | fast | — | 0.6 / 1.8 |  |  | 503/503 (100%) | 1.00 | **INERT** — index median beta 0.565 vs anchors 0.6/1.8, so nearly all clamp to max safety |
| `downside_beta` | `price_risk.downside_beta` | fast | — | 0.6 / 2.0 |  |  | 503/503 (100%) | 0.97 | **INERT** — same cause as `beta` |
| `downside_deviation` | `price_risk.downside_deviation` | fast | — | 10 / 40 |  |  | 503/503 (100%) | 0.61 | anchors re-fixed 2026-08-10 (Sortino semideviation); median norm now healthy |
| `max_drawdown_1y` | `price_risk.max_drawdown_1y` | fast | — | 8 / 55 |  |  | 503/503 (100%) | 0.64 | ok |
| `max_drawdown_3y` | `price_risk.max_drawdown_3y` | fast | — | 15 / 70 |  |  | 497/503 (99%) | 0.63 | ok |
| `pct_below_52w_high` | `price_risk.pct_below_52w_high` | fast | — | 3 / 40 |  |  | 503/503 (100%) | 0.79 | ok |
| `return_skew` | `price_risk.return_skew` | fast | — | 0.3 / -1.0 |  |  | 503/503 (100%) | 0.68 | ok |
| `ulcer_index` | `price_risk.ulcer_index` | fast | — | 3 / 25 |  |  | 503/503 (100%) | 0.61 | ok |
| `volatility_252d` | `price_risk.volatility_252d` | fast | — | 15 / 55 |  |  | 503/503 (100%) | 0.60 | ok |

---

## 5. Screen and exit thresholds — 50 parameters

`enabled` gates the **nightly alert only** — `backtest_universe.py` and
`tune_screen.py` deliberately ignore it, because a screen gets switched off
precisely when it is underperforming, which is when you most need to measure
it.

#### `breakout_strategy` — Tier 1 — breakout · consumed by `breakout_scanner.py`

| Parameter | Value | Swept by `tune_screen` | In grid | Note |
|---|---|---|---|---|
| `enabled` | `true` | — |  |  |
| `consolidation_window_days` | `312` | [126, 252, 312] |  | total lookback; needs `data.download_period` > 312 bars — 2y ≈ 504 ✓ |
| `max_consolidation_range_pct` | `0.38` | [0.2, 0.3, 0.38, 0.5] | ✓ |  |
| `breakout_multiplier` | `1.01` | [1.0, 1.005, 1.01, 1.02] |  |  |
| `min_candle_body_pct` | `0.01` | [0.0, 0.005, 0.01, 0.02] | ✓ |  |
| `volume_sma_days` | `30` | — |  |  |
| `volume_surge_multiplier` | `1.1` | [1.0, 1.1, 1.3, 1.5] | ✓ |  |
| `near_miss_max_gap_pct` | `0.01` | [0.005, 0.01, 0.03, 0.05] |  |  |

#### `pullback_strategy` — Tier 1 — SMA pullback · consumed by `sma_pullback.py`

| Parameter | Value | Swept by `tune_screen` | In grid | Note |
|---|---|---|---|---|
| `enabled` | `true` | — |  |  |
| `sma_days` | `150` | [100, 150, 200] |  | with `trend_lookback_days` → 402 bars needed; 2y ≈ 504 ✓ |
| `touch_band_pct` | `0.02` | [0.01, 0.02, 0.03] | ✓ |  |
| `trend_lookback_days` | `252` | — |  |  |
| `sma_slope_lookback_days` | `63` | — |  |  |
| `min_days_above_sma_pct` | `0.85` | [0.75, 0.85, 0.9] | ✓ |  |
| `max_candle_body_pct` | `0.01` | [0.005, 0.01, 0.02] |  |  |
| `min_candle_range_pct` | `0.03` | [0.02, 0.03, 0.04] |  |  |
| `require_reversal_candle` | `false` | [true, false] | ✓ | **live value is `false`** — CLAUDE.md describes the default as true; confirm this is intended |
| `alert_only_on_band_entry` | `true` | — |  |  |

#### `reclaim_strategy` — Tier 1 — SMA reclaim · consumed by `sma_reclaim.py`

| Parameter | Value | Swept by `tune_screen` | In grid | Note |
|---|---|---|---|---|
| `enabled` | `false` | — |  | **OFF since 2026-07-26** — excess −1.65 vs random-entry baseline; deliberately still measurable by backtest/tuner |
| `sma_days` | `180` | — |  | with `below_lookback_days` → 380 bars needed; 2y ≈ 504 ✓ |
| `below_lookback_days` | `200` | [100, 150, 200, 250] |  |  |
| `min_days_below_pct` | `0.8` | [0.6, 0.7, 0.8, 0.9] |  |  |
| `cross_margin_pct` | `0.01` | [0.005, 0.01, 0.02, 0.025] | ✓ |  |
| `min_candle_body_pct` | `0.0` | [0.0, 0.01, 0.02, 0.03] | ✓ |  |
| `min_day_gain_pct` | `null` | [null, 0.03, 0.05] |  | `null` reproduces body-only R5; the gap route can only add signals |
| `volume_sma_days` | `30` | — |  |  |
| `volume_surge_multiplier` | `1.2` | [1.0, 1.2, 1.5] |  |  |
| `sma_slope_lookback_days` | `30` | [21, 30, 63] |  |  |
| `min_sma_slope_pct` | `null` | [null, -0.2, -0.15, -0.1, -0.05] | ✓ | `null` = slope confirmation disabled |
| `alert_only_on_cross` | `true` | — |  |  |

#### `trend_strategy` — Tier 1 — near-linear uptrend · consumed by `trend_line.py`

| Parameter | Value | Swept by `tune_screen` | In grid | Note |
|---|---|---|---|---|
| `enabled` | `false` | — |  | **OFF since 2026-08-13** — shipped disabled to be measured first; reads excess −1.79 (30d) / −2.22 (60d) vs the random-entry baseline on 428 signals. Deliberately still measurable by backtest/tuner |
| `trend_window_days` | `210` | [168, 210, 252] |  | the **duration** knob, ~10 months; → 211 bars needed, 2y ≈ 504 ✓ |
| `fit_on_log_price` | `true` | [true, false] |  | log space makes a constant-% grower a straight line and the residual scale-free; `false` normalizes slope and residual by the window's mean price |
| `min_annual_slope_pct` | `0.2` | [0.1, 0.2, 0.3] |  | the **angle** floor, a *qualifier*. Barely binds — r² ≥ 0.85 with a positive slope already implies a steep one |
| `max_annual_slope_pct` | `0.8` | [0.6, 0.8, 1.5, null] | ✓ | the ceiling, a **disqualifier** applied to the signal, never to the state freshness watches. Folded into the state it fired when a hot trend *decelerated* through the bar — 18% of all signals. Removing it improves measured excess (−0.63 at `null`); it is kept because a parabola is not a line, which is a definition rather than a backtest |
| `min_r_squared` | `0.85` | [0.75, 0.8, 0.85, 0.9] | ✓ | the linearity gate, and the one axis with real gradient: 0.9 reads excess −0.06 on 208 signals at a 60.6% win rate |
| `max_residual_pct` | `0.08` | [0.05, 0.06, 0.08, 0.1] | ✓ | the **volatility around the line** knob (residual std as a fraction of price) |
| `max_last_dev_pct` | `null` | [null, 0.05, 0.1] |  | optional "not extended today" gate. Off by default: switching it on makes the qualifying state flicker, so one trend re-signals repeatedly — it more than doubles the signal count and changes the event from "the trend began" to "price came back to the line" |
| `max_partial_fails` | `1` | — |  | width of the **backtest-only control cohort**; never reaches the alert. A test pins that changing it leaves the signal count untouched |
| `alert_only_on_new_trend` | `true` | — |  | the signal is the transition, not the state (~0.57/night against ~53/night) |

#### `exit_strategy` — Tier 4 — double-top exit · consumed by `portfolio_sim/exits.py`

| Parameter | Value | Swept by `tune_screen` | In grid | Note |
|---|---|---|---|---|
| `enabled` | `true` | — |  |  |
| `recent_window_days` | `20` | — |  |  |
| `prior_window_days` | `90` | — |  |  |
| `max_peak_diff_pct` | `0.03` | — |  |  |
| `min_trough_depth_pct` | `0.05` | — |  |  |
| `break_confirm_pct` | `0.005` | — |  |  |
| `volume_sma_days` | `30` | — |  |  |
| `min_volume_ratio` | `null` | — |  | `null` = volume confirmation disabled |
| `alert_only_on_break` | `true` | — |  |  |
| `discord_alert` | `true` | — |  | the only tier-4 path that posts to Discord |

---

## 6. Operational config — 140 settings

Not scoring parameters: schedules, paths, windows, and the meta-knobs that
govern how the 77 are combined. The Discord webhook is redacted here — it is
live, and this file is committed.

| Setting | Value | Consumer | Note |
|---|---|---|---|
| `data.universe_sources` | `sp500` (alert), `sp400` (alert since 2026-08-14) | scanner_common.py | one entry per index: `name`, `label`, `url`, `alert`. **`alert` gates the nightly Discord message only** — the plane, the peer stats and the backtest grade every source, the same split `<screen>.enabled` draws. Wikipedia publishes the S&P 500/400/600 lists with identical headings, so a new index needs no parser. |
| `data.download_period` | `"2y"` | scanner_common.py | must exceed every screen's lookback: breakout 312, pullback 402, reclaim 380, trend 211 bars. 2y ≈ 504 ✓ |
| `data.download_interval` | `"1d"` | scanner_common.py |  |
| `charts.enabled` | `true` | charts.py, run_scanners.py |  |
| `charts.partial_charts` | `true` | charts.py, run_scanners.py |  |
| `charts.lookback_days` | `250` | charts.py, run_scanners.py |  |
| `charts.dpi` | `120` | charts.py, run_scanners.py |  |
| `backtest.years` | `3` | backtest_universe.py |  |
| `backtest.holding_days` | `[10, 30, 60]` | backtest_universe.py |  |
| `backtest.entry_delay_days` | `[0, 1, 2, 3, 4, 5]` | backtest_universe.py |  |
| `backtest.entry` | `"next_open"` | backtest_universe.py |  |
| `backtest.detail.entry_delay_days` | `0` | backtest_universe.py |  |
| `backtest.detail.holding_days` | `30` | backtest_universe.py |  |
| `backtest.split_by_tier` | `false` | backtest_universe.py |  |
| `backtest.measure_excursions` | `true` | backtest_universe.py |  |
| `backtest.benchmark_ticker` | `"SPY"` | backtest_universe.py |  |
| `backtest.cache_path` | `"backtest_universe_cache.pkl"` | backtest_universe.py |  |
| `backtest.cache_max_age_days` | `1` | backtest_universe.py | the panel `tune_screen.py` and the tests also read |
| `backtest.screens` | `["breakout_strategy", "pullback_strategy", "reclaim_strategy", "trend_strategy"]` | backtest_universe.py | a screen in the `SCREENS` registry but missing here is silently skipped |
| `backtest.output.trades_csv` | `"backtest_universe_trades.csv"` | backtest_universe.py |  |
| `backtest.output.summary_csv` | `"backtest_universe_summary.csv"` | backtest_universe.py |  |
| `backtest.output.chart_path` | `"backtest_universe.png"` | backtest_universe.py |  |
| `backtest.output.grid_chart_path` | `"backtest_universe_grid.png"` | backtest_universe.py |  |
| `backtest.output.chart_dpi` | `120` | backtest_universe.py |  |
| `tuning.years` | `5` | tune_screen.py |  |
| `tuning.holding_days` | `[30, 60]` | tune_screen.py |  |
| `tuning.protected_cases` | `[{"screen": "reclaim_strategy", "ticker": "META", "date": "2023-02…` | tune_screen.py |  |
| `universe.dir` | `"universe"` | universe_scan.py |  |
| `universe.cache` | `"fundamentals_cache.pkl"` | universe_scan.py |  |
| `universe.table_csv` | `"risk_reward"` | universe_scan.py |  |
| `universe.chart_path` | `"risk_reward.png"` | universe_scan.py |  |
| `universe.html_path` | `"risk_reward.html"` | universe_scan.py |  |
| `universe.chart_dpi` | `120` | universe_scan.py |  |
| `universe.cache_max_age_days` | `7` | universe_scan.py | per-ticker freshness, not whole-file |
| `universe.price_period` | `"5y"` | universe_scan.py | 5y — deliberately not `backtest_universe.cached_panel` |
| `universe.request_delay_s` | `0.2` | universe_scan.py |  |
| `universe.retries` | `2` | universe_scan.py |  |
| `universe.progress_every` | `10` | universe_scan.py |  |
| `universe.signal_window_days` | `7` | universe_scan.py | the weekly `--from-signals` window |
| `research.latest_hits_path` | `"latest_hits.json"` | research_report.py, sec.py |  |
| `research.report_subdir` | `"reports"` | research_report.py, sec.py |  |
| `research.auto.enabled` | `true` | research_report.py, sec.py |  |
| `research.auto.gate` | `"all"` | research_report.py, sec.py | **loose gate** — `all` means every tier-1 hit is a tier-3 candidate, not just ⭐ passes |
| `research.auto.max_reports` | `15` | research_report.py, sec.py |  |
| `research.auto.discord_send` | `true` | research_report.py, sec.py | not writable via MCP config tools, by design |
| `research.history.enabled` | `true` | research_report.py, sec.py |  |
| `research.history.dir` | `"history"` | research_report.py, sec.py |  |
| `research.history.csv` | `"signals.csv"` | research_report.py, sec.py |  |
| `research.history.on_demand_csv` | `"on_demand_scans_results.csv"` | research_report.py, sec.py |  |
| `research.logging.enabled` | `true` | research_report.py, sec.py |  |
| `research.logging.dir` | `"logs"` | research_report.py, sec.py |  |
| `research.logging.keep_runs` | `200` | research_report.py, sec.py |  |
| `research.financials.years` | `4` | research_report.py, sec.py |  |
| `research.financials.quarters` | `4` | research_report.py, sec.py |  |
| `research.financials.chart_dpi` | `120` | research_report.py, sec.py |  |
| `research.sec.user_agent` | `""` — set in `.env` | research_report.py, sec.py, newsfeed.py | Resolved from `STOCK_ANALYZER_SEC_USER_AGENT`; SEC requires a contact in the UA. `scanner_common.sec_user_agent` is the one reader |
| `research.sec.forms` | `["10-Q", "10-K"]` | research_report.py, sec.py |  |
| `research.sec.max_section_chars` | `24000` | research_report.py, sec.py |  |
| `research.sec.xbrl_concepts` | `["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"…` | research_report.py, sec.py |  |
| `research.synthesis.tiers` | `[{"label": "STRONG", "min": 80, "color": "2E9C6B"}, {"label": "WAT…` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.valuation.weight` | `0.18` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.valuation.metrics.pe_percentile_2y.good` | `10` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.valuation.metrics.pe_percentile_2y.bad` | `90` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.valuation.metrics.analyst_upside_pct.good` | `25` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.valuation.metrics.analyst_upside_pct.bad` | `-10` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.growth.weight` | `0.18` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.growth.metrics.growth_this_year_pct.good` | `20` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.growth.metrics.growth_this_year_pct.bad` | `0` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.growth.metrics.growth_next_year_pct.good` | `15` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.growth.metrics.growth_next_year_pct.bad` | `0` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.estimate_momentum.weight` | `0.15` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.estimate_momentum.metrics.eps_revision_net.good` | `0.6` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.estimate_momentum.metrics.eps_revision_net.bad` | `-0.4` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.estimate_momentum.metrics.eps_trend_change.good` | `0.05` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.estimate_momentum.metrics.eps_trend_change.bad` | `-0.05` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.earnings_quality.weight` | `0.12` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.earnings_quality.metrics.earnings_avg_surprise.good` | `8` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.earnings_quality.metrics.earnings_avg_surprise.bad` | `-2` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.earnings_quality.metrics.earnings_beat_rate.good` | `1.0` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.earnings_quality.metrics.earnings_beat_rate.bad` | `0.5` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.financial_quality.weight` | `0.22` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.financial_quality.metrics.return_on_assets.good` | `15` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.financial_quality.metrics.return_on_assets.bad` | `2` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.financial_quality.metrics.gross_margin.good` | `60` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.financial_quality.metrics.gross_margin.bad` | `20` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.financial_quality.metrics.net_debt_to_ebitda.good` | `0` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.financial_quality.metrics.net_debt_to_ebitda.bad` | `4` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.financial_quality.metrics.buyback_2y.good` | `-5` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.financial_quality.metrics.buyback_2y.bad` | `3` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.analyst_sentiment.weight` | `0.15` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.analyst_sentiment.metrics.analyst_buy_ratio.good` | `0.8` | research_report.py, sec.py |  |
| `research.synthesis.dimensions.analyst_sentiment.metrics.analyst_buy_ratio.bad` | `0.3` | research_report.py, sec.py |  |
| `portfolio.enabled` | `true` | portfolio_sim/ |  |
| `portfolio.dir` | `"portfolio"` | portfolio_sim/ |  |
| `portfolio.positions_csv` | `"positions.csv"` | portfolio_sim/ |  |
| `portfolio.findings_csv` | `"findings.csv"` | portfolio_sim/ |  |
| `portfolio.exits_csv` | `"exits.csv"` | portfolio_sim/ |  |
| `portfolio.entry` | `"next_open"` | portfolio_sim/ |  |
| `portfolio.horizons` | `[10, 30, 60]` | portfolio_sim/ |  |
| `portfolio.notional` | `10000` | portfolio_sim/ |  |
| `portfolio.benchmark_ticker` | `"SPY"` | portfolio_sim/ |  |
| `portfolio.measure_excursions` | `true` | portfolio_sim/ |  |
| `portfolio.analysis.min_n` | `20` | portfolio_sim/ | the `sufficient_n` bar every finding is gated on |
| `portfolio.analysis.alpha` | `0.05` | portfolio_sim/ |  |
| `portfolio.analysis.bootstrap_iters` | `2000` | portfolio_sim/ |  |
| `portfolio.analysis.fdr` | `true` | portfolio_sim/ | Benjamini–Hochberg across the whole findings file |
| `portfolio.analysis.keep_dated_findings` | `true` | portfolio_sim/ |  |
| `enrichment.enabled` | `true` | enrichment.py | `false` makes `record` a no-op; the agent's judgment is then simply not on the record |
| `enrichment.dir` | `"enrichment"` | enrichment.py | resolved inside `output/` |
| `enrichment.csv` | `"enrichment.csv"` | enrichment.py | one row per `(scan_date, ticker)`, rewritten not appended |
| `combined.dir` | `"combined"` | combined_report.py | dossiers; the filename carries the scope so a subset cannot overwrite a wider run |
| `ibkr.enabled` | `false` | ibkr.py | **false** — with `reports: []` the 3 `ibkr.*` parameters would resolve to `{}` even if switched on |
| `ibkr.host` | `"127.0.0.1"` | ibkr.py |  |
| `ibkr.port` | `4001` | ibkr.py |  |
| `ibkr.client_id` | `17` | ibkr.py |  |
| `ibkr.connect_timeout_seconds` | `8` | ibkr.py |  |
| `ibkr.request_timeout_seconds` | `20` | ibkr.py |  |
| `ibkr.market_data` | `true` | ibkr.py |  |
| `ibkr.reports` | `[]` | ibkr.py | empty — no Refinitiv add-on on this account |
| `ibkr.market_data_type` | `3` | ibkr.py |  |
| `ibkr.snapshot_wait_seconds` | `8` | ibkr.py |  |
| `ibkr.fundamental_ticks` | `false` | ibkr.py |  |
| `discord.webhook_url` | «live webhook — redacted» | scanner_common.py |  |
| `discord.send_message_when_no_breakouts` | `true` | scanner_common.py |  |
| `discord.username` | `"Breakout Scanner"` | scanner_common.py |  |
| `discord.request_timeout_seconds` | `15` | scanner_common.py |  |
| `quality.enabled` | `true` | quality.py |  |
| `quality.badge` | `"\u2b50"` | quality.py |  |
| `quality.statement_years` | `5` | quality.py |  |
| `quality.moat_roic_hurdle_pct` | `12` | quality.py |  |
| `quality.veto_badge` | `"\ud83d\udeab"` | quality.py |  |
| `quality.veto_tier` | `"AVOID"` | quality.py |  |
| `quality.veto_enforced` | `false` | quality.py | **false** — the veto is a recorded label, not a gate, so tier 4 can grade it |
| `quality.moat_incremental_min_base_growth_pct` | `5` | quality.py | added 2026-08-11 — minimum invested-capital growth before `incremental_roic` is defined at all, which is what suppresses the small-denominator artifacts |
| `quality.quadrant.reward_min` | `58` | quality.py, universe_scan.py | recalibrated 60 → 58 when peer scoring went live |
| `quality.quadrant.risk_max` | `56` | quality.py, universe_scan.py | recalibrated 30 → 56 when `worst_k: 3` went live |
| `quality.peers.enabled` | `true` | peers.py |  |
| `quality.peers.path` | `"universe/peer_stats.json"` | peers.py |  |
| `quality.peers.min_peers` | `12` | peers.py | a sector thinner than this falls back to absolute anchors, silently |
| `quality.peers.veto_percentile` | `0.1` | peers.py | the conjunction half — a peer veto also needs the absolute breach |

---

## 7. Findings — the worklist

Everything here is measured against the 2026-08-11 pass, not inferred.

### 7.1 Coverage

**Two parameters are undefined for part of the index by design, and their
coverage must not be read as a defect.** A low number here means "not
applicable", not "missing" — the same distinction `quality.has_values` draws
between failed and not evaluated.

| Parameter | Applicable population | Verified |
|---|---|---|
| `cash_runway_quarters` | the 50 companies with negative latest FCF — runway is meaningless for a cash generator (`derived.py:425`) | **50 of 50 resolve.** Zero burners without a runway, zero non-burners with one. |
| `incremental_roic` | companies whose invested-capital base grew **materially** — at least `quality.moat_incremental_min_base_growth_pct` (5%) | **109 resolve** (124 before the guard) |

`cash_runway_quarters` was checked end to end, because a veto rule that only
sees 10% of the index looks alarming until you follow it through:

- The 453 cash-generative companies are **not** penalized on the ⭐ badge —
  `gate_failures` skips every `veto: true` parameter, by design.
- A missing value **never** vetoes, so they are not excluded either.
- 28 of the 50 burners breach the absolute `min: 4` gate. **The peer conjunction
  quiets 21 of them, all Utilities** — leaving 7 vetoed: SRE (0.02q) and PNW
  (0.03q), which really are the worst of their own sector, plus 5 names in
  sectors too thin to rank (FANG, IRM, NCLH, APD, MOS).

That is `peers.py` doing exactly the job it was written for. Utilities finance a
rate base and run negative FCF structurally; before peer scoring this metric was
one of the three that excluded 26 of 31 of them. **Nothing to fix.**

**`incremental_roic` — fixed 2026-08-11.** The genuine problem here was never
coverage but the denominator: `100 × ΔEBIT / ΔInvestedCapital` explodes when the
capital base barely moved. Measured across the index, every reading beyond ±800
came from a base that grew under 4% — ZTS +1102 on **0.5%** growth, CHD +1001 on
0.7%, EL −2159 on 1.6% — while genuinely large readings came from real
reinvestment (CAH +259 on 24.8%).

The fix is a materiality guard on the denominator
(`quality.moat_incremental_min_base_growth_pct`, default **5%**), not
winsorization of the output. Clamping would have kept the artifacts and merely
moved them to the boundary — and because this parameter is `sector_relative`,
its score is a peer *percentile*, where a clamp manufactures a tie cluster at
exactly the cap. That is the failure `peers.percentile` documents for
`fcf_negative_years`. Returning `None` instead says "not measurable for this
company", which every consumer already handles.

Confirmed by a full `--refresh` pass on 2026-08-11: **15 readings suppressed**,
and the range collapsed from −2158.9 … +1101.6 to **−352.4 … +259.0** with a
median of 15.9. EL, UPS, ZTS, CHD, CTVA, GEV, GILD and PSX now return `None`;
BLDR (−352), NUE (−268), CAH (+259), JNJ (+107), KO and MSFT are unchanged — the
guard is a floor on *growth*, not on the reading.

The plane barely moved, which is the expected result and worth recording so the
next scoring change has a baseline: index median reward **50.8 → 51.0**, median
risk 60.2 → 60.1, the buy quadrant unchanged at **46** names, and exactly one
ticker crossing a boundary (BBY, dull → avoid, on the risk axis — its reward rose).
**`quality.quadrant` needed no recalibration**, unlike the peer-scoring and
`worst_k` changes before it.

| Parameter | Coverage | Note |
|---|---|---|
| `beneish_m` | **385/503 (77%)** | Genuinely thin, not conditional. Veto rule; a quarter of the index unverifiable. Fails open by design, but the manipulation screen is off for 118 companies. |

### 7.2 `worst_risk` explained the score on the wrong basis — fixed 2026-08-11

The plane's `worst_risk` column names the three readings that drove a company's
risk score. It computed them with `quality.normalize` against the **absolute**
anchors, while the score itself used the **peer** path for the 30
`sector_relative` parameters — so the explanation contradicted the number beside
it.

NEE printed `Altman Z 0.00; Int coverage 0.00; ST debt/cash 0.00` next to a risk
of **35.2**, one of the lowest in the index. Peer-aware, those same values read
**0.82, 0.11 and 0.73** — NEE sits in the *safer* 82nd percentile of Utilities on
Altman Z. Two of the three named metrics were not among its worst three at all.
A utility financing a rate base has a structurally low absolute Altman Z, so the
column resurrected precisely the sector bias `peers.py` exists to remove.

**Blast radius: 314 of 503 rows (62%) named the wrong worst-three**, every sector
affected — Industrials 57, Financials 56, Health Care 31, Utilities 30.

Why the existing AST guard missed it: that check verifies the five grading
*entry points* receive a `sector`. `_worst_risk` bypassed them entirely by
calling the lower-level `quality.normalize`, which has no `sector` argument to
omit — so there was nothing to match.

The fix extracts `quality.normalized_of(key, spec, value, cfg, sector)` as the
one definition of "what did this metric read", now shared by `group_scores`,
`_worst_risk` and `risk_report` (which held a correct but third copy of the same
logic). The guard was widened to fail any direct `quality.normalize` call in a
production module — and it immediately found that third copy.

Verified after the fix: correlation between risk and the mean reported worst-3
is **−0.923**, with zero rows showing a low risk score beside near-zero readings
(or the converse). Only the explanation column was affected — the `risk` axis
itself was always computed correctly.

### 7.3 Metrics carrying no information

Median normalized reading at or above 0.85 — the metric marks nearly the whole
index "fine" and, under `worst_k: 3`, is essentially never selected.

| Parameter | Med.norm | Diagnosis |
|---|---|---|
| `beta` | **1.00** | Index median beta is **0.565** against anchors `good 0.6 / bad 1.8`, so almost every constituent clamps to maximum safety. |
| `downside_beta` | **0.97** | Same cause (anchors 0.6 / 2.0). |
| `negative_equity` | 1.00 | Known and documented — clean 1.00 for all 503. |
| `accruals_ratio` | 0.91 | Anchors `-0.05 / 0.1`; index p90 is −0.000, so the "bad" end is never approached. |
| `short_ratio` | 0.87 | Anchors `2 / 10`; index max is 11.15 and median 3.05. |

**On `beta` specifically** — the low median is a real regime reading, not a
defect. Realized 1-year betas: NVDA 1.88, TSLA 2.25, MSFT 0.93, AAPL 0.73,
against KO −0.28, XOM −0.46, PG −0.07. In a market whose variance is carried by
a handful of mega-caps, defensives genuinely print near-zero realized beta. I
checked the one suspicious thing in the code path —
`price_risk._aligned_returns` cleans the benchmark but not the stock series —
against holed synthetic data: beta moved 1.2535 → 1.2437. **The asymmetry is
harmless; this is an anchor problem, not a bug.**

The practical effect on `market_risk`: with `beta` and `downside_beta` pinned
near 1.00, `worst_k: 3` is drawing from 7 live metrics, not 9. The same is true
of `risk`, where `negative_equity` (1.00), `accruals_ratio` (0.91),
`short_ratio` (0.87) and the mostly-absent `cash_runway_quarters` leave roughly
9 of 13 fast metrics doing the work.

### 7.4 Untuned

- **The double-top exit has no sweep grid at all.** `exit_strategy` carries 8
  numeric thresholds and appears in neither `tuning.sweeps` nor `tuning.grid`,
  and `tune_screen.py` only handles entry screens. The repo's only exit rule is
  the only strategy nobody has swept.
- `breakout_strategy.volume_sma_days`, `pullback_strategy.trend_lookback_days`
  and `sma_slope_lookback_days` have no sweep entry either.

### 7.5 Orphans and dormant entries

- `price_risk.volatility_60d`, `spy_correlation`, `momentum_12_1` — computed on
  every run, consumed by no parameter. `momentum_12_1` is the interesting one:
  whichever parameter adopts it must sit on the **reward** axis, and
  `market_risk` is a risk-axis group.
- The 3 `ibkr.*` parameters are `enabled: false`; with `ibkr.enabled: false` and
  `reports: []` they would resolve to `{}` even if switched on.
- `officer_departure` — off, documented (routine director elections gave it a
  100% base rate).
- `yahoo_info` fetches ~150 fields to consume 6. Free — it is one call either
  way — but worth knowing the surface is there.

### 7.6 Config drift

- The legacy **`fundamentals.*` block is dead config**, read only by
  `migrate_config.py` and one test — and its `quality.rules.debtToEquity` gate
  is **71** against the live registry's **85**. A divergent duplicate of a live
  threshold is the kind of thing that gets read by mistake.
- **`research.synthesis.dimensions` is dead**; only `tiers` is still read from
  that section.
- **`pullback_strategy.require_reversal_candle` is `false`**, while CLAUDE.md
  describes the default as true. Worth confirming this is the intended live
  value — it is a strict filter, so the screen is currently looser than the docs
  suggest.
- `research.auto.gate` is `"all"`, the loose gate: every tier-1 hit becomes a
  tier-3 candidate, not just ⭐ passes.
- `reclaim_strategy.enabled: false` — deliberate and documented (excess −1.65 vs
  the random-entry baseline), and correctly still measurable by the backtest and
  tuner.
- `trend_strategy.enabled: true` — shipped `false` on 2026-08-13 so the screen
  would be measured before it was acted on, then switched on 2026-08-14 by user
  decision against that measurement: excess −1.79 (30d) / −2.22 (60d) on 428
  signals. `min_r_squared` is the axis to tune (0.9 reads −0.06 at a 60.6% win
  rate on n=208). Its first live alert was AIT on 2026-08-13.

### 7.7 Clean — things checked that are fine

- **No fast metric falls below a 0.25 median normalized reading.** The `worst_k`
  failure mode that sank two earlier attempts is currently clear.
- All 30 `sector_relative` metrics centre at exactly 0.500 — `peers.py` is live
  and the percentile path is being taken.
- Every registry `source` resolves to a real implementation. Zero broken
  prefixes, zero field-name mismatches, across all 8 resolvers.
- Group weights sum to **1.32**, which is *not* a bug — `quality._weighted`
  renormalizes over participating members, which is also what makes
  `enabled: false` safe.
- `data.download_period: 2y` (≈504 bars) clears every screen's lookback:
  breakout 312, pullback 402, reclaim 380.
- The deep stage resolves cleanly — 30/30 enabled parameters on the 5 post-fix
  `_facts.json` snapshots. Thin sample, but no holes.
- `downside_deviation` (med 0.61), `current_ratio` and `quick_ratio` all read
  healthy after the 2026-08-10 re-anchoring.

---

## 8. Reproducing

The coverage and median-normalized readings come from the cached full pass, so
they need no network:

```powershell
# inputs the tables were built from
python -c "import pickle; print(len(pickle.load(open('output/universe/fundamentals_cache.pkl','rb'))))"   # 503
python -c "import json,io; print(len(json.load(io.open('config.json',encoding='utf-8'))['quality']['parameters']))"  # 77

# the resolved registry, including switched-off parameters
# (the params_list MCP tool is the better surface interactively)

# refresh the underlying pass (~13 min, no Discord)
python universe_scan.py
python universe_scan.py --no-fetch      # re-render from cache, no network
```

`universe_scan.regrade` re-grades cached entries at render time, so a threshold
change takes effect under `--no-fetch` with no refetch. The **full** pass is on
no schedule — only the weekly signals pass is — and it is the base population
for the sector-relative percentiles, so letting it go stale silently degrades
peer scoring.
