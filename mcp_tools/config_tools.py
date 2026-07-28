"""Reading and (carefully) writing `config.json`.

`config.json` holds the **live Discord webhook** and is committed on purpose to
a private repo, so `config_get` redacts it by default. Everything user-facing in
this project is config-driven, which is what makes these tools worth having --
and also what makes `config_set` the most dangerous tool on the server.
"""

import copy
import datetime as dt
import difflib
import json
from typing import Any

from scanner_common import CONFIG_PATH, load_config, output_dir

REDACTED = "<redacted -- pass reveal=True>"

# Subtrees a tool may rewrite. Everything else -- notably `data`, `research`
# and `discord` -- is refused.
WRITABLE = ("breakout_strategy", "pullback_strategy", "reclaim_strategy",
            "exit_strategy", "backtest", "tuning", "charts", "portfolio",
            "fundamentals")

# Refused even though a prefix above would otherwise allow them.
#
# `discord.*` is the live webhook. `research.auto.discord_send` is the second
# gate on `deepdive_post_verdicts` -- a tool able to flip it could unlock its
# own broadcast, which would make every other dry-run default in this server
# decorative. Both stay editable by hand, in the file, by a person.
FORBIDDEN = ("discord", "research.auto.discord_send")


def _redact(node, path: str = ""):
    """Copy `node`, blanking the webhook wherever it appears."""
    if isinstance(node, dict):
        return {k: (REDACTED if k == "webhook_url" else _redact(v, f"{path}.{k}"))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_redact(v, path) for v in node]
    return node


def _resolve(cfg: dict, path: str | None):
    """Walk a dotted path like 'breakout_strategy.enabled'."""
    if not path:
        return cfg
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"no such config path: {path!r} (stopped at {part!r})")
        node = node[part]
    return node


def get_impl(path: str | None = None, reveal: bool = False) -> dict:
    cfg = load_config()
    value = _resolve(cfg, path)
    return {"path": path or "<root>",
            "value": value if reveal else _redact(value, path or "")}


def _check_writable(path: str) -> None:
    if not path:
        raise ValueError("config_set needs a dotted path, not the whole file")
    for blocked in FORBIDDEN:
        if path == blocked or path.startswith(blocked + "."):
            raise ValueError(
                f"{path!r} is not writable through this tool. "
                f"{'The webhook is live' if blocked == 'discord' else 'This is the gate on Discord sends'} "
                f"-- edit config.json by hand if you really mean it.")
    if path.split(".")[0] not in WRITABLE:
        raise ValueError(
            f"{path!r} is outside the writable subtrees {WRITABLE}")


def _download_period_ok(cfg: dict) -> list[str]:
    """Every enabled screen's lookback must fit inside `data.download_period`.

    The gotcha this guards: a period shorter than a screen's total lookback
    means its rolling windows never fill, so the screen can *never fire* -- and
    it reports zero signals rather than an error.
    """
    import breakout_scanner
    import sma_pullback
    import sma_reclaim

    period = str(cfg.get("data", {}).get("download_period", "")).strip()
    if not period.endswith("mo") or not period[:-2].isdigit():
        return []                        # not a form we can check; leave it be
    bars = int(period[:-2]) * 21         # ~21 trading days a month
    problems = []
    for module in (breakout_scanner, sma_pullback, sma_reclaim):
        strategy = cfg.get(module.CONFIG_KEY)
        if not strategy or not strategy.get("enabled", True):
            continue
        need = module.required_history(strategy)
        if need > bars:
            problems.append(
                f"{module.CONFIG_KEY} needs {need} bars but "
                f"data.download_period={period} gives about {bars} -- "
                f"it could never fire")
    return problems


_DECODER = json.JSONDecoder()


def _coerce(value):
    """Parse a JSON scalar that arrived as text.

    An MCP client with no type on the parameter sends everything as a string,
    so `140` arrives as `"140"` and would be written as a string -- `charts.dpi`
    then reaches `savefig(dpi=...)` as text. `value: Any` lets a well-behaved
    client send the real type; this is the fallback for one that cannot.
    A string that is not valid JSON is a genuine string value ("next_open"),
    so it passes through untouched.
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _member_span(text: str, start: int, key: str) -> tuple[int, int]:
    """Span of `key`'s value within the JSON object beginning at/after `start`.

    Walks the object's *immediate* members, so a nested object reusing the same
    key name can never be matched by accident -- which a regex over the whole
    file would happily do.
    """
    i = text.index("{", start) + 1
    while True:
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
        if i >= len(text) or text[i] == "}":
            raise KeyError(key)
        name, i = _DECODER.raw_decode(text, i)
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text) or text[i] != ":":
            raise ValueError(f"malformed config.json near offset {i}")
        i += 1
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        vstart = i
        _, i = _DECODER.raw_decode(text, vstart)
        if name == key:
            return vstart, i


def _rewrite_one_value(text: str, parts: list[str], new_value) -> str:
    """Replace exactly one value, leaving every other byte of the file alone.

    `json.dumps(cfg)` would reformat the whole file: config.json is
    hand-maintained with compact inline collections (`"horizons": [10, 30, 60]`)
    and a full reserialisation expands every one of them, so a one-value edit
    would churn ~200 unrelated lines and bury the diff it is supposed to show.
    The user hand-tunes this file constantly; their formatting is not ours to
    rewrite.
    """
    start = 0
    for part in parts[:-1]:
        start, _ = _member_span(text, start, part)
    vstart, vend = _member_span(text, start, parts[-1])
    return text[:vstart] + json.dumps(new_value, ensure_ascii=False) + text[vend:]


def set_impl(path: str, value, confirm: bool = False) -> dict:
    _check_writable(path)
    value = _coerce(value)
    before_text = CONFIG_PATH.read_text(encoding="utf-8")
    cfg = json.loads(before_text)

    parts = path.split(".")
    node = cfg
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"no such config path: {path!r} (stopped at {part!r})")
        node = node[part]
    leaf = parts[-1]
    if not isinstance(node, dict) or leaf not in node:
        raise KeyError(f"no such config key: {path!r} "
                       f"(this tool edits existing keys, it does not add them)")

    old = copy.deepcopy(node[leaf])
    node[leaf] = value
    after_text = _rewrite_one_value(before_text, parts, value)

    # The surgical edit is text surgery, so prove it before offering to write
    # it: the rewritten file must parse to exactly the tree we validated.
    if json.loads(after_text) != cfg:
        raise ValueError("refusing to write: the edited text does not match the "
                         "intended config -- this is a bug in _rewrite_one_value")

    problems = _download_period_ok(cfg)
    diff = list(difflib.unified_diff(
        before_text.splitlines(), after_text.splitlines(),
        fromfile="config.json", tofile="config.json (proposed)", lineterm="", n=2))

    out = {"path": path, "old": old, "new": value,
           "diff": diff, "validation": problems, "written": False}

    if problems:
        out["refused"] = "validation failed -- nothing written"
        return out
    if not confirm:
        out["note"] = "preview only -- call again with confirm=True to write"
        return out

    backups = output_dir() / "config_backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backups / f"config_{stamp}.json"
    backup.write_text(before_text, encoding="utf-8")
    CONFIG_PATH.write_text(after_text, encoding="utf-8")

    out["written"] = True
    out["backup"] = str(backup)
    return out


def register(mcp) -> None:
    @mcp.tool()
    def config_set(path: str, value: Any, confirm: bool = False) -> dict:
        """Change one config.json value, with a diff preview and a backup.

        Returns the unified diff and writes nothing unless `confirm=True`.
        Backs up to `output/config_backups/` before every write. Only the one
        value is rewritten -- the file's hand-maintained formatting is left
        byte-for-byte alone, so the diff shows your change and nothing else.

        `value` keeps its JSON type: send 140, true or [10, 30, 60], not
        "140". A string that parses as JSON is converted; one that does not
        ("next_open") is stored as the string it is.

        Edits existing keys only, inside the strategy/backtest/tuning/charts/
        portfolio/fundamentals subtrees. `discord.*` and
        `research.auto.discord_send` are refused outright -- the webhook is
        live, and the latter is what gates Discord sends.

        Validates before writing that every enabled screen's lookback still
        fits inside `data.download_period`; a screen whose windows cannot fill
        reports zero signals rather than an error, so this is caught here.
        """
        return set_impl(path, value, confirm)

    @mcp.tool()
    def config_get(path: str | None = None, reveal: bool = False) -> dict:
        """Read config.json, or one dotted path within it.

        The Discord webhook is redacted unless `reveal=True`; it is a live URL
        in a committed file, so never paste it into output that leaves this
        machine. Examples: 'breakout_strategy', 'fundamentals.quality.rules'.
        """
        return get_impl(path, reveal)
