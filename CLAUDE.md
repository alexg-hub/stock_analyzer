# CLAUDE.md

Guidance for Claude Code in this repository. It lists the rules that keep the
code correct. Detail lives in module docstrings and tests; where a rule exists
because something broke silently, the pointer says where to look.

## What this is

A stock analyzer over the S&P 500 + S&P MidCap 400 (903 names), run nightly by
Windows Task Scheduler. **All four tiers run in `run_scanners.py`, in one
process, and post ONE Discord message.**

| Tier | Question | Code | Records |
|---|---|---|---|
| 1 technical | Is the chart set up? | `breakout_scanner`, `sma_pullback`, `sma_reclaim`, `trend_line` | `Setup` (`full`/`partial`), `Missing`, `Index` |
| 2 quality + exclusion | Good company? Visibly falling over? | `quality.py`, `fast` stage | `Quality`/`Quality Missing` (⭐), `Veto`/`Veto Reasons` (🚫), `Reward`/`Risk`/`Quadrant` |
| 3 verdict | How does the business grade, 0-100? | `research_report.py`, all stages | tier + conviction, `_facts.json`, financials chart |
| 4 portfolio | Was any of it right? | `portfolio_sim/` | positions, findings, the double-top exit |

On-demand surfaces, none of them nightly: `universe_scan.py` (risk/reward plane),
`industry_valuation.py` (industry multiples vs their own history), the
`theme-screen` skill (`newsfeed.py` + `theme_signals.py`), the `enrich` skill
(`enrichment.py`), `combined_report.py` (the one page showing graded and
researched halves together), and `mcp_server.py` (all of it from a session).

**No code path in the analyzer can start a model** (`tests/test_no_model.py`).
Every number, tier and sentence is computed in Python. Agent judgment is recorded
in its own tables (`enrichment.csv`, `themes.csv`), where tier 4 grades it, and
has no field that can move a score.

## Commands

```powershell
python tests/run_all.py                 # ~4 min; offline + cache-backed; exit 0/1/2(skip)
python tests/run_all.py --network       # + the Yahoo round-trip test
python mcp_server.py --selftest         # list tools, call the read-only ones

python run_scanners.py                  # the nightly run -- POSTS to the live channel
python run_scanners.py --no-send        # same run, cards printed

python backtest_breakout.py             # single-ticker validation (defaults = the
python backtest_pullback.py             #   documented case: JNJ / MSFT / META / COST)
python backtest_reclaim.py
python backtest_trend.py
python backtest_universe.py             # profit backtest, every screen x all history
python tune_screen.py sensitivity reclaim_strategy   # needs the cached panel

python research_report.py candidates    # tier-3 candidates per the tier-2 gate
python research_report.py verdicts MSFT JNJ          # grade and record
python research_report.py scan PGR      # tiers 1+2 for a named ticker
python research_report.py risk INTC     # every risk rule beside its threshold

python universe_scan.py                 # all 903 -> risk_reward_*   (~17 min)
python universe_scan.py --from-signals  # the week's signals -> signals_plane_*
python universe_scan.py --tickers MSFT KO               # -> subset_plane_*
python industry_valuation.py scan       # ~20 min cold, seconds warm; --limit writes nothing
python industry_valuation.py record Pharmaceuticals --top 5
python newsfeed.py datacenter --refresh
python theme_signals.py record picks.json [--deep]
python enrichment.py show TJX
python combined_report.py TJX           # -> output/reports/

python -m portfolio_sim open|mark|exit-scan|analyze|status
type output\logs\<run_id>.log           # one line per step of a run
```

To test alert formatting without posting: monkeypatch
`run_scanners.universe_constituents` and `run_scanners.send_discord_alert` in a
scratchpad script and call `run_scanners.main()`, or use `--no-send`.

## Ground rules

- **The user retunes `config.json` between sessions.** Read current values from
  the file (or `params_list`); never trust docs or memory for a threshold, and
  never revert their edits.
- **Every generated file goes under `output/`** via `scanner_common.output_dir()`.
  Never `Path(__file__).parent`. Config stores bare filenames that `output_dir()`
  resolves; an absolute path in config overrides (that is how tests redirect).
  The `.bat` files keep `if not exist output md output` because `cmd` opens the
  `>>` redirect before Python runs.
- **The code stays flat in the repo root** (`PROJECT_ROOT` depends on it). Only
  `portfolio_sim/` and `mcp_tools/` are packages, and each puts the root on
  `sys.path`.
- **Secrets come from `.env`, never `config.json`.** `SECRET_ENV` maps
  `discord.webhook_url` → `STOCK_ANALYZER_DISCORD_WEBHOOK` and
  `research.sec.user_agent` → `STOCK_ANALYZER_SEC_USER_AGENT`. Precedence:
  environment variable if *present* (even empty), then `.env`, then config.
  Presence rather than truthiness is what lets the test harness blank every
  secret. `sec_user_agent()` is the one reader of the SEC contact.
- **Step logs go to stderr, never stdout** (`log_step` / `step()`), so JSON on
  stdout stays parseable; `run_scanner.bat` keeps its `2>&1`. Logging never
  raises; `step()` logs a failure and re-raises. One run id per process
  (`STOCK_ANALYZER_RUN_ID`).
- **Entry points call `enable_utf8_output()`** in `__main__`, never at import: a
  redirected Windows stream is cp1255 here and cannot encode ⭐.
- **Everything tunable lives in `config.json`**, and every user-facing string is
  built from it. Never hardcode a threshold or "150d SMA".
- **Each piece of machinery exists once.** Shared helpers to reuse rather than
  copy: `run_scanners.run_screens`/`grade`/`grade_named`/`grade_batch`,
  `scanner_common.screen_hits`, `signal_row`, `record_signal_rows`, `index_map`,
  `read_table`, `closes_with_benchmark`, `ticker_cache`, `agent_records`,
  `single_backtest`, `analysis.book_status`. Copies drift silently.

## Tier 1: the screens

- **The registry contract.** A screen module exposes `CONFIG_KEY`, `compute`,
  `required_history`, `fires_mask`, `partial_mask`, `scan`, `EMBED_COLOR`,
  `describe_hit`, `plot_hit`, `build_calc_table`. Register it in
  `run_scanners.SCANNERS` and `backtest_universe.SCREENS`, and give it a config
  section with `enabled`.
- **`compute` is the single source of truth** for the condition math, fully
  vectorized over `(days, tickers)` frames with no per-ticker loops. Production
  reads `.iloc[-1]` of `fires_mask`; the backtests evaluate every day. Never
  reimplement condition math in a backtest.
- **`enabled` gates the nightly alert only.** `backtest_universe.py`,
  `tune_screen.py` and on-demand scans ignore it: a screen is switched off when
  it underperforms, which is when it most needs measuring.
- **One list, two tiers.** `full` and `partial` setups are alerted together,
  with `Missing` naming the failing leg. Pullback and trend stay strict
  (`fires_mask == signal`); their `partial_mask` is a backtest-only control
  cohort.
- **Rolling-window conventions differ on purpose; don't "fix" them.** Breakout
  baselines use `shift(1)`. Pullback/reclaim SMAs include today, while their
  persistence counts and volume baseline use `shift(1)`.
- **Candles.** Breakout C4 is a green body ≥ `min_candle_body_pct`. Reclaim R5 is
  gap-aware (body **or** `min_day_gain_pct` vs the previous close while closing
  green); never simplify it back to body-only. Pullback T4 is a small-body,
  long-range reversal bar.
- **`trend_line.py` signals a transition into a state.** The fit is closed-form
  rolling OLS (checked against `numpy.polyfit` in `test_trend_line.py`).
  - The slope ceiling disqualifies the signal and stays out of the freshness
    state.
  - `had_fit` stops a data start or interior hole from minting a phantom trend.
  - The two tiers arm independently.
- **`data.download_period` must exceed every screen's `required_history`**, or
  that screen can never fire (it warns). `backtest_universe.py` derives its own
  download length.
- **`exits.py` (double top) is not a screen.** Never register it in `SCANNERS`
  or `backtest.screens`: they read a signal as a buy.

## Data hygiene

- **Layout contract:** `(Field, Ticker)` column MultiIndex
  (`yf.download(group_by="column", auto_adjust=False)`). Single-ticker frames are
  normalized to it (`download_history`, `single_ticker_panel`).
- **`drop_unsettled_bars` runs on every download and cache load.**
  - It first calls `drop_open_session_bar`. A session still trading has a real
    close but partial volume, and partial volume produced 0/903 volume surges
    and a phantom trend signal.
  - It then drops any bar, **interior or trailing**, where most tickers have a
    null `Close`. A NaN inside a rolling window voids that window for the next
    `window` sessions. Drop the bar; never forward-fill it.
  - `warn_ticker_holes` reports per-ticker interior holes and does not act on
    them. A re-download does not repair a bar that is absent upstream.
  - A zero-signal night: check the `DOWNLOAD warn` lines first.
- **A missing measurement is never a failed condition.** `forward_trades`
  excursions are NaN when a hole falls inside the window, while the return
  survives. Tests assert "NaN on both sides", never an ordering violation.
- **Yahoo quirks are tolerated, not "fixed"**:
  - missing `info` fields render `n/a`;
  - a ticker that fails to download drops out of the scan;
  - Wikipedia needs a browser User-Agent;
  - tickers use `-`, not `.`.

## The universe

- `data.universe_sources`: one entry per index (`name`, `label`, `url`, `alert`).
  `alert` gates the nightly message only; everything that measures grades every
  source. `run_scanners.main` is the only `alert_only=True` caller.
- One parser (`_index_table`) covers every S&P list page. Each source fails open,
  and duplicates are dropped first-wins. All sources failing yields an empty
  frame and a loud download error.
- **`Index` is recorded on every signal row**, nightly and on-demand, via
  `index_map`. Membership is not reconstructable after a rebalance.
- Peer distributions pool the indices by sector. If mid-caps' share of the buy
  quadrant ever collapses below their population share, add a
  `(sector, index)` fallback chain.

## Tiers 2-3: the quality registry (`quality.py`)

- **One registry, `config.json` → `quality.parameters`.** Each parameter
  declares:
  - `source` (resolver prefix: `yahoo_info`, `yahoo_stmt`, `yahoo_deep`,
    `distress`, `moat`, `sec_flags`, `price_risk`);
  - `group`, `stage` (`fast` = every tier-1 hit, `deep` = tier-3 candidates only);
  - optionally `gate` (`min`/`max`/`increasing`), `score` (`good`/`bad`) and
    `veto: true`.

  One `quality.evaluate` call returns badge, score, axes and veto together.
  `quality.validate` runs before every config write, because every case it
  catches fails silently at runtime.
- **Missing ≠ failing ≠ not evaluated.**
  - A missing value fails its gate and is skipped in the score.
  - A group with parameters but no values scores 0.5; a group with no
    parameters at this stage is dropped.
  - With the layer off, no columns are written at all.
- **`enabled: false` makes a parameter invisible** (not gated, scored, listed or
  weighted). Weights renormalize. Use it instead of deleting a rule.
- **Values are keyed by internal key; rows by display label.**
  `quality.row_values` is the bridge. Renaming a label is safe; renaming a key
  is not.
- **Graded exactly once, in `quality.annotate`**, before the hand-off is
  written. The card, `latest_hits.json` and `signals.csv` read that one result.
  Never add a second grading call site.
- **The veto is a label, not a gate** (`quality.veto_enforced: false`). Vetoed
  names are still alerted (sorted last), keep their score, and are bought by
  tier 4. That is what lets tier 4 test the exclusion thesis.
  - A veto **fails open**: a missing value never vetoes, and vetoes are skipped
    by `gate_failures`.
  - Every flag a resolver produces must be an **`int` 0/1**: `scalar` rejects
    `bool`, so a bool flag can never fire.
  - When enforced, a veto overrides the tier, never the score.
- **Risk and reward are separate axes.** Every group declares `axis`.
  `axis_scores` returns `reward`, `safety` and `risk` (= 100 − safety; the
  polarity is stated, never inferred). A missing axis is `None` and must never
  plot as 0. Quadrant thresholds live in `quality.quadrant` and nowhere else;
  `quadrant_of` is the only definition.
- **`aggregate: {"worst_k": n}`** averages a group's n lowest readings, which
  amplifies a miscalibrated anchor. Before adding or re-anchoring a non-peer
  scored metric:
  - check its median *normalized* value across names (under ~0.25 means it marks
    the whole index bad);
  - for a 0/1 flag, check its base rate (a high-base-rate filing reads 0.00 for
    every holder);
  - re-anchor with `bad` near p10 and `good` so the median lands near 0.5.
- **When scoring or the population changes**, recalibrate `reward_min`/`risk_max`
  at the percentile the old bar sat at, never by eye. Check the parameter's
  **stage** first: the plane and the card use `fast` only, so a `deep` change
  must not move the quadrant bars.
- **`peers.py`: `sector_relative: true` reads a metric against its sector.**
  - The veto is a conjunction: absolute breach **and** worst `veto_percentile`
    of the sector. It can only make exclusion quieter.
  - Ties take the midpoint rank; never `bisect_right`.
  - Thin evidence returns None and the absolute anchor stands.
  - `peer_stats.json` is written by a full `universe_scan` pass only, and holds
    `fast` values only. Don't mark a `deep` parameter `sector_relative`: it
    would silently fall back.
  - Every grading entry point takes `sector` as its 4th argument. Omitting it
    silently grades on absolute anchors (`test_peers.py` walks the AST).
  - Explain a score with `quality.normalized_of`, never `quality.normalize` (an
    AST check enforces this).
  - The ⭐ gates stay on absolute anchors on purpose.
- **Resolvers.**
  - `price_risk.py` does no I/O. Too little history returns `None`.
    `downside_deviation` divides by the **total** bar count (Sortino).
    Benchmark series are intersected, never zipped. Beta needs a benchmark:
    every caller passes one (`run_scanners.price_history`,
    `research_report.price_inputs`, the universe pass).
  - `derived.py` (`distress`, `moat`): `statement_frames` is fetched once per
    ticker and shared. Cross-statement years are intersected (`_aligned`). Row
    names go through `ROWS` candidates. Profit-without-cash is a trailing
    consecutive run. The Beneish veto sits at −0.5 because Beneish also flags
    growth.
  - `sec.flags()` (`sec_flags`, deep): the going-concern scan keeps its
    negation guard.
  - `yahoo_deep`: every `.get(...) or {}` in `deep_metrics` is load-bearing.
  - The pretax-margin fallback lives only in `research_collect._financials`.
    Porting it to `quality._statement_metrics` would move the ⭐ for every
    financial. `gross_margin` uses `zero_is_missing`.
  - `collect` is the only network path in grading. `percent: true` means a
    fraction ×100; `dividendYield` is already a percent.

## Tier 3: the verdict (`research_report.py`)

- **Deterministic and computed inside the nightly scan.**
  `deterministic_verdict` collects **every** stage (`stage=None`), sets
  `conviction = score` and `tier = quality.tier_for(score)`, writes
  `<T>_<date>_facts.json` + chart to `output/reports/`, and builds the thesis in
  Python. Nothing revises it.
- **One price download per verdict batch** (`price_inputs`: closes + SPY),
  passed down to `quality.collect`, so beta resolves and history is not fetched
  twice. `_valuation` trims to two years, so `pe_percentile_2y` keeps its
  meaning.
- **The gate re-grades tier 2 against the parameters in force now**
  (`verdict_of`). `has_values` guards rows with nothing to grade.
- **A verdict is recorded in exactly one table, by provenance**: `signals.csv`
  for a signal and `on_demand_scans_results.csv` for an ad-hoc look. Routing
  reads `source` from the facts file. A ticker on two screens has two signal
  rows, so de-duplicate on `(scan_date, ticker)` before grouping by verdict.
- **History CSVs are rewritten, not appended** (`merge_history_csv`, keyed),
  because columns are config-driven labels.
  - `protect=VERDICT_COLS` carries tier 3's columns across a re-scan.
  - `Veto`/`Veto Reasons` (written by the scan) stay **out** of `protect`;
    `Deep Veto*` (written by tier 3) stay **in** it. Don't merge the two pairs.
- **Every history read goes through `scanner_common.read_table`**, writers
  included. A headerless `signals.csv` used to kill every later nightly run.
- **The tier-1→3 hand-off is `output/latest_hits.json`**, in exactly the shape
  `scan_ticker` also produces in memory. Lists are JSON-safe, and `verdict_of`
  still grades a round-tripped row. Only enabled screens appear.

## Tier 4: the portfolio (`portfolio_sim/`)

- **The nightly order:**
  1. `archive_scan`
  2. `run_verdicts`
  3. `run_ledger`: `open` → `mark` → exit scan
  4. one message

  Verdicts come first so tonight's positions copy a row that already carries
  the verdict. Each step is fail-safe on its own.
- **`open` and `mark` are separate**: the entry is `Open[t+1]`, which does not
  exist at scan time. A `pending` position has no price and is in no statistic.
- **Arithmetic is `backtest_universe.forward_trades`**, shared: one
  next-day-open convention, and a test checks ledger == backtest cell for cell.
- **Rule flags are frozen point-in-time** against the rule set recorded with
  the position.
  - `qr_*` is True when the rule passed; `vt_*` is True when the veto
    **tripped**.
  - `en_*` comes from the enrichment table.
  - None of `qr_*`, `vt_*`, `vetoed` or `en_*` may enter `mark_columns()`
    (`protect=`), or they could never update. `EXIT_COLS` **must** be in it.
  - An absent source writes no columns at all.
- **`exits.py` flags and never closes.** It scans every bar since entry, which
  makes it idempotent and self-healing. A pending exit writes no `exits.csv`
  row. `exit-scan` is the only tier-4 command that touches Discord; its charts
  go to `portfolio_dir/exit_charts/`.
- **`analyze` never over-claims.**
  - Significance is the BH `q_value`, gated by `sufficient_n`.
  - Ticker attributes are de-duplicated on `(scan_date, ticker)`.
  - Every conclusion sentence is generated in Python.
  - The `roadmap` section lists every question, even empty ones.
  - `stats.py` is hand-rolled; no scipy.

## On-demand surfaces

- **`scan_ticker`** = the nightly registry on a one-ticker universe, in the
  `latest_hits.json` shape. It ignores `enabled`, grades tier 2 even when
  nothing fires (`Setup: "none"`), and dates itself off its own last settled bar.
- **Named picks (theme + industry valuation)** record through
  `run_scanners.grade_batch` and `scanner_common.signal_row` /
  `record_signal_rows`.
  - Graded **before** anything is written. A ticker with no bars is refused.
  - The batch is atomic: one refusal writes nothing.
  - `Trigger` is `"none"`, never `""` (NaN drops out of `groupby`).
  - Lists go in as JSON.
  - `record` may carry numbers Python computed; `Verdict`/`Conviction` stay out.
- **Theme screen**: the agent names a ticker and a mechanism and never writes a
  number. `agent_records.JudgmentSchema` rejects score fields by name.
  - Evidence is frozen at record time (`events_<theme>_<date>.json`). No events
    means no file.
  - Events are clustered on wording **or** a matching money figure. A
    disagreeing figure vetoes a merge, and nothing is FX-converted.
  - Filings are ordered index-constituents-first, and `filing_item` is set
    nowhere.
  - EDGAR full-text matches an exact phrase, hence the separate `filing_query`.
  - Social sources are unavailable; it is a news-and-filings screen.
  - It belongs in neither `SCANNERS` nor `SCREENS` (tests pin both).
- **Industry valuation**: `dlog(Price) = dlog(P/E) + dlog(EPS)`.
  - The level uses the median; the decomposition uses equal-weight means, which
    are additive. The `*_dlog` columns are not rounded.
  - The z-score is on log P/E.
  - Buckets are GICS sub-industries, rolling up to sector below `min_members` 6.
    Keep 6, not peers' 12; a test pins it.
  - A loss-maker has no P/E. Days under the floor are NaN.
  - The flag is a conjunction.
  - Only a full pass writes, and `--limit` writes nothing.
  - TTM EPS is forward-filled from **announcement** dates
    (`research_collect.ttm_from_quarterly`), never a plain reindex.
  - Membership is today's, applied backwards (survivorship); say so.
  - In neither registry.
- **Enrichment**: a categorical judgment keyed `(scan_date, ticker)`. An invalid
  row raises instead of recording (a wrong category is a cohort of one). A row
  with no matching scan logs `ENRICH warn`.
- **Combined report renders, never computes.** No grading call and no download.
  Pass/fail is read back from the recorded lists. Use `quality.format_scalar`
  for flattened series. The exclusion line reads **both** veto pairs. Scope
  decides the filename.
- **`universe_scan.py`**:
  - `fast` stage only, which every output says.
  - Per-ticker cache freshness via `ticker_cache`, with progress logged every N
    (the MCP watchdog kills a silent child after 60 s).
  - One bulk price download.
  - Grades at render time (`regrade`).
  - **Scope decides the files**: universe / signals / subset. `--limit` is a
    subset. Only a full pass writes `peer_stats.json`.
  - The full pass is on no schedule; the weekly task grades the week's signals.

## The MCP server (`mcp_server.py`, `mcp_tools/`)

- **Long-lived process: restart it after editing any module.** `sys.modules`
  pins code at launch, and lazy imports surface the mismatch later as an
  `ImportError`. `--selftest` runs a fresh process, so it can pass while the live
  server fails.
- **Nothing may write to stdout in the server** (JSON-RPC on fd 1).
  `_serve_stdio` quarantines fd 1 at OS and Python level. Don't replace this with
  per-tool redirection.
- **Side effects default off**: `send=False`, `confirm=False`. The config writers
  refuse `discord.*` and `research.auto.discord_send`.
- **Permissions**: a tool prompts iff it can post to Discord, rewrite
  `config.json`, or write `signals.csv`. Everything else is in
  `.claude/settings.json`. `test_mcp_server.py` fails if a new tool is in
  neither list.
- **Subprocess jobs**: `stdin=DEVNULL`, and a watchdog kills a child silent for
  `FIRST_OUTPUT_TIMEOUT`. A stall records `error`.
- **No file-reading tools.** `output/` is read with Read/Glob.
- **`config_tools` edits are surgical text edits**, re-parsed and compared to the
  intended tree before writing.
  - Write with `newline=""`: config is LF in git and CRLF in the autocrlf
    working copy.
  - `_delete_member` handles first, middle, last, only and inline members.
  - Writable sections are `*_strategy` plus a named list.
- **Account boundary**: never call IBKR `get_account_*` / `get_pa_*`, and never
  mention holdings, position size or concentration. A security is graded on its
  own merits.

## Tests (`tests/`, see `tests/CLAUDE.md`)

Plain scripts, invariants not snapshots. Exit 0 pass, 1 fail, 2 skip. Three
files need `output/backtest_universe_cache.pkl` (run `backtest_universe.py`
once). `_harness` blanks every secret and redirects logs, and
`output_fingerprint()` proves no test wrote into `output/`.

## Schedule

- **"SP500 Breakout Scanner"**: Tue–Sat 07:00 Israel time (00:00 ET) →
  `run_scanner.bat` → `output/scanner_log.txt`. It grades the previous session.
  **Don't move it toward the close**: at 16:30 ET Yahoo's volume is still
  missing the closing auction, and breakout C3 under-fires.
- Check the live `ExecutionTimeLimit` with `Get-ScheduledTask`.
  `3221225786` = killed mid-run.
- **"SP500 Universe Plane"**: Sunday 18:00 → `run_universe.bat`
  (`--from-signals`). No Discord.

## Standing measurements

These shape the current config. Re-measure before changing the related setting.

- S&P 400 breakout signals underperform their own baseline in the backtest
  (excess −1.76; S&P 500 +0.59). Mid-caps alert by user decision; tier 4's
  `Index` split will settle it.
- S&P 500 breakout is mean-positive but median-negative: a few large winners.
- Partial breakout setups outperformed full ones (+3.10% vs +0.67% at 30 days),
  which is why partials are alerted.
- Reclaim (−1.65) and trend (−1.79) read below the random-entry baseline at 30
  days, yet both are currently enabled. For trend, `min_r_squared` is the only
  knob with real gradient.
