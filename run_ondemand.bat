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

echo ==== On-demand deep-dive started %date% %time% (%* / model %MODEL%) ==== >> output\deepdive_log.txt

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
type output\ondemand_prompt.txt | claude -p ^
  --model %MODEL% ^
  --permission-mode dontAsk ^
  --allowedTools "Bash(python research_report.py *) Read Write Glob Grep WebSearch WebFetch mcp__claude_ai_Interactive_Brokers_IBKR__*" ^
  --output-format json >> output\deepdive_log.txt 2>&1
echo ==== On-demand deep-dive finished %date% %time% (exit %errorlevel%) ==== >> output\deepdive_log.txt
