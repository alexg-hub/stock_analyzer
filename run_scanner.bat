@echo off
rem Daily S&P 500 scan (all scanners) -- invoked by Windows Task Scheduler.
cd /d "C:\Users\Lenovo\CC\stock_analyzer"
rem cmd expands the >> redirect before Python runs, so the output directory has
rem to exist first -- scanner_common.output_dir() would create it too late.
if not exist output md output

rem One run id for the whole night, minted here and exported: tiers 1+2 below and
rem tier 3 in run_deepdive.bat (whose `run-id` inherits it rather than minting)
rem then write to the SAME output\logs\<run_id>.log, so one file holds everything
rem that happened tonight. Run either .bat on its own and it mints its own id.
rem Handed over through a file, like auto-model: cmd's `for /f` mangles a quoted
rem interpreter path inside backticks.
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" -c "import scanner_common; print(scanner_common.new_run_id())" > output\run_id.txt
set /p STOCK_ANALYZER_RUN_ID=<output\run_id.txt

echo ==== Scan started %date% %time% (run %STOCK_ANALYZER_RUN_ID%) ==== >> output\scanner_log.txt
rem 2>&1 matters: the step log writes to stderr, so scanner_log.txt keeps it.
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" run_scanners.py >> output\scanner_log.txt 2>&1
echo ==== Scan finished %date% %time% (exit %errorlevel%) ==== >> output\scanner_log.txt

rem Tiers 3 and 4 now run INSIDE run_scanners.py, not as separate lines here.
rem Everything deterministic -- the screens, the quality check, the ledger, the
rem exit scan and the graded verdict -- happens in that one process so it can go
rem out in ONE Discord message: the signal, what it graded out at, and what to
rem sell, together instead of three posts at three different times. Each step is
rem still individually fail-safe (see run_ledger / run_verdicts): a broken ledger
rem or a failed verdict costs its own section of the alert and nothing more.

rem Tier 3's NARRATIVE half is what is left out here, and it is optional: the
rem verdict is already computed, recorded and posted by the line above. This
rem adds the written report (moat, news, filings) and may revise the conviction
rem within its bounded adjustment. It logs separately, skips itself when nothing
rem qualifies, and is switched off with research.narrative.enabled.
call "C:\Users\Lenovo\CC\stock_analyzer\run_deepdive.bat"
