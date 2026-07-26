"""
Shared infrastructure for all S&P 500 scanners.

Everything here is scanner-agnostic: config loading, ticker universe,
bulk/single-ticker price downloads, fundamentals, and Discord delivery.
Screen modules (breakout_scanner.py, sma_pullback.py, ...) contain only
their own condition math and formatting; run_scanners.py orchestrates.
"""

import io
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

# The code lives flat in the project root; this is the one line to revisit if
# it ever moves into a package directory.
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"


def output_dir(create: bool = True) -> Path:
    """The single directory every *generated* artifact goes to.

    Logs, the `latest_hits.json` scan hand-off, backtest tables and charts, and
    the cached price panel -- all of it lands here, so the project root holds
    only inputs (code, config.json, docs). Config keeps storing bare filenames
    and this resolves them; an absolute path in config still wins.
    """
    out = PROJECT_ROOT / "output"
    if create:
        out.mkdir(parents=True, exist_ok=True)
    return out

# Discord hard limit is 2000 chars per message; stay under it so long
# hit lists get split across several messages instead of being rejected.
DISCORD_CHAR_LIMIT = 1900
# Discord allows at most 10 embeds and 10 attachments per webhook message,
# and at most 6000 chars across all embeds of one message.
DISCORD_MAX_EMBEDS = 10
DISCORD_EMBED_CHAR_BUDGET = 5500

# Embed side-bar color for a *partial* setup's card (a full setup gets the
# screen's own EMBED_COLOR) -- one card type, tier visible at a glance.
PARTIAL_COLOR = 0x898781

# Reserved column (added by fetch_fundamentals) holding the company name for
# the embed titles; not a config-driven fundamentals field, so it never
# renders as an inline field.
COMPANY_COL = "Company"

# Reserved columns (added by annotate_quality) holding the tier-2 quality
# verdict, mirroring the tier-1 Setup/Missing pair: the badge decision and the
# rules that failed. Like COMPANY_COL these are not config-driven fundamentals
# fields and never render as inline fields.
QUALITY_COL = "Quality"
QUALITY_MISSING_COL = "Quality Missing"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def enable_utf8_output() -> None:
    """Make stdout/stderr able to carry the text this project actually prints.

    Windows picks the locale codepage when output is redirected (cp1255 on this
    box), and the quality badge -- a real character in the Discord payload --
    has no mapping there, so a dry-run print of a passing ticker raises
    UnicodeEncodeError. That would take down `run_deepdive.bat` in exactly the
    `discord_send: false` configuration used to shake the nightly run down.
    `errors="replace"` keeps it non-fatal even if reconfigure is unavailable.

    Called from entry-point `__main__` blocks rather than at import, so
    importing this module never mutates global streams.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):   # not a reconfigurable stream
            pass


# --------------------------------------------------------------------------
# Scan result contract (what every screen module's scan() returns)
# --------------------------------------------------------------------------

@dataclass
class ScanResult:
    """Output of one screen over the whole universe on the scan day.

    `hits` is a ticker-indexed DataFrame of every signal the screen fired --
    one list, with a `Setup` column of `full`/`partial` and `Missing` naming
    the failing test on a partial. `strategy` is the config section the screen
    ran with, kept here so formatting/plotting stay config-driven.
    """
    title: str
    hits: pd.DataFrame
    strategy: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------

def get_sp500_tickers(source_url: str) -> list[str]:
    """Scrape the current S&P 500 constituents from Wikipedia.

    Yahoo uses '-' where Wikipedia uses '.' in share-class tickers
    (BRK.B -> BRK-B), so normalize before downloading.
    """
    # Wikipedia rejects requests without a browser-like User-Agent.
    resp = requests.get(
        source_url,
        headers={"User-Agent": "Mozilla/5.0 (breakout-scanner)"},
        timeout=30,
    )
    resp.raise_for_status()

    tables = pd.read_html(io.StringIO(resp.text))
    constituents = next(t for t in tables if "Symbol" in t.columns)
    tickers = (
        constituents["Symbol"]
        .astype(str)
        .str.strip()
        .str.replace(".", "-", regex=False)
        .tolist()
    )
    print(f"Fetched {len(tickers)} S&P 500 tickers from Wikipedia.")
    return tickers


def drop_unsettled_tail(data: pd.DataFrame, max_missing_pct: float = 0.5) -> pd.DataFrame:
    """Drop trailing bars that have no settled close.

    Yahoo serves an unsettled session as an ordinary daily row with Open/High/
    Low/Volume filled in but **Close null** -- and it sometimes reverts an
    already-settled bar to that form hours later (observed 2026-07-24: 503 of
    504 closes withdrawn on the Saturday after Friday's nightly run had scanned
    that same bar successfully). Every condition in every screen compares
    against Close, so such a row makes each test NaN, `fillna(False)` turns
    that into "no signal", and the scan reports a confident zero on data that
    looks complete. Scanning the last *settled* bar instead is also the right
    behaviour for an intraday run.

    A fraction, not `any`: individual tickers legitimately go missing
    (delistings, per-ticker download failures) and must not discard the day.
    """
    if "Close" not in data.columns.get_level_values(0):
        return data
    missing = data["Close"].isna().mean(axis=1)   # NaN fraction per bar
    keep = len(data)
    while keep and missing.iloc[keep - 1] > max_missing_pct:
        keep -= 1
    if keep == len(data):
        return data
    dropped = [str(d.date()) for d in data.index[keep:]]
    if not keep:
        raise RuntimeError(
            f"No bar has a settled close (checked {len(dropped)}) -- Yahoo is "
            f"serving unsettled rows; retry later.")
    print(f"WARNING: {', '.join(dropped)} has no settled close for most tickers "
          f"(Yahoo has not published it) -- dropping and scanning "
          f"{data.index[keep - 1].date()} instead.")
    return data.iloc[:keep]


def download_price_data(tickers: list[str], period: str, interval: str) -> pd.DataFrame:
    """Bulk-download OHLCV for all tickers in one threaded yfinance call.

    Returns a DataFrame with a (Field, Ticker) column MultiIndex, e.g.
    data["Close"]["AAPL"] is the close series for AAPL.
    """
    print(f"Downloading {period} of {interval} data for {len(tickers)} tickers...")
    data = yf.download(
        tickers,
        period=period,
        interval=interval,
        group_by="column",
        auto_adjust=False,
        threads=True,
        progress=sys.stdout.isatty(),  # no progress-bar spam in log files
    )
    if data.empty:
        raise RuntimeError("yfinance returned no data -- check connectivity.")
    return drop_unsettled_tail(data)


def warmup_months(window: int) -> int:
    """Extra calendar months to download before a backtest's analysis window
    so a rolling window of `window` trading days (~21/month) is fully warmed
    up by the first analysis day."""
    return math.ceil(window / 21) + 2


def download_history(ticker: str, start: pd.Timestamp, end: pd.Timestamp,
                     window: int) -> pd.DataFrame:
    """Download one ticker's OHLCV in the same (Field, Ticker) column layout
    the scanners use, so the compute_* functions run unchanged."""
    months = warmup_months(window)
    dl_start = start - pd.DateOffset(months=months)
    print(f"Downloading {ticker} daily data {dl_start.date()} .. {end.date()} "
          f"(includes {months} months of warm-up for the {window}-day rolling window)")
    data = yf.download(
        ticker,
        start=dl_start,
        end=end + pd.Timedelta(days=1),
        interval="1d",
        group_by="column",
        auto_adjust=False,
        progress=False,
    )
    if data.empty:
        raise SystemExit(f"No data returned for {ticker} -- check the ticker/dates.")
    if not isinstance(data.columns, pd.MultiIndex):
        data.columns = pd.MultiIndex.from_product([data.columns, [ticker]])
    return drop_unsettled_tail(data)


def single_ticker_panel(data: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Slice one ticker out of a bulk (Field, Ticker) frame, keeping the
    MultiIndex layout so the compute_* functions run unchanged."""
    return data.loc[:, pd.IndexSlice[:, [ticker]]]


# --------------------------------------------------------------------------
# Fundamentals for the hits
# --------------------------------------------------------------------------

def stmt_value(df: pd.DataFrame, row_name: str, column) -> float | None:
    """One cell of a financial statement, or None when it isn't there.

    Tolerant by design -- a missing row, a missing column and a NaN cell all
    come back as None, because Yahoo genuinely omits rows per issuer (banks
    have no Operating Income, negative-equity companies no Debt/Equity).
    Shared with `research_collect._financials`, which reads the same frames
    for the tier-3 chart.
    """
    if df.empty or row_name not in df.index or column not in df.columns:
        return None
    value = df.loc[row_name, column]
    return float(value) if pd.notna(value) else None


def _yearly_series(values: list[tuple[int, float | None]]) -> list[tuple[int, float]]:
    """Drop missing years; keep (fiscal_year, value) oldest -> newest."""
    return [(year, value) for year, value in values if value is not None]


def _statement_metrics(tk: "yf.Ticker", stmt_cfg: dict, info: dict) -> dict:
    """Compute the statement-based metrics for one ticker.

    Multi-year metrics (FCF, margins) are stored as [(fiscal_year, value)]
    lists oldest -> newest; ROE/ROIC as scalar percents. Anything Yahoo
    doesn't provide for this company simply stays None -> 'n/a'.
    """
    years = stmt_cfg.get("years", 2)
    metrics = stmt_cfg.get("metrics", {})

    def statement(name: str) -> pd.DataFrame:
        try:
            df = getattr(tk, name)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
        except Exception as exc:  # noqa: BLE001 - missing statements must not kill the alert
            print(f"  {name} failed for {tk.ticker}: {exc}")
        return pd.DataFrame()

    income = statement("income_stmt")
    cashflow = statement("cash_flow")
    balance = statement("balance_sheet")

    # The `years` most recent annual columns, oldest -> newest.
    inc_cols = sorted(income.columns)[-years:] if not income.empty else []
    cf_cols = sorted(cashflow.columns)[-years:] if not cashflow.empty else []

    row = {}
    if "fcf" in metrics:
        row[metrics["fcf"]] = _yearly_series(
            [(c.year, stmt_value(cashflow, "Free Cash Flow", c)) for c in cf_cols]
        )
    if "operating_margin" in metrics or "profit_margin" in metrics:
        def margin(numerator_row):
            series = []
            for c in inc_cols:
                num = stmt_value(income, numerator_row, c)
                rev = stmt_value(income, "Total Revenue", c)
                series.append((c.year, 100 * num / rev if num is not None and rev else None))
            return _yearly_series(series)

        if "operating_margin" in metrics:
            row[metrics["operating_margin"]] = margin("Operating Income")
        if "profit_margin" in metrics:
            row[metrics["profit_margin"]] = margin("Net Income")

    if "roe" in metrics:
        roe = None
        for c in reversed(inc_cols):  # latest year with both rows present
            net = stmt_value(income, "Net Income", c)
            equity = stmt_value(balance, "Stockholders Equity", c)
            if net is not None and equity:
                roe = 100 * net / equity
                break
        if roe is None and isinstance(info.get("returnOnEquity"), (int, float)):
            roe = 100 * info["returnOnEquity"]
        row[metrics["roe"]] = roe

    if "roic" in metrics:
        roic = None
        for c in reversed(inc_cols):
            ebit = stmt_value(income, "EBIT", c)
            tax = stmt_value(income, "Tax Provision", c)
            pretax = stmt_value(income, "Pretax Income", c)
            invested = stmt_value(balance, "Invested Capital", c)
            if None in (ebit, tax, pretax) or not invested or pretax <= 0:
                continue
            nopat = ebit * (1 - tax / pretax)
            roic = 100 * nopat / invested
            break
        row[metrics["roic"]] = roic

    return row


def fetch_fundamentals(tickers: list[str], fund_cfg: dict) -> pd.DataFrame:
    """Pull the configured fundamentals from Yahoo for each ticker.

    Two layers, both config-driven (`fundamentals` section):
      * `fields`  -- snapshot values from `info` (fields listed in
        `percent_fields` come from Yahoo as fractions and are x100;
        note dividendYield is already a percentage);
      * `statements` -- per-year metrics computed from the annual income
        statement / cash flow / balance sheet (FCF, margins, ROE, ROIC).

    Only runs on the (small) list of signalling tickers, so a plain
    loop is fine here.
    """
    fields = fund_cfg["fields"]
    percent_fields = fund_cfg.get("percent_fields", [])
    stmt_cfg = fund_cfg.get("statements", {})

    rows = {}
    for ticker in tickers:
        tk = yf.Ticker(ticker)
        try:
            info = tk.info
        except Exception as exc:  # noqa: BLE001 - a bad ticker must not kill the alert
            print(f"  fundamentals failed for {ticker}: {exc}")
            info = {}
        row = {COMPANY_COL: info.get("longName") or info.get("shortName")}
        for key, label in fields.items():
            value = info.get(key)
            if key in percent_fields and isinstance(value, (int, float)):
                value *= 100
            row[label] = value
        if stmt_cfg.get("enabled"):
            row.update(_statement_metrics(tk, stmt_cfg, info))
        rows[ticker] = row
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("Ticker")


def fmt_value(value) -> str:
    """n/a-tolerant number formatting shared by all alert sections."""
    return f"{value:.2f}" if isinstance(value, (int, float)) and pd.notna(value) else "n/a"


def fmt_compact(value) -> str:
    """19.3B / 850M style formatting for large currency amounts."""
    if not isinstance(value, (int, float)) or pd.isna(value):
        return "n/a"
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(value) >= divisor:
            return f"{value / divisor:.1f}{suffix}"
    return f"{value:,.0f}"


def _fmt_pct(value) -> str:
    return f"{value:.1f}%" if isinstance(value, (int, float)) and pd.notna(value) else "n/a"


def _fmt_yearly(series, fmt) -> str:
    """'18.1B->19.3B' for a [(fiscal_year, value)] list, oldest -> newest."""
    if not isinstance(series, list) or not series:
        return "n/a"
    return "->".join(fmt(value) for _, value in series)


def _year_span(series) -> str:
    """'FY24->FY25' label for a [(fiscal_year, value)] list."""
    if not isinstance(series, list) or not series:
        return ""
    years = [f"FY{year % 100:02d}" for year, _ in series]
    return f" ({years[0]}->{years[-1]})" if len(years) > 1 else f" ({years[0]})"


def fundamentals_fields(row, fund_cfg: dict) -> list[dict]:
    """The per-ticker fundamentals as Discord embed fields (inline, so the
    client lays them out as a 3-per-row grid -- the 'table')."""
    if not fund_cfg.get("enabled"):
        return []

    def field(name, value):
        return {"name": name, "value": value, "inline": True}

    fields = [field(label, fmt_value(row.get(label)))
              for label in fund_cfg["fields"].values()]

    stmt_cfg = fund_cfg.get("statements", {})
    metrics = stmt_cfg.get("metrics", {})
    if not stmt_cfg.get("enabled") or not metrics:
        return fields

    for key in ("roe", "roic"):
        if key in metrics:
            fields.append(field(metrics[key], _fmt_pct(row.get(metrics[key]))))
    for key in ("operating_margin", "profit_margin"):
        if key in metrics:
            series = row.get(metrics[key])
            fields.append(field(metrics[key] + _year_span(series),
                                _fmt_yearly(series, _fmt_pct)))
    if "fcf" in metrics:
        series = row.get(metrics["fcf"])
        fields.append(field(metrics["fcf"] + _year_span(series),
                            _fmt_yearly(series, fmt_compact)))
    return fields


def _rule_value(row, key: str, fund_cfg: dict):
    """Resolve a quality-rule key to the row's value.

    Rule keys are the same keys used elsewhere in the fundamentals config:
    Yahoo `info` keys for snapshot fields (trailingPE, ...) and metric keys
    for statement metrics (roe, fcf, ...), so renaming a display label
    never breaks a rule.
    """
    if key in fund_cfg.get("fields", {}):
        return row.get(fund_cfg["fields"][key])
    metrics = fund_cfg.get("statements", {}).get("metrics", {})
    if key in metrics:
        return row.get(metrics[key])
    return None


def quality_failures(row, fund_cfg: dict) -> list[str]:
    """Which quality rules (fundamentals.quality.rules) this ticker fails.

    Rule semantics: `min`/`max` are strict compares against the value
    (latest fiscal year for multi-year metrics); `increasing` requires the
    latest year above the previous one. A missing value fails its rule --
    unverifiable quality doesn't earn the badge.
    """
    failed = []
    for key, rule in fund_cfg.get("quality", {}).get("rules", {}).items():
        value = _rule_value(row, key, fund_cfg)
        series = value if isinstance(value, list) else None
        if series is not None:
            value = series[-1][1] if series else None
        ok = isinstance(value, (int, float)) and pd.notna(value)
        if ok and "min" in rule:
            ok = value > rule["min"]
        if ok and "max" in rule:
            ok = value < rule["max"]
        if ok and rule.get("increasing"):
            ok = series is not None and len(series) >= 2 and series[-1][1] > series[-2][1]
        if not ok:
            failed.append(key)
    return failed


def quality_check(row, fund_cfg: dict) -> bool:
    """True when the ticker passes every configured quality rule."""
    if not (fund_cfg.get("enabled") and fund_cfg.get("quality", {}).get("enabled")):
        return False
    return not quality_failures(row, fund_cfg)


def quality_enabled(fund_cfg: dict) -> bool:
    """Whether the tier-2 quality layer is switched on at all."""
    return bool(fund_cfg.get("enabled") and fund_cfg.get("quality", {}).get("enabled"))


def annotate_quality(hits: pd.DataFrame, fund_cfg: dict) -> pd.DataFrame:
    """Record the tier-2 verdict on a hits frame, mirroring Setup/Missing.

    Adds `Quality` (passed every rule?) and `Quality Missing` (the rule keys
    that failed, `[]` when it passed). Computing it here rather than inside
    `build_embeds` gives the badge and the research hand-off **one** source of
    truth -- they used to be a Discord-only decision that the hand-off never
    saw, so nothing downstream could tell whether a ticker earned the star.

    A no-op when the quality layer is disabled: the columns stay *absent*,
    which downstream readers must treat as "not evaluated" rather than as a
    failure. Mutates and returns `hits`.
    """
    if hits.empty or not quality_enabled(fund_cfg):
        return hits
    failures = [quality_failures(row, fund_cfg) for _, row in hits.iterrows()]
    hits[QUALITY_COL] = [not f for f in failures]
    hits[QUALITY_MISSING_COL] = pd.Series(failures, index=hits.index, dtype=object)
    return hits


def build_embeds(module, result: ScanResult, fund_cfg: dict,
                 chart_files: dict[str, Path] = {}) -> list[dict]:
    """One embed card per signal, with its chart image bound in.

    `module` supplies the screen-specific pieces of the registry contract:
    `EMBED_COLOR` and `describe_hit(row, strategy)`. A `partial` setup keeps
    the same card shape but gets the grey `PARTIAL_COLOR` side bar and its
    `Missing` text appended, so one list still shows the tier at a glance.

    The quality badge reads the verdict `annotate_quality` already recorded,
    falling back to computing it only when the column is absent -- so a card
    and the hand-off row behind it can never disagree.
    """
    badge = fund_cfg.get("quality", {}).get("badge", "")

    def passed(row) -> bool:
        value = row.get(QUALITY_COL)
        return bool(value) if value is not None else quality_check(row, fund_cfg)

    def label(ticker, row) -> str:
        """`TICKER (Company Name)` when the name is available, else the ticker."""
        name = row.get(COMPANY_COL)
        if isinstance(name, str) and name.strip():
            name = name.strip()
            if len(name) > 48:
                name = name[:47].rstrip() + "…"
            return f"{ticker} ({name})"
        return str(ticker)

    embeds = []
    for ticker, row in result.hits.iterrows():
        prefix = f"{badge} " if badge and passed(row) else ""
        partial = row.get("Setup") == "partial"
        title = f"{prefix}{label(ticker, row)} -- {result.title}"
        if partial:
            title += " (partial)"
        description = module.describe_hit(row, result.strategy)
        missing = row.get("Missing")
        if partial and isinstance(missing, str) and missing:
            description += f"\n**Missing:** {missing}"
        embed = {
            "title": title,
            "description": description,
            "color": PARTIAL_COLOR if partial else module.EMBED_COLOR,
            "fields": fundamentals_fields(row, fund_cfg),
        }
        if ticker in chart_files:
            embed["image"] = {"url": f"attachment://{chart_files[ticker].name}"}
        embeds.append(embed)
    return embeds


def _embed_size(embed: dict) -> int:
    return (len(embed.get("title", "")) + len(embed.get("description", ""))
            + sum(len(f["name"]) + len(str(f["value"]))
                  for f in embed.get("fields", [])))


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------

def send_discord_alert(content: str, discord_cfg: dict, embeds: list[dict] = (),
                       image_paths: list[Path] = ()) -> None:
    """POST the alert to a Discord webhook (free tier -- no bot needed).

    `content` is the short header/summary text; `embeds` are the per-ticker
    cards (built by build_embeds), batched to respect Discord's limits of 10
    embeds / 10 attachments / ~6000 embed chars per message. Chart files in
    `image_paths` are attached with the batch whose embed references them
    (via attachment://<filename>), so each chart renders inside its card.
    """
    url = discord_cfg["webhook_url"]
    username = discord_cfg.get("username", "Breakout Scanner")
    timeout = discord_cfg.get("request_timeout_seconds", 15)
    paths = {Path(p).name: Path(p) for p in image_paths}

    if not url or "PASTE_YOUR" in url:
        print("\nDiscord webhook URL not configured -- printing message instead:\n")
        print(content)
        for embed in embeds:
            print(f"\n[{embed['title']}]")
            print(embed.get("description", ""))
            print(" | ".join(f"{f['name']} {f['value']}"
                             for f in embed.get("fields", [])))
        return

    batches, current, size = [], [], 0
    for embed in embeds:
        embed_size = _embed_size(embed)
        if current and (len(current) >= DISCORD_MAX_EMBEDS
                        or size + embed_size > DISCORD_EMBED_CHAR_BUDGET):
            batches.append(current)
            current, size = [], 0
        current.append(embed)
        size += embed_size
    if current or not batches:
        batches.append(current)  # a content-only message when no embeds

    for i, batch in enumerate(batches):
        payload = {"username": username}
        if i == 0:
            payload["content"] = content[:DISCORD_CHAR_LIMIT]
        if batch:
            payload["embeds"] = batch

        files = {}
        for embed in batch:
            image_url = embed.get("image", {}).get("url", "")
            name = image_url.removeprefix("attachment://")
            if name != image_url and name in paths:
                files[f"files[{len(files)}]"] = (name, paths[name].read_bytes(),
                                                 "image/png")
        if files:
            resp = requests.post(url, data={"payload_json": json.dumps(payload)},
                                 files=files, timeout=timeout)
        else:
            resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
    print(f"Discord alert sent ({len(batches)} message(s), "
          f"{len(embeds)} card(s), {len(paths)} chart(s)).")


# --------------------------------------------------------------------------
# Research hand-off (Stage 1 -> Stage 2)
# --------------------------------------------------------------------------
# The nightly scan writes latest_hits.json so the deep-dive -- whether the
# nightly unattended run or an on-demand interactive session -- picks up
# exactly what fired, with no Discord read-back. It carries both tiers: the
# technical Setup/Missing and the tier-2 Quality/Quality Missing verdict.

def _json_safe(obj):
    """Recursively convert pandas/numpy row values to JSON-native types.

    NaN/NaT -> None; numpy scalars -> Python scalars; Timestamps -> str;
    the fundamentals' [(year, value)] lists survive as nested arrays.
    """
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    item = getattr(obj, "item", None)  # numpy / pandas scalar
    if callable(item):
        try:
            return _json_safe(obj.item())
        except (ValueError, TypeError):
            pass
    if isinstance(obj, pd.Timestamp):
        return str(obj)
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    return str(obj)


def write_latest_hits(path: Path, scan_date, results) -> dict:
    """Serialize every screen's signal rows to `path` for the deep-dive.

    `results` is the list of (module, ScanResult) the nightly run already
    holds. Rows are whatever each screen put in its hits frame (day stats,
    the Setup/Missing tier columns, the Quality/Quality Missing tier-2
    verdict, and any joined fundamentals), made JSON-safe.

    Returns the payload so `archive_scan` can keep a dated copy without
    re-serializing it.
    """
    payload = {
        "scan_date": str(scan_date),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "screens": [],
    }
    for module, result in results:
        payload["screens"].append({
            "config_key": module.CONFIG_KEY,
            "title": result.title,
            "strategy": _json_safe(result.strategy),
            "hits": {str(t): _json_safe(row.to_dict())
                     for t, row in result.hits.iterrows()},
        })
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                          encoding="utf-8")
    n = sum(len(s["hits"]) for s in payload["screens"])
    print(f"Wrote {path} ({n} ticker row(s) across {len(payload['screens'])} screen(s)).")
    return payload


# --------------------------------------------------------------------------
# Signal history (the long-term record of tiers 1 + 2)
# --------------------------------------------------------------------------
# `latest_hits.json` is overwritten nightly, so without this every scan's
# output is gone within a day and there is nothing to study later. Two forms,
# because they answer different questions: a dated JSON snapshot preserves the
# hand-off exactly as the deep-dive saw it, while one flat CSV is what you
# actually load into pandas to join signals against forward returns.

HISTORY_KEYS = ["scan_date", "config_key", "ticker"]


def history_dir(cfg: dict, create: bool = True) -> Path:
    """Where the signal history lives (`research.history.dir` under output/)."""
    hist_cfg = cfg.get("research", {}).get("history", {})
    path = Path(hist_cfg.get("dir", "history"))
    if not path.is_absolute():
        path = output_dir(create) / path
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _history_rows(payload: dict) -> list[dict]:
    """Flatten one scan payload to one row per (scan_date, screen, ticker)."""
    rows = []
    for screen in payload.get("screens", []):
        for ticker, hit in screen.get("hits", {}).items():
            row = {
                "scan_date": payload.get("scan_date"),
                "generated_at": payload.get("generated_at"),
                "config_key": screen.get("config_key"),
                "screen": screen.get("title"),
                "ticker": ticker,
            }
            for key, value in hit.items():
                # Lists (the [year, value] series, the failed-rule keys) have
                # to survive a CSV round trip -- JSON keeps them re-readable.
                row[key] = json.dumps(value) if isinstance(value, list) else value
            rows.append(row)
    return rows


def archive_scan(payload: dict, cfg: dict) -> None:
    """Append this scan to the permanent record under `output/history/`.

    Writes `hits_<scan_date>.json` (the payload verbatim) and merges the
    flattened rows into `signals.csv`.

    The CSV is rewritten rather than appended, which matters for two reasons:
    the fundamentals columns are *config-driven display labels*, so retuning
    the config changes the schema and a blind append would misalign every
    later row; and de-duplicating on (scan_date, screen, ticker) makes a
    same-day re-run idempotent instead of double-counting it. At a handful of
    rows a night the full rewrite stays trivially cheap for years.
    """
    hist_cfg = cfg.get("research", {}).get("history", {})
    if not hist_cfg.get("enabled", True):
        return
    out = history_dir(cfg)

    scan_date = payload.get("scan_date", "unknown")
    snapshot = out / f"hits_{scan_date}.json"
    snapshot.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")

    rows = _history_rows(payload)
    csv_path = Path(hist_cfg.get("csv", "signals.csv"))
    if not csv_path.is_absolute():
        csv_path = out / csv_path
    frame = pd.DataFrame(rows)
    if csv_path.exists():
        previous = pd.read_csv(csv_path, dtype={"scan_date": str})
        frame = pd.concat([previous, frame], ignore_index=True)
    if not frame.empty:
        frame = frame.drop_duplicates(subset=HISTORY_KEYS, keep="last")
        frame = frame.sort_values(HISTORY_KEYS, kind="stable")
    frame.to_csv(csv_path, index=False, encoding="utf-8")
    print(f"Archived {len(rows)} row(s) to {snapshot.name}; "
          f"{len(frame)} row(s) total in {csv_path.name}.")
