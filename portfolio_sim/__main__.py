"""Tier 4 CLI.

    python -m portfolio_sim open       # recorded signals -> virtual positions
    python -m portfolio_sim mark       # fill entries, mark every horizon
    python -m portfolio_sim exit-scan  # double tops on the book -> exits.csv
    python -m portfolio_sim analyze    # the attribution report -> findings.csv
    python -m portfolio_sim status     # what is on the book, what can be asked

`open`, `mark` and `exit-scan` run in the nightly chain, so by default they
**report** a failure and exit 0: a broken ledger must never take down the scan
or the deep dive that produced the signals in the first place. `--strict`
restores a nonzero exit for a manual run.

`exit-scan` is the only tier-4 command that sends to Discord, and the only one
that can: a double top on a held name is actionable the day it happens, while
`open`/`mark`/`analyze` are bookkeeping and a measurement does not need
announcing. `--no-send` records the exit without posting.

`mark` re-syncs the ledger before pricing it. That is not redundancy -- tier 3
writes its verdict hours after tier 1 wrote the row, so a `mark` at the end of
the deep dive is what gets tonight's verdict onto tonight's position instead of
tomorrow's.
"""

import argparse
import sys

import pandas as pd

from scanner_common import enable_utf8_output, load_config, log_step, run_id

from . import analysis, exits, ledger, marking

# The step-log phase each command reports under, so a line in
# output/logs/<run_id>.log names what actually ran rather than which module
# happened to own the entry point.
PHASE = {"open": "LEDGER", "mark": "MARK", "exit-scan": "EXIT",
         "analyze": "ANALYZE", "status": "LEDGER"}


def _status(cfg: dict) -> int:
    status = analysis.book_status(cfg)
    if not status["positions"]:
        print("Ledger is empty. Run `python -m portfolio_sim open`.")
        return 0
    print(f"positions: {status['positions']}  " + "  ".join(
        f"{k}={v}" for k, v in status["by_status"].items()))
    dates = status["scan_dates"]
    print(f"scan dates: {dates['distinct']} ({dates['first']} .. {dates['last']})")
    print(f"tickers:    {status['tickers']}")
    for horizon, n in status["settled_by_horizon"].items():
        print(f"  {horizon:>3}d horizon: {n} settled return(s)")
    if "double_tops" in status:
        tops = status["double_tops"]
        print(f"  double tops: {tops['flagged']} flagged, {tops['sold']} sold")
    if not status["attribution_ready"]:
        print(f"\nAttribution {status['attribution_note']}")
    return 0

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="portfolio_sim",
        description="Tier 4: virtually buy every recorded signal and grade "
                    "which of its attributes predicted the return.")
    parser.add_argument("command",
                        choices=["open", "mark", "exit-scan", "analyze",
                                 "status"])
    parser.add_argument("--strict", action="store_true",
                        help="exit nonzero on failure (default: report and "
                             "exit 0, so the nightly chain survives)")
    parser.add_argument("--no-send", action="store_true",
                        help="exit-scan: record the exit but do not post it "
                             "to Discord")
    parser.add_argument("--no-baseline", action="store_true",
                        help="analyze: skip the random-entry baseline, which "
                             "reads backtest_universe's cached panel")
    args = parser.parse_args(argv)

    cfg = load_config()
    port_cfg = cfg.get("portfolio", {})
    phase = PHASE[args.command]
    if not port_cfg.get("enabled", True):
        log_step(phase, "skip", "portfolio.enabled is false", cfg=cfg)
        return 0

    try:
        if args.command == "status":
            return _status(cfg)
        if args.command == "open":
            frame = ledger.sync(cfg)
            print(f"ledger: {len(frame)} position(s)")
            return 0
        if args.command == "mark":
            ledger.sync(cfg)
            frame = marking.mark(cfg)
            if not frame.empty and "status" in frame:
                print(dict(frame["status"].astype(str).value_counts()))
            return 0
        if args.command == "exit-scan":
            sold = exits.exit_scan(cfg, send=not args.no_send)
            print(f"exits: {len(sold)} sell(s) recorded")
            return 0
        findings = analysis.analyze(cfg, baseline=not args.no_baseline)
        if not findings.empty:
            _print_headlines(findings)
        return 0
    except Exception as exc:                       # noqa: BLE001
        # Logged, never raised, unless the caller asked for strictness -- see
        # the module docstring.
        log_step(phase, "error", f"{type(exc).__name__}: {exc}", cfg=cfg)
        print(f"portfolio_sim {args.command} failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if args.strict:
            raise
        return 0


def _print_headlines(findings: pd.DataFrame) -> None:
    """The rows worth reading before opening the CSV."""
    print()
    for kind in ("portfolio", "ranking"):
        block = findings[findings["analysis"] == kind]
        for text in block["conclusion"].dropna().head(8):
            print(f"  {text}")
    print(f"\n{len(findings)} finding(s) written; read the CAVEAT rows first.")


if __name__ == "__main__":
    # Entry point, so this is where the stream guard belongs: every .bat
    # redirects output, and Windows then hands Python the locale codepage.
    enable_utf8_output()
    # `run_id()` inherits STOCK_ANALYZER_RUN_ID rather than minting one, so a
    # tier-4 step joins the night's existing log instead of starting a file of
    # its own. Standalone, it mints -- an ad-hoc run is still recorded.
    log_step(PHASE.get(sys.argv[1] if len(sys.argv) > 1 else "", "LEDGER"),
             "start", f"run={run_id()}")
    sys.exit(main())
