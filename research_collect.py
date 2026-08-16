"""
Investment-case data collection -- Tier A (Yahoo / yfinance).

Pure collection, no rendering: `collect_yahoo(ticker)` gathers everything
useful yfinance exposes for one ticker into a plain nested dict (JSON-friendly
scalars / lists), grouped by the job each field does in an investment case:
valuation-in-context, forward estimates + revision trend, analyst view,
earnings cadence & quality, balance-sheet durability, ownership & insiders,
recent news, and profile.

This is deliberately separate from `scanner_common.fetch_fundamentals` (which
feeds the live nightly Discord embed) so the working alert stays untouched
while the richer research layer is built and validated. Nothing is rendered
here and nothing is hardcoded that belongs in config -- synthesis/formatting
is a later step.

Tier B (IBKR company graph / market stats / account) is *not* here: it is
reachable only through the claude.ai MCP connector in an interactive session,
never from this unattended-capable Python module.

Run standalone to eyeball / validate collection:
    python research_collect.py MSFT JNJ JPM
"""

import json
import sys
import time

import pandas as pd
import yfinance as yf

from scanner_common import log_step, stmt_value


# --------------------------------------------------------------------------
# n/a-tolerant scalar helpers (a missing value never raises -- it becomes None)
# --------------------------------------------------------------------------

def _num(value):
    """Return a plain float for a real number, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and pd.notna(value):
        return float(value)
    return None


def _pct(value, scale=100.0):
    """Yahoo fraction (0.1616) -> percent (16.16), else None."""
    v = _num(value)
    return v * scale if v is not None else None


def _get(info: dict, key: str):
    return _num(info.get(key))


def _df(obj):
    """Only a non-empty DataFrame passes through; anything else -> None."""
    return obj if isinstance(obj, pd.DataFrame) and not obj.empty else None


# --------------------------------------------------------------------------
# Per-group collectors -- each is self-contained and swallows its own errors
# so one bad group never aborts the rest of the ticker.
# --------------------------------------------------------------------------

def _profile(info: dict) -> dict:
    return {
        "company": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "employees": info.get("fullTimeEmployees"),
        "country": info.get("country"),
        "summary": info.get("longBusinessSummary"),
    }


def ttm_eps_series(tk: yf.Ticker, index: pd.DatetimeIndex):
    """Daily trailing-twelve-month EPS aligned to `index`, from reported
    quarterly EPS (rolling 4). None if not reconstructable.

    Public because `industry_valuation.py` reconstructs the same P/E path at
    industry scale, and one definition of "what were this company's trailing
    earnings on day d" is what keeps the per-ticker `pe_percentile_2y` and the
    industry median comparable -- the same single-source-of-truth rule the
    `compute_*` screen functions follow.

    Two properties are load-bearing:
      * the index is the earnings **announcement** date, so a forward-fill onto
        a price index never uses an EPS figure before it was public;
      * fewer than 4 quarters returns None, never a partial sum -- a
        three-quarter "TTM" is a wrong number, not a small one.
    """
    return ttm_from_quarterly(quarterly_eps(tk), index)


def quarterly_eps(tk: yf.Ticker):
    """Reported quarterly EPS, ascending, indexed by **announcement date**.

    Split from the TTM roll so a caller can cache the expensive half. This is
    the only network call in the pair; `ttm_from_quarterly` is pure arithmetic
    over its result, which is what lets `industry_valuation.py` cache the
    quarterly points and rebuild a daily path from a fresh price panel for free.
    """
    ed = _df(getattr(tk, "earnings_dates", None))
    if ed is None or "Reported EPS" not in ed.columns:
        return None
    eps = ed["Reported EPS"].dropna()
    if eps.empty:
        return None
    eps = eps.sort_index()
    eps.index = pd.DatetimeIndex(eps.index).tz_localize(None).normalize()
    return eps


def ttm_from_quarterly(eps, index: pd.DatetimeIndex):
    """Roll quarterly EPS to TTM and forward-fill onto `index`. None if thin.

    The forward-fill is what makes this point-in-time: `eps` is indexed by the
    date each figure was *announced*, so a value can only ever propagate
    forward, never onto a bar that preceded its publication.
    """
    if eps is None or len(eps) < 4:
        return None
    ttm = eps.rolling(4).sum().dropna()
    if ttm.empty:
        return None
    # Two announcements can share a date -- a company reporting two quarters
    # together after a delay, or Yahoo simply serving a duplicate row. The
    # rolling sum above is positional so every TTM value is still right; it is
    # `reindex` that refuses a duplicated axis. Keep the LAST reading for the
    # date, which is the trailing-twelve-month figure as of that day once both
    # quarters are in. Dropping duplicates *before* the roll would instead lose
    # a quarter out of the sum.
    ttm = ttm[~ttm.index.duplicated(keep="last")]
    return ttm.reindex(index, method="ffill")


def _valuation(info: dict, tk: yf.Ticker, close: pd.Series | None) -> dict:
    out = {
        "trailingPE": _get(info, "trailingPE"),
        "forwardPE": _get(info, "forwardPE"),
        "priceToSales": _get(info, "priceToSalesTrailing12Months"),
        "priceToBook": _get(info, "priceToBook"),
        "evToEbitda": _get(info, "enterpriseToEbitda"),
        "evToRevenue": _get(info, "enterpriseToRevenue"),
        "enterpriseValue": _get(info, "enterpriseValue"),
        "marketCap": _get(info, "marketCap"),
        "price_percentile_2y": None,
        "pe_percentile_2y": None,
        "pe_2y_low": None,
        "pe_2y_high": None,
    }
    if close is None or close.dropna().empty:
        return out
    px = close.dropna()
    px.index = pd.DatetimeIndex(px.index).tz_localize(None).normalize()
    last = float(px.iloc[-1])
    lo, hi = float(px.min()), float(px.max())
    if hi > lo:
        out["price_percentile_2y"] = round(100 * (last - lo) / (hi - lo), 1)
    try:
        ttm = ttm_eps_series(tk, px.index)
        if ttm is not None:
            pe = (px / ttm).replace([float("inf"), float("-inf")], pd.NA).dropna()
            pe = pe[pe > 0]
            if len(pe) > 20:
                cur = float(pe.iloc[-1])
                out["pe_2y_low"] = round(float(pe.min()), 1)
                out["pe_2y_high"] = round(float(pe.max()), 1)
                out["pe_percentile_2y"] = round(100 * (pe < cur).mean(), 1)
    except Exception as exc:  # noqa: BLE001
        log_step("YAHOO", "failed", f"pe_percentile: {exc}")
    return out


def _estimates(tk: yf.Ticker) -> dict:
    """Forward EPS/revenue consensus, growth, and the estimate-revision trend
    (whether analysts are marking numbers up or down)."""
    def records(attr):
        d = _df(getattr(tk, attr, None))
        if d is None:
            return None
        return {str(period): {k: _num(v) if not isinstance(v, str) else v
                              for k, v in row.items()}
                for period, row in d.to_dict("index").items()}

    return {
        "eps": records("earnings_estimate"),
        "revenue": records("revenue_estimate"),
        "growth": records("growth_estimates"),
        "eps_trend": records("eps_trend"),
        "eps_revisions": records("eps_revisions"),
    }


def _analyst(tk: yf.Ticker, info: dict) -> dict:
    targets = getattr(tk, "analyst_price_targets", None)
    targets = targets if isinstance(targets, dict) else {}
    mean = _num(targets.get("mean"))
    cur = _num(targets.get("current")) or _get(info, "currentPrice")
    upside = round(100 * (mean - cur) / cur, 1) if mean and cur else None

    recs = None
    rs = _df(getattr(tk, "recommendations_summary", None))
    if rs is not None:
        recs = [{k: (_num(v) if k != "period" else v) for k, v in row.items()}
                for row in rs.to_dict("records")]

    actions = None
    ud = _df(getattr(tk, "upgrades_downgrades", None))
    if ud is not None:
        recent = ud.sort_index().tail(6)
        actions = [{
            "date": str(idx.date()) if hasattr(idx, "date") else str(idx),
            "firm": row.get("Firm"),
            "action": row.get("Action"),
            "from": row.get("FromGrade"),
            "to": row.get("ToGrade"),
            "target": _num(row.get("currentPriceTarget")),
        } for idx, row in recent.iterrows()]

    return {
        "targets": {"current": cur, "mean": mean,
                    "low": _num(targets.get("low")), "high": _num(targets.get("high")),
                    "upside_pct": upside},
        "num_analysts": info.get("numberOfAnalystOpinions"),
        "recommendation_key": info.get("recommendationKey"),
        "recommendations": recs,
        "recent_actions": actions,
    }


def _earnings(tk: yf.Ticker) -> dict:
    """Next earnings date (a 'reports in N days' guard) + the beat/miss
    surprise history."""
    next_date, days_to = None, None
    cal = getattr(tk, "calendar", None)
    if isinstance(cal, dict):
        dates = cal.get("Earnings Date")
        if isinstance(dates, list) and dates:
            next_date = dates[0]
        elif dates:
            next_date = dates
    if next_date is not None:
        try:
            nd = pd.Timestamp(next_date).tz_localize(None).normalize()
            days_to = int((nd - pd.Timestamp.now().normalize()).days)
            next_date = str(nd.date())
        except Exception:  # noqa: BLE001
            next_date = str(next_date)

    history = None
    eh = _df(getattr(tk, "earnings_history", None))
    if eh is not None:
        history = [{
            "quarter": str(idx.date()) if hasattr(idx, "date") else str(idx),
            "eps_actual": _num(row.get("epsActual")),
            "eps_estimate": _num(row.get("epsEstimate")),
            "surprise_pct": _pct(row.get("surprisePercent")),
        } for idx, row in eh.sort_index().tail(8).iterrows()]

    return {"next_date": next_date, "days_to_next": days_to,
            "surprise_history": history}


def _quality(info: dict) -> dict:
    """Balance-sheet durability ratios (extends the ROE/ROIC/FCF/margins the
    live alert already computes)."""
    ebitda = _get(info, "ebitda")
    total_debt = _get(info, "totalDebt")
    total_cash = _get(info, "totalCash")
    net_debt_to_ebitda = None
    if ebitda and total_debt is not None and total_cash is not None:
        net_debt_to_ebitda = round((total_debt - total_cash) / ebitda, 2)
    return {
        "returnOnAssets_pct": _pct(info.get("returnOnAssets")),
        "grossMargins_pct": _pct(info.get("grossMargins")),
        "currentRatio": _get(info, "currentRatio"),
        "quickRatio": _get(info, "quickRatio"),
        "debtToEquity": _get(info, "debtToEquity"),
        "netDebtToEbitda": net_debt_to_ebitda,
    }


def _financials(tk: yf.Ticker, years: int = 4, quarters: int = 4) -> dict:
    """Annual and quarterly history of the five trend metrics, oldest -> newest.

    Revenue, earnings, margin, free cash flow and leverage -- the shape of the
    business over time, which none of the other collectors carry (they are all
    snapshots or forward estimates). Feeds `charts.plot_financials` and the
    report's trend table.

    **Margin is issuer-dependent and says so.** Yahoo reports no Operating
    Income for banks or insurers (verified absent for JPM and PGR, present for
    MSFT), and Gross Profit is missing for exactly the same issuers -- so the
    margin falls back to Pretax Income / Revenue and records `margin_kind` for
    the label. This fallback lives *here only*: `scanner_common` keeps the
    strict Operating-Income definition, because that one feeds the quality
    rules and changing it would move the tier-2 badge for every financial.
    """
    def frame(name: str) -> pd.DataFrame:
        try:
            df = getattr(tk, name)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
        except Exception as exc:  # noqa: BLE001 - a missing statement is not fatal
            log_step("YAHOO", "failed", f"{name} for {tk.ticker}: {exc}")
        return pd.DataFrame()

    def series(income, cashflow, balance, count, label) -> list[dict]:
        # Union of the three frames, because they don't always publish the same
        # periods -- but *filter before slicing*: Yahoo can carry a newest
        # column with no revenue yet (seen on JPM's quarterlies), and letting
        # that empty period take one of the `count` slots costs a real one.
        # Oldest -> newest so the chart reads left to right.
        cols = sorted(set(income.columns) | set(cashflow.columns)
                      | set(balance.columns))
        rows = []
        for c in cols:
            revenue = (stmt_value(income, "Total Revenue", c)
                       or stmt_value(income, "Operating Revenue", c))
            operating = stmt_value(income, "Operating Income", c)
            pretax = stmt_value(income, "Pretax Income", c)
            numerator, kind = ((operating, "operating") if operating is not None
                               else (pretax, "pretax"))
            equity = stmt_value(balance, "Stockholders Equity", c)
            debt = stmt_value(balance, "Total Debt", c)
            rows.append({
                "period": label(c),
                "end": str(c.date()),
                "revenue": revenue,
                "earnings": (stmt_value(income, "Net Income", c)
                             or stmt_value(income, "Net Income Common Stockholders", c)),
                "margin_pct": (100 * numerator / revenue
                               if numerator is not None and revenue else None),
                "margin_kind": kind if numerator is not None else None,
                "fcf": stmt_value(cashflow, "Free Cash Flow", c),
                "debt_to_equity": (100 * debt / equity
                                   if debt is not None and equity else None),
            })
        usable = [r for r in rows if r["revenue"] is not None]
        return usable[-count:]

    return {
        "annual": series(frame("income_stmt"), frame("cash_flow"),
                         frame("balance_sheet"), years,
                         lambda c: f"FY{c.year % 100:02d}"),
        "quarterly": series(frame("quarterly_income_stmt"),
                            frame("quarterly_cash_flow"),
                            frame("quarterly_balance_sheet"), quarters,
                            lambda c: f"Q{(c.month - 1) // 3 + 1} {c.year % 100:02d}"),
    }


def _split_factor(tk: yf.Ticker, since) -> float:
    """Cumulative split ratio applied since `since`, or 1.0 when there was none.

    Multiplying an old share count by this restates it onto today's basis, so a
    2-for-1 stops reading as 100% dilution. Returns 1.0 on any failure -- an
    unadjusted comparison is the behaviour that already existed, and losing the
    metric entirely would be worse than losing the adjustment.
    """
    try:
        splits = getattr(tk, "splits", None)
        if not isinstance(splits, pd.Series) or splits.empty:
            return 1.0
        since = pd.Timestamp(since)
        index = splits.index
        if getattr(index, "tz", None) is not None:
            since = since.tz_localize(index.tz) if since.tzinfo is None \
                else since.tz_convert(index.tz)
        recent = splits[index > since].dropna()
        factor = 1.0
        for ratio in recent:
            if isinstance(ratio, (int, float)) and ratio > 0:
                factor *= float(ratio)
        return factor or 1.0
    except Exception as exc:  # noqa: BLE001 - a missing split history is not an error
        log_step("YAHOO", "failed", f"splits: {exc}")
        return 1.0


def _ownership(tk: yf.Ticker, info: dict) -> dict:
    top = None
    ih = _df(getattr(tk, "institutional_holders", None))
    if ih is not None and "Holder" in ih.columns:
        top = [{"holder": row.get("Holder"), "pct": _pct(row.get("pctHeld"))}
               for row in ih.head(5).to_dict("records")]

    insider_net_6m = None
    ip = _df(getattr(tk, "insider_purchases", None))
    if ip is not None:
        col = ip.columns[0]
        for row in ip.to_dict("records"):
            if str(row.get(col, "")).startswith("Net Shares"):
                insider_net_6m = _num(row.get("Shares"))
                break

    # Buyback: net change in share count over the available window.
    #
    # `get_shares_full` reports **raw** counts, so a stock split doubles the
    # series and reads as 100% dilution. Fastenal's 2025 2-for-1 measured
    # +100.4% against an actual buyback. That corrupts the `buyback_2y` score
    # for every splitting company -- and splits cluster in exactly the names a
    # breakout screener surfaces -- so the earlier count is restated onto
    # today's share basis before the comparison.
    shares_change_pct = None
    try:
        start = (pd.Timestamp.now() - pd.Timedelta(days=730)).date().isoformat()
        sf = tk.get_shares_full(start=start)
        if isinstance(sf, pd.Series) and len(sf.dropna()) >= 2:
            s = sf.dropna()
            first, latest = float(s.iloc[0]), float(s.iloc[-1])
            factor = _split_factor(tk, s.index[0])
            if first and factor:
                first *= factor
                shares_change_pct = round(100 * (latest - first) / first, 2)
    except Exception as exc:  # noqa: BLE001
        log_step("YAHOO", "failed", f"shares_full: {exc}")

    return {
        "institutions_pct": _pct(info.get("heldPercentInstitutions")),
        "insiders_pct": _pct(info.get("heldPercentInsiders")),
        "top_holders": top,
        "insider_net_shares_6m": insider_net_6m,
        "shares_change_2y_pct": shares_change_pct,
    }


def _news(tk: yf.Ticker, limit: int = 8) -> list:
    items = getattr(tk, "news", None)
    if not isinstance(items, list):
        return []
    out = []
    for item in items[:limit]:
        c = item.get("content", item) if isinstance(item, dict) else {}
        provider = c.get("provider") or {}
        url = c.get("canonicalUrl") or c.get("clickThroughUrl") or {}
        out.append({
            "title": c.get("title"),
            "publisher": provider.get("displayName") if isinstance(provider, dict) else None,
            "published": c.get("pubDate") or c.get("displayTime"),
            "link": url.get("url") if isinstance(url, dict) else None,
        })
    return out


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def collect_yahoo(ticker: str, close: pd.Series | None = None,
                  years: int = 4, quarters: int = 4) -> dict:
    """Gather every Tier-A (Yahoo) group for one ticker into a plain dict.

    `close` is an optional 2y daily close Series (the nightly pipeline already
    holds it -- pass it to avoid a redundant download and to keep the P/E
    percentile on the exact same series the screens use). If omitted, a 2y
    history is fetched here. `years`/`quarters` size the financials history.
    """
    t0 = time.perf_counter()
    tk = yf.Ticker(ticker)
    try:
        info = tk.info or {}
    except Exception as exc:  # noqa: BLE001
        log_step("YAHOO", "failed", f"info for {ticker}: {exc}")
        info = {}

    if close is None:
        try:
            hist = tk.history(period="2y", auto_adjust=False)
            close = hist["Close"] if isinstance(hist, pd.DataFrame) and not hist.empty else None
        except Exception as exc:  # noqa: BLE001
            log_step("YAHOO", "failed", f"history for {ticker}: {exc}")
            close = None

    groups = {
        "profile": (_profile, (info,)),
        "valuation": (_valuation, (info, tk, close)),
        "estimates": (_estimates, (tk,)),
        "analyst": (_analyst, (tk, info)),
        "earnings": (_earnings, (tk,)),
        "quality": (_quality, (info,)),
        "financials": (_financials, (tk, years, quarters)),
        "ownership": (_ownership, (tk, info)),
    }
    out = {"ticker": ticker}
    failed = []
    for name, (fn, args) in groups.items():
        try:
            out[name] = fn(*args)
        except Exception as exc:  # noqa: BLE001 - one bad group must not sink the ticker
            log_step("YAHOO", "failed", f"group '{name}' for {ticker}: {exc}")
            out[name] = None
            failed.append(name)
    try:
        out["news"] = _news(tk)
    except Exception as exc:  # noqa: BLE001
        log_step("YAHOO", "failed", f"group 'news' for {ticker}: {exc}")
        out["news"] = []
        failed.append("news")
    total = len(groups) + 1
    # One summary line whether or not anything broke: "9/9 groups" is the
    # evidence that a thin report had thin *inputs*, not a thin analysis.
    log_step("YAHOO", "ok" if not failed else "partial",
             f"{total - len(failed)}/{total} groups for {ticker}"
             + (f" -- missing {', '.join(failed)}" if failed else ""),
             ms=(time.perf_counter() - t0) * 1000)
    return out


def _default(o):
    if isinstance(o, (pd.Timestamp,)):
        return str(o)
    return str(o)


if __name__ == "__main__":
    tickers = sys.argv[1:] or ["MSFT"]
    for t in tickers:
        print(f"\n{'#' * 72}\n# {t}\n{'#' * 72}")
        data = collect_yahoo(t)
        print(json.dumps(data, indent=2, default=_default, ensure_ascii=False))
