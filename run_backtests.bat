@echo off
rem Run all 4 single-ticker backtests (default validation cases) and write
rem their combined output to output\backtest_log.txt. Separate from the nightly
rem production log (output\scanner_log.txt); fresh file each run.
rem %~dp0 is this file's own directory, so the repo can live anywhere. Task
rem Scheduler runs with an arbitrary working directory, hence the explicit cd.
cd /d "%~dp0"
rem `python` off PATH. Override by setting PYTHON first -- useful when Task
rem Scheduler's account resolves a different interpreter than your shell:
rem     set PYTHON=C:\Path\to\python.exe
if not defined PYTHON set PYTHON=python
set PY="%PYTHON%"
rem cmd expands the redirects before Python runs, so the output directory has to
rem exist first -- scanner_common.output_dir() would create it too late.
if not exist output md output
set LOG=output\backtest_log.txt

echo ==== Backtests started %date% %time% ==== > %LOG%

echo. >> %LOG%
echo ##### breakout -- JNJ 2025-01-01..2025-10-31 ##### >> %LOG%
%PY% backtest_breakout.py --ticker JNJ --start 2025-01-01 --end 2025-10-31 >> %LOG% 2>&1

echo. >> %LOG%
echo ##### pullback -- MSFT 2024-01-01..2025-06-30 ##### >> %LOG%
%PY% backtest_pullback.py --ticker MSFT --start 2024-01-01 --end 2025-06-30 >> %LOG% 2>&1

echo. >> %LOG%
echo ##### reclaim -- META 2023-01-01..2023-12-31 ##### >> %LOG%
%PY% backtest_reclaim.py --ticker META --start 2023-01-01 --end 2023-12-31 >> %LOG% 2>&1

echo. >> %LOG%
echo ##### trend -- COST 2023-06-01..2024-06-30 ##### >> %LOG%
%PY% backtest_trend.py --ticker COST --start 2023-06-01 --end 2024-06-30 >> %LOG% 2>&1

echo. >> %LOG%
echo ==== Backtests finished %date% %time% ==== >> %LOG%
