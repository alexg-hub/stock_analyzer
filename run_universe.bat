@echo off
rem Weekly universe pass -- every S&P 500 name onto the risk/reward plane.
rem Invoked by the Windows Task Scheduler task "SP500 Universe Plane".
cd /d "C:\Users\Lenovo\CC\stock_analyzer"
rem cmd expands the >> redirect before Python runs, so the output directory has
rem to exist first -- scanner_common.output_dir() would create it too late.
if not exist output md output

rem Its own run id, and deliberately NOT inherited: this is a weekly pass that
rem stands alone, not part of a night's chain, so it gets its own line in
rem output\logs\ rather than appending to whatever the last scan opened.
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" -c "import scanner_common; print(scanner_common.new_run_id())" > output\universe_run_id.txt
set /p STOCK_ANALYZER_RUN_ID=<output\universe_run_id.txt

echo ==== Universe pass started %date% %time% (run %STOCK_ANALYZER_RUN_ID%) ==== >> output\universe_log.txt
rem 2>&1 matters: the step log writes to stderr, so the progress lines -- the
rem only way to tell a 12-minute run from a hung one -- would otherwise be lost.
rem Weekly, so the cache is always stale by `universe.cache_max_age_days` (7) and
rem every ticker is refetched; --refresh is not needed and would only remove the
rem ability of a re-run to skip what it already has.
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" universe_scan.py >> output\universe_log.txt 2>&1
echo ==== Universe pass finished %date% %time% (exit %errorlevel%) ==== >> output\universe_log.txt

rem No Discord. This is a reference artifact, not an event: the table, the PNG
rem and the interactive page land under output\universe\ and are read when you
rem want them. The nightly alert already carries each signal's own coordinates,
rem which is the part that is actionable on the day.
