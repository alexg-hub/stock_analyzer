"""MCP server -- drive the whole four-tier analyzer from a Claude Code session.

Lives flat in the repo root on purpose: the production modules are flat and
`scanner_common.PROJECT_ROOT` is `Path(__file__).resolve().parent`, so a root
module imports `scanner_common`, `run_scanners`, `research_report` and
`portfolio_sim` with no path juggling and every config-relative output path
still resolves.

**Never create a top-level `mcp.py` or `mcp/`** -- it would shadow the installed
`mcp` package. `mcp_server.py` and `mcp_tools/` are safe.

What is deliberately *not* here: tools that only read a file. Claude Code has
`Read`/`Glob`, and `output/` is full of reports, logs, CSVs and charts that are
better read directly than through a wrapper. The tools here are the things a
file read cannot do -- run something, run it safely, or run it without blocking.

Usage:
    python mcp_server.py                 # stdio (what .mcp.json launches)
    python mcp_server.py --selftest      # list tools, call the read-only ones
    python mcp_server.py --transport http --port 8765
"""

import argparse
import os
import sys
from io import TextIOWrapper

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

from mcp_tools import backtests, config_tools, deepdive, jobs, portfolio, signals

mcp = FastMCP("stock_analyzer")

for module in (signals, deepdive, portfolio, backtests, config_tools, jobs):
    module.register(mcp)


async def _serve_stdio(server: FastMCP) -> None:
    """Serve over stdio with fd 1 quarantined.

    The transport owns stdout, and this codebase prints a *lot* on the paths
    these tools call -- `run_scanners.main` prints hit tables,
    `download_price_data` prints progress. One stray write to fd 1 corrupts a
    JSON-RPC frame and kills the session.

    `scanner_common.stdout_to_stderr()` is the wrong instrument here: it swaps
    the global `sys.stdout` for the duration of a call, FastMCP runs sync tool
    bodies on anyio worker threads, and the job pool adds more -- two
    overlapping calls would restore each other's handle. So quarantine once,
    at both levels, and hand the transport a private duplicate of the real
    stdout that nothing else can reach.

    `stdio_server()` accepts an explicit `stdout` and otherwise re-wraps
    `sys.stdout.buffer` itself, which is exactly what we must stop it doing.
    """
    real_fd = os.dup(sys.stdout.fileno())
    out = anyio.wrap_file(TextIOWrapper(os.fdopen(real_fd, "wb"),
                                        encoding="utf-8", write_through=True))
    os.dup2(sys.stderr.fileno(), 1)   # C-level writes to fd 1 -> stderr
    sys.stdout = sys.stderr           # ...and Python-level ones too

    async with stdio_server(stdout=out) as (read_stream, write_stream):
        inner = server._mcp_server
        await inner.run(read_stream, write_stream,
                        inner.create_initialization_options())


def _selftest() -> int:
    """Offline smoke test: every tool registered, read-only ones callable.

    Deliberately not part of `tests/run_all.py` -- that suite is plain scripts
    with no async dependency, and this needs the server object.
    """
    tools = anyio.run(mcp.list_tools)
    print(f"{len(tools)} tool(s) registered:", file=sys.stderr)
    for tool in sorted(tools, key=lambda t: t.name):
        summary = (tool.description or "").strip().splitlines()
        print(f"  {tool.name:28} {summary[0] if summary else '(no docstring)'}",
              file=sys.stderr)

    print("\nread-only calls:", file=sys.stderr)
    failures = 0
    checks = [
        ("scan_status", signals.scan_status_impl),
        ("deepdive_candidates", deepdive.candidates_impl),
        ("portfolio_status", portfolio.status_impl),
        ("portfolio_positions", portfolio.positions_impl),
        ("config_get", config_tools.get_impl),
        ("params_list", config_tools.params_list_impl),
        ("list_jobs", jobs.listing),
    ]
    for name, fn in checks:
        try:
            fn()
            print(f"  ok    {name}", file=sys.stderr)
        except Exception as exc:                      # noqa: BLE001
            failures += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"\n{'ok' if not failures else f'{failures} failure(s)'}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--selftest", action="store_true",
                        help="list tools, call the read-only ones, exit")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "http"],
                        help="stdio for Claude Code; http is the fallback "
                             "for clients that cannot launch a local process")
    parser.add_argument("--port", type=int, default=8765,
                        help="port for --transport http")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.transport == "http":
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
        return 0
    anyio.run(_serve_stdio, mcp)
    return 0


if __name__ == "__main__":
    # Before the fd quarantine, and never at import: when output is redirected
    # Windows hands Python the locale codepage (cp1255 here) and the quality
    # badge has no mapping in it. log_step echoes that badge to stderr.
    from scanner_common import enable_utf8_output
    enable_utf8_output()
    sys.exit(main())
