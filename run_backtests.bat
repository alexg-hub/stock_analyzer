@echo off
rem Run all 3 single-ticker backtests (default validation cases) and write
rem their combined output to backtest_log.txt. Separate from the nightly
rem production log (scanner_log.txt); fresh file each run.
cd /d "C:\Users\Lenovo\CC\stock_analyzer"
set PY="C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe"

echo ==== Backtests started %date% %time% ==== > backtest_log.txt

echo. >> backtest_log.txt
echo ##### breakout -- JNJ 2025-01-01..2025-10-31 ##### >> backtest_log.txt
%PY% backtest_breakout.py --ticker JNJ --start 2025-01-01 --end 2025-10-31 >> backtest_log.txt 2>&1

echo. >> backtest_log.txt
echo ##### pullback -- MSFT 2024-01-01..2025-06-30 ##### >> backtest_log.txt
%PY% backtest_pullback.py --ticker MSFT --start 2024-01-01 --end 2025-06-30 >> backtest_log.txt 2>&1

echo. >> backtest_log.txt
echo ##### reclaim -- META 2023-01-01..2023-12-31 ##### >> backtest_log.txt
%PY% backtest_reclaim.py --ticker META --start 2023-01-01 --end 2023-12-31 >> backtest_log.txt 2>&1

echo. >> backtest_log.txt
echo ==== Backtests finished %date% %time% ==== >> backtest_log.txt
