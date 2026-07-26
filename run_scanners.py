"""
Nightly S&P 500 scan -- entry point.

Downloads the whole universe once, runs every enabled screen module over
it, and sends one combined Discord alert (text sections + a chart image
per hit).

`scan_ticker()` runs the same registry over a one-ticker universe for the
on-demand path (`research_report.py scan TICKER`), returning the identical
hand-off shape so tier 3 cannot tell the two apart.

Adding a new scanner:
  1. write a module exposing CONFIG_KEY, scan(), EMBED_COLOR + describe_hit(),
     plot_hit() (see breakout_scanner.py / sma_pullback.py / sma_reclaim.py);
  2. add it to SCANNERS below;
  3. add its config section (with an "enabled" flag) to config.json.

Usage:
    pip install -r requirements.txt
    python run_scanners.py
"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import breakout_scanner
import sma_pullback
import sma_reclaim
from scanner_common import (
    ScanResult,
    annotate_quality,
    archive_scan,
    build_embeds,
    build_hits_payload,
    download_price_data,
    enable_utf8_output,
    fetch_fundamentals,
    get_sp500_tickers,
    load_config,
    output_dir,
    send_discord_alert,
    write_latest_hits,
)

# Every screen that runs nightly, in alert order.
SCANNERS = [breakout_scanner, sma_pullback, sma_reclaim]

# The screen entry a ticker gets when nothing fired: an on-demand look is
# mostly about tier 2, so the payload still needs somewhere to put the row.
NO_SIGNAL_KEY = "on_demand"
NO_SIGNAL_TITLE = "No active technical signal"


def scan_ticker(ticker: str, cfg: dict) -> dict:
    """Run tiers 1 and 2 for one named ticker, on demand.

    Returns a payload in exactly the `latest_hits.json` shape, so
    `research_report.find_ticker` and everything behind it handle an ad-hoc
    look and a nightly signal without knowing which they got.

    Two deliberate differences from the nightly scan:

    * **`enabled` is ignored**, as `backtest_universe.py` and `tune_screen.py`
      already ignore it. That flag gates the nightly *alert*; here you asked
      about this ticker specifically, and a screen is switched off precisely
      when you most want to see what it would have said. Its title says so.
    * **Tier 2 is graded whether or not a screen fires.** The quality check is
      the point of an on-demand look, so when nothing fires the payload still
      carries one row -- `Setup: "none"` -- holding the fundamentals.
    """
    ticker = ticker.upper()
    data = download_price_data(
        [ticker],
        period=cfg["data"]["download_period"],
        interval=cfg["data"]["download_interval"],
    )
    # From the price data, never from latest_hits.json: this is the last
    # *settled* bar (drop_unsettled_tail already ran), and it makes a re-look
    # next week a new row rather than a collision with today's.
    scan_date = data.index[-1].date()

    results = []
    for module in SCANNERS:
        strategy = cfg.get(module.CONFIG_KEY)
        if not strategy:
            print(f"No '{module.CONFIG_KEY}' section in config.json -- skipping.")
            continue
        result = module.scan(data, strategy)
        if result.hits.empty:
            continue
        if not strategy.get("enabled", True):
            result.title += " [screen disabled nightly]"
        results.append((module, result))

    if not results:
        print(f"{ticker}: no screen fires today -- grading fundamentals only.")
        row = pd.DataFrame(index=pd.Index([ticker], name="Ticker"))
        row["Setup"] = "none"
        row["Missing"] = ""
        results = [(SimpleNamespace(CONFIG_KEY=NO_SIGNAL_KEY),
                    ScanResult(title=NO_SIGNAL_TITLE, hits=row))]

    # -- tier 2, the same two steps and the same single grading call as main() --
    fund_cfg = cfg["fundamentals"]
    if fund_cfg["enabled"]:
        fundamentals = fetch_fundamentals([ticker], fund_cfg)
        for _, result in results:
            result.hits = result.hits.join(fundamentals)
    for _, result in results:
        annotate_quality(result.hits, fund_cfg)

    return build_hits_payload(scan_date, results)


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

    # -- one fundamentals pass for every signalling ticker of every screen --
    fund_cfg = cfg["fundamentals"]
    wanted = []
    for _, result in results:
        for ticker in result.hits.index:
            if ticker not in wanted:
                wanted.append(ticker)
    if fund_cfg["enabled"] and wanted:
        print("Fetching fundamentals for signalling tickers...")
        fundamentals = fetch_fundamentals(wanted, fund_cfg)
        for _, result in results:
            result.hits = result.hits.join(fundamentals)

    # -- tier 2: grade the fundamentals once, here. Both the Discord badge and
    #    the hand-off read the recorded verdict, so they cannot disagree --
    for _, result in results:
        annotate_quality(result.hits, fund_cfg)

    for _, result in results:
        if not result.hits.empty:
            print(result.hits.to_string())

    # -- hand-off for the deep-dive: always written (even when empty) so the
    #    nightly tier-3 run and any interactive session see exactly what fired
    #    tonight, then archived under output/history/ so it survives tomorrow's
    #    scan overwriting it --
    research_cfg = cfg.get("research", {})
    hits_path = Path(research_cfg.get("latest_hits_path", "latest_hits.json"))
    if not hits_path.is_absolute():
        hits_path = output_dir() / hits_path
    payload = write_latest_hits(hits_path, scan_date, results)
    archive_scan(payload, cfg)

    all_empty = all(r.hits.empty for _, r in results)
    if all_empty and not cfg["discord"]["send_message_when_no_breakouts"]:
        print("Nothing found by any screen and empty alerts are disabled -- done.")
        return 0

    # -- one chart per signal (partial setups included unless disabled),
    #    rendered first so its embed can reference it --
    chart_cfg = cfg.get("charts", {})
    chart_files = {}  # module CONFIG_KEY -> {ticker: Path}
    if chart_cfg.get("enabled", True):
        chart_dir = Path(tempfile.mkdtemp(prefix="scanner_charts_"))
        for module, result in results:
            per_screen = {}
            hits = result.hits
            if not chart_cfg.get("partial_charts", True) and "Setup" in hits:
                hits = hits[hits["Setup"] != "partial"]
            for ticker in hits.index:
                out_path = chart_dir / f"{module.CONFIG_KEY}_{ticker}.png"
                try:
                    module.plot_hit(data, ticker, result.strategy, chart_cfg, out_path)
                    per_screen[ticker] = out_path
                except Exception as exc:  # noqa: BLE001 - a chart must not kill the alert
                    print(f"  chart failed for {ticker}: {exc}")
            chart_files[module.CONFIG_KEY] = per_screen

    # -- header/summary text + one embed card per ticker --
    summary = [f"**S&P 500 Scan -- {scan_date}**"]
    embeds, image_paths = [], []
    for module, result in results:
        if result.hits.empty:
            summary.append(f"{result.title}: nothing today.")
            continue
        n_full = int((result.hits["Setup"] == "full").sum())
        n_partial = len(result.hits) - n_full
        counts = f"{len(result.hits)} signal(s) ({n_full} full"
        counts += f", {n_partial} partial)" if n_partial else ")"
        summary.append(f"{result.title}: {counts}")
        per_screen = chart_files.get(module.CONFIG_KEY, {})
        embeds += build_embeds(module, result, fund_cfg, per_screen)
        image_paths += per_screen.values()

    send_discord_alert("\n".join(summary), cfg["discord"], embeds, image_paths)
    return 0


if __name__ == "__main__":
    enable_utf8_output()   # the nightly log is a redirect, i.e. a codepage stream
    sys.exit(main())
