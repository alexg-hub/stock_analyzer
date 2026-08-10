"""Offline -- the statement-derived distress and moat metrics (`derived.py`).

Invariants, not recorded output:

  * **Altman Z and Beneish M are pinned against hand-computed values.** Both are
    published formulas with published cut-offs, so unlike a tuned threshold
    they have a right answer that does not move when config is retuned. Same
    approach `portfolio_sim/stats.py`'s tests take to the statistics.
  * **Flags are `int` 0/1, never `bool`.** `quality.scalar` rejects booleans and
    reads them as missing, and a missing value never vetoes -- so a bool flag
    would look correct in config and silently never fire.
  * **A missing statement row yields None, never an exception**, and never a
    partially-built composite: a Beneish score computed from six of its eight
    indices is a different statistic, not a weaker one.
  * **Cross-statement metrics only use years all three statements report**, so a
    ratio never mixes an income row with a balance column a year apart.
  * The module does **no I/O** beyond `statement_frames`.

The fixtures are built here rather than downloaded: these are arithmetic
identities, so a synthetic company with round numbers is both sufficient and
checkable by hand.
"""

import sys

import pandas as pd

from _harness import Checks

import derived

c = Checks("derived: distress and moat metrics")

YEARS = [pd.Timestamp(f"{y}-12-31") for y in (2021, 2022, 2023, 2024, 2025)]


def frames(income=None, cashflow=None, balance=None, columns=None):
    """Three statements from row dicts, oldest -> newest."""
    cols = columns or YEARS

    def frame(rows):
        return (pd.DataFrame.from_dict(rows, orient="index", columns=cols)
                if rows else pd.DataFrame())

    return {"income": frame(income), "cashflow": frame(cashflow),
            "balance": frame(balance)}


HEALTHY = frames(
    income={"Total Revenue": [100, 110, 120, 130, 140],
            "Cost Of Revenue": [60, 64, 68, 71, 74],
            "EBIT": [10, 12, 14, 16, 18],
            "Tax Provision": [2, 2, 3, 3, 4],
            "Pretax Income": [9, 11, 13, 15, 17],
            "Net Income": [7, 9, 10, 12, 13],
            "Interest Expense": [1, 1, 1, 1, 1],
            "Selling General And Administration": [20, 21, 22, 23, 24]},
    cashflow={"Free Cash Flow": [8, 9, 11, 12, 14],
              "Operating Cash Flow": [12, 13, 15, 17, 19],
              "Capital Expenditure": [-4, -4, -4, -5, -5],
              "Depreciation And Amortization": [5, 5, 6, 6, 7]},
    balance={"Total Assets": [200, 210, 220, 230, 240],
             "Stockholders Equity": [90, 95, 100, 110, 120],
             "Invested Capital": [120, 125, 130, 135, 140],
             "Working Capital": [30, 32, 34, 36, 38],
             "Retained Earnings": [40, 45, 50, 58, 66],
             "Total Liabilities Net Minority Interest": [110, 115, 120, 120, 120],
             "Cash And Cash Equivalents": [20, 22, 24, 26, 28],
             "Current Debt": [5, 5, 5, 5, 5],
             "Current Assets": [60, 62, 64, 66, 68],
             "Current Liabilities": [30, 30, 30, 30, 30],
             "Net PPE": [50, 52, 54, 56, 58],
             "Accounts Receivable": [15, 16, 17, 18, 19],
             "Long Term Debt": [60, 60, 60, 60, 60]})

INFO = {"marketCap": 500, "shortPercentOfFloat": 0.02, "shortRatio": 1.5,
        "sharesShort": 100, "sharesShortPriorMonth": 90}

# --------------------------------------------------------------------------
c.section("Altman Z -- the 1968 public-manufacturer formula")

# X1 = WC/TA        = 38/240 = 0.158333
# X2 = RE/TA        = 66/240 = 0.275
# X3 = EBIT/TA      = 18/240 = 0.075
# X4 = MVE/TL       = 500/120 = 4.166667
# X5 = Sales/TA     = 140/240 = 0.583333
# Z = 1.2(0.158333) + 1.4(0.275) + 3.3(0.075) + 0.6(4.166667) + 1.0(0.583333)
#   = 0.19 + 0.385 + 0.2475 + 2.5 + 0.583333 = 3.905833
c.close("Z matches the hand-computed value",
        derived.altman_z(HEALTHY, INFO), 3.905833, tol=1e-5)
c.ok("...and lands above the 3.0 safe-zone cut-off",
     derived.altman_z(HEALTHY, INFO) > 3.0,
     "the published cut-offs only mean anything against the original weights")

c.ok("a missing market cap yields None, not a partial Z",
     derived.altman_z(HEALTHY, {}) is None)
c.ok("no balance sheet at all yields None",
     derived.altman_z(frames(), INFO) is None)

# Working capital is derivable when Yahoo omits the row outright.
no_wc = frames(
    income={"Total Revenue": [140] * 5, "EBIT": [18] * 5},
    balance={"Total Assets": [240] * 5, "Retained Earnings": [66] * 5,
             "Total Liabilities Net Minority Interest": [120] * 5,
             "Current Assets": [68] * 5, "Current Liabilities": [30] * 5})
c.close("working capital falls back to current assets minus current liabilities",
        derived.altman_z(no_wc, INFO), 3.905833, tol=1e-5)

# --------------------------------------------------------------------------
c.section("Beneish M -- the eight-variable score")

# Using 2024 -> 2025 on HEALTHY:
#   DSRI = (19/140)/(18/130)      = 0.135714/0.138462 = 0.980151
#   GM_t = (140-74)/140 = 0.471429 ; GM_p = (130-71)/130 = 0.453846
#   GMI  = 0.453846/0.471429      = 0.962700
#   AQ_t = 1-(68+58)/240 = 0.475  ; AQ_p = 1-(66+56)/230 = 0.469565
#   AQI  = 0.475/0.469565         = 1.011574
#   SGI  = 140/130                = 1.076923
#   rate_t = 7/(7+58) = 0.107692  ; rate_p = 6/(6+56) = 0.096774
#   DEPI = 0.096774/0.107692      = 0.898609
#   SGAI = (24/140)/(23/130)      = 0.171429/0.176923 = 0.968944
#   TATA = (13-19)/240            = -0.025
#   Lev_t = (30+60)/240 = 0.375   ; Lev_p = (30+60)/230 = 0.391304
#   LVGI = 0.375/0.391304         = 0.958333
# M = -4.84 + 0.920(0.980151) + 0.528(0.962700) + 0.404(1.011574)
#     + 0.892(1.076923) + 0.115(0.898609) - 0.172(0.968944)
#     + 4.679(-0.025) - 0.327(0.958333)
EXPECTED_M = (-4.84 + 0.920 * 0.980151 + 0.528 * 0.962700 + 0.404 * 1.011574
              + 0.892 * 1.076923 + 0.115 * 0.898609 - 0.172 * 0.968944
              + 4.679 * -0.025 - 0.327 * 0.958333)
c.close("M matches the hand-computed value",
        derived.beneish_m(HEALTHY), EXPECTED_M, tol=1e-4)
c.ok("...and lands below the -1.78 manipulation cut-off",
     derived.beneish_m(HEALTHY) < -1.78)

one_year = frames(income={"Total Revenue": [140]}, cashflow={"Free Cash Flow": [14]},
                  balance={"Total Assets": [240]}, columns=YEARS[-1:])
c.ok("a single reported year yields None -- the score is a year-on-year ratio",
     derived.beneish_m(one_year) is None)

missing_sga = frames(
    income={k: v for k, v in
            {"Total Revenue": [100, 110, 120, 130, 140],
             "Cost Of Revenue": [60, 64, 68, 71, 74],
             "Net Income": [7, 9, 10, 12, 13]}.items()},
    cashflow={"Operating Cash Flow": [12, 13, 15, 17, 19]},
    balance={"Total Assets": [200, 210, 220, 230, 240]})
c.ok("one missing index yields None, never a partial score",
     derived.beneish_m(missing_sga) is None,
     "a six-of-eight M is a different statistic, not a weaker one")

# Guarding a ratio's *inputs* is not the same as guarding its result: `_ratio`
# returns None whenever the denominator is missing, and the asset-quality index
# then did `1 - None`. That raised `TypeError` for any company whose Total Assets
# row Yahoo omits -- and because `quality.fetch_fast` catches per ticker, the
# symptom was the ticker silently vanishing from tier 2, not an error anyone saw.
# Found on ADM during the first universe pass. Two more callers below it use the
# same `_ratio` result, so the whole function has to survive the shape.
no_assets = frames(
    income={"Total Revenue": [100, 110, 120, 130, 140],
            "Cost Of Revenue": [60, 64, 68, 71, 74],
            "Net Income": [7, 9, 10, 12, 13],
            "Selling General And Administration": [20, 21, 22, 23, 24]},
    cashflow={"Operating Cash Flow": [12, 13, 15, 17, 19]},
    balance={"Current Assets": [50, 52, 54, 56, 58],
             "Net PPE": [80, 84, 88, 92, 96]})
c.ok("a balance sheet with no Total Assets row yields None, never raises",
     derived.beneish_m(no_assets) is None,
     "1 - _ratio(...) raised TypeError here and dropped the ticker in silence")

c.ok("_one_minus passes None through rather than arithmetic on it",
     derived._one_minus(None) is None and derived._one_minus(0.25) == 0.75)

# The whole resolver must survive it too, not just the one index.
c.ok("distress_metrics survives a missing Total Assets row",
     isinstance(derived.distress_metrics(no_assets, INFO, 5), dict))

# --------------------------------------------------------------------------
c.section("distress -- flags are ints, and conjunctions need both legs")

d = derived.distress_metrics(HEALTHY, INFO, 5)
flags = ["negative_equity", "negative_equity_burn"]
c.ok("every flag is an int, never a bool",
     all(isinstance(d[k], int) and not isinstance(d[k], bool)
         for k in flags if d[k] is not None),
     "quality.scalar rejects bools, so a bool flag could never trip a veto")

c.close("interest coverage is EBIT / |interest|", d["interest_coverage"], 18.0)
c.close("accruals are (net income - CFO) / total assets",
        d["accruals_ratio"], (13 - 19) / 240, tol=1e-9)
c.close("short interest change is month over month",
        d["short_interest_change_pct"], 100 * (100 - 90) / 90, tol=1e-9)
c.ok("a cash-generative company has no runway reading",
     d["cash_runway_quarters"] is None,
     "runway only means anything while the company is burning")

# Negative equity alone is not distress: MCD, HD and SBUX all carry it from
# buybacks. The veto is the conjunction with cash burn.
buyback = frames(
    income={"Total Revenue": [140] * 5, "Net Income": [13] * 5},
    cashflow={"Free Cash Flow": [14] * 5, "Operating Cash Flow": [19] * 5},
    balance={"Total Assets": [240] * 5, "Stockholders Equity": [-10] * 5})
b = derived.distress_metrics(buyback, INFO, 5)
c.ok("negative equity alone is flagged", b["negative_equity"] == 1)
c.ok("...but does not trip the burn conjunction", b["negative_equity_burn"] == 0,
     "buyback-driven negative equity is not distress")

burning = frames(
    income={"Total Revenue": [140] * 5, "Net Income": [-5] * 5},
    cashflow={"Free Cash Flow": [-20] * 5, "Operating Cash Flow": [-15] * 5},
    balance={"Total Assets": [240] * 5, "Stockholders Equity": [-10] * 5,
             "Cash And Cash Equivalents": [30] * 5})
n = derived.distress_metrics(burning, INFO, 5)
c.ok("negative equity WITH burn trips it", n["negative_equity_burn"] == 1)
c.close("cash runway is cash over the quarterly burn rate",
        n["cash_runway_quarters"], 30 / (20 / 4), tol=1e-9)
c.close("every burning year is counted", n["fcf_negative_years"], 5.0)

# --------------------------------------------------------------------------
c.section("distress -- profit without cash must be a TRAILING run")

# Scattered negative-CFO years are structural for banks and insurers (loan
# origination and trading-book swings run through operating cash flow), so
# counting occurrences across the window vetoes JPM on a healthy balance sheet.
scattered = derived._trailing_divergence([1, 1, 1, 1, 1], [-1, 1, -1, 1, 5])
trailing = derived._trailing_divergence([1, 1, 1, 1, 1], [5, 1, -1, -1, -1])
c.ok("a broken run counts zero when the latest year converted",
     scattered == 0, f"two divergent years, none trailing -> {scattered}")
c.ok("an unbroken trailing run counts every consecutive year",
     trailing == 3, f"-> {trailing}")

# --------------------------------------------------------------------------
c.section("moat -- persistence, not a good year")

m = derived.moat_metrics(HEALTHY, INFO, 5, roic_hurdle_pct=12.0)
# Five reported years give four year-on-year comparisons.
c.close("revenue growth years counts the YoY comparisons, not the years",
        m["revenue_growth_years"], 4.0)
c.close("revenue CAGR compounds first over last",
        m["revenue_cagr_5y"], 100 * ((140 / 100) ** 0.25 - 1), tol=1e-9)
c.close("FCF margin is the mean of FCF/revenue",
        m["fcf_margin"],
        100 * sum(f / r for f, r in zip([8, 9, 11, 12, 14],
                                        [100, 110, 120, 130, 140])) / 5,
        tol=1e-9)
c.close("capex intensity uses the absolute capex",
        m["capex_intensity"],
        100 * sum(abs(cx) / r for cx, r in zip([-4, -4, -4, -5, -5],
                                               [100, 110, 120, 130, 140])) / 5,
        tol=1e-9)
c.close("incremental ROIC is the change in EBIT over the change in capital",
        m["incremental_roic"], 100 * (18 - 10) / (140 - 120), tol=1e-9)
c.close("ROIC persistence counts years clearing the hurdle",
        m["roic_years_above"], 0.0)

flat = frames(income={"Total Revenue": [100] * 5, "EBIT": [10] * 5},
              cashflow={"Free Cash Flow": [5] * 5},
              balance={"Total Assets": [200] * 5, "Invested Capital": [100] * 5})
c.ok("a perfectly flat series has no stability reading, not an infinite one",
     derived.stability([5, 5, 5]) is None)
c.ok("a single point has no stability reading", derived.stability([5]) is None)
c.ok("a flat company earns no incremental-ROIC reading",
     derived.moat_metrics(flat, INFO, 5)["incremental_roic"] is None,
     "the invested-capital base did not grow, so the ratio is undefined")
c.ok("...and no growth-consistency reading beyond a flat count",
     derived.moat_metrics(flat, INFO, 5)["revenue_growth_years"] == 0.0)

c.close("slope is the least-squares trend per step",
        derived.slope([1.0, 2.0, 3.0, 4.0]), 1.0, tol=1e-9)
c.ok("slope needs two points", derived.slope([1.0]) is None)

# --------------------------------------------------------------------------
c.section("tolerance -- a thin filing is quiet, never loud")

empty = frames()
c.ok("no statements at all yields all-None distress, not an exception",
     all(v is None for v in derived.distress_metrics(empty, {}, 5).values()))
c.ok("...and all-None moat",
     all(v is None for v in derived.moat_metrics(empty, {}, 5).values()))
c.ok("an empty info dict is tolerated",
     derived.distress_metrics(HEALTHY, {}, 5)["short_ratio"] is None)

# A year only counts when all three statements report it: mixing an income row
# with a balance column a year apart produces a plausible number and no error.
ragged = {"income": HEALTHY["income"],
          "cashflow": HEALTHY["cashflow"].iloc[:, :3],
          "balance": HEALTHY["balance"]}
c.ok("cross-statement years are intersected, not zipped",
     derived._aligned(ragged, 5) == sorted(HEALTHY["cashflow"].columns[:3]),
     "a ratio must never mix statements from different years")

sys.exit(c.finish())
