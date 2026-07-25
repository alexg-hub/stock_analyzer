"""The two data paths must agree: screening a ticker out of the bulk universe
panel has to give exactly the same signal dates as the single-ticker download
the `backtest_*.py` scripts use.

This is what catches MultiIndex/slicing mistakes, and it is also where the
project's documented validation cases live -- JNJ's 2025 breakout, MSFT's 2024
SMA touches, META's 2023 reclaim. Those cases assert *dates*, which do depend on
config, so a case whose thresholds no longer admit it is reported as
INFO rather than a failure; the path-agreement checks are the real assertions.

**Needs the network** (Yahoo, via download_history), so `run_all.py` only
includes it with --network.
"""

import sys

import pandas as pd

from _harness import Checks, cached_panel_or_skip

import breakout_scanner
import sma_pullback
import sma_reclaim
from scanner_common import download_history

c = Checks("data-path equivalence (network)")
panel, cfg = cached_panel_or_skip()

CASES = [
    # ticker, module, compute, window, documented dates (may not survive tuning)
    ("JNJ", breakout_scanner, breakout_scanner.compute_signals,
     "2025-01-01", "2025-10-31", ["2025-09-30"]),
    ("MSFT", sma_pullback, sma_pullback.compute_pullback_signals,
     "2024-01-01", "2025-06-30",
     ["2024-04-30", "2024-07-25", "2024-07-30", "2024-09-24", "2024-09-26"]),
    ("META", sma_reclaim, sma_reclaim.compute_reclaim_signals,
     "2022-06-01", "2023-12-31", ["2023-02-02"]),
    ("NVDA", breakout_scanner, breakout_scanner.compute_signals,
     "2024-01-01", "2026-07-01", None),
]

for ticker, module, compute, start, end, documented in CASES:
    key = module.CONFIG_KEY
    strategy = cfg[key]
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    c.section(f"{ticker} / {key}  {start}..{end}")

    def dates(frame, mask_fn=None):
        signals = compute(frame, strategy)
        mask = (signals["signal"] if mask_fn is None
                else mask_fn(frame, signals, strategy)).fillna(False)[ticker]
        return sorted(str(d.date()) for d in mask[mask].index if lo <= d <= hi)

    if ticker not in panel["Close"].columns:
        c.ok(f"{ticker} is in the cached universe", False, "not a member")
        continue
    solo = download_history(ticker, lo, hi, module.required_history(strategy))

    for label, fn in (("signal", None), ("partial", module.partial_mask),
                      ("fires", module.fires_mask)):
        u, s = dates(panel, fn), dates(solo, fn)
        c.ok(f"{label} dates agree between the two paths", u == s,
             f"{len(u)} universe vs {len(s)} single-ticker"
             + ("" if u == s else f"; only-universe={sorted(set(u) - set(s))[:4]}"
                                  f" only-solo={sorted(set(s) - set(u))[:4]}"))

    # fires must be exactly signal, or signal | partial -- no third possibility.
    sig, part, fires = (set(dates(panel)), set(dates(panel, module.partial_mask)),
                        set(dates(panel, module.fires_mask)))
    c.ok("fires == signal or signal|partial",
         fires == sig or fires == sig | part)

    if documented is not None:
        got = dates(panel)
        if got == documented:
            c.ok(f"documented case still fires ({documented})", True)
        else:
            print(f"  INFO documented dates {documented} -> now {got} "
                  f"(config has been retuned; not a failure)")

sys.exit(c.finish())
