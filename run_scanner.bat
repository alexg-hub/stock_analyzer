@echo off
rem Daily S&P 500 breakout scan -- invoked by Windows Task Scheduler.
cd /d "C:\Users\Lenovo\CC\stock_analyzer"
echo ==== Scan started %date% %time% ==== >> scanner_log.txt
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" breakout_scanner.py >> scanner_log.txt 2>&1
echo ==== Scan finished %date% %time% (exit %errorlevel%) ==== >> scanner_log.txt
