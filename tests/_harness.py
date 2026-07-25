"""Shared plumbing for the test scripts.

Deliberately not pytest: the project is a set of plain scripts with no test
dependency, and these follow the same shape -- print OK/FAIL per check, exit
0 (pass) / 1 (fail) / 2 (skipped, a prerequisite was missing). `run_all.py`
reads those codes.

The tests assert **invariants**, not snapshots of past output. The user retunes
`config.json` constantly, so anything comparing against recorded counts would
be stale within a session; every check here has to hold at any thresholds.
"""

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SKIP = 2


class Checks:
    """OK/FAIL reporter with a nonzero exit when anything failed."""

    def __init__(self, title: str):
        print(f"=== {title} ===")
        self.failed = []
        self.n = 0

    def section(self, text: str) -> None:
        print(f"\n{text}")

    def ok(self, label: str, cond, detail: str = "") -> bool:
        cond = bool(cond)
        self.n += 1
        suffix = f" -- {detail}" if detail else ""
        print(f"  {'OK  ' if cond else 'FAIL'} {label}{suffix}")
        if not cond:
            self.failed.append(label)
        return cond

    def close(self, label, got, want, tol: float = 1e-9) -> bool:
        """Numeric equality within a tolerance, reporting both values."""
        both_nan = pd.isna(got) and pd.isna(want)
        near = both_nan or abs(float(got) - float(want)) <= tol
        return self.ok(label, near, "" if near else f"got {got}, want {want}")

    def same_date(self, label, got, want) -> bool:
        return self.ok(label, got == want, f"got {got}, want {want}")

    def finish(self) -> int:
        if self.failed:
            print(f"\nFAILED {len(self.failed)}/{self.n}: {self.failed}")
            return 1
        print(f"\nPASSED all {self.n} checks")
        return 0


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def config():
    from scanner_common import load_config
    return load_config()


def cached_panel_or_skip():
    """The price panel `backtest_universe.py` already cached, or skip.

    Tests never download: a suite that can silently spend two minutes pulling
    500 tickers is a suite people stop running.

    Unsettled trailing bars are dropped, exactly as the download path does, so
    the fixture matches what production sees -- a cache written mid-session
    otherwise hands every test a last row with no closes.
    """
    import backtest_universe
    from scanner_common import drop_unsettled_tail
    cfg = config()
    path = backtest_universe.cache_path(cfg["backtest"])
    if not path.exists():
        print(f"SKIP: no cached panel at {path}\n"
              f"      run `python backtest_universe.py` once first.")
        sys.exit(SKIP)
    panel = drop_unsettled_tail(pd.read_pickle(path))
    print(f"panel: {panel.shape[1] // 6} tickers, {len(panel)} days, "
          f"{panel.index[0].date()} .. {panel.index[-1].date()}")
    return panel, cfg


def screens(cfg):
    """(module, compute, strategy) for every screen with a config section."""
    from backtest_universe import SCREENS
    return [(m, c, cfg[m.CONFIG_KEY]) for m, c in SCREENS if cfg.get(m.CONFIG_KEY)]


def busiest_day(panel, cfg, min_signals: int = 3):
    """A truncation of the panel ending on the day the most signals fired.

    Found from the data rather than hardcoded, so it keeps working when
    thresholds change and never depends on a recorded date.
    """
    total = None
    for module, compute, strategy in screens(cfg):
        fires = module.fires_mask(panel, compute(panel, strategy),
                                  strategy).fillna(False)
        per_day = fires.sum(axis=1)
        total = per_day if total is None else total + per_day
    if total is None or total.max() < min_signals:
        print(f"SKIP: no day with >= {min_signals} signals in the panel.")
        sys.exit(SKIP)
    day = total.idxmax()
    cut = list(panel.index).index(day) + 1
    print(f"busiest scan day: {day.date()} ({int(total.max())} signals)")
    return panel.iloc[:cut], day
