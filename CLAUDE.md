# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **four-tier stock filter** running nightly on this Windows machine via Task
Scheduler, plus historical tooling (single-ticker backtesters, a universe-wide
profit backtest, a threshold tuner).

1. **Tier 1 — technical.** The S&P 500 screens (breakout, pullback; reclaim
   disabled). Records `Setup` (`full`/`partial`) + `Missing`.
2. **Tier 2 — quality.** `fundamentals.quality.rules` graded over the tier-1
   hits only. Records `Quality` (the ⭐ badge) + `Quality Missing`.
3. **Tier 3 — deep dive.** The `deep-dive` skill over Yahoo + IBKR + SEC + web,
   producing a graded report per ticker.
4. **Tier 4 — the virtual portfolio.** `portfolio_sim/` buys every recorded
   signal at the next open, tracks it, and grades which recorded attribute
   actually predicted the return. Tiers 1–3 decide what looks interesting;
   this is the only thing that ever checks whether any of it was right. It also
   owns the repo's **only exit rule** (`exits.py`, the double top) — everything
   else here is entry-side.

Tiers 1+2 are `run_scanners.py`: one combined Discord alert (text + per-signal
chart images) and the `output/latest_hits.json` hand-off. Tier 3 reads that
hand-off, both automatically (`run_deepdive.bat`, chained from
`run_scanner.bat`) and on demand. Tier 4 reads the two history CSVs and runs
from both `.bat` files.

Validation is `python tests/run_all.py` — plain scripts, no test dependency,
asserting **invariants** rather than recorded output (config gets retuned
constantly, so snapshot tests would be stale within a session). The documented
known cases (JNJ 2025 breakout, MSFT 2024 SMA pullbacks, META 2023-02-02 SMA
reclaim) live in `tests/test_path_equivalence.py`, which needs the network and
so runs only with `--network`.

## Commands

```powershell
# Tests. Offline + cache-backed by default; exit 0 pass / 1 fail / 2 skip.
# Never downloads and never sends to Discord.
python tests/run_all.py
python tests/run_all.py --network      # adds the Yahoo round-trip test

# Full production scan (all screens). WARNING: sends a real Discord message
# to the user's channel (config.json contains a live webhook URL). Don't run
# it casually.
python run_scanners.py
python run_scanners.py --no-send   # same scan, cards printed instead of posted

# Historical validation of the same logic on one ticker (no Discord send);
# each writes output/backtest_*.csv and output/backtest_*.png
python backtest_breakout.py --ticker JNJ  --start 2025-01-01 --end 2025-10-31
python backtest_pullback.py --ticker MSFT --start 2024-01-01 --end 2025-06-30
python backtest_reclaim.py  --ticker META --start 2023-01-01 --end 2023-12-31

# Universe-wide profit backtest: every screen x all history, sweeping a grid of
# "wait x trading days, then hold y". No Discord. Caches the price panel, so
# re-runs after a config tweak take seconds; --refresh re-downloads.
python backtest_universe.py
python backtest_universe.py --entry-delay 0 --holding-days 30   # one cell
python backtest_universe.py --screens breakout_strategy --entry signal_close
python backtest_universe.py --split-by-tier                     # full vs partial

# Threshold tuning (see the tune-thresholds skill). Reads the cached panel
# only -- run backtest_universe.py once first. No Discord.
python tune_screen.py sensitivity reclaim_strategy

# Tier 3 selection: who is worth a deep dive tonight, per the tier-2 gate.
# Reads output/latest_hits.json only. No network, no Discord. `auto-prompt`
# exits 1 when there is nothing to do -- that is what run_deepdive.bat branches
# on, so don't make it exit 0 with an empty prompt.
python research_report.py candidates            # --all / --json also available
python research_report.py auto-prompt

# On-demand: one named ticker, whether or not it signalled. `scan` is tiers
# 1+2 (one ticker downloaded, no Discord) and records its own row;
# run_ondemand.bat adds the tier-3 deep dive and posts the card.
python research_report.py scan PGR RL
run_ondemand.bat PGR

# What a deep-dive did: one line per step, both halves merged. Written live,
# so a run that died still has everything up to that point.
type output\logs\<run_id>.log

# Complete the log of a run that was killed before log-session ran (the ids are
# in the "Deep-dive started" banner in output/deepdive_log.txt).
python research_report.py log-session <run_id> <session_id>

# Tier 4: the virtual portfolio. `open`, `mark` and `exit-scan` run in the
# nightly chain; `analyze` is on demand. `exit-scan` is the ONLY subcommand
# that touches Discord -- the other three never do.
python -m portfolio_sim open        # recorded signals -> positions (no network)
python -m portfolio_sim mark        # re-sync, fill entries, mark every horizon
python -m portfolio_sim exit-scan   # double tops on the book -> exits.csv + alert
python -m portfolio_sim exit-scan --no-send     # record the exit, post nothing
python -m portfolio_sim analyze     # -> output/portfolio/findings.csv
python -m portfolio_sim analyze --no-baseline   # skip the cached-panel baseline
python -m portfolio_sim status      # what is on the book, what can be asked yet

# The MCP server: the same four tiers, driven from a Claude Code session.
# `.mcp.json` launches it; `/mcp` shows it connected. Read reports, logs and
# CSVs under output/ with Read/Glob -- there are deliberately no tools for that.
python mcp_server.py --selftest     # list tools, call the read-only ones, exit
python mcp_server.py                # stdio (what .mcp.json runs)
```

To test alert formatting/sending without spamming the channel, monkeypatch
`run_scanners.get_sp500_tickers` (small ticker list) and
`run_scanners.send_discord_alert` (print instead of POST) in a scratchpad
script and call `run_scanners.main()` — or confirm with the user before any
real send.

## Architecture

- **`run_scanners.py` is the entry point** (also what `run_scanner.bat` /
  Task Scheduler invokes). It downloads the universe once, then runs every
  module in its `SCANNERS` registry. Each screen module implements the same
  contract, consumed by the registry loop:
  - `CONFIG_KEY` — its section name in `config.json`; the section's
    `enabled` flag skips the screen. **`enabled` gates the nightly alert
    only** — `backtest_universe.py` and `tune_screen.py` deliberately ignore
    it (their own selectors are `backtest.screens`/`--screens` and the screen
    argument), because a screen gets switched off precisely when it is
    underperforming, which is when you most need to measure it. Reclaim has
    been off since 2026-07-26 for exactly that reason (excess −1.65 vs the
    random-entry baseline) and must stay measurable.

  The rest of the registry contract (`scan`, `EMBED_COLOR`/`describe_hit`,
  `plot_hit`) and how to add a screen are in `run_scanners.py`'s own docstring.
- **The `compute_*` function in each screen module is the single source of
  truth** for its condition math (`breakout_scanner.compute_signals`,
  `sma_pullback.compute_pullback_signals`,
  `sma_reclaim.compute_reclaim_signals`). Fully vectorized: every
  input/output is a `(days, tickers)` DataFrame, no per-ticker loops. The
  production scan evaluates only the last row; the backtests
  (`backtest_breakout.py`, `backtest_pullback.py`, `backtest_reclaim.py`)
  evaluate every historical day for one ticker and import the compute
  functions — never reimplement the condition math there.
- **One signal list per screen, two tiers.** There is no separate near-miss
  list: `find_*` returns a single `hits` frame with a **`Setup`** column
  (`full`/`partial`) and **`Missing`** (the failing test, `""` when full),
  sorted full-first. Alongside `compute_*` each module exposes:
  - `required_history(strategy)` — total lookback, used by its own "NO signal
    can ever fire" warning and the universe backtest's warm-up;
  - `partial_mask(data, signals, strategy)` — the *partial-tier* combination
    logic, vectorized over all days like `compute_*`;
  - `fires_mask(...)` = `signal | partial_mask` — every day the screen alerts
    on. **The pullback screen is the exception**: it stays strict, so its
    `fires_mask` is just `signal` and its `partial_mask` (touch-but-no-fire) is
    **backtest-only**, a control cohort that is never alerted.
  `find_*` takes `.iloc[-1]` of `fires_mask`, so production and the backtest
  share one definition. `missing_reason(...)` (breakout: positional args;
  reclaim: a calc-table row) names the failing leg.
  Merging the tiers was driven by the backtest: partial breakout setups
  returned +3.10% vs +0.67% for full ones over 30 days, so suppressing them was
  discarding the better cohort. Keep the tier recorded — it is the only thing
  that preserves that distinction.
- **`backtest_universe.py` is the profit backtest** — the whole universe ×
  all history, one fixed-horizon trade per signal, reusing the production
  `compute_*` + `fires_mask` unchanged. It caches its price panel to
  `output/backtest_universe_cache.pkl`, which `tune_screen.py` and the tests
  also read — run it once before either. See the `universe-backtest` skill for
  its timing knobs, the excursion/`cohort_values`/baseline gotchas, and the
  caveats that must stay visible in its output.
- **Shared infra lives in `scanner_common.py`** (config, Wikipedia tickers,
  bulk/single downloads, fundamentals, Discord send — content text + embed
  cards, batched automatically under Discord's 10-embed / 10-file / ~6000
  embed-char per-message limits) and **`charts.py`**
  (validated palette + the chart builders used by the alert, the backtests and
  the tier-3 report). `charts.py` never reads config or fetches data — the
  caller shapes a frame and passes the config section down; keep it that way.
  Its palette has only two hues, so a multi-series chart either groups by hue
  role or, like `plot_financials`, uses small multiples with one series each.
- **`tests/` asserts invariants, never snapshots** — see `tests/CLAUDE.md`,
  which loads whenever you work under that directory.
- **`tune_screen.py` is the threshold tuner** — see the `tune-thresholds`
  skill for how to sweep a screen and read its `protected` column.
- **Tier 2's verdict is computed exactly once**, by
  `scanner_common.annotate_quality`, in `run_scanners.main()` right after the
  fundamentals join and *before* the hand-off is written. `build_embeds` reads
  the recorded `Quality` column (falling back to `quality_check` only when the
  column is absent), so the ⭐ badge and `latest_hits.json` cannot disagree.
  Don't reintroduce a second call site — the bug this fixed was exactly that:
  the badge was computed inside `build_embeds`, which runs *after*
  `write_latest_hits`, so the hand-off never carried it and tier 3 was blind to
  tier 2. **Absent quality columns mean "not evaluated", not "failed"** — the
  helper is a deliberate no-op when the quality layer is off.
- **Tier 3's figures and charts are programmatic — a model-made one is a bug.**
  `assemble_context` renders `output/reports/<TICKER>_<date>_financials.png`
  (via `charts.plot_financials`) and writes `..._facts.json` *before it
  returns*, and also hands back `financials_table_md`. The skill embeds the
  path and pastes the table; it never draws a chart or retypes a figure, and
  `SKILL.md` says so explicitly. Same reason the Discord card reads its numbers
  from `_facts.json` rather than from the verdict dict: the model contributes
  only `tier`/`conviction`/`narrative_adj`/`thesis`. This is load-bearing
  because tier 3 *is* an LLM — left unconstrained it will happily invent a
  plot or mistype a percentage.
- **The pretax-margin fallback lives only in `research_collect._financials`.**
  Yahoo publishes no `Operating Income` (nor `Gross Profit`) for banks and
  insurers, so the tier-3 chart falls back to `Pretax Income / Revenue` and
  records `margin_kind` so the panel and table can name the basis. Never port
  that fallback into `scanner_common._statement_metrics`: its `operating_margin`
  feeds `fundamentals.quality.rules`, so a fallback there would silently change
  the ⭐ badge and the tier-3 gate for every financial. A test pins the split.
- **`research_report.py` is tier 3's deterministic half** (`list_candidates` +
  the gate, the quant score, the financials chart/table/facts, `report_dir`/
  `write_report`, the Discord verdict cards); the synthesis itself is the
  `deep-dive` skill, i.e. Claude reasoning, not a function — which is why the
  nightly run invokes `claude -p` from `run_deepdive.bat` rather than calling
  Python. `research_collect.py` (Yahoo)
  and `sec.py` (EDGAR) are its collectors; `RESEARCH_DATA.md` maps what each
  source can and cannot supply. **The IBKR MCP tools must stay in
  `run_deepdive.bat`'s `--allowedTools`** — under `--permission-mode dontAsk` an
  un-allowed tool is refused *silently*, which would drop the moat/competitor
  section from every report with no error to explain it. They are **enumerated,
  not wildcarded**, and `run_ondemand.bat` must carry the identical list: the
  `get_account_*` / `get_pa_*` family is deliberately excluded because **the
  user's real IBKR book is out of scope** — a deep-dive grades the security and
  tier 4 grades the signal against a fixed-notional virtual ledger, so what is
  already held changes neither, and reports must not mention holdings, position
  size or concentration. `SKILL.md` step 2 and `RESEARCH_DATA.md` say the same;
  the allow-list is what enforces it when nobody is watching. The cost is that a
  genuinely new IBKR tool has to be added in both `.bat` files — it surfaces as
  a `DENIED` line in the run log, so check there when a section goes missing.
  `Edit` is allowed: it grants nothing `Write` does not already grant over the
  same paths, and without it a refusal costs a whole report re-issued through
  `Write` (run `710e2c61` lost GM's final revision that way).
- **The unattended run only speaks in subcommands.** `Bash(python
  research_report.py *)` is a prefix rule, and Claude Code requires *every*
  segment of a compound command to be allowed — so `cmd > file`, `cmd; echo $?`,
  `python -c "..."`, a scratch `.py`, and any `PowerShell(...)` are refused, and
  refused *silently*. The 2026-07-26 shakedown wrote a full RL report and then
  never posted the verdict for exactly that reason, at no visible cost but a
  couple of wasted turns. `post-verdicts <file.json> [--send]` exists so the
  skill never needs `python -c`. When the skill needs something new, add a
  subcommand — never widen this to `Bash(python *)`, which is arbitrary code
  execution. A refused tool now shows up as a `DENIED` line in the run log and a
  `denials` column in `output/logs/deepdive_runs.csv`, so verify a shakedown
  there rather than by grepping the result JSON — but the underlying trap is
  unchanged: **the run exits 0 with denials in it**, so the exit code alone
  still proves nothing.
- **All three tiers write one step log per run** (`output/logs/<run_id>.log`):
  one line per step — timestamp, phase, status, short description — built by
  `scanner_common.log_step` / `step()`. Tiers 1+2 use `SCAN`, `UNIVERSE`,
  `DOWNLOAD`, `SCREEN`, `YAHOO`, `QUALITY`, `HANDOFF`, `ARCHIVE`, `CHARTS`;
  tier 3 adds `CONTEXT`, `QUANT`, `SEC`, `FACTS`, `RECORD`, `REPORT`, `VERDICT`;
  tier 4 adds `LEDGER`, `MARK`, `EXIT`, `ANALYZE`.
  `run_scanner.bat` mints the id and exports it, and `run-id` **inherits** it
  (`run_id()`, not `new_run_id()`), so the whole nightly chain is one file.
  Three rules hold it together:
  - **It writes to stderr, never stdout**, in every tier. That is what lets
    `research_report.py context` print a JSON bundle on stdout while the whole
    assembly — including tiers 1+2, reached through `scan_ticker` — logs freely.
    Tiers 1+2's progress lines used to be `print`s, i.e. stdout, because for
    `run_scanners.py` stdout *is* the log; reached through `context` they landed
    in front of the JSON, and the `drop_unsettled_bars` WARNING — the diagnostic
    most worth seeing — was the one that broke the parse. Two consequences:
    **`run_scanner.bat` must keep its `2>&1`** or `scanner_log.txt` loses every
    step, and the `context` branch of `main()` keeps its `stdout_to_stderr()`
    guard as belt-and-braces for anything that still prints. Don't move that
    guard into `assemble_context`: like `enable_utf8_output`, mutating global
    streams belongs in `__main__`, not at import or in a library call.
  - **It never raises**, and `research.logging.enabled: false` is a full no-op.
    A lost log line must cost you the record, never the report. `step()` is the
    exception that proves it: it logs the failure *and* re-raises, so every
    caller's existing n/a-tolerant `except` behaves exactly as before.
  - **Tests must redirect it before touching production code.** `tests/_harness`
    pins it at import via `configure_logging`, and `config()` runs every config
    it hands out through `redirect_logging` so per-section deep copies inherit
    it. Both halves are needed: `log_step` with no `cfg` falls back to the real
    `config.json`, and loading the cached panel logs (`drop_unsettled_bars`)
    *before* a test body could redirect anything. `output_fingerprint()` in
    `test_signal_contract.py` catches it when this slips — it already has.
  - **Three processes, one log.** `STOCK_ANALYZER_RUN_ID` is exported by the
    `.bat` and inherited through `claude` into the `context` subprocess. Unset
    means standalone — an ad-hoc `context`/`scan` mints its own id rather than
    going unrecorded.
- **The model's half of the log is rendered, not collected.** Claude Code
  already writes every tool call, web fetch and denial to its session transcript
  (`~/.claude/projects/*/<session_id>.jsonl`), so `log-session` reads that and
  emits one short line per call, merging both halves by timestamp. Two
  consequences: **`--session-id` must stay on both `.bat` files** (it is what
  makes the transcript findable *before* the run starts, so a run killed
  mid-flight — result `3221225786` — can still be completed by hand), and the
  renderer must keep converting the transcript's **UTC** stamps to local time,
  or every model step sorts hours away from the steps it belongs between. The
  transcript format is Claude Code's, not ours: `render_session` degrades to
  "no transcript found" rather than failing, and the Python half never depends
  on it.
- **The signal history CSV is rewritten, not appended** (`archive_scan` →
  `merge_history_csv`). The fundamentals columns are config-driven display
  labels, so retuning `config.json` changes the schema and a blind append would
  misalign every later row; de-duplicating on `(scan_date, config_key, ticker)`
  also makes re-running a day idempotent. Cheap at a handful of rows a night —
  don't "optimize" it into an append. **The rewrite must keep carrying
  `VERDICT_COLS` forward** (`merge_history_csv(..., protect=...)`): tier 3
  writes `Verdict`/`Conviction` into a row tier 1 created hours earlier, so
  without the carry a same-day re-scan erases the verdict with no error at all.
  A test pins it.
- **A verdict is recorded in exactly one of two tables, by provenance.**
  `signals.csv` gets `Verdict`/`Conviction` on the ticker's existing row;
  `on_demand_scans_results.csv` holds one row per `(scan_date, ticker)` you
  asked about yourself, with the ratios and the figures behind the judgment.
  The split exists because an ad-hoc look **has no signal row** — PGR and MSFT
  were deep-dived on 2026-07-26 and appear nowhere in `signals.csv` — so a
  single-table design drops exactly the verdicts you most want to study. The
  routing key is `source` in `<T>_<date>_facts.json`, written by
  `assemble_context` because it is the only place that knows; `post-verdicts`
  only reads it back. `record_on_demand` skips a ticker `signals.csv` already
  covers, so the on-demand table can never accumulate orphan rows that no
  verdict will ever reach. Recording runs with or without `--send`.
  Note for analysis: a ticker that fired on two screens has **two**
  `signals.csv` rows and the verdict is on both — de-duplicate on
  `(scan_date, ticker)` before grouping by it.
- **On-demand scanning is the nightly registry on a one-ticker universe**
  (`run_scanners.scan_ticker`), and it returns a payload in **exactly the
  `latest_hits.json` shape** — that is what lets `find_ticker`, the tier-2
  re-grade, `_facts` and the Discord card consume an ad-hoc look unchanged.
  `assemble_context` falls back to it whenever the ticker is absent from the
  hand-off. Two intentional deviations from the nightly path: `enabled` is
  ignored (as `backtest_universe.py`/`tune_screen.py` already do — you asked
  about *this* ticker), and **tier 2 is graded even when no screen fires**
  (`Setup: "none"`), because the quality check is usually the point of asking.
  Its `scan_date` comes from its own price data, not from `latest_hits.json`:
  the stale nightly date would collapse two separate looks into one CSV row.
- **Tier 4 is `portfolio_sim/`** — the only package in the repo; everything else
  stays flat in the root, and `scanner_common.PROJECT_ROOT` still depends on
  that, so don't "fix" it. `python -m portfolio_sim <open|mark|analyze|status>`;
  `portfolio_sim/__init__.py` puts the root on `sys.path` so an invocation from
  another cwd still resolves the flat modules. Its own output directory,
  `output/portfolio/` (`scanner_common.portfolio_dir`), because a position is
  rewritten on every mark while `history/` records what the scan saw and is
  never revised. The rules that hold it together:
  - **`open` and `mark` are separate because the entry price does not exist
    yet.** The nightly run fires after the US close, so `Open[t+1]` is most of
    a day away: `open` records the position as `pending` and `mark` fills it
    later. A pending row has no entry price and is in no statistic — never
    "fill" one at the signal close to make the ledger look complete.
  - **The arithmetic is `backtest_universe.forward_trades`, not a second copy.**
    One next-day-open convention in the repo (buy `Open[i+1]`, sell
    `Close[i+1+h]`, MFE/MAE rolled-then-shifted) means a ledger return and a
    universe-backtest return are the same measurement. A test asserts they
    agree cell for cell. `mark` downloads only the held tickers + benchmark
    rather than reading `backtest_universe_cache.pkl`, which is a day stale at
    best and keyed to a fixed universe.
  - **`mark` re-syncs the ledger first**, which is why it also runs at the end
    of `run_deepdive.bat`: tier 3 writes its verdict hours after tier 1 wrote
    the row, and the verdict is exactly the attribute tier 4 exists to grade.
    Both commands **exit 0 on failure** by design (`--strict` flips it) — a
    broken ledger must never take down the scan or the deep dive.
  - **The row carries the whole source row plus point-in-time derivations.**
    `Quality Missing` is exploded into `qr_<rule>` booleans against the rule set
    **recorded with the position** (`Quality Rules`, frozen at first sight), so
    retuning `fundamentals.quality.rules` cannot rewrite past findings and a
    rule invented later never reads as "passed" on an older signal. Absent
    quality means *no* `qr_*` columns — "not evaluated" is not "failed", same
    rule as `_row_quality`/`_has_fundamentals`. `mark` adds tier 3's
    `quant_score`, per-dimension `quant_*` and `qm_*` metrics from
    `<T>_<date>_facts.json`.
  - **`exits.py` is the exit side, and it flags rather than closes.** The
    double-top rule writes `dt_*` onto the position and leaves `status` and
    every `ret_*d_%` running, so the fixed horizon and the signal exit stay two
    measurements of the **same** position — which is the only way a later
    analysis can ask which one you should have taken. A version that closed the
    position would answer that question by deleting the evidence. Four
    consequences:
    - **It is not a screen.** Never register it in `run_scanners.SCANNERS` or
      `backtest.screens`: both read a signal as a *buy*, so
      `backtest_universe.forward_trades` would score "buy the neckline break,
      sell 30 days later" — the exact inverse of what it means.
    - **`EXIT_COLS` must stay inside `ledger.mark_columns()`.** That is what
      puts them in `sync`'s `protect=`, and without it the nightly re-sync
      erases a recorded exit with no error at all — the same trap the tier-3
      verdict carry exists to close. A test pins it.
    - **Detection scans every bar since entry, not `.iloc[-1]`**, and a
      position already carrying `dt_signal_date` is skipped. That is what makes
      the step idempotent *and* lets a night the scan did not run be picked up
      by the next one. Don't "optimize" it to the last bar.
    - **A `pending` exit writes no `exits.csv` row** (a sell with no exit price
      is not a sell) but is still flagged and still alerted — the signal is
      what's actionable tonight. The row lands on the next run.
    The condition math is a **rolling-extrema** double top (two disjoint
    `shift().rolling()` windows), not a swing-pivot walk. That is a deliberate
    approximation that keeps it vectorized over the whole panel like every
    `compute_*`; don't turn it into a per-ticker loop.
  - **`exit-scan` is the one tier-4 command that speaks to Discord.** `open`,
    `mark` and `analyze` never do and must not start — they are bookkeeping,
    and a measurement does not need announcing. An exit is different: it is the
    only thing tier 4 produces that is actionable on the day it happens. It
    alerts only for positions it flagged *on that run*, so a name that sits
    under its neckline is announced once, not nightly. Its charts go to
    `portfolio_dir(cfg)/exit_charts/`, **not** `output_dir()`, so redirecting
    the ledger in a test redirects the PNGs with it.
  - **A zero-signal night writes a headerless `signals.csv`.** `archive_scan`
    with no rows hands `merge_history_csv` an empty frame, which `to_csv`
    writes as a **zero-byte** file, and `pd.read_csv` raises `EmptyDataError`
    on it. Every read of a history table goes through `ledger.read_table`,
    which treats missing, zero-byte and headerless alike as "nothing recorded
    yet". Found by the 2026-07-27 nightly shakedown: a fresh install whose
    first night was quiet would have logged a ledger failure every night until
    something finally fired. A test pins it.
  - **`analyze` never over-claims.** Significance is keyed off a
    Benjamini–Hochberg `q_value` over the whole file, not a raw p — it runs
    dozens of tests on one thin sample, and ranking by p would reliably crown
    noise. Every row also carries `sufficient_n`; a thin one is still written,
    marked, and says so in its own `conclusion`. Ticker-level attributes are
    de-duplicated on `(scan_date, ticker)` first (the two-screen problem above),
    and on-demand rows are excluded from "which screen paid" — they had no
    trigger, so they are not a screen. **Every conclusion sentence is generated
    in Python.** Same rule as tier 3's chart: a measurement narrated by a model
    is a measurement you cannot check.
  - **The `roadmap` section is why an empty report is still legible.** A cohort
    with no settled returns produces no statistics, so without it the question
    would simply be *missing* and a reader could not tell that from "asked and
    came back empty". It lists every question, its recorded group sizes, and
    what each is still short of. Today that section *is* the report.
  - No scipy: `portfolio_sim/stats.py` implements Mann-Whitney (tie-corrected,
    continuity-corrected), Welch's t, Spearman, a bootstrap CI and BH-FDR, each
    pinned against a hand-computed value in the tests. p-values are normal
    approximations — acceptable only because `sufficient_n` and `q_value` gate
    every claim.
- **`mcp_server.py` is the Claude Code surface**, flat in the repo root for the
  same reason everything else is — `PROJECT_ROOT` depends on it. `mcp_tools/` is
  a package (like `portfolio_sim/`) and copies its `sys.path` header. Two
  invariants hold it together, and both fail *silently* when broken:
  - **Nothing may write to stdout in the server process.** The transport speaks
    JSON-RPC over fd 1, and the production code these tools call prints freely
    (`run_scanners.main` prints hit tables, `download_price_data` prints
    progress). `_serve_stdio` quarantines fd 1 at both the OS and Python level
    and hands the transport a private `os.dup` of the real stdout — do **not**
    replace this with per-tool `stdout_to_stderr()`, which swaps a global while
    FastMCP runs tool bodies on worker threads and the job pool adds more.
    `tests/test_mcp_server.py` pins it; that check is the most important one in
    the file.
  - **Side-effecting tools default to dry-run.** `send: bool = False` on
    anything that can reach Discord, `confirm: bool = False` on `config_set`.
    `.claude/settings.json` also omits those four tools so they always prompt —
    but the default is the real guard, because **a permission rule that fails to
    match fails silently**. MCP rules are `mcp__stock_analyzer__<tool>`; a bare
    tool name matches nothing. `config_set` additionally refuses `discord.*` and
    `research.auto.discord_send` outright: a tool that could flip the send gate
    would make every other dry-run default decorative.
  There are deliberately **no file-reading tools** — reports, logs, CSVs and
  charts under `output/` are read with `Read`/`Glob`, which do it better. A
  `read_report`/`tail_log`/`backtest_results` reappearing means the surface
  crept; a test asserts they have not.
- **Every generated file goes to `output/`** via
  `scanner_common.output_dir()` — logs, `latest_hits.json`, the cached price
  panel, all backtest tables/charts, the tier-3 reports (`output/reports/`), the
  signal history (`output/history/`), the deep-dive step logs
  (`output/logs/`) and the tier-4 ledger and findings (`output/portfolio/`).
  There are no exceptions; Google Drive
  was one until 2026-07-26 and was removed, partly because Claude Code cannot
  `--add-dir` a path containing the U+200F mark in that folder's name. The
  project root holds only inputs (code, `config.json`, docs); `output/` is
  gitignored as one directory. Never write an artifact with
  `Path(__file__).parent` — that is exactly what this replaced. `config.json`
  deliberately still stores **bare filenames** (`backtest_universe_cache.pkl`,
  `reports`, `history`, …) which `output_dir()` resolves, so an absolute path in
  config keeps overriding it (which is how the tests redirect them) and no
  sub-paths leak into config. `PROJECT_ROOT` assumes the code is flat in the
  repo root and **still holds** — `portfolio_sim/` is a package but
  `scanner_common.py` is not in it; that line only needs revisiting if the flat
  modules themselves move. All
  three `.bat` files must keep their `if not exist output md output` guard:
  `cmd` expands `>>` before Python runs, so `output_dir()`'s `mkdir` would be
  too late.
- **Data layout contract**: `yf.download(..., group_by="column",
  auto_adjust=False)` giving a `(Field, Ticker)` column MultiIndex
  (`data["Close"]["AAPL"]`). Single-ticker frames must be normalized to this
  shape (`scanner_common.download_history` does it;
  `scanner_common.single_ticker_panel` slices one ticker back out of a bulk
  frame).
- **Candle conditions read `Open`/`High`/`Low`** (all single-day, so no
  `download_period` impact), but each screen wants a different shape:
  - Breakout **C4**: a strong **green** candle,
    `close > (1 + min_candle_body_pct) * open` (positive threshold enforces
    green + a minimum body). A `full` breakout needs all four conditions, a
    `partial` one exactly 3 of 4.
  - Reclaim **R5** is **gap-aware** (`is_strong_day`): the body route above
    **OR**, when `min_day_gain_pct` is not null, `close/prev_close - 1 >=
    min_day_gain_pct` while still closing green. A body compares close to
    *open*, so it cannot see an overnight gap — and the biggest reclaims gap
    (META 2023-02-02 closed **+23.3%** on the day with a body of only
    **+2.9%**, having opened +19.8% up; `min_candle_body_pct: 0.03` rejected
    it). The gap route can only ever *add* signals, so leaving
    `min_day_gain_pct: null` reproduces the old behaviour exactly. Don't
    "simplify" R5 back to a body-only test.
  - Pullback **T4** (`sma_pullback.py`): the *opposite* — a small-body,
    long-tailed reversal bar at the touch: `|close-open|/open <=
    max_candle_body_pct` AND `(high-low)/open >= min_candle_range_pct` (body
    red or green). Gated by `require_reversal_candle` (default true); it's a
    strict filter, so most ordinary touch days stop qualifying when it's on.
- **Rolling-window conventions differ by design**: the breakout screen uses
  `shift(1)` so the prior range/volume baseline excludes the current day; the
  pullback and reclaim screens' SMA *includes* the current day
  (charting-standard "touch/cross of the line") while their persistence
  counts (time above/below the SMA) and the reclaim screen's volume baseline
  use `shift(1)`. All are intentional — don't "fix" any of them.
- **The `partial` tier differs by screen.** Breakout = exactly 3 of 4
  conditions; a failing *breakout* leg is additionally filtered to closes
  within `near_miss_max_gap_pct` of the required level (alert only — the
  single-ticker backtest log intentionally shows all 3-of-4 days). Reclaim = a
  genuine fresh cross **out of a long downtrend** (both mandatory, as for a
  full setup) with **1 or 2** of the remaining confirmations (volume, candle,
  and slope when enabled) failing — 0 = full, 3+ dropped; `missing_reasons`
  joins every failing test. The pullback screen never alerts a partial; its
  `partial_mask` is the backtest's touch-but-no-fire control cohort (measured
  at +1.57% vs a +2.10% random-entry baseline — i.e. its filters earn their
  keep).
- **Fundamentals are two config-driven layers** (`scanner_common.py`):
  `fields` = snapshot values from Yahoo `info` (`percent_fields` lists keys
  Yahoo returns as fractions, ×100 before display — but `dividendYield` is
  already a percentage, keep it OUT of `percent_fields`); `statements` =
  per-year metrics computed from `Ticker.income_stmt`/`cash_flow`/
  `balance_sheet` (FCF, OpM, PM per fiscal year; ROE with `info` fallback;
  ROIC = EBIT×(1−tax rate)/Invested Capital). Multi-year values are stored
  as `[(fiscal_year, value)]` lists in the joined DataFrame;
  `fundamentals_fields()` renders them as embed fields. Missing statement
  rows (banks lack Operating Income; a bank's hugely negative FCF is
  genuine) render as `n/a` — same tolerance rule as `info` fields.
  `fetch_fundamentals` also adds a reserved `COMPANY_COL` ("Company")
  column (`info` longName/shortName) that `build_embeds` puts in each card
  title as `TICKER (Company Name)`; it is not a config field and never
  renders as an inline field.
- **Quality badge = tier 2** (`fundamentals.quality` in config):
  `quality_failures(row, fund_cfg)` in `scanner_common.py` evaluates `rules` —
  keyed by the same `info`/metric keys as the display config, each
  `{min, max, increasing}`; strict compares, latest fiscal year for multi-year
  metrics, missing value = rule fails (banks can never pass). `annotate_quality`
  records the result, `build_embeds` prefixes the configured `badge` to any
  passing signal card's title, every screen automatically — **including
  `partial` setups** (it grades fundamentals, which are independent of setup
  completeness; this changed when the tiers merged). The rule set is
  intentionally strict: **most tickers fail at least one rule**, which is why
  the tier-3 gate is a soft, configurable one (`research.auto.gate`:
  `quality_pass` | `all`) — a hard gate would routinely leave tier 3 with
  nothing. If `candidates` keeps coming back empty, that is the rule set doing
  its job, and the fix is a config decision (loosen the rules or switch the gate
  to `all`), not a code change.
- **The tier 1→2→3 hand-off contract** is `output/latest_hits.json`: per screen
  a `config_key`/`title`/`strategy` and a `hits` map of ticker → row, where the
  row is whatever the screen's frame held plus `Setup`/`Missing` (tier 1),
  `Quality`/`Quality Missing` (tier 2), `Company`, and the joined fundamentals
  under their **display labels**. `_json_safe` makes it JSON-native: NaN→`null`,
  numpy scalars→Python, and the `[(year, value)]` series→nested arrays.
  `quality_failures` still grades a round-tripped row because it looks values up
  by label and indexes the series positionally — that is what keeps *archived*
  scans readable after the format moves on. Only **enabled** screens appear: the
  hand-off follows the alert, unlike the backtest and tuner. The same shape is
  produced in memory by `scan_ticker` for on-demand tickers — keep the two
  identical, since that identity is the only reason tier 3 needs no second code
  path.
- **Tier 3 re-grades tier 2 against the rules in force now** — `_row_quality`
  recomputes rather than trusting the verdict the scan recorded, and both the
  gate (`list_candidates`) and the Discord card (`_facts`) go through it, so they
  cannot disagree. The reason: `config.json` gets retuned between scans, and a
  gate answering with last night's bar silently ignores the change until the next
  scan — loosening a rule and watching `candidates` still report the old failures
  is genuinely confusing. `output/history/` keeps the original verdict, so nothing
  historical is rewritten. `_has_fundamentals` guards the case where the row has
  no values to grade (fundamentals were off that night): without it, re-grading
  would score every rule as failed rather than reporting "not evaluated".
- **Everything tunable lives in `config.json`** (per-screen strategy
  sections, charts, Discord, fundamentals fields) and all user-facing text
  (alert lines, backtest STEP logs, chart labels) is built from those values
  at runtime — never hardcode a threshold or a literal like "150d SMA".
  Adding a metric to alerts is a config-only change.

## Constraints and gotchas

- `data.download_period` must exceed each screen's total lookback
  (breakout: `consolidation_window_days`; pullback: `sma_days +
  trend_lookback_days`; reclaim: `sma_days + below_lookback_days`; ~21
  trading days per calendar month), or the rolling windows never fill and
  that screen can never fire (each prints a warning). Check this whenever
  any of those values change. `backtest_universe.py` is exempt: it derives its
  own download length from `backtest.years` + `required_history()`.
- The user frequently hand-tunes strategy values in `config.json` between
  sessions — read the file for current values; don't trust README's table or
  prior conversation, and don't revert their changes.
- `config.json` holds the **live Discord webhook URL** and is committed on
  purpose (private repo). Never paste it into issues/PRs or public output.
- Nightly run: Task Scheduler task **"SP500 Breakout Scanner"**, Mon–Fri 23:30
  Israel time → `run_scanner.bat` (tiers 1+2, then tier 4's
  `open`+`mark`+`exit-scan` → `output/scanner_log.txt`) → `run_deepdive.bat`
  (tier 3, then a second tier-4
  `mark` to pick up tonight's verdict → `output/deepdive_log.txt`). One task,
  all four tiers. README's "Nightly schedule" section has the exact
  `Register-ScheduledTask` command and diagnostics; keep it in sync if the
  schedule changes. Result code `3221225786` in `Get-ScheduledTaskInfo` means
  the run was killed mid-scan (usually PC shutdown), and that night's alert is
  simply lost — though the signals themselves are archived before tier 3
  starts. **The task's `ExecutionTimeLimit` has to cover tier 3**: it was
  registered at `PT30M`, which is ample for the scan but will guillotine a run
  of five deep-dives.
- Yahoo quirks the code already tolerates (don't "fix" into hard failures):
  missing `info` fundamentals render as `n/a` (e.g. negative-equity companies
  have no Debt/Equity); individual ticker download failures just drop out of
  the scan; Wikipedia scraping needs the browser-like User-Agent header;
  tickers use `-` not `.` (BRK-B).
- **Unsettled bars, trailing *and* interior.** Yahoo serves a session it has not
  settled as an ordinary daily row — Open/High/Low/Volume present, **`Close`
  null** — and it sometimes *reverts an already-settled bar to that form hours
  later* (seen 2026-07-24: 503 of 504 closes withdrawn on the Saturday, after
  Friday's nightly run had scanned that same bar fine). Every condition compares
  against `Close`, so the row makes each test NaN, `fillna(False)` reads that as
  "no signal", and **the scan reports a confident 0 signals on data that looks
  complete** — no error, no warning. `scanner_common.drop_unsettled_bars` (a
  *fraction*-of-tickers test, since individual tickers legitimately go missing)
  strips such bars in both download functions and on every cache load, logging
  which ones it dropped (`DOWNLOAD warn`); for a trailing bar that also makes an
  intraday run scan the last settled session instead of a partial one.
  **It must keep dropping interior bars, not just the tail.** A withdrawn bar
  stops being the tail the moment the next session lands on top of it, but every
  `compute_*` builds its baselines with `rolling(window)` at the default
  `min_periods=window` — so one NaN *inside* the window voids the output for the
  next `window` sessions. That is the 2026-07-27 night: the guard was tail-only,
  Yahoo still had 2026-07-24 blank for 502 of 503 tickers, Monday's bar sat on
  top, `prior_high` went NaN for 502 tickers, and the alert said "nothing today"
  while suppressing 6 real signals — with 312 more sessions of the same queued up
  before the bad bar aged out of the breakout window. The bar is **dropped, not
  forward-filled**: a session Yahoo withdrew is not a session, and a synthetic
  flat bar would corrupt the volume baselines and candle tests rather than just
  shorten the window by a day. Tests pin both the interior case and the
  rolling-window recovery. If a zero-signal night ever looks wrong, check that
  run's `output/logs/<run_id>.log` for the `DOWNLOAD warn` line first (or
  `output/scanner_log.txt`, which captures the same steps via `2>&1`), and never
  assume a NaN close means a failed condition.
- Windows box, Microsoft Store Python 3.13 (`python` on PATH). yfinance's
  progress bar is disabled for non-TTY output so `output/scanner_log.txt` stays
  readable. matplotlib uses the Agg backend (set in `charts.py`).
- **Entry points must call `scanner_common.enable_utf8_output()`.** When output
  is redirected — which every `.bat` and the test runner do — Windows hands
  Python the locale codepage (cp1255 here), and the ⭐ quality badge has no
  mapping in it, so printing a passing ticker raises `UnicodeEncodeError`. It
  took down the deep-dive dry-run in exactly the `discord_send: false`
  configuration used to shake the nightly chain down. The call sits in each
  `__main__` block (and in `tests/_harness.py`), never at import, so importing a
  module never mutates global streams. A test asserts the badge is genuinely
  unencodable in cp1255 *and* that the guard rescues it — if the first half ever
  starts passing, the second half has stopped testing anything.
