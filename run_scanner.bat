@echo off
rem Daily S&P 500 scan (all scanners) -- invoked by Windows Task Scheduler.
cd /d "C:\Users\Lenovo\CC\stock_analyzer"
rem cmd expands the >> redirect before Python runs, so the output directory has
rem to exist first -- scanner_common.output_dir() would create it too late.
if not exist output md output
echo ==== Scan started %date% %time% ==== >> output\scanner_log.txt
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" run_scanners.py >> output\scanner_log.txt 2>&1
echo ==== Scan finished %date% %time% (exit %errorlevel%) ==== >> output\scanner_log.txt

rem Tier 3 runs off the hand-off the scan just wrote, so the Discord alert lands
rem first and the deep-dive verdicts follow a while later. It logs separately and
rem skips itself when nothing qualifies.
call "C:\Users\Lenovo\CC\stock_analyzer\run_deepdive.bat"
