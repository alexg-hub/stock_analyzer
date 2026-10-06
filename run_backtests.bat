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

rem Each script's own defaults are its documented validation case (JNJ breakout,
rem MSFT pullback, META reclaim, COST trend), so nothing is repeated here.
for %%S in (breakout pullback reclaim trend) do (
    echo.>> %LOG%
    echo ##### %%S ##### >> %LOG%
    %PY% backtest_%%S.py >> %LOG% 2>&1
)

echo. >> %LOG%
echo ==== Backtests finished %date% %time% ==== >> %LOG%
