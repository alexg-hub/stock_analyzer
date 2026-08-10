# S&P 500 Scanners

Nightly scans of all S&P 500 stocks (via Windows Task Scheduler) with results
sent to Discord via a webhook — a short summary line per screen, then one
**embed card per ticker**: colored side-bar (orange = breakout, blue =
pullback, green = reclaim, gray = a *partial* setup), a title with the ticker
and company name, the signal description, a fundamentals field grid, and the
ticker's chart rendered inside the card.

Each screen reports **one signal list with two tiers** (there is no separate
"near-miss" list): `Setup` is `full` when every condition held and `partial`
when the setup is incomplete, with `Missing` naming the failing test. Full
setups are listed first. See [Signal tiers](#signal-tiers).

## The four-tier pipeline

The scan is the first of four stages. The first three narrow, each recording
its verdict so the next can read it; the fourth grades all three against what
the market actually did:

| tier | what it asks | how | output |
|---|---|---|---|
| **1 — technical** | Is the chart set up? | `breakout_scanner` / `sma_pullback` / `sma_reclaim`, nightly over all 503 names | `Setup` (`full`/`partial`) + `Missing` |
| **2 — quality** | Are the fundamentals sound? | the `fast` half of the `quality` registry over the tier-1 hits only, one Yahoo pass | `Quality` (the ⭐ badge) + `Quality Missing` |
| **3 — verdict** | How does the *business* grade out? | the `deep` half of the same registry, weighted to 0-100 → a tier and a conviction. Deterministic, inside the nightly scan. An **optional** narrative pass (the `deep-dive` skill over IBKR + SEC + live web) writes the report and may revise the conviction | a tier/conviction verdict + `_facts.json` + the financials chart |
| **4 — portfolio** | Was any of it *right*? | `portfolio_sim`: buy every recorded signal at the next open, grade every recorded attribute against the realized return, and watch the book for a double-top exit | `output/portfolio/positions.csv` + `findings.csv` + `exits.csv` |

**All four tiers run in `run_scanners.py`, in one process, and produce ONE
Discord message** — what fired, what it graded out at, and what to sell. They
used to be three posts at three different times, which meant the verdict for
tonight's signal arrived hours after, and detached from, the signal. Each
addition is individually fail-safe: a broken ledger or a failed verdict costs
its own section of the message and nothing else.

Tier 3 is still the only tier that costs real time per name, which is why the
first two exist — the gate (`research.auto.gate` / `max_reports`) caps how many
names reach it. The optional narrative pass runs afterwards from
`run_deepdive.bat`.

One honest caveat: Discord caps a message at 10 embeds / 10 attachments /
~6000 embed chars, and `send_discord_alert` batches on exactly that. This is one
*send*, one header, one contiguous batch — a busy night still splits into
several HTTP requests. See
[Tier 3](#tier-3-deep-dive-research), [Tier 4](#tier-4-the-virtual-portfolio)
and [Signal history](#signal-history).

Current screens:

1. **Breakout from consolidation** — upward breakout from a horizontal range.
2. **SMA pullback** — a stock in a year-long uptrend pulling back to its
   rising 150-day SMA.
3. **SMA reclaim** — a stock that spent most of the last year *below* its
   long-term SMA (`sma_days`) crossing back above it on volume
   (trend-reversal / Weinstein "Stage 2" entry). *Currently disabled in the
   nightly alert — see [Screen 3](#screen-3-reclaim-of-a-long-term-sma-after-a-downtrend).*

## Code layout

| File | Role |
|---|---|
| `run_scanners.py` | Entry point: one bulk download → every enabled screen → one Discord alert |
| `breakout_scanner.py` | Breakout screen module (condition math, alert section, hit chart) |
| `sma_pullback.py` | SMA-pullback screen module (same shape) |
| `sma_reclaim.py` | SMA-reclaim screen module (same shape) |
| `scanner_common.py` | Shared infra: config, tickers, downloads, statement access, Discord |
| `quality.py` | **The one quality check**: the parameter registry, its resolvers, the gates (tier 2's ⭐) and the weighted 0-100 score (tier 3's anchor) |
| `ibkr.py` | IBKR over the TWS API (`ib_async`) — ratios and market stats, optional, never account data |
| `migrate_config.py` | One-shot: proves the unified `quality` section reproduces the two it replaced |
| `charts.py` | Shared chart rendering (palette + per-screen chart builders) |
| `backtest_breakout.py` / `backtest_pullback.py` / `backtest_reclaim.py` | Single-ticker historical validators |
| `backtest_universe.py` | Universe-wide profit backtest: every screen × all history, buy the trigger / sell N days later |
| `tune_screen.py` | Parameter tuning: sweep one screen's thresholds, scored against the baseline **and** against cases that must keep firing |
| `research_report.py` | Tier 3: the candidate gate, the deterministic verdict, report archive, Discord verdict cards |
| `research_collect.py` | Tier 3 data collection: everything Yahoo has on one ticker (valuation, estimates, analyst, earnings, ownership, news) |
| `sec.py` | Tier 3 filings: EDGAR 10-Q/10-K MD&A / Risk Factors / Business sections + curated XBRL |
| `.claude/skills/deep-dive/` | The tier-3 procedure itself — synthesis is Claude's reasoning, not a function |
| `portfolio_sim/` | Tier 4: the virtual portfolio (`ledger` → `marking` → `analysis`, on `stats`) |
| `portfolio_sim/exits.py` | Tier 4's exit side: the double-top detector, the sell record, the Discord warning |
| `run_scanner.bat` / `run_deepdive.bat` | Task Scheduler entry point (all four tiers, one message), and the optional narrative pass it chains to |
| `tests/` | Invariant test suite + `run_all.py` runner (no test dependency; plain scripts) |

Adding a new scanner = new module exposing `CONFIG_KEY`, `scan()`,
`EMBED_COLOR` + `describe_hit()`, `plot_hit()` + one entry in
`run_scanners.SCANNERS` + a config section with an `enabled` flag.

## Screen 1: breakout from consolidation

All four conditions must be true on the most recent trading day (parameter
names refer to the `breakout_strategy` section of `config.json`):

1. **Horizontal movement** — over the previous `consolidation_window_days`
   trading days (excluding today),
   `(max High − min Low) / min Low ≤ max_consolidation_range_pct`.
2. **Upward breakout** — today's Close is above `breakout_multiplier ×` that
   window's max High (e.g. 1.01 = at least 1% above it, filtering marginal
   pokes above the range).
3. **Volume surge** — today's Volume ≥ `volume_surge_multiplier ×` the average
   volume of the previous `volume_sma_days` trading days.
4. **Long green candle** — today's Close is above
   `(1 + min_candle_body_pct) ×` the day's Open, so the breakout day itself
   closes strongly (green, with a body of at least `min_candle_body_pct`)
   instead of gapping up and fading to a weak or red close.

A ticker passing all four is a `full` setup; passing exactly three is a
**`partial`** setup, alerted in the same list with the failed condition named
in `Missing`. When the *breakout* condition is the one that failed, the partial
only counts if the close is within `near_miss_max_gap_pct` of the required
level, so routine volume spikes deep inside a range don't flood the alert.

## Screen 2: pullback to a rising SMA

All conditions must be true on the most recent trading day (parameter names
refer to the `pullback_strategy` section):

1. **Rising SMA** — the `sma_days` SMA of Close is higher than it was
   `sma_slope_lookback_days` trading days ago.
2. **Sustained uptrend** — the Close was above the SMA on at least
   `min_days_above_sma_pct` of the previous `trend_lookback_days` trading
   days, so today's touch is the exception in a year-long uptrend.
3. **Touch** — today's Close is within `touch_band_pct` of the SMA (either
   side).
4. **Reversal candle** (optional, `require_reversal_candle`) — the touch day
   is a small-body, long-tailed bar: body `|Close − Open| ≤
   max_candle_body_pct` of the Open **and** range `High − Low ≥
   min_candle_range_pct` of the Open. The body may be red or green — the
   shape (open and close close together, high and low far apart) is the
   "buyers stepped in at support" signal. Set `require_reversal_candle` to
   `false` to drop it. This is a strict filter; most ordinary touch days
   don't qualify.
5. **Fresh entry** (optional, `alert_only_on_band_entry`) — yesterday's close
   was still above the band, so the signal fires only on the day the pullback
   actually reaches the SMA instead of re-alerting every night the stock sits
   on it.

Unlike the breakout screen's prior-window (shift-by-one) convention, the SMA
here includes the current day — that is the charting-standard SMA a "touch of
the 150-day line" refers to.

## Screen 3: reclaim of a long-term SMA after a downtrend

> **Currently disabled** (`reclaim_strategy.enabled: false`, since 2026-07-26).
> The universe backtest measures it *below* a random entry — excess **−1.65**
> (mean +0.45% over 30 days against a +2.10% baseline), and the tuner found no
> threshold combination that lifts it (see [Tuning a screen](#tuning-a-screen-tune_screenpy)).
> It is switched off in the nightly alert while it is reworked. **`enabled`
> silences the alert only** — `backtest_universe.py` and `tune_screen.py`
> deliberately still run it, since a screen is switched off exactly when it
> most needs measuring.

The mirror image of screen 2 — instead of a dip in an uptrend, it catches
the *birth* of a new uptrend: a stock that lived below its long-term SMA
(`sma_days`) for most of a year crossing back above it. All conditions must be true on the
most recent trading day (parameter names refer to the `reclaim_strategy`
section):

1. **Above the cross level** — today's Close is above `(1 +
   cross_margin_pct) ×` the `sma_days` SMA, so a marginal poke over the
   line doesn't count.
2. **Long prior downtrend** — the Close was below the SMA on at least
   `min_days_below_pct` of the previous `below_lookback_days` trading days,
   so the reclaim is an event, not chop around a flat SMA.
3. **Volume confirmation** — today's Volume ≥ `volume_surge_multiplier ×`
   the average of the previous `volume_sma_days` days (a reclaim on dead
   volume usually fails).
4. **SMA slope floor** (optional, off by default) — when
   `min_sma_slope_pct` is set (not `null`), the SMA's change over
   `sma_slope_lookback_days` must be at least that fraction; filters
   knife-catching in stocks still in freefall, at the cost of later entry.
5. **Strong reclaim day** — the cross day closes strongly rather than being a
   weak or red cross. Two ways to qualify:
   - **body** — Close above `(1 + min_candle_body_pct) ×` the day's Open; or
   - **the day's move** — when `min_day_gain_pct` is set (not `null`), Close at
     least that much above the **previous close**, while still closing green.

   The second route exists because a body can't see an overnight gap, and the
   biggest reclaims gap. META's 2023-02-02 turn closed **+23.3% on the day**
   but had a body of only **+2.9%** (it opened +19.8% higher), so a body-only
   test rejects exactly the moves worth catching. A gap that fades to a red
   close never qualifies either way.
6. **Fresh cross** (optional, `alert_only_on_cross`) — yesterday's close
   was not yet above the level, so a stock that stays above it doesn't
   re-alert every night.

The fresh cross out of a real downtrend is always mandatory; the remaining
confirmations (volume, strong day, and the slope floor when enabled) set the
tier — none failing is a `full` setup, **one or two** failing is a `partial`
one (failures named in `Missing`). A cross that wasn't from a long downtrend, or
that misses on three or more confirmations, is dropped entirely.

First crosses of a long-term SMA are whipsaw-prone by nature — expect some
signals to fail back below the line; this is a watchlist alert, not an
entry system.

## Signal tiers

There is one signal list per screen. `Setup` grades it:

| tier | breakout | pullback | reclaim |
|---|---|---|---|
| `full` | all 4 conditions | all conditions (the only tier) | every confirmation held |
| `partial` | exactly 3 of 4 (+ proximity guard when the breakout leg failed) | — never; this screen stays strict | fresh cross out of a downtrend, 1–2 confirmations failing |

Both tiers are alerted together, sorted full-first, and both are carried into
`output/latest_hits.json` for the deep-dive. A partial card uses the grey side bar and
appends a **Missing:** line naming what failed.

Why: the universe backtest measured the tiers separately and the *partial*
breakout setups outperformed the full ones (+3.10% vs +0.67% over 30 days,
n=2418 vs 201), so suppressing them was discarding the better cohort. Splitting
them back apart for analysis is still one flag away
(`python backtest_universe.py --split-by-tier`).

Consequence worth knowing: the `⭐` quality badge used to be hits-only, so a
near-miss could never earn it. Now that everything is one list, a **partial
setup can carry the badge** — it grades fundamentals, which are independent of
how complete the technical setup is.

## The quality check (`quality.py`)

There is **one** quality check, and it produces both of tier 2's badge and
tier 3's score. It used to be two systems that shared no keys, no config shape
and no code path — `fundamentals.quality.rules` for the badge and
`research.synthesis.dimensions` for the score — which meant "quality" meant two
different things depending on which tier you asked.

Everything lives in `config.json`'s `quality` section. A **parameter** is one
measurable thing about a company:

```jsonc
"trailingPE": {
    "enabled": true,                  // the master switch -- see below
    "label": "P/E",                   // the column and embed-field name
    "source": "yahoo_info.trailingPE",// which resolver supplies the value
    "group": "valuation",             // which weighted bucket it scores into
    "stage": "fast",                  // fast = every hit; deep = candidates only
    "weight": 1.0,                    // its weight *within* its group
    "display": true, "format": "number",
    "gate":  {"max": 37},             // pass/fail -> the badge   (optional)
    "score": {"good": 12, "bad": 45}  // 0-1 anchors -> the score (optional)
}
```

### The `enabled` flag

Every parameter has one, and **`enabled: false` makes it invisible** — not
gated, not scored, absent from `Quality Missing` and from the embed fields, and
not counted in any weight. That is the whole point: a company that pays no
dividend can earn the badge by switching `dividendYield` off, without deleting
the rule and losing the record that it ever existed. Group weights renormalize
over what is left, so switching one off never silently reweights the rest.

```powershell
# From a Claude Code session (preview first, nothing is written without confirm)
params_list("quality")
config_set("quality.parameters.dividendYield.enabled", false)
```

### Where values come from

| `source` prefix | what it reads | stage |
|---|---|---|
| `yahoo_info` | `yf.Ticker.info` snapshot fields (P/E, PEG, D/E, revenue growth, yield, payout) | fast |
| `yahoo_stmt` | the last N annual statements: FCF, operating and profit margin per year, ROE (with an `info` fallback), ROIC = EBIT × (1 − tax rate) / Invested Capital | fast |
| `yahoo_deep` | `research_collect.collect_yahoo`: P/E percentile, analyst upside, forward growth, EPS revisions and trend, earnings surprise history, ROA, gross margin, net debt/EBITDA, buybacks, analyst mix | deep |
| `ibkr` | `ibkr.py` over the TWS API: Refinitiv ratios and market stats. **Optional** — off by default, and `n/a` whenever IB Gateway is not running | deep |

`stage` is what keeps the nightly run affordable: `fast` parameters need one
`info` call plus the statements and run for **every** tier-1 hit; `deep` ones
need ~9 more Yahoo round-trips and run only for the gated candidates.

Metrics Yahoo doesn't provide for a company (banks have no operating income,
negative-equity companies no D/E) show as `n/a`. A bank's FCF can be a large
negative number — that is Yahoo's genuine figure (deposit and loan flows
dominate bank cash-flow statements), not a bug.

### The badge (the gate half)

A hit whose fundamentals pass **every enabled gate** gets the configured badge
(default ⭐) in front of its card title, on every screen automatically. Gates
support:

- `min` / `max` — **strict** compare against the value (the latest fiscal year
  for multi-year metrics like FCF and margins);
- `increasing: true` — the latest fiscal year must be above the previous one
  (needs ≥ 2 years of data).

A **missing value fails its gate** — unverifiable quality doesn't earn the
badge. The gate set is deliberately strict, and **most S&P 500 names fail at
least one** — read the current thresholds from `config.json` (or `params_list`)
rather than from here, and loosen them there if the tier-2 gate is starving
tier 3.

The verdict is computed once, by `quality.annotate`, immediately after the
values are joined — before the alert is built and before the hand-off is
written. Both the badge and `latest_hits.json` read that one recorded result,
so a card and the row behind it can never disagree:

| column | meaning |
|---|---|
| `Quality` | `true` when every enabled gate passed — the ⭐ badge decision |
| `Quality Missing` | the parameter keys whose gate failed (`[]` when it passed) |
| *(both absent)* | quality was **not evaluated** — different from failing |

### The veto (the exclusion half)

The thesis behind this half is that **identifying the losers matters at least as
much as identifying the winners**: you beat the index by owning it minus the
names that break. A parameter carrying `veto: true` is an exclusion rule rather
than a quality gate, and the two are kept deliberately apart.

|   | badge gate (⭐) | veto (🚫) |
|---|---|---|
| asks | "is this a good company?" | "is this one visibly falling over?" |
| expected to fire | often — most names fail one | rarely |
| a **missing value** | **fails** the gate | **never** vetoes |
| appears in | `Quality Missing` | `Veto Reasons` |
| effect on the verdict | none directly | labels the row; gates it only if `veto_enforced` |

`quality.veto_enforced` is **`false`**, so a tripped veto is recorded and badged
but does not override the tier. That is a measurement decision, not leniency: the
thesis is that excluding the losers beats owning them, and only tier 4 grading
excluded names *against* clean ones can show it. Relabelled `AVOID`, an excluded
name stops being comparable to anything, and on the risk/reward plane it becomes
a category rather than a point. As a label the same rule is a hypothesis you can
watch — vetoed names should cluster in the high-risk quadrant, and if they don't,
that is a finding about the rules rather than a bug.

That middle row is the load-bearing one. Unverifiable quality doesn't earn the
badge, but unverifiable is not *proof of disaster* — and Yahoo leaves holes in
nearly every company's statements. The layer therefore **fails open**: an EDGAR
outage or a statement Yahoo won't publish can only make it quieter, never
trigger-happy. (One sharp edge: `scalar` rejects `bool`, so a flag stored as
`True`/`False` reads as *missing* and can never fire. Every flag a resolver
produces must be an `int` 0/1.)

Currently 17 veto rules across solvency (Altman Z, interest coverage, negative
equity **with** cash burn), cash burn (FCF-negative years, cash runway),
accounting (Beneish M, profit-without-cash), dilution, short interest, and the
SEC filing flags (going concern, restatement, bankruptcy, delisting, late
filing). Thresholds start at the loose end deliberately — tier 4 grades whether
each one earned its keep before any of them is tightened.

A vetoed signal is **not hidden**. It keeps its card (with the 🚫 badge, the red
side bar and a `Veto` field naming the rules), sorted after the clean ones; it
keeps its numeric conviction; and tier 4 goes on buying and tracking it. A
ledger that declined to buy what it excluded would answer the thesis by
deleting the evidence — the same argument that makes `exits.py` flag a position
rather than close it.

Three of these rules were rewritten during the first live shakedown, because the
single-leg version fired on healthy companies:

- **thin liquidity is normal** for a negative-working-capital business —
  Marriott (0.54/0.48) and HP (0.79/0.44) both trip the ratio pair while
  generating billions in free cash flow — so it only vetoes alongside cash burn;
- **a stock split is not dilution** — `get_shares_full` reports raw counts, and
  Fastenal's 2-for-1 measured as +100.4% against an actual buyback, so the
  earlier count is now restated onto today's share basis;
- **Beneish flags growth**, not only manipulation, so its veto sits well above
  the textbook −1.78 while the score still penalises it.

Altman Z and interest coverage remain structurally low for banks, insurers and
anything with a captive finance arm. That is the formula behaving as designed,
not a defect — but it is why those thresholds are loose and why
`enabled: false` per parameter is the intended escape hatch.

### The moat score

`moat` is a weighted group like any other, scored from ten measurable
persistence proxies rather than prose: years of ROIC above the configured
hurdle, ROIC and operating-margin stability, gross-margin slope, revenue-growth
consistency and 5-year CAGR, FCF conversion and margin, capex intensity, and
incremental ROIC. All ten come from the three annual statements `yahoo_stmt`
already fetches, so they cost no extra network round trip and grade **every**
tier-1 hit.

The qualitative side — switching costs, network effects, brand, regulatory
licence — stays in the deep-dive report's prose and moves only the bounded
narrative adjustment. Same rule as everywhere else here: a sentence a model
wrote is a sentence you cannot check.

### The score (the anchor half)

Parameters carrying a `score` block contribute to a weighted 0-100 number:
each is normalized to [0,1] against its `good`/`bad` anchors (the *ordering*
carries the direction, so an inverted metric like `net_debt_to_ebitda`
`{good: 0, bad: 4}` needs no special case), averaged within its group by
parameter weight, then across groups by group weight.

Three emptiness rules, all deliberate:

- a **missing value** is skipped in the score (though it still fails its gate);
- a group with parameters but **no values** scores a neutral **0.5**, not 0 —
  that is what stops a bank being driven to the bottom by statement rows Yahoo
  does not publish for it;
- a group with **no parameters at this stage** is dropped from the weighted mean
  entirely rather than neutralised — at `fast` there is no estimate data to be
  neutral *about*.

The score becomes tier 3's conviction directly: `conviction = score`,
`tier = tier_for(score)` against the `quality.tiers` bands. `migrate_config.py`
proves the gates and anchors reproduce the two sections they replaced.

## Usage

```
pip install -r requirements.txt
python run_scanners.py            # tiers 1+2: full S&P 500 scan + Discord alert
python run_scanners.py --no-send  # the same scan, cards printed instead of posted
python universe_scan.py           # the whole index on the risk/reward plane (~13 min)
python universe_scan.py --no-fetch                   # re-render from cache, no network
python research_report.py candidates                 # tier 3: who to deep-dive
python research_report.py risk INTC MSFT             # every risk rule beside its threshold
python research_report.py scan PGR                   # on-demand: tiers 1+2 for one ticker
run_ondemand.bat PGR                                 # on-demand: all three tiers
type output\logs\<run_id>.log                        # what that deep-dive actually did
python backtest_breakout.py       # historical validation, breakout screen
python backtest_pullback.py       # historical validation, pullback screen
python backtest_reclaim.py        # historical validation, reclaim screen
python backtest_universe.py       # universe-wide profit backtest (all screens)
python tune_screen.py sensitivity reclaim_strategy   # which thresholds matter?
python tests/run_all.py                              # the test suite
```

### Driving it from Claude Code (the MCP server)

`mcp_server.py` exposes all four tiers as MCP tools, so the pipeline can be run
from a Claude Code session instead of by hand. `.mcp.json` in the repo root is
the entire install — open the project and `/mcp` shows `stock_analyzer`
connected. To check it without a client:

```
python mcp_server.py --selftest    # list the tools, call the read-only ones
```

**Reports, logs, CSVs and charts are read with `Read`/`Glob`, not through
tools.** Everything generated lands under `output/` and is plain text or PNG, so
a wrapper would only get in the way. The tools are the things a file read cannot
do: run something, run it safely, or run it without blocking.

| Tool | What |
|---|---|
| `scan_status` | latest scan: date, per-screen counts, tickers |
| `scan_tickers` | tiers 1+2 for named tickers, on demand |
| `run_nightly_scan` | the full S&P 500 scan **(prompts)** |
| `deepdive_candidates` | who is worth a deep dive, per the tier-2 gate |
| `deepdive_context` | the deterministic tier-3 research bundle |
| `params_list` | **every tunable parameter**, its current value and the path to change it |
| `deepdive_post_verdicts` | record verdicts, optionally post them **(prompts)** |
| `deepdive_complete_log` | finish the log of a run that was killed mid-flight |
| `portfolio_status` / `portfolio_positions` | what is on the virtual book |
| `portfolio_open` / `portfolio_mark` | signals → positions; fill and mark them |
| `portfolio_exit_scan` | double tops on the book **(prompts)** |
| `portfolio_analyze` | which recorded attribute predicted the return |
| `backtest_universe` / `backtest_ticker` | the profit backtests |
| `tune_screen` | sweep one screen's thresholds |
| `config_get` | read config, webhook redacted |
| `config_set` / `config_edit` / `config_delete` | change one value, a batch, or remove a key **(all prompt)** |
| `job_status` / `job_result` / `list_jobs` | poll the long runs |

Anything that takes minutes — the nightly scan, the universe backtest, tuning,
marking, deep-dive context — returns a **`job_id`** immediately rather than
blocking the session. Poll `job_status(job_id)`; it returns the tail of that
run's step log, which is the only progress these runs emit.

**Six tools are deliberately left off the allow-list in
`.claude/settings.json`, so they prompt every time:** `run_nightly_scan`,
`portfolio_exit_scan`, `deepdive_post_verdicts`, and the three config writers
(`config_set`, `config_edit`, `config_delete`). The first three can post to the
live Discord channel; the last three rewrite `config.json`. They also all
default to dry-run (`send=False` / `confirm=False`), which is the guard that
matters — a permission rule that fails to match fails silently, but a default
cannot.

#### Editing strategy and quality parameters

`params_list` is the discovery surface — start there rather than reading
`config.json`, because it resolves the quality registry **including the
parameters that are switched off** (usually the ones you want) and annotates
each strategy value with its `tuning.sweeps` grid:

```
params_list("quality")             # every parameter, group and weight
params_list("breakout_strategy")   # thresholds + their sweep ranges

config_set("quality.parameters.dividendYield.enabled", false)   # previews
config_set("quality.parameters.dividendYield.enabled", false, confirm=true)

config_set("quality.parameters.currentRatio", {...}, create=true)  # add one
config_delete("quality.parameters.currentRatio")                   # remove one
config_edit([{"path": "...", "value": ...}, ...])                  # a batch
```

Every writer previews a unified diff, validates, and writes nothing without
`confirm=true`; each write backs the old file up to `output/config_backups/`
first. `config_edit` applies its whole batch or none of it — a parameter is
only coherent as a set, so a `source` updated without its `group` would score
nothing.

The writers refuse `discord.*` and `research.auto.discord_send` outright (the
webhook is live, and the latter gates every Discord send), and `data.*` — its
`download_period` accepts forms the validator cannot check, and a bad one leaves
every screen unable to fire while reporting zero signals rather than an error.

Validation before each write catches the things that otherwise fail *silently*:
an unknown `source` (resolves to `None` for every company), a typo'd gate
keyword (never applied), a group with no weight (contributes nothing), and any
enabled screen whose lookback no longer fits inside `data.download_period`.

Writes are surgical text edits — only the bytes that must change do, so
`git diff` shows your change and not a reformatted file.

The three skills (`deep-dive`, `tune-thresholds`, `universe-backtest`) still
drive the CLI rather than these tools. That is intentional: the unattended
nightly run executes those same skill bodies through `claude -p` behind a
`Bash(python research_report.py *)` allow-list, and a mis-specified allow-list
there fails silently. Interactively you can use either.

### Backtests

Each backtest runs the exact production condition math (the shared
`compute_*` functions) over a historical window for one ticker, with
step-by-step logging of every calculation, partial-setup analysis, a per-day
calculation table (CSV), and a chart (PNG):

```
python backtest_breakout.py --ticker JNJ  --start 2025-01-01 --end 2025-10-31
python backtest_pullback.py --ticker MSFT --start 2024-01-01 --end 2025-06-30
python backtest_reclaim.py  --ticker META --start 2023-01-01 --end 2023-12-31
```

`run_backtests.bat` runs all three default validation cases in one go and
writes their combined step-by-step output to `output/backtest_log.txt`
(overwritten each run) — separate from the nightly production
`output/scanner_log.txt`.

### Where generated files go

Everything the project *generates* lands in **`output/`** (gitignored as a
single directory): both logs, the `latest_hits.json` scan hand-off, the cached
price panel, and every backtest table and chart. The project root holds only
inputs — code, `config.json`, docs. `config.json` keeps storing bare filenames
(`backtest_universe_cache.pkl`, …) and `scanner_common.output_dir()` resolves
them; an absolute path in config still overrides. There are **no exceptions** —
the tier-3 reports (`output/reports/`), the signal history (`output/history/`)
and the deep-dive step logs (`output/logs/`) live there too.

### Universe backtest — did the screens make money?

The single-ticker backtests prove the *math* fires on a known case.
`backtest_universe.py` answers the different question: **if every signal had
been bought and sold `holding_days` later, what was the profit?**

```
python backtest_universe.py                            # config defaults: the wait x hold grid
python backtest_universe.py --entry-delay 0 --holding-days 30   # a single cell
python backtest_universe.py --entry-delay 0,2,5 --holding-days 10,30,60
python backtest_universe.py --screens breakout_strategy --entry signal_close
python backtest_universe.py --split-by-tier            # full vs partial setups
python backtest_universe.py --years 5 --refresh        # longer window, fresh download
```

It downloads the universe once (analysis window + the longest screen's
rolling-window warm-up) and **caches it to `output/backtest_universe_cache.pkl`**, so
re-running after a `config.json` tweak takes seconds; `--refresh` re-downloads.
It adds no condition math — every screen's `compute_*` already returns
`(days, tickers)` frames, so the whole `signal` frame is masked against a
vectorized forward-return matrix.

Per screen it measures **one cohort — every signal the scan would have sent**
(`fires_mask`, i.e. both tiers). `--split-by-tier` breaks it into `full` and
`partial` instead; for the pullback screen, whose `partial` set is never
alerted, that tier is a pure control group.
Each cohort is compared against two baselines: **random entry** (the same
forward-return matrix over *all* stock-days — the bar a screen must clear) and
**SPY buy-and-hold**. Outputs: a console stats table + per-signal-year
breakdown, plus (all under `output/`) `backtest_universe_trades.csv` (every
trade, with `status` `closed`/`open`), `backtest_universe_summary.csv`, and
`backtest_universe.png`.

Entry conventions: `next_open` (default — the signal is only known after the
close, so the earliest tradeable price is the next open) or `signal_close`
(the price shown in the alert). `mfe_pct`/`mae_pct` are the best/worst excursion
while the position was open — useful for judging whether a stop or target would
help.

### The wait × hold grid

Two timing knobs, both in **trading** days, swept as a cross product:

- **`entry_delay_days` (x)** — how much longer to wait *beyond the earliest
  tradeable bar* before buying. `x=0` buys as soon as possible (so look-ahead is
  impossible however the entry convention is set); `x=3` waits three more days
  and buys that open.
- **`holding_days` (y)** — how long the position is then held after the entry
  day.

```
signal day i  (its close is only known after the bell)
  x=0 -> buy Open[i+1]      x=1 -> buy Open[i+2]      x=2 -> buy Open[i+3]
  then sell at the Close y trading days after entry
```

The run prints a wait × hold matrix of mean return and win rate per screen and
writes `output/backtest_universe_grid.png` — one heatmap panel per screen, cells
labelled with mean return and coloured by **excess over the random-entry
baseline** on a diverging scale whose neutral point is exactly zero (blue beat
buying at random, orange did worse).

Because a full grid would be ~1M trade rows, **per-trade rows are written for
one cell only** — `backtest.detail`, an explicit `{entry_delay_days,
holding_days}` pair (not "first in the list", so widening the swept lists never
silently moves it). That cell also gets the detailed stats table, the per-year
breakdown and `backtest_universe.png`. Every cell still gets full aggregate
stats in the summary CSV and the grid.

**Read these caveats before believing any number** (they are also printed in
the run header):

- **Survivorship bias** — the universe is *today's* S&P 500, so companies
  dropped, acquired or delisted during the window are absent. Results are
  biased upward. This is a screen-*comparison* tool, not a tradeable backtest.
- **No costs, no dividends** — no commission, spread or slippage; returns are
  price-only (splits handled, dividends ignored).
- **No position sizing or capital limit** — every signal is an independent
  equal-weight trade, even when dozens fire on the same day.
- **Signals cluster in time**, so trades overlap and share market beta:
  per-trade statistics are *not* independent samples (`distinct_dates` shows
  how concentrated a cohort is). Don't read a t-statistic off them.

## Tests (`tests/`)

```
python tests/run_all.py              # offline + cache-backed tests
python tests/run_all.py --network    # also the Yahoo round-trip test
python tests/test_forward_trades.py  # or run one directly
```

Plain scripts, no test dependency — each prints `OK`/`FAIL` per check and exits
`0` pass / `1` fail / `2` skipped, which is how the runner classifies them.

| file | needs | asserts |
|---|---|---|
| `test_quality.py` | nothing | The quality registry: `enabled: false` removes a parameter from the gate, the score, `Quality Missing` **and** the embed fields; missing ≠ failing ≠ not-evaluated; a group with no values is neutral 0.5 and one with no parameters at this stage is dropped; strict gate compares and two-year `increasing`; weights renormalize; a JSON round trip changes no verdict; `validate` catches every silent misconfiguration. **The veto layer**: a missing value never vetoes while the same value still fails every badge gate; a veto never enters the badge's failed list; a bool reads as missing so flags must be ints; thin liquidity is only distress alongside cash burn; `validate` refuses a veto with no gate |
| `test_derived.py` | nothing | Altman Z and Beneish M pinned against hand-computed values; a missing index yields `None` rather than a partial composite; flags are `int` not `bool`; negative equity alone is not distress but negative equity **with** burn is; profit-without-cash counts a trailing consecutive run, not scattered years; the moat proxies (CAGR, FCF margin, capex intensity, incremental ROIC) match their definitions; cross-statement years are intersected, not zipped; a thin filing yields all-`None`, never an exception |
| `test_ibkr.py` | nothing | No account/position/PnL function exists **or is called**; a missing `ib_async` and a refused connection both degrade to `{}` without raising; the Refinitiv parser maps known fields, keeps unknown ones, and drops the `-99999` sentinel |
| `test_combined_alert.py` | cached panel | **One** `send_discord_alert` for the whole night, carrying signal + verdict + exit cards with every chart still attached and no orphans; the verdict is recorded whether or not `discord_send` is on; a failing verdict pass or exit scan costs its own section and not the alert; tonight's verdict reaches tonight's position |
| `test_forward_trades.py` | nothing | Trade arithmetic on a synthetic panel: entry/exit offsets for both conventions and any delay, the excursion window, tail NaNs, `delay=0` identity, input rejection |
| `test_signal_contract.py` | cached panel | `fires_mask` is `signal` or exactly `signal \| partial`; tiers disjoint; `Setup`/`Missing` agree; one card per signal with the grey bar + **Missing** line on partials; hand-off is a single list; `find_ticker` resolves either tier; an empty day doesn't crash. Then with stubbed fundamentals: the tier-2 verdict survives the JSON round trip, the ⭐ badge is exactly that verdict, the archive accumulates without duplicating a re-run, and the gate ranks/filters candidates |
| `test_research_output.py` | nothing | Tier-3 output: the margin falls back to pretax exactly when Operating Income is absent and names its basis; an empty trailing period doesn't consume a slot; the chart renders for complete/bank/single-period/all-missing/no-data input without raising; `quality._statement_metrics` stays untouched by the fallback; verdict cards bind their chart, read figures from the facts file, and fit the embed budget |
| `test_portfolio_sim.py` | nothing | Tier 4 on a synthetic panel and synthetic history: the entry price is `Open[t+1]` and agrees with `forward_trades` cell for cell; a re-`open` refreshes attributes but never erases a mark or re-freezes the recorded quality rule set; a signal whose entry bar has not traded stays `pending` and is in no statistic; `qr_*` distinguishes failed from not-evaluated; Mann-Whitney/Spearman/BH match hand-computed values; nothing under `min_n` is ever called significant, and every section (including `roadmap`) is present |
| `test_backtest_stats.py` | cached panel | `cohort_values` == the `collect_trades` path at several (wait, hold) cells; real rows re-derive from the panel at a nonzero delay; the trades CSV reconciles with the summary grid |
| `test_path_equivalence.py` | **network** | Screening out of the bulk panel gives the same dates as the single-ticker download, plus the documented JNJ/MSFT/META cases |

**They assert invariants, not recorded output.** Since `config.json` gets retuned
constantly, any test comparing against saved counts would be stale within a
session — so the checks are properties that hold at *any* thresholds. The one
exception is the documented validation dates in `test_path_equivalence.py`, which
are inherently config-dependent and are therefore reported as `INFO` if tuning
moves them, not as failures.

Tests **never download** (except the `--network` one) and never send to Discord:
they read the panel `backtest_universe.py` already cached, and a missing cache is
a skip with instructions rather than a two-minute surprise.

## Tier 3: the graded verdict

Tiers 1 and 2 are cheap and run over the whole universe. Tier 3 costs ~9 extra
Yahoo round-trips per name, so it runs on a handful of tickers chosen by the
tier-2 gate (`research.auto.gate`, capped by `max_reports`).

**The verdict is deterministic and computed inside the nightly scan.**
`deterministic_verdict` collects the `deep` half of the quality registry, scores
it, renders the financials chart, writes `<TICKER>_<date>_facts.json`, and sets:

```
conviction = score                 # the weighted 0-100 quality score
tier       = tier_for(score)       # STRONG / WATCH / PASS, per quality.tiers
```

It is recorded to `output/history/` and posted in the same Discord message as
the signal that produced it. Even the one-line thesis on the card is generated
in Python (`deterministic_thesis`, a rendering of the group breakdown) — same
rule as tier 4's conclusions: a sentence a model wrote is a sentence you cannot
check.

**The narrative pass is optional and revises rather than originates.**
`run_deepdive.bat` runs the `deep-dive` skill afterwards over SEC filings,
IBKR's competitive graph and live web research; it writes the full report and
may move the conviction by a bounded `narrative_adj` (±`research.synthesis.
narrative_adj_max`). Switch it off with `research.narrative.enabled` and nothing
downstream is missing — the verdict already exists, was already recorded and
already went out.

Why it was split: the verdict used to arrive hours after the signal, from an
LLM, and only if that LLM ran to completion. A run killed mid-flight (result
`3221225786`, usually a PC shutdown) left the night with no verdict at all.

### The hand-off

`output/latest_hits.json`, rewritten every scan (even an empty one):

```json
{
  "scan_date": "2026-07-23",
  "generated_at": "2026-07-25T16:01:48+00:00",
  "screens": [{
    "config_key": "breakout_strategy",
    "title": "Breakout from 312-day consolidation",
    "strategy": { ...the config section the screen ran with... },
    "hits": {
      "CSX": {
        "Close": 52.81, "Range %": 88.7, "Vol Ratio": 2.37,
        "Setup": "partial",
        "Missing": "range too wide: 88.7% > 38% limit",
        "Quality": false,
        "Quality Missing": ["debtToEquity", "operating_margin"],
        "Company": "CSX Corporation",
        "P/E": 30.95, "FCF": [[2024, 2718000000.0], [2025, 1711000000.0]]
      }
    }
  }]
}
```

Row keys are whatever the screen put in its hits frame, so the day-stat columns
differ per screen; `Setup`/`Missing`/`Quality`/`Quality Missing`/`Company` are
common to all. Only **enabled** screens appear — the hand-off follows the alert
(unlike the backtest and tuner, which deliberately ignore `enabled`).

### Choosing candidates

```powershell
python research_report.py candidates          # the configured gate
python research_report.py candidates --all    # every tier-1 hit
python research_report.py candidates --json   # machine-readable

python research_report.py verdicts            # grade tonight's candidates now
python research_report.py verdicts MSFT JNJ   # ...or these names
```

`verdicts` is the same call the nightly scan makes: it records tier and
conviction to `output/history/` and prints them. Posting is the scan's job.

A ticker you name explicitly needs none of this — `scan` and `/deep-dive` run
tiers 1 and 2 for it on the spot (see *On-demand* below), so the candidate list
is only about who the *nightly* run should pick.

Ranked **quality-pass first, then `full` before `partial`, then ticker**, so a
cap takes the best candidates rather than an arbitrary slice. The gate
(`research.auto.gate`) is `quality_pass` or `all`; it is a *soft* filter, printed
with a footer saying how many it held back, and it never overrides a ticker you
name explicitly.

**Quality is re-graded against the rules in force now**, not the verdict the scan
recorded — so if you loosen a gate in `quality.parameters`, `candidates` reflects it
immediately instead of waiting for the next scan. The dated snapshot in
`output/history/` keeps the original verdict, so the archive is never rewritten.
A hand-off predating the verdict is gradeable the same way; one scanned with
fundamentals switched off has nothing to grade and honestly reports "not
evaluated" rather than failing every rule.

### Running it

The deterministic verdict needs no invocation — it happens inside the nightly
scan. What follows is the **optional narrative pass**.

On demand, ask for a deep-dive and the `deep-dive` skill takes over. Nightly it
is automatic: `run_scanner.bat` chains to `run_deepdive.bat`, which asks
`research_report.py auto-prompt` what to do (exiting quietly if the gate is
empty or `research.narrative.enabled` is false) and then runs the skill through
headless Claude Code:

```
claude -p --model <research.narrative.model> --permission-mode dontAsk
        --allowedTools "... mcp__claude_ai_Interactive_Brokers_IBKR__*"
```

The IBKR MCP tools **must** be in the allow-list: under `dontAsk` an un-allowed
tool is refused silently, which would drop the moat/competitor section with no
error to explain it. Don't add `--bare` (forces an API key, dropping the OAuth
credential the IBKR server is bound to) or `--strict-mcp-config` (ignores
registered servers).

Each report is written to `output/reports/<TICKER>_<scan_date>.md`. If the pass
revises a conviction, that revision is recorded through the same
`post-verdicts` path the scan used, and `run_deepdive.bat`'s trailing
`portfolio_sim mark` carries it onto the position.

Two independent switches: `research.auto.{enabled, gate, max_reports,
discord_send}` governs the **deterministic** verdict inside the scan;
`research.narrative.{enabled, model}` governs this pass. The common case is
wanting a graded verdict every night and a written report only sometimes.

### The financial trend chart

Alongside the report, `assemble_context` renders
`output/reports/<TICKER>_<scan_date>_financials.png` — **five metrics × two
periodicities** as small multiples: revenue, earnings, margin, free cash flow
and debt/equity, over the last 4 fiscal years (left) and 4 quarters (right).
One metric per row so each keeps its own scale (revenue dwarfs the rest), with
the latest period highlighted. The same numbers go into the report as a
markdown table, and the chart is embedded in the Discord card.

**Margin is issuer-dependent and the chart says which.** Yahoo publishes no
`Operating Income` for banks or insurers — and no `Gross Profit` either — so the
margin falls back to `Pretax Income / Revenue`, and the panel is labelled
"Pretax margin (no Operating Income)". That fallback is deliberately confined to
the tier-3 collector: tier 2's quality rules keep the strict Operating-Income
definition, so the ⭐ badge is unaffected.

Everything on the chart, in the table and on the Discord card is generated by
Python before the model runs — `charts.plot_financials`,
`research_report.financials_table_md`, and a `_facts.json` the card reads back.
The model supplies only the tier, conviction, adjustment and thesis. Sizing:
`research.financials` (`years`, `quarters`, `chart_dpi`).

### The Discord verdict card

One card per ticker rather than one line for all of them: a tier-coloured side
bar (colours from `research.synthesis.tiers`), the thesis, the financials chart,
and six decision fields — quant score with the narrative adjustment, the trigger
(screen + full/partial), the tier-2 quality result with the failing rules named,
price vs analyst target, P/E with its 2-year percentile, and next earnings.
`send_discord_alert` batches them under Discord's caps, so a five-report night
still fits (~1.1k of the ~5500-char embed budget).

## The step log — what a run actually did

All three tiers write **one log per run**, `output/logs/<run_id>.log` — one line
per step: timestamp, phase, status, a short description. The nightly chain mints
a single run id in `run_scanner.bat` and exports it, so tiers 1, 2 and 3 all
append to the same file and one night is one log.

```
2026-07-27 23:30:01  SCAN      start   nightly scan (tiers 1+2)  run=a1b2c3d4
2026-07-27 23:30:03  UNIVERSE  ok      503 S&P 500 tickers from Wikipedia
2026-07-27 23:30:22  DOWNLOAD  ok      2y of 1d: 503/503 ticker(s), 501 bars  (18.6s)
2026-07-27 23:30:22  DOWNLOAD  warn    dropped 2026-07-24 -- no settled close for most tickers; scanning 2026-07-23 instead
2026-07-27 23:30:25  SCREEN    ok      breakout_strategy 2026-07-23: 4 signal(s) (2 full, 2 partial)
2026-07-27 23:30:27  SCREEN    ok      pullback_strategy 2026-07-23: 2 signal(s) (all full)
2026-07-27 23:30:27  SCREEN    off     reclaim_strategy disabled in config
2026-07-27 23:30:39  YAHOO     ok      fundamentals for 6/6 ticker(s)  (12.1s)
2026-07-27 23:30:39  QUALITY   ok      1/6 ticker(s) passed -- RL
2026-07-27 23:30:39  HANDOFF   ok      latest_hits.json: 6 row(s) across 2 screen(s)
2026-07-27 23:30:39  ARCHIVE   ok      6 row(s) -> hits_2026-07-23.json; signals.csv now 63 row(s)
2026-07-27 23:30:45  CHARTS    ok      6 rendered, 0 failed  (5.8s)
2026-07-27 23:30:47  DISCORD   sent    1 message(s), 2 card(s), 6 chart(s)
2026-07-27 23:30:47  SCAN      ok      tiers 1+2 complete, 2026-07-23, 6 signal(s)  (46.1s)
2026-07-27 23:31:02  CONTEXT   start   RL  run=a1b2c3d4
2026-07-27 23:31:05  HANDOFF   hit     RL 2026-07-23  pullback_strategy/full  quality PASS
2026-07-27 23:31:10  YAHOO     ok      9/9 groups for RL  (4.6s)
2026-07-27 23:31:11  QUANT     ok      score 80.7
2026-07-27 23:31:13  SEC       ok      10-K 200 2.3 MB  business 24000 / risk_factors n-a / mdna 15925  (0.6s)
2026-07-27 23:31:14  CONTEXT   ok      RL bundle ready  (12.1s)
2026-07-27 23:32:29  BASH      DENIED  python -c "import json..." (permission-rule)
2026-07-27 23:33:15  SEARCH    ok      "Ralph Lauren FY26 guidance..." 8 hits  (4.2s)
2026-07-27 23:34:10  FETCH     ok      reuters.com/...tariffs  200  425.0 KB  (7.8s)
2026-07-27 23:38:52  WRITE     ok      RL_2026-07-23.md  28.2 KB
2026-07-27 23:39:44  VERDICT   ok      RL STRONG 75 -> signals.csv (1 row(s))
2026-07-27 23:39:46  DISCORD   sent    RL  1 card(s), 1 chart(s)
2026-07-27 23:39:50  END       ok      33 turns  507s  $4.06  30 model step(s)  1 DENIAL(S)
```

The **Python half** is written live as the run proceeds, so a run that dies still
leaves everything up to that point:

| tier | phases |
|---|---|
| 1 | `SCAN`, `UNIVERSE`, `DOWNLOAD` (incl. the unsettled-bar `warn`), `SCREEN` (one per screen, `off` when disabled), `CHARTS` |
| 2 | `YAHOO` (fundamentals), `QUALITY` (how many passed) |
| 1+2 hand-off | `HANDOFF`, `ARCHIVE` |
| 3 | `CONTEXT`, `HANDOFF`, `SCAN`, `YAHOO`, `QUANT`, `CHART`, `FACTS`, `SEC`, `RECORD`, `REPORT`, `VERDICT`, `DISCORD` |
| 4 | `LEDGER`, `MARK`, `EXIT` (the double-top scan, plus its `DISCORD`), `ANALYZE` |

The **model half** (`BASH`, `READ`, `WRITE`, `GREP`, `SEARCH`, `FETCH`, `MCP`,
and anything `DENIED`) is not collected at runtime — Claude Code already records
it in the session transcript, and `log-session` renders one short line per tool
call and merges the two halves by timestamp when the run ends. Nothing else from
the transcript is kept; the point is which steps ran, when, and whether they
worked.

Three things this surfaces that were previously silent: a **refused tool** (the
run still exits 0 — CLAUDE.md used to tell you to grep the JSON for
`permission_denials`); an **SEC section that came back `n-a`**, which is what
makes a report thinner without saying so; and the **unsettled-bar drop**, which
is the first thing to check when a night reports a confident zero.

`output/scanner_log.txt` is unchanged as tiers 1+2's transcript — the step log
writes to **stderr** and `run_scanner.bat` already redirects `2>&1`, so it
captures every step alongside the hits tables. Keep that `2>&1`.

**`output/logs/deepdive_runs.csv`** is one row per run — `run_id`, `started`,
`finished`, `mode`, `tickers`, `model`, `session_id`, `exit_code`, `turns`,
`duration_s`, `cost_usd`, `web_searches`, `web_fetches`, `denials`, `errors`,
`reports`, `verdicts`:

```python
runs = pd.read_csv("output/logs/deepdive_runs.csv")
runs.groupby("mode")[["cost_usd", "duration_s"]].sum()   # what tier 3 costs
runs[runs.denials > 0]                                   # runs that were refused something
```

`output/deepdive_log.txt` keeps only the start/finish banners; the raw
`--output-format json` blob now goes to `output/logs/<run_id>_result.json` so
what you read stays readable. `research.logging`: `enabled`, `dir`, `manifest`,
`keep_runs` (oldest run logs pruned beyond that count).

Two wiring details worth knowing if you edit the batch files:

- `--session-id` **must stay** on both. It is what makes the transcript findable
  *before* the run starts, so a run killed mid-flight (result `3221225786`, a PC
  shutdown) can still have its log completed by hand:
  `python research_report.py log-session <run_id> <session_id>`. Drop the flag
  and the model half goes missing with no error.
- `STOCK_ANALYZER_RUN_ID` is minted in `run_scanner.bat`, exported, and
  inherited all the way through `claude` into the `context` subprocess — that is
  how four processes write one log. `run_deepdive.bat`'s `run-id` *inherits* it
  rather than minting, which is what joins tier 3 to the same night. Unset (a
  `context`/`scan`/`run_ondemand.bat` you run yourself) simply mints its own.


## On-demand: one ticker, all three tiers

The nightly run analyses what the screens surface. To ask about a ticker
yourself — whether or not it signalled — name it:

```powershell
python research_report.py scan PGR          # tiers 1 + 2, printed and recorded
run_ondemand.bat PGR                        # + the tier-3 deep dive and Discord card
```

Interactively, `/deep-dive PGR` does the same thing: `assemble_context` looks
the ticker up in tonight's hand-off and, finding nothing, runs
`run_scanners.scan_ticker` for it — the same registry loop, the same screens,
the same fundamentals grading, on a one-ticker universe. Two differences from
the nightly path, both deliberate:

- **A disabled screen still reports** (labelled `[screen disabled nightly]`).
  `enabled` gates the nightly *alert*; `backtest_universe.py` and
  `tune_screen.py` already ignore it for the same reason.
- **Tier 2 is graded even when nothing fires.** No signal is an answer —
  `Setup: none` — not a missing trigger, and the quality check is usually the
  point of asking.

The scan dates itself off its own price data (the last *settled* bar), so a
second look next week is a new row rather than a collision with today's.

## Signal history and the verdict record

`latest_hits.json` is overwritten nightly, so every scan is archived on the way
past (`research.history`):

- `output/history/hits_<scan_date>.json` — the hand-off verbatim, one per scan day
- `output/history/signals.csv` — one row per `(scan_date, screen, ticker)` with
  both tiers' verdicts and every day-stat and fundamentals column, plus
  **`Verdict` and `Conviction`** once tier 3 has judged that ticker
- `output/history/on_demand_scans_results.csv` — one row per
  `(scan_date, ticker)` you asked about yourself: the same fundamentals columns,
  plus the verdict and the figures behind it (`Narrative Adj`, `Quant Score`,
  `Price`, `Upside %`, `P/E Pctile 2y`, `Report`, `Thesis`)

Two tables split by provenance, not by content. An ad-hoc look has no signal row
to annotate, so folding it into `signals.csv` would silently drop it; a
`(scan_date, ticker)` is therefore recorded in **exactly one** of them, and
`post-verdicts` routes on the `source` key `assemble_context` wrote into the
facts file. Recording happens with or without `--send` — the record is the
point, the notification is not.

Both CSVs are rewritten rather than appended, because the fundamentals columns
are *config-driven display labels* — retuning the config changes the schema, and
a blind append would misalign every later row. De-duplicating on the key also
makes re-running a day idempotent instead of double-counting it. The rewrite
**carries the verdict columns forward**: tier 3 writes them hours after tier 1
created the row, so without that, re-scanning a day would erase last night's
judgment with no error to explain the loss.

```python
import pandas as pd
sig = pd.read_csv("output/history/signals.csv", parse_dates=["scan_date"])
sig[sig["Quality"]].groupby("config_key")["ticker"].count()   # passers per screen

# A ticker that fired on two screens has two rows and the verdict is on both --
# de-duplicate before grouping by it, or those names count twice.
verdicts = sig.dropna(subset=["Verdict"]).drop_duplicates(["scan_date", "ticker"])
verdicts.groupby("Verdict")["Conviction"].describe()
```

Multi-year metrics and `Quality Missing` are stored as JSON strings in the CSVs
(`json.loads` them back); the dated snapshots keep them as real nested lists.

## Tier 4: the virtual portfolio

Tiers 1–3 decide what looks interesting. Nothing until now ever checked whether
any of it was right. Tier 4 buys every recorded signal on paper at the next
trading day's open, tracks it, and — on demand — grades every attribute the
pipeline recorded against what the stock actually did.

```powershell
python -m portfolio_sim open       # recorded signals -> positions (no network)
python -m portfolio_sim mark       # re-sync, fill entries, mark every horizon
python -m portfolio_sim exit-scan  # double tops on the book -> exits.csv + Discord
python -m portfolio_sim analyze    # -> output/portfolio/findings.csv
python -m portfolio_sim status     # what is on the book, what can be asked yet
```

`open`, `mark` and `exit-scan` run in the nightly chain (`run_scanner.bat`, and
`mark` again at the end of `run_deepdive.bat`). `analyze` is on demand.
`exit-scan` is the **only** command here that touches Discord — `open`, `mark`
and `analyze` never do and must not start.

### The trade model

Buy `Open[t+1]`, hold a fixed number of trading days, sell at the close —
`backtest_universe.forward_trades` unchanged, so a ledger return and a
universe-backtest return are literally the same measurement. Fixed notional per
signal (`portfolio.notional`), unlimited capital, no stops, no targets, no
costs. That is deliberate: with no cash constraint a return depends only on the
signal, never on which trade happened to get funded first, which is what makes
the attribution below readable.

Every position is marked at each `portfolio.horizons` entry (10/30/60 trading
days by default) plus a live mark to the last settled close, and against the
benchmark over the same bars (`excess_<h>d_%`).

**`open` and `mark` are separate because the entry price does not exist yet.**
The nightly run fires after the US close, so `Open[t+1]` is most of a day away.
`open` records the position as `pending` with no price; `mark` fills it on a
later run. A pending position is on the book and in no statistic.

### What each position carries

`output/portfolio/positions.csv`, one row per `(scan_date, ticker, config_key)`
— the same grain as `signals.csv`, so a ticker that fired on two screens is two
positions. Each row carries the **entire** source row (all of tier 1's
technicals, tier 2's badge, the config-labelled fundamentals, tier 3's verdict)
plus:

| column | meaning |
|---|---|
| `status` | `pending` → `open` → `closed` (every horizon has an exit bar) |
| `entry_date` / `entry_price` / `shares` / `notional` | the fill |
| `ret_<h>d_%`, `exit_price_<h>d`, `mfe_<h>d_%`, `mae_<h>d_%` | per horizon |
| `bench_ret_<h>d_%`, `excess_<h>d_%` | the benchmark over the same bars |
| `open_ret_%`, `last_close`, `days_held` | the live mark |
| `qr_<rule>` | per quality rule: `True` = passed that night |
| `Quality Rules` | the rule set in force when the signal was graded |
| `quant_score`, `quant_<dimension>`, `qm_<metric>` | tier 3's breakdown |
| `dt_*` | the double-top exit's verdict — see below |

`qr_*` is exploded from the recorded `Quality Missing` list against the rule set
**frozen with the position**, so retuning `quality.parameters` later
cannot rewrite past findings, and a rule invented after a signal was recorded
never reads as "passed" on it. A row the scan never graded gets no `qr_*`
columns at all — *not evaluated* is not *failed*.

### The exit strategy: double top

Everything above is entry-side, and the only exit is the clock. `exit-scan`
adds a signal-driven one: it watches the names actually on the book and records
a virtual sell when one completes a **double top** and breaks its neckline.

Two equal-height peaks, a real valley between them, and a close below that
valley:

```
    P1      P2
     /\      /\
    /  \    /  \
   /    \  /    \
  /      \/      \
        trough    \
  - - - - - - - - -\- - -   neckline
                    X   <-  signal: close < trough
```

Computed vectorized over the whole panel, like every screen, as two disjoint
trailing windows at bar `t`:

| term | window |
|---|---|
| peak 2 | highest high of `[t-recent, t-1]` |
| peak 1 | highest high of `[t-recent-prior, t-recent-1]` |
| trough (neckline) | lowest low of `[t-recent, t-1]` |

and four conditions: the peaks within `max_peak_diff_pct` of each other; the
trough at least `min_trough_depth_pct` below the lower peak; the close below
the trough by `break_confirm_pct`; and — under `alert_only_on_break` — only the
*first* such close, so a name that stays under its neckline is announced once
rather than every night. An optional `min_volume_ratio` adds a volume
confirmation; `null` means the leg is off.

This is a **rolling-extrema** double top, not a swing-pivot one. That is a
deliberate approximation: it keeps the rule vectorized over the entire panel
instead of walking each ticker's bars looking for pivots.

**It flags the position; it does not close it.** `status` and every
`ret_<h>d_%` keep running exactly as before, so the fixed horizon and the
double-top exit become two measurements of the *same* position and a later
analysis can ask which one you should have taken. A version that closed the
position would answer that question by deleting the evidence.

| column on the position | meaning |
|---|---|
| `dt_signal_date` | the bar the neckline broke |
| `dt_exit_date` / `dt_exit_price` | the sale — `Open[t+1]`, the repo's one convention |
| `dt_status` | `pending` (the exit bar has not traded) / `filled` |
| `dt_ret_%` | the sale against the entry price |
| `dt_peak1`, `dt_peak2`, `dt_neckline` | the pattern's own numbers |

The sell record itself is a separate, deliberately narrow table —
`output/portfolio/exits.csv`, one row per position ever exited:

```
position_id, ticker, name, entry_date, entry_price, exit_date, exit_price, ret_%
```

Narrow on purpose: it exists to grade the *exit* rule, and everything else
about the position joins back on `position_id`. A `pending` exit writes no row
— a sell with no exit price is not a sell — but the position is still flagged
and the alert still fires, and the row lands on the next run.

Detection scans every bar **since entry**, not just the last one. So the step
is idempotent (a second run in the same night records nothing new and re-alerts
nothing) and self-healing: a night the scan did not run is picked up by the
next one instead of being lost.

A red Discord card goes out for each newly flagged position, with a chart under
`output/portfolio/exit_charts/`. `--no-send` records the exit without posting.

> This is **not** a screen, and must not be registered in
> `run_scanners.SCANNERS` or `backtest.screens`. Both consumers read a signal as
> a *buy*: `backtest_universe.forward_trades` would happily score "buy the
> neckline break, sell 30 days later", the exact inverse of what it means.

### The findings file

`analyze` writes one tidy CSV, one row per finding, in sections:

| section | answers |
|---|---|
| `CAVEAT` | the caveats, first in the file so they cannot be missed |
| `portfolio` | what the whole book returned, per horizon, versus the benchmark |
| `roadmap` | every question this file will answer, and what each is still short of |
| `cohort` | grouped stats by screen, setup tier, quality badge, verdict, source |
| `split` | **which signals were better** — full vs partial, quality pass vs fail, deep-dived vs not, verdict tiers |
| `rule_impact` | **which quality rule mattered** — per rule, the tickers that passed it against those that failed |
| `dimension_impact` | **which deep-dive check was worth anything** — per quant dimension, rank correlation plus a top-vs-bottom-tercile split |
| `metric_corr` | every recorded fundamental and technical against the return |
| `baseline` | the random-entry bar from `backtest_universe`'s cached panel |
| `ranking` | the direct answer: everything ranked, most significant first |

Each row carries `n`, the descriptive statistics, an `effect` with its units, a
bootstrap CI, `p_value`, `q_value`, `sufficient_n`, `significant`, and a
`conclusion` sentence. Every conclusion is generated in Python — same rule as
tier 3's chart: a measurement narrated by a model is a measurement you cannot
check.

### How it avoids lying to you

The honest problem with this analysis is that it runs dozens of tests against a
sample that starts at zero and grows by a handful of rows a night. Four things
hold the line:

- **Significance is keyed off a Benjamini–Hochberg `q_value`** across the whole
  file, not a raw p. Ranked by p, the largest effect on a thin sample is
  reliably the luckiest one.
- **`sufficient_n`** (`portfolio.analysis.min_n`, default 20 per side) gates
  every claim. A thinner finding is still written — omitting it would hide the
  question — but it is marked and says so in its own conclusion.
- **Ticker-level attributes are de-duplicated on `(scan_date, ticker)`** before
  grouping, or a name that fired on two screens votes twice in exactly the
  cohorts a deep dive was most likely to touch.
- **The `roadmap` section** lists every question with its current group sizes
  and shortfall, so an empty report reads as "not yet" rather than as "nothing
  there". Until the ledger fills, that section *is* the report.

Statistics are hand-rolled in `portfolio_sim/stats.py` (Mann-Whitney with tie
and continuity corrections, Welch's t, Spearman, a bootstrap CI, BH-FDR) rather
than adding scipy for five functions; each is pinned against a hand-computed
value in `tests/test_portfolio_sim.py`. The p-values are normal approximations,
which is acceptable only because of the two gates above.

Caveats that stay in the output, not just here: paper fills with no costs,
slippage or dividends; overlapping and clustered trades that are not
independent samples; survivorship (today's index membership).

## Tuning a screen (`tune_screen.py`)

The backtest tells you how a screen performs *as configured*. This answers the
next question — **which threshold should I change, and what does it cost me?**

```
python tune_screen.py sensitivity reclaim_strategy    # one knob at a time
python tune_screen.py grid breakout_strategy --csv    # 2-3 knobs crossed
python tune_screen.py delay reclaim_strategy          # wait x hold per candidate
```

It re-runs the screen over the **cached** panel (never downloads — run
`backtest_universe.py` once first) through the production `compute_*` /
`fires_mask` / `partial_mask`, and scores every candidate with
`backtest_universe`'s own statistics. Each row reports both tiers (`full` and
the alerted `fires` cohort), the excess over the random-entry baseline, and:

**Protected cases.** `tuning.protected_cases` lists setups that must keep
firing — e.g. META's 2023-02-02 reclaim. Every candidate shows `FULL`,
`partial` or `MISSED` for each, because *a config that scores well by dropping
the setups you wanted is not an improvement*. This is the column that decides
acceptability; excess only ranks the survivors.

Modes: **sensitivity** first (it shows which knobs matter at all), then
**grid** for interactions one-at-a-time can't see — such as two thresholds that
each independently block the same protected case. **delay** asks whether a
config's edge depends on *not* buying the signal day; it dedupes candidates
whose cohorts come out identical (a confirmation threshold often just moves the
full/partial boundary and leaves the alerted set untouched).

Every range, protected case and grid axis lives in `config.json` → `tuning`, so
tuning is a config edit rather than a code edit. Results inherit the backtest's
caveats — survivorship bias, no costs, no dividends, clustered trades — so treat
small differences as noise.

## Configuration (`config.json`)

| Key | Current | Meaning |
|---|---|---|
| `discord.webhook_url` | (set) | Discord webhook URL (Server Settings → Integrations → Webhooks → New Webhook → Copy URL). Until set, the alert prints to the console instead. |
| `discord.send_message_when_no_breakouts` | `true` | Also send a "nothing found" message |
| `data.download_period` | `2y` | History to download — must exceed each screen's total lookback (~21 trading days per calendar month) or its rolling windows never fill and no signal can ever fire; the scanners warn if violated |
| `breakout_strategy.enabled` | `true` | Run the breakout screen |
| `breakout_strategy.consolidation_window_days` | `312` | Length of the prior consolidation window (trading days) |
| `breakout_strategy.max_consolidation_range_pct` | `0.32` | Max high-to-low range of that window |
| `breakout_strategy.breakout_multiplier` | `1.01` | Close must exceed this × the range high (1.01 = +1%) |
| `breakout_strategy.min_candle_body_pct` | `0.01` | Breakout day's Close must exceed its Open by at least this fraction (green candle with a body ≥ 1%); `0.0` = any green candle |
| `breakout_strategy.volume_sma_days` | `30` | Lookback for the average-volume baseline |
| `breakout_strategy.volume_surge_multiplier` | `1.1` | Required volume vs. that baseline |
| `breakout_strategy.near_miss_max_gap_pct` | `0.05` | A partial setup whose *breakout* leg failed only counts if the close is within this fraction of the required level |
| `pullback_strategy.enabled` | `true` | Run the SMA-pullback screen |
| `pullback_strategy.sma_days` | `150` | SMA length (trading days) |
| `pullback_strategy.touch_band_pct` | `0.02` | "Touch" = close within this fraction of the SMA |
| `pullback_strategy.trend_lookback_days` | `252` | Uptrend persistence lookback (~1 year) |
| `pullback_strategy.sma_slope_lookback_days` | `63` | SMA must be higher than this many days ago |
| `pullback_strategy.min_days_above_sma_pct` | `0.9` | Min fraction of the lookback the close spent above the SMA |
| `pullback_strategy.max_candle_body_pct` | `0.01` | Touch day's body `\|Close−Open\|` must be ≤ this fraction of the Open (small body) |
| `pullback_strategy.min_candle_range_pct` | `0.03` | Touch day's range `High−Low` must be ≥ this fraction of the Open (long tails) |
| `pullback_strategy.require_reversal_candle` | `true` | Require the small-body/long-tailed touch candle; `false` disables it |
| `pullback_strategy.alert_only_on_band_entry` | `true` | Alert only on the day the close enters the band from above |
| `reclaim_strategy.enabled` | `false` | Run the SMA-reclaim screen **in the nightly alert**; off since 2026-07-26 (underperforms — see Screen 3). The backtest and tuner ignore this flag |
| `reclaim_strategy.sma_days` | `180` | SMA length (trading days) |
| `reclaim_strategy.below_lookback_days` | `200` | Downtrend-persistence lookback |
| `reclaim_strategy.min_days_below_pct` | `0.8` | Min fraction of the lookback the close spent below the SMA |
| `reclaim_strategy.cross_margin_pct` | `0.01` | Close must exceed the SMA by this fraction (1.01 × SMA) |
| `reclaim_strategy.min_candle_body_pct` | `0.0` | Body route to R5: Close must exceed its Open by this fraction; `0.0` = any green candle |
| `reclaim_strategy.min_day_gain_pct` | `null` | Gap-inclusive route to R5: a day closing this far above the **previous** close qualifies even with a small body (must still close green). `null` = off, in which case only the body route applies — and at `min_candle_body_pct: 0.0` the body route already admits every green day, so this knob only bites once you raise the body floor |
| `reclaim_strategy.volume_sma_days` | `30` | Lookback for the average-volume baseline |
| `reclaim_strategy.volume_surge_multiplier` | `1.2` | Required volume vs. that baseline |
| `reclaim_strategy.sma_slope_lookback_days` | `63` | Lookback for the optional SMA-slope floor |
| `reclaim_strategy.min_sma_slope_pct` | `null` | Optional slope floor (e.g. `-0.02`); `null` = off |
| `reclaim_strategy.alert_only_on_cross` | `true` | Alert only on the day the close first crosses the level |
| `charts.enabled` | `true` | Attach a chart image per signal to the Discord alert |
| `charts.partial_charts` | `true` | Also chart `partial` setups (set `false` to chart full setups only) |
| `charts.lookback_days` | `250` | Trading days shown in alert charts |
| `charts.dpi` | `120` | Alert-chart resolution |
| `backtest.years` | `3` | Length of the analysis window; the download adds the warm-up the longest screen lookback needs |
| `backtest.holding_days` | `[10, 30, 60]` | Holding periods (y) in **trading** days, held after the entry day |
| `backtest.entry_delay_days` | `[0, …, 5]` | Extra **trading** days (x) to wait before buying, beyond the earliest tradeable bar; `0` = buy as soon as possible. Swept against `holding_days` as a grid |
| `backtest.entry` | `next_open` | `next_open` (buy the open after the signal) or `signal_close` (buy the trigger close) |
| `backtest.detail` | `{0, 30}` | The one (wait, hold) cell that gets per-trade rows, the detailed table, the year breakdown and the bar chart |
| `backtest.split_by_tier` | `false` | Measure `full` and `partial` setups as separate cohorts instead of one combined signal cohort |
| `backtest.measure_excursions` | `true` | Compute MFE/MAE (best/worst excursion while the trade was open) |
| `backtest.benchmark_ticker` | `SPY` | Buy-and-hold benchmark, downloaded alongside the universe |
| `backtest.cache_path` | `backtest_universe_cache.pkl` | Cached price panel, resolved inside `output/`; `--refresh` re-downloads |
| `backtest.cache_max_age_days` | `1` | Reuse the cache only while it is younger than this |
| `backtest.screens` | all three | Config keys of the screens to include |
| `backtest.output.*` | — | Trades CSV, summary CSV, bar-chart path, `grid_chart_path` for the wait × hold heatmap, chart DPI |
| `tuning.years` | `5` | Analysis window `tune_screen.py` scores over |
| `tuning.holding_days` | `[30, 60]` | Holding periods; the first is the default scored in the tables, all are used by `delay` mode |
| `tuning.protected_cases` | 3 cases | `{screen, ticker, date, note}` setups that must keep firing; every candidate is graded `FULL`/`partial`/`MISSED` against them |
| `tuning.sweeps.<screen>` | per-screen | Parameter → list of values to try. Add a value here rather than editing the tool |
| `tuning.grid.<screen>` | 3 names | Which parameters `grid` mode crosses (keep it to 2–3 — it is a full cross product) |
| `quality.enabled` | `true` | Run the quality check at all. Off = no badge, no score, and **no columns** — "not evaluated", never "failed" |
| `quality.badge` | `⭐` | Prefix added to a passing hit's card title |
| `quality.tiers` | 3 bands | `{label, min, color}` conviction bands (STRONG ≥ 80, WATCH ≥ 45, PASS ≥ 0) |
| `quality.groups.<name>` | 7 groups | `{enabled, weight}` — the weighted buckets the score is built from. Weights renormalize over the **enabled** ones |
| `quality.parameters.<key>.enabled` | mostly `true` | **The switch.** `false` = not gated, not scored, not in `Quality Missing`, not displayed, not counted in any weight |
| `quality.parameters.<key>.label` | e.g. `P/E` | Display label — the DataFrame column, CSV header and embed-field name. Safe to rename; the **key** is not |
| `quality.parameters.<key>.source` | e.g. `yahoo_info.trailingPE` | `yahoo_info` / `yahoo_stmt` / `yahoo_deep` / `ibkr`, then the field within it |
| `quality.parameters.<key>.group` | e.g. `valuation` | Which `quality.groups` entry it scores into |
| `quality.parameters.<key>.stage` | `fast` / `deep` | `fast` = collected for every tier-1 hit; `deep` = only for gated tier-3 candidates |
| `quality.parameters.<key>.weight` | `1.0` | Its weight **within** its group |
| `quality.parameters.<key>.gate` | e.g. `{"max": 37}` | `min`/`max` (strict) and `increasing`. Omit for score-only. A missing value **fails** its gate |
| `quality.parameters.<key>.score` | e.g. `{"good": 12, "bad": 45}` | 0-1 anchors; the ordering carries the direction. Omit for gate-only (e.g. `fcf`, whose scale is company-specific) |
| `quality.parameters.<key>.display` | `true` on fast | Render it as an inline embed field on the signal card |
| `quality.parameters.<key>.format` | `number` | `number` / `pct` / `pct_series` / `money_series` |
| `quality.parameters.<key>.percent` | on 2 | Value arrives as a fraction and is ×100 (note `dividendYield` is *not* one — Yahoo already returns a %) |
| `quality.parameters.<key>.zero_is_missing` | on `gross_margin` | Treat a hard `0.0` as missing — Yahoo reports it for banks rather than omitting it, and it would otherwise score them at the bottom of a metric that does not apply |
| `ibkr.enabled` | `false` | Use IBKR over the TWS API. Needs `pip install ib_async` **and** IB Gateway/TWS logged in; everything degrades to `n/a` when it is not |
| `ibkr.host` / `port` / `client_id` | `127.0.0.1` / `7496` / `17` | Gateway socket. 7496 = TWS live, 7497 = TWS paper, 4001/4002 = IB Gateway |
| `ibkr.connect_timeout_seconds` / `request_timeout_seconds` | `8` / `20` | How long to wait before giving up and logging `IBKR skip` |
| `ibkr.market_data` | `true` | Also request the snapshot ticks (52w range, volatility, average volume) alongside the ratios |
| `ibkr.reports` | `["ReportSnapshot"]` | Which `reqFundamentalData` reports to pull and flatten |
| `research.latest_hits_path` | `latest_hits.json` | The tiers 1+2 → tier 3 hand-off, resolved inside `output/` |
| `research.report_subdir` | `reports` | Where deep-dive reports are written, resolved inside `output/` |
| `research.auto.enabled` | `true` | Compute the **deterministic** tier-3 verdict inside the nightly scan |
| `research.auto.gate` | `all` | `quality_pass` (only tier-2 passers) or `all` (every tier-1 hit) |
| `research.auto.max_reports` | `3` | Cap on how many names tier 3 grades, taken off the top of the ranking |
| `research.auto.discord_send` | `true` | Include the verdict cards in the alert. **`false` still records them** — the record is the point, the notification is not. Not writable through the MCP tools |
| `research.narrative.enabled` | `true` | Also run the **optional** LLM pass (`run_deepdive.bat`) for the written report and a bounded conviction revision. Independent of `research.auto.enabled` |
| `research.narrative.model` | `opus` | Model that headless pass uses |
| `research.history.enabled` | `true` | Archive every scan under `output/history/` |
| `research.history.dir` / `.csv` | `history` / `signals.csv` | Archive location and the signal table's name |
| `research.history.on_demand_csv` | `on_demand_scans_results.csv` | The on-demand scan table, in the same directory |
| `research.logging.enabled` | `true` | Write the tier-3 step log; `false` makes every log call a no-op |
| `research.logging.dir` | `logs` | Where run logs live, resolved inside `output/` |
| `research.logging.manifest` | `deepdive_runs.csv` | One row per deep-dive run, in the same directory |
| `research.logging.keep_runs` | `200` | Oldest run logs (and their result JSON) pruned beyond this count |
| `research.financials.years` / `.quarters` | `4` / `4` | Periods on the financial-trend chart and table |
| `research.financials.chart_dpi` | `120` | Resolution of that chart |
| `research.sec.*` | — | EDGAR user agent (must carry an email), forms, section size cap, XBRL concepts |
| `research.synthesis.narrative_adj_max` | `15` | How far the optional narrative pass may move the conviction, either way |
| `portfolio.enabled` | `true` | Run tier 4 at all; `false` makes every subcommand a no-op |
| `portfolio.dir` | `portfolio` | Ledger + findings directory, resolved inside `output/` |
| `portfolio.positions_csv` / `.findings_csv` / `.exits_csv` | `positions.csv` / `findings.csv` / `exits.csv` | The three tables, in that directory |
| `portfolio.entry` | `next_open` | Fill convention, same values as `backtest.entry` |
| `portfolio.horizons` | `[10, 30, 60]` | Holding periods in **trading** days that each position is marked at |
| `portfolio.notional` | `10000` | Fixed cash per signal. No cash constraint, so a return never depends on which trade got funded first |
| `portfolio.benchmark_ticker` | `SPY` | Marked over the same bars as each position, giving `excess_<h>d_%` |
| `portfolio.measure_excursions` | `true` | Record MFE/MAE per horizon |
| `portfolio.analysis.min_n` | `20` | Per-side sample a cohort needs before a finding may be called significant |
| `portfolio.analysis.alpha` | `0.05` | Significance threshold, applied to the FDR-adjusted `q_value` |
| `portfolio.analysis.bootstrap_iters` | `2000` | Resamples behind each difference-in-means CI |
| `portfolio.analysis.fdr` | `true` | Key `significant` off `q_value`; `false` falls back to the raw p (don't) |
| `portfolio.analysis.keep_dated_findings` | `true` | Also write `findings_<date>.csv` per run |
| `exit_strategy.enabled` | `true` | Run `exit-scan` at all; `false` makes it a clean no-op |
| `exit_strategy.recent_window_days` | `20` | Bars holding the second peak **and** the trough |
| `exit_strategy.prior_window_days` | `90` | Bars before those, holding the first peak |
| `exit_strategy.max_peak_diff_pct` | `0.03` | How near-equal the two peaks must be, or it is a trend that pulled back |
| `exit_strategy.min_trough_depth_pct` | `0.05` | How far the valley must sit below the lower peak, or sideways drift fires |
| `exit_strategy.break_confirm_pct` | `0.005` | How far below the neckline the close must be |
| `exit_strategy.volume_sma_days` | `30` | Baseline for the optional volume confirmation |
| `exit_strategy.min_volume_ratio` | `null` | Volume confirmation on the break; `null` = leg off |
| `exit_strategy.alert_only_on_break` | `true` | Only the first close under the neckline, so a name below it is announced once |
| `exit_strategy.discord_alert` | `true` | Post the exit card; `--no-send` overrides per run |

## Nightly schedule (Windows Task Scheduler)

The scan runs Mon–Fri at **23:30 Israel time** (~30 min after the 16:00 ET US
market close) via the task **"SP500 Breakout Scanner"**, which executes
`run_scanner.bat` and appends all output to `output/scanner_log.txt`.

`run_scanners.py` now does all four tiers itself — the screens, the quality
check, the ledger (`open` → `mark` → exit scan) and the graded verdict — and
issues **one** Discord message carrying all of them. The batch file then chains
to `run_deepdive.bat` for the optional narrative pass
(→ `output/deepdive_log.txt`, ending with a tier-4 `mark` so any conviction
revision lands on tonight's position).

Ordering inside the run, all of it load-bearing: the archive is written before
the ledger opens positions from it; `exit-scan` comes after `mark` because it
only looks at positions whose entry price has been filled; and the verdict pass
comes after the exit scan, so a final `sync` carries the tier and conviction
onto tonight's positions rather than tomorrow's.

The exit scan needs no separate schedule: detection searches every bar since
entry, so a night the task did not run is picked up by the next one rather than
lost.

> **`ExecutionTimeLimit` must cover the whole chain.** The screens alone finish
> in about a minute, but the verdict pass adds ~9 Yahoo round-trips per
> candidate and the optional narrative pass takes considerably longer still;
> Task Scheduler kills the chain at the limit. The original task was registered
> with `PT30M`; raise it:
>
> ```powershell
> Set-ScheduledTask -TaskName "SP500 Breakout Scanner" -Settings (
>   New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
>     -ExecutionTimeLimit (New-TimeSpan -Hours 3))
> ```

To (re)create the task from scratch, run in PowerShell:

```powershell
$action   = New-ScheduledTaskAction -Execute "C:\Users\Lenovo\CC\stock_analyzer\run_scanner.bat" -WorkingDirectory "C:\Users\Lenovo\CC\stock_analyzer"
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 23:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName "SP500 Breakout Scanner" -Action $action -Trigger $trigger -Settings $settings -Description "Scans S&P 500 for breakouts from consolidation ~30 min after US market close, alerts via Discord webhook, then runs the tier-3 deep-dive on the gated candidates."
```

A second, **weekly** task grades the whole index on the risk/reward plane. It is
separate on purpose: fundamentals move quarterly, the pass takes ~12 minutes, and
it posts nothing to Discord — the nightly card already carries each signal's own
coordinates, which is the part that is actionable on the day.

```powershell
$action   = New-ScheduledTaskAction -Execute "C:\Users\Lenovo\CC\stock_analyzer\run_universe.bat" -WorkingDirectory "C:\Users\Lenovo\CC\stock_analyzer"
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 18:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "SP500 Universe Plane" -Action $action -Trigger $trigger -Settings $settings -Description "Grades every S&P 500 constituent on the risk/reward plane and writes the table, scatter and interactive page under output/universe/. No Discord."
```

Useful commands:

```powershell
Get-ScheduledTaskInfo -TaskName "SP500 Breakout Scanner"   # last/next run + result code
Start-ScheduledTask   -TaskName "SP500 Breakout Scanner"   # trigger a run right now
Get-Content output\scanner_log.txt  -Tail 40               # all four tiers
Get-Content output\deepdive_log.txt -Tail 40               # the narrative pass
Get-ScheduledTaskInfo -TaskName "SP500 Universe Plane"     # the weekly plane pass
Get-Content output\universe_log.txt -Tail 20               # its progress lines
```

Notes:

- `StartWhenAvailable` runs the scan at next boot if the PC was off at 23:30,
  and `WakeToRun` wakes it from sleep — but a run that *started* and was then
  interrupted (e.g. shutting the PC down at ~23:31) is **not** retried, and
  that night's alert is lost. Avoid shutting down between ~23:25 and ~23:35.
  With tier 3 chained on, the window is longer; the signals themselves are
  archived before the deep-dive starts, so only the reports are lost.
- A result code of `0` in `Get-ScheduledTaskInfo` means success;
  `3221225786` (0xC000013A) means the run was terminated mid-scan.
- Tier 3 needs Claude Code authenticated for this Windows user. If a nightly
  report is missing its **Business & moat** section, the IBKR MCP login has
  expired — run `/mcp` in an interactive session to renew it.

## Notes

- Both screens are fully vectorized: one bulk `yf.download` for all ~503
  tickers, then rolling-window math across the whole universe at once. A full
  scan takes about a minute.
- Discord allows at most 10 embeds / 10 attachments / ~6000 embed characters
  per webhook message; the alert is split into multiple messages
  automatically if more tickers fire.
- Occasional per-ticker download failures (delistings, transient Yahoo errors)
  are tolerated — those tickers simply drop out of the scan.
- A trailing bar with **no settled close** is dropped and the previous session
  scanned instead, with a `WARNING:` line naming the dropped date. Yahoo returns
  an unsettled session as a normal row with Open/High/Low/Volume but a null
  `Close`, and can revert a settled bar to that state hours later; since every
  condition compares against the close, scanning it would report zero signals
  with no sign of trouble. So **an unexplained zero-signal run is worth checking
  in `output/scanner_log.txt`** — either the warning is there (bad bar, screens
  fine) or the day genuinely had no setups.
- Yahoo legitimately lacks some fundamentals for some companies (e.g. no P/E
  when trailing earnings are negative, no Debt/Equity when equity is negative);
  those show as `n/a` in the alert.
- Not investment advice; screens produce false positives. Do your own
  research before trading.
