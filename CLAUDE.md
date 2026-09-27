# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **four-tier stock filter** running nightly on this Windows machine via Task
Scheduler, plus historical tooling (single-ticker backtesters, a universe-wide
profit backtest, a threshold tuner).

1. **Tier 1 — technical.** The index screens (breakout, pullback and trend;
   reclaim disabled). Records `Setup` (`full`/`partial`) + `Missing`, and the
   `Index` the ticker signalled in.
2. **Tier 2 — quality *and* exclusion.** The `fast` half of the `quality`
   registry graded over the tier-1 hits. Records `Quality` (the ⭐ badge) +
   `Quality Missing`, and — the "identify the losers" half of the thesis —
   `Veto` + `Veto Reasons` (the 🚫 badge). The badge asks "is this a good
   company"; the veto asks the much narrower "is this one visibly falling
   over", and the two answer a missing value in **opposite** directions.
3. **Tier 3 — the graded verdict.** The `deep` half of the same registry,
   scored 0-100 → a tier and a conviction, plus the financials chart and the
   `_facts.json` snapshot. **Deterministic, computed inside the nightly scan,
   and nothing revises it.** Even the thesis sentence is built in Python
   (`deterministic_thesis`). A narrative pass used to move the conviction by a
   bounded `narrative_adj`; it was removed on 2026-08-11 after measuring that it
   had changed no tier in its whole life — see `AI_ROLE.md`.
4. **Tier 4 — the virtual portfolio.** `portfolio_sim/` buys every recorded
   signal at the next open, tracks it, and grades which recorded attribute
   actually predicted the return. Tiers 1–3 decide what looks interesting;
   this is the only thing that ever checks whether any of it was right. It also
   owns the repo's **only exit rule** (`exits.py`, the double top) — everything
   else here is entry-side.

**All four tiers run in `run_scanners.py`, in one process, and produce ONE
Discord message.** The screens, the quality check, the ledger, the exit scan
and the graded verdict are all deterministic, so they all go out together: what
fired, what it graded out at, and what to sell. It used to be three posts at
three different times, which meant the verdict for tonight's signal arrived
hours after — and detached from — the signal. Each addition is individually
fail-safe (`run_ledger`, `run_verdicts`): a broken ledger or a failed verdict
costs its own section of the message and nothing else. **Nothing is chained
after it**, and no code path in the analyzer can start a model —
`tests/test_no_model.py` is the guard.

**`combined_report.py` is the one page that shows both halves.** The two are
recorded in separate tables precisely so neither can move the other, which
leaves no artifact you can read end to end -- this is it. Per ticker: the
trigger, the ⭐ gate, the 🚫 exclusion, both plane coordinates, the recorded tier
and conviction with its group breakdown, then the agent's stance / moat view /
social read, then the tier-4 position. It **renders and never computes**: every
figure is copied from a recorded file, every sentence is generated in Python,
the agent's prose is quoted verbatim (inlined for one ticker, linked for many),
and a test asserts it never calls `axis_scores`, `quality.evaluate`,
`compute_quant_score` or `download`. A ticker nobody researched still gets a
page saying so -- coverage is a finding. The one place enrichment may appear
outside its own section is the header's coverage count, and a test pins that too.

**It prints every parameter beside the threshold it was compared against.** A
group score of 0.34 is not checkable; `P/E 17.96` against a `max 37` gate is. So
`_parameters_block` renders the whole registry per ticker, grouped in registry
order -- values from the facts file's `quant_metrics`, and only the label, group,
gate and number format resolved from `quality.parameters`, which is config lookup
rather than grading. **Pass/fail is read back from the recorded
`quality_missing` and veto-reason lists, never re-derived**, so moving a value
cannot change the verdict the page reports; a test moves one to both extremes and
asserts the verdict does not budge. A parameter the registry knows but the scan
never resolved prints `not evaluated`, which is how a verdict predating a
parameter announces itself -- CVX 2026-08-07 shows all nine `market_risk`
parameters that way. Inline for one ticker, collapsed in `<details>` for many,
the same rule the prose already follows. Two rendering traps, both pinned:
`quant_metrics` stores series parameters **already flattened to scalars**, so
`format_value` hands them to the series formatter and yields `n/a` for a value
that was present and scored -- `quality.format_scalar` is the one to use; and its
format lookup must default to *the format itself*, not to `"number"`, or every
plain `pct` parameter silently loses its unit.

**The exclusion line reads both veto column pairs.** `Veto` is written by the
scan, `Deep Veto` hours later by tier 3 over the `deep` stage -- where
`dilution_veto`, `eps_collapse_veto` and every SEC filing flag live. Reading only
the fast pair reported VTR, excluded on two deep rules, as `Exclusion: clean`
while the parameter table directly beneath it marked both 🚫. A headline that
contradicts its own table is worse than either error alone; a test pins it.

**It writes into `output/reports/`, which is the one report directory.** That
directory holds tier 3's recorded computation per ticker -- `_facts.json` and
`_financials.png` -- and the dossier that renders them, so a page sits beside its
own inputs. `combined.dir` was pointed there on 2026-08-12; it had been its own
`output/combined/`, which meant the only readable artifact in the project lived
apart from everything it quoted. Nothing globs either path, so the move was a
config value. Note `output/reports/` is *not* a place anything writes prose
directly: the narrative deep-dive used to write `<T>_<date>.md` there, that pass
was removed on 2026-08-11, and its `write_report` outlived it by a day -- called
from nowhere while 13 stale files sat in the directory looking current, each
headed with a `quant N + narrative M` conviction the scoring no longer produces.
Both are gone. Enrichment prose stays in `output/enrichment/` beside the
`enrichment.csv` tier 4 joins on; the dossier inlines it verbatim.

**The sixth surface is the `theme-screen` skill, and it is the only one that
*finds* rather than grades.** Every screen above asks which of the 903
constituents did something on the tape; this asks which companies benefit from a
dated real-world event — a data-centre announcement, an outbreak, a fab
commitment — which no rolling window can reach, because the beneficiaries two
links down the chain have done nothing on their own charts yet.
`mcp_tools/themes.py` assembles the deterministic bundle (`newsfeed.py`: Google
News RSS plus EDGAR full-text search, both dated and sourced) and returns a
brief; a session traces the chain with IBKR's theme graph and web search;
`theme_signals.record` writes the picks into `signals.csv` under
`config_key = "theme_screen"`. **On demand only** — it is in neither
`run_scanners.SCANNERS` nor `backtest_universe.SCREENS`, and
`tests/test_theme_screen.py` pins both absences: the latter would enrol it in
`test_signal_contract.py`, which demands a full-history `(days × tickers)` mask
that reproduces itself under bar-drop perturbation, and a list dated *today*
cannot supply one. Four rules:
- **The agent names a ticker and a mechanism; it never writes a number.**
  `validate` rejects `conviction`, `tier`, `score`, `Verdict`, `Reward`, `Risk`
  and `price_target` by name. Every figure on a theme row — the ⭐ badge, the 🚫
  veto, both plane axes, `Index` — is computed by `run_scanners.scan_ticker`
  *after* the pick. On the first live run the registry graded the agent's three
  picks `avoid`, `speculative` and `dull`, which is the design working.
- **That same scan is the tradeability guard.** IBKR's theme graph returns
  Prysmian (Milan), NKT (Copenhagen) and POWERGRID (NSE), and a model can invent
  a symbol outright; a name with no bars is refused rather than written into a
  table tier 4 buys from.
- **The batch is atomic.** A theme is a *chain*, so one invalid pick writes
  nothing — half a chain on the record is a misleading cohort, not a partial
  answer. Same rule as `config_edit`: the unit of validity is the set.
- **`Trigger` names the no-trigger case explicitly** (`"none"`, never `""`).
  An empty string round-trips through CSV as NaN and `groupby` drops NaN
  silently, which would discard exactly the cohort the column exists to isolate
  — the picks the agent found *before* the tape did.
- **The evidence behind a pick is frozen, because the news cache is not a
  record.** `newsfeed._save_cache` replaces a theme's entry wholesale on every
  refresh, so reading a three-week-old pick back through the cache shows
  *today's* headlines — auditable-looking without being auditable.
  `snapshot_evidence` writes the clustered events at record time to
  `events_<theme>_<scan_date>.json` and the row points at it via
  `evidence_file`/`evidence_n`. No events means **no file** rather than an empty
  one: `""` says the trail is missing, while an empty snapshot would claim the
  pick was made from no evidence at all.

  Two collection rules, both measured rather than assumed:
  - **Events are clustered and ranked, never served in date order.** Raw feeds
    repeat one story per outlet and bury a $50bn commitment under an op-ed.
    `cluster_events` merges on wording overlap **or** a matching money figure —
    the second route is load-bearing, because three reports of SK Hynix's $38bn
    fab commitment shared under a third of their words and stayed three separate
    "events" without it. A *disagreeing* figure vetoes a merge even when the
    wording matches (`$13bn Texas` vs `C$13bn Alberta` are two projects), and
    **no FX conversion happens anywhere** — a currency mismatch is a mismatch.
    `sources_n` on a cluster is exactly the corroboration `min_sources` asks
    about.
  - **Filings are ordered index-constituents-first, and 8-K item codes are
    *not* the fix for the size bias.** Micro-caps mention a theme promotionally
    far more than large filers mention one materially. Filtering on `Item 1.01`
    was the obvious remedy and was measured on 2026-08-14: it surfaced PLD for
    `datacenter` but collapsed `energy` to **one** hit and `biopharma` to
    **zero**, losing Chevron and Oshkosh. So `filing_item` stays supported per
    theme and **set nowhere**, and the ordering does the work instead — IRM, NI,
    AMD, PEG, CVX, ON, LSCC and ZTS now lead where ARMP/QUCY/ZSQR did. Nothing
    is dropped; an off-index filer is still a find, just not the one to read
    first.
  Two things measured rather than assumed (2026-08-14): the "social media" half
  of the original idea is **unavailable** — Reddit blocked, StockTwits 403, X
  paid-tier, Facebook no public search, Google Trends no API — so this is a
  news-and-filings screen and the skill says so; and **EDGAR full-text matches an
  exact phrase**, so each theme carries a `filing_query` separate from its news
  query. Passing the news keyword soup returns zero hits, which reads as "nobody
  filed about this" — a wrong answer rather than a missing one.

**The seventh surface is `industry_valuation.py`, and it is the first thing here
that grades an *industry* rather than a company.** Tiers 1-3 ask what a ticker did
on the tape and what its own statements say; `peers.py` compares a company to its
sector *today*. None of them can answer "money rotated into semiconductors while
pharmaceuticals went sideways — was the sector that did nothing actually cheap?"
A price gap cannot answer it, because over any window the identity

    dlog(Price) = dlog(P/E) + dlog(EPS)

holds exactly, and it separates the two things a price move can mean. An industry
whose **earnings grew while its multiple compressed** has been de-rated — that is
the opportunity. One whose multiple fell *because* earnings fell has been repriced
correctly. The module is that decomposition, per industry. On demand only
(`scan` / `show` / `record`, plus `valuation_scan`/`valuation_read`/
`valuation_record`), in neither `run_scanners.SCANNERS` nor
`backtest_universe.SCREENS`; `tests/test_industry_valuation.py` pins both
absences, as for `theme_screen`. Unlike `theme_screen` a full-history mask *is* in
principle computable here, which is a real future option and deliberately
deferred. Seven rules:
- **The P/E history is genuinely reconstructable, and that is the whole premise.**
  `research_collect.quarterly_eps` returns reported quarterly EPS indexed by
  **announcement date** (~24 quarters from Yahoo), and `ttm_from_quarterly` rolls
  it to TTM and forward-fills onto a 5y price panel. The forward-fill is what
  makes it point-in-time: a figure can only ever propagate forward from the day it
  was published. The pair was split out of the old private `_ttm_eps_series` so
  the per-ticker `pe_percentile_2y` and the industry path can never drift on what
  "trailing earnings on day d" means — the same single-source-of-truth rule the
  `compute_*` screens follow. **A plain reindex there would make every historical
  P/E clairvoyant**, silently; a test pins the step onto the announce date.
- **Two statistics, two aggregations, and the difference is load-bearing.** The
  *level* (`pe_now`, `pe_z`, `pe_pctile`) is a cross-sectional **median** — robust,
  and "pharma trades at 14x" is a sentence. The *decomposition* is built from
  equal-weight indices (**mean** of member ratios), because the identity is
  additive in logs and a median is not: `median(a+b) != median(a)+median(b)`, so a
  median decomposition would not add up and the one property worth testing would
  be untestable. The `*_dlog` columns are therefore **not rounded** — rounding them
  to 6 places broke the additivity the column set exists to make checkable from the
  CSV, which is what the test caught. `*_chg_pct` are the readable ones and add
  only approximately.
- **The z-score is on log P/E**, because multiples are ratio-scaled: 10x→20x and
  20x→40x are the same event and a linear z would not say so.
- **Buckets are GICS sub-industry, rolling up to sector below `min_members` (6).**
  First consumer of `sub_industry` as a grouping key — `peers.py` buckets by sector
  only, which would lump Semiconductors with Application Software and
  Pharmaceuticals with Health Care Equipment, hiding exactly the rotation this
  exists to see. **The floor is 6 against `peers.min_peers` 12 deliberately**: peers
  needs resolution for a within-bucket *percentile rank*, this needs only a
  *central tendency*. At 12, Pharmaceuticals (9 members) would silently vanish.
  Don't "harmonise" them; a test pins the inequality. A rolled-up bucket carries a
  `" (other)"` suffix so it can never be read as a real GICS sub-industry.
- **A loss-maker has no P/E — not a low one.** Non-positive EPS yields NaN, so a
  bucket's member count varies over time, which matters most exactly where this
  gets used (much of Biotechnology is loss-making). A day with fewer than
  `min_members` priced members is **NaN, never a median of three and never
  forward-filled** — the same rule `price_risk` applies with `min_obs`.
- **The flag is a conjunction, never a z-score alone.** `cheap` needs the multiple
  historically low **and** earnings growing over the window; `rich` needs it
  historically high **and** reached by expanding. Deliberately not symmetric —
  forcing symmetry ("rich = high z and earnings falling") would miss an industry
  whose earnings grew strongly and whose multiple ran further still, which is the
  semiconductor case. Like `peers.in_sector_tail`, the extra conjunct can only ever
  make the flag quieter.
- **Only a full pass writes anything; `--limit` is a probe that writes nothing.**
  `universe_scan` learned this the hard way — its date-stamped artifacts meant a
  thirty-ticker probe silently replaced the day's whole-index plane. A bucket-level
  table built from a subset is meaningless anyway, so the simple rule beats
  reproducing three artifact scopes. Two more things to keep visible: the **EPS
  cache stores quarterly points, not the P/E path** (price moves daily, earnings
  step quarterly, so a warm re-run rebuilds the path from a fresh panel in
  seconds against ~20 minutes cold); and **bucket membership is today's membership
  applied backwards**, so every historical path is survivorship-biased — hence the
  `membership_asof` column, the chart subtitle and the MCP payload's `caveat`, the
  same discipline `universe_scan` applies to `stage`.
- **`record` may write numbers, unlike `theme_signals`.** There the picks are named
  by a model, so `BANNED_FIELDS` rejects every score by name; here every figure is
  computed in Python from a recorded panel, so `pe_z` and the decomposition belong
  on the row — they are the attributes tier 4 exists to grade. `Verdict`/
  `Conviction` still stay out (`protect=VERDICT_COLS`). Everything else follows
  `theme_signals.record`: graded through `scan_ticker` **before** anything is
  written, a name with no bars refused, and the **batch atomic** — half an industry
  on the record is a misleading cohort, not a partial answer. **Forward P/E is a
  level, not a path**: Yahoo serves one value with no history and nothing here had
  ever stored it, so `valuation_history.csv` is the only thing that ever builds it
  into a series.

**The fifth surface is not a tier: the `enrich` skill.** Qualitative research —
competitive position, whether a tripped rule is a sector artifact, what the
filings and the tape say — runs from a Claude Code session over the MCP tools,
IBKR's connection graph and web search, and records to `output/enrichment/`
(`enrichment.py`). It is graded by tier 4 like any other recorded attribute and
**cannot change a verdict**: there is no numeric field for it to change one
with, and `enrichment.validate` rejects `conviction`/`tier`/`score`/
`narrative_adj` by name. That is the whole design — a judgment on the record is
a hypothesis you can measure, a judgment inside the score is a number nobody can
check.

One caveat to state honestly: Discord caps a message at 10 embeds / 10
attachments / ~6000 embed chars, and `send_discord_alert` batches on exactly
that. This is one *send*, one header, one contiguous batch — a busy night still
splits into several HTTP requests.

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

# The whole night: all four tiers, one Discord message. WARNING: sends a real
# message to the user's channel (config.json contains a live webhook URL).
# Don't run it casually.
python run_scanners.py
python run_scanners.py --no-send   # same run, cards printed instead of posted

# The quality registry: what is tunable and what it is set to. Also the MCP
# `params_list` tool, which is the better surface interactively.
python migrate_config.py           # prove the unified section == the old two

# Historical validation of the same logic on one ticker (no Discord send);
# each writes output/backtest_*.csv and output/backtest_*.png
python backtest_breakout.py --ticker JNJ  --start 2025-01-01 --end 2025-10-31
python backtest_pullback.py --ticker MSFT --start 2024-01-01 --end 2025-06-30
python backtest_reclaim.py  --ticker META --start 2023-01-01 --end 2023-12-31
python backtest_trend.py    --ticker COST --start 2023-06-01 --end 2024-06-30

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

# Tier 3 selection: who is worth researching tonight, per the tier-2 gate.
# Reads output/latest_hits.json only. No network, no Discord.
python research_report.py candidates            # --all / --json also available

# Tier 3's deterministic verdict on demand -- the same call the nightly scan
# makes. Records tier + conviction to output/history/ and prints; posting is
# the scan's job. No arguments = tonight's gated candidates.
python research_report.py verdicts
python research_report.py verdicts MSFT JNJ

# On-demand: one named ticker, whether or not it signalled. `scan` is tiers
# 1+2 (one ticker downloaded, no Discord) and records its own row; `verdicts`
# then grades it. Together these are what run_ondemand.bat used to wrap.
python research_report.py scan PGR RL
python research_report.py verdicts PGR RL

# The enrichment record -- what the `enrich` skill concluded. Written only
# through this CLI or the `enrichment_record` MCP tool, never by hand: an
# invalid row raises instead of recording, because a wrong categorical value is
# not a missing measurement, it is a cohort of one that `analyze` then grades.
python enrichment.py record row.json
python enrichment.py show TJX

# The thematic screen -- AI names the candidate, Python grades it. On demand
# only; the `theme-screen` skill or the `theme_research`/`theme_record` MCP
# tools are the interactive surface. Writes into signals.csv, so tier 4 buys
# these on the next `portfolio_sim open`. No Discord.
python newsfeed.py                     # the evidence base: dated, sourced events
python newsfeed.py datacenter --refresh
python theme_signals.py record picks.json          # {theme, event, picks:[...]}
python theme_signals.py record picks.json --deep   # + tier 3's verdict per pick
python theme_signals.py show PWR

# Industry valuation anomalies -- has an industry's multiple left its own
# history, and did its earnings move with it? Reconstructs a ~5y P/E path per
# company from announcement-dated quarterly EPS, aggregates to GICS
# sub-industry, and splits each industry's price move into multiple change and
# earnings change. On demand only. No Discord. ~20 min cold, seconds warm.
# `record` writes a bucket's members into signals.csv, so tier 4 grades them.
python industry_valuation.py scan
python industry_valuation.py scan --limit 30        # timing probe; writes NOTHING
python industry_valuation.py scan --no-fetch        # rebuild from cache, no network
python industry_valuation.py show Semiconductors
python industry_valuation.py record Pharmaceuticals --top 5

# The combined dossier: the graded half and the researched half on one page.
# Reads recorded files only -- no network, no grading, nothing recomputed.
# Scope decides the filename, so a subset run can never overwrite a wider one.
# Lands in output/reports/ beside the facts.json and chart it renders from.
python combined_report.py TJX                # -> reports/TJX_<date>_combined.md
python combined_report.py TJX GOOG           # -> reports/subset_combined_<date>.md
python combined_report.py --from-signals 7   # -> reports/signals_combined_<date>.md

# The risk/reward plane. Cached per ticker, so a re-run is instant. No Discord.
# Each scope writes its OWN table/PNG/HTML -- a subset can never overwrite the
# whole-index plane (it silently did until 2026-08-10).
python universe_scan.py                      # all 903 -> risk_reward_*    ~17 min
python universe_scan.py --from-signals       # the week's signals -> signals_plane_*
python universe_scan.py --from-signals 14    # ...a 14-day window instead
python universe_scan.py --tickers MSFT KO    # just these -> subset_plane_*
python universe_scan.py --limit 30           # timing probe before committing
python universe_scan.py --no-fetch           # re-render from cache, no network
python universe_scan.py --refresh            # ignore the cache (needed after a
                                             # new parameter is added)

# Tier C on demand: every disaster symptom and moat proxy for one ticker, with
# each value beside the threshold it was compared against, split into
# tripped / clean / unknown. Collects every stage, so it reaches the SEC filing
# flags the nightly scan cannot. No Discord. `--json` for the raw bundle.
python research_report.py risk INTC MSFT

# What a run did: one line per step, written live, so a run that died still
# has everything up to that point.
type output\logs\<run_id>.log

# Tier 4: the virtual portfolio. `open`, `mark` and `exit-scan` now run INSIDE
# run_scanners.py (see run_ledger) so the exit cards can join the one nightly
# message; these are the standalone forms, still useful on demand. `analyze` is
# on demand only. `exit-scan` is the ONLY subcommand that touches Discord.
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
`run_scanners.universe_constituents` (a small frame) and
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
- **The universe is a list of indices, and `alert` gates only the alert.**
  `data.universe_sources` holds one entry per index (`name`, `label`, `url`,
  `alert`); `scanner_common.universe_constituents(cfg, alert_only=)` is the one
  fetch and the one parse, `universe_tickers` its list form. Today: **S&P 500
  (503) + S&P MidCap 400 (400) = 903, both alerted**, no overlap,
  because S&P's indices are mutually exclusive by construction. The Nasdaq-100
  was considered first and rejected on measurement — 87 of its 102 members were
  already constituents, and of the 15 net-new names ~7 are foreign private
  issuers that file 20-F/6-K, for which `sec.flags()` returns a confident
  **all-zero** rather than `{}`: ten risk flags reading a clean 1.00, which
  `worst_k` then never selects, so those names would grade structurally safer
  than any US filer with nothing saying why. Four rules:
  - **`alert: false` gates the nightly Discord message and nothing else.**
    Identical semantics to `<screen>.enabled`, for the identical reason:
    `universe_scan.py`, `peers.py` and `backtest_universe.py` all grade every
    source, because a universe is held back from the alert precisely when
    nobody has measured whether its signals pay. `run_scanners.py` is the only
    caller passing `alert_only=True`. Promotion is a config flip, and **`sp400`
    was flipped to `alert: true` on 2026-08-14 — by user decision, against the
    measurement, which is recorded here rather than erased.** Measured over 3
    years of the 903-name panel, split by `index_name` against each index's own
    baseline (wait 0, hold 30):

    | screen | S&P 500 excess | S&P 400 excess |
    |---|---|---|
    | breakout | **+0.59** | **−1.76** |
    | pullback | **+0.68** | +0.06 |
    | reclaim (off) | −1.40 | −0.34 |

    Not an outlier artifact: mid-cap breakout is below its baseline on mean,
    **median** (0.51 vs 1.53) and **win rate** (51.7% vs 55.8%) alike, on 2008
    signals, and the sign holds at 10, 30 and 60 days. Mid-cap pullback is
    flat — better than baseline on median and win rate, level on mean. So the
    mid-cap breakout card is the one to watch: **the standing hypothesis is that
    it underperforms**, and tier 4 now buys those signals, so `portfolio_sim
    analyze` split on `Index` is what settles it with live rows rather than a
    backtest. Re-run the split before revisiting; it is not a repo script yet,
    which is the gap to close if this becomes a recurring question. Reverting is
    the same one-line flip.
    (Noted in passing, and a separate matter: **breakout on the S&P 500 is
    mean-positive but median-negative** — +0.59 mean against a median of 1.37
    vs the baseline's 1.55. Its edge is a handful of large winners, not a
    typical trade. That predates this change and is worth its own look.)
  - **One parse covers every S&P index.** The 500, 400 and 600 list pages all
    publish `Symbol` / `GICS Sector` / `GICS Sub-Industry`, so `_index_table`
    and `_CONSTITUENT_COLS` are unchanged and adding the 600 is a config line.
    That is *why* the MidCap 400 was the cheap expansion and the Nasdaq-100 was
    not — the NDX page carries no ticker table at all any more, and its foreign
    members have no GICS sector in any source here, which would have dropped
    exactly those names to absolute anchors while everything else used peers.
  - **Fail-open per source, and de-duplicated first-wins.** An unreachable or
    reshaped index logs `UNIVERSE warn` and is skipped; losing the mid-caps
    must never cost the S&P 500 scan. Every source failing yields an empty
    frame, and the download that follows raises — loud, which is right. The
    de-duplication guards a rebalance moving a name between indices: a
    duplicate would be downloaded, screened and peer-ranked twice.
  - **`Index` is recorded on every signal** (`INDEX_COL`, joined in
    `run_scanners.main()` before `quality.annotate`), so it reaches
    `latest_hits.json`, `signals.csv` and — because a position copies its whole
    source row — the tier-4 ledger. Same rule as `Setup`: S&P rewrites
    membership at every rebalance, so it is not reconstructable later, and it
    is the only thing that makes "did the mid-caps pay?" answerable. Written by
    the scan, so like `VETO_COLS` it stays **out** of `merge_history_csv`'s
    `protect=`. Wiring it as a graded tier-4 *question* is still to do.
  - **Peer distributions pool the indices by sector**, deliberately, and this
    was checked rather than assumed. Every pooled bucket is 29–170 names
    against `MIN_PEERS` 12, whereas bucketing by `(sector, index)` puts S&P 400
    Communication Services at 6 — under the floor, and so silently back on
    absolute anchors. The worry was that pooling would introduce a *size* bias
    the way absolute anchors introduced a sector one. Measured on the first
    903-name pass: mid-caps do read riskier (mean risk **66.3 vs 61.2**, veto
    rate **17.2% vs 11.5%**), but that is a gradient, not an exclusion — the
    tell is the buy quadrant, where they hold **47.5%** of the places against a
    **44.3%** population share. Compare the utilities case that motivated
    `peers.py`: 84% of the sector vetoed and the highest mean risk in the
    index. So pooling stands. If it ever stops standing the symptom is
    mid-caps' buy share collapsing below their population share, and the fix is
    a `(sector, index)` → `sector` → absolute fallback chain. Re-run the
    diagnostic after any scoring change; the 15 buckets already under
    `MIN_PEERS` fall back to absolute anchors and are expected to.
- **The `compute_*` function in each screen module is the single source of
  truth** for its condition math (`breakout_scanner.compute_signals`,
  `sma_pullback.compute_pullback_signals`,
  `sma_reclaim.compute_reclaim_signals`, `trend_line.compute_trend_signals`).
  Fully vectorized: every
  input/output is a `(days, tickers)` DataFrame, no per-ticker loops. The
  production scan evaluates only the last row; the backtests
  (`backtest_breakout.py`, `backtest_pullback.py`, `backtest_reclaim.py`,
  `backtest_trend.py`)
  evaluate every historical day for one ticker and import the compute
  functions — never reimplement the condition math there.
  `trend_line.py` is the one whose math is not elementwise: it is a rolling OLS
  fit of `log(Close)`, kept vectorized by computing the fit **from rolling sums
  in closed form** rather than fitting each window. The re-basing of the cross
  term from the absolute bar index onto the window's own `x = 0..n-1` is the
  step that fails silently — an off-by-one there yields a plausible slope and
  raises nothing, so `tests/test_trend_line.py` checks slope, intercept, r² and
  residual against `numpy.polyfit`. Cost is not a reason to change it: the whole
  904 x 1254 panel fits in **0.238 s**, cheaper than any of the other three
  screens, and weekly bars were measured and rejected (same signal count, same
  excess within noise, and the signal date collapses to the resample boundary).
- **One signal list per screen, two tiers.** There is no separate near-miss
  list: `find_*` returns a single `hits` frame with a **`Setup`** column
  (`full`/`partial`) and **`Missing`** (the failing test, `""` when full),
  sorted full-first. Alongside `compute_*` each module exposes:
  - `required_history(strategy)` — total lookback, used by its own "NO signal
    can ever fire" warning and the universe backtest's warm-up;
  - `partial_mask(data, signals, strategy)` — the *partial-tier* combination
    logic, vectorized over all days like `compute_*`;
  - `fires_mask(...)` = `signal | partial_mask` — every day the screen alerts
    on. **The pullback and trend screens are the exceptions**: both stay
    strict, so their `fires_mask` is just `signal` and their `partial_mask`
    (pullback: touch-but-no-fire; trend: the same trend with linearity relaxed)
    is **backtest-only**, a control cohort that is never alerted.
  `find_*` takes `.iloc[-1]` of `fires_mask`, so production and the backtest
  share one definition. `missing_reason(...)` (breakout: positional args;
  reclaim: a calc-table row) names the failing leg.
  Merging the tiers was driven by the backtest: partial breakout setups
  returned +3.10% vs +0.67% for full ones over 30 days, so suppressing them was
  discarding the better cohort. Keep the tier recorded — it is the only thing
  that preserves that distinction.
- **`trend_line.py` screens a *state*, and turning that into an event is the
  whole design.** The other three screens ask about one bar; this one fits the
  trailing `trend_window_days` (210 ≈ 10 months) and asks whether the path is
  close to a line. That condition stays true for months — ~53 tickers a night —
  so alerting on it would swamp the one nightly message and give
  `forward_trades` no dated entry to score. The signal is therefore the
  **transition** into it (~0.57/night), re-arming if the trend breaks and
  re-forms. Three rules make that transition mean what it says, and each was
  wrong once:
  - **The angle floor and ceiling are not symmetric.** The floor
    (`min_annual_slope_pct`) is a qualifier — a trend can begin by getting steep
    enough. The ceiling (`max_annual_slope_pct`) is a **disqualifier**, applied
    to the signal and deliberately kept *out* of the state freshness watches.
    Folded in, it fired on the day a hot trend **decelerated** through the bar:
    a momentum-fade entry wearing a trend entry's clothes, and **18% of all
    signals**, against 0% that ever fired by crossing the floor upward. The
    ceiling exists because a parabola scores a very high r² — r² rises with
    slope for a given noise level — so linearity alone will not reject one.
    Note removing it *improves* measured excess (−0.63 vs −1.79); it is kept on
    the definition, not the backtest, and the sweep carries `null` so that stays
    checkable.
  - **A transition needs something to transition from** (`had_fit`). On the
    first bar with a full window, and on the first bar after an interior `Close`
    hole rolls out of one, yesterday has no fit at all — so "not in trend
    yesterday, in trend today" is an artefact of where the data starts. Without
    the guard a hole manufactures a phantom trend exactly `window` bars later,
    and **116 of the 904 tickers** on the 5y panel carry one. This is the same
    family as `drop_unsettled_bars`: a missing measurement must not read as a
    finding.
  - **The two tiers are armed independently.** Gating both off one combined
    "in trend" state let the far wider control-cohort door swallow a trend
    before it tightened — **33 signals against 1512 control rows**, from a rule
    worth 656 on its own. `max_partial_fails` widens the control cohort only; a
    test pins that changing it leaves the signal count untouched.
  Measured on the 904-name panel over 3 years, and the reason it ships
  `enabled: false`: 428 signals, excess **−1.79** at 30 days and **−2.22** at
  60, against a +2.27/+4.81 baseline. The control cohort reads −0.91, i.e.
  **relaxing the linearity requirement did better than demanding it** — which is
  the finding that cohort exists to surface. `min_r_squared` is the one axis with
  real gradient (0.9 → excess −0.06, win 60.6%, n=208).
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
- **`quality.py` is the one quality check, for both tiers 2 and 3.** There used
  to be two systems that shared no keys, no config shape and no code path:
  `fundamentals.quality.rules` (a pass/fail gate → the ⭐ badge) and
  `research.synthesis.dimensions` (a weighted 0-100 score → tier 3's anchor).
  They are one **registry** now, `config.json`'s `quality.parameters`. Each
  parameter declares where its value comes from (`source`, resolved by prefix:
  `yahoo_info` / `yahoo_stmt` / `yahoo_deep` / `ibkr` / `distress` / `moat` /
  `sec_flags`), which weighted `group`
  it belongs to, when it is affordable to collect (`stage`: `fast` for every
  tier-1 hit, `deep` for gated candidates only), and optionally a `gate`
  (`min`/`max`/`increasing`), a `score` (`good`/`bad` anchors) and/or
  `veto: true`. One `quality.evaluate` call yields all three verdicts at once.
  Rules that hold it together:
  - **The veto is a label, not a gate — `quality.veto_enforced` is `false`.**
    The rules still run, still record `Veto`/`Veto Reasons` and `Deep Veto*`, and
    still badge the card; what they no longer do is override the tier. The reason
    is measurement, not leniency: the thesis is that excluding the losers beats
    owning them, and only tier 4 grading vetoed names *against* clean ones can
    confirm it. Relabelled to AVOID, an excluded name is no longer comparable to
    anything, and on the risk/reward plane it would be a category rather than a
    point. Left as a label the same rule becomes a hypothesis you can watch —
    vetoed names should cluster in the high-risk quadrant, and if they do not,
    that is a finding about the rules. Set it `true` to restore the gate; both
    modes are pinned by tests, because each silently breaks a different thing.
    This is deliberately not `enabled: false` per veto parameter, which would
    stop them being collected and lose the record — the same distinction
    `has_values` draws between "failed" and "not evaluated".
  - **A `veto: true` parameter is an exclusion rule, not a stricter gate**, and
    it is the whole of tier C ("identify the losers"). It is **skipped by
    `gate_failures`** — the ⭐ set is already strict enough that most tickers
    fail one, and letting vetoes in would make the badge mean two things — and
    it inverts the missing-value rule: **a missing value never vetoes.**
    Unverifiable quality does not earn the badge, but unverifiable is not
    *proof of disaster*, and Yahoo leaves holes in nearly every company's
    statements. The layer therefore **fails open**: an EDGAR outage, a
    logged-out gateway or a statement Yahoo does not publish can only make it
    quieter. The sharp edge: `scalar` rejects `bool`, so a flag stored as
    `True`/`False` reads as *missing* and can never fire — every flag a
    resolver produces must be an **`int` 0/1**. `validate` cannot catch that
    (it sees config, not values); `tests/test_quality.py` and
    `tests/test_derived.py` pin it instead.
  - **When enforced, a veto overrides the tier and never the score.** With
    `veto_enforced: true` `_facts` forces `quality.veto_tier` ("AVOID") and
    `record_verdict` re-forces it over whatever it is handed. On the nightly path
    that is now a tautology — nothing there can report a disagreeing tier — but
    `enforce_veto` stays as the guard for every *other* way a verdict is
    recorded: a hand-written one, or a re-record after `quality.parameters` was
    retuned. The enrichment agent may argue a rule is miscalibrated *in its
    report*, where a human reads it and can retune the threshold, and its
    `rule_disputes` column makes that argument countable across a sector — but it
    has no field that touches the tier. The conviction stays on the record so
    tier 4 can measure what the exclusion cost. Vetoed signals are still alerted (sorted after the clean ones) and
    still bought by tier 4, flagged — same argument as `exits.py` flagging
    rather than closing: a ledger that declined to buy what it excluded would
    answer the thesis by deleting the evidence.
  - **Risk and reward are two axes, and the blend is kept only for
    continuity.** Every `quality.groups` entry declares an `axis`
    (`reward` | `risk`, default `reward`), and `quality.axis_scores` returns
    `reward`/`safety`/`risk` alongside the old 0-100 `score`. The point is that
    a single number cannot say whether a 53 means "mid risk, mid reward" or
    "high reward, high risk" — which is exactly the distinction the intended
    risk/reward quadrant chart exists to draw. This also fixed the weighting
    problem without touching a weight: on the blend the `risk` group fought for
    `0.10/1.24` ≈ 8% of the score against 19 metrics diluting each other to 0.4%
    apiece, and on its own axis it holds 100%. Three rules:
    - **The polarity is stated, never inferred.** Every `normalize` maps
      good→1, so the risk group's composite is a *safety* reading; `risk` is its
      complement so it reads the way an axis labelled "risk" must (high = bad).
      Both are returned so no caller has to remember the direction. A silent
      sign flip here would file the most dangerous companies in the buy
      quadrant and nothing else would notice.
    - **A missing axis is `None`, and must never be plotted as 0** — that would
      put an unmeasurable company in the best quadrant. Same rule as
      `quality.has_values`: not measured is not good news.
    - **`aggregate: {"worst_k": n}` averages the n *lowest* readings** instead
      of all of them, because risk is about the worst thing true of a company,
      not the average thing. **Enabled at `worst_k: 3` on both risk groups since
      2026-08-10**, after two failed attempts that are the whole reason the rule
      below exists.
      Measured: it lifts vetoed-vs-clean separation on the risk axis from an
      effect size of **+0.91 to +1.09**, and it automatically drops the
      no-variance metrics (`negative_equity` reads a clean 1.00 for all 503, so
      averaging let it dilute every company's risk while `worst_k` simply never
      selects it).
      **`worst_k` amplifies a miscalibrated anchor into control of the axis**,
      which is what sank it twice. The failure signature is a metric whose
      *normalized* reading centres far below 0.5 across the index: it then
      occupies a worst-k slot for nearly every company, and the axis stops
      measuring the company at all. The diagnostic is a per-metric median of the
      normalized value — anything under ~0.25 is marking the whole index bad.
      Three anchors had to be fixed first, in three different ways:
      - `officer_departure` — 8-K item 5.02 covers routine director elections, so
        it read 0.00 for MSFT, INTC *and* KO. A 100%-base-rate flag carries no
        information; `enabled: false`.
      - `downside_deviation` — its *implementation* was wrong, not its anchors
        (see `price_risk.downside_deviation_pct`). It occupied **89%** of all
        worst-2 slots and no S&P 500 member ever reached its `good: 10` bar.
      - `current_ratio` / `quick_ratio` — genuinely miscalibrated absolute
        anchors. Measured over 481 constituents: median current ratio **1.21**
        against `good: 2.5 / bad: 1.0` normalized to **0.14**, with 59% of the
        index below 0.25, so these two owned 2 of the 3 deep-stage slots for
        almost everything. Re-anchored to `1.8 / 0.6` and `1.3 / 0.3` — `bad` at
        roughly the 10th percentile and `good` set so the *median* company lands
        near 0.5. The textbook "current ratio above 2" describes 1960s
        manufacturers; modern large caps run lean working capital on purpose,
        which is the same argument that makes Altman Z structurally low for
        financials. MSFT moved 0.15 → 0.52 on that metric alone.
      Two more were found on 2026-09-27 by re-running that same diagnostic, and
      the first of them **reverses what this file used to say**:
      - `shelf_registration` — recorded here as the *suspected* fourth that
        "turns out to be fine", on the reasoning that like the other `sec_flags`
        it reads 0 → a clean 1.00 and so never enters a worst-k slice. True for
        78% of names and false for the rest: measured over the 175 tickers
        carrying a `_facts.json`, **22.2% have a shelf**, and for those the
        reading is 0.00 — the minimum possible, so it does not merely
        *sometimes* enter the slice, it **always claims one**, displacing a real
        distress metric. Cost to the affected names: risk **+7.9 median, +16.6
        max**, and shelf=1 median risk **78.7** against **63.5** for shelf=0. An
        S-3 is routine debt-shelf housekeeping for a large investment-grade
        issuer — **Alphabet** carried 16.6 risk points for having one. Same
        argument as `officer_departure`, a high-base-rate filing read as
        distress; `enabled: false` 2026-09-27. The lesson for the next
        `sec_flags` addition: "reads 0 for the examples I checked" is not a base
        rate — measure it.
      - `return_on_assets` — miscalibrated absolute anchors, the `current_ratio`
        case again. Over the same 175: normalized median **0.25** with **50.6%
        below 0.25**, because `good: 15` sat above the sample's *90th* percentile
        (raw p90 13.45, median 5.23), so essentially nothing reached it.
        Re-anchored `15 / 2` → **`9.1 / 1.3`** by the same method — `bad` at
        roughly p10, `good` set so the median company lands near 0.5. It sits in
        `financial_quality`, which takes a plain **mean**, so unlike the worst-k
        cases it was a uniform drag on every company rather than a takeover:
        sample reward median 60.6 → 61.3.
      **Neither change moved a quadrant threshold, and the reason is the rule
      below read in reverse.** Both parameters are `deep`, `quality.quadrant_of`
      is only ever called at `fast` (`quality.annotate`,
      `universe_scan.collect_one`), and the fast axes for all 903 cached
      constituents come out **bit-identical** across both edits. So re-anchoring
      `reward_min`/`risk_max` for them would have moved the nightly card's bar to
      fix a tier-3 reading. Check which **stage** a parameter lives at before
      recalibrating a threshold for it.
      Two live caveats:
      - **It shifts the LEVEL of the axis, so `quality.quadrant` must be
        recalibrated with it** — index median risk went 33 → 60, and leaving
        `risk_max` at 30 would have collapsed the buy quadrant to 1 name that had
        not become any safer. `risk_max` was re-anchored at the **same
        selectivity** (the percentile the old bar sat at, ~40% of the index),
        giving 30 → 56 and 47 buys across all 11 sectors. Same trap percentile
        scoring set off; see `peers.py`.
      - **`worst_k` is per group and therefore per *stage* too.** The `risk`
        group carries 10 scored metrics at `fast` and 17 at `deep`, so worst-3-of-17
        is a harsher slice than worst-3-of-10 and a deep-graded company reads
        riskier than the same company on the plane. Tolerable only because the 7
        deep additions are mostly binary `sec_flags` that read clean at 1.00 and
        so never get selected — if a future deep parameter centres low, it will
        quietly take over every tier-3 risk reading. Check the median normalized
        value before adding one. **That hazard was never hypothetical**:
        `shelf_registration` was one of those binary flags, and it read clean only
        for the 78% of names that have no shelf (above). So the thing to check is
        the flag's **base rate**, not whether a clean reading is possible.
  - **`enabled: false` makes a parameter invisible** — not gated, not scored,
    absent from `Quality Missing` and from the embed fields, and not counted in
    any weight. That is the flag's whole purpose: a company with no dividend
    earns the badge by switching `dividendYield` off, without deleting the rule
    and losing the record that it ever existed. Weights renormalize over what
    is left, so switching one off never silently reweights the rest.
  - **Missing ≠ failing ≠ not evaluated.** A missing value *fails its gate*
    (unverifiable quality does not earn the badge) but is *skipped* in the
    score. A group with parameters but no values scores a neutral **0.5**, not
    0 — that is what stops a bank being zeroed by statement rows Yahoo does not
    publish for it. A group with no parameters *at this stage* is dropped from
    the weighted mean entirely rather than neutralised. And with the layer off,
    `annotate` writes no columns at all: **absent columns mean "not evaluated"**.
  - **Values are keyed by internal key, rows by display label.** The hits frame,
    `latest_hits.json` and `signals.csv` all store fundamentals under
    `parameters[*].label`; every gate, score and `Quality Missing` entry uses
    the key. `quality.row_values` is the one bridge. Renaming a label is safe;
    renaming a key is not.
  - **Graded exactly once per scan**, by `quality.annotate` in
    `run_scanners.main()` right after the join and *before* the hand-off is
    written. `build_embeds` reads the recorded `Quality` column, so the badge
    and `latest_hits.json` cannot disagree. Don't reintroduce a second call
    site — the bug this fixed was the badge being computed inside
    `build_embeds`, which runs *after* `write_latest_hits`, so the hand-off
    never carried it and tier 3 was blind to tier 2.
  - **`quality.validate` runs before any config write** (`config_set` and
    friends call it). Every case it catches fails *silently* at runtime: an
    unknown `source` resolves to None for every company, a typo'd gate keyword
    is never applied, a group with no weight contributes nothing.
  - `migrate_config.py` proves the translation from the old two sections and
    can be deleted once `fundamentals` and `research.synthesis.dimensions` come
    out of `config.json`.
- **Tier 3's verdict is deterministic, and nothing revises it.**
  `research_report.deterministic_verdict` collects the `deep` stage, scores it,
  renders `output/reports/<TICKER>_<date>_financials.png` and writes
  `..._facts.json` — then sets `conviction = score` and
  `tier = tier_for(score)`. It runs inside the nightly scan, so the verdict
  exists before anything is posted. Even the batch **thesis is generated in
  Python** (`deterministic_thesis`, a rendering of the group breakdown) — same
  rule as tier 4's `conclusion` column: a sentence a model wrote is a sentence
  you cannot check. A narrative pass used to contribute a bounded
  `narrative_adj`; over its whole life it moved nine convictions by at most 5
  points against a ±15 bound and changed **no** tier, so on 2026-08-11 the field
  was removed rather than kept as a clamped exception to the rule. The judgment
  it represented did not disappear — it moved to `output/enrichment/`, where it
  is graded instead of trusted (`AI_ROLE.md`).
- **`universe_scan.py` is the risk/reward plane — the first thing here that
  grades the whole index.** Tiers 1–3 only ever grade what fired a screen or
  passed a gate, so the thesis (own the index minus the losers) could be argued
  but not *looked at*. This runs the `fast` stage over every constituent and
  writes `output/universe/`: a per-ticker cache, `risk_reward_<date>.csv`, the
  PNG (`charts.plot_risk_reward`) and a self-contained interactive HTML. Five
  rules:
  - **`fast` stage only, and it says so on every output.** The `deep` stage means
    an EDGAR fetch plus a `collect_yahoo` pass per ticker — fine for a handful of
    candidates, not for 500. So the risk axis here is the 13 `fast` distress
    parameters plus the 9 `market_risk` ones and **not** the 18 `sec_flags`
    ones. That is a genuinely partial reading, so `stage` and the metrics-used
    counts ride along on the table, the chart subtitle and the MCP payload. Same
    discipline as `metrics_used` and tier 4's `sufficient_n`.
  - **Per-ticker cache freshness, not whole-file.**
    `backtest_universe_cache.pkl` is one frame with one age check because it is
    one download; this is 500 independent fetches where any one can fail, so
    `is_stale` is per ticker and an entry that errored is always stale. Checkpointed
    every `progress_every` tickers, so a kill costs ~10 tickers rather than the run.
  - **It is the only thing here that retries or sleeps between calls.** The
    `fast` stage is 4 sequential Yahoo calls per ticker and nothing else in the
    repo had ever run that pattern more than ~21 times in a row; measured at
    ~1.5s/ticker, so ~13 min for the index. `request_delay_s` and a bounded
    backoff exist because 500× is a load shape we had not measured — not because
    a throttle was ever observed.
  - **Progress must be logged every N tickers.** `quality.fetch_fast` logs once
    *after* its whole loop, which at universe scale looks exactly like the
    stalled interpreter `mcp_tools.backtests.FIRST_OUTPUT_TIMEOUT` kills at 60s.
  - **The price panel is downloaded in bulk and threaded down**, never per
    ticker: `yf.download` batches server-side, so all 500 closes plus the
    benchmark is one threaded call. Deliberately *not*
    `backtest_universe.cached_panel` — that pickle is keyed to a fixed universe
    and a volatility reading two weeks stale is wrong in a way nobody notices.
- **The plane reaches the nightly alert; the universe pass does not run nightly.**
  Two separate things, and conflating them is the mistake to avoid:
  - `quality.annotate` records `Reward`/`Risk`/`Quadrant` (`AXIS_COLS`) on the
    same single grading pass that writes the badge and the veto, so the card,
    `latest_hits.json`, `signals.csv` and tier 4 all read one answer.
    `build_embeds` renders it as a `**Plane:**` line via `_plane_line`, which
    reads the recorded columns and **never recomputes**. Absent columns print
    nothing (not evaluated); an `unknown` quadrant names itself without inventing
    numbers. Like `VETO_COLS` and unlike `DEEP_VETO_COLS` these are written by the
    scan, so they stay **out** of `merge_history_csv`'s `protect=`.
  - The thresholds live in **`quality.quadrant`** (`reward_min`, `risk_max`), not
    in the `universe` section, so the card, the table, the PNG and the
    interactive page cannot disagree about where "buy" is.
    `quality.quadrant_of`/`quadrant_thresholds` are the only definition;
    `universe_scan` re-exports the labels rather than keeping a copy.
  - `run_scanners.price_history` makes **one extra bulk download** for the
    signalling tickers plus the benchmark, rather than reusing the scan's own
    panel. Both reasons are about the card and the plane agreeing about the same
    company on the same day: the scan panel is `data.download_period` (2y), which
    is *shorter than the longest price-risk window*, and it holds constituents
    only, so beta has no benchmark — and the benchmark cannot just be added to
    the scan download because every column there goes through every screen. It
    is a handful of tickers, so one batched call. Fail-open to the scan panel.
  - **`universe_scan.py` runs weekly over the *week's signals*, not the index**
    ("SP500 Universe Plane", Sunday 18:00, `run_universe.bat`, which passes
    `--from-signals`). `signal_tickers` reads `signals.csv` — which holds screen
    signals only, so "identified by a strategy" needs no extra filtering — for
    the last `universe.signal_window_days` (7), de-duplicated across screens and
    nights. A quiet week exits 0 having written nothing, rather than re-rendering
    the previous week's page over it. It posts nothing to Discord: a reference
    artifact is not an event, and the nightly card already carries the part that
    is actionable that day.
    **The full 903-ticker pass is therefore on no schedule.** Run it by hand when
    the whole plane needs refreshing — and note it is the base population for the
    sector-relative percentiles the scoring still needs, so it going stale
    silently blocks that work.
  - **Scope decides which files a run may write**, and this is load-bearing. The
    artifacts are date-stamped, so before `artifact_paths` existed a ten-ticker
    run resolved to *exactly* the same `risk_reward_<date>.csv`,
    `risk_reward.png` and `.html` as the full pass and silently replaced the
    day's whole-index plane with a plane of ten — no error, just a quietly wrong
    chart. Three scopes now: `SCOPE_UNIVERSE` keeps the configured names (the MCP
    tools glob them and the published page is built from them), `SCOPE_SIGNALS`
    writes `signals_plane_*`, `SCOPE_SUBSET` writes `subset_plane_*` — and
    **`--limit` is a subset too**, which it was not until 2026-08-13: the
    timing probe kept `SCOPE_UNIVERSE`, so it republished the day's whole-index
    plane from thirty names *and* rebuilt `peer_stats.json` from them, the one
    file whose entire contract is that only a full pass may narrow it. The
    **cache is shared** by all three, which is correct — it is keyed per ticker,
    so any run simply refreshes the rows it touched. A test pins that the three
    scopes cannot collide.
- **`peers.py` is sector-relative scoring — the fix for absolute anchors.** A
  parameter carrying `sector_relative: true` is read against its **sector's
  distribution** instead of its fixed `good`/`bad` pair. Measured cause: the first
  full pass excluded **26 of 31 utilities** on Altman Z, consecutive negative FCF
  and cash runway — all structural for a regulated business financing a rate base
  — and scored the sector the *highest* risk in the index. After: **5 of 31**
  vetoed, sector mean risk 38.7 → 28.7, and Financials' share of the buy quadrant
  42% → 23% against a 15% index weight. Five rules:
  - **It reads, never fetches.** The distributions come from `universe_scan`'s
    cache, which already holds resolved values for every constituent, and are
    written to `output/universe/peer_stats.json` by a **full** pass only — a
    subset run must never narrow the population everything else is compared
    against.
  - **The veto is a conjunction, never a percentile rule of its own.** A
    `sector_relative` veto fires only when the absolute threshold is breached
    **and** the value sits in the worst `veto_percentile` (10%) of its sector. So
    the layer can only ever make the exclusion *quieter* — a test asserts the
    vetoed set is always a subset of the absolute one. A pure percentile veto
    would exclude a fixed share of every sector forever and stop meaning "visibly
    falling over".
  - **Ties take the midpoint of their range, and this is load-bearing.**
    `fcf_negative_years` is capped at the statement window, so 20 of 31 utilities
    hold the identical worst value. Counting "peers at or below" handed all twenty
    rank 1.00 and therefore "worst 10% of sector" — reproducing the exact
    sector-wide false positive the module exists to remove. It was the difference
    between 67.7% and 16.1% of utilities vetoed. Never revert to `bisect_right`.
  - **Thin evidence falls back rather than guessing.** A missing stats file, an
    unknown sector, an uncollected parameter and a bucket under `min_peers` (12)
    all return None and the absolute anchor stands. That is what keeps a fresh
    install, a thin sector and a cold cache behaving exactly as before.
  - **`sector_relative: true` is inert on every `deep`-stage parameter**, and
    silently, by way of the fallback above. `peer_stats.json` is written by
    `universe_scan`, which runs the **`fast`** stage, so the file simply holds no
    distribution for a deep parameter and `normalized_of` takes the documented
    None path to the absolute anchor. Measured 2026-09-27: of the 30 parameters
    declaring `sector_relative`, the 25 that have a distribution are **all**
    `fast` and the 5 that do not are **all** `deep` — `return_on_assets`,
    `gross_margin`, `current_ratio`, `quick_ratio`, `net_debt_to_ebitda_veto`. So
    tier 3 grades those five cross-sector while the plane grades everything else
    against peers, which is how a defence prime's 25% gross margin reads 0.14
    beside a 39% index median. Nothing violates the fallback contract; what
    misleads is that the declaration reads as though it applies. The fix is
    either moving those five to `fast` or having `universe_scan` collect deep
    values for the peer file — both real options, neither done.
  - **A peer-scored metric always reads a median of 0.50, so anchor-calibration
    risk lives entirely in the metrics peers does *not* reach.** A percentile
    rank is uniform on [0,1] by construction, and it shows: measured over all
    903, every one of the 21 peer-active scored metrics reads a normalized median
    of **0.50** with ~24.8% below 0.25, to two decimals, identically. That is
    what makes the `worst_k` anchor diagnostic cheap — only a non-peer metric can
    ever centre low, so those are the only ones worth re-anchoring. Both
    2026-09-27 fixes were in that set, and the same pass **cleared**
    `gross_margin` (normalized median 0.48 over 172; the suspicion that
    `good: 60` described a software business did not survive measurement).
  - **Percentile scoring re-centres a metric at 0.5**, so switching it on moves
    the *level* of both axes and invalidates fixed quadrant thresholds. They were
    recalibrated once (`reward_min` 60→58, `risk_max` 25→30), again when
    `worst_k` was enabled (`risk_max` 30→56, 47 buys across all 11 sectors),
    and again when the MidCap 400 doubled the population (`reward_min` 58→59,
    `risk_max` 56→58, 80 buys of 903 across all 11 sectors). That third one
    barely moved, which is itself the finding: adding 400 mid-caps shifted the
    *level* of neither axis much. Expect to do it every time the scoring or
    the population changes, and re-anchor at the **percentile the old bar sat
    at**, never by eye.
  - **A caller that forgets to pass `sector` gets absolute anchors, silently.**
    Every grading entry point takes `sector` as its 4th argument
    (`evaluate`, `axis_scores`, `score_of`, `group_scores`, `veto_failures`), and
    omitting it is not an error — it is the documented fallback, so the call
    returns a plausible number computed on a different basis than the plane used.
    This shipped broken: `research_report.compute_quant_score` and `risk_report`
    both omitted it, so **tier 3 graded on absolute anchors while the plane used
    peers** — MSFT read reward 78.6 / risk 43.8 in a report and 63.0 / 53.6 on the
    chart — and `universe_scan.collect_one` stored a veto count (92) that its own
    rendered table (59) contradicted. Worse for the veto than for the score: with
    no sector the peer *conjunction* is skipped entirely, so tier 3 could exclude
    a name the plane had cleared, defeating the "peers can only make it quieter"
    property. `tests/test_peers.py` walks the AST of every production module and
    fails on any such call, because there is no observable symptom to test for.
  - **Explaining a score goes through `quality.normalized_of`, never
    `quality.normalize`.** Checking the entry points is not enough on its own:
    `normalize` is the raw two-anchor primitive, so calling it from production
    **bypasses the peer path by construction** — there is no `sector` argument to
    forget, and the AST check above has nothing to match. That is how
    `universe_scan._worst_risk` came to print "Altman Z 0.00; Int coverage 0.00;
    ST debt/cash 0.00" for NEE beside a risk of 35.2 that had scored those same
    numbers 0.82, 0.11 and 0.73 — a utility's absolute Altman Z is structurally
    low, so the column resurrected the exact sector bias `peers.py` removes from
    the score, and **62% of the 503 rows named the wrong worst-three**. A wrong
    explanation is worse than none: it aims tuning at metrics that are already
    fine. `normalized_of` is now the one definition of "what did this metric
    read", shared by `group_scores`, `_worst_risk` and `risk_report`; the AST
    check fails any direct `quality.normalize` outside `quality.py`.
  Note the ⭐ gates stay on absolute anchors deliberately — the badge is meant to
  be strict, and moving it would also move tier 3's candidate gate.
- **`universe_scan.regrade` re-grades cached entries at render time.** The cache's
  expensive content is `values`; the axes stored beside them are only what the
  config at collection time made of those numbers. Grading therefore happens when
  the table is built, so a threshold change or a peer-stats rebuild takes effect
  with `--no-fetch` and no network — the same rule `quality.verdict_of` already
  follows for tier 3. Don't "optimise" it back to trusting the stored axes.
- **`price_risk.py` is the `price_risk` resolver**, and its whole point is that
  the risk axis was built from accounting data alone — no volatility, no
  drawdown, no beta, which are the three most directly *measurable* risks a
  listed company has. Volatility (60d/252d), max drawdown (1y/3y), downside
  deviation, Ulcer index, beta and **downside** beta, SPY correlation, return
  skew, distance from the 52-week high, and 12-1 momentum. Four rules:
  - **No I/O, ever.** Every function takes a `close` series the caller already
    has. `quality.collect` gained a `benchmark` argument beside the `close` it
    already threaded through to `research_collect`, and `fetch_fast` takes a
    whole `closes` frame — which is what makes these parameters free in the
    nightly scan, the on-demand scan *and* the universe pass. Without a series
    `collect` falls back to fetching history, which is what the offline tests
    would otherwise have started doing.
  - **Too little history returns `None`, never a number.** A 3-year drawdown over
    40 bars is not a small drawdown, it is a wrong one, and the registry handles
    None correctly while a plausible wrong number gets scored. Every window
    carries its own `min_obs` — `pct_below_52w_high` included, or a young listing
    reads a confident 0% below its high.
  - **`downside_deviation_pct` divides the squared losses by the TOTAL bar count**,
    not by the number of losses — the Sortino semideviation. Dividing by the loss
    count instead yields the RMS of the negative returns, which for an index
    member is numerically almost identical to total volatility (measured across
    the S&P 500: median **30.86 vs 30.94**), so the metric silently duplicated
    `volatility_252d` while its `good: 10 / bad: 40` anchors stayed calibrated for
    the semideviation they were written for — the index *minimum* was 14.16, so no
    company ever reached "good", and enabling `worst_k` handed this one metric 89%
    of the slots on the axis. Fixed 2026-08-10; the median is now 21.58. The
    consequence worth keeping: **loss frequency counts**, so two names with equally
    deep losses rank differently when one falls twice as often. A test pins the
    ratio at volatility/√2 on an evenly-split series, which is what fails if the
    loss-count divisor ever comes back.
  - **Benchmark series are intersected, not zipped** (`_aligned_returns`), the
    same rule as `derived._aligned`: a beta against a benchmark offset by a day
    returns a plausible number and raises nothing. A test shifts the dates and
    asserts the answer changes.
  - **Beta is missing on the nightly path**, because SPY is not a constituent and
    adding it to that download would put a non-constituent through every screen.
    The universe pass adds it explicitly, so the chart has beta and tier 2 does
    not. `momentum_12_1` is computed but has no registry parameter yet — whichever
    one consumes it must sit on the **reward** axis.
  These live in their own `market_risk` group rather than in `risk`, and that is
  load-bearing: members are averaged *within* a group, so folding 9 price metrics
  into `risk` would have cut each existing distress metric's influence roughly in
  half — diluting the group the addition was meant to strengthen, which is exactly
  the mistake Tier C already made once.
- **`derived.py` is the `distress` and `moat` resolvers** — Altman Z (the 1968
  public-manufacturer form), Beneish M, interest coverage, cash runway, the
  accrual and short-interest reads, and the ten moat-persistence proxies (ROIC
  years above hurdle, ROIC/margin stability, gross-margin slope, revenue
  consistency and CAGR, FCF conversion and margin, capex intensity, incremental
  ROIC). Three rules:
  - **It does no I/O except `statement_frames`**, which `quality.collect` calls
    **once** and hands to `_statement_metrics`, `distress_metrics` and
    `moat_metrics` alike. That sharing is why both new resolvers sit at the
    `fast` stage and grade **every** tier-1 hit at zero extra network cost —
    `yahoo_stmt` was already fetching those three statements. Don't let a
    metric family fetch its own.
  - **Cross-statement years are intersected, not zipped** (`_aligned`). A ratio
    that mixes an income row with a balance column a year apart produces a
    plausible number and no exception at all.
  - **Row names go through `ROWS`, a candidate list per concept.** Yahoo's
    statement labels drift by sector and release; the alternative to tolerating
    that is a veto set that silently stops firing.
  Two calibration facts worth keeping, both measured rather than assumed:
  **Beneish flags growth**, not just manipulation (SGI and DSRI both rise with
  it) — NVDA scores −1.13 against −2.1…−2.6 for MSFT/JNJ/KO/MCD/AVGO, so its
  veto sits at −0.5 rather than the textbook −1.78 while the *score* still
  penalises it. And **profit-without-cash must be a trailing consecutive run**
  (`_trailing_divergence`), not a count over the window: scattered negative-CFO
  years are structural for banks, and counting occurrences vetoed JPM on a
  healthy balance sheet. Altman Z and interest coverage remain structurally low
  for financials and anything with a captive finance arm — that is the formula,
  not a bug, and the thresholds start loose pending tier-4 measurement.
- **`sec.flags()` is the `sec_flags` resolver** — going-concern language, 8-K
  item codes (1.03 bankruptcy, 2.04 acceleration, 3.01 delisting, 3.02
  unregistered sale, 4.01 auditor change, 4.02 restatement, 5.02 officer
  departure), `NT 10-K`/`NT 10-Q` late filings and `S-3` shelves. `deep` stage,
  so EDGAR only runs for gated candidates. The going-concern scan **must keep
  its negation guard**: the phrase appears in the negative far more often than
  the positive ("no conditions were identified that raise substantial doubt"),
  and a naive search flags most of the index. `_recent` and `_document_text`
  cache per process so `flags` and `fetch_filing_sections` share one fetch.
- **Tier 3 collects every stage, not just `deep`.** `deterministic_verdict`
  passes `stage=None` to `quality.collect`, because `compute_quant_score`
  grades `stage=None`. Collecting only `deep` — which is what it did until
  2026-08-09 — left all eleven `fast` parameters resolving to None and silently
  absent from the score: every `_facts.json` written before that date shows
  `fast 0/11`, with `financial_quality` (the heaviest group at 0.24) running on
  3 of its 8 metrics. If the two ever diverge again the symptom is the same and
  just as quiet.
- **The pretax-margin fallback lives only in `research_collect._financials`.**
  Yahoo publishes no `Operating Income` (nor `Gross Profit`) for banks and
  insurers, so the tier-3 chart falls back to `Pretax Income / Revenue` and
  records `margin_kind` so the panel and table can name the basis. Never port
  that fallback into `quality._statement_metrics`: its `operating_margin`
  feeds the `quality` gates, so a fallback there would silently change the ⭐
  badge and the tier-3 gate for every financial. A test pins the split.
  (`gross_margin` has the mirror-image problem — Yahoo returns a hard `0.0` for
  banks rather than omitting it, which is numeric enough to score them at the
  bottom of a metric that does not apply. `zero_is_missing: true` on that
  parameter is the fix.)
- **`research_report.py` is tier 3**, and it is entirely deterministic
  (`resolve_trigger`, `deterministic_verdict`, `deterministic_thesis`,
  `verdicts_for`, `list_candidates` + the gate, the financials chart/table/facts,
  `risk_report`, the Discord verdict cards). `research_collect.py` (Yahoo) and
  `sec.py` (EDGAR) are its collectors; `RESEARCH_DATA.md` maps what each source
  can and cannot supply.
  The qualitative half is the **`enrich` skill**, run from a Claude Code session
  — not a subprocess, and deliberately so. Two headless `claude -p` paths used to
  live here, each carrying an enumerated IBKR allow-list that had to stay
  byte-identical with the other; under `--permission-mode dontAsk` an un-allowed
  tool is refused *silently*, so a drift between them cost a whole report section
  with no error to explain it. A session governs its own tools, so that entire
  failure mode is gone along with the `.bat` files. What must **not** be
  reintroduced is a third such path: if the skill needs something new, it needs a
  tool or a subcommand, never an allow-list.
  **The account boundary survives the change.** `get_account_*` / `get_pa_*` are
  still out of scope — a security is graded on its own merits and tier 4 grades
  the signal against a fixed-notional virtual ledger, so what is already held
  changes neither, and no report may mention holdings, position size or
  concentration. It is now enforced in three places that are not allow-lists:
  `.claude/skills/enrich/SKILL.md` states it, `ibkr.py` never implemented the
  calls (`FORBIDDEN_CALLS` plus a test), and `risk_research`'s own prompt repeats
  it at the point of use.
- **Every tier writes one step log per run** (`output/logs/<run_id>.log`):
  one line per step — timestamp, phase, status, short description — built by
  `scanner_common.log_step` / `step()`. Tiers 1+2 use `SCAN`, `UNIVERSE`,
  `DOWNLOAD`, `SCREEN`, `YAHOO`, `QUALITY`, `HANDOFF`, `ARCHIVE`, `CHARTS`;
  tier 3 adds `CONTEXT`, `QUANT`, `SEC`, `FACTS`, `RECORD`, `REPORT`, `VERDICT`;
  tier 4 adds `LEDGER`, `MARK`, `EXIT`, `ANALYZE`; the enrichment record adds
  `ENRICH`.
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
  - **One process, one log.** `STOCK_ANALYZER_RUN_ID` is exported by the
    `.bat` and inherited by anything the scan starts. Unset means standalone —
    an ad-hoc `context`/`scan`/`enrichment` call mints its own id rather than
    going unrecorded.
- **There is no second half of the log any more.** A run used to span three
  processes — the scan, a headless `claude` run, and the `context` subprocess it
  spawned — so `log-session` rendered the model's tool calls out of Claude Code's
  session transcript and merged them in by timestamp. All of that went with the
  narrative pass (2026-08-11): one process, one log, and `prune_run_logs` moved
  into `run_scanners.main()` because it had been called only from `log-session`,
  which made the one job that bounded `output/logs/` the one job allowed not to
  run. If a merged transcript view is ever wanted again, it belongs beside the
  `enrich` skill, not inside the analyzer.
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
  This is also why the veto needs **two** column pairs. `Veto`/`Veto Reasons`
  are written by the scan itself and must stay **out** of `protect=` (the
  incoming value is the fresh one); `Deep Veto`/`Deep Veto Reasons` are written
  hours later by tier 3 and are in `VERDICT_COLS` for the same reason the
  verdict is. Folding them into one pair breaks whichever half you choose.
- **A verdict is recorded in exactly one of two tables, by provenance.**
  `signals.csv` gets `Verdict`/`Conviction` on the ticker's existing row;
  `on_demand_scans_results.csv` holds one row per `(scan_date, ticker)` you
  asked about yourself, with the ratios and the figures behind the judgment.
  The split exists because an ad-hoc look **has no signal row** — PGR and MSFT
  were deep-dived on 2026-07-26 and appear nowhere in `signals.csv` — so a
  single-table design drops exactly the verdicts you most want to study. The
  routing key is `source` in `<T>_<date>_facts.json`, written by
  `assemble_context` because it is the only place that knows; `record_verdict`
  only reads it back. `record_on_demand` skips a ticker `signals.csv` already
  covers, so the on-demand table can never accumulate orphan rows that no
  verdict will ever reach.
  **The enrichment record is a third table and joins on the same key.**
  `output/enrichment/enrichment.csv` is keyed `(scan_date, ticker)` like the
  on-demand one, but it is written by the agent rather than by any scan, so it
  is deliberately not routed by provenance: it simply names the pair it is about
  and warns (`ENRICH warn`) when no scan row matches, because a row nothing can
  join to is an enrichment tier 4 will never grade.
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
  - **`mark` re-syncs the ledger first**, and the verdict has to be carried
    across separately. Ordering inside `main()`: `run_ledger` (open → mark →
    exit scan) must come **before** `run_verdicts`, because the exit cards have
    to exist before the message is built and the exit scan needs filled entry
    prices — but the verdict is written to `signals.csv` after that. So
    `carry_verdicts_to_ledger` re-syncs once more at the end. Without it the
    tier and conviction would only reach the position on the *next* night's run,
    and the verdict is exactly the attribute tier 4 exists to grade. The
    A trailing `mark` in the old narrative `.bat` used to cover this; that file
    is gone, so the in-process carry is the only thing that does. A test pins it.
    `sync` alone, not `mark`: it copies the source row verbatim (verdict columns
    included) and needs no network.
    Both commands **exit 0 on failure** by design (`--strict` flips it) — a
    broken ledger must never take down the scan.
  - **The row carries the whole source row plus point-in-time derivations.**
    `Quality Missing` is exploded into `qr_<rule>` booleans against the rule set
    **recorded with the position** (`Quality Rules`, frozen at first sight), so
    retuning `quality.parameters` cannot rewrite past findings and a
    rule invented later never reads as "passed" on an older signal. Absent
    quality means *no* `qr_*` columns — "not evaluated" is not "failed", same
    rule as `quality.verdict_of`/`quality.has_values`. `mark` adds tier 3's
    `quant_score`, per-dimension `quant_*` and `qm_*` metrics from
    `<T>_<date>_facts.json`.
    The exclusion side mirrors it with `vt_<key>` against a frozen
    `Veto Rules`, plus a `vetoed` summary — but **the polarity is inverted**:
    `qr_` is True when the rule *passed*, `vt_` is True when the veto
    *tripped*. Its reason set is the **union of two columns written at two
    different times** (`Veto Reasons` from the scan, `Deep Veto Reasons` from
    tier 3), which is exactly why neither `vt_*` nor `vetoed` may go into
    `mark_columns()`: `protect=` inherits the recorded value and drops the
    incoming one, so a protected flag would pin itself to the fast-only answer
    and could never learn about the deep half. `qr_*` is out for the same
    reason. A test pins it.
    **`en_*` is out for a third variant of the same reason.** `mark` reads
    `output/enrichment/enrichment.csv` (`marking._enrichment_columns`, modelled
    on `_tier3_columns`) and flattens the agent's judgment into `en_stance`,
    `en_moat_view`, `en_social_sentiment`, `en_sources_n`, `en_concerns_n`. An
    enrichment is normally written *days after* the position opened, so a
    protected column would inherit "never enriched" and could never learn
    otherwise. Absent enrichment writes **no `en_*` columns at all**, same rule
    as `qr_*`. `analysis.py` grades the categorical three by group split (like
    the tier-3 verdict) and the two counts as ordinary numeric predictors, and
    the `roadmap` names all three even before a single position carries one —
    `question(..., always=True)`, because an omitted question is
    indistinguishable from one that came back empty.
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
  - **The veto is graded like everything else, which is the point of tracking
    it.** `analyze` asks three new questions: `vetoed` as a two-group split
    (reported **inverted**, "clean minus excluded", so a positive effect means
    the exclusion earned its keep), a `_rule_rows` pass over `vt_*` for which
    individual veto predicted the worst returns, and the matching `roadmap`
    entries so all of it is legible before it has data. `vetoed` is in
    `NOT_PREDICTORS` for the same reason `quality_pass` is: a 0/1 correlation
    would file the same finding twice and give it two votes in the one FDR
    family. `quant_moat` and `quant_risk` need no wiring — `QUANT_PREFIX`
    already earns them the tercile-plus-correlation treatment — but they only
    appear once a position **fills**, because `mark` skips pending rows before
    it reaches `_tier3_columns`.
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
    anything that can reach Discord, `confirm: bool = False` on every config
    writer (`config_set`, `config_edit`, `config_delete`).
    `.claude/settings.json` also omits those tools so they always prompt —
    but the default is the real guard, because **a permission rule that fails to
    match fails silently**. MCP rules are `mcp__stock_analyzer__<tool>`; a bare
    tool name matches nothing. The writers additionally refuse `discord.*` and
    `research.auto.discord_send` outright: a tool that could flip the send gate
    would make every other dry-run default decorative.
  - **A subprocess job must prove it started.** `run_script` pins `stdin` to
    `DEVNULL` and kills any child that writes nothing for
    `FIRST_OUTPUT_TIMEOUT` (60s). Both halves close the same silent failure: an
    unset `stdin` hands the child the server's **JSON-RPC pipe**, and a child
    that blocks during interpreter start-up writes nothing — so the log stays
    zero bytes and `job_status` reports `running`, truthfully, until
    `timeout=3600` expires an hour later. That happened twice on 2026-08-05
    while the identical command run by hand finished in 74s. The watchdog keys
    off **silence, not elapsed time** (a real scan runs for minutes after its
    first line), and a stall sets `stalled` so `jobs.submit` records the job as
    `error` — a plain non-zero exit still reports `done`, because several of
    these scripts use exit 1 to mean something specific. Relatedly, `jobs._tail`
    separates a **missing** log from an **unreadable** one: both used to return
    `[]`, which is the same answer a quiet run gives.
  - **`mcp_tools/universe.py` holds the one door to AI risk research, and it runs
    no model.** `risk_research(ticker)` assembles everything deterministic — every
    rule beside the threshold it was compared against, the moat and risk metrics,
    both axis coordinates, the sector peer group — and returns it with a prompt
    naming *only* the questions arithmetic cannot answer (competitive position,
    capital-allocation record, regulatory trajectory, whether a tripped rule is
    real or a sector artifact). The calling session does the reasoning with the
    tools it already has, and its prompt names which IBKR tool answers which
    question. This is deliberately **not** a `claude -p` path. Two used to exist,
    each carrying an enumerated IBKR allow-list that had to stay byte-identical
    with the other, and under `--permission-mode dontAsk` an un-allowed tool is
    refused *silently* — so a drift between them lost a report section with no
    error. A tool that returns a bundle has no subprocess, no allow-list and no
    new silent-failure mode.
    `universe_scan` (a `_script_job`) and `universe_quadrant` (cache-only, no
    network) are the other two.
  - **`mcp_tools/enrichment.py` is the way back in.** `enrichment_record` is the
    only tool in this server that records a *judgment* rather than a
    measurement, and the guard is that it cannot record a score: `validate`
    rejects `conviction`, `tier`, `score` and `narrative_adj` by name, and an
    invalid row raises instead of being written. That last part inverts the
    house rule — everything else here fails open, because a missing measurement
    is honest, whereas a wrong *categorical* value is a cohort of one that
    `analyze` will faithfully grade.
  There are deliberately **no file-reading tools** — reports, logs, CSVs and
  charts under `output/` are read with `Read`/`Glob`, which do it better. A
  `read_report`/`tail_log`/`backtest_results` reappearing means the surface
  crept; a test asserts they have not.
- **`mcp_tools/config_tools.py` is how strategy and quality parameters are
  edited from a session.** `params_list` is the discovery surface — start there
  rather than reading `config.json`, because it resolves the quality registry
  including the parameters that are switched **off** (the ones you usually want)
  and annotates each strategy value with its `tuning.sweeps` grid. Then
  `config_set` (one value, `create=True` to add one), `config_edit` (a batch,
  applied atomically under one diff — a `source` updated without its `group`
  scores nothing, so the unit of validity is the set) or `config_delete`.
  Four things must not be undone:
  - **Writes are surgical text edits, and must stay that way.**
    `json.dumps(cfg)` would expand every hand-maintained inline collection and
    churn ~200 unrelated lines, burying the change. `_rewrite_one_value` /
    `_insert_member` / `_delete_member` touch only the bytes they must, and the
    result is re-parsed and compared against the intended tree before anything
    is offered.
  - **`newline=""` on both writes.** Windows text mode translates LF→CRLF, and
    `config.json` is committed LF — without it every write rewrites all ~300
    lines and `git diff` shows the whole file. That silently defeated the
    surgical edit until it was fixed.
  - **Writable is `*_strategy` by suffix** plus a named list, so a new entry or
    exit strategy is tunable the day it is added rather than after someone
    remembers to extend a tuple.
  - **Validation runs before every write** (`config_tools.validate` =
    `_download_period_ok` + `quality.validate`). Every case it catches is one
    that fails silently at runtime.
- **Every generated file goes to `output/`** via
  `scanner_common.output_dir()` — logs, `latest_hits.json`, the cached price
  panel, all backtest tables/charts, the tier-3 facts, charts and the combined
  dossier that renders them (`output/reports/` — the one report directory), the
  signal history (`output/history/`), the enrichment record
  (`output/enrichment/`), the deep-dive step logs
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
- **Collection is `quality.collect`, and it is the only thing here that touches
  the network.** `sources_needed` asks the registry which resolvers this stage
  requires, and only those are fetched:
  - `yahoo_info` — snapshot values from `yf.Ticker.info`. A parameter with
    `"percent": true` is a fraction Yahoo returns and gets ×100 — but
    `dividendYield` is *already* a percentage, so it must NOT carry that flag.
  - `yahoo_stmt` — per-year metrics from `income_stmt`/`cash_flow`/
    `balance_sheet` (FCF, OpM, PM per fiscal year; ROE with an `info` fallback;
    ROIC = EBIT×(1−tax rate)/Invested Capital), stored as `[(fiscal_year,
    value)]` lists. Missing rows (banks lack Operating Income; a bank's hugely
    negative FCF is genuine) render as `n/a` — same tolerance rule throughout.
  - `yahoo_deep` — `research_collect.collect_yahoo` flattened by
    `quality.deep_metrics`. Every `.get(...) or {}` in there is load-bearing:
    `_estimates` sets its keys to **None** (not `{}`) when yfinance returns no
    frame, so a plain `.get("eps_revisions", {})` raises and aborts the whole
    deep pass for that ticker.
  - `ibkr` — `ibkr.metrics`, always optional (below).

  `fetch_fast` also adds a reserved `COMPANY_COL` ("Company") column (`info`
  longName/shortName) that `build_embeds` puts in each card title as
  `TICKER (Company Name)`; it is not a registry parameter and never renders as
  an inline field.
- **The ⭐ badge is the gate half of the registry.** `build_embeds` prefixes the
  configured `quality.badge` to any passing signal card's title, every screen
  automatically — **including `partial` setups** (it grades fundamentals, which
  are independent of setup completeness). The gate set is intentionally strict:
  **most tickers fail at least one**, which is why the tier-3 gate is a soft,
  configurable one (`research.auto.gate`: `quality_pass` | `all`) — a hard gate
  would routinely leave tier 3 with nothing. If `candidates` keeps coming back
  empty, that is the gates doing their job, and the fix is a config decision
  (loosen a threshold, or switch a parameter off, or move the gate to `all`),
  not a code change.
- **`ibkr.py` is IBKR over the TWS API (`ib_async`), and it is always optional.**
  Retail IBKR has **no headless API** — OAuth 1.0a is institutional-only — so
  the socket API needs IB Gateway or TWS logged in on this box. Everything
  IBKR-sourced therefore has to degrade: `available()` answers False when the
  gateway is down *or* `ib_async` is not installed, every getter returns `{}`,
  and the engine reads that as missing values. One `IBKR skip` step line makes a
  thinner alert visibly thinner. Three more rules:
  - **No account surface exists.** `reqAccountSummary`, `reqPositions`,
    `reqPnL` and friends are not implemented — not behind a flag. The user's
    real book is out of scope (a deep-dive grades the *security*; tier 4 grades
    the signal against a fixed-notional virtual ledger), and the MCP
    allow-lists used to enforce that by enumeration. A capability that was
    never written is stronger than an instruction a model can talk itself past.
    `FORBIDDEN_CALLS` documents the list and a test asserts none is called.
  - **It runs on a private event loop in a private thread** (`_run`).
    `ib_async`'s sync wrappers patch asyncio for re-entry in the *calling*
    thread; the MCP server runs tool bodies on anyio worker threads while its
    own loop is live, and a global asyncio patch under that fails once,
    mysteriously, in production.
  - **The moat/competitor graph does not come back.** `get_company_connections`
    / `get_company_themes` were Reflexivity products on the claude.ai MCP
    connector with no public-API equivalent. They fed only the narrative
    sections, and the nine IBKR MCP tools stay in both `.bat` allow-lists for
    exactly that reason.
- **The tier 1→2→3 hand-off contract** is `output/latest_hits.json`: per screen
  a `config_key`/`title`/`strategy` and a `hits` map of ticker → row, where the
  row is whatever the screen's frame held plus `Setup`/`Missing` (tier 1),
  `Quality`/`Quality Missing` (tier 2), `Company`, and the joined fundamentals
  under their **display labels**. `_json_safe` makes it JSON-native: NaN→`null`,
  numpy scalars→Python, and the `[(year, value)]` series→nested arrays.
  `quality.verdict_of` still grades a round-tripped row because `row_values`
  looks values up by label and `scalar` indexes the series positionally — that
  is what keeps *archived* scans readable after the format moves on. A test
  pins it. Only **enabled** screens appear: the
  hand-off follows the alert, unlike the backtest and tuner. The same shape is
  produced in memory by `scan_ticker` for on-demand tickers — keep the two
  identical, since that identity is the only reason tier 3 needs no second code
  path.
- **Tier 3 re-grades tier 2 against the parameters in force now** —
  `quality.verdict_of` (wrapped as `_row_quality`) recomputes rather than
  trusting the verdict the scan recorded, and both the gate
  (`list_candidates`) and the Discord card (`_facts`) go through it, so they
  cannot disagree. The reason: `config.json` gets retuned between scans, and a
  gate answering with last night's bar silently ignores the change until the next
  scan — loosening a rule and watching `candidates` still report the old failures
  is genuinely confusing. `output/history/` keeps the original verdict, so nothing
  historical is rewritten. `quality.has_values` guards the case where the row has
  nothing to grade (the layer was off that night): without it, re-grading would
  score every gate as failed rather than reporting "not evaluated".
- **Everything tunable lives in `config.json`** (per-screen strategy sections,
  the `quality` registry, charts, Discord) and all user-facing text (alert
  lines, backtest STEP logs, chart labels, and now the parameter labels and
  number formats) is built from those values at runtime — never hardcode a
  threshold or a literal like "150d SMA". Adding a metric to the alert is a
  config-only change: a new `quality.parameters` entry with a `label`, a
  `source`, a `format` and `display: true`. The embed field ordering used to be
  hardcoded in `fundamentals_fields`; it is the registry's order now.

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
- Nightly run: Task Scheduler task **"SP500 Breakout Scanner"**, Tue–Sat 07:00
  Israel time → `run_scanner.bat` → `run_scanners.py`, all four tiers in one
  process → `output/scanner_log.txt`. One task, one command, nothing chained
  after it since 2026-08-11. **It grades the previous session's close, and
  running it just after that close is a measured mistake** — it was Mon–Fri
  23:30 (16:30 ET, 30 min after the bell) until 2026-08-13, and at that hour
  Yahoo's **volume** has not absorbed the closing auction or the late prints
  while `Close` is already final, so nothing looks wrong. Over the 29 breakout
  rows in `signals.csv`, `Close` matched a later re-read to the cent 29/29 while
  `Vol Ratio` was understated in **20/29, mean 12.6%, max 58%** — split purely
  by run time, with every next-morning run matching to ±0.2%. Breakout C3 was
  therefore graded on incomplete volume, costing **8 signals against 17
  recorded** over the last ten scan dates; the 2026-08-12 zero-signal night was
  one of them (MPC reads 1.168 against the 1.1 gate once settled). Don't move it
  back toward the close to make the alert same-day. README's "Nightly schedule" section has the exact
  `Register-ScheduledTask` command and diagnostics; keep it in sync if the
  schedule changes. Result code `3221225786` in `Get-ScheduledTaskInfo` means
  the run was killed mid-scan (usually PC shutdown), and that night's alert is
  simply lost — though the signals themselves are archived before the verdicts
  are graded. `ExecutionTimeLimit` is registered at `PT3H`, which is now far
  more headroom than the run needs; check the live value with
  `(Get-ScheduledTask …).Settings.ExecutionTimeLimit` rather than trusting any
  doc.
- Yahoo quirks the code already tolerates (don't "fix" into hard failures):
  missing `info` fundamentals render as `n/a` (e.g. negative-equity companies
  have no Debt/Equity); individual ticker download failures just drop out of
  the scan; Wikipedia scraping needs the browser-like User-Agent header;
  tickers use `-` not `.` (BRK-B).
- **A session still in progress is not a bar** (`drop_open_session_bar`, called
  first inside `drop_unsettled_bars` so every download *and* every cache load
  passes through it). The guard below keys off a **null `Close`**; the *live*
  session has a perfectly real one — the last trade price — so it survives every
  check and gets scanned. What is wrong is the **volume**: an hour of trading
  against a 30-day average. Measured 2026-08-14, a run at 17:38 Israel time
  (11:15 ET) pulled a 502nd bar carrying **5–25% of a normal day's volume**, and
  breakout C3 compares volume to that average — so `is_volume_surge` was true for
  **0 of 903 tickers**, an impossible reading on settled data, and the scan
  announced "nothing today" while the same code on the previous settled bar fired
  two signals. Worse than a lost alert: the trend screen *did* fire on the partial
  bar, and that signal was written to `signals.csv` and bought by tier 4, where
  `merge_history_csv` de-duplicates but never deletes — a phantom signal is
  permanent unless removed by hand. Two states, one drop:
  - **Session open** → the bar is dropped and the last settled session is scanned,
    exactly as for a withdrawn close.
  - **Session closed earlier today** → kept, but warned: Yahoo's volume has not
    necessarily absorbed the closing auction, which is the same 20/29 · mean
    12.6% understatement that moved the nightly schedule off 23:30. A calibration
    hazard, not a wrong bar, so it is reported and not acted on — the split
    `warn_ticker_holes` already draws.
  The close is a fixed 16:00 ET, so on the ~3 early-close sessions a year a run
  between 13:00 and 16:00 drops a bar that did finish. That is the conservative
  direction and it says so in the log; inferring completeness from the volume
  itself would be guessing at the very thing that was wrong. `now` is injectable
  so the tests pin both sides without waiting for a market session.
  **The nightly schedule never hits this** — 07:00 Israel is ~9h after the close.
  This is a hazard of running by hand during US market hours.
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
  which ones it dropped (`DOWNLOAD warn`). It handles only the **null-`Close`**
  form; a session still being traded is a different shape and needs
  `drop_open_session_bar` below.
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
  **The sub-threshold case is reported, not acted on** (`warn_ticker_holes`,
  called from `drop_unsettled_bars` so every download *and* every cache load
  passes through one chokepoint). Keeping the bar when only a handful of tickers
  are blank is right — the other 490 must not lose the day — but the handful is
  not fine, it is a per-*ticker* version of the same poisoning, and it was
  completely silent until 2026-08-13. Measured on 2026-08-11: Yahoo had no bar
  at all for 28 constituents (ABBV, CARR, PSX, HLT, …), 5.6% of the index and
  nowhere near the 50% threshold, which blacked each of them out of the breakout
  screen for 312 sessions — about fifteen months — with nothing logged. A
  re-download does not repair it (the bar is absent upstream, not lost in the
  batch), so the warning is the entire remedy. **Interior holes only**: a leading
  NaN run is a young listing and a trailing one a delisting or an unsettled tail,
  both legitimate and neither poisoning any later bar's window. That distinction
  is what keeps it at ~30 tickers on a normal 2y panel instead of firing every
  night and going unread; tests pin both halves. On the 5y/904 backtest panel
  it reads **116** — bigger universe, longer window, same defect.
  **The same hole voids an excursion without touching the return.**
  `forward_trades` takes MFE/MAE with `rolling` at the default
  `min_periods=window`, so one interior NaN inside the holding window makes
  both NaN, while `return_pct` reads only the entry open and the exit close and
  survives. A closed trade with NaN excursions is therefore *correct* — the
  path could not be measured, and a max over the bars that happened to survive
  would be a plausible wrong number. `describe` already uses `nanmean`, so the
  cost is nil (3 of 9036 trades on 2026-08-13). `test_backtest_stats.py` used
  to compare the columns with a bare `.all()`, which a NaN fails — so an
  unmeasurable excursion reported itself as an *ordering violation*, the exact
  missing-vs-failing conflation the registry forbids everywhere else. It now
  asserts the ordering where measured and, separately, that an unmeasured one
  is NaN on both sides and still carries a real return.
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
