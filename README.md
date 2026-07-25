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

Current screens:

1. **Breakout from consolidation** — upward breakout from a horizontal range.
2. **SMA pullback** — a stock in a year-long uptrend pulling back to its
   rising 150-day SMA.
3. **SMA reclaim** — a stock that spent most of the last year *below* its
   200-day SMA crossing back above it on volume (trend-reversal /
   Weinstein "Stage 2" entry).

## Code layout

| File | Role |
|---|---|
| `run_scanners.py` | Entry point: one bulk download → every enabled screen → one Discord alert |
| `breakout_scanner.py` | Breakout screen module (condition math, alert section, hit chart) |
| `sma_pullback.py` | SMA-pullback screen module (same shape) |
| `sma_reclaim.py` | SMA-reclaim screen module (same shape) |
| `scanner_common.py` | Shared infra: config, tickers, downloads, fundamentals, Discord |
| `charts.py` | Shared chart rendering (palette + per-screen chart builders) |
| `backtest_breakout.py` / `backtest_pullback.py` / `backtest_reclaim.py` | Single-ticker historical validators |
| `backtest_universe.py` | Universe-wide profit backtest: every screen × all history, buy the trigger / sell N days later |
| `tune_screen.py` | Parameter tuning: sweep one screen's thresholds, scored against the baseline **and** against cases that must keep firing |
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

The mirror image of screen 2 — instead of a dip in an uptrend, it catches
the *birth* of a new uptrend: a stock that lived below its 200-day SMA for
most of a year crossing back above it. All conditions must be true on the
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

## Fundamentals

Every signalling ticker gets a fundamentals field grid on its embed
card, from two config-driven layers (`fundamentals` in `config.json`):

- **Snapshot fields** from Yahoo `info` (`fields` map): currently P/E, PEG,
  Total Debt/Equity, latest-quarter YoY revenue growth, dividend yield,
  payout ratio.
- **Statement metrics** computed from the last `statements.years` annual
  reports (income statement / cash flow / balance sheet):
  - FCF per year (cash-flow "Free Cash Flow")
  - Operating margin and profit margin per year
  - ROE = Net Income / Stockholders Equity (falls back to Yahoo's `info`
    value when statement rows are missing)
  - ROIC = EBIT × (1 − tax rate) / Invested Capital

Metrics Yahoo doesn't provide for a company (banks have no operating income,
negative-equity companies no D/E, ...) show as `n/a`. A bank's FCF can be a
large negative number — that is Yahoo's genuine figure (deposit/loan flows
dominate bank cash-flow statements), not a bug.

### Quality badge

A hit whose fundamentals pass **every** rule in `fundamentals.quality.rules`
gets the configured badge (default ⭐) in front of its card title. This is
computed in the shared embed builder, so it applies to the hits of every
screen — current and future — automatically.

Rules are keyed by the same keys as the display config (Yahoo `info` keys
for snapshot fields, metric keys for statement metrics) and support:

- `min` / `max` — strict compare against the value (the **latest fiscal
  year** for multi-year metrics like FCF and margins);
- `increasing: true` — the latest fiscal year must be above the previous
  one (requires ≥ 2 years of data).

A missing value fails its rule — unverifiable quality doesn't earn the
badge (so banks, with no operating income, can never carry it). The default
rule set is deliberately strict (P/E < 35, PEG < 2, D/E < 75, revenue
growth > 10%, a dividend, payout < 50%, ROE > 15%, ROIC > 15%, OpM > 20%
and rising, PM > 15% and rising, positive and rising FCF) — most S&P 500
names fail at least one rule; edit `config.json` to loosen it.

## Usage

```
pip install -r requirements.txt
python run_scanners.py            # full S&P 500 scan + Discord alert
python backtest_breakout.py       # historical validation, breakout screen
python backtest_pullback.py       # historical validation, pullback screen
python backtest_reclaim.py        # historical validation, reclaim screen
python backtest_universe.py       # universe-wide profit backtest (all screens)
python tune_screen.py sensitivity reclaim_strategy   # which thresholds matter?
python tests/run_all.py                              # the test suite
```

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
them; an absolute path in config still overrides. Deep-dive reports are the one
exception: they go to the Google Drive folder instead.

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
| `test_forward_trades.py` | nothing | Trade arithmetic on a synthetic panel: entry/exit offsets for both conventions and any delay, the excursion window, tail NaNs, `delay=0` identity, input rejection |
| `test_signal_contract.py` | cached panel | `fires_mask` is `signal` or exactly `signal \| partial`; tiers disjoint; `Setup`/`Missing` agree; one card per signal with the grey bar + **Missing** line on partials; hand-off is a single list; `find_ticker` resolves either tier; an empty day doesn't crash |
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
| `reclaim_strategy.enabled` | `true` | Run the SMA-reclaim screen |
| `reclaim_strategy.sma_days` | `200` | SMA length (trading days) |
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
| `fundamentals.enabled` | `true` | Fetch fundamentals for every signalling ticker |
| `fundamentals.fields` | 6 fields | Yahoo `info` key → display label; add/remove entries to change what the alert shows |
| `fundamentals.percent_fields` | `revenueGrowth`, `payoutRatio` | Fields Yahoo returns as fractions, converted to % (note: `dividendYield` is *not* here — Yahoo already returns it as a %) |
| `fundamentals.statements.enabled` | `true` | Compute per-year metrics from the annual statements |
| `fundamentals.statements.years` | `2` | How many recent fiscal years to show |
| `fundamentals.statements.metrics` | 5 metrics | Metric → display label; remove an entry to drop it from the alert |
| `fundamentals.quality.enabled` | `true` | Evaluate the quality rules and badge passing hits |
| `fundamentals.quality.badge` | `⭐` | Prefix added to a passing hit's card title |
| `fundamentals.quality.rules` | 11 rules | Per-metric `min`/`max`/`increasing` thresholds; a hit must pass **all** of them to get the badge |

## Nightly schedule (Windows Task Scheduler)

The scan runs Mon–Fri at **23:30 Israel time** (~30 min after the 16:00 ET US
market close) via the task **"SP500 Breakout Scanner"**, which executes
`run_scanner.bat` and appends all output to `output/scanner_log.txt`.

To (re)create the task, run in PowerShell:

```powershell
$action   = New-ScheduledTaskAction -Execute "C:\Users\Lenovo\CC\stock_analyzer\run_scanner.bat" -WorkingDirectory "C:\Users\Lenovo\CC\stock_analyzer"
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 23:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "SP500 Breakout Scanner" -Action $action -Trigger $trigger -Settings $settings -Description "Scans S&P 500 for breakouts from consolidation ~30 min after US market close and alerts via Discord webhook."
```

Useful commands:

```powershell
Get-ScheduledTaskInfo -TaskName "SP500 Breakout Scanner"   # last/next run + result code
Start-ScheduledTask   -TaskName "SP500 Breakout Scanner"   # trigger a run right now
Get-Content output\scanner_log.txt -Tail 20                # inspect the last run's output
```

Notes:

- `StartWhenAvailable` runs the scan at next boot if the PC was off at 23:30,
  and `WakeToRun` wakes it from sleep — but a run that *started* and was then
  interrupted (e.g. shutting the PC down at ~23:31) is **not** retried, and
  that night's alert is lost. Avoid shutting down between ~23:25 and ~23:35.
- A result code of `0` in `Get-ScheduledTaskInfo` means success;
  `3221225786` (0xC000013A) means the run was terminated mid-scan.

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
