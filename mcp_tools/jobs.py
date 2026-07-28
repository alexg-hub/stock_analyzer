"""Background jobs, so a long run does not block the MCP client.

A universe backtest downloads ~500 tickers and a full nightly scan is minutes
of Yahoo traffic; MCP clients time a tool call out well before either finishes.
Long tools therefore hand back a `job_id` immediately and the caller polls.

Three things this file has to get right, each learned the hard way:

- **Progress has to be real.** `status()` promises a log tail, so every job must
  be given a `log_path` that actually fills up while it runs. A subprocess job
  streams its merged stdout+stderr to `output/jobs/<job_id>.log` line by line
  (hence `-u`); an in-process job points at the server's own step log. A job
  started without a log path shows an empty tail forever, and a caller with no
  signal guesses -- it will conclude "still downloading" and back off while the
  run has already finished.
- **The run id is minted here, not in the child.** The parent needs to know it
  up front so the step log is findable, and exporting it keeps the whole run in
  one `output/logs/<run_id>.log` -- the same "three processes, one log" rule the
  nightly `.bat` chain follows.
- **The registry outlives the process.** `_JOBS` is memory, and an MCP server is
  restarted by every `/mcp` reconnect. Each record is mirrored to
  `output/jobs/<job_id>.json` so a finished job still answers after a restart
  instead of reporting "no such job".
"""

import json
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scanner_common import RUN_ID_ENV, new_run_id, output_dir, run_id, run_log_path

RUNNING = "running"
DONE = "done"
ERROR = "error"
# Recorded as running, but by a server process that is gone. The work may well
# have finished -- the log file is the only remaining evidence, so say so rather
# than claiming either outcome.
ORPHANED = "orphaned"

# Long runs are I/O-bound (Yahoo, SEC, disk), so a small pool is plenty and
# keeps two universe backtests from fighting over the same cache file.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sa-job")
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()

# How many jobs to keep. Bounded so a long session cannot grow without limit;
# the results themselves live in output/, not here.
_MAX_JOBS = 200
# Files are cheap but not free, and a job log can be thousands of lines.
_MAX_JOB_FILES = 400


def jobs_dir(create: bool = True) -> Path:
    """`output/jobs/` -- same rule as every other generated artifact."""
    path = output_dir(create) / "jobs"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def job_log_path(job_id: str, create: bool = True) -> Path:
    return jobs_dir(create) / f"{job_id}.log"


def job_meta_path(job_id: str, create: bool = True) -> Path:
    return jobs_dir(create) / f"{job_id}.json"


def server_step_log() -> Path:
    """The step log this server process writes.

    Progress for an *in-process* job: it runs inside the server, so its
    `log_step` lines land in the server's own run log rather than a job file.
    """
    return run_log_path(None, run_id())


def _persist(record: dict) -> None:
    """Mirror a record to disk. Never raises -- a lost job file must not fail
    the job, exactly as `log_step` must never fail the run it is logging."""
    try:
        payload = {k: v for k, v in record.items() if k != "result"}
        payload["result"] = record.get("result")
        job_meta_path(record["job_id"]).write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except Exception:                                            # noqa: BLE001
        pass


def _recover(job_id: str) -> dict | None:
    """Read a record this process did not start (i.e. from before a restart)."""
    try:
        record = json.loads(job_meta_path(job_id, create=False)
                            .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if record.get("status") == RUNNING:
        # Whatever was running it is gone; the thread died with the old process.
        record["status"] = ORPHANED
        record["note"] = ("recorded while running by a server process that has "
                          "since restarted -- read log_tail to see how far it "
                          "got, and check the artifacts it would have written")
    return record


def _prune() -> None:
    """Drop the oldest finished jobs once the registry gets long."""
    if len(_JOBS) <= _MAX_JOBS:
        return
    finished = sorted(
        ((jid, j) for jid, j in _JOBS.items() if j["status"] != RUNNING),
        key=lambda kv: kv[1].get("finished") or 0,
    )
    for jid, _ in finished[: len(_JOBS) - _MAX_JOBS]:
        _JOBS.pop(jid, None)


def _prune_files() -> None:
    """Keep `output/jobs/` bounded, oldest first. Never raises."""
    try:
        files = sorted(jobs_dir().glob("*.*"), key=lambda p: p.stat().st_mtime)
        for path in files[: max(0, len(files) - _MAX_JOB_FILES)]:
            path.unlink(missing_ok=True)
    except Exception:                                            # noqa: BLE001
        pass


def reserve(kind: str, log_path: str | Path | None = None) -> dict:
    """Register a job and hand back its id, run id and log path.

    Split from `submit` because a subprocess job needs its log path *before* the
    work starts -- the path is derived from the job id, so the id has to exist
    first.
    """
    job_id = f"{kind}-{uuid.uuid4().hex[:8]}"
    rid = new_run_id()
    if log_path is None:
        log_path = job_log_path(job_id)
    record = {
        "job_id": job_id,
        "kind": kind,
        "status": RUNNING,
        "started": time.time(),
        "finished": None,
        "log_path": str(log_path),
        "run_id": rid,
        "result": None,
        "error": None,
    }
    with _LOCK:
        _JOBS[job_id] = record
        _prune()
    _persist(record)
    _prune_files()
    return {"job_id": job_id, "run_id": rid, "log_path": str(log_path)}


def submit(job_id: str, fn, *args, **kwargs) -> dict:
    """Attach the work to a reserved job and return the handle immediately."""
    with _LOCK:
        record = _JOBS[job_id]

    def _run():
        try:
            value = fn(*args, **kwargs)
            with _LOCK:
                record["result"] = value
                record["status"] = DONE
        except BaseException as exc:             # noqa: BLE001 -- a job must never
            with _LOCK:                          # take the server down with it
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["traceback"] = traceback.format_exc()
                record["status"] = ERROR
        finally:
            with _LOCK:
                record["finished"] = time.time()
                snapshot = dict(record)
            _persist(snapshot)

    _POOL.submit(_run)
    return {
        "job_id": job_id,
        "kind": record["kind"],
        "status": RUNNING,
        "run_id": record["run_id"],
        "log_path": record["log_path"],
        "poll_with": "job_status",
        "note": "Poll job_status(job_id); call job_result(job_id) once it is 'done'.",
    }


def start(kind: str, fn, *args, log_path: str | Path | None = None, **kwargs) -> dict:
    """Reserve and submit in one call, for a job that needs no path up front."""
    handle = reserve(kind, log_path=log_path)
    return submit(handle["job_id"], fn, *args, **kwargs)


def child_env(rid: str) -> dict:
    """Environment for a subprocess job.

    `-u` on the command line plus `PYTHONUNBUFFERED` is what makes the job log
    grow *while* the run works rather than all at once when it exits -- without
    it the tail is empty until the moment it stops being useful.
    """
    env = dict(os.environ)
    env[RUN_ID_ENV] = rid
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _tail(path: str | None, lines: int) -> list[str]:
    if not path or lines <= 0:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        # A log that does not exist yet is normal: the run may not have logged
        # its first step. Never let a missing log fail a status call.
        return []
    return text.splitlines()[-lines:]


def _elapsed(record: dict) -> float:
    end = record.get("finished") or time.time()
    return round(end - record["started"], 1)


def _snapshot(job_id: str) -> dict | None:
    with _LOCK:
        record = _JOBS.get(job_id)
        if record is not None:
            return dict(record)
    return _recover(job_id)


def status(job_id: str, tail: int = 40) -> dict:
    """State, elapsed seconds, and the tail of the run's log."""
    snapshot = _snapshot(job_id)
    if snapshot is None:
        return {"job_id": job_id, "status": "unknown",
                "error": "no such job -- not in this session and no record on disk"}
    out = {
        "job_id": job_id,
        "kind": snapshot["kind"],
        "status": snapshot["status"],
        "elapsed_s": _elapsed(snapshot),
        "run_id": snapshot.get("run_id"),
        "log_path": snapshot.get("log_path"),
        "log_tail": _tail(snapshot.get("log_path"), tail),
    }
    for key in ("error", "note"):
        if snapshot.get(key):
            out[key] = snapshot[key]
    return out


def result(job_id: str) -> dict:
    """The job's return value, once it has one."""
    snapshot = _snapshot(job_id)
    if snapshot is None:
        return {"job_id": job_id, "status": "unknown",
                "error": "no such job -- not in this session and no record on disk"}
    if snapshot["status"] == RUNNING:
        return {"job_id": job_id, "status": RUNNING,
                "elapsed_s": _elapsed(snapshot),
                "note": "still running -- poll job_status"}
    if snapshot["status"] == ERROR:
        return {"job_id": job_id, "status": ERROR,
                "error": snapshot["error"],
                "traceback": snapshot.get("traceback")}
    if snapshot["status"] == ORPHANED:
        return {"job_id": job_id, "status": ORPHANED,
                "elapsed_s": _elapsed(snapshot),
                "log_path": snapshot.get("log_path"),
                "note": snapshot.get("note"),
                "log_tail": _tail(snapshot.get("log_path"), 40)}
    return {"job_id": job_id, "status": DONE,
            "elapsed_s": _elapsed(snapshot), "result": snapshot.get("result")}


def listing(limit: int = 20) -> list[dict]:
    """Most recent jobs first, this session's and earlier ones alike."""
    with _LOCK:
        records = {j["job_id"]: dict(j) for j in _JOBS.values()}
    try:
        for meta in jobs_dir(create=False).glob("*.json"):
            if meta.stem not in records:
                recovered = _recover(meta.stem)
                if recovered:
                    records[meta.stem] = recovered
    except OSError:
        pass
    ordered = sorted(records.values(), key=lambda j: j["started"], reverse=True)
    return [{"job_id": j["job_id"], "kind": j["kind"], "status": j["status"],
             "elapsed_s": _elapsed(j), "run_id": j.get("run_id")}
            for j in ordered[:limit]]


def register(mcp) -> None:
    @mcp.tool()
    def job_status(job_id: str, tail: int = 40) -> dict:
        """Check a background job: state, elapsed time, and its log tail.

        The tail is the run's live output -- a subprocess job streams stdout and
        stderr into it as it works, so it is the real progress signal. Poll this
        rather than guessing how long a run should take.

        A job recorded by a server process that has since restarted (any `/mcp`
        reconnect) comes back as 'orphaned' with its log, not as 'running'.
        """
        return status(job_id, tail)

    @mcp.tool()
    def job_result(job_id: str) -> dict:
        """Fetch a finished job's return value.

        Returns a 'still running' note rather than blocking if it is not done.
        """
        return result(job_id)

    @mcp.tool()
    def list_jobs(limit: int = 20) -> list[dict]:
        """List recent jobs, newest first -- including ones from earlier sessions."""
        return listing(limit)
