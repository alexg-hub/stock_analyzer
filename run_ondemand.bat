@echo off
rem On-demand deep dive for one named ticker: all three tiers, no nightly scan
rem required. Usage:  run_ondemand.bat PGR
rem
rem The deterministic half (tiers 1 + 2, the financials chart, the facts file)
rem runs inside `research_report.py context`, which scans the ticker itself when
rem the nightly hand-off has no row for it. This file only supplies the prompt
rem and the same headless invocation the nightly run uses.
if "%~1"=="" (
    echo usage: run_ondemand.bat TICKER [TICKER ...]
    exit /b 1
)
cd /d "C:\Users\Lenovo\CC\stock_analyzer"
rem cmd expands the >> redirect before Python runs, so the output directory has
rem to exist first -- output_dir() would create it too late.
if not exist output md output

rem Same model as the nightly run, handed over through a file: cmd's `for /f`
rem mangles a quoted interpreter path inside backticks.
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" research_report.py auto-model > output\deepdive_model.txt
set /p MODEL=<output\deepdive_model.txt

rem Same step-log wiring as run_deepdive.bat, for the same reasons: the run id
rem is exported so Claude's `context` subprocess appends to this run's log, and
rem the session UUID makes the transcript findable before the run starts. The
rem result path comes from config too (tokens=1,2* so a path with spaces
rem survives), so nothing here hardcodes research.logging.dir.
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" research_report.py run-id > output\deepdive_ids.txt
set /p RUN_IDS=<output\deepdive_ids.txt
for /f "tokens=1,2*" %%a in ("%RUN_IDS%") do (
    set STOCK_ANALYZER_RUN_ID=%%a
    set SESSION_ID=%%b
    set RESULT_JSON=%%c
)

rem Both ids go in the banner: it is the only durable record of them if the run
rem is killed before log-session runs, and both are needed to complete the log.
echo ==== On-demand deep-dive started %date% %time% (%* / model %MODEL%, run %STOCK_ANALYZER_RUN_ID%, session %SESSION_ID%) ==== >> output\deepdive_log.txt

rem The prompt is written to a file rather than piped through `echo`, so a
rem multi-line instruction stays readable and cmd never re-parses its contents.
> output\ondemand_prompt.txt (
    echo /deep-dive %*
    echo.
    echo On-demand run -- these tickers were named explicitly, not surfaced by the
    echo nightly scan, so the tier-2 gate does not apply. `context` runs tiers 1
    echo and 2 for each of them; report the trigger it returns, including "no
    echo active technical signal" when that is the answer.
    echo Post the combined Discord summary at the end with send=true.
)

rem Same invocation as run_deepdive.bat, and for the same reasons: dontAsk so an
rem un-allowed tool is refused rather than hanging on a prompt nobody answers,
rem the IBKR MCP tools listed explicitly because an un-allowed tool is refused
rem *silently*, and a subcommand-only Bash rule -- never `Bash(python *)`, which
rem would be arbitrary code execution.
rem --session-id must stay: drop it and log-session has no transcript to find,
rem so the model's half of the step log goes missing without an error.
rem Keep this allow-list identical to run_deepdive.bat's -- same enumerated IBKR
rem tools (the `get_account_*`/`get_pa_*` family is deliberately excluded; the
rem user's real book is out of scope, see SKILL.md step 2) and the same Edit,
rem which grants nothing Write does not already grant over the same paths.
type output\ondemand_prompt.txt | claude -p ^
  --model %MODEL% ^
  --session-id %SESSION_ID% ^
  --permission-mode dontAsk ^
  --allowedTools "Bash(python research_report.py *) Read Write Edit Glob Grep WebSearch WebFetch mcp__claude_ai_Interactive_Brokers_IBKR__search_contracts mcp__claude_ai_Interactive_Brokers_IBKR__get_company_connections mcp__claude_ai_Interactive_Brokers_IBKR__get_company_themes mcp__claude_ai_Interactive_Brokers_IBKR__get_theme_details mcp__claude_ai_Interactive_Brokers_IBKR__search_investment_topics mcp__claude_ai_Interactive_Brokers_IBKR__get_price_snapshot mcp__claude_ai_Interactive_Brokers_IBKR__get_price_history mcp__claude_ai_Interactive_Brokers_IBKR__get_option_parameters mcp__claude_ai_Interactive_Brokers_IBKR__get_option_data" ^
  --output-format json > "%RESULT_JSON%" 2>>output\deepdive_log.txt
echo ==== On-demand deep-dive finished %date% %time% (exit %errorlevel%) ==== >> output\deepdive_log.txt

rem Merge the model's steps into the run log, write the END line, record the run.
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" research_report.py log-session %STOCK_ANALYZER_RUN_ID% %SESSION_ID% --mode on_demand >> output\deepdive_log.txt 2>&1
