@echo off
rem Tier 3 -- the unattended deep-dive, run after the nightly scan (and usable
rem standalone). The synthesis is a skill-driven reasoning procedure, not a
rem Python function, so this invokes Claude Code headlessly over the candidates
rem the scan just handed off.
rem
rem All the policy lives in config.json (research.auto): whether to run at all,
rem the tier-2 gate, how many reports, whether to post to Discord. auto-prompt
rem exits non-zero when there is nothing to do, which is what keeps this file
rem free of any decision-making of its own.
cd /d "C:\Users\Lenovo\CC\stock_analyzer"
rem cmd expands the >> redirect before Python runs, so the output directory has
rem to exist first -- output_dir() would create it too late.
if not exist output md output

"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" research_report.py auto-prompt > output\deepdive_prompt.txt 2>> output\deepdive_log.txt
if errorlevel 1 (
    echo ==== Deep-dive skipped %date% %time% ^(nothing to analyse^) ==== >> output\deepdive_log.txt
    exit /b 0
)

rem The model comes from config, handed over through a file: cmd's `for /f`
rem mangles a quoted interpreter path inside backticks.
"C:\Users\Lenovo\AppData\Local\Microsoft\WindowsApps\python.exe" research_report.py auto-model > output\deepdive_model.txt
set /p MODEL=<output\deepdive_model.txt

echo ==== Deep-dive started %date% %time% (model %MODEL%) ==== >> output\deepdive_log.txt
rem --permission-mode dontAsk, not bypassPermissions: anything outside the
rem allow-list is refused instead of hanging on a prompt nobody will answer.
rem The IBKR MCP tools MUST be listed -- an un-allowed tool is refused silently,
rem which would drop the moat/competitor section with no error to explain it.
rem Do not add --bare (forces an API key, dropping the OAuth credential the
rem IBKR server is bound to) or --strict-mcp-config (ignores registered servers).
type output\deepdive_prompt.txt | claude -p ^
  --model %MODEL% ^
  --permission-mode dontAsk ^
  --allowedTools "Bash(python research_report.py *) Read Write Glob Grep WebSearch WebFetch mcp__claude_ai_Interactive_Brokers_IBKR__*" ^
  --output-format json >> output\deepdive_log.txt 2>&1
echo ==== Deep-dive finished %date% %time% (exit %errorlevel%) ==== >> output\deepdive_log.txt
