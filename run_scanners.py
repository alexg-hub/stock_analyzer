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
    python run_scanners.py              # the production path -- posts to Discord
    python run_scanners.py --no-send    # same scan, cards printed instead of posted
"""

import argparse
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import breakout_scanner
import quality
import sma_pullback
import sma_reclaim
from scanner_common import (
    DISCLAIMER,
    QUALITY_COL,
    VETO_COL,
    VETO_REASONS_COL,
    ScanResult,
    archive_scan,
    build_embeds,
    build_hits_payload,
    download_price_data,
    enable_utf8_output,
    get_sp500_tickers,
    load_config,
    log_step,
    output_dir,
    run_id,
    send_discord_alert,
    write_latest_hits,
)

# Every screen that runs nightly, in alert order.
SCANNERS = [breakout_scanner, sma_pullback, sma_reclaim]

# The screen entry a ticker gets when nothing fired: an on-demand look is
# mostly about tier 2, so the payload still needs somewhere to put the row.
NO_SIGNAL_KEY = "on_demand"
NO_SIGNAL_TITLE = "No active technical signal"


def parse_args(argv: list[str] | None = None):
    """The nightly scan's only CLI surface.

    Deliberately one flag. `run_scanner.bat` and Task Scheduler invoke this
    with no arguments and must keep behaving exactly as before, so anything
    added here has to be optional and default to the production path.
    """
    parser = argparse.ArgumentParser(
        description="Nightly S&P 500 scan (tiers 1 and 2).")
    parser.add_argument(
        "--no-send", action="store_true",
        help="print the Discord cards instead of posting them (dry run)")
    return parser.parse_args(argv)


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
    # *settled* bar (drop_unsettled_bars already ran), and it makes a re-look
    # next week a new row rather than a collision with today's.
    scan_date = data.index[-1].date()

    results = []
    for module in SCANNERS:
        strategy = cfg.get(module.CONFIG_KEY)
        if not strategy:
            log_step("SCREEN", "skip",
                     f"no '{module.CONFIG_KEY}' section in config.json", cfg=cfg)
            continue
        result = module.scan(data, strategy)
        if result.hits.empty:
            continue
        if not strategy.get("enabled", True):
            result.title += " [screen disabled nightly]"
        results.append((module, result))

    if not results:
        log_step("SCREEN", "none", f"{ticker}: no screen fires -- tier 2 only",
                 cfg=cfg)
        row = pd.DataFrame(index=pd.Index([ticker], name="Ticker"))
        row["Setup"] = "none"
        row["Missing"] = ""
        results = [(SimpleNamespace(CONFIG_KEY=NO_SIGNAL_KEY),
                    ScanResult(title=NO_SIGNAL_TITLE, hits=row))]

    # -- tier 2, the same two steps and the same single grading call as main() --
    # Same panel reuse as the nightly path, so an on-demand look reads the price
    # risk off the bars it just downloaded rather than fetching them twice.
    fundamentals = quality.fetch_fast([ticker], cfg, closes=data.get("Close"))
    if not fundamentals.empty:
        for _, result in results:
            result.hits = result.hits.join(fundamentals)
    for _, result in results:
        quality.annotate(result.hits, cfg)
    log_quality(results, cfg)

    return build_hits_payload(scan_date, results)


def log_quality(results: list, cfg: dict) -> None:
    """One `QUALITY` line: how many of the tier-1 hits tier 2 passed.

    Counted off the recorded `Quality` column rather than re-grading, so the
    log states the same verdict the badge and the hand-off carry -- the whole
    reason grading happens exactly once.
    """
    if not quality.is_enabled(cfg):
        log_step("QUALITY", "skip", "quality layer is off -- not evaluated",
                 cfg=cfg)
        return
    graded = {t: bool(v) for _, result in results
              for t, v in result.hits.get(QUALITY_COL, {}).items()}
    passed = sum(graded.values())
    log_step("QUALITY", "ok" if graded else "none",
             f"{passed}/{len(graded)} ticker(s) passed"
             + (f" -- {', '.join(t for t, ok in graded.items() if ok)}"
                if passed else ""),
             cfg=cfg)

    # The exclusion half, logged separately because it answers a different
    # question and is meant to be rare enough that every firing is worth a line.
    vetoed = {t: list(v or []) for _, result in results
              for t, v in result.hits.get(VETO_REASONS_COL, {}).items() if v}
    log_step("VETO", "ok" if vetoed else "none",
             f"{len(vetoed)}/{len(graded)} ticker(s) excluded"
             + (" -- " + "; ".join(f"{t}: {quality.veto_text(r, cfg)}"
                                   for t, r in vetoed.items()) if vetoed else ""),
             cfg=cfg)


def run_ledger(cfg: dict) -> tuple[list, list]:
    """Tier 4 in-process: open tonight's signals, price the book, scan for exits.

    Returns the exit cards and their charts for the combined alert.

    `open` first and on its own try/except so the signal is recorded even if the
    download in `mark` fails -- the ledger is the thing that cannot be
    reconstructed later, the prices always can. Every step swallows its own
    failure for the same reason `portfolio_sim/__main__.py` exits 0 on error: a
    broken ledger must never take down the alert. This used to be three separate
    lines in `run_scanner.bat`; it is here now because the exit cards have to
    exist before the message is built.
    """
    if not cfg.get("portfolio", {}).get("enabled", True):
        log_step("LEDGER", "off", "portfolio.enabled is false", cfg=cfg)
        return [], []

    from portfolio_sim import exits, ledger, marking

    for name, fn in (("open", ledger.sync), ("mark", marking.mark)):
        try:
            fn(cfg)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not sink the scan
            log_step("LEDGER", "failed",
                     f"{name}: {type(exc).__name__}: {exc}", cfg=cfg)

    try:
        _, embeds, charts = exits.exit_scan(cfg, send=False, collect=True)
        return embeds, list(charts)
    except Exception as exc:  # noqa: BLE001
        log_step("EXIT", "failed", f"{type(exc).__name__}: {exc}", cfg=cfg)
        return [], []


def carry_verdicts_to_ledger(cfg: dict) -> None:
    """Re-sync the ledger after the verdicts are recorded.

    Ordering problem this closes: `run_ledger` has to run *before*
    `run_verdicts` (the exit cards must exist before the message is built, and
    the exit scan needs filled entry prices), but the verdict is written to
    `signals.csv` after that -- so without this the tier and conviction would
    sit on the position only from tomorrow's run. The verdict is exactly the
    attribute tier 4 exists to grade, and it used to be the trailing `mark` in
    `run_deepdive.bat` that carried it across; that pass is optional now, so it
    can no longer be relied on.

    `sync` alone, not `mark`: it copies the source row verbatim (verdict
    columns included) and needs no network, which the prices already had.
    """
    if not cfg.get("portfolio", {}).get("enabled", True):
        return
    from portfolio_sim import ledger

    try:
        ledger.sync(cfg)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not sink the alert
        log_step("LEDGER", "failed",
                 f"verdict carry: {type(exc).__name__}: {exc}", cfg=cfg)


def run_verdicts(payload: dict, cfg: dict) -> tuple[list, list, list]:
    """Tier 3's deterministic half for tonight's gated candidates.

    The verdict is `conviction = quant score`, so it exists the moment the scan
    finishes and can travel in the same message as the signal. It is recorded to
    `output/history/` either way -- the record is the point, the notification is
    not -- and the cards are withheld when `research.auto.discord_send` is
    false, which is the same switch that used to gate `post-verdicts --send`.

    A failure here costs the verdict section and nothing else: the signals, the
    charts and the exits have already been built.
    """
    auto = cfg.get("research", {}).get("auto", {})
    if not auto.get("enabled", True):
        log_step("VERDICT", "off", "research.auto.enabled is false", cfg=cfg)
        return [], [], []

    import research_report

    try:
        candidates = research_report.list_candidates(
            payload, cfg, limit=auto.get("max_reports"))
        # One verdict per ticker: a name that fired on two screens is two
        # candidate rows and one company.
        tickers = list(dict.fromkeys(c["ticker"] for c in candidates))
        if not tickers:
            log_step("VERDICT", "none",
                     f"no candidate passed the '{auto.get('gate', 'quality_pass')}'"
                     f" gate", cfg=cfg)
            return [], [], []
        verdicts = research_report.verdicts_for(tickers, cfg)
        if not verdicts or not auto.get("discord_send", False):
            if verdicts:
                log_step("VERDICT", "quiet", f"{len(verdicts)} recorded, not "
                         f"posted (research.auto.discord_send is false)", cfg=cfg)
            return verdicts, [], []
        embeds, charts = research_report.build_verdict_embeds(verdicts, cfg)
        return verdicts, embeds, list(charts)
    except Exception as exc:  # noqa: BLE001 - the alert goes out regardless
        log_step("VERDICT", "failed", f"{type(exc).__name__}: {exc}", cfg=cfg)
        return [], [], []


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config()
    if args.no_send:
        # Reuse the dry-run branch `send_discord_alert` already has for an
        # unconfigured webhook rather than adding a second suppression path:
        # blanking the URL makes it print the cards instead of posting them.
        cfg["discord"] = {**cfg.get("discord", {}), "webhook_url": ""}
    t0 = time.perf_counter()
    log_step("SCAN", "start", f"nightly scan (tiers 1+2)  run={run_id()}", cfg=cfg)

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
            log_step("SCREEN", "skip",
                     f"no '{module.CONFIG_KEY}' section in config.json", cfg=cfg)
            continue
        if not strategy.get("enabled", True):
            # Recorded, not silent: a screen switched off is the most likely
            # explanation for a night that found nothing.
            log_step("SCREEN", "off", f"{module.CONFIG_KEY} disabled in config",
                     cfg=cfg)
            continue
        results.append((module, module.scan(data, strategy)))

    # -- one `fast` quality pass for every signalling ticker of every screen --
    wanted = []
    for _, result in results:
        for ticker in result.hits.index:
            if ticker not in wanted:
                wanted.append(ticker)
    # The panel is already in memory, so the `price_risk` parameters cost
    # nothing here. No benchmark: SPY is not a constituent and adding it to the
    # download would put a non-constituent through every screen, so the two beta
    # readings stay missing on this path and the universe pass supplies them.
    fundamentals = quality.fetch_fast(wanted, cfg, closes=data.get("Close"))
    if not fundamentals.empty:
        for _, result in results:
            result.hits = result.hits.join(fundamentals)

    # -- tier 2: grade the fundamentals once, here. Both the Discord badge and
    #    the hand-off read the recorded verdict, so they cannot disagree --
    for _, result in results:
        quality.annotate(result.hits, cfg)
    log_quality(results, cfg)

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

    # -- tiers 4 and 3, in this process, before anything is posted. Everything
    #    from here on is deterministic, so it can all go out together; see
    #    run_ledger / run_verdicts for why each is individually fail-safe --
    exit_embeds, exit_charts = run_ledger(cfg)
    verdicts, verdict_embeds, verdict_charts = run_verdicts(payload, cfg)
    if verdicts:
        carry_verdicts_to_ledger(cfg)

    all_empty = all(r.hits.empty for _, r in results)
    if (all_empty and not exit_embeds
            and not cfg["discord"]["send_message_when_no_breakouts"]):
        log_step("DISCORD", "skip",
                 "nothing fired and send_message_when_no_breakouts is false",
                 cfg=cfg)
        log_step("SCAN", "ok", "complete, no alert sent",
                 ms=(time.perf_counter() - t0) * 1000, cfg=cfg)
        print("Nothing found by any screen and empty alerts are disabled -- done.")
        return 0

    # -- one chart per signal (partial setups included unless disabled),
    #    rendered first so its embed can reference it --
    chart_cfg = cfg.get("charts", {})
    chart_files = {}  # module CONFIG_KEY -> {ticker: Path}
    if not chart_cfg.get("enabled", True):
        log_step("CHARTS", "skip", "charts.enabled is false", cfg=cfg)
    else:
        chart_t0, n_failed = time.perf_counter(), 0
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
                    log_step("CHARTS", "failed",
                             f"{module.CONFIG_KEY} {ticker}: {exc}", cfg=cfg)
                    n_failed += 1
            chart_files[module.CONFIG_KEY] = per_screen
        n_ok = sum(len(v) for v in chart_files.values())
        log_step("CHARTS", "ok" if not n_failed else "partial",
                 f"{n_ok} rendered, {n_failed} failed",
                 ms=(time.perf_counter() - chart_t0) * 1000, cfg=cfg)

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
        n_vetoed = int(result.hits.get(VETO_COL, pd.Series(dtype=bool)).sum())
        if n_vetoed:
            counts += f" -- {n_vetoed} excluded"
        summary.append(f"{result.title}: {counts}")
        per_screen = chart_files.get(module.CONFIG_KEY, {})
        embeds += build_embeds(module, result, cfg, per_screen)
        image_paths += per_screen.values()

    # Tiers 3 and 4 join the same message, in reading order: what fired, what
    # it graded out at, and what to sell. Three separate posts at three
    # different times was the old shape, and it made the verdict for tonight's
    # signal arrive detached from the signal.
    if verdict_embeds:
        summary.append(f"Verdicts: {len(verdicts)} graded "
                       + ", ".join(f"{v['ticker']} {v['tier']} "
                                   f"{v['conviction']}/100" for v in verdicts))
        embeds += verdict_embeds
        image_paths += verdict_charts
    if exit_embeds:
        plural = "s" if len(exit_embeds) != 1 else ""
        summary.append(f"**Exits:** {len(exit_embeds)} held position{plural} "
                       f"broke a double-top neckline.")
        embeds += exit_embeds
        image_paths += exit_charts
    summary.append(DISCLAIMER)

    send_discord_alert("\n".join(summary), cfg["discord"], embeds, image_paths)
    log_step("SCAN", "ok", f"all tiers complete, {scan_date}, "
             f"{sum(len(r.hits) for _, r in results)} signal(s), "
             f"{len(verdicts)} verdict(s), {len(exit_embeds)} exit(s)",
             ms=(time.perf_counter() - t0) * 1000, cfg=cfg)
    return 0


if __name__ == "__main__":
    enable_utf8_output()   # the nightly log is a redirect, i.e. a codepage stream
    sys.exit(main())
