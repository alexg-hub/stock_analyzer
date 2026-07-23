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
# Discord allows at most 10 attachments per webhook message.
DISCORD_MAX_FILES = 10


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

def fetch_fundamentals(tickers: list[str], fields: dict[str, str],
                       percent_fields: list[str] = ()) -> pd.DataFrame:
    """Pull the configured fundamentals from Yahoo for each ticker.

    Fields listed in `percent_fields` come from Yahoo as fractions
    (e.g. revenueGrowth 0.058 = +5.8% latest quarter vs a year ago) and
    are converted to percentages. Only runs on the (small) list of
    hit/near-miss tickers, so a plain loop is fine here.
    """
    rows = {}
    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).info
        except Exception as exc:  # noqa: BLE001 - a bad ticker must not kill the alert
            print(f"  fundamentals failed for {ticker}: {exc}")
            info = {}
        row = {}
        for key, label in fields.items():
            value = info.get(key)
            if key in percent_fields and isinstance(value, (int, float)):
                value *= 100
            row[label] = value
        rows[ticker] = row
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("Ticker")


def fmt_value(value) -> str:
    """n/a-tolerant number formatting shared by all alert sections."""
    return f"{value:.2f}" if isinstance(value, (int, float)) and pd.notna(value) else "n/a"


def fund_suffix(row, fund_labels: list[str]) -> str:
    """' | P/E 29.69 | PEG 4.14 | ...' suffix for one alert line."""
    return "".join(f" | {label} {fmt_value(row.get(label))}" for label in fund_labels)


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------

def send_discord_alert(message: str, discord_cfg: dict,
                       image_paths: list[Path] = ()) -> None:
    """POST the alert to a Discord webhook (free tier -- no bot needed).

    Splits the text on line boundaries to respect Discord's 2000-char limit,
    then attaches chart images (rendered inline by Discord) in batches of at
    most DISCORD_MAX_FILES per follow-up message.
    """
    url = discord_cfg["webhook_url"]
    username = discord_cfg.get("username", "Breakout Scanner")
    timeout = discord_cfg.get("request_timeout_seconds", 15)
    if not url or "PASTE_YOUR" in url:
        print("\nDiscord webhook URL not configured -- printing message instead:\n")
        print(message)
        if image_paths:
            print(f"({len(image_paths)} chart(s) rendered but not sent: "
                  + ", ".join(str(p) for p in image_paths) + ")")
        return

    chunks, current = [], ""
    for line in message.split("\n"):
        if len(current) + len(line) + 1 > DISCORD_CHAR_LIMIT:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    chunks.append(current)

    for chunk in chunks:
        resp = requests.post(
            url,
            json={"content": chunk, "username": username},
            timeout=timeout,
        )
        resp.raise_for_status()
    print(f"Discord alert sent ({len(chunks)} message(s)).")

    image_paths = [Path(p) for p in image_paths]
    for start in range(0, len(image_paths), DISCORD_MAX_FILES):
        batch = image_paths[start:start + DISCORD_MAX_FILES]
        files = {
            f"files[{i}]": (path.name, path.read_bytes(), "image/png")
            for i, path in enumerate(batch)
        }
        resp = requests.post(
            url,
            data={"payload_json": json.dumps({"username": username})},
            files=files,
            timeout=timeout,
        )
        resp.raise_for_status()
    if image_paths:
        print(f"Discord charts sent ({len(image_paths)} image(s)).")
