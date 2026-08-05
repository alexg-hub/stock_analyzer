"""Backtests and threshold tuning -- all long, so all jobs.

Every one of these is a subprocess of the script the user would run by hand.
That is deliberate: it keeps an MCP-triggered backtest and a hand-run one from
ever diverging, it isolates pandas/matplotlib/yfinance state from the server
process, and it means the tool surface is a thin argument-validation layer
rather than a second implementation.

None of these touch Discord.
"""

import subprocess
import sys
import time
from pathlib import Path

from scanner_common import PROJECT_ROOT, load_config

from . import jobs

# Which single-ticker backtester belongs to which screen. A dict rather than a
# derived lookup because there is no strategy registry yet -- that is a later
# step, and this is the only place that would use one today.
SINGLE_BACKTESTS = {
    "breakout_strategy": "backtest_breakout.py",
    "pullback_strategy": "backtest_pullback.py",
    "reclaim_strategy": "backtest_reclaim.py",
}


# How long a child may go without writing a single byte before it is treated as
# hung. Every script reachable from here narrates within a second or two of
# starting -- `run_scanners.py` logs `SCAN start` before it downloads anything
# and `backtest_universe.py` prints STEP 1 -- so silence this long is not slow
# work, it is a child that never got going. Generous enough to absorb a cold
# import of pandas/matplotlib on a busy box.
FIRST_OUTPUT_TIMEOUT = 60

# How often the wait loop wakes to check on a running child.
_POLL_S = 1.0


def _stop(proc: subprocess.Popen) -> None:
    """Kill a child that will not be waited on, without ever hanging here."""
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:                            # pragma: no cover
        pass


def _run_to_log(cmd: list[str], log_path: str | Path, env: dict | None,
                timeout: int, first_output_timeout: int) -> tuple[int, str]:
    """Run `cmd` into `log_path`, watching that it actually starts working.

    Returns `(exit_code, stall_reason)`; `stall_reason` is "" for a child that
    ran to completion on its own.

    The watchdog exists because the failure it catches is invisible otherwise: a
    child that blocks during interpreter start-up writes nothing, so the log
    stays zero bytes and `job_status` reports `running` -- truthfully -- until
    `timeout` expires an hour later. Observed twice on 2026-08-05, where the MCP
    subprocess stalled in module import while the identical command run by hand
    completed in 74s. Watching the log *size* rather than parsing it keeps this
    agnostic about what any given script prints.
    """
    deadline = time.monotonic() + timeout
    with open(log_path, "w", encoding="utf-8", errors="replace") as sink:
        proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), stdout=sink,
                                stderr=subprocess.STDOUT, env=env,
                                stdin=subprocess.DEVNULL)
        started = time.monotonic()
        while True:
            try:
                return proc.wait(timeout=_POLL_S), ""
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            silent_for = now - started
            if (first_output_timeout
                    and silent_for > first_output_timeout
                    and _log_size(log_path) == 0):
                _stop(proc)
                reason = (f"no output after {int(silent_for)}s -- the child "
                          f"never reached its first log line and was killed")
                # Into the log as well: the log file is what `job_status` shows,
                # and an empty one is exactly the symptom being explained.
                sink.write(reason + "\n")
                sink.flush()
                return proc.returncode, reason
            if now > deadline:
                _stop(proc)
                raise subprocess.TimeoutExpired(cmd, timeout)


def _log_size(log_path: str | Path) -> int:
    try:
        return Path(log_path).stat().st_size
    except OSError:
        return 0


def run_script(args: list[str], log_path: str | Path | None = None,
               run_id: str | None = None, timeout: int = 3600,
               first_output_timeout: int = FIRST_OUTPUT_TIMEOUT) -> dict:
    """Run a project script to completion, streaming both streams to `log_path`.

    Streamed to a file rather than captured into a pipe because the log *is* the
    progress signal: `capture_output=True` holds everything until the process
    exits, so `job_status` shows nothing until the moment it no longer matters.
    These scripts narrate to stdout (`backtest_universe.py` prints STEP 1..6 and
    logs no steps at all), so stderr is merged in and the two interleave in the
    order they actually happened.

    `-u` matters: without it the child buffers its own stdout and the file stays
    empty for minutes even though it is open for writing.

    `stdin` is **always** `DEVNULL`. Left unset the child inherits the server's
    own stdin, which under `.mcp.json` is the JSON-RPC pipe from Claude Code --
    a handle no scanner or backtest has any business holding, and one that makes
    a stalled child indistinguishable from a slow one. None of these scripts
    read stdin, so closing it can only turn a silent block into a prompt `EOF`.
    """
    env = jobs.child_env(run_id) if run_id else None
    cmd = [sys.executable, "-u", *args]
    stall = ""
    if log_path is None:
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL)
        lines = (proc.stdout or "").splitlines()
        code = proc.returncode
    else:
        code, stall = _run_to_log(cmd, log_path, env, timeout,
                                  first_output_timeout)
        lines = Path(log_path).read_text(
            encoding="utf-8", errors="replace").splitlines()
    out = {
        "command": " ".join(args),
        "exit_code": code,
        "output_tail": lines[-60:],
        "log_path": str(log_path) if log_path else None,
        "run_id": run_id,
        "ok": code == 0 and not stall,
    }
    if stall:
        # `stalled` is the flag `jobs.submit` keys off to record the job as an
        # error rather than a completed run -- distinct from a plain non-zero
        # exit, which several of these scripts use to mean something specific.
        out["stalled"] = True
        out["error"] = stall
    return out


def _script_job(kind: str, args: list[str]) -> dict:
    """Reserve a job, then run `args` into its log with its run id exported."""
    handle = jobs.reserve(kind)
    return jobs.submit(handle["job_id"], run_script, args,
                       log_path=handle["log_path"], run_id=handle["run_id"])


def _csv_list(values, name: str) -> str:
    """Accept 30, [10,30], or '10,30' for the list-valued backtest flags."""
    if values is None:
        return ""
    if isinstance(values, (list, tuple)):
        return ",".join(str(int(v)) for v in values)
    if isinstance(values, int):
        return str(values)
    text = str(values).strip()
    for part in text.split(","):
        if part.strip() and not part.strip().lstrip("-").isdigit():
            raise ValueError(f"{name} must be integers, got {text!r}")
    return text


def register(mcp) -> None:
    @mcp.tool()
    def backtest_universe(years: int | None = None,
                          holding_days: str | None = None,
                          entry_delay: str | None = None,
                          entry: str | None = None,
                          screens: str | None = None,
                          split_by_tier: bool = False,
                          refresh: bool = False,
                          no_chart: bool = False) -> dict:
        """Run the universe-wide profit backtest: every screen x all history.

        Sweeps a 'wait N trading days, then hold M' grid and scores each cell
        against a random-entry baseline. Long (minutes) -- returns a job_id.

        `holding_days`/`entry_delay` take a comma-separated list ('10,30,60').
        `refresh` re-downloads the cached price panel; without it a re-run after
        a config tweak takes seconds. Deliberately ignores each screen's
        `enabled` flag -- a screen gets switched off when it underperforms,
        which is when you most need to measure it. Never touches Discord.
        """
        args = ["backtest_universe.py"]
        if years is not None:
            args += ["--years", str(int(years))]
        if holding_days is not None:
            args += ["--holding-days", _csv_list(holding_days, "holding_days")]
        if entry_delay is not None:
            args += ["--entry-delay", _csv_list(entry_delay, "entry_delay")]
        if entry is not None:
            if entry not in ("next_open", "signal_close"):
                raise ValueError("entry must be 'next_open' or 'signal_close'")
            args += ["--entry", entry]
        if screens:
            args += ["--screens", screens]
        if split_by_tier:
            args.append("--split-by-tier")
        if refresh:
            args.append("--refresh")
        if no_chart:
            args.append("--no-chart")
        return _script_job("backtest_universe", args)

    @mcp.tool()
    def backtest_ticker(screen: str, ticker: str,
                        start: str | None = None, end: str | None = None) -> dict:
        """Replay one screen over one ticker's history, day by day.

        `screen` is a config key: breakout_strategy, pullback_strategy or
        reclaim_strategy. Writes output/backtest_*.csv and .png. Always
        downloads (no cache), so it is a job. Never touches Discord.
        """
        script = SINGLE_BACKTESTS.get(screen)
        if script is None:
            raise ValueError(
                f"unknown screen {screen!r} -- expected one of "
                f"{sorted(SINGLE_BACKTESTS)}")
        args = [script, "--ticker", ticker.upper()]
        if start:
            args += ["--start", start]
        if end:
            args += ["--end", end]
        return _script_job("backtest_ticker", args)

    @mcp.tool()
    def tune_screen(mode: str, screen: str, years: int | None = None,
                    holding: int | None = None, entry: str | None = None,
                    csv: bool = False) -> dict:
        """Sweep one screen's thresholds and score them against the baseline.

        `mode` is 'sensitivity' (one parameter at a time), 'grid' (a cross
        product) or 'delay'. Reads the cached price panel only, so run
        `backtest_universe` once first -- this checks for the cache and tells
        you rather than starting a job that dies. Read the `protected` column:
        a tightening that stops a known good signal firing is a bad tightening.
        """
        if mode not in ("sensitivity", "grid", "delay"):
            raise ValueError("mode must be sensitivity, grid or delay")
        if screen not in load_config():
            raise ValueError(f"no '{screen}' section in config.json")

        # Pre-flight: tune_screen.py dies inside `cached_panel` without this,
        # minutes after the job started, with the reason buried in a log tail.
        import backtest_universe
        cache = backtest_universe.cache_path(load_config().get("backtest", {}))
        if not cache.exists():
            return {"error": "no cached price panel",
                    "expected_at": str(cache),
                    "fix": "run backtest_universe once first (it writes the cache)"}

        args = ["tune_screen.py", mode, screen]
        if years is not None:
            args += ["--years", str(int(years))]
        if holding is not None:
            args += ["--holding", str(int(holding))]
        if entry is not None:
            args += ["--entry", entry]
        if csv:
            args.append("--csv")
        return _script_job("tune_screen", args)
