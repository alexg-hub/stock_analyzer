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
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

CONFIG_PATH = Path(__file__).with_name("config.json")

# Discord hard limit is 2000 chars per message; stay under it so long
# hit lists get split across several messages instead of being rejected.
DISCORD_CHAR_LIMIT = 1900
# Discord allows at most 10 embeds and 10 attachments per webhook message,
# and at most 6000 chars across all embeds of one message.
DISCORD_MAX_EMBEDS = 10
DISCORD_EMBED_CHAR_BUDGET = 5500

# Embed side-bar color for near-miss cards (screens define their own).
NEAR_MISS_COLOR = 0x898781


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Scan result contract (what every screen module's scan() returns)
# --------------------------------------------------------------------------

@dataclass
class ScanResult:
    """Output of one screen over the whole universe on the scan day.

    `hits`/`near` are ticker-indexed DataFrames (near may be empty for
    screens without a near-miss concept). `strategy` is the config section
    the screen ran with, kept here so formatting/plotting stay config-driven.
    """
    title: str
    hits: pd.DataFrame
    near: pd.DataFrame = field(default_factory=pd.DataFrame)
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
    return data


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
    return data


def single_ticker_panel(data: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Slice one ticker out of a bulk (Field, Ticker) frame, keeping the
    MultiIndex layout so the compute_* functions run unchanged."""
    return data.loc[:, pd.IndexSlice[:, [ticker]]]


# --------------------------------------------------------------------------
# Fundamentals for the hits
# --------------------------------------------------------------------------

def _stmt_value(df: pd.DataFrame, row_name: str, column) -> float | None:
    """One cell of an annual statement, or None when the row is missing
    (banks have no Operating Income, negative-equity companies etc.)."""
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
            [(c.year, _stmt_value(cashflow, "Free Cash Flow", c)) for c in cf_cols]
        )
    if "operating_margin" in metrics or "profit_margin" in metrics:
        def margin(numerator_row):
            series = []
            for c in inc_cols:
                num = _stmt_value(income, numerator_row, c)
                rev = _stmt_value(income, "Total Revenue", c)
                series.append((c.year, 100 * num / rev if num is not None and rev else None))
            return _yearly_series(series)

        if "operating_margin" in metrics:
            row[metrics["operating_margin"]] = margin("Operating Income")
        if "profit_margin" in metrics:
            row[metrics["profit_margin"]] = margin("Net Income")

    if "roe" in metrics:
        roe = None
        for c in reversed(inc_cols):  # latest year with both rows present
            net = _stmt_value(income, "Net Income", c)
            equity = _stmt_value(balance, "Stockholders Equity", c)
            if net is not None and equity:
                roe = 100 * net / equity
                break
        if roe is None and isinstance(info.get("returnOnEquity"), (int, float)):
            roe = 100 * info["returnOnEquity"]
        row[metrics["roe"]] = roe

    if "roic" in metrics:
        roic = None
        for c in reversed(inc_cols):
            ebit = _stmt_value(income, "EBIT", c)
            tax = _stmt_value(income, "Tax Provision", c)
            pretax = _stmt_value(income, "Pretax Income", c)
            invested = _stmt_value(balance, "Invested Capital", c)
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

    Only runs on the (small) list of hit/near-miss tickers, so a plain
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
        row = {}
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


def build_embeds(module, result: ScanResult, fund_cfg: dict,
                 chart_files: dict[str, Path] = {}) -> list[dict]:
    """One embed card per hit (chart image bound in) and per near-miss.

    `module` supplies the screen-specific pieces of the registry contract:
    `EMBED_COLOR` and `describe_hit(row, strategy)`.
    """
    badge = fund_cfg.get("quality", {}).get("badge", "")
    embeds = []
    for ticker, row in result.hits.iterrows():
        prefix = f"{badge} " if badge and quality_check(row, fund_cfg) else ""
        embed = {
            "title": f"{prefix}{ticker} -- {result.title}",
            "description": module.describe_hit(row, result.strategy),
            "color": module.EMBED_COLOR,
            "fields": fundamentals_fields(row, fund_cfg),
        }
        if ticker in chart_files:
            embed["image"] = {"url": f"attachment://{chart_files[ticker].name}"}
        embeds.append(embed)
    for ticker, row in result.near.iterrows():
        embed = {
            "title": f"{ticker} -- near miss ({result.title})",
            "description": row["Reason"],
            "color": NEAR_MISS_COLOR,
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
