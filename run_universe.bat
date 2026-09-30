@echo off
rem Weekly universe pass -- the week's signals onto the risk/reward plane.
rem Invoked by the Windows Task Scheduler task "SP500 Universe Plane".
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

rem Its own run id, and deliberately NOT inherited: this is a weekly pass that
rem stands alone, not part of a night's chain, so it gets its own line in
rem output\logs\ rather than appending to whatever the last scan opened.
"%PYTHON%" -c "import scanner_common; print(scanner_common.new_run_id())" > output\universe_run_id.txt
set /p STOCK_ANALYZER_RUN_ID=<output\universe_run_id.txt

echo ==== Weekly plane started %date% %time% (run %STOCK_ANALYZER_RUN_ID%) ==== >> output\universe_log.txt
rem 2>&1 matters: the step log writes to stderr, so the progress lines -- the
rem only way to tell a long run from a hung one -- would otherwise be lost.
rem
rem --from-signals grades only what the screens actually flagged in the window
rem (universe.signal_window_days, 7), which is the week's real candidate list and
rem a few dozen names rather than 903. It writes signals_plane_<date>.csv and
rem signals_plane.png/.html -- deliberately NOT the shared risk_reward.* files, so
rem a weekly run can never overwrite the whole-index plane.
rem
rem A quiet week exits 0 having written nothing, rather than re-rendering last
rem week's page over this one.
"%PYTHON%" universe_scan.py --from-signals >> output\universe_log.txt 2>&1
echo ==== Weekly plane finished %date% %time% (exit %errorlevel%) ==== >> output\universe_log.txt

rem The FULL 903-ticker pass is not on a schedule. It is the base population for
rem the sector-relative percentiles this scoring still needs, so re-run it by hand
rem (or via the MCP `universe_scan` tool) when you want the whole plane refreshed:
rem     python universe_scan.py
rem
rem No Discord either way. This is a reference artifact, not an event: the table,
rem the PNG and the interactive page land under output\universe\ and are read when
rem you want them. The nightly alert already carries each signal's own
rem coordinates, which is the part that is actionable on the day.
