@echo off
rem Daily S&P 500 scan (all scanners) -- invoked by Windows Task Scheduler.
rem %~dp0 is this file's own directory, so the repo can live anywhere. Task
rem Scheduler runs with an arbitrary working directory, hence the explicit cd.
cd /d "%~dp0"
rem `python` off PATH. Override by setting PYTHON first -- useful when Task
rem Scheduler's account resolves a different interpreter than your shell:
rem     set PYTHON=C:\Path\to\python.exe
if not defined PYTHON set PYTHON=python
rem cmd expands the >> redirect before Python runs, so the output directory has
rem to exist first -- scanner_common.output_dir() would create it too late.
if not exist output md output

rem One run id for the whole night, minted here and exported so everything the
rem scan starts writes to the SAME output\logs\<run_id>.log. Run run_scanners.py
rem on its own and it mints its own id. Handed over through a file because cmd's
rem `for /f` mangles a quoted interpreter path inside backticks.
"%PYTHON%" -c "import scanner_common; print(scanner_common.new_run_id())" > output\run_id.txt
set /p STOCK_ANALYZER_RUN_ID=<output\run_id.txt

echo ==== Scan started %date% %time% (run %STOCK_ANALYZER_RUN_ID%) ==== >> output\scanner_log.txt
rem 2>&1 matters: the step log writes to stderr, so scanner_log.txt keeps it.
"%PYTHON%" run_scanners.py >> output\scanner_log.txt 2>&1
echo ==== Scan finished %date% %time% (exit %errorlevel%) ==== >> output\scanner_log.txt

rem That one line is the whole night: all four tiers run inside run_scanners.py
rem and post one Discord message. Nothing is chained after it, and nothing here
rem can start a model (tests/test_no_model.py). See AI_ROLE.md.
