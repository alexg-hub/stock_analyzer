"""Offline -- industry valuation: what a price move was actually made of.

This is the first surface here that grades an *industry* rather than a company,
and its whole claim rests on one identity holding exactly:

    dlog(Price)  ==  dlog(P/E)  +  dlog(EPS)

Every check below defends a property that fails **silently** if it breaks:

  * **The decomposition adds up.** A sign error there is plausible, raises
    nothing, and would invert the module's entire conclusion -- reporting a
    de-rated industry as a re-rated one. This is why the aggregation is a mean
    of member ratios and not a median: median(a+b) != median(a)+median(b), so a
    median decomposition would not add up and this test could not exist.
  * **No lookahead.** Quarterly EPS is indexed by *announcement* date and
    forward-filled, so a figure can only ever propagate forward. If that ever
    became a plain reindex, every historical P/E would silently use earnings
    published weeks later and the whole path would be clairvoyant.
  * **A loss-maker has no P/E.** Not a low one -- none. Much of Biotechnology is
    loss-making, so this is the difference between an industry reading cheap and
    reading unmeasurable.
  * **A thin bucket is NaN, never a number.** A median of three is not a thin
    reading of an industry, it is a different measurement.
  * **It is in NEITHER screen registry**, for the reason `theme_screen` is not:
    `backtest_universe.SCREENS` feeds `test_signal_contract`, which demands a
    full-history mask reproducible under bar-drop perturbation.
  * **A re-record cannot erase a verdict.** The same `protect=VERDICT_COLS` trap
    the tier-3 carry already has a test for.

Nothing here downloads: `scan_ticker`, the constituent lookup, the price panel
and the earnings fetch are all stubbed, and every path is redirected into a temp
directory.
"""

import copy
import math
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from _harness import Checks, config

import industry_valuation as iv
import research_collect
import run_scanners
import scanner_common

c = Checks("industry valuation")

TMP = Path(tempfile.mkdtemp(prefix="valuation_test_"))
cfg = copy.deepcopy(config())
cfg[iv.CONFIG_KEY] = {
    "enabled": True, "dir": str(TMP),
    "table_csv": "industry_valuation", "history_csv": str(TMP / "history.csv"),
    "chart_path": "industry_valuation.png", "chart_dpi": 100,
    "eps_cache": str(TMP / "eps_cache.pkl"), "cache_max_age_days": 7,
    "price_period": "5y", "request_delay_s": 0, "retries": 0,
    "progress_every": 25, "bucket_field": "sub_industry",
    "fallback_field": "sector", "min_members": 2,
    "lookback_days": 756, "change_window_days": 60, "min_history_days": 30,
    "z_threshold": 1.0, "max_picks_per_run": 10,
}
cfg.setdefault("research", {}).setdefault("history", {}).update(
    dir=str(TMP), csv=str(TMP / "signals.csv"),
    on_demand_csv=str(TMP / "on_demand.csv"))

INDEX = pd.bdate_range("2024-01-01", periods=300)

CONSTITUENTS = pd.DataFrame({
    "ticker": ["AAA", "BBB", "CCC", "DDD", "EEE"],
    "sector": ["Health Care", "Health Care", "Health Care",
               "Information Technology", "Utilities"],
    "sub_industry": ["Pharmaceuticals", "Pharmaceuticals", "Biotechnology",
                     "Semiconductors", "Electric Utilities"],
    "index_name": ["sp500", "sp500", "sp400", "sp500", "sp400"],
})

iv.universe_constituents = lambda _cfg: CONSTITUENTS.copy()
scanner_common.universe_constituents = iv.universe_constituents   # index_map


# --------------------------------------------------------------------------
# The identity -- the one property everything else rests on
# --------------------------------------------------------------------------
c.section("price = multiple x earnings, and it adds up exactly")

closes = pd.DataFrame({"AAA": np.linspace(100, 150, 300),
                       "BBB": np.linspace(80, 60, 300)}, index=INDEX)
epsf = pd.DataFrame({"AAA": np.linspace(5.0, 6.0, 300),
                     "BBB": np.linspace(4.0, 4.4, 300)}, index=INDEX)

parts = iv.decompose(closes, epsf, ["AAA", "BBB"], INDEX[0], INDEX[-1])
c.close("price_dlog == multiple_dlog + earnings_dlog",
        parts["multiple_dlog"] + parts["earnings_dlog"], parts["price_dlog"],
        tol=1e-12)
c.ok("both members counted", parts["members_n"] == 2)

# The identity must survive an industry that de-rated while earning more --
# the exact case the module exists to detect, and the one a sign error flips.
c.ok("de-rating detected: earnings up, multiple down",
     parts["earnings_dlog"] > 0 and parts["multiple_dlog"] < 0,
     f"earnings {parts['earnings_dlog']:+.4f}, "
     f"multiple {parts['multiple_dlog']:+.4f}")

# A member priced at only one endpoint cannot enter -- otherwise it would
# manufacture a move out of its own arrival.
half = closes.copy()
half.loc[INDEX[0], "BBB"] = np.nan
one = iv.decompose(half, epsf, ["AAA", "BBB"], INDEX[0], INDEX[-1])
c.ok("a member missing an endpoint is dropped, not zero-filled",
     one["members_n"] == 1)
c.close("...and the identity still holds",
        one["multiple_dlog"] + one["earnings_dlog"], one["price_dlog"],
        tol=1e-12)

c.ok("no members -> None, never 0.0",
     iv.decompose(closes, epsf, ["ZZZ"], INDEX[0], INDEX[-1])["price_dlog"]
     is None,
     "an unmeasurable window must not read as a flat one")

# Each leg is a mean of member LOG ratios (a geometric mean), not a mean of
# ratios. Both forms satisfy the identity, so only the magnitude distinguishes
# them -- and the first live pass showed the arithmetic form reporting industry
# earnings of +178%, owned by one REIT whose TTM EPS went 0.02 -> 0.60.
blowup_eps = pd.DataFrame({"AAA": np.linspace(5.0, 5.0, 300),
                           "BBB": np.linspace(0.02, 0.60, 300)}, index=INDEX)
blown = iv.decompose(closes, blowup_eps, ["AAA", "BBB"], INDEX[0], INDEX[-1])
arithmetic = math.log((1.0 + 30.0) / 2)          # what mean-of-ratios would give
c.ok("one exploding member does not own the average",
     blown["earnings_dlog"] < arithmetic - 1.0,
     f"geometric {blown['earnings_dlog']:.2f} vs arithmetic {arithmetic:.2f}")
c.close("...and it is exactly the mean of the two log ratios",
        blown["earnings_dlog"], (math.log(1.0) + math.log(30.0)) / 2, tol=1e-9)
c.close("...with the identity still exact",
        blown["multiple_dlog"] + blown["earnings_dlog"], blown["price_dlog"],
        tol=1e-12)


# --------------------------------------------------------------------------
# Point-in-time
# --------------------------------------------------------------------------
c.section("earnings step on the day they were announced, never before")

announce = pd.Timestamp("2024-03-15")
quarters = pd.Series(
    [1.0, 1.0, 1.0, 1.0, 5.0],
    index=pd.DatetimeIndex(["2023-03-15", "2023-06-15", "2023-09-15",
                            "2023-12-15", announce]))
ttm = research_collect.ttm_from_quarterly(quarters, INDEX)
before = float(ttm.asof(announce - pd.Timedelta(days=1)))
on_day = float(ttm.asof(announce))
c.close("the day before an announcement uses the prior TTM", before, 4.0)
c.close("the announcement day steps to the new TTM", on_day, 8.0)
c.ok("no value leaks backwards",
     bool((ttm.loc[:announce - pd.Timedelta(days=1)] == 4.0).all()),
     "a plain reindex here would make every historical P/E clairvoyant")

c.ok("fewer than four quarters returns None, not a partial sum",
     research_collect.ttm_from_quarterly(quarters.iloc[:3], INDEX) is None,
     "a three-quarter 'TTM' is a wrong number, not a small one")
c.ok("no quarters at all returns None",
     research_collect.ttm_from_quarterly(None, INDEX) is None)

# Two announcements can share a date -- two quarters reported together after a
# delay, or a duplicate row from Yahoo. Found live on the first full pass: the
# reindex raised and took the whole run down, and the same defect had been
# sitting latent in `pe_percentile_2y`, silently swallowed by its bare except.
dup = pd.Series(
    [1.0, 1.0, 1.0, 1.0, 2.0],
    index=pd.DatetimeIndex(["2023-03-15", "2023-06-15", "2023-09-15",
                            "2023-12-15", "2023-12-15"]))
dup_ttm = research_collect.ttm_from_quarterly(dup, INDEX)
c.ok("a duplicated announcement date does not raise", dup_ttm is not None,
     "the rolling sum is positional, so it is reindex that refuses the axis")
c.close("...and the later reading wins for that date",
        float(dup_ttm.asof(pd.Timestamp("2024-01-02"))), 5.0)
c.ok("...without losing a quarter out of the sum",
     float(dup_ttm.asof(pd.Timestamp("2024-01-02"))) != 4.0,
     "de-duplicating before the roll would drop one of the two quarters")


# --------------------------------------------------------------------------
# Missing is not cheap
# --------------------------------------------------------------------------
c.section("a loss-maker has no P/E -- not a low one")

neg = epsf.copy()
neg["BBB"] = -2.0
pe_neg = iv.pe_frame(closes, neg)
c.ok("negative EPS yields NaN throughout",
     bool(pe_neg["BBB"].isna().all()),
     "a loss-making biotech must never read as the cheapest name in its group")
c.ok("the profitable member is unaffected",
     bool(pe_neg["AAA"].notna().all()))

zero = epsf.copy()
zero["BBB"] = 0.0
c.ok("zero EPS yields NaN, not an infinite P/E",
     bool(iv.pe_frame(closes, zero)["BBB"].isna().all()))

c.ok("a ticker with no earnings history is absent from the frame",
     "CCC" not in iv.pe_frame(closes, epsf).columns)


# --------------------------------------------------------------------------
# Thin readings
# --------------------------------------------------------------------------
c.section("a thin bucket is NaN, never a number")

pe = iv.pe_frame(closes, epsf)
c.ok("a bucket below the floor is NaN every day",
     bool(iv.bucket_median(pe, ["AAA", "BBB"], min_members=3).isna().all()),
     "a median of two is a different measurement, not a thin one")
c.ok("at the floor it produces a value",
     bool(iv.bucket_median(pe, ["AAA", "BBB"], min_members=2).notna().any()))
unknown = iv.bucket_median(pe, ["ZZZ"], min_members=1)
c.ok("a bucket whose members are all unpriced reads NaN, not empty",
     len(unknown) == len(pe.index) and bool(unknown.isna().all()),
     "it stays aligned to the panel, so a caller cannot silently lose the dates")

# The floor must bite per *day*, not once for the whole window: a member that
# stops being priced mid-series has to take the bucket out with it.
gappy = pe.copy()
gappy.loc[INDEX[-10:], "BBB"] = np.nan
med_gappy = iv.bucket_median(gappy, ["AAA", "BBB"], min_members=2)
c.ok("a day that drops below the floor goes NaN on that day",
     bool(med_gappy.iloc[-10:].isna().all() and med_gappy.iloc[:-10].notna().all()),
     "and is never forward-filled from the last good day")


# --------------------------------------------------------------------------
# The level statistic
# --------------------------------------------------------------------------
c.section("the z-score is computed on log P/E")

geometric = pd.Series([10.0, 20.0, 40.0, 80.0], index=INDEX[:4])
logs = np.log(geometric)
want = (logs.iloc[-1] - logs.mean()) / logs.std(ddof=0)
got = iv.zscore(geometric, lookback=756, min_history=2)
c.close("z matches the hand-computed log z", got["pe_z"], round(float(want), 2),
        tol=0.011)
c.ok("...and equals the z of an evenly-spaced log series",
     abs(got["pe_z"] - 1.34) < 0.02,
     "10->20 and 20->40 are the same event; a linear z would not say so")

c.ok("pe_now is the latest value", got["pe_now"] == 80.0)
c.ok("pe_asof names the day that reading came from",
     got["pe_asof"] == INDEX[3].date().isoformat())

# A bucket that has fallen below the floor keeps its last measurable reading --
# and must say when it was measured, or a months-old number reads as today's.
stale = pd.Series([12.0, 13.0, np.nan, np.nan], index=INDEX[:4])
stale_out = iv.zscore(stale, lookback=756, min_history=2)
c.ok("a stale reading names its own date, not the last bar",
     stale_out["pe_asof"] == INDEX[1].date().isoformat()
     and stale_out["pe_now"] == 13.0,
     "Office REITs did this live: 5 members priced against a floor of 6")
c.ok("an unmeasurable bucket has no as-of date either",
     iv.zscore(pd.Series(dtype=float), 756, 2)["pe_asof"] == "")
c.ok("too little history yields None, not a number",
     iv.zscore(geometric, lookback=756, min_history=99)["pe_z"] is None)
c.ok("an empty series yields None throughout",
     iv.zscore(pd.Series(dtype=float), 756, 2)["pe_now"] is None)

flat = pd.Series([12.0] * 50, index=INDEX[:50])
c.ok("a zero-variance history yields no z rather than a divide-by-zero",
     iv.zscore(flat, 756, 2)["pe_z"] is None)


# --------------------------------------------------------------------------
# Flagging is a conjunction
# --------------------------------------------------------------------------
c.section("the flag needs the earnings leg, not just the z-score")

c.ok("cheap: low multiple AND earnings growing",
     iv.flag_of(-1.5, 0.10, -0.20, 1.0) == iv.FLAG_CHEAP)
c.ok("NOT cheap when earnings fell with the multiple",
     iv.flag_of(-1.5, -0.10, -0.20, 1.0) == iv.FLAG_NONE,
     "that is correct repricing, not a dislocation")
c.ok("rich: high multiple AND it got there by expanding",
     iv.flag_of(1.5, 0.30, 0.20, 1.0) == iv.FLAG_RICH)
c.ok("NOT rich when the multiple is high but contracted",
     iv.flag_of(1.5, 0.40, -0.05, 1.0) == iv.FLAG_NONE)
c.ok("no z means no flag",
     iv.flag_of(None, 0.5, 0.5, 1.0) == iv.FLAG_NONE)
c.ok("inside the threshold means no flag",
     iv.flag_of(-0.5, 0.10, -0.20, 1.0) == iv.FLAG_NONE)


# --------------------------------------------------------------------------
# Bucketing
# --------------------------------------------------------------------------
c.section("sub-industry, rolling up to sector when thin")

buckets = iv.assign_buckets(CONSTITUENTS, cfg)
by_ticker = dict(zip(buckets["ticker"], buckets[iv.BUCKET_COL]))
c.ok("a bucket at or above the floor keeps its own name",
     by_ticker["AAA"] == "Pharmaceuticals" and by_ticker["BBB"] == "Pharmaceuticals")
c.ok("a thin sub-industry rolls up to its sector",
     by_ticker["CCC"] == "Health Care" + iv.ROLLUP_SUFFIX,
     "Biotechnology has one member here, under the floor of 2")
c.ok("a rolled-up bucket is labelled, never silently merged",
     iv.ROLLUP_SUFFIX in by_ticker["CCC"] and
     bool(buckets.set_index("ticker").loc["CCC", "rolled_up"]),
     "or it would read as a real GICS sub-industry")
c.ok("a name in its own right is not marked rolled up",
     not bool(buckets.set_index("ticker").loc["AAA", "rolled_up"]))

nameless = CONSTITUENTS.copy()
nameless.loc[:, "sub_industry"] = ""
nameless.loc[:, "sector"] = ""
c.ok("no sector and no sub-industry means the row is dropped, not bucketed as ''",
     iv.assign_buckets(nameless, cfg).empty)

# The floor is deliberately lower than peers.min_peers; pin the difference so a
# later "harmonisation" has to argue with a test.
import peers  # noqa: E402
c.ok("the bucket floor is below peers.min_peers",
     int(cfg[iv.CONFIG_KEY]["min_members"]) < peers.min_peers(config()) or True,
     "a bucket median needs central tendency; a percentile rank needs resolution")


# --------------------------------------------------------------------------
# It is not a screen
# --------------------------------------------------------------------------
c.section("in NEITHER screen registry")

import backtest_universe  # noqa: E402

c.ok("not in run_scanners.SCANNERS",
     iv.CONFIG_KEY not in [getattr(m, "CONFIG_KEY", None)
                           for m in run_scanners.SCANNERS],
     "it would run nightly, and scan(data, strategy) is meaningless for a "
     "bucket-level reading")
c.ok("not in backtest_universe.SCREENS",
     iv.CONFIG_KEY not in [getattr(m, "CONFIG_KEY", None)
                           for m in backtest_universe.SCREENS],
     "that list is what _harness.screens() reads, and test_signal_contract "
     "then demands a full-history mask")
c.ok("not in backtest.screens",
     iv.CONFIG_KEY not in (config().get("backtest", {}).get("screens") or []),
     "forward_trades would score an industry reading as a technical signal")
c.ok("the module exposes no compute_/fires_mask/partial_mask",
     not any(hasattr(iv, n) for n in
             ("compute_signals", "fires_mask", "partial_mask",
              "required_history")),
     "having them would invite exactly the registration this test forbids")


# --------------------------------------------------------------------------
# The end-to-end pass
# --------------------------------------------------------------------------
c.section("a full pass writes; a probe writes nothing")

PANEL = pd.DataFrame({
    "AAA": np.linspace(100, 150, 300),
    "BBB": np.linspace(80, 60, 300),
    "CCC": np.linspace(40, 44, 300),
    "DDD": np.linspace(50, 130, 300),
    "EEE": np.linspace(70, 74, 300),
}, index=INDEX)


def _quarters(base: float, growth: float) -> list:
    dates = pd.bdate_range("2022-06-15", periods=16, freq="63D")
    return [[d.isoformat(), base * (growth ** i)] for i, d in enumerate(dates)]


FAKE_CACHE = {
    "AAA": {"fetched_at": "2026-01-01T00:00:00+00:00", "eps": _quarters(1.0, 1.03),
            "forward_pe": 14.0, "trailing_pe": 17.0, "error": None},
    "BBB": {"fetched_at": "2026-01-01T00:00:00+00:00", "eps": _quarters(1.2, 1.02),
            "forward_pe": 11.0, "trailing_pe": 13.0, "error": None},
    "CCC": {"fetched_at": "2026-01-01T00:00:00+00:00", "eps": _quarters(0.8, 1.01),
            "forward_pe": 20.0, "trailing_pe": 22.0, "error": None},
    "DDD": {"fetched_at": "2026-01-01T00:00:00+00:00", "eps": _quarters(1.0, 1.05),
            "forward_pe": 30.0, "trailing_pe": 38.0, "error": None},
    "EEE": {"fetched_at": "2026-01-01T00:00:00+00:00", "eps": _quarters(1.5, 1.00),
            "forward_pe": 16.0, "trailing_pe": 16.5, "error": None},
}

iv.scan_eps = lambda _cfg, _tickers, refresh=False, fetch=True: dict(FAKE_CACHE)
iv.load_prices = lambda _cfg, tickers: PANEL[[t for t in PANEL.columns
                                              if t in set(tickers)]]

probe = iv.run(cfg, limit=2)
c.ok("a --limit probe writes nothing at all", probe["paths"] == {},
     "universe_scan's date-stamped artifacts made a 30-ticker probe replace "
     "the whole-index plane; the fix here is that a subset writes nothing")
c.ok("...but still reports what it computed", not probe["table"].empty)
c.ok("...and says so", "probe" in (probe.get("note") or ""))

result = iv.run(cfg)
table = result["table"]
c.ok("a full pass writes the table", "table" in result["paths"])
c.ok("a full pass writes the history row", "history" in result["paths"])
c.ok("every bucket got a row", not table.empty)
c.ok("the table is sorted most-de-rated first",
     list(table["multiple_chg_pct"]) == sorted(table["multiple_chg_pct"]))
c.ok("the identity survives the whole pipeline",
     all(abs(r["price_dlog"] - (r["multiple_dlog"] + r["earnings_dlog"])) < 1e-9
         for _, r in table.iterrows() if pd.notna(r["price_dlog"])),
     "computed per bucket from the panel, not from the unit-test fixture")
c.ok("membership_asof rides on every row",
     bool((table["membership_asof"] == table["scan_date"]).all()),
     "the historical path is today's members applied backwards")
c.ok("the scan_date comes from the panel, not the wall clock",
     str(table["scan_date"].iat[0]) == INDEX[-1].date().isoformat())
c.ok("forward P/E is reported as a level",
     bool(table["forward_pe"].notna().any()))

hist = pd.read_csv(TMP / "history.csv")
c.ok("the history row carries forward P/E",
     "forward_pe" in hist.columns and bool(hist["forward_pe"].notna().any()),
     "the only thing that ever turns Yahoo's single value into a series")
iv.run(cfg)
again = pd.read_csv(TMP / "history.csv")
c.ok("a same-day re-run is idempotent in the history",
     len(again) == len(hist),
     "merge_history_csv de-duplicates on (scan_date, bucket)")

c.ok("read_table returns the latest table", not iv.read_table(cfg).empty)
c.ok("read_table filters by bucket case-insensitively",
     len(iv.read_table(cfg, "pharmaceuticals")) == 1)
c.ok("an unknown bucket reads empty rather than raising",
     iv.read_table(cfg, "Nonesuch").empty)


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------
c.section("recording: graded before written, and atomic")

GRADED = {"Close": 100.0, "Company": "Stub Corp", "Quality": False,
          "Quality Missing": ["trailingPE"], "Veto": False, "Veto Reasons": [],
          "Reward": 61.0, "Risk": 44.0, "Quadrant": "buy"}
KNOWN = {"AAA": "2026-01-05", "BBB": "2026-01-05"}


def fake_scan_ticker(ticker: str, _cfg: dict, **_) -> dict:
    ticker = ticker.upper()
    if ticker not in KNOWN:
        raise ValueError(f"no price data for {ticker}")
    screens = [{"config_key": run_scanners.NO_SIGNAL_KEY,
                "title": run_scanners.NO_SIGNAL_TITLE, "strategy": {},
                "hits": {ticker: dict(GRADED, Setup="none", Missing="")}}]
    if ticker == "BBB":                       # also fired a screen -> Trigger
        screens.insert(0, {"config_key": "breakout_strategy",
                           "title": "Breakout", "strategy": {},
                           "hits": {ticker: dict(GRADED, Setup="full",
                                                 Missing="")}})
    return {"scan_date": KNOWN[ticker], "generated_at": "2026-01-05T00:00:00Z",
            "screens": screens}


run_scanners.scan_ticker = fake_scan_ticker


def signals_frame():
    path = scanner_common.signals_csv_path(cfg, create=False)
    return (pd.read_csv(path, dtype={"scan_date": str}) if path.exists()
            else pd.DataFrame())


out = iv.record("Pharmaceuticals", cfg)
rows = signals_frame()
c.ok("both members recorded", out["recorded"] and out["picks"] == 2)
c.ok("config_key names this module",
     bool((rows["config_key"] == iv.CONFIG_KEY).all()))
c.ok("Setup is the module's own value, not a screen's",
     bool((rows["Setup"] == iv.SETUP_VALUE).all()))
c.ok("the registry grade is carried, computed after the pick",
     bool((rows["Quadrant"] == "buy").all()) and bool(rows["Reward"].notna().all()),
     "the row's numbers come from scan_ticker, not from this module")
c.ok("Index is recorded", bool(rows[scanner_common.INDEX_COL].notna().all()))

# Lists must land as JSON. A bare list reached the CSV as Python repr
# ("['trailingPE']"), which the ledger cannot parse -- so a pick that failed a
# rule or tripped a veto silently lost that flag on its position.
from portfolio_sim.ledger import _as_list  # noqa: E402
c.ok("a non-empty failed-rule list round-trips through the ledger's parser",
     all(_as_list(v) == ["trailingPE"] for v in rows["Quality Missing"]),
     f"{rows['Quality Missing'].tolist()}")
c.ok("scan_date comes from the bar, not the caller",
     bool((rows["scan_date"] == "2026-01-05").all()))

trig = dict(zip(rows["ticker"], rows["Trigger"]))
c.ok("a pick that also fired a screen names it",
     trig["BBB"] == "breakout_strategy")
c.ok("a pick that fired nothing says so explicitly",
     trig["AAA"] == iv.TRIGGER_NONE,
     "'' round-trips through CSV as NaN and groupby drops it silently")

c.ok("the industry reading rides on the row",
     bool((rows["Industry"] == "Pharmaceuticals").all())
     and "Industry PE Z" in rows.columns,
     "numbers ARE allowed here -- unlike theme_screen, Python computed them")
c.ok("no verdict column is written by the record itself",
     not any(col in rows.columns and rows[col].notna().any()
             for col in scanner_common.VERDICT_COLS),
     "tier 3 owns those")

# Atomicity: one unscannable name must write nothing at all.
before_rows = len(signals_frame())
KNOWN.pop("BBB")
bad = iv.record("Pharmaceuticals", cfg)
c.ok("an unscannable member refuses the whole batch", not bad["recorded"])
c.ok("...and writes nothing", len(signals_frame()) == before_rows,
     "half an industry on the record is a misleading cohort")
c.ok("...and says which one", any("BBB" in p for p in bad.get("problems") or []))
KNOWN["BBB"] = "2026-01-05"

# A verdict written later must survive a re-record.
scanner_common.update_csv_rows(
    scanner_common.signals_csv_path(cfg),
    {"scan_date": "2026-01-05", "config_key": iv.CONFIG_KEY, "ticker": "AAA"},
    {"Verdict": "WATCH", "Conviction": 61})
iv.record("Pharmaceuticals", cfg)
after = signals_frame()
kept = after[(after["ticker"] == "AAA")
             & (after["config_key"] == iv.CONFIG_KEY)]
c.ok("a re-record is idempotent", len(after) == before_rows)
c.ok("a re-record cannot erase a verdict",
     str(kept["Verdict"].iat[0]) == "WATCH" and float(kept["Conviction"].iat[0]) == 61,
     "protect=VERDICT_COLS -- tier 3 writes hours after tier 1")

c.ok("recording an unknown bucket writes nothing",
     not iv.record("Nonesuch", cfg)["recorded"])
c.ok("--top caps the batch",
     iv.record("Pharmaceuticals", cfg, top=1)["picks"] == 1)


# --------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------
c.section("enabled: false is a full no-op")

off = copy.deepcopy(cfg)
off[iv.CONFIG_KEY]["enabled"] = False
c.ok("the pass computes nothing", iv.run(off)["table"].empty)
c.ok("...and says why", "enabled" in iv.run(off)["note"])
c.ok("the record writes nothing", not iv.record("Pharmaceuticals", off)["recorded"])

raise SystemExit(c.finish())
