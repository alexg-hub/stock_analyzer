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
2. Upward breakout -- today's Close exceeds `breakout_multiplier` x the max
   High of that prior window (e.g. 1.01 = closes at least 1% above it).
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

def compute_signals(data: pd.DataFrame, strategy: dict) -> dict[str, pd.DataFrame]:
    """Compute every breakout condition for every day and every ticker.

    Each of close/high/low/volume below is a DataFrame of shape
    (days, tickers); every rolling/comparison operates on the whole
    universe simultaneously -- no per-ticker loops.

    Returns a dict of (days, tickers) DataFrames: the intermediate
    series, the three boolean conditions, and the combined `signal`.
    """
    window = strategy["consolidation_window_days"]
    max_range = strategy["max_consolidation_range_pct"]
    brk_mult = strategy.get("breakout_multiplier", 1.0)
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

    # Condition 2: today's close clears the prior window's high by the
    # breakout multiplier (e.g. 1.01 = at least 1% above the range high).
    is_breakout = close > brk_mult * prior_high

    # Condition 3: volume surge vs. the prior 30-day average volume.
    prior_vol_sma = volume.shift(1).rolling(vol_days).mean()
    vol_ratio = volume / prior_vol_sma
    is_volume_surge = volume >= vol_mult * prior_vol_sma

    # NaNs (insufficient history / dead tickers) compare as False, so
    # they drop out automatically.
    return {
        "prior_high": prior_high,
        "prior_low": prior_low,
        "range_pct": range_pct,
        "prior_vol_sma": prior_vol_sma,
        "vol_ratio": vol_ratio,
        "is_consolidating": is_consolidating,
        "is_breakout": is_breakout,
        "is_volume_surge": is_volume_surge,
        "signal": is_consolidating & is_breakout & is_volume_surge,
    }


def near_miss_reason(close, prior_high, range_pct, vol_ratio, strategy: dict) -> str:
    """Explain which single condition a 2-of-3 near-miss failed.

    `range_pct` is a fraction (0.28 = 28%), matching compute_signals.
    """
    max_range = strategy["max_consolidation_range_pct"]
    brk_mult = strategy.get("breakout_multiplier", 1.0)
    vol_mult = strategy["volume_surge_multiplier"]
    if range_pct > max_range:
        return f"range too wide: {range_pct * 100:.1f}% > {max_range:.0%} limit"
    if close <= brk_mult * prior_high:
        return (f"no breakout: Close {close:.2f} <= {brk_mult} x prior high "
                f"{prior_high:.2f} = {brk_mult * prior_high:.2f}")
    return f"volume too low: {vol_ratio:.2f}x < {vol_mult}x required"


def find_breakouts(data: pd.DataFrame, strategy: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Screen the whole universe on the most recent trading day.

    Returns two DataFrames indexed by ticker:
      * hits -- tickers passing all three conditions;
      * near-misses -- tickers passing exactly two, with the failed
        condition explained (C2 failures only when the close is within
        `near_miss_max_gap_pct` of the required breakout level, so a
        volume spike deep inside a range doesn't spam the alert).
    """
    window = strategy["consolidation_window_days"]
    if len(data) <= window:
        print(f"WARNING: only {len(data)} rows of history for a {window}-day "
              f"consolidation window -- the rolling window never fills, so NO "
              f"signal can ever fire. Increase data.download_period in config.json.")

    signals = compute_signals(data, strategy)
    last = {name: df.iloc[-1] for name, df in signals.items()}

    def day_stats(tickers: list[str]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Close": data["Close"].iloc[-1][tickers].round(2),
                "Range High": last["prior_high"][tickers].round(2),
                "Range %": (last["range_pct"][tickers] * 100).round(1),
                "Vol Ratio": last["vol_ratio"][tickers].round(2),
            },
            index=pd.Index(tickers, name="Ticker"),
        )

    signal_today = last["signal"].fillna(False)
    hits = day_stats(signal_today[signal_today].index.tolist())

    # Near-misses: exactly two of the three conditions true today.
    conds = pd.DataFrame(
        {c: last[c].fillna(False)
         for c in ("is_consolidating", "is_breakout", "is_volume_surge")}
    )
    near_mask = (conds.sum(axis=1) == 2) & ~signal_today
    # For C2 failures, require the close to be near the breakout level.
    gap = strategy.get("near_miss_max_gap_pct", 0.05)
    brk_level = strategy.get("breakout_multiplier", 1.0) * last["prior_high"]
    near_mask &= conds["is_breakout"] | (data["Close"].iloc[-1] >= (1 - gap) * brk_level)

    near = day_stats(near_mask[near_mask].index.tolist())
    near["Reason"] = [
        near_miss_reason(row["Close"], row["Range High"], row["Range %"] / 100,
                         row["Vol Ratio"], strategy)
        for _, row in near.iterrows()
    ]

    scan_date = data.index[-1].date()
    print(f"Scan date: {scan_date} -- {len(hits)} breakout(s), "
          f"{len(near)} near-miss candidate(s).")
    return hits, near


# --------------------------------------------------------------------------
# Fundamentals for the hits
# --------------------------------------------------------------------------

def fetch_fundamentals(tickers: list[str], fields: dict[str, str],
                       percent_fields: list[str] = ()) -> pd.DataFrame:
    """Pull the configured fundamentals from Yahoo for each ticker.

    Fields listed in `percent_fields` come from Yahoo as fractions
    (e.g. revenueGrowth 0.058 = +5.8% latest quarter vs a year ago) and
    are converted to percentages. Only runs on the (small) list of
    breakout/near-miss tickers, so a plain loop is fine here.
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


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------

def format_message(results: pd.DataFrame, near: pd.DataFrame, scan_date,
                  fund_labels: list[str] = ()) -> str:
    def fmt(value):
        return f"{value:.2f}" if isinstance(value, (int, float)) and pd.notna(value) else "n/a"

    def fund_suffix(row) -> str:
        return "".join(f" | {label} {fmt(row.get(label))}" for label in fund_labels)

    header = f"**S&P 500 Breakout Scan -- {scan_date}**\n"
    if results.empty and near.empty:
        return header + "No stocks broke out of consolidation today, and no near-misses."

    lines = [header]
    if results.empty:
        lines.append("No confirmed breakouts today.")
    else:
        lines.append(f"{len(results)} breakout(s) from consolidation:\n")
        for ticker, row in results.iterrows():
            lines.append(
                f"**{ticker}** | Close {fmt(row['Close'])} broke range high {fmt(row['Range High'])} "
                f"(range {fmt(row['Range %'])}%, vol {fmt(row['Vol Ratio'])}x avg)"
                + fund_suffix(row)
            )

    if not near.empty:
        lines.append(f"\n{len(near)} near-miss candidate(s) (failed one condition):")
        for ticker, row in near.iterrows():
            lines.append(f"**{ticker}** | {row['Reason']}{fund_suffix(row)}")
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

    results, near = find_breakouts(data, cfg["strategy"])
    scan_date = data.index[-1].date()

    fund_cfg = cfg["fundamentals"]
    fund_labels = list(fund_cfg["fields"].values()) if fund_cfg["enabled"] else []
    wanted = results.index.tolist() + near.index.tolist()
    if fund_cfg["enabled"] and wanted:
        print("Fetching fundamentals for breakout + near-miss tickers...")
        fundamentals = fetch_fundamentals(
            wanted, fund_cfg["fields"], fund_cfg.get("percent_fields", [])
        )
        results = results.join(fundamentals)
        near = near.join(fundamentals)
    if not results.empty:
        print(results.to_string())
    if not near.empty:
        print(near.to_string())

    if results.empty and near.empty and not cfg["discord"]["send_message_when_no_breakouts"]:
        print("No breakouts, no near-misses, and no-breakout alerts are disabled -- done.")
        return 0

    send_discord_alert(format_message(results, near, scan_date, fund_labels), cfg["discord"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
