"""Reading and (carefully) writing `config.json`.

`config.json` holds the **live Discord webhook** and is committed on purpose to
a private repo, so `config_get` redacts it by default. Everything user-facing in
this project is config-driven, which is what makes these tools worth having --
and also what makes the writers the most dangerous thing on this server.

Four writers, one shape: every one previews a unified diff, validates, and
writes **nothing** unless `confirm=True`. Every write backs the old file up to
`output/config_backups/` first.

  * `params_list`   -- what is tunable, and what it is set to right now
  * `config_get`    -- read a dotted path
  * `config_set`    -- change one value (or add one with `create=True`)
  * `config_edit`   -- several changes, atomically, under one diff
  * `config_delete` -- remove one key

The text surgery in `_rewrite_one_value` / `_insert_member` / `_delete_member`
exists for one reason: `json.dumps(cfg)` would reformat the whole file.
`config.json` is hand-maintained with compact inline collections
(`"horizons": [10, 30, 60]`) and a full reserialisation expands every one of
them, so a one-value edit would churn ~200 unrelated lines and bury the change
it is supposed to show. The user retunes this file constantly; their formatting
is not ours to rewrite.
"""

import copy
import datetime as dt
import difflib
import json
from typing import Any

import quality
from scanner_common import CONFIG_PATH, load_config, output_dir

REDACTED = "<redacted -- pass reveal=True>"

# Subtrees a tool may rewrite. Anything ending `_strategy` is matched by suffix
# rather than named, so a new entry or exit strategy is tunable the day it is
# added -- which is the point of asking for these tools in the first place.
#
# `data` is deliberately NOT here. `_download_period_ok` can only check the
# `<N>mo` form, so `download_period: "1y"` would sail past validation and leave
# every screen unable to fire while reporting zero signals rather than an error
# -- the precise failure this file exists to prevent. It stays a hand edit.
WRITABLE = ("quality", "ibkr", "research", "backtest", "tuning", "charts",
            "portfolio", "enrichment")
WRITABLE_SUFFIX = "_strategy"

# Refused even though a prefix above would otherwise allow them.
#
# `discord.*` is the live webhook. `research.auto.discord_send` is the gate on
# every Discord send this project makes -- a tool able to flip it could unlock
# its own broadcast, which would make every dry-run default in this server
# decorative. Both stay editable by hand, in the file, by a person.
FORBIDDEN = ("discord", "research.auto.discord_send")

# Sections whose parameters `params_list` knows how to describe individually.
STRATEGY_HINT = "swept by `tune_screen.py`; see tuning.sweeps for the grid"


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
        raise ValueError("this tool needs a dotted path, not the whole file")
    for blocked in FORBIDDEN:
        if path == blocked or path.startswith(blocked + "."):
            raise ValueError(
                f"{path!r} is not writable through this tool. "
                f"{'The webhook is live' if blocked == 'discord' else 'This is the gate on Discord sends'} "
                f"-- edit config.json by hand if you really mean it.")
    root = path.split(".")[0]
    if root not in WRITABLE and not root.endswith(WRITABLE_SUFFIX):
        raise ValueError(
            f"{path!r} is outside the writable subtrees {WRITABLE} "
            f"(plus anything named *{WRITABLE_SUFFIX})")


# --------------------------------------------------------------------------
# Validation -- everything that fails silently at runtime
# --------------------------------------------------------------------------

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


def validate(cfg: dict) -> list[str]:
    """Everything wrong with a proposed config, as plain sentences.

    `quality.validate` is the important half: an unknown `source` resolves to
    None for every company, a typo'd gate keyword is never applied, and a group
    with no weight contributes nothing -- all three look configured and do
    nothing, which is the failure mode these tools exist to prevent.
    """
    return _download_period_ok(cfg) + quality.validate(cfg)


# --------------------------------------------------------------------------
# Text surgery
# --------------------------------------------------------------------------

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


def _members(text: str, start: int):
    """Yield `(name, name_start, value_start, value_end)` for one object.

    Walks the object's *immediate* members only, so a nested object reusing the
    same key name can never be matched by accident -- which a regex over the
    whole file would happily do.
    """
    i = text.index("{", start) + 1
    while True:
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
        if i >= len(text) or text[i] == "}":
            return
        name_start = i
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
        yield name, name_start, vstart, i


def _member_span(text: str, start: int, key: str) -> tuple[int, int]:
    """Span of `key`'s value within the object beginning at/after `start`."""
    for name, _, vstart, vend in _members(text, start):
        if name == key:
            return vstart, vend
    raise KeyError(key)


def _member_full_span(text: str, start: int, key: str) -> tuple[int, int]:
    """Span of the whole `"key": value` member, name included."""
    for name, name_start, _, vend in _members(text, start):
        if name == key:
            return name_start, vend
    raise KeyError(key)


def _walk_to_parent(text: str, parts: list[str]) -> int:
    """Offset at which the object holding `parts[-1]` begins."""
    start = 0
    for part in parts[:-1]:
        start, _ = _member_span(text, start, part)
    return start


def _rewrite_one_value(text: str, parts: list[str], new_value) -> str:
    """Replace exactly one value, leaving every other byte of the file alone."""
    start = _walk_to_parent(text, parts)
    vstart, vend = _member_span(text, start, parts[-1])
    return text[:vstart] + json.dumps(new_value, ensure_ascii=False) + text[vend:]


def _indent_of(text: str, offset: int) -> str:
    """The leading whitespace of the line `offset` sits on.

    A member name normally starts right after its indentation, so the text
    between the line start and `offset` *is* the indent. When it is not (two
    members on one line), fall back to the file's four spaces rather than
    copying whatever happened to precede it.
    """
    line_start = text.rfind("\n", 0, offset) + 1
    prefix = text[line_start:offset]
    return prefix if prefix and not prefix.strip() else "    "


def _insert_member(text: str, parts: list[str], new_value) -> str:
    """Add `parts[-1]` to its parent object, matching the file's indentation.

    Appended after the last existing member rather than sorted in: the file's
    order is meaningful to the person maintaining it (parameters are grouped by
    what they measure), and re-sorting would produce a diff nobody asked for.
    """
    start = _walk_to_parent(text, parts)
    obj_start = text.index("{", start)
    _, obj_end = _DECODER.raw_decode(text, obj_start)
    members = list(_members(text, start))
    if members:
        last_end = members[-1][3]
        indent = _indent_of(text, members[-1][1])
    else:
        last_end = obj_start + 1
        indent = "    "
    rendered = json.dumps(new_value, ensure_ascii=False,
                          indent=4 if isinstance(new_value, dict) else None)
    if isinstance(new_value, dict):
        rendered = rendered.replace("\n", "\n" + indent)
    body = f",\n{indent}{json.dumps(parts[-1], ensure_ascii=False)}: {rendered}"
    return text[:last_end] + body + text[last_end:]


def _delete_member(text: str, parts: list[str]) -> str:
    """Remove one whole member, plus the comma that joined it to its siblings."""
    start = _walk_to_parent(text, parts)
    name_start, vend = _member_full_span(text, start, parts[-1])
    before = text.rfind(",", 0, name_start)
    line_start = text.rfind("\n", 0, name_start) + 1
    if before >= line_start - 1 and before != -1 and not text[before + 1:name_start].strip():
        cut_from = before                    # swallow the preceding comma
    else:
        cut_from = line_start                # first member: take the line
    cut_to = vend
    if text[cut_to:cut_to + 1] == ",":       # ...and the following one instead
        cut_to += 1
    tail = text[cut_to:]
    if cut_from == line_start and tail.startswith("\n"):
        cut_to += 1
    return text[:cut_from] + text[cut_to:]


# --------------------------------------------------------------------------
# Change application
# --------------------------------------------------------------------------

def _apply(cfg: dict, text: str, change: dict) -> tuple[dict, str]:
    """Apply one `{path, value?, action?}` to both the tree and the text."""
    path = change.get("path") or ""
    action = change.get("action", "set")
    _check_writable(path)
    parts = path.split(".")

    node = cfg
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"no such config path: {path!r} (stopped at {part!r})")
        node = node[part]
    leaf = parts[-1]
    if not isinstance(node, dict):
        raise KeyError(f"{'.'.join(parts[:-1])!r} is not an object")

    if action == "delete":
        if leaf not in node:
            raise KeyError(f"no such config key: {path!r}")
        del node[leaf]
        return cfg, _delete_member(text, parts)

    value = _coerce(change.get("value"))
    if leaf in node:
        node[leaf] = value
        return cfg, _rewrite_one_value(text, parts, value)
    if not change.get("create"):
        raise KeyError(f"no such config key: {path!r} -- pass create=True to add it")
    node[leaf] = value
    return cfg, _insert_member(text, parts, value)


def edit_impl(changes: list[dict], confirm: bool = False) -> dict:
    """Apply a list of changes atomically: all of them, or none.

    Atomic because a half-applied batch is worse than a refused one -- a
    parameter whose `source` was updated but whose `group` was not is a
    parameter that silently scores nothing.
    """
    if not changes:
        raise ValueError("no changes given")
    before_text = CONFIG_PATH.read_text(encoding="utf-8")
    cfg = json.loads(before_text)
    old = {}

    after_text = before_text
    for change in changes:
        path = change.get("path") or ""
        try:
            old[path] = copy.deepcopy(_resolve(cfg, path))
        except KeyError:
            old[path] = None
        cfg, after_text = _apply(cfg, after_text, change)

    # The surgery is text surgery, so prove it before offering to write it: the
    # rewritten file must parse to exactly the tree we validated.
    if json.loads(after_text) != cfg:
        raise ValueError("refusing to write: the edited text does not match the "
                         "intended config -- this is a bug in the text surgery")

    problems = validate(cfg)
    diff = list(difflib.unified_diff(
        before_text.splitlines(), after_text.splitlines(),
        fromfile="config.json", tofile="config.json (proposed)", lineterm="", n=2))

    out = {"changes": [{"path": c.get("path"),
                        "action": c.get("action", "set"),
                        "old": old.get(c.get("path")),
                        "new": (None if c.get("action") == "delete"
                                else _coerce(c.get("value")))}
                       for c in changes],
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
    # newline="" disables Windows' LF -> CRLF translation. Without it every
    # write rewrites all ~300 lines of a LF-committed file, which defeats the
    # entire point of the surgical edit above: `git diff` would show the whole
    # file changed and bury the one value that actually moved.
    backup.write_text(before_text, encoding="utf-8", newline="")
    CONFIG_PATH.write_text(after_text, encoding="utf-8", newline="")

    out["written"] = True
    out["backup"] = str(backup)
    return out


def set_impl(path: str, value, confirm: bool = False,
             create: bool = False) -> dict:
    result = edit_impl([{"path": path, "value": value, "create": create}],
                       confirm)
    one = result["changes"][0]
    return {"path": one["path"], "old": one["old"], "new": one["new"],
            **{k: v for k, v in result.items() if k != "changes"}}


def delete_impl(path: str, confirm: bool = False) -> dict:
    result = edit_impl([{"path": path, "action": "delete"}], confirm)
    one = result["changes"][0]
    return {"path": one["path"], "removed": one["old"],
            **{k: v for k, v in result.items() if k != "changes"}}


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def _type_name(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "null"


def _quality_params(cfg: dict) -> list[dict]:
    """Every quality parameter, enabled or not -- you cannot flip a switch you
    cannot see, and the disabled ones are exactly the ones worth listing."""
    out = []
    for key, spec in quality.parameters(cfg, enabled_only=False).items():
        out.append({
            "path": f"quality.parameters.{key}",
            "key": key,
            "label": quality.label_of(key, spec),
            "enabled": bool(spec.get("enabled", True)),
            "source": spec.get("source"),
            "group": spec.get("group"),
            "stage": spec.get("stage", quality.STAGE_FAST),
            "weight": spec.get("weight", 1.0),
            "gate": spec.get("gate"),
            "score": spec.get("score"),
            "gates_badge": bool(spec.get("gate")),
            "scores": bool(spec.get("score")),
        })
    return out


def params_list_impl(section: str | None = None) -> dict:
    """Every tunable value, with its current setting and how to change it."""
    cfg = load_config()
    sweeps = cfg.get("tuning", {}).get("sweeps", {})
    out: dict = {}

    want = (lambda name: True) if not section else (lambda name: name == section)

    if want("quality"):
        sect = quality.section(cfg)
        out["quality"] = {
            "enabled": bool(sect.get("enabled")),
            "badge": sect.get("badge"),
            "tiers": sect.get("tiers"),
            "groups": [{"path": f"quality.groups.{name}", "name": name,
                        "enabled": bool(spec.get("enabled", True)),
                        "weight": spec.get("weight")}
                       for name, spec in (sect.get("groups") or {}).items()],
            "parameters": _quality_params(cfg),
            "note": ("`enabled: false` removes a parameter from the gate, the "
                     "score, Quality Missing and the embed fields. Weights "
                     "renormalize over what is left."),
        }

    for name, block in cfg.items():
        if name in ("quality", "discord") or not isinstance(block, dict):
            continue
        if not (name.endswith(WRITABLE_SUFFIX)
                or name in ("charts", "backtest", "portfolio", "ibkr", "data",
                            "enrichment")):
            continue
        if not want(name):
            continue
        entries = []
        for key, value in block.items():
            if isinstance(value, dict):
                continue                     # nested blocks: read with config_get
            entry = {"path": f"{name}.{key}", "key": key, "value": value,
                     "type": _type_name(value)}
            grid = (sweeps.get(name) or {}).get(key)
            if grid is not None:
                entry["sweep"] = grid
                entry["hint"] = STRATEGY_HINT
            entries.append(entry)
        out[name] = {"parameters": entries}

    if section and not out:
        raise KeyError(f"no tunable section named {section!r} -- try one of "
                       f"{sorted(k for k in cfg if k != 'discord')}")
    out["_writable"] = list(WRITABLE) + [f"*{WRITABLE_SUFFIX}"]
    out["_forbidden"] = list(FORBIDDEN)
    return out


# --------------------------------------------------------------------------

def register(mcp) -> None:
    @mcp.tool()
    def params_list(section: str | None = None) -> dict:
        """Every tunable parameter, its current value, and the path to change it.

        Read-only. Start here rather than reading config.json: it resolves the
        quality registry (including the parameters that are switched **off**,
        which are the ones you usually want), and annotates each strategy value
        with its `tuning.sweeps` grid where one exists.

        `section` narrows it to one block ('quality', 'breakout_strategy',
        'portfolio', ...). Omit it for everything.
        """
        return params_list_impl(section)

    @mcp.tool()
    def config_set(path: str, value: Any, confirm: bool = False,
                   create: bool = False) -> dict:
        """Change one config.json value, with a diff preview and a backup.

        Returns the unified diff and writes nothing unless `confirm=True`.
        Backs up to `output/config_backups/` before every write. Only the one
        value is rewritten -- the file's hand-maintained formatting is left
        byte-for-byte alone, so the diff shows your change and nothing else.

        `value` keeps its JSON type: send 140, true or [10, 30, 60], not
        "140". A string that parses as JSON is converted; one that does not
        ("next_open") is stored as the string it is.

        `create=True` adds a key that does not exist yet -- that is how you add
        a new quality parameter or a threshold to a new screen. Without it an
        unknown path is an error, so a typo cannot quietly create a setting
        nothing reads.

        Writable: the strategy sections (anything named `*_strategy`), plus
        quality, ibkr, research, backtest, tuning, charts, portfolio,
        enrichment and data.
        `discord.*` and `research.auto.discord_send` are refused outright --
        the webhook is live, and the latter is what gates every Discord send.

        Validates before writing that every enabled screen's lookback still
        fits inside `data.download_period`, and that the quality registry still
        makes sense (known source, real group, gate keywords the engine
        implements, positive group weights). Each of those fails *silently* at
        runtime, which is why they are checked here.
        """
        return set_impl(path, value, confirm, create)

    @mcp.tool()
    def config_edit(changes: list[dict], confirm: bool = False) -> dict:
        """Apply several config changes at once, atomically, under one diff.

        `changes` is a list of `{"path": ..., "value": ...}`; add
        `"create": true` to add a new key, or `"action": "delete"` to remove
        one. All of them apply or none do, and the whole batch is validated as
        a unit -- which matters because a parameter is only coherent as a set
        (a `source` updated without its `group` scores nothing).

        Same guarantees as `config_set`: preview by default, one backup, and
        the file's formatting untouched.
        """
        return edit_impl(changes, confirm)

    @mcp.tool()
    def config_delete(path: str, confirm: bool = False) -> dict:
        """Remove one key from config.json, with a diff preview and a backup.

        Previews by default. Prefer switching a quality parameter off
        (`quality.parameters.<key>.enabled = false`) over deleting it: a
        disabled parameter is inert but still documented, and tier 4's frozen
        `Quality Rules` keeps grading past positions against the set that was
        in force when they were opened either way.
        """
        return delete_impl(path, confirm)

    @mcp.tool()
    def config_get(path: str | None = None, reveal: bool = False) -> dict:
        """Read config.json, or one dotted path within it.

        The Discord webhook is redacted unless `reveal=True`; it is a live URL
        in a committed file, so never paste it into output that leaves this
        machine. Examples: 'breakout_strategy', 'quality.parameters.roe'.
        """
        return get_impl(path, reveal)
