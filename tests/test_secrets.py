"""Offline -- the Discord webhook comes from `.env`, never from `config.json`.

`config.json` is tracked, and it is tracked on purpose: it is 1400 lines of
tuned parameters, the substance of the project, and every figure on a card can
be traced back to it. A credential sitting in that file gets committed with
them -- which is exactly what happened here, and why the webhook that was live
until this change sits in 67 of the repo's commits.

So the rule is structural rather than remembered: secrets resolve from the
environment or an untracked `.env`, and `config.json` carries an empty string.
This file is what stops one being pasted back in. The first check is the one
that matters -- everything else pins the mechanism that makes it possible to
keep.
"""

import io
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from _harness import Checks

import scanner_common

c = Checks("secrets stay out of the tracked config")

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
c.section("nothing credential-shaped is in the tracked config")

config_text = io.open(ROOT / "config.json", encoding="utf-8").read()
c.ok("no Discord webhook URL in config.json",
     "api/webhooks" not in config_text)
c.ok("discord.webhook_url is empty",
     json.loads(config_text)["discord"]["webhook_url"] == "",
     "it resolves from STOCK_ANALYZER_DISCORD_WEBHOOK at load time")

# A webhook is the only secret today; the guard is written against the table so
# it keeps holding when the SEC contact email or anything else joins it.
for dotted in scanner_common.SECRET_ENV:
    section, _, key = dotted.rpartition(".")
    value = json.loads(config_text)
    for part in section.split("."):
        value = value.get(part, {})
    c.ok(f"{dotted} carries no value in config.json",
         not value.get(key), f"got {value.get(key)!r}")

# --------------------------------------------------------------------------
c.section(".env is ignored, .env.example is not")

ignored = subprocess.run(["git", "check-ignore", "-q", ".env"],
                         cwd=ROOT).returncode == 0
c.ok(".env is matched by .gitignore", ignored,
     "without this the next run of the scan commits the webhook back")
c.ok(".env.example exists", (ROOT / ".env.example").exists(),
     "the template is how a fresh clone learns the variable name")
example = io.open(ROOT / ".env.example", encoding="utf-8").read()
for var in scanner_common.SECRET_ENV.values():
    c.ok(f"{var} is named in .env.example", var in example)
c.ok(".env.example holds no value itself",
     not re.search(r"^[A-Z_]+=\S", example, re.M))

# --------------------------------------------------------------------------
c.section("_dotenv_values never raises")

c.ok("a missing file is {}",
     scanner_common._dotenv_values(ROOT / "does_not_exist.env") == {})
c.ok("a directory is {} rather than an error",
     scanner_common._dotenv_values(ROOT) == {})

tmp = Path(tempfile.mkdtemp(prefix="test_secrets_")) / ".env"
tmp.write_text(
    "\n".join([
        "# a comment",
        "",
        "   ",
        "PLAIN=value",
        "  SPACED  =  padded  ",
        'QUOTED="in quotes"',
        "SINGLE='in singles'",
        "no_equals_sign_at_all",
        "=novalue",
        "EMPTY=",
    ]),
    encoding="utf-8")
parsed = scanner_common._dotenv_values(tmp)
c.ok("a plain pair parses", parsed.get("PLAIN") == "value")
c.ok("surrounding whitespace is stripped", parsed.get("SPACED") == "padded")
c.ok("double quotes are stripped", parsed.get("QUOTED") == "in quotes")
c.ok("single quotes are stripped", parsed.get("SINGLE") == "in singles")
c.ok("an empty value survives as ''", parsed.get("EMPTY") == "")
c.ok("comments, blanks and malformed lines are dropped",
     set(parsed) == {"PLAIN", "SPACED", "QUOTED", "SINGLE", "EMPTY"},
     str(sorted(parsed)))

# --------------------------------------------------------------------------
c.section("precedence: environ (if present) -> .env -> config.json")

VAR = scanner_common.SECRET_ENV["discord.webhook_url"]
tmp.write_text(f"{VAR}=from-dotenv\n", encoding="utf-8")
base = {"discord": {"webhook_url": "from-config"}}
saved = os.environ.get(VAR)


def resolved(env, cfg=None, path=tmp):
    """_resolve_secrets under a chosen environment, restored afterwards."""
    if env is None:
        os.environ.pop(VAR, None)
    else:
        os.environ[VAR] = env
    try:
        return scanner_common._resolve_secrets(
            json.loads(json.dumps(cfg if cfg is not None else base)), path
        )["discord"]["webhook_url"]
    finally:
        if saved is None:
            os.environ.pop(VAR, None)
        else:
            os.environ[VAR] = saved


c.ok("a set variable beats .env", resolved("from-environ") == "from-environ")
c.ok(".env beats config.json", resolved(None) == "from-dotenv")
c.ok("config.json stands when neither supplies one",
     resolved(None, path=ROOT / "does_not_exist.env") == "from-config")

# The harness exports every secret as "" precisely so this path exists: an
# empty variable is *present*, so it wins, and nothing in the suite can post.
c.ok("an empty variable forces empty rather than falling through",
     resolved("") == "",
     "this is the kill-switch tests/_harness.py relies on")

# --------------------------------------------------------------------------
c.section("the SEC contact has one reader and one fallback")

# Two endpoints need this header -- data.sec.gov and efts.sec.gov -- and each
# used to carry its own copy of the fallback string. Two copies that must stay
# byte-identical is the failure this project has already paid for once.
sec_src = io.open(ROOT / "sec.py", encoding="utf-8").read()
news_src = io.open(ROOT / "newsfeed.py", encoding="utf-8").read()
for name, text in (("sec.py", sec_src), ("newsfeed.py", news_src)):
    c.ok(f"{name} resolves the UA through scanner_common",
         "sec_user_agent(cfg)" in text)
    c.ok(f"{name} keeps no fallback of its own",
         "research@example.com" not in text)

c.ok("no personal address survives in the tracked config",
     "@" not in json.loads(config_text)["research"]["sec"]["user_agent"])

import sec          # noqa: E402  -- imported here so the checks above run first
import newsfeed     # noqa: E402

probe = {"research": {"sec": {"user_agent": "probe/1.0 (a@b.test)"}}}
c.ok("a configured contact reaches both callers",
     sec._headers(probe)["User-Agent"]
     == newsfeed._headers(probe)["User-Agent"]
     == "probe/1.0 (a@b.test)")
c.ok("an unset contact falls back rather than sending nothing",
     scanner_common.sec_user_agent({}) == scanner_common.SEC_UA_FALLBACK,
     "efts.sec.gov answers 403 to a request with no User-Agent")
c.ok("the fallback is visibly a placeholder",
     "example.com" in scanner_common.SEC_UA_FALLBACK)
c.ok("whitespace-only is treated as unset",
     scanner_common.sec_user_agent(
         {"research": {"sec": {"user_agent": "   "}}})
     == scanner_common.SEC_UA_FALLBACK)

# A value with spaces and parentheses is the *normal* shape here, and it is
# exactly what a naive KEY=value split would mangle.
tmp.write_text("STOCK_ANALYZER_SEC_USER_AGENT="
               "stock-analyzer/1.0 (me@x.test)" + chr(10),
               encoding="utf-8")
c.ok("a UA with spaces and parens round-trips through .env",
     scanner_common._dotenv_values(tmp).get("STOCK_ANALYZER_SEC_USER_AGENT")
     == "stock-analyzer/1.0 (me@x.test)")

# --------------------------------------------------------------------------
c.section("the harness kill-switch is actually armed")

c.ok("every secret variable is set for this process",
     all(v in os.environ for v in scanner_common.SECRET_ENV.values()))
c.ok("the webhook this test process would load is empty",
     scanner_common.load_config()["discord"]["webhook_url"] == "",
     "so no test can reach a live channel even with a real .env present")

raise SystemExit(c.finish())
