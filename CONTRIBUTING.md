# Contributing

Thanks for looking. This project has a few rules that are load-bearing rather
than stylistic — they exist because breaking each one has already cost a real
bug here. Read these before a pull request; everything else is ordinary.

## Run the tests

```bash
python tests/run_all.py            # offline + cache-backed; never downloads, never posts
python tests/run_all.py --network  # adds the one Yahoo round-trip test
```

Exit codes are the interface: **0 pass, 1 fail, 2 skip**. Three tests need the
price panel `backtest_universe.py` caches and skip cleanly without it, so a
fresh clone runs green with skips. Run `python backtest_universe.py` once if you
want those three to execute.

There is deliberately no pytest. The tests are plain scripts sharing
`tests/_harness.py`, which is also where the suite forces every secret empty so
nothing can reach a live Discord channel.

## The rules

**Tests assert invariants, not snapshots.** Thresholds in `config.json` get
retuned constantly, so any test comparing against a recorded signal count is
stale within a session. Assert the property that must hold at *any* thresholds.

**Every number stays computed in Python.** No model may write a figure, a score,
a tier or a thesis sentence. `tests/test_no_model.py` enforces this and will
fail your PR if a code path can start one. Qualitative judgment is welcome — it
goes in the enrichment record, where tier 4 grades it, not into the score.

**A missing value is not a failing one.** The registry distinguishes *missing*,
*failing* and *not evaluated*, and conflating them is the single most common bug
in this codebase's history. A gate treats missing as a fail (unverifiable
quality earns no badge); the score skips it; a veto **fails open**, because
unverifiable is not proof of disaster. Absent columns mean "not evaluated" and
must never render as zero — that would file an unmeasurable company in the best
quadrant.

**The `compute_*` function in each screen module is the single source of truth**
for its condition math, and it is fully vectorized over a `(days, tickers)`
frame. The backtests import it; never reimplement the conditions there.

**Secrets come from `.env`, never `config.json`.** `scanner_common.SECRET_ENV`
maps a config path to an environment variable. `config.json` is committed on
purpose — it is the tuned parameter surface — so nothing credential-shaped may
go in it. `tests/test_secrets.py` will catch it.

**No new dependency without a good reason.** This project hand-rolls where the
alternative is a package for fifteen lines: `portfolio_sim/stats.py` implements
Mann-Whitney, Spearman, bootstrap CI and BH-FDR rather than take scipy.

**Config is the surface.** All user-facing text, thresholds and labels are built
from `config.json` at runtime. Never hardcode a threshold or a literal like
"150d SMA".

## Where things live

`CLAUDE.md` lists the rules that keep the code correct, one line each, with a
pointer to the docstring or test that explains it. If you are changing
behaviour, read the relevant rule and that docstring first: the obvious
simplification has often been tried and reverted. `params_list` (MCP) or
`config.json` lists every tunable value.

Generated artifacts go to `output/` via `scanner_common.output_dir()` — there
are no exceptions, and never `Path(__file__).parent`. Before adding a helper,
check whether one already exists (`CLAUDE.md` lists the shared ones): this
codebase's worst bugs came from two copies of the same logic drifting apart.

## Pull requests

Describe what you measured, not just what you changed. A threshold change
without a number behind it is hard to accept, because the whole design is built
on preferring a measurement to an intuition. If you found that something here is
wrong, that is a valuable PR.
