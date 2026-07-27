@echo off
rem Tier 3 -- the unattended deep-dive, run after the nightly scan (and usable
rem standalone). The synthesis is a skill-driven reasoning procedure, not a
rem Python function, so this invokes Claude Code headlessly over the candidates
rem the scan just handed off.
rem
rem All the policy lives in config.json (research.auto): whether to run at all,
rem the tier-2 gate, how many reports, whether to post to Discord. auto-prompt
rem exits non-zero when there is nothing to do, which is what keeps this file
rem free of any decision-making of its own.
cd /d "C:\Users\Lenovo\CC\stock_analyzer"
rem cmd expands the >> redirect before Python runs, so the output directory has
rem to exist first -- output_dir() would create it too late.
if not exist output md output

"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" research_report.py auto-prompt > output\deepdive_prompt.txt 2>> output\deepdive_log.txt
if errorlevel 1 (
    echo ==== Deep-dive skipped %date% %time% ^(nothing to analyse^) ==== >> output\deepdive_log.txt
    exit /b 0
)

rem The model comes from config, handed over through a file: cmd's `for /f`
rem mangles a quoted interpreter path inside backticks.
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" research_report.py auto-model > output\deepdive_model.txt
set /p MODEL=<output\deepdive_model.txt

rem Ids for the step log, minted together and split out of one line. The run id
rem is exported so the `context` subprocess Claude spawns appends to the same
rem log; the session UUID is handed to --session-id so the transcript is
rem findable BEFORE the run starts -- which is what lets a run killed mid-flight
rem (result 3221225786, a PC shutdown) still have its log completed by hand.
rem The result path is resolved by Python (tokens=1,2* so a path with spaces
rem survives) rather than hardcoded here, so research.logging.dir stays a real
rem setting; that call also creates the directory before cmd expands the ">".
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" research_report.py run-id > output\deepdive_ids.txt
set /p RUN_IDS=<output\deepdive_ids.txt
for /f "tokens=1,2*" %%a in ("%RUN_IDS%") do (
    set STOCK_ANALYZER_RUN_ID=%%a
    set SESSION_ID=%%b
    set RESULT_JSON=%%c
)

rem Both ids go in the banner: it is the only durable record of them if the run
rem is killed before log-session runs (no result JSON is written in that case),
rem and both are needed to complete the log by hand afterwards.
echo ==== Deep-dive started %date% %time% (model %MODEL%, run %STOCK_ANALYZER_RUN_ID%, session %SESSION_ID%) ==== >> output\deepdive_log.txt
rem --permission-mode dontAsk, not bypassPermissions: anything outside the
rem allow-list is refused instead of hanging on a prompt nobody will answer.
rem The IBKR MCP tools MUST be listed -- an un-allowed tool is refused silently,
rem which would drop the moat/competitor section with no error to explain it.
rem Do not add --bare (forces an API key, dropping the OAuth credential the
rem IBKR server is bound to) or --strict-mcp-config (ignores registered servers).
rem
rem Bash(python research_report.py *) is a PREFIX rule and Claude Code requires
rem every segment of a compound command to be allowed, so `cmd > file`,
rem `cmd; echo $?`, `python -c "..."` and scratch scripts are all refused. That
rem cost the 2026-07-26 shakedown its verdict post. Everything the skill needs is
rem a subcommand (context / candidates / post-verdicts) -- widen the skill's
rem vocabulary with a new subcommand, never this allow-list with `Bash(python *)`,
rem which would be arbitrary code execution.
rem --session-id must stay: without it log-session has no transcript to find and
rem the model's half of the step log goes missing (silently -- the Python half
rem still writes). The result JSON goes to its own file rather than into this
rem log, so what you read stays readable; log-session mines it for the END line.
type output\deepdive_prompt.txt | claude -p ^
  --model %MODEL% ^
  --session-id %SESSION_ID% ^
  --permission-mode dontAsk ^
  --allowedTools "Bash(python research_report.py *) Read Write Glob Grep WebSearch WebFetch mcp__claude_ai_Interactive_Brokers_IBKR__*" ^
  --output-format json > "%RESULT_JSON%" 2>>output\deepdive_log.txt
echo ==== Deep-dive finished %date% %time% (exit %errorlevel%) ==== >> output\deepdive_log.txt

rem Merge the model's steps into the run log, write the END line, record the run.
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" research_report.py log-session %STOCK_ANALYZER_RUN_ID% %SESSION_ID% --mode nightly >> output\deepdive_log.txt 2>&1

rem Tier 4 again, now that post-verdicts has written tonight's tier and
rem conviction. `mark` re-syncs the ledger before pricing it, so this is what
rem gets tonight's verdict onto tonight's position instead of tomorrow's -- and
rem the verdict is exactly the attribute the attribution analysis exists to
rem grade. Exits 0 on failure, like the copy in run_scanner.bat.
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" -m portfolio_sim mark >> output\deepdive_log.txt 2>&1
