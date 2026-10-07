# S&P 500 / MidCap 400 Scanners

A nightly stock screener over the **S&P 500 and S&P MidCap 400** (903 names)
that records what it concluded and then grades itself:

1. Four technical screens find setups.
2. A registry of fundamental parameters decides whether the company behind each
   setup is any good, and whether it is visibly failing.
3. A deterministic 0-100 verdict scores it.
4. A virtual portfolio buys **every** recorded signal and measures which
   recorded attribute actually predicted the return.

Results arrive as one Discord message a night.

**Every number is computed in Python.** No model writes a figure, score or
thesis sentence; `tests/test_no_model.py` fails if any code path could start
one. AI-assisted research runs separately from a Claude session and is recorded
beside the data, where the portfolio grades it like any other hypothesis (see
[`AI_ROLE.md`](AI_ROLE.md)).

> ### ⚠️ Not investment advice
>
> This is a research and measurement tool, not a recommendation engine. The
> screens produce false positives, the thresholds are tuned on limited
> history, and the "portfolio" has never placed an order. Backtested results do
> not predict future returns. **Do your own research; you trade at your own
> risk.**

## Quickstart

```bash
git clone https://github.com/alexg-hub/stock_analyzer.git
cd stock_analyzer
pip install -r requirements.txt          # Python 3.10+; developed on 3.13

cp .env.example .env                     # optional: Discord webhook, SEC contact
python run_scanners.py --no-send         # a full scan, cards printed not posted
python tests/run_all.py                  # the test suite: offline, ~4 minutes
```

Nothing is required to start. With no `.env`, the scan prints its cards instead
of posting them, and SEC requests use a placeholder contact (with a warning).
Set these in `.env` when you need them:

| Variable | For |
|---|---|
| `STOCK_ANALYZER_DISCORD_WEBHOOK` | Posting the nightly message (Discord: Server Settings → Integrations → Webhooks → New Webhook → Copy URL) |
| `STOCK_ANALYZER_SEC_USER_AGENT` | SEC EDGAR requests, which require a real contact, e.g. `stock-analyzer/1.0 (you@example.com)` |

The first scan downloads two years of daily bars for 903 tickers (about a
minute). Run `python backtest_universe.py` once to cache a five-year price panel.
The backtest, the threshold tuner and three of the tests read that cache.

## How it works

| Tier | Asks | How | Records |
|---|---|---|---|
| **1: technical** | Is the chart set up? | Four screens over the 903 names | `Setup` (`full`/`partial`) and `Missing` |
| **2: quality** | Are the fundamentals sound? Is it visibly failing? | The `fast` half of the quality registry, for the tier-1 hits | ⭐ badge (`Quality`), 🚫 veto (`Veto Reasons`), risk/reward coordinates |
| **3: verdict** | How does the business grade out? | The whole registry, weighted to 0-100 → a tier and a conviction | `Verdict` and `Conviction`, a facts file and a financials chart |
| **4: portfolio** | Was any of it right? | Buys every signal at the next open, marks it at 10/30/60 days, watches for a double-top exit, grades every recorded attribute against the return | `output/portfolio/` |

All four tiers run in `run_scanners.py`, in one process, and post **one**
Discord message: what fired, how it graded, and what to sell. Each section is
fail-safe on its own, so a broken ledger or a failed verdict costs only its own
part of the message.

On-demand tools, run by hand or from a Claude session:

| Tool | What it does |
|---|---|
| `research_report.py scan / verdicts` | Tiers 1-3 for any named ticker, signalled or not |
| `universe_scan.py` | Places every constituent (or the week's signals) on the risk/reward plane |
| `industry_valuation.py` | Finds industries whose multiple left its own history, and whether earnings moved with it |
| `theme-screen` skill | Finds the companies that benefit from a dated real-world event (a data-centre build, a fab commitment) |
| `enrich` skill | Qualitative research on a graded ticker, recorded as categories tier 4 can grade |
| `combined_report.py` | One page per ticker: everything graded and everything researched |

### The nightly message

There is one summary line per screen, then one embed card per ticker. The side
bar's colour shows the screen (grey for a *partial* setup, red for a vetoed
name). The title is the ticker and company name, followed by the signal, a
fundamentals grid, the risk/reward plane line and the chart. Graded verdicts
and exit warnings follow in the same message.

## The screens

Parameter names refer to each screen's section in `config.json`. Every screen
reports **one list with two tiers**: `full` when every condition held, `partial`
when the setup is incomplete, with `Missing` naming the failed test. Full
setups are listed first.

| | breakout | pullback | reclaim | trend |
|---|---|---|---|---|
| `full` | all 4 conditions | all conditions | every confirmation held | all conditions |
| `partial` | exactly 3 of 4 | never (strict) | fresh cross out of a downtrend, 1–2 confirmations failing | never (strict) |

The backtest found partial breakouts outperformed full ones (+3.10% vs +0.67%
over 30 days), so partials are alerted rather than suppressed.

`enabled: false` on a screen silences the nightly alert only. The backtest and
the tuner still measure it.

### 1. Breakout from consolidation (`breakout_strategy`)

1. **Horizontal range:** over the previous `consolidation_window_days`,
   `(max High − min Low) / min Low ≤ max_consolidation_range_pct`.
2. **Breakout:** today's close exceeds `breakout_multiplier ×` that range's high.
3. **Volume surge:** volume ≥ `volume_surge_multiplier ×` the prior
   `volume_sma_days` average.
4. **Strong green candle:** close > `(1 + min_candle_body_pct) ×` open.

When the *breakout* leg is the one that failed, a partial only counts if the
close is within `near_miss_max_gap_pct` of the required level.

### 2. Pullback to a rising SMA (`pullback_strategy`)

1. **Rising SMA:** the `sma_days` SMA is above its value `sma_slope_lookback_days` ago.
2. **Sustained uptrend:** close above the SMA on ≥ `min_days_above_sma_pct` of
   the previous `trend_lookback_days`.
3. **Touch:** close within `touch_band_pct` of the SMA.
4. **Reversal candle** (`require_reversal_candle`): body ≤ `max_candle_body_pct`
   and range ≥ `min_candle_range_pct` of the open.
5. **Fresh entry** (`alert_only_on_band_entry`): fires on the day the close
   enters the band, not every day it stays there.

### 3. Reclaim of a long-term SMA (`reclaim_strategy`)

This screen catches the start of a new uptrend: a stock that spent most of a
year below its `sma_days` SMA crossing back above it.

1. **Cross level:** close > `(1 + cross_margin_pct) ×` SMA.
2. **Long prior downtrend:** close below the SMA on ≥ `min_days_below_pct` of the
   previous `below_lookback_days`.
3. **Volume:** ≥ `volume_surge_multiplier ×` the prior `volume_sma_days` average.
4. **Slope floor** (optional, `min_sma_slope_pct`).
5. **Strong day:** either a green body ≥ `min_candle_body_pct`, **or**, when
   `min_day_gain_pct` is set, a close that far above the previous close while
   still green. The second route catches gap-ups, which a body cannot see.
6. **Fresh cross** (`alert_only_on_cross`).

The fresh cross out of a real downtrend is mandatory. The other confirmations
set the tier: none failing is `full`, one or two failing is `partial`.

### 4. Near-linear uptrend (`trend_strategy`)

This screen fits a least-squares line to the trailing `trend_window_days` (on log
price by default) and signals the **day the trend first qualifies**, not every
day it holds:

1. **Angle:** annualized slope between `min_annual_slope_pct` and
   `max_annual_slope_pct`. The ceiling rejects parabolic blow-offs, which score
   a high r².
2. **Linearity:** r² ≥ `min_r_squared`.
3. **Tight around the line:** residual std ≤ `max_residual_pct` of price.
4. **Not extended** (optional, `max_last_dev_pct`).
5. **Fresh qualification** (`alert_only_on_new_trend`): re-arms if the trend
   breaks and re-forms.

### Theme screen: who benefits from a real-world event

This screen runs on demand, from the `theme-screen` skill.

1. `theme_research` returns a bundle: dated, multi-source news events (Google
   News RSS), recent 8-Ks (EDGAR full-text search) and the configured
   beneficiary chain.
2. A session traces the chain from the event to the companies that sell into it.
3. `theme_record` records the picks.

Rules that hold the boundary:

- **The agent names a ticker and a mechanism; Python grades it.** The record
  rejects any score field.
- **A pick must be tradeable.** Every pick is scanned before it is written, and a
  ticker with no US price data is refused.
- **The batch is atomic.** One invalid pick writes nothing.
- **The evidence is frozen** to `output/themes/events_<theme>_<date>.json` at
  record time.

Picks land in `signals.csv` under `config_key = "theme_screen"`, and tier 4
buys them. `Trigger` names any technical screen that also fired, or `"none"`.
Themes are configured in `theme_screen.themes`: `datacenter`, `energy`,
`ai_semi`, `biopharma` and `defense`. Social media sources are unavailable
(blocked, paywalled or without an API), so this is a news-and-filings screen.

### Industry valuation

Over any window, `dlog(Price) = dlog(P/E) + dlog(EPS)`. `industry_valuation.py`
rebuilds a five-year P/E path per company from announcement-dated quarterly
EPS, aggregates by GICS sub-industry (rolling up to sector when a bucket has
fewer than 6 members), and splits each industry's price move into multiple
change and earnings change:

- **`cheap`**: the multiple is historically low **and** earnings grew. The
  industry was de-rated.
- **`rich`**: the multiple is historically high **and** it got there by
  expanding.

`record <bucket>` writes the industry's members into `signals.csv` so tier 4
grades them. Bucket membership is today's, applied backwards, so the history is
survivorship-biased; every output says so.

## The quality check

There is one registry, `config.json` → `quality.parameters`. It produces the
tier-2 badge, the veto, both plane coordinates and the tier-3 score. A
parameter is one measurable thing about a company:

```jsonc
"trailingPE": {
    "enabled": true,                   // false = invisible: not gated, scored or shown
    "label": "P/E",                    // display name (safe to rename; the key is not)
    "source": "yahoo_info.trailingPE", // which resolver supplies the value
    "group": "valuation",              // which weighted group it scores into
    "stage": "fast",                   // fast = every hit; deep = tier-3 candidates
    "gate":  {"max": 37},              // pass/fail -> the badge   (optional)
    "score": {"good": 12, "bad": 45}   // 0-1 anchors -> the score (optional)
}
```

| `source` prefix | Supplies | Stage |
|---|---|---|
| `yahoo_info` | snapshot fields: P/E, PEG, D/E, revenue growth, yield, payout | fast |
| `yahoo_stmt` | from the annual statements: FCF, margins, ROE, ROIC | fast |
| `distress` | Altman Z, Beneish M, interest coverage, cash runway, accruals, short interest | fast |
| `moat` | ten persistence proxies: ROIC above hurdle, margin stability, revenue consistency, FCF conversion, … | fast |
| `price_risk` | volatility, drawdowns, downside deviation, Ulcer index, beta, skew, distance from the 52-week high | fast |
| `yahoo_deep` | P/E percentile, analyst upside, estimates and revisions, surprises, ROA, gross margin, net debt/EBITDA, buybacks | deep |
| `sec_flags` | going concern, restatement, bankruptcy, delisting, late filing, auditor change | deep |

**The badge (⭐)** marks a hit that passes every enabled gate. A **missing value
fails its gate**: unverifiable quality does not earn the badge. The gates are
deliberately strict, and most names fail at least one.

**The veto (🚫)** is an exclusion rule (`veto: true`). It asks "is this company
visibly failing?" and is meant to fire rarely. A missing value **never**
vetoes, because a data gap is not proof of disaster. The veto is a *label*
(`quality.veto_enforced: false`): a vetoed signal keeps its card (sorted last),
its score and its place in the portfolio. That is how tier 4 can test whether
excluding these names actually helps.

**The score** normalizes each parameter between its `good` and `bad` anchors,
averages within groups and weights across groups. A group with no data scores a
neutral 0.5, so a bank is not penalized for statement rows Yahoo does not
publish. The score becomes tier 3's conviction, and `quality.tiers` maps it to
STRONG / WATCH / PASS.

**The risk/reward plane.** Each group sits on a `reward` or a `risk` axis.
Every company gets two 0-100 coordinates, and `quality.quadrant` (`reward_min`,
`risk_max`) defines *buy*, *speculative*, *dull* and *avoid*. A company whose
axis cannot be measured is `unknown`, never plotted at zero.

**Sector-relative scoring.** A parameter marked `sector_relative: true` is read
against its sector's distribution rather than fixed anchors. Utilities,
financials and others are structurally different, and absolute anchors had
been excluding most of a sector for being that sector. Peer distributions come
from a full `universe_scan.py` pass. A sector-relative veto fires only when the
absolute threshold is breached **and** the value is in the sector's worst 10%.

Edit parameters with `params_list` and `config_set` (below), or by hand. A
change takes effect on the next run, and tier 3 re-grades against the current
values.

## Using it from Claude (MCP server)

`mcp_server.py` exposes the whole pipeline as MCP tools, so you can drive it
from a conversation: scan a ticker, read the plane, run a backtest, record
research, tune a threshold. First check that the server starts:

```powershell
python mcp_server.py --selftest     # lists every tool and calls the read-only ones
```

### Install in Claude Code

The repository already contains the configuration (`.mcp.json`):

1. Open Claude Code in the repository folder (`cd stock_analyzer`, then `claude`).
2. Approve the `stock_analyzer` project server when asked (once).
3. Run `/mcp` to confirm it shows **connected**.

To add it by hand instead:

```powershell
claude mcp add stock_analyzer --scope project -- python mcp_server.py
```

Claude Code also loads the project's skills from `.claude/skills/`: `enrich`,
`theme-screen`, `tune-thresholds` and `universe-backtest`. The allow-list in
`.claude/settings.json` lets read-only tools run without a prompt.

### Install in Claude Desktop

1. Find your Python interpreter (the one you ran `pip install` with):

   ```powershell
   python -c "import sys; print(sys.executable)"
   ```

2. In Claude Desktop, open **Settings → Developer → Edit Config**. This opens
   `claude_desktop_config.json`:
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

3. Add the server, with **absolute paths**, because Desktop does not start in
   the repo folder:

   ```json
   {
     "mcpServers": {
       "stock_analyzer": {
         "command": "C:\\path\\to\\python.exe",
         "args": ["C:\\path\\to\\stock_analyzer\\mcp_server.py"],
         "env": { "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1" }
       }
     }
   }
   ```

4. Quit Claude Desktop completely and reopen it. The tools appear under the
   tools (connectors) menu.

Desktop gets the tools but not the skills, which are a Claude Code feature.
Secrets still come from the repo's `.env`, because the server locates it from
its own path.

**After editing any project file, restart the server.** It is a long-lived
process and keeps the code it started with: in Claude Code run `/mcp` and
reconnect; in Desktop, restart the app. A stale server typically fails later
with an `ImportError`.

### The tools

| Tool | What |
|---|---|
| `scan_status` / `scan_tickers` | the latest scan; tiers 1+2 for named tickers |
| `run_nightly_scan` | the full nightly run **(prompts; `send=False` by default)** |
| `deepdive_candidates` / `deepdive_context` | tier-3 candidates; the full research bundle for one ticker |
| `risk_research` | every risk rule beside its threshold, plus the questions arithmetic cannot answer |
| `universe_scan` / `universe_quadrant` | grade tickers onto the plane; read the plane from cache |
| `valuation_scan` / `valuation_read` / `valuation_record` | industry valuation **(record prompts)** |
| `theme_research` / `theme_read` / `theme_record` | the theme screen **(record prompts)** |
| `enrichment_read` / `enrichment_record` | the `enrich` skill's record |
| `combined_report` | the one-page dossier |
| `portfolio_status` / `portfolio_positions` / `portfolio_open` / `portfolio_mark` / `portfolio_analyze` | tier 4 |
| `portfolio_exit_scan` | double tops on the book **(prompts; `send=False` by default)** |
| `backtest_universe` / `backtest_ticker` / `tune_screen` | backtests and tuning |
| `params_list` / `config_get` | every tunable parameter and its value; config with secrets redacted |
| `config_set` / `config_edit` / `config_delete` | change config **(prompt; preview unless `confirm=True`)** |
| `job_status` / `job_result` / `list_jobs` | long runs return a `job_id` immediately; poll it |

**A tool prompts** if it can post to Discord, rewrite `config.json`, or write
into `signals.csv`. Every other tool is pre-approved, and a test fails if a new
tool is not classified. Reports, logs and CSVs under `output/` are read with
ordinary file reads; there are no file-reading tools.

Editing parameters from a session:

```
params_list("quality")                                          # includes disabled parameters
config_set("quality.parameters.dividendYield.enabled", false)   # previews a diff
config_set("quality.parameters.dividendYield.enabled", false, confirm=true)
config_edit([{"path": "...", "value": ...}, ...])               # a batch, all or nothing
```

Every write is validated first and backed up to `output/config_backups/`, and
changes only the bytes it must. The writers refuse `discord.*` and
`research.auto.discord_send`.

## Command reference

```powershell
# The nightly run
python run_scanners.py                    # all four tiers, one Discord message
python run_scanners.py --no-send          # same, cards printed

# Tier 3
python research_report.py candidates      # who the tier-2 gate selects (--all, --json)
python research_report.py verdicts MSFT   # grade and record named tickers
python research_report.py scan PGR        # tiers 1+2 for any ticker
python research_report.py risk INTC       # every risk rule beside its threshold

# Tier 4
python -m portfolio_sim open              # recorded signals -> positions
python -m portfolio_sim mark              # fill entries, mark every horizon
python -m portfolio_sim exit-scan         # double tops -> exits.csv + Discord (--no-send)
python -m portfolio_sim analyze           # -> output/portfolio/findings.csv
python -m portfolio_sim status

# On demand
python universe_scan.py                   # all 903 on the plane (~17 min; --no-fetch re-renders)
python universe_scan.py --from-signals    # the last 7 days of signals
python universe_scan.py --tickers MSFT KO
python industry_valuation.py scan         # (~20 min cold); show <bucket>; record <bucket> --top 5
python newsfeed.py [theme] [--refresh]    # the theme screen's evidence
python theme_signals.py record picks.json [--deep]
python enrichment.py show TJX
python combined_report.py TJX [GOOG ...]  # or --from-signals 7

# Backtesting and tuning
python backtest_breakout.py               # one ticker, every step logged (--ticker/--start/--end)
python backtest_pullback.py
python backtest_reclaim.py
python backtest_trend.py
python backtest_universe.py               # every screen x all history
python tune_screen.py sensitivity reclaim_strategy

python tests/run_all.py                   # the suite (--network adds the Yahoo test)
```

## Tier 3: the verdict

The tier-2 gate (`research.auto.gate`: `quality_pass` or `all`, capped by
`max_reports`) chooses the candidates. For each one, the scan collects the whole
registry, scores it, and records:

```
conviction = score                 # 0-100
tier       = tier_for(score)       # STRONG / WATCH / PASS (quality.tiers)
```

It also writes `output/reports/<TICKER>_<date>_facts.json` and a financials chart
(revenue, earnings, margin, FCF and D/E over four years and four quarters). The
one-line thesis on the card is generated from the group breakdown. Nothing
revises the verdict afterwards.

Records live in `output/history/`:

- `signals.csv`: one row per signal `(scan_date, screen, ticker)`, with every
  stat, the fundamentals, and the verdict once graded.
- `on_demand_scans_results.csv`: one row per ticker you asked about yourself.
- `hits_<date>.json`: each night's hand-off, verbatim.

A ticker that fired on two screens has two signal rows, so de-duplicate on
`(scan_date, ticker)` before grouping by verdict. List columns are stored as
JSON.

## Tier 4: the virtual portfolio

Every recorded signal is bought on paper at the **next open** with a fixed
notional (`portfolio.notional`), held without stops or costs, and marked at each
`portfolio.horizons` entry (10/30/60 trading days) and against SPY. A position
is `pending` until its entry bar trades.

Each position carries its whole source row, plus point-in-time flags:

- `qr_<rule>`: True when a quality rule **passed**.
- `vt_<key>`: True when a veto **tripped**.
- `quant_*`: tier 3's breakdown.
- `en_*`: the enrichment record.

These are frozen against the rules in force that night, so retuning later never
rewrites history.

**Exit rule: double top.** `exit-scan` flags a held position when two
near-equal peaks are followed by a close below the valley between them (the
neckline). It records the virtual sale at the next open in `exits.csv` and posts
a red card. It **flags rather than closes**: the fixed-horizon returns keep
running, so the two exit methods can be compared on the same positions.

**`analyze`** writes `output/portfolio/findings.csv`. It reports which screens,
tiers, quality rules, vetoes, verdict tiers and enrichment judgments predicted
returns. Significance uses a Benjamini–Hochberg `q_value`, every finding needs
`min_n` per side before it can be called significant, and a `roadmap` section
lists every question with how much data it still needs.

## Backtesting and tuning

**`backtest_universe.py`** runs every screen over the whole universe and
`backtest.years` (4) of history, using the production condition code, and trades each signal on
a grid of **wait** (`entry_delay_days`) × **hold** (`holding_days`). It compares
each cohort with a random-entry baseline and with SPY. Outputs are under
`output/`: a trades CSV, a summary CSV, a bar chart and a wait × hold heatmap.
`--split-by-tier` separates full and partial setups.

Read every number with its caveats:

- survivorship bias (today's index members only);
- no costs or dividends;
- no position limits;
- clustered, overlapping trades, so the trades are not independent samples.

**`tune_screen.py`** sweeps one screen's thresholds over the cached panel
(`sensitivity`, `grid`, `delay`) and scores each candidate against the baseline
**and** against `tuning.protected_cases`, the setups that must keep firing. The
sweep ranges live in `config.json` → `tuning`.

## Configuration

`config.json` holds every threshold and label. Secrets do not belong there; put
them in `.env` (see Quickstart). The best way to browse it is
`params_list` from a session, or the file itself.

| Section | Controls |
|---|---|
| `data` | the index sources (`universe_sources`, with `alert` per index) and `download_period` (must exceed every screen's lookback) |
| `breakout_strategy` / `pullback_strategy` / `reclaim_strategy` / `trend_strategy` | each screen's thresholds and `enabled` |
| `exit_strategy` | the double-top rule |
| `quality` | the parameter registry, groups and weights, tiers, quadrant thresholds, badge and veto settings |
| `research` | tier 3: gate, `max_reports`, `discord_send`, history and log locations, SEC settings |
| `portfolio` | tier 4: horizons, notional, analysis thresholds |
| `universe` / `industry_valuation` / `theme_screen` / `enrichment` / `combined` | the on-demand tools |
| `backtest` / `tuning` | the universe backtest and the tuner |
| `charts` / `discord` | rendering and delivery |

## Where files go

Everything generated goes under **`output/`**, which git ignores:

| Path | Contents |
|---|---|
| `latest_hits.json`, `scanner_log.txt` | tonight's hand-off; the nightly transcript |
| `history/` | `signals.csv`, `on_demand_scans_results.csv`, `hits_<date>.json` |
| `reports/` | tier-3 facts files and charts, and the combined dossiers |
| `portfolio/` | `positions.csv`, `findings.csv`, `exits.csv`, exit charts |
| `universe/`, `valuation/`, `themes/`, `enrichment/` | the on-demand tools' tables, charts and caches |
| `logs/<run_id>.log` | one line per step of a run (timestamp, phase, status, detail) |
| `backtest_*` | backtest tables, charts and the cached price panel |

When a night reports zero signals, check its log for a `DOWNLOAD warn` line
first. Yahoo sometimes serves an unsettled bar (no close), which the scan drops,
or leaves a hole for individual tickers.

## Nightly schedule (Windows Task Scheduler)

The scan runs Tue–Sat at **00:00 ET**, the morning after each US session, and
grades the previous day's close. Don't schedule it right after the close: at
that hour Yahoo's volume does not yet include the closing auction, and the
volume-surge test under-fires.

```powershell
$repo     = "C:\path\to\stock_analyzer"
$action   = New-ScheduledTaskAction -Execute "$repo\run_scanner.bat" -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday,Wednesday,Thursday,Friday,Saturday -At 7:00am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName "SP500 Breakout Scanner" -Action $action -Trigger $trigger -Settings $settings
```

`7:00am` is 00:00 ET in Israel; use your local equivalent.

A weekly task grades the week's signals onto the risk/reward plane. It posts
nothing to Discord.

```powershell
$action   = New-ScheduledTaskAction -Execute "$repo\run_universe.bat" -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 18:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "SP500 Universe Plane" -Action $action -Trigger $trigger -Settings $settings
```

The full 903-ticker plane is on no schedule. Run `python universe_scan.py` when
you want it refreshed; it also rebuilds the sector peer distributions.

```powershell
Get-ScheduledTaskInfo -TaskName "SP500 Breakout Scanner"   # last result: 0 = ok, 3221225786 = killed mid-run
Start-ScheduledTask   -TaskName "SP500 Breakout Scanner"   # run now
Get-Content output\scanner_log.txt -Tail 40
```

The `.bat` files find their own folder and call `python` from `PATH`. If Task
Scheduler resolves a different interpreter, set `PYTHON` to an absolute path.
A run interrupted mid-way (for example by a shutdown at 07:01) is not retried.
Its signals are archived first, so only that night's message is lost.

## Tests

```powershell
python tests/run_all.py              # offline + cache-backed, ~4 minutes
python tests/run_all.py --network    # adds the Yahoo round-trip test
```

The tests are plain scripts with no test dependency. Each exits 0 (pass),
1 (fail) or 2 (skip: a missing prerequisite such as the cached panel). They
assert **invariants**, not recorded output, because the thresholds are retuned
constantly. They never download data (except `--network`) and never post to
Discord.

## Account boundary

There is no brokerage-account code in this project. Nothing reads positions,
balances or P&L, and Claude sessions are instructed never to call IBKR's
account tools. A security is graded on its own merits, and the portfolio is a
virtual ledger that cannot place an order.

## Data sources, and their terms

Everything reads public sources. If you run it, you are the one making the
requests, so check that your use fits each provider's terms:

| Source | Used for | Notes |
|---|---|---|
| Yahoo Finance (`yfinance`) | prices, fundamentals, statements | Unofficial; terms restrict redistribution. Data stays in your local, git-ignored `output/` |
| Wikipedia | index constituent lists | Content is CC BY-SA |
| SEC EDGAR | filings, 8-K items, full-text search | Requires a real contact in the User-Agent (`STOCK_ANALYZER_SEC_USER_AGENT`) |
| Google News RSS | dated events for the theme screen | Headlines and links only |
| Interactive Brokers (Claude connector) | company and theme graph for the `enrich` and `theme-screen` skills | Optional, sessions only; see `docs/overview.html` |

No free source provides earnings-call transcripts.

Companion documents:

- [`docs/overview.html`](docs/overview.html): a one-page visual overview of the
  pipeline, the capabilities and MCP management (download and open in a browser).
- [`AI_ROLE.md`](AI_ROLE.md): where AI is and is not used.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short:

- tests assert invariants, not snapshots;
- every number stays computed in Python;
- `python tests/run_all.py` must pass.

Found a security issue? Report it privately; see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE). Read the warranty disclaimer, and the investment-advice note at
the top of this file.
