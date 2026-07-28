"""Tool implementations for `mcp_server.py`.

One module per tier, each exposing `register(mcp)`. `mcp_server.py` calls them
in turn, so adding a tool never touches the server itself.

Two rules hold this package together, and both exist because the server speaks
JSON-RPC over stdout:

- **Nothing here may write to stdout.** `mcp_server.py` quarantines fd 1 at
  startup, so a stray `print()` lands on stderr rather than corrupting the
  protocol -- but do not rely on that as a licence to print. Diagnostics belong
  in `scanner_common.log_step`, which is already stderr-only.
- **Side-effecting tools default to dry-run.** Anything that can reach Discord
  takes `send: bool = False`; anything that rewrites `config.json` takes
  `confirm: bool = False`. The Claude Code allow-list deliberately omits those
  tools so they always prompt, but the default is the real guard: an allow-list
  that fails to match fails *silently*.
"""

import sys
from pathlib import Path

# The production modules are flat in the repo root (`scanner_common.PROJECT_ROOT`
# depends on that and must stay as it is). This package is one level down, so
# put the root on the path rather than relying on the cwd -- MCP clients launch
# the server from the project directory, but a `python -c "import mcp_tools"`
# from anywhere else should still resolve.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
