"""Statement-derived distress and moat metrics -- the `distress` and `moat`
resolvers of the quality registry.

Two questions the registry could not previously ask, both answered from the
three annual statements `quality.collect` **already** fetches for every tier-1
hit:

  * **distress** -- is this company visibly falling over? Solvency, cash burn,
    accounting quality and short-side pressure. Most of these parameters carry
    `veto: true`, which is the "identify the losers" half of the thesis.
  * **moat** -- is its advantage durable? Returns persistence, margin stability
    and trend, growth consistency, cash conversion, capital intensity. All
    proxies, all measurable, all gradeable by tier 4 -- as opposed to the
    qualitative moat narrative, which stays prose in the deep-dive report.

Three rules hold this module together:

  * **It does no I/O.** `statement_frames` is the one function that touches
    yfinance, and it exists so `collect` can fetch the three statements *once*
    and hand the same frames to `_statement_metrics`, `distress_metrics` and
    `moat_metrics`. Everything else here is pure arithmetic over frames it was
    given, which is what makes it testable offline.
  * **Nothing raises and nothing guesses.** A row Yahoo does not publish for
    this company yields `None`, exactly as `_statement_metrics` does for banks
    with no Operating Income. `None` is skipped by the score and -- critically
    -- never trips a veto, so a thin filing makes this layer quieter rather
    than trigger-happy. That is why `_first` tries several row spellings: the
    alternative to tolerating Yahoo's naming drift is a veto set that silently
    stops firing.
  * **Flags are `int` 0/1, never `bool`.** `quality.scalar` rejects `bool` and
    returns None, which for a veto parameter means "no veto" -- a boolean flag
    would look correct in config and never fire. `tests/test_quality.py` pins
    the underlying behaviour and `tests/test_derived.py` pins these outputs.

The two composite scores are the textbook forms, kept intact rather than
tuned, so they can be checked against any published worked example:
`altman_z` is the original 1968 public-manufacturer Z (distress below 1.8) and
`beneish_m` the eight-variable M (manipulation likely above -1.78).
"""

import math

import pandas as pd

from scanner_common import log_step, stmt_value

# Yahoo's statement row labels drift between sectors and releases, so every
# lookup goes through a candidate list. Order matters: the most specific
# spelling first, the broadest fallback last.
ROWS = {
    "revenue": ("Total Revenue", "Operating Revenue"),
    "cogs": ("Cost Of Revenue", "Cost of Revenue", "Reconciled Cost Of Revenue"),
    "ebit": ("EBIT", "Operating Income", "Total Operating Income As Reported"),
    "net_income": ("Net Income", "Net Income Common Stockholders",
                   "Net Income From Continuing Operation Net Minority Interest"),
    "net_income_continuing": ("Net Income Continuous Operations",
                              "Net Income From Continuing Operation Net Minority Interest",
                              "Net Income"),
    "interest": ("Interest Expense", "Interest Expense Non Operating",
                 "Net Interest Income"),
    "sga": ("Selling General And Administration",
            "Selling General And Administrative Expense"),
    "pretax": ("Pretax Income",),
    "tax": ("Tax Provision",),
    "assets": ("Total Assets",),
    "liabilities": ("Total Liabilities Net Minority Interest", "Total Liabilities"),
    "equity": ("Stockholders Equity", "Total Equity Gross Minority Interest"),
    "retained": ("Retained Earnings",),
    "working_capital": ("Working Capital",),
    "current_assets": ("Current Assets", "Total Current Assets"),
    "current_liabilities": ("Current Liabilities", "Total Current Liabilities"),
    "ppe": ("Net PPE", "Net Property Plant And Equipment"),
    "receivables": ("Accounts Receivable", "Receivables", "Gross Accounts Receivable"),
    "cash": ("Cash And Cash Equivalents",
             "Cash Cash Equivalents And Short Term Investments"),
    "current_debt": ("Current Debt", "Current Debt And Capital Lease Obligation",
                     "Other Current Borrowings"),
    "long_term_debt": ("Long Term Debt", "Long Term Debt And Capital Lease Obligation"),
    "invested_capital": ("Invested Capital",),
    "cfo": ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
    "fcf": ("Free Cash Flow",),
    "capex": ("Capital Expenditure", "Purchase Of PPE"),
    "depreciation": ("Depreciation And Amortization", "Reconciled Depreciation",
                     "Depreciation Amortization Depletion"),
}


# --------------------------------------------------------------------------
# Frames and cell access
# --------------------------------------------------------------------------

def statement_frames(tk) -> dict:
    """The three annual statements for one ticker. The only I/O in this module.

    Fetched once per ticker and shared by every consumer, because the alternative
    -- each metric family calling `tk.income_stmt` for itself -- triples the
    Yahoo round trips for data that is identical every time. A statement that
    fails to load comes back as an empty frame, so every metric built on it
    resolves to None rather than raising.
    """
    out = {}
    for key, attr in (("income", "income_stmt"), ("cashflow", "cash_flow"),
                      ("balance", "balance_sheet")):
        try:
            df = getattr(tk, attr)
            out[key] = df if isinstance(df, pd.DataFrame) and not df.empty \
                else pd.DataFrame()
        except Exception as exc:  # noqa: BLE001 - a missing statement is data, not a crash
            log_step("YAHOO", "failed", f"{attr} for {getattr(tk, 'ticker', '?')}: {exc}")
            out[key] = pd.DataFrame()
    return out


def _is_num(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _first(frames: dict, which: str, row: str, column):
    """The first spelling of `row` that this statement actually publishes."""
    df = frames.get(which)
    if not isinstance(df, pd.DataFrame) or df.empty or column is None:
        return None
    for name in ROWS.get(row, (row,)):
        value = stmt_value(df, name, column)
        if _is_num(value):
            return float(value)
    return None


def columns(frames: dict, which: str, years: int | None = None) -> list:
    """The annual columns of one statement, oldest -> newest."""
    df = frames.get(which)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    cols = sorted(df.columns)
    return cols[-years:] if years else cols


def _aligned(frames: dict, years: int) -> list:
    """Columns present on **all three** statements, oldest -> newest.

    Every cross-statement ratio here (accruals, Altman, Beneish, ROIC) mixes
    rows from two or three of them, so a year only counts when all three report
    it. Comparing an income row against a balance column a year apart is the
    kind of error that produces a plausible number and no exception at all.
    """
    sets = [set(columns(frames, w)) for w in ("income", "cashflow", "balance")]
    common = sorted(set.intersection(*sets)) if all(sets) else []
    return common[-years:] if years else common


# --------------------------------------------------------------------------
# Small statistics -- deliberately not scipy/numpy-dependent
# --------------------------------------------------------------------------

def _mean(xs):
    xs = [x for x in xs if _is_num(x)]
    return sum(xs) / len(xs) if xs else None


def _stdev(xs):
    """Sample standard deviation; None below two points."""
    xs = [x for x in xs if _is_num(x)]
    if len(xs) < 2:
        return None
    mu = sum(xs) / len(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))


def stability(xs):
    """Mean over standard deviation -- higher is steadier. None below two points.

    The inverse coefficient of variation, which is what "stable margin" means
    operationally. Reported as an absolute value so a business with negative
    average margins is scored on its *variability*, not handed a sign flip; and
    `None` for a flat series, where the ratio would be infinite.
    """
    mu, sd = _mean(xs), _stdev(xs)
    if mu is None or sd is None or sd == 0:
        return None
    return abs(mu) / sd


def slope(values):
    """Ordinary least-squares slope per step over an evenly spaced series."""
    ys = [v for v in values if _is_num(v)]
    n = len(ys)
    if n < 2:
        return None
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def _ratio(num, den):
    """`num/den`, or None when either is missing or the denominator is zero."""
    if not (_is_num(num) and _is_num(den)) or den == 0:
        return None
    return num / den


def _flag(condition) -> int | None:
    """A veto-safe 0/1 flag. `None` propagates as "unknown", never as "clean".

    Returns `int`, never `bool`: `quality.scalar` rejects booleans, so a bool
    here would read as a missing value and could never trip a veto.
    """
    return None if condition is None else int(bool(condition))


# --------------------------------------------------------------------------
# Series over the aligned years
# --------------------------------------------------------------------------

def _series(frames: dict, which: str, row: str, cols: list) -> list:
    return [_first(frames, which, row, c) for c in cols]


def gross_margin_series(frames: dict, cols: list) -> list:
    """(Revenue - COGS) / Revenue as a percent, per year."""
    out = []
    for c in cols:
        rev = _first(frames, "income", "revenue", c)
        cogs = _first(frames, "income", "cogs", c)
        gm = _ratio(rev - cogs, rev) if _is_num(rev) and _is_num(cogs) else None
        out.append(100 * gm if gm is not None else None)
    return out


def operating_margin_series(frames: dict, cols: list) -> list:
    out = []
    for c in cols:
        ebit = _first(frames, "income", "ebit", c)
        rev = _first(frames, "income", "revenue", c)
        m = _ratio(ebit, rev)
        out.append(100 * m if m is not None else None)
    return out


def roic_series(frames: dict, cols: list) -> list:
    """After-tax return on invested capital, per year, as a percent.

    The same definition `quality._statement_metrics` uses for its scalar
    `roic` -- EBIT x (1 - effective tax rate) / invested capital -- evaluated
    for every year rather than only the latest, because persistence is the
    whole point of the moat reading.
    """
    out = []
    for c in cols:
        ebit = _first(frames, "income", "ebit", c)
        tax = _first(frames, "income", "tax", c)
        pretax = _first(frames, "income", "pretax", c)
        invested = _first(frames, "balance", "invested_capital", c)
        if None in (ebit, tax, pretax, invested) or not invested or pretax <= 0:
            out.append(None)
            continue
        out.append(100 * ebit * (1 - tax / pretax) / invested)
    return out


# --------------------------------------------------------------------------
# Distress
# --------------------------------------------------------------------------

def altman_z(frames: dict, info: dict, column=None):
    """The 1968 Altman Z for a public manufacturer. Below 1.8 is the distress zone.

    Z = 1.2*WC/TA + 1.4*RE/TA + 3.3*EBIT/TA + 0.6*MVE/TL + 1.0*Sales/TA

    Deliberately the original coefficients rather than a re-fit: the published
    cut-offs (1.8 distress, 3.0 safe) only mean anything against them. It is
    weakest exactly where it is famous for being weak -- banks, insurers and
    asset-light software all score low for structural reasons rather than
    distress -- which is why its veto threshold sits at the loose end and why
    `enabled: false` per sector is the intended escape hatch.
    """
    column = column or (columns(frames, "balance") or [None])[-1]
    total_assets = _first(frames, "balance", "assets", column)
    if not _is_num(total_assets) or total_assets == 0:
        return None

    working_capital = _first(frames, "balance", "working_capital", column)
    if working_capital is None:
        ca = _first(frames, "balance", "current_assets", column)
        cl = _first(frames, "balance", "current_liabilities", column)
        working_capital = ca - cl if _is_num(ca) and _is_num(cl) else None

    x1 = _ratio(working_capital, total_assets)
    x2 = _ratio(_first(frames, "balance", "retained", column), total_assets)
    x3 = _ratio(_first(frames, "income", "ebit", column), total_assets)
    x4 = _ratio(info.get("marketCap"),
                _first(frames, "balance", "liabilities", column))
    x5 = _ratio(_first(frames, "income", "revenue", column), total_assets)
    if None in (x1, x2, x3, x4, x5):
        return None
    return 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5


def beneish_m(frames: dict):
    """The eight-variable Beneish M-score. Above -1.78 flags likely manipulation.

    Needs two consecutive years on all three statements; returns None when any
    of the eight indices cannot be built, which is common and is the correct
    answer -- a partial M-score is not a weaker signal, it is a different
    statistic.

    **Its veto threshold in config is deliberately looser than -1.78.** Two of
    the eight indices, sales growth (SGI) and days-in-receivables (DSRI), rise
    with growth alone, so the score systematically flags fast-growing companies
    -- which is precisely the population a breakout screener surfaces. Measured
    here: NVDA scores -1.13 against -2.1 to -2.6 for MSFT, JNJ, KO, MCD and
    AVGO. Vetoing at the textbook cut-off would have excluded the single
    best-performing name in the index for growing quickly. The score anchors
    still penalise it; only the exclusion is held back for extreme readings.
    """
    cols = _aligned(frames, 2)
    if len(cols) < 2:
        return None
    prev, cur = cols[-2], cols[-1]

    def pair(which, row):
        return _first(frames, which, row, cur), _first(frames, which, row, prev)

    sales_t, sales_p = pair("income", "revenue")
    cogs_t, cogs_p = pair("income", "cogs")
    recv_t, recv_p = pair("balance", "receivables")
    ca_t, ca_p = pair("balance", "current_assets")
    ppe_t, ppe_p = pair("balance", "ppe")
    ta_t, ta_p = pair("balance", "assets")
    dep_t, dep_p = pair("cashflow", "depreciation")
    sga_t, sga_p = pair("income", "sga")
    cl_t, cl_p = pair("balance", "current_liabilities")
    ltd_t, ltd_p = pair("balance", "long_term_debt")
    ni_t = _first(frames, "income", "net_income_continuing", cur)
    cfo_t = _first(frames, "cashflow", "cfo", cur)

    # Days sales in receivables, gross margin, asset quality, sales growth.
    dsri = _ratio(_ratio(recv_t, sales_t), _ratio(recv_p, sales_p))
    gm_t = _ratio(sales_t - cogs_t, sales_t) if _is_num(sales_t) and _is_num(cogs_t) else None
    gm_p = _ratio(sales_p - cogs_p, sales_p) if _is_num(sales_p) and _is_num(cogs_p) else None
    gmi = _ratio(gm_p, gm_t)
    aq_t = 1 - _ratio(ca_t + ppe_t, ta_t) if _is_num(ca_t) and _is_num(ppe_t) else None
    aq_p = 1 - _ratio(ca_p + ppe_p, ta_p) if _is_num(ca_p) and _is_num(ppe_p) else None
    aqi = _ratio(aq_t, aq_p)
    sgi = _ratio(sales_t, sales_p)

    # Depreciation rate, SGA intensity, accruals, leverage.
    rate_t = _ratio(dep_t, dep_t + ppe_t) if _is_num(dep_t) and _is_num(ppe_t) else None
    rate_p = _ratio(dep_p, dep_p + ppe_p) if _is_num(dep_p) and _is_num(ppe_p) else None
    depi = _ratio(rate_p, rate_t)
    sgai = _ratio(_ratio(sga_t, sales_t), _ratio(sga_p, sales_p))
    tata = _ratio(ni_t - cfo_t, ta_t) if _is_num(ni_t) and _is_num(cfo_t) else None
    lev_t = _ratio(cl_t + ltd_t, ta_t) if _is_num(cl_t) and _is_num(ltd_t) else None
    lev_p = _ratio(cl_p + ltd_p, ta_p) if _is_num(cl_p) and _is_num(ltd_p) else None
    lvgi = _ratio(lev_t, lev_p)

    parts = (dsri, gmi, aqi, sgi, depi, sgai, tata, lvgi)
    if any(p is None for p in parts):
        return None
    return (-4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
            + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)


def _trailing_divergence(net_income: list, cfo: list) -> int:
    """Consecutive most-recent years reporting profit on negative cash flow.

    The run has to be **trailing and unbroken**, not a count of occurrences
    across the window. Banks and insurers throw off scattered negative-CFO
    years for entirely structural reasons -- loan origination and trading-book
    swings run through operating cash flow -- so counting occurrences vetoes
    JPM and most of the financial sector on a healthy balance sheet. A profit
    that has failed to convert to cash *for consecutive years, up to and
    including the latest one* is the pattern actually worth excluding.
    """
    run = 0
    for ni, cf in zip(reversed(net_income), reversed(cfo)):
        if _is_num(ni) and _is_num(cf) and ni > 0 and cf < 0:
            run += 1
        else:
            break
    return run


def distress_metrics(frames: dict, info: dict, years: int = 5) -> dict:
    """Every `distress.*` value for one ticker, keyed by parameter key."""
    info = info or {}
    cols = _aligned(frames, years)
    latest = cols[-1] if cols else None

    equity = _first(frames, "balance", "equity", latest)
    fcf_values = _series(frames, "cashflow", "fcf", cols)
    latest_fcf = fcf_values[-1] if fcf_values else None

    ebit = _first(frames, "income", "ebit", latest)
    interest = _first(frames, "income", "interest", latest)
    coverage = _ratio(ebit, abs(interest)) if _is_num(interest) and interest != 0 else None

    cash = _first(frames, "balance", "cash", latest)
    short_debt = _first(frames, "balance", "current_debt", latest)

    # Runway only means anything while the company is actually burning; a
    # cash-generative business would otherwise score an infinite (or negative)
    # number of quarters and land wherever the anchors happen to clamp it.
    runway = None
    if _is_num(latest_fcf) and latest_fcf < 0 and _is_num(cash):
        runway = cash / (abs(latest_fcf) / 4)

    net_income = _series(frames, "income", "net_income", cols)
    cfo = _series(frames, "cashflow", "cfo", cols)
    divergence = _trailing_divergence(net_income, cfo)

    accruals = None
    if cols:
        ta = _first(frames, "balance", "assets", latest)
        if _is_num(net_income[-1]) and _is_num(cfo[-1]):
            accruals = _ratio(net_income[-1] - cfo[-1], ta)

    burned = [f for f in fcf_values if _is_num(f)]
    short_now, short_prior = info.get("sharesShort"), info.get("sharesShortPriorMonth")
    short_change = _ratio(short_now - short_prior, short_prior) \
        if _is_num(short_now) and _is_num(short_prior) else None

    return {
        "negative_equity": _flag(equity < 0) if _is_num(equity) else None,
        "negative_equity_burn": (
            _flag(equity < 0 and latest_fcf < 0)
            if _is_num(equity) and _is_num(latest_fcf) else None),
        "interest_coverage": coverage,
        "short_term_debt_to_cash": _ratio(short_debt, cash),
        "altman_z": altman_z(frames, info, latest),
        "fcf_negative_years": float(sum(1 for f in burned if f < 0)) if burned else None,
        "cash_runway_quarters": runway,
        "accruals_ratio": accruals,
        "earnings_cash_divergence_years": float(divergence) if net_income and cfo else None,
        "beneish_m": beneish_m(frames),
        "short_percent_float": (100 * info["shortPercentOfFloat"]
                                if _is_num(info.get("shortPercentOfFloat")) else None),
        "short_interest_change_pct": 100 * short_change if short_change is not None else None,
        "short_ratio": info.get("shortRatio") if _is_num(info.get("shortRatio")) else None,
    }


# --------------------------------------------------------------------------
# Moat
# --------------------------------------------------------------------------

def moat_metrics(frames: dict, info: dict, years: int = 5,
                 roic_hurdle_pct: float = 12.0) -> dict:
    """Every `moat.*` value for one ticker, keyed by parameter key.

    The unifying idea is **persistence**: a moat is not a good year, it is the
    inability of competitors to take the good years away. So most of these read
    a multi-year series and report how consistent it was, not how high its last
    point happened to be.
    """
    info = info or {}
    cols = _aligned(frames, years)
    inc_cols = columns(frames, "income", years)

    roic = [r for r in roic_series(frames, cols) if _is_num(r)]
    gross = gross_margin_series(frames, inc_cols)
    operating = operating_margin_series(frames, inc_cols)
    revenue = [r for r in _series(frames, "income", "revenue", inc_cols) if _is_num(r)]

    growth_years = sum(1 for a, b in zip(revenue, revenue[1:]) if b > a)
    cagr = None
    if len(revenue) >= 2 and revenue[0] > 0 and revenue[-1] > 0:
        cagr = 100 * ((revenue[-1] / revenue[0]) ** (1 / (len(revenue) - 1)) - 1)

    fcf = _series(frames, "cashflow", "fcf", cols)
    net_income = _series(frames, "income", "net_income", cols)
    rev_aligned = _series(frames, "income", "revenue", cols)
    capex = _series(frames, "cashflow", "capex", cols)

    conversion = _mean([_ratio(f, n) for f, n in zip(fcf, net_income)
                        if _is_num(f) and _is_num(n) and n > 0])
    fcf_margin = _mean([_ratio(f, r) for f, r in zip(fcf, rev_aligned)
                        if _is_num(f) and _is_num(r) and r > 0])
    intensity = _mean([_ratio(abs(c), r) for c, r in zip(capex, rev_aligned)
                       if _is_num(c) and _is_num(r) and r > 0])

    # Incremental ROIC: what the *marginal* capital earned over the window, which
    # is the reinvestment question a headline ROIC cannot answer. Only meaningful
    # when the invested-capital base actually grew.
    incremental = None
    if len(cols) >= 2:
        ebit_first = _first(frames, "income", "ebit", cols[0])
        ebit_last = _first(frames, "income", "ebit", cols[-1])
        ic_first = _first(frames, "balance", "invested_capital", cols[0])
        ic_last = _first(frames, "balance", "invested_capital", cols[-1])
        if all(_is_num(v) for v in (ebit_first, ebit_last, ic_first, ic_last)) \
                and ic_last > ic_first:
            incremental = 100 * (ebit_last - ebit_first) / (ic_last - ic_first)

    return {
        "roic_years_above": float(sum(1 for r in roic if r > roic_hurdle_pct)) if roic else None,
        "roic_stability": stability(roic),
        "gross_margin_slope": slope(gross),
        "operating_margin_stability": stability(operating),
        "revenue_growth_years": float(growth_years) if len(revenue) >= 2 else None,
        "revenue_cagr_5y": cagr,
        "fcf_conversion": conversion,
        "fcf_margin": 100 * fcf_margin if fcf_margin is not None else None,
        "capex_intensity": 100 * intensity if intensity is not None else None,
        "incremental_roic": incremental,
    }
