"""The MCP server's contract with the stdio protocol and with the dry-run rule.

Offline: nothing here downloads, and nothing here can reach Discord -- the two
tools that could are only ever *inspected*, never called.

The load-bearing check is `no tool writes to stdout`. The server speaks JSON-RPC
over stdout, and the tools call production code that prints freely
(`run_scanners.main` prints hit tables, `download_price_data` prints progress),
so a single stray write corrupts a frame and kills the session. `mcp_server`
quarantines fd 1 for exactly that reason; this is what proves the quarantine
works rather than assuming it.
"""

import inspect
import io
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from contextlib import redirect_stdout

from _harness import Checks, config

import anyio

c = Checks("mcp server")

# --------------------------------------------------------------------------
c.section("import purity")

# Import *after* the harness, so the step-log redirect is already installed.
before = sys.stdout
import mcp_server                                                  # noqa: E402
from mcp_tools import (backtests, config_tools, deepdive, jobs,  # noqa: E402
                       portfolio, signals, universe)

c.ok("importing the server writes nothing to stdout", sys.stdout is before)
c.ok("import does not mutate global streams (no enable_utf8_output at import)",
     sys.stdout is before and sys.stderr is not None)

# --------------------------------------------------------------------------
c.section("tool registry")

tools = anyio.run(mcp_server.mcp.list_tools)
names = {t.name for t in tools}

c.ok("at least one tool is registered", len(tools) > 0, f"{len(tools)} tools")
c.ok("every tool has a description",
     all((t.description or "").strip() for t in tools),
     ", ".join(sorted(t.name for t in tools if not (t.description or "").strip()))
     or "all documented")
c.ok("tool names are unique", len(names) == len(tools))

# The server exists to *do* things; reading files is Read/Glob's job. If one of
# these ever appears it means the read-only surface crept back in.
readers = {"read_report", "list_reports", "tail_log", "read_run_log",
           "portfolio_findings", "backtest_results", "tune_results"}
c.ok("no pure file-reading tools crept back in", not (names & readers),
     ", ".join(sorted(names & readers)) or "none")

# --------------------------------------------------------------------------
c.section("stdout purity -- the stdio-protocol invariant")

# Each of these is a real call against real repo state, chosen because they
# reach code that prints: `load_hits` reports a missing hand-off, and the
# portfolio readers go through pandas CSV loading.
buffer = io.StringIO()
errors = []
with redirect_stdout(buffer):
    for label, fn in (("scan_status", signals.scan_status_impl),
                      ("deepdive_candidates", deepdive.candidates_impl),
                      ("portfolio_status", portfolio.status_impl),
                      ("portfolio_positions", portfolio.positions_impl),
                      ("config_get", config_tools.get_impl),
                      ("list_jobs", jobs.listing),
                      # Reads a pickle and a CSV through pandas, and reports a
                      # missing cache -- both paths that print in this codebase.
                      ("universe_quadrant", universe.quadrant_impl)):
        try:
            fn()
        except Exception as exc:                      # noqa: BLE001
            errors.append(f"{label}: {type(exc).__name__}: {exc}")

c.ok("read-only tools all return without raising", not errors,
     "; ".join(errors) or f"{7} called")
c.ok("no tool wrote to stdout", buffer.getvalue() == "",
     repr(buffer.getvalue()[:200]) if buffer.getvalue() else "clean")

# The quarantine is what makes the above survive a tool that *does* print.
c.ok("server exposes the stdio runner that quarantines fd 1",
     callable(getattr(mcp_server, "_serve_stdio", None)))

# --------------------------------------------------------------------------
c.section("dry-run defaults")

# Signatures, not calls: these are precisely the tools that must never fire
# during a test run. The allow-list in .claude/settings.json omits them so they
# always prompt, but an allow-list that fails to match fails *silently* -- the
# default value is the guard that does not depend on matching.
SEND_TOOLS = ["run_nightly_scan", "portfolio_exit_scan"]
CONFIRM_TOOLS = ["config_set"]


def _param(tool_name: str, param: str):
    fn = mcp_server.mcp._tool_manager._tools[tool_name].fn
    return inspect.signature(fn).parameters.get(param)


for name in SEND_TOOLS:
    if name not in names:
        c.ok(f"{name} is registered", False, "missing")
        continue
    p = _param(name, "send")
    c.ok(f"{name} defaults to send=False",
         p is not None and p.default is False,
         "no `send` parameter" if p is None else f"default={p.default!r}")

for name in CONFIRM_TOOLS:
    if name not in names:
        c.ok(f"{name} is registered", False, "missing")
        continue
    p = _param(name, "confirm")
    c.ok(f"{name} defaults to confirm=False",
         p is not None and p.default is False,
         "no `confirm` parameter" if p is None else f"default={p.default!r}")

# --------------------------------------------------------------------------
c.section("config_get redacts the live webhook")

shown = config_tools.get_impl("discord")["value"]
c.ok("webhook is redacted by default",
     shown.get("webhook_url") == config_tools.REDACTED, str(shown.get("webhook_url")))
c.ok("reveal=True returns the real value",
     config_tools.get_impl("discord", reveal=True)["value"]["webhook_url"]
     == config()["discord"]["webhook_url"])
c.ok("a dotted path resolves",
     config_tools.get_impl("breakout_strategy")["value"].get("enabled") is not None)

missing = None
try:
    config_tools.get_impl("no.such.path")
except KeyError as exc:
    missing = str(exc)
c.ok("an unknown path raises rather than returning None", missing is not None)

# --------------------------------------------------------------------------
c.section("config_set is inert without confirm, and refuses the live webhook")

from scanner_common import CONFIG_PATH                              # noqa: E402

before_bytes = CONFIG_PATH.read_bytes()
before_mtime = CONFIG_PATH.stat().st_mtime

preview = config_tools.set_impl("charts.dpi",
                                config()["charts"]["dpi"], confirm=False)
c.ok("preview reports written=False", preview["written"] is False)
c.ok("preview returns a diff key", "diff" in preview)
c.ok("config.json bytes are unchanged", CONFIG_PATH.read_bytes() == before_bytes)
c.ok("config.json mtime is unchanged", CONFIG_PATH.stat().st_mtime == before_mtime)


def _refused(path, value):
    try:
        config_tools.set_impl(path, value, confirm=True)
    except (ValueError, KeyError):
        return True
    return False


c.ok("the live webhook cannot be written",
     _refused("discord.webhook_url", "https://example.invalid"))
c.ok("the Discord send gate cannot be flipped",
     _refused("research.auto.discord_send", True))
c.ok("a path outside the writable subtrees is refused",
     _refused("data.download_period", "1mo"))
c.ok("an unknown key is refused rather than created",
     _refused("charts.no_such_key", 1))
c.ok("config.json still unchanged after the refusals",
     CONFIG_PATH.read_bytes() == before_bytes)

# --------------------------------------------------------------------------
c.section("config_set writes JSON types, and only the one value")

# An MCP client with no type on the parameter sends every value as text, so
# without the coercion `charts.dpi` would be written as "140" and reach
# savefig(dpi=...) as a string.
c.ok("a numeric string becomes a number", config_tools._coerce("140") == 140
     and isinstance(config_tools._coerce("140"), int))
c.ok("'true' becomes a bool", config_tools._coerce("true") is True)
c.ok("a JSON list becomes a list", config_tools._coerce("[10, 30]") == [10, 30])
c.ok("a real string stays a string", config_tools._coerce("next_open") == "next_open")
c.ok("a value that is already typed is left alone", config_tools._coerce(120) == 120)

# The file is hand-maintained with compact inline collections; a full
# json.dumps() would expand every one of them, churning ~200 unrelated lines
# on a one-value edit and burying the diff the preview exists to show.
_text = CONFIG_PATH.read_text(encoding="utf-8")
_edited = config_tools._rewrite_one_value(_text, ["charts", "dpi"], 140)
c.ok("the rewritten file still parses",
     json.loads(_edited)["charts"]["dpi"] == 140)
c.ok("nothing but the target value changed",
     {**json.loads(_edited), "charts": {**json.loads(_edited)["charts"],
                                        "dpi": json.loads(_text)["charts"]["dpi"]}}
     == json.loads(_text))
c.ok("the line count is unchanged (no reformatting)",
     len(_edited.splitlines()) == len(_text.splitlines()))
c.ok("setting a value to itself is byte-identical",
     config_tools._rewrite_one_value(
         _text, ["charts", "dpi"], json.loads(_text)["charts"]["dpi"]) == _text)

# A key name that also exists at other nesting levels must resolve to the one
# the path names -- `breakout_strategy` is a top-level section *and* a member of
# both tuning.sweeps and tuning.grid.
_grid = config_tools._rewrite_one_value(
    _text, ["tuning", "grid", "breakout_strategy"], ["only_me"])
_after = json.loads(_grid)
_before = json.loads(_text)
c.ok("a repeated key resolves to the one the path names",
     _after["tuning"]["grid"]["breakout_strategy"] == ["only_me"]
     and _after["breakout_strategy"] == _before["breakout_strategy"]
     and _after["tuning"]["sweeps"] == _before["tuning"]["sweeps"])

c.ok("config.json unchanged by the rewrite checks",
     CONFIG_PATH.read_bytes() == before_bytes)

# --------------------------------------------------------------------------
c.section("job registry")

# `jobs_dir()` hangs off output_dir(), which is not config-driven, so redirect
# it here or every test run litters the real output/jobs/ with `test-*.json`.
_JOBDIR = Path(tempfile.mkdtemp(prefix="sa-jobs-"))
jobs.jobs_dir = lambda create=True: _JOBDIR


def _wait(job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if jobs.status(job_id)["status"] != jobs.RUNNING:
            return jobs.status(job_id)["status"]
        time.sleep(0.02)
    return "timeout"

handle = jobs.start("test", lambda: {"value": 42})
c.ok("start returns a job_id immediately", bool(handle.get("job_id")))
c.ok("a trivial job reaches done", _wait(handle["job_id"]) == jobs.DONE)
c.ok("job_result returns the value",
     jobs.result(handle["job_id"]).get("result") == {"value": 42})


def _boom():
    raise ValueError("intentional")

bad = jobs.start("test", _boom)
c.ok("a raising job reaches error without propagating",
     _wait(bad["job_id"]) == jobs.ERROR)
c.ok("the error is reported, not swallowed",
     "intentional" in (jobs.result(bad["job_id"]).get("error") or ""))
c.ok("an unknown job_id is reported, not raised",
     jobs.status("nope-00000000")["status"] == "unknown")

# --------------------------------------------------------------------------
c.section("every job gets a real log path, and progress is readable")

# The bug this pins: no caller passed `log_path`, so `log_tail` was empty for
# every job kind forever. `job_status` promises the tail *is* the progress
# signal, so a caller with no signal guesses -- and it guessed "still
# downloading" for six minutes after a universe backtest had finished.
c.ok("reserve hands back a log path", bool(jobs.reserve("test")["log_path"]))
c.ok("reserve mints a run id for the child to inherit",
     len(jobs.reserve("test")["run_id"]) == 8)
c.ok("a started job records a log path, never None",
     jobs.status(handle["job_id"])["log_path"])
from scanner_common import RUN_ID_ENV                               # noqa: E402

c.ok("child_env exports the run id under the name the step log reads",
     jobs.child_env("abc12345")[RUN_ID_ENV] == "abc12345")
c.ok("child_env unbuffers the child, or the tail fills only at exit",
     jobs.child_env("abc12345").get("PYTHONUNBUFFERED") == "1")

_probe = jobs.reserve("test")
Path(_probe["log_path"]).write_text("STEP 3 of 6\nhalfway\n", encoding="utf-8")
c.ok("a log that exists is tailed",
     jobs.status(_probe["job_id"], tail=5)["log_tail"] == ["STEP 3 of 6", "halfway"])
c.ok("job_status still tolerates a log file that does not exist",
     jobs.status(jobs.reserve("test")["job_id"])["log_tail"] == [])

# A log that exists but cannot be read must not read as "nothing logged yet":
# that is the same answer a quiet run gives, so the caller backs off and polls
# a fault forever. A directory stands in for any unreadable path.
_lines, _err = jobs._tail(str(_JOBDIR), 5)
c.ok("an unreadable log is reported, not silently empty",
     _lines == [] and _err.startswith("log unreadable"), _err or "no error given")
c.ok("a missing log stays silent (missing != unreadable)",
     jobs._tail(str(_JOBDIR / "absent.log"), 5) == ([], ""))

# --------------------------------------------------------------------------
c.section("a subprocess job that never starts is killed, not waited out")

# The bug this pins (2026-08-05): an MCP-spawned child blocked during
# interpreter start-up and wrote nothing, so the log stayed zero bytes and
# `job_status` reported `running` -- truthfully -- with `timeout=3600` ahead of
# it. The identical command run by hand finished in 74s. Two runs stalled that
# way before anyone could tell it was not just slow.
_silent = backtests.run_script(
    ["-c", "import time; time.sleep(120)"],
    log_path=str(_JOBDIR / "silent.log"), timeout=3600, first_output_timeout=2)
c.ok("a child that writes nothing is killed by the watchdog",
     _silent.get("stalled") is True and not _silent["ok"])
c.ok("the reason lands in the log, which is what job_status shows",
     any("no output" in line for line in _silent["output_tail"]),
     repr(_silent["output_tail"][-1:]))

# The watchdog must key off *silence*, not elapsed time -- a real scan runs for
# minutes after its first line.
_chatty = backtests.run_script(
    ["-c", "print('STEP 1'); import time; time.sleep(4); print('STEP 2')"],
    log_path=str(_JOBDIR / "chatty.log"), timeout=3600, first_output_timeout=2)
c.ok("a child that narrates then works is left alone",
     _chatty["ok"] and not _chatty.get("stalled")
     and _chatty["output_tail"][-1:] == ["STEP 2"], f"{_chatty['output_tail']}")

# Unset, stdin is inherited -- under `.mcp.json` that is the server's JSON-RPC
# pipe, a handle no scanner has any business holding.
_stdin = backtests.run_script(
    ["-c", "import sys; print('READ=%r' % sys.stdin.read())"],
    log_path=str(_JOBDIR / "stdin.log"), timeout=60, first_output_timeout=30)
c.ok("a child's stdin is closed, so reading it EOFs instead of blocking",
     _stdin["ok"] and any("READ=''" in line for line in _stdin["output_tail"]),
     f"{_stdin['output_tail']}")

_stalled_job = jobs.reserve("test")
jobs.submit(_stalled_job["job_id"], backtests.run_script,
            ["-c", "import time; time.sleep(120)"],
            log_path=_stalled_job["log_path"], first_output_timeout=2)
c.ok("a stalled job reports 'error', not 'done'",
     _wait(_stalled_job["job_id"], timeout=30) == jobs.ERROR)

# ...but a plain non-zero exit still means the run happened. Several scripts
# here use exit 1 to mean something specific, and that is not a job failure.
_rc1 = jobs.reserve("test")
jobs.submit(_rc1["job_id"], backtests.run_script,
            ["-c", "print('bye'); raise SystemExit(1)"],
            log_path=_rc1["log_path"], first_output_timeout=10)
c.ok("a non-zero exit is still 'done' (exit 1 is meaningful, not a stall)",
     _wait(_rc1["job_id"], timeout=30) == jobs.DONE)

# --------------------------------------------------------------------------
c.section("a job outlives the server process")

# `_JOBS` is memory and every `/mcp` reconnect restarts the server, so without
# the on-disk mirror a finished job answers "no such job" and its result is
# simply lost.
_done = jobs.start("test", lambda: {"kept": True})
_wait(_done["job_id"])
jobs._JOBS.clear()                                    # i.e. the server restarted
c.ok("a finished job is still 'done' after a restart",
     jobs.status(_done["job_id"])["status"] == jobs.DONE)
c.ok("its result survives the restart",
     jobs.result(_done["job_id"]).get("result") == {"kept": True})
c.ok("list_jobs finds jobs from an earlier session",
     any(j["job_id"] == _done["job_id"] for j in jobs.listing(50)))

# A record left 'running' by a process that is gone must not claim to be
# running -- nothing is running it. Say so and hand back the log instead.
_orphan = jobs.reserve("test")
Path(_orphan["log_path"]).write_text("got this far\n", encoding="utf-8")
jobs._JOBS.clear()
c.ok("a job orphaned by a restart is not reported as running",
     jobs.status(_orphan["job_id"])["status"] == jobs.ORPHANED)
c.ok("an orphaned job still hands back its log",
     jobs.status(_orphan["job_id"])["log_tail"] == ["got this far"])

shutil.rmtree(_JOBDIR, ignore_errors=True)

sys.exit(c.finish())
