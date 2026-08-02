"""The IBKR client: no account surface, and a dead gateway is never an error.

Fully offline. `ib_async` need not even be installed -- that is one of the
cases under test, since the nightly run has to survive it.

Two invariants, both of which fail *silently* in production if broken:

  * **No account data exists here.** The user's real IBKR book is out of scope:
    a deep-dive grades the security and tier 4 grades the signal against a
    fixed-notional virtual ledger, so what is already held changes neither, and
    reports must not mention holdings, position size or concentration. The MCP
    allow-lists enforced that by enumeration; moving to a Python client would
    quietly lose the guarantee unless the capability is simply never written.
  * **Unreachable is a skip, not a failure.** Retail IBKR has no headless API,
    so the gateway being logged out is an ordinary Tuesday. Every getter must
    return empty and the quality engine must read that as missing values.
"""

import copy
import inspect

from _harness import Checks, config

import ibkr
import quality

c = Checks("ibkr client")
cfg = config()

# --------------------------------------------------------------------------
c.section("the account surface does not exist")

source = inspect.getsource(ibkr)
public = {name for name in dir(ibkr) if not name.startswith("_")}

c.ok("no account/position/PnL function is exposed",
     not [n for n in ibkr.FORBIDDEN_CALLS if n in public],
     f"{[n for n in ibkr.FORBIDDEN_CALLS if n in public]}")

# The names may appear in the docstring and in FORBIDDEN_CALLS itself -- that is
# the point of documenting them. What must not appear is a *call*.
called = [n for n in ibkr.FORBIDDEN_CALLS if f".{n}(" in source]
c.ok("...and none of them is ever called", not called, f"{called}")

c.ok("the prohibition is documented in the module docstring",
     "account" in (ibkr.__doc__ or "").lower()
     and "out of scope" in (ibkr.__doc__ or "").lower())
c.ok("the connection is opened read-only",
     "readonly=True" in source,
     "belt and braces: this module never places an order")

# --------------------------------------------------------------------------
c.section("an unreachable gateway degrades instead of raising")

off = copy.deepcopy(cfg)
off.setdefault("ibkr", {})["enabled"] = False
ibkr.reset_cache()
c.ok("disabled reports unavailable", ibkr.available(off) is False)
c.ok("...and every getter returns empty",
     ibkr.metrics("AAPL", off) == {}
     and ibkr.fundamentals("AAPL", off) == {}
     and ibkr.snapshot(["AAPL"], off) == {})

on = copy.deepcopy(cfg)
on.setdefault("ibkr", {})
on["ibkr"].update(enabled=True, host="127.0.0.1", port=1,   # nothing listens
                  connect_timeout_seconds=2, request_timeout_seconds=2)
ibkr.reset_cache()
c.ok("an enabled but unreachable gateway reports unavailable",
     ibkr.available(on) is False,
     "missing ib_async and a refused connection are both ordinary")
c.ok("...and still raises nothing", ibkr.metrics("AAPL", on) == {})
ibkr.reset_cache()

# The engine must read an empty bundle as missing values, not as failures --
# same rule as a Yahoo field Yahoo does not publish.
ibkr_params = {k: s for k, s in quality.parameters(cfg, enabled_only=False).items()
               if (s.get("source") or "").startswith("ibkr.")}
resolved = quality.resolve({"ibkr": {}}, cfg)
c.ok("an empty IBKR bundle resolves every ibkr parameter to None",
     all(resolved.get(k) is None for k in ibkr_params if k in resolved),
     f"{len(ibkr_params)} ibkr parameter(s) configured")

# --------------------------------------------------------------------------
c.section("the Refinitiv ratio parser")

XML = """<ReportSnapshot><Ratios>
<Group ID="Profitability">
  <Ratio FieldName="TTMROEPCT" Type="N">28.4</Ratio>
  <Ratio FieldName="QTOTD2EQ" Type="N">154.5</Ratio>
  <Ratio FieldName="TTMREVCHG" Type="N">-99999</Ratio>
  <Ratio FieldName="NOTMAPPEDYET" Type="N">7.5</Ratio>
  <Ratio FieldName="EMPTY" Type="N"></Ratio>
</Group></Ratios></ReportSnapshot>"""

ratios = ibkr.parse_ratios(XML)
c.ok("a mapped field gets its friendly name",
     ratios.get("return_on_equity") == 28.4 and ratios.get("debt_to_equity") == 154.5)
c.ok("an unmapped field is still returned under its raw name",
     ratios.get("NOTMAPPEDYET") == 7.5,
     "so a new parameter is a config change, not a code change")
c.ok("Refinitiv's -99999 'not reported' sentinel is dropped",
     "revenue_growth_rate" not in ratios and "TTMREVCHG" not in ratios,
     "left alone it reads as a real, catastrophically bad number")
c.ok("an empty element is dropped rather than parsed as zero",
     "EMPTY" not in ratios)
c.ok("malformed XML yields nothing rather than raising",
     ibkr.parse_ratios("not xml at all") == {}
     and ibkr.parse_ratios("") == {}
     and ibkr.parse_ratios(None) == {})

c.ok("every mapped name is reachable from a config source",
     all(isinstance(v, str) and v for v in ibkr.RATIO_FIELDS.values()))

# --------------------------------------------------------------------------
c.section("subscription realities that cost data when ignored")

# Verified against a live gateway on 2026-08-02: an account without the Reuters
# Worldwide Fundamentals add-on gets error 10358 for tick 258 and the request
# then returns NOTHING -- not even the 52-week range and volume that would
# otherwise have arrived. Including it by default trades every market-data
# field for one that is denied.
c.ok("the fundamentals tick is not requested by default",
     ibkr.FUNDAMENTAL_TICK not in ibkr.GENERIC_TICKS.split(","),
     f"default ticks: {ibkr.GENERIC_TICKS}")
c.ok("...but the range/volume/volatility ticks are",
     {"104", "106", "165"} <= set(ibkr.GENERIC_TICKS.split(",")))
c.ok("...and it can be opted into for an account that holds the subscription",
     "fundamental_ticks" in inspect.getsource(ibkr.snapshot))

# `reports: []` is a decision, not an absence. Treating it as unset (the `or`
# idiom) made a config that had already been told the account cannot serve
# reports still pay a failed round-trip, and log a 10358, for every ticker.
disabled_reports = copy.deepcopy(cfg)
disabled_reports.setdefault("ibkr", {}).update(enabled=True, reports=[])
ibkr.reset_cache()
c.ok("an explicitly empty `reports` list asks for nothing",
     ibkr.fundamentals("AAPL", disabled_reports) == {},
     "and must not fall back to the default report")
ibkr.reset_cache()

# Live US equity data needs a subscription this project does not require; the
# screens run off Yahoo's daily bars, so a 15-minute delay is irrelevant to a
# 52-week range or an average volume.
c.ok("market data defaults to delayed rather than live",
     ibkr.MARKET_DATA_DELAYED == 3
     and "market_data_type" in inspect.getsource(ibkr.snapshot))

# --------------------------------------------------------------------------
c.section("NaN handling")

c.ok("an unsubscribed market-data field (NaN) becomes None",
     ibkr._finite(float("nan")) is None
     and ibkr._finite(float("inf")) is None
     and ibkr._finite(None) is None)
c.ok("a real number survives", ibkr._finite(12.5) == 12.5)

raise SystemExit(c.finish())
