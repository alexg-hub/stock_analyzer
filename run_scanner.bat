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

rem Tier 4: ledger tonight's signals as virtual positions straight away, then
rem price the book. `open` first and on its own line so the signal is recorded
rem even if the download in `mark` fails -- the ledger is the thing that cannot
rem be reconstructed later, the prices always can. Both exit 0 on failure by
rem design (see portfolio_sim/__main__.py): a broken ledger must never take down
rem the scan or the deep dive. Same 2>&1, same reason -- the step log is stderr.
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" -m portfolio_sim open >> output\scanner_log.txt 2>&1
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" -m portfolio_sim mark >> output\scanner_log.txt 2>&1

rem Tier 3 runs off the hand-off the scan just wrote, so the Discord alert lands
rem first and the deep-dive verdicts follow a while later. It logs separately and
rem skips itself when nothing qualifies.
call "C:\Users\Lenovo\CC\stock_analyzer\run_deepdive.bat"
