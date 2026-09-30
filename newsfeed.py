"""Dated, sourced news events per configured theme -- the thematic screen's evidence base.

This is the first module here that fetches general web content, so it is worth
saying plainly why that is not a hole in the AI boundary `AI_ROLE.md` defends.
It pulls **facts on a schedule**, exactly as the Yahoo and EDGAR collectors do:
a headline, its publisher, its date and its link. It forms no opinion, ranks
nothing, and names no company. The judgment -- *who benefits from this event* --
happens in a session, over the bundle `mcp_tools/themes.py` assembles, and lands
as a categorical record that tier 4 grades (`theme_signals.py`).

The split matters because it is what makes a recorded pick reviewable. Without
it the session would search blind and the only trace of *what it was looking at*
would be prose it wrote afterwards.

**The cache here is not that record.** `_save_cache` replaces a theme's entry
wholesale on every refresh, so reading a three-week-old pick back through the
cache shows today's headlines -- auditable-looking without being auditable.
`theme_signals.snapshot_evidence` freezes the events at record time into
`events_<theme>_<scan_date>.json` and the row points at it; that file is the
audit trail, and this cache is only a way to avoid refetching.

Sources were tested rather than assumed (2026-08-14):

  * **Google News RSS** -- works, no key, no auth. The primary feed.
  * **SEC EDGAR full-text search** -- `efts.sec.gov`, free, reuses `sec.py`'s
    contact User-Agent. Rejects a request without one.
  * Reddit, StockTwits, X and Facebook are all unreachable (blocked, 403, or
    paid-tier only), and Google Trends publishes no official API. This is a
    news-and-filings screen, not a social-sentiment one, and the skill says so
    rather than implying a sentiment read that never happened.

Fail-open throughout, like every other collector: an unreachable feed logs a
`THEME warn` and returns `[]`. A dead feed must cost you that theme's events and
nothing else.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

import requests

from scanner_common import log_step, output_dir, sec_user_agent

CONFIG_KEY = "theme_screen"

DEFAULT_FEED = ("https://news.google.com/rss/search"
                "?q={query}&hl=en-US&gl=US&ceid=US:en")

# SEC's full-text search endpoint. Documented as covering 2001-present.
SEC_FULLTEXT = "https://efts.sec.gov/LATEST/search-index"

CACHE_NAME = "news_cache.json"


# --------------------------------------------------------------------------
# Config and paths
# --------------------------------------------------------------------------

def section(cfg: dict) -> dict:
    return cfg.get(CONFIG_KEY) or {}


def is_enabled(cfg: dict) -> bool:
    return bool(section(cfg).get("enabled", True))


def themes_dir(cfg: dict, create: bool = True) -> Path:
    """Where the theme record and its news cache live (`theme_screen.dir`).

    Its own directory rather than a file in `history/`, for the reason
    `enrichment_dir` gives: history records what the *scan* saw and is never
    revised, while this is written by something else entirely and rewritten
    whenever the feed is refreshed.
    """
    path = Path(section(cfg).get("dir", "themes"))
    if not path.is_absolute():
        path = output_dir(create) / path
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def themes(cfg: dict) -> list[dict]:
    """The configured themes, each `{name, query, chain}`."""
    out = []
    for entry in section(cfg).get("themes") or []:
        if isinstance(entry, dict) and entry.get("name"):
            out.append(entry)
    return out


def theme_names(cfg: dict) -> list[str]:
    return [t["name"] for t in themes(cfg)]


def theme_of(name: str, cfg: dict) -> dict:
    for entry in themes(cfg):
        if entry["name"] == name:
            return entry
    return {}


def chain_of(name: str, cfg: dict) -> list[str]:
    """The beneficiary-chain template for one theme, `[]` when unknown.

    The chain is the whole method: an event names an operator, and the tiers
    behind it -- engineering, equipment, power, materials -- are where the names
    nobody has bid up yet actually sit.
    """
    return list(theme_of(name, cfg).get("chain") or [])


# --------------------------------------------------------------------------
# The cache -- per theme, never whole-file
# --------------------------------------------------------------------------
# Same reasoning as `universe_scan.is_stale`: this is N independent fetches
# where any one can fail, so a single whole-file timestamp would let one dead
# feed invalidate every other theme's perfectly good events.

def _cache_path(cfg: dict, create: bool = True) -> Path:
    return themes_dir(cfg, create) / CACHE_NAME


def _load_cache(cfg: dict) -> dict:
    path = _cache_path(cfg, create=False)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        # An unreadable cache is an absent one -- never fatal.
        return {}


def _save_cache(cache: dict, cfg: dict) -> None:
    try:
        _cache_path(cfg).write_text(
            json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - a lost cache costs a refetch
        log_step("THEME", "warn", f"could not write news cache: {exc}", cfg=cfg)


def _is_fresh(entry: dict, max_age_hours: float) -> bool:
    stamp = (entry or {}).get("fetched_at")
    if not stamp:
        return False
    try:
        fetched = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - fetched
    return age <= timedelta(hours=max_age_hours)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def _headers(cfg: dict) -> dict:
    """Reuse SEC's configured contact User-Agent.

    Not politeness for its own sake: `efts.sec.gov` returns 403 to a request
    without one, which is exactly how this endpoint fails when it is called from
    a generic fetcher.
    """
    return {"User-Agent": sec_user_agent(cfg),
            "Accept-Encoding": "gzip, deflate"}


def _text(node, tag: str) -> str:
    found = node.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _published(node) -> str:
    """`pubDate` as an ISO date string, or "" when it will not parse."""
    raw = _text(node, "pubDate")
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return raw


def parse_rss(xml_text: str, theme: str = "") -> list[dict]:
    """RSS <item>s as plain dicts. Separate from the fetch so it is testable."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    items = []
    for node in root.iter("item"):
        source = node.find("source")
        items.append({
            "theme": theme,
            "title": _text(node, "title"),
            "link": _text(node, "link"),
            "published": _published(node),
            "source": ((source.text or "").strip()
                       if source is not None else ""),
        })
    return items


# --------------------------------------------------------------------------
# Ranking -- magnitude and duplicate coverage
# --------------------------------------------------------------------------
# The raw feed is date-ordered and undeduplicated, so an op-ed outranks a $50bn
# commitment and one event arrives once per outlet that covered it. Both of
# those are fixable in Python from the titles alone, and both matter: size is
# the best single proxy for whether an event is worth tracing, and the number
# of outlets covering it is exactly the corroboration `min_sources` asks for.

_MONEY = re.compile(
    r"(?P<cur>US\$|C\$|A\$|\$|€|£)\s?(?P<num>[\d][\d,]*(?:\.\d+)?)\s*"
    r"(?P<scale>trillion|billion|bn\b|million|mn\b|m\b)", re.I)

_SCALES = {"trillion": 1e12, "billion": 1e9, "bn": 1e9,
           "million": 1e6, "mn": 1e6, "m": 1e6}

# Words that carry no distinguishing signal when comparing two headlines.
_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "with", "at",
         "its", "is", "are", "as", "by", "new", "s", "will", "said", "says",
         "from", "that", "this", "it", "be", "has", "have", "after", "over"}


def _strip_outlet(title: str) -> str:
    """Google News appends ` - Publisher`; it is not part of the headline."""
    return title.rsplit(" - ", 1)[0].strip() if " - " in title else title.strip()


def magnitude(title: str) -> dict:
    """The largest money amount in a headline, or `{}`.

    **No FX conversion.** `C$13 billion` is recorded as 13e9 CAD, not silently
    turned into USD -- there is no rate source here, and a wrong number that
    looks right is the failure mode this project spends most of its rules on.
    Ranking across currencies at this granularity is close enough to order
    events by size; anything finer needs a rate and should say so.
    """
    best = None
    for match in _MONEY.finditer(title):
        try:
            value = float(match.group("num").replace(",", ""))
        except ValueError:
            continue
        scale = _SCALES.get(match.group("scale").lower().rstrip("."), 1.0)
        amount = value * scale
        if best is None or amount > best["magnitude"]:
            best = {"magnitude": amount,
                    "magnitude_currency": match.group("cur").upper(),
                    "magnitude_text": match.group(0)}
    return best or {}


# US/UK spellings that matter to these themes. Outlets split on them -- the same
# Ontario announcement was filed as "data center" and "data centre" on the same
# day -- and an unnormalized token set treats those as different words.
_SPELLING = {"centre": "center", "centres": "centers", "defence": "defense",
             "programme": "program", "fibre": "fiber", "aluminium": "aluminum",
             "licence": "license", "organisation": "organization"}


def _tokens(title: str) -> set:
    words = re.findall(r"[a-z0-9]+", _strip_outlet(title).lower())
    return {_SPELLING.get(w, w) for w in words
            if w not in _STOP and len(w) > 1}


def _overlap(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _same_amount(a: dict, b: dict, tol: float = 0.05) -> bool:
    """Two headlines quoting the same figure, within a rounding tolerance.

    Outlets round the same commitment differently -- `$38 billion` and
    `$38.4 Billion` are one announcement -- so an exact match would miss most
    real duplicates. Currency must agree: `C$13bn` and `$13bn` are not the same
    number, and nothing here converts between them.
    """
    x, y = a.get("magnitude"), b.get("magnitude")
    if not x or not y:
        return False
    if a.get("magnitude_currency") != b.get("magnitude_currency"):
        return False
    return abs(x - y) / max(x, y) <= tol


def _similar(item: dict, cluster: dict, threshold: float) -> bool:
    """Whether a headline belongs to an existing cluster.

    Two routes, because one is not enough on real headlines. Wording alone
    (Jaccard over content words) misses duplicates that outlets rewrote --
    measured 2026-08-14, three separate reports of SK Hynix's $38bn fab
    commitment shared under a third of their words and stayed three "events".
    So a matching money figure is the second route, and it is a strong one: two
    headlines quoting the same amount in the same currency with even loose
    wording overlap are almost always the same announcement.

    The amount route still demands real overlap (`threshold / 2`), or every
    unrelated "$1 billion" story in a theme would collapse into one.

    And a *disagreeing* amount vetoes the merge outright, wording route
    included: "$13 billion Texas data center" and "C$13 billion Alberta data
    center" share almost every word but are two different projects, and merging
    them would both hide one event and overstate the other's corroboration.
    Only applied when both carry a figure -- one silent headline says nothing
    about the other.
    """
    overlap = _overlap(item["_tokens"], cluster["_tokens"])
    if item.get("magnitude") and cluster.get("magnitude"):
        return _same_amount(item, cluster) and overlap >= threshold / 2
    return overlap >= threshold


def cluster_events(items: list[dict], threshold: float = 0.5) -> list[dict]:
    """Group headlines describing the same event, newest and biggest first.

    Greedy single-pass clustering on Jaccard overlap of the headline's
    content words. Deliberately conservative -- a high threshold merges the
    obvious duplicates ("Meta's Louisiana data center investment to reach $50
    billion" across four outlets) and leaves anything doubtful separate.
    Over-merging is the dangerous direction: it would hide a distinct event
    *and* inflate the corroboration count of the one it merged into.

    Each cluster carries `sources_n`, which is the corroboration `min_sources`
    is asking about -- an event two outlets carried independently is a
    different thing from one blog post.
    """
    clusters: list[dict] = []
    for item in sorted(items, key=lambda i: i.get("published") or "",
                       reverse=True):
        title = item.get("title") or ""
        candidate = {"_tokens": _tokens(title), **magnitude(title)}
        for cluster in clusters:
            if _similar(candidate, cluster, threshold):
                cluster["sources"].append(item.get("source") or "")
                cluster["links"].append(item.get("link") or "")
                # Keep the largest amount seen anywhere in the cluster: outlets
                # routinely report the same commitment with and without it, and
                # the fuller figure is the one worth ranking on.
                if candidate.get("magnitude", 0) > cluster.get("magnitude", 0):
                    cluster.update({k: v for k, v in candidate.items()
                                    if k != "_tokens"})
                # Widen the cluster's vocabulary so a third rewording of the
                # same story still matches, even if it shares little with the
                # headline that happened to arrive first.
                cluster["_tokens"] = cluster["_tokens"] | candidate["_tokens"]
                break
        else:
            clusters.append({
                "theme": item.get("theme", ""),
                "title": _strip_outlet(title),
                "published": item.get("published") or "",
                "sources": [item.get("source") or ""],
                "links": [item.get("link") or ""],
                **candidate,
            })

    for cluster in clusters:
        cluster.pop("_tokens", None)
        cluster["sources"] = sorted({s for s in cluster["sources"] if s})
        cluster["sources_n"] = len(cluster["sources"])

    # Size first, then corroboration, then recency. Stated rather than tuned:
    # an event with a number attached is the one worth tracing, and the ordering
    # is what the session reads top-down.
    clusters.sort(key=lambda c: (c.get("magnitude", 0), c["sources_n"],
                                 c["published"]), reverse=True)
    return clusters


def fetch(theme: str, cfg: dict, refresh: bool = False) -> list[dict]:
    """Recent news items for one configured theme. `[]` on any failure.

    Cached per theme; `refresh` ignores the cache for this theme only.
    """
    entry = theme_of(theme, cfg)
    query = entry.get("query")
    if not query:
        log_step("THEME", "skip", f"no query configured for theme {theme!r}",
                 cfg=cfg)
        return []

    cache = _load_cache(cfg)
    max_age = float(section(cfg).get("news_cache_hours", 6))
    if not refresh and _is_fresh(cache.get(theme), max_age):
        return list(cache[theme].get("items") or [])

    url = (section(cfg).get("news_feed") or DEFAULT_FEED).format(
        query=requests.utils.quote(query))
    try:
        response = requests.get(url, headers=_headers(cfg), timeout=30)
        response.raise_for_status()
        items = parse_rss(response.text, theme)
    except Exception as exc:  # noqa: BLE001 - a dead feed costs one theme
        log_step("THEME", "warn", f"{theme}: news feed unreachable ({exc}) -- "
                                  "no events for this theme", cfg=cfg)
        # Serve stale rather than nothing: a week-old event is still an event.
        return list((cache.get(theme) or {}).get("items") or [])

    cache[theme] = {"fetched_at": datetime.now(timezone.utc).isoformat(),
                    "query": query, "items": items}
    _save_cache(cache, cfg)
    log_step("THEME", "ok", f"{theme}: {len(items)} news item(s)", cfg=cfg)
    return items


def recent(cfg: dict, names: list[str] | None = None, days: int | None = None,
           refresh: bool = False) -> list[dict]:
    """Every configured theme's events inside `days`, newest first.

    An item whose date will not parse is **kept**, not dropped: an undated
    headline is still evidence, and silently discarding it would make the feed
    look emptier than it is.
    """
    if days is None:
        days = int(section(cfg).get("lookback_days", 14))
    wanted = names or theme_names(cfg)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    out = []
    for name in wanted:
        for item in fetch(name, cfg, refresh=refresh):
            stamp = item.get("published") or ""
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                out.append(item)
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= cutoff:
                out.append(item)
    out.sort(key=lambda i: i.get("published") or "", reverse=True)
    return out


_TICKER_IN_NAME = re.compile(r"\(([A-Z][A-Z.\-]{0,6})(?:,[^)]*)?\)")


def _display_name(raw: str) -> tuple[str, str]:
    """Split EDGAR's `"NAME  (TICK, TICK-PA)  (CIK 0000…)"` into name + ticker.

    The ticker is the reason to call this at all -- a company *name* still has
    to be resolved to something tradeable, and EDGAR has already done it.
    """
    name = raw.split("  (")[0].strip()
    ticker = ""
    for match in _TICKER_IN_NAME.finditer(raw):
        candidate = match.group(1)
        if candidate != "CIK" and not candidate.startswith("CIK"):
            ticker = candidate
            break
    return name, ticker


def sec_fulltext(query: str, cfg: dict, forms: str = "8-K", limit: int = 20,
                 days: int | None = None, item: str = "") -> list[dict]:
    """EDGAR full-text search hits for a phrase. `[]` on any failure.

    Which companies are *telling the SEC* about a theme -- materially stronger
    than which ones a journalist associated with it, and it arrives with a
    ticker already attached.

    **The date window is not optional in practice.** EDGAR sorts by relevance,
    so an unbounded query happily returns 2002 and 2014 filings for a query
    about this month's news; `startdt`/`enddt` are what make the result mean
    "who is filing about this *now*".

    **`item` is what makes the result mean something material.** A bare phrase
    matches any mention, and micro-caps mention a theme promotionally far more
    often than large filers mention one materially -- measured 2026-08-14, a
    bare `"data center"` search returned Riot, Workhorse and CleanCore, while
    adding `Item 1.01` (entry into a material definitive agreement, i.e. "we
    signed something") surfaced Prologis. The bias is systematic, not bad luck,
    and this argument is the only thing in the collection layer that corrects
    for it.

    Optional by design: it needs the contact User-Agent above (without one the
    endpoint 403s), and losing it costs one corroboration source and nothing
    structural.
    """
    if days is None:
        days = int(section(cfg).get("lookback_days", 14))
    today = datetime.now(timezone.utc).date()
    phrase = f'"{query}"'
    if item:
        phrase = f'"Item {item}" {phrase}'
    params = {"q": phrase, "forms": forms,
              "startdt": (today - timedelta(days=days)).isoformat(),
              "enddt": today.isoformat()}
    try:
        response = requests.get(SEC_FULLTEXT, params=params,
                                headers=_headers(cfg), timeout=30)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - corroboration is optional
        log_step("THEME", "warn", f"EDGAR full-text search unavailable: {exc}",
                 cfg=cfg)
        return []

    hits = ((payload.get("hits") or {}).get("hits") or [])[:limit]
    out = []
    for hit in hits:
        source = hit.get("_source") or {}
        names = source.get("display_names") or []
        roots = source.get("root_forms") or []
        company, ticker = _display_name(names[0] if names else "")
        out.append({
            "company": company,
            "ticker": ticker,
            "form": (roots[0] if roots else source.get("file_type") or ""),
            "filed": source.get("file_date") or "",
            "adsh": source.get("adsh") or "",
        })
    return out


# --------------------------------------------------------------------------
# CLI -- a look at the evidence base without recording anything
# --------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    from scanner_common import enable_utf8_output, load_config

    enable_utf8_output()
    cfg = load_config()
    names = [a for a in argv if not a.startswith("-")] or None
    events = recent(cfg, names, refresh="--refresh" in argv)
    if not events:
        print("No events. Check `theme_screen.themes` in config.json, "
              "or the feed may be unreachable (see the step log).")
        return 1
    for item in events:
        print(f"{(item.get('published') or '')[:10]}  "
              f"{item.get('theme', ''):<12.12s} "
              f"{(item.get('source') or '')[:22]:<22.22s} "
              f"{item.get('title', '')}")
    print(f"\n{len(events)} event(s) across "
          f"{len(set(i.get('theme') for i in events))} theme(s).")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
