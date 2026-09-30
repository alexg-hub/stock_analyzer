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

rem That ONE line is the whole night. All four tiers run inside run_scanners.py
rem -- the screens, the quality check, the veto, the graded verdict, the ledger
rem and the exit scan -- so they can go out in ONE Discord message: the signal,
rem what it graded out at, and what to sell, together instead of three posts at
rem three different times. Each step is individually fail-safe (see run_ledger /
rem run_verdicts): a broken ledger or a failed verdict costs its own section of
rem the alert and nothing more.
rem
rem Nothing is chained after it, and nothing here invokes a model. Every number
rem the scan produces is computed in Python, which is what makes a re-run
rem reproducible and every figure checkable -- `tests/test_no_model.py` asserts
rem no code path can start one. Qualitative research is the `enrich` skill, run
rem from a Claude Code session on the tickers you choose; it records to
rem output/enrichment/ and cannot alter a verdict. See AI_ROLE.md.
