"""Interactive Brokers as a Python source, over the TWS API.

Until now IBKR reached this project only through the claude.ai MCP connector,
which meant it was available to a *model in a session* and to nothing else --
no nightly job, no backtest, no deterministic score. This module is the other
half: `ib_async` against a locally running IB Gateway or TWS, so IBKR data can
feed the same config-driven quality registry Yahoo feeds.

**Retail IBKR has no headless API.** OAuth 1.0a is institutional-only, so the
socket API needs IB Gateway logged in on this machine. Everything here is
therefore *optional by construction*: `available()` answers False when the
gateway is down, every getter returns an empty dict, and the quality engine
reads that as missing values -- exactly how it already tolerates a Yahoo field
Yahoo does not publish. A night with the gateway logged out produces a complete
alert with the IBKR parameters showing `n/a`, and one `IBKR skip` step line so
the thinner report is visibly thinner rather than quietly wrong.

**No account data, ever -- the user's real book is out of scope.**
`get_account_*` / `get_pa_*` were excluded from the MCP allow-lists on purpose:
a deep-dive grades the *security* and tier 4 grades the signal against a
fixed-notional virtual ledger, so what is already held changes neither, and
reports must not mention holdings, position size or concentration. That
prohibition does not weaken because the transport changed.
The functions simply do not exist here -- see `FORBIDDEN_CALLS` and the test
that pins it. An instruction a model can talk itself past is weaker than a
capability that was never written.

What IBKR adds that Yahoo does not: an **independent** ratio set (Refinitiv,
via `reqFundamentalData`) for the numbers the score already leans on, plus
option-implied volatility. What it cannot supply, and what the MCP connector
still uniquely had: the qualitative moat / competitor / theme graph -- those are
Reflexivity products with no public-API equivalent.

Setup:
    pip install ib_async
    # then run IB Gateway (or TWS) and enable:
    #   Configure > API > Settings > Enable ActiveX and Socket Clients
    # Live port is 7496 (TWS) / 4001 (Gateway); paper is 7497 / 4002.
"""

import asyncio
import threading
import time
import xml.etree.ElementTree as ET

from scanner_common import log_step

CONFIG_KEY = "ibkr"

# Documented, not implemented. Listed so the prohibition is greppable and so a
# test can assert this module never grew one of them.
FORBIDDEN_CALLS = (
    "reqAccountSummary", "reqAccountUpdates", "accountValues", "accountSummary",
    "reqPositions", "positions", "portfolio", "reqPnL", "reqPnLSingle",
    "reqCompletedOrders", "trades", "fills", "reqExecutions",
)

# Refinitiv ratio field names in a ReportSnapshot, mapped to the names the
# `quality.parameters[*].source` strings use. Everything else in the report is
# still returned under its raw field name, so adding a parameter is a config
# change rather than a code change.
RATIO_FIELDS = {
    "TTMROEPCT": "return_on_equity",
    "TTMROIPCT": "return_on_investment",
    "TTMREVCHG": "revenue_growth_rate",
    "TTMEPSCHG": "eps_growth_rate",
    "TTMGROSMGN": "gross_margin",
    "TTMOPMGN": "operating_margin",
    "TTMNPMGN": "net_margin",
    "QTOTD2EQ": "debt_to_equity",
    "PEEXCLXOR": "price_earnings",
    "PRICE2BK": "price_to_book",
    "YIELD": "dividend_yield",
    "TTMPAYRAT": "payout_ratio",
    "MKTCAP": "market_cap",
    "NPRICE": "price",
    "TTMREV": "revenue_ttm",
    "TTMEPSXCLX": "eps_ttm",
}

# Whether the gateway answered, cached for the life of the process: a nightly
# run asks once per ticker and a refused TCP connect costs the full timeout
# each time.
_AVAILABLE: bool | None = None


def _section(cfg: dict) -> dict:
    return cfg.get(CONFIG_KEY) or {}


def is_enabled(cfg: dict) -> bool:
    return bool(_section(cfg).get("enabled"))


def reset_cache() -> None:
    """Forget whether the gateway answered. For tests and long-lived servers."""
    global _AVAILABLE
    _AVAILABLE = None


# --------------------------------------------------------------------------
# Running async code from this synchronous codebase
# --------------------------------------------------------------------------

def _run(factory, timeout: float):
    """Run one coroutine on a private event loop in a private thread.

    `ib_async`'s synchronous wrappers drive an event loop in the *calling*
    thread and patch asyncio to allow re-entry. This codebase is synchronous,
    but the MCP server is not: FastMCP runs tool bodies on anyio worker threads
    while its own loop is live, and a global asyncio patch applied under it is
    exactly the kind of action-at-a-distance that fails once, mysteriously, in
    production. Owning a loop nobody else can see costs one thread per call and
    removes the whole class of problem.
    """
    box = {}

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            box["value"] = loop.run_until_complete(factory())
        except BaseException as exc:            # noqa: BLE001 - relayed below
            box["error"] = exc
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except BaseException:               # noqa: BLE001,S110 - closing anyway
                pass
            asyncio.set_event_loop(None)
            loop.close()

    thread = threading.Thread(target=worker, name="ibkr", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"IBKR call did not finish within {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


async def _connected(cfg: dict):
    """An `IB` connected to the configured gateway. Caller must disconnect."""
    from ib_async import IB

    sect = _section(cfg)
    ib = IB()
    await ib.connectAsync(
        sect.get("host", "127.0.0.1"),
        int(sect.get("port", 7496)),
        clientId=int(sect.get("client_id", 17)),
        timeout=float(sect.get("connect_timeout_seconds", 8)),
        readonly=True,      # belt and braces: this module never places an order
    )
    return ib


def _with_session(cfg: dict, body, extra_seconds: float = 0.0):
    """Open one connection, run `body(ib)`, always disconnect.

    One connection per call rather than a pooled one: the nightly run touches a
    handful of tickers, and a socket held open across a multi-hour job is a
    liability the saved handshake does not pay for.
    """
    sect = _section(cfg)
    timeout = (float(sect.get("connect_timeout_seconds", 8))
               + float(sect.get("request_timeout_seconds", 20))
               + extra_seconds)

    async def run():
        ib = await _connected(cfg)
        try:
            return await body(ib)
        finally:
            ib.disconnect()

    return _run(run, timeout + 5)


def available(cfg: dict) -> bool:
    """Whether IB Gateway/TWS is reachable. Cached, and never raises."""
    global _AVAILABLE
    if not is_enabled(cfg):
        return False
    if _AVAILABLE is not None:
        return _AVAILABLE

    try:
        import ib_async  # noqa: F401
    except ImportError:
        log_step("IBKR", "skip", "ib_async is not installed "
                                 "-- `pip install ib_async`", cfg=cfg)
        _AVAILABLE = False
        return False

    async def ping(ib):
        return ib.isConnected()

    try:
        _AVAILABLE = bool(_with_session(cfg, ping))
        log_step("IBKR", "ok" if _AVAILABLE else "skip",
                 f"gateway {_section(cfg).get('host')}:{_section(cfg).get('port')}"
                 + ("" if _AVAILABLE else " did not answer"), cfg=cfg)
    except Exception as exc:  # noqa: BLE001 - a dead gateway is a skip, not a failure
        log_step("IBKR", "skip", f"gateway unreachable: {exc}", cfg=cfg)
        _AVAILABLE = False
    return _AVAILABLE


# --------------------------------------------------------------------------
# Contract resolution
# --------------------------------------------------------------------------

async def _resolve(ib, ticker: str):
    """The US primary listing for a symbol, or None.

    Qualifying is what turns `AAPL` into a conId; without it every later call
    silently addresses the wrong instrument for a symbol that exists on several
    exchanges. `SMART`/`USD` plus `primaryExchange` is the same exact-symbol,
    US-primary rule the MCP path used with `search_contracts`.
    """
    from ib_async import Stock

    for primary in ("NASDAQ", "NYSE", "ARCA", "AMEX"):
        contract = Stock(ticker.replace("-", " "), "SMART", "USD",
                         primaryExchange=primary)
        try:
            details = await ib.reqContractDetailsAsync(contract)
        except Exception:  # noqa: BLE001 - try the next exchange
            continue
        for detail in details or []:
            if detail.contract.symbol.upper() == ticker.replace("-", " ").upper():
                return detail.contract
    return None


# --------------------------------------------------------------------------
# Fundamentals -- Refinitiv reports over reqFundamentalData
# --------------------------------------------------------------------------

def _number(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    # Refinitiv uses -99999 as its "not reported" sentinel; left alone it reads
    # as a real, catastrophically bad number in every score that touches it.
    return None if value <= -99998 else value


def parse_ratios(xml_text: str) -> dict:
    """Flatten a ReportSnapshot's `<Ratio>` elements to `{name: number}`.

    Returns both the friendly names in `RATIO_FIELDS` and every raw field name,
    so a parameter can be pointed at a ratio this module has never heard of
    without touching Python.
    """
    if not xml_text:
        return {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    out = {}
    for ratio in root.iter("Ratio"):
        name = ratio.get("FieldName")
        value = _number((ratio.text or "").strip())
        if not name or value is None:
            continue
        out[name] = value
        if name in RATIO_FIELDS:
            out[RATIO_FIELDS[name]] = value
    return out


def fundamentals(ticker: str, cfg: dict) -> dict:
    """The configured Refinitiv reports for one ticker, flattened.

    Empty when IBKR is off, unreachable, or has no coverage for the symbol --
    all three are ordinary, none of them raise.
    """
    if not available(cfg):
        return {}
    # An explicitly empty list means "do not ask" -- `or` would treat it as
    # unset and fall back to the default, which is how a config that had
    # already been told the account cannot serve reports still paid a failed
    # round-trip (and an error 10358 in the log) for every ticker.
    reports = _section(cfg).get("reports")
    if reports is None:
        reports = ["ReportSnapshot"]
    if not reports:
        return {}

    async def body(ib):
        contract = await _resolve(ib, ticker)
        if contract is None:
            return {}
        merged = {}
        for report in reports:
            try:
                xml_text = await ib.reqFundamentalDataAsync(contract, report)
            except Exception as exc:  # noqa: BLE001 - one report failing is not fatal
                log_step("IBKR", "warn", f"{ticker} {report}: {exc}", cfg=cfg)
                continue
            merged.update(parse_ratios(xml_text))
        return merged

    try:
        return _with_session(cfg, body) or {}
    except Exception as exc:  # noqa: BLE001
        log_step("IBKR", "warn", f"fundamentals for {ticker}: {exc}", cfg=cfg)
        return {}


# --------------------------------------------------------------------------
# Market data -- the snapshot fields the deep-dive used to read over MCP
# --------------------------------------------------------------------------

# 104 = historical volatility, 106 = option implied volatility, 165 = the
# 13/26/52-week range and average volume. An unsubscribed field arrives as NaN,
# which reads as missing.
#
# **258 is deliberately not here.** It carries the Reuters fundamental ratio
# block over the market-data channel -- the same numbers
# `reqFundamentalData(ReportSnapshot)` returns -- which would be a way to get
# ratios without the Reuters Worldwide Fundamentals add-on. It is not: an
# account without that add-on gets error 10358 for the tick and the request
# then returns **nothing at all**, including the 52-week range and volume that
# would otherwise have arrived. Verified 2026-08-02: with 258 the snapshot came
# back empty, without it every other field populated. Accounts that do hold the
# subscription can opt in with `ibkr.fundamental_ticks: true`.
GENERIC_TICKS = "104,106,165"
FUNDAMENTAL_TICK = "258"

# reqMarketDataType: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen. Delayed is
# the default because live US equity data needs a subscription this project
# does not require -- the screens run off Yahoo's daily bars, and a 15-minute
# delay is irrelevant to a 52-week range or an average volume.
MARKET_DATA_DELAYED = 3


def _ratios_from_tick(data) -> dict:
    """Flatten a Ticker's `fundamentalRatios` through the same name mapping."""
    ratios = getattr(data, "fundamentalRatios", None)
    if ratios is None:
        return {}
    raw = dict(getattr(ratios, "__dict__", {}) or {})
    out = {}
    for name, value in raw.items():
        value = _finite(value)
        if value is None or value <= -99998:
            continue
        out[name] = value
        if name in RATIO_FIELDS:
            out[RATIO_FIELDS[name]] = value
    return out


def snapshot(tickers: list[str], cfg: dict) -> dict:
    """Per-ticker market statistics: 52-week range, volatility, average volume.

    Also returns whatever ratios arrive on tick 258, so an account without the
    fundamentals add-on can still populate the `ibkr.*` parameters.

    The MCP connector also returned an *IV percentile*, which the TWS API does
    not expose -- that was a Reflexivity computation, not an IBKR field. Raw
    implied and historical volatility are here; a percentile would have to be
    built from a stored history, so it is deliberately absent rather than
    approximated.
    """
    if not available(cfg) or not tickers:
        return {}

    sect = _section(cfg)
    wait = float(sect.get("snapshot_wait_seconds", 8))
    md_type = int(sect.get("market_data_type", MARKET_DATA_DELAYED))
    ticks = GENERIC_TICKS + (f",{FUNDAMENTAL_TICK}"
                             if sect.get("fundamental_ticks") else "")

    async def body(ib):
        ib.reqMarketDataType(md_type)
        out = {}
        for ticker in tickers:
            contract = await _resolve(ib, ticker)
            if contract is None:
                continue
            data = ib.reqMktData(contract, ticks, snapshot=False,
                                 regulatorySnapshot=False)
            await asyncio.sleep(wait)       # let the first tick batch arrive
            row = {
                "price": _finite(data.marketPrice()),
                "close": _finite(data.close),
                "high_52w": _finite(data.high52week),
                "low_52w": _finite(data.low52week),
                "high_26w": _finite(data.high26week),
                "low_26w": _finite(data.low26week),
                "high_13w": _finite(data.high13week),
                "low_13w": _finite(data.low13week),
                "avg_volume": _finite(data.avVolume),
                "historical_volatility": _finite(data.histVolatility),
                "implied_volatility": _finite(data.impliedVolatility),
            }
            row.update(_ratios_from_tick(data))
            out[ticker] = {k: v for k, v in row.items() if v is not None}
            ib.cancelMktData(contract)
        return out

    try:
        # Each ticker costs `wait` seconds of tick-gathering on top of the
        # request budget, so the session timeout has to grow with the list.
        return _with_session(cfg, body, extra_seconds=wait * len(tickers)) or {}
    except Exception as exc:  # noqa: BLE001
        log_step("IBKR", "warn", f"snapshot: {exc}", cfg=cfg)
        return {}


def _finite(value):
    """IB returns NaN for a field the account is not subscribed to."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


# --------------------------------------------------------------------------
# The quality-registry resolver
# --------------------------------------------------------------------------

def metrics(ticker: str, cfg: dict) -> dict:
    """One flat `{name: value}` for `quality.resolve` to index by `ibkr.<name>`.

    Ratios and market statistics merged into one namespace, because from the
    registry's point of view they are both just "something IBKR knows".
    """
    if not available(cfg):
        return {}
    t0 = time.perf_counter()
    out = dict(fundamentals(ticker, cfg))
    if _section(cfg).get("market_data", True):
        out.update(snapshot([ticker], cfg).get(ticker) or {})
    log_step("IBKR", "ok" if out else "none",
             f"{ticker}: {len(out)} field(s)",
             ms=(time.perf_counter() - t0) * 1000, cfg=cfg)
    return out


if __name__ == "__main__":
    import json
    import sys

    from scanner_common import enable_utf8_output, load_config

    enable_utf8_output()
    config = load_config()
    if not is_enabled(config):
        print("ibkr.enabled is false in config.json -- nothing to do.")
        raise SystemExit(0)
    print(f"gateway reachable: {available(config)}")
    for symbol in sys.argv[1:] or ["AAPL"]:
        print(f"\n{symbol}:")
        print(json.dumps(metrics(symbol, config), indent=2, sort_keys=True))
