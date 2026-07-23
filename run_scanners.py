"""
Nightly S&P 500 scan -- entry point.

Downloads the whole universe once, runs every enabled screen module over
it, and sends one combined Discord alert (text sections + a chart image
per hit).

Adding a new scanner:
  1. write a module exposing CONFIG_KEY, scan(), format_section(), plot_hit()
     (see breakout_scanner.py / sma_pullback.py);
  2. add it to SCANNERS below;
  3. add its config section (with an "enabled" flag) to config.json.

Usage:
    pip install -r requirements.txt
    python run_scanners.py
"""

import sys
import tempfile
from pathlib import Path

import breakout_scanner
import sma_pullback
from scanner_common import (
    download_price_data,
    fetch_fundamentals,
    get_sp500_tickers,
    load_config,
    send_discord_alert,
)

# Every screen that runs nightly, in alert order.
SCANNERS = [breakout_scanner, sma_pullback]


def main() -> int:
    cfg = load_config()

    tickers = get_sp500_tickers(cfg["data"]["sp500_source_url"])
    data = download_price_data(
        tickers,
        period=cfg["data"]["download_period"],
        interval=cfg["data"]["download_interval"],
    )
    scan_date = data.index[-1].date()

    # -- run every enabled screen on the one shared download --
    results = []  # (module, ScanResult)
    for module in SCANNERS:
        strategy = cfg.get(module.CONFIG_KEY)
        if not strategy:
            print(f"No '{module.CONFIG_KEY}' section in config.json -- skipping.")
            continue
        if not strategy.get("enabled", True):
            print(f"'{module.CONFIG_KEY}' is disabled -- skipping.")
            continue
        results.append((module, module.scan(data, strategy)))

    # -- one fundamentals pass for every hit/near ticker of every screen --
    fund_cfg = cfg["fundamentals"]
    wanted = []
    for _, result in results:
        for ticker in list(result.hits.index) + list(result.near.index):
            if ticker not in wanted:
                wanted.append(ticker)
    if fund_cfg["enabled"] and wanted:
        print("Fetching fundamentals for hit + near-miss tickers...")
        fundamentals = fetch_fundamentals(wanted, fund_cfg)
        for _, result in results:
            result.hits = result.hits.join(fundamentals)
            if not result.near.empty:
                result.near = result.near.join(fundamentals)

    for _, result in results:
        if not result.hits.empty:
            print(result.hits.to_string())
        if not result.near.empty:
            print(result.near.to_string())

    all_empty = all(r.hits.empty and r.near.empty for _, r in results)
    if all_empty and not cfg["discord"]["send_message_when_no_breakouts"]:
        print("Nothing found by any screen and empty alerts are disabled -- done.")
        return 0

    # -- compose one message from each screen's section --
    sections = [module.format_section(result, fund_cfg)
                for module, result in results]
    message = "\n\n".join([f"**S&P 500 Scan -- {scan_date}**"] + sections)

    # -- one chart per hit, attached to the alert --
    image_paths = []
    chart_cfg = cfg.get("charts", {})
    if chart_cfg.get("enabled", True):
        chart_dir = Path(tempfile.mkdtemp(prefix="scanner_charts_"))
        for module, result in results:
            for ticker in result.hits.index:
                out_path = chart_dir / f"{module.CONFIG_KEY}_{ticker}.png"
                try:
                    module.plot_hit(data, ticker, result.strategy, chart_cfg, out_path)
                    image_paths.append(out_path)
                except Exception as exc:  # noqa: BLE001 - a chart must not kill the alert
                    print(f"  chart failed for {ticker}: {exc}")

    send_discord_alert(message, cfg["discord"], image_paths)
    return 0


if __name__ == "__main__":
    sys.exit(main())
