"""Tier 4 -- the virtual portfolio and its signal-attribution analysis.

Tiers 1-3 decide what looks interesting. Nothing until now ever checked
whether any of it made money. This package buys every recorded signal at the
next trading day's open, tracks it, and -- on demand -- asks which of the
attributes the pipeline records actually predicted the return: the screen, the
`full`/`partial` tier, the tier-2 quality rules, the recorded fundamentals, and
the tier-3 verdict and its quant dimensions.

`backtest_universe.py` answers the neighbouring question (would trading the
*technical* screens over all history have paid?) and answers it on far more
data. It cannot answer this one: the quality badge, the fundamentals and the
deep-dive verdict exist only on rows the nightly scan actually wrote, so the
sample has to accumulate forward, one night at a time.

Run as `python -m portfolio_sim <open|mark|analyze|status>`.
"""

import sys
from pathlib import Path

# The production modules are flat in the repo root (`scanner_common.PROJECT_ROOT`
# depends on that and must stay as it is). This package is one level down, so
# put the root on the path rather than relying on the cwd -- the nightly .bat
# files run from the repo directory, but an ad-hoc invocation need not.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
