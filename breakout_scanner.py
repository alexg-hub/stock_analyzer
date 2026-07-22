"""
S&P 500 Breakout-from-Consolidation Scanner
===========================================

Scans every S&P 500 stock for an upward breakout from a horizontal
trading range (consolidation), using fully vectorized pandas operations
on a bulk yfinance download.

Setup detected (all three must be true on the most recent trading day):

1. Horizontal movement -- over the previous `consolidation_window_days`
   trading days (excluding today), (max High - min Low) / min Low must be
   <= `max_consolidation_range_pct`.
2. Upward breakout -- today's Close strictly exceeds the max High of that
   prior window.
3. Volume surge -- today's Volume >= `volume_surge_multiplier` x the SMA
   of Volume over the previous `volume_sma_days` trading days.

Hits are enriched with fundamentals (P/E, PEG, Total Debt/Equity) from
Yahoo Finance and pushed to a Discord channel via a standard webhook
(free tier, no bot required).

All parameters live in config.json next to this script.

Usage:
    pip install -r requirements.txt
    python breakout_scanner.py
"""

import io
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

CONFIG_PATH = Path(__file__).with_name("config.json")

# Discord hard limit is 2000 chars per message; stay under it so long
# hit lists get split across several messages instead of being rejected.
DISCORD_CHAR_LIMIT = 1900


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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


# --------------------------------------------------------------------------
# Vectorized breakout screen
# --------------------------------------------------------------------------

def find_breakouts(data: pd.DataFrame, strategy: dict) -> pd.DataFrame:
    """Apply all three breakout conditions across every ticker at once.

    Each of close/high/low/volume below is a DataFrame of shape
    (days, tickers); every rolling/comparison operates on the whole
    universe simultaneously -- no per-ticker loops.

    Returns a DataFrame (indexed by ticker) of today's stats for the
    tickers that pass all three conditions.
    """
    window = strategy["consolidation_window_days"]
    max_range = strategy["max_consolidation_range_pct"]
    vol_days = strategy["volume_sma_days"]
    vol_mult = strategy["volume_surge_multiplier"]

    close = data["Close"]
    high = data["High"]
    low = data["Low"]
    volume = data["Volume"]

    # Prior-window extremes: shift(1) excludes the current day, so the
    # 126-day range is measured strictly before the potential breakout day.
    prior_high = high.shift(1).rolling(window).max()
    prior_low = low.shift(1).rolling(window).min()

    # Condition 1: tight horizontal range over the prior window.
    range_pct = (prior_high - prior_low) / prior_low
    is_consolidating = range_pct <= max_range

    # Condition 2: today's close strictly above the prior window's high.
    is_breakout = close > prior_high

    # Condition 3: volume surge vs. the prior 30-day average volume.
    prior_vol_sma = volume.shift(1).rolling(vol_days).mean()
    is_volume_surge = volume >= vol_mult * prior_vol_sma

    # NaNs (insufficient history / dead tickers) compare as False, so
    # they drop out automatically.
    signal_today = (is_consolidating & is_breakout & is_volume_surge).iloc[-1]
    hits = signal_today[signal_today.fillna(False)].index.tolist()

    scan_date = data.index[-1].date()
    print(f"Scan date: {scan_date} -- {len(hits)} breakout(s) found.")

    return pd.DataFrame(
        {
            "Close": close.iloc[-1][hits].round(2),
            "Range High": prior_high.iloc[-1][hits].round(2),
            "Range %": (range_pct.iloc[-1][hits] * 100).round(1),
            "Vol Ratio": (volume.iloc[-1] / prior_vol_sma.iloc[-1])[hits].round(2),
        },
        index=pd.Index(hits, name="Ticker"),
    )


# --------------------------------------------------------------------------
# Fundamentals for the hits
# --------------------------------------------------------------------------

def fetch_fundamentals(tickers: list[str], fields: dict[str, str]) -> pd.DataFrame:
    """Pull P/E, PEG and Total Debt/Equity from Yahoo for each hit.

    Only runs on the (small) list of breakout tickers, so a plain loop
    is fine here.
    """
    rows = {}
    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).info
        except Exception as exc:  # noqa: BLE001 - a bad ticker must not kill the alert
            print(f"  fundamentals failed for {ticker}: {exc}")
            info = {}
        rows[ticker] = {label: info.get(key) for key, label in fields.items()}
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("Ticker")


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------

def format_message(results: pd.DataFrame, scan_date) -> str:
    header = f"**S&P 500 Breakout Scan -- {scan_date}**\n"
    if results.empty:
        return header + "No stocks broke out of consolidation today."

    lines = [header, f"{len(results)} breakout(s) from consolidation:\n"]
    for ticker, row in results.iterrows():
        def fmt(value):
            return f"{value:.2f}" if isinstance(value, (int, float)) and pd.notna(value) else "n/a"

        lines.append(
            f"**{ticker}** | Close {fmt(row['Close'])} broke range high {fmt(row['Range High'])} "
            f"(range {fmt(row['Range %'])}%, vol {fmt(row['Vol Ratio'])}x avg) | "
            f"P/E {fmt(row.get('P/E'))} | PEG {fmt(row.get('PEG'))} | "
            f"Debt/Eq {fmt(row.get('Total Debt/Equity'))}"
        )
    return "\n".join(lines)


def send_discord_alert(message: str, discord_cfg: dict) -> None:
    """POST the alert to a Discord webhook (free tier -- no bot needed).

    Splits on line boundaries to respect Discord's 2000-char limit.
    """
    url = discord_cfg["webhook_url"]
    if not url or "PASTE_YOUR" in url:
        print("\nDiscord webhook URL not configured -- printing message instead:\n")
        print(message)
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
            json={"content": chunk, "username": discord_cfg.get("username", "Breakout Scanner")},
            timeout=discord_cfg.get("request_timeout_seconds", 15),
        )
        resp.raise_for_status()
    print(f"Discord alert sent ({len(chunks)} message(s)).")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    cfg = load_config()

    tickers = get_sp500_tickers(cfg["data"]["sp500_source_url"])
    data = download_price_data(
        tickers,
        period=cfg["data"]["download_period"],
        interval=cfg["data"]["download_interval"],
    )

    results = find_breakouts(data, cfg["strategy"])
    scan_date = data.index[-1].date()

    if not results.empty and cfg["fundamentals"]["enabled"]:
        print("Fetching fundamentals for breakout tickers...")
        fundamentals = fetch_fundamentals(
            results.index.tolist(), cfg["fundamentals"]["fields"]
        )
        results = results.join(fundamentals)
        print(results.to_string())

    if results.empty and not cfg["discord"]["send_message_when_no_breakouts"]:
        print("No breakouts and no-breakout alerts are disabled -- done.")
        return 0

    send_discord_alert(format_message(results, scan_date), cfg["discord"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
