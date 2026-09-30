"""
SEC EDGAR -- 10-Q / 10-K filings collector (free, no API key).

`fetch_filing_sections(ticker)` returns the latest 10-Q and 10-K with their
narrative sections (MD&A, Risk Factors, Business) extracted as plain text, plus a
curated set of as-reported XBRL financial concepts. Claude analyzes the text
in-session for the deep-dive synthesis (management commentary -> earnings section;
risk factors -> bear case; business -> moat; XBRL -> a cross-check to Yahoo).

Everything is n/a-tolerant: a missing CIK, an unparseable filing, or any request
error returns None / omits the piece rather than raising -- the same rule as the
rest of the pipeline. SEC requires a User-Agent containing a contact email
(config `research.sec.user_agent`); their fair-access limit (~10 req/s) is far
above what a few deep-dives need.
"""

import re
import time
from datetime import date

import requests
from lxml import html as lxml_html

from scanner_common import fmt_bytes, load_config, log_step, sec_user_agent

_TICKER_MAP = None  # {TICKER -> zero-padded CIK}, cached per process
_SUBMISSIONS = {}   # cik -> the `filings.recent` dict, cached per process
_DOC_TEXT = {}      # (cik, accession) -> cleaned document text

# One run is one point in time, so a filing fetched for the narrative sections
# and a filing scanned for distress flags are the same bytes. These caches are
# what stop `flags()` doubling the EDGAR traffic of every deep-dive.


def _headers(cfg: dict) -> dict:
    return {"User-Agent": sec_user_agent(cfg),
            "Accept-Encoding": "gzip, deflate"}


def _get(url: str, cfg: dict) -> requests.Response:
    r = requests.get(url, headers=_headers(cfg), timeout=30)
    r.raise_for_status()
    return r


def ticker_to_cik(ticker: str, cfg: dict) -> str | None:
    """Map a ticker to its zero-padded 10-digit CIK via company_tickers.json."""
    global _TICKER_MAP
    if _TICKER_MAP is None:
        data = _get("https://www.sec.gov/files/company_tickers.json", cfg).json()
        _TICKER_MAP = {str(row["ticker"]).upper(): f"{int(row['cik_str']):010d}"
                       for row in data.values()}
    t = ticker.upper()
    for cand in (t, t.replace("-", "."), t.replace(".", "-"),
                 t.replace("-", ""), t.replace(".", "")):
        if cand in _TICKER_MAP:
            return _TICKER_MAP[cand]
    return None


def _recent(cik: str, cfg: dict) -> dict:
    """The submissions `filings.recent` arrays for one CIK, cached per process.

    Read by both `_pick_latest` (which filing to parse) and `flags` (which
    forms and 8-K item codes appeared recently), so it is fetched once.
    """
    if cik not in _SUBMISSIONS:
        _SUBMISSIONS[cik] = (_get(f"https://data.sec.gov/submissions/CIK{cik}.json", cfg)
                             .json().get("filings", {}).get("recent", {}))
    return _SUBMISSIONS[cik]


def _document_text(cik: str, meta: dict, cfg: dict) -> tuple[str, int]:
    """The cleaned text of one filing's primary document, cached per process."""
    key = (cik, meta["accession"])
    if key not in _DOC_TEXT:
        resp = _get(_doc_url(cik, meta["accession"], meta["primary_doc"]), cfg)
        _DOC_TEXT[key] = (_clean_text(resp.content), len(resp.content))
    return _DOC_TEXT[key]


def _pick_latest(cik: str, cfg: dict, forms: list[str]) -> dict:
    """Most recent filing per requested form (recent arrays are newest-first)."""
    recent = _recent(cik, cfg)
    report_dates = recent.get("reportDate", [])
    out = {}
    for i, form in enumerate(recent.get("form", [])):
        if form in forms and form not in out:
            out[form] = {
                "accession": recent["accessionNumber"][i],
                "primary_doc": recent["primaryDocument"][i],
                "filing_date": recent["filingDate"][i],
                "report_date": report_dates[i] if i < len(report_dates) else None,
            }
        if all(f in out for f in forms):
            break
    return out


def _doc_url(cik: str, accession: str, primary_doc: str) -> str:
    return (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession.replace('-', '')}/{primary_doc}")


def _clean_text(content: bytes) -> str:
    doc = lxml_html.fromstring(content)
    for bad in doc.xpath("//script | //style"):
        bad.getparent().remove(bad)
    text = doc.text_content().replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text


def _extract_between(text: str, start_pat: str, end_pats: list[str],
                     max_chars: int) -> str | None:
    """Slice from a section's title-bearing header to the next section's header.

    Two robustness tricks: the `start_pat` includes the section TITLE, so
    in-text cross-references ("Item 1A of this Form 10-K ...") don't match --
    only the table-of-contents line and the real header do; and the end
    boundary is the *next section's* title-bearing header, not any "Item N"
    mention (filings quote item numbers constantly). The TOC copy yields a tiny
    slice, so taking the LONGEST candidate lands on the real section.
    """
    joined = "|".join(f"(?:{p})" for p in end_pats)
    ends = [m.start() for m in re.finditer(joined, text, re.I)] if end_pats else []
    best = ""
    for m in re.finditer(start_pat, text, re.I):
        s = m.start()
        later = [e for e in ends if e > s + 80]
        end = later[0] if later else min(len(text), s + max_chars)
        chunk = text[s:end]
        if len(chunk) > len(best):
            best = chunk
    best = best.strip()
    return best[:max_chars] if len(best) > 400 else None


def _xbrl_facts(cik: str, cfg: dict) -> dict:
    """Latest value of each curated us-gaap concept from companyfacts."""
    concepts = cfg.get("research", {}).get("sec", {}).get("xbrl_concepts", [])
    if not concepts:
        return {}
    try:
        gaap = (_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", cfg)
                .json().get("facts", {}).get("us-gaap", {}))
    except Exception as exc:  # noqa: BLE001
        log_step("SEC", "failed", f"xbrl: {exc}")
        return {}
    out = {}
    for c in concepts:
        units = (gaap.get(c) or {}).get("units", {})
        series = units.get("USD") or next(iter(units.values()), [])
        if not series:
            continue
        latest = max(series, key=lambda x: x.get("end", ""))
        out[c] = {"val": latest.get("val"), "end": latest.get("end"),
                  "form": latest.get("form"), "unit": "USD" if "USD" in units else ""}
    log_step("SEC", "ok", f"xbrl {len(out)}/{len(concepts)} concepts")
    return out


def _sections_summary(sections: dict) -> str:
    """`mdna 24000 / risk_factors n-a` -- which sections the parse actually got.

    Worth a log line of its own: a section coming back n/a is silent today, and
    it is exactly what makes a report thinner (the RL run built its bear case
    without Item 1A and only said so in prose).
    """
    return " / ".join(f"{name} {len(text)}" if text else f"{name} n-a"
                      for name, text in sections.items())


# --------------------------------------------------------------------------
# Distress flags -- the `sec_flags` resolver of the quality registry
# --------------------------------------------------------------------------

# "substantial doubt ... going concern" is the auditor's phrase, but it appears
# in the *negative* far more often than the positive: a healthy filing states
# that management identified no conditions raising substantial doubt, and a
# recovering one states that its plans alleviate the doubt. A naive search for
# the phrase flags most of the S&P 500.
_GOING_CONCERN = re.compile(
    r"substantial\s+doubt.{0,240}?going\s+concern", re.I | re.S)

# Cues that turn a match into a denial or a resolution. Scanned in the ~200
# characters *before* the phrase and inside the match itself.
_NEGATIONS = re.compile(
    r"\b(no|not|non|never|without|alleviate[sd]?|alleviating|mitigate[sd]?|"
    r"resolved|no\s+longer|did\s+not|does\s+not|has\s+not|have\s+not|"
    r"were\s+not|was\s+not|absence\s+of)\b", re.I)

# 8-K item codes worth knowing about, and what each one means when it appears.
ITEM_MEANINGS = {
    "1.03": "bankruptcy or receivership",
    "2.04": "debt acceleration or covenant trigger",
    "3.01": "listing-rule non-compliance or delisting notice",
    "3.02": "unregistered sale of equity",
    "4.01": "change of certifying accountant",
    "4.02": "non-reliance on previously issued financials",
    "5.02": "departure of a director or principal officer",
}


def _within(dates: list, months: int) -> set:
    """Indices of filings made within the last `months`.

    EDGAR reports `filingDate` as an ISO date string, so the comparison is a
    plain string compare against the cutoff -- no parsing, and no timezone to
    get wrong.
    """
    if not dates:
        return set()
    today = date.today()
    total = today.month - 1 - months
    cutoff = date(today.year + total // 12, total % 12 + 1,
                  min(today.day, 28)).isoformat()
    return {i for i, d in enumerate(dates) if isinstance(d, str) and d >= cutoff}


def going_concern_hits(text: str) -> int:
    """How many *affirmative* going-concern statements this filing contains.

    Every match is checked against the negation cues around it, because the
    boilerplate denial ("no conditions or events were identified that raise
    substantial doubt about our ability to continue as a going concern") is far
    more common than the real thing. A remaining hit is still only a hint --
    which is why the deep-dive pass is asked to corroborate it from the text
    rather than the veto being the last word.
    """
    hits = 0
    for match in _GOING_CONCERN.finditer(text or ""):
        window = text[max(0, match.start() - 200):match.end()]
        if not _NEGATIONS.search(window):
            hits += 1
    return hits


def flags(ticker: str, cfg: dict | None = None) -> dict:
    """Filing-derived distress flags for one ticker, as `int` 0/1 or counts.

    Every value is an `int`, never a `bool`: `quality.scalar` rejects booleans
    and would read one as a missing value, which for a veto parameter means "no
    veto" -- a flag that looks right in config and never fires.

    Returns `{}` on any failure, and that is the whole safety story for this
    resolver. A missing value never trips a veto (`quality.veto_failures`), so
    an EDGAR outage, a renamed form or an unparseable document can only make
    the layer quieter. It fails open on purpose: excluding a company because
    the SEC was briefly unreachable would be far worse than missing a flag.
    """
    cfg = cfg or load_config()
    sec = cfg.get("research", {}).get("sec", {})
    lookback = int(sec.get("flag_lookback_months", 12))
    restate_lookback = int(sec.get("restatement_lookback_months", 24))
    forms = sec.get("forms", ["10-Q", "10-K"])
    try:
        cik = ticker_to_cik(ticker, cfg)
        if not cik:
            log_step("SEC", "miss", f"no CIK for {ticker} -- no filing flags")
            return {}
        recent = _recent(cik, cfg)
        form_list = recent.get("form", [])
        items_list = recent.get("items", [])
        dates = recent.get("filingDate", [])

        window = _within(dates, lookback)
        restate_window = _within(dates, restate_lookback)

        def item_seen(code: str, indices: set) -> int:
            for i in indices:
                if i < len(form_list) and form_list[i] == "8-K" \
                        and code in str(items_list[i] if i < len(items_list) else ""):
                    return 1
            return 0

        def form_seen(names: tuple, indices: set) -> int:
            return int(any(i < len(form_list) and form_list[i] in names
                           for i in indices))

        out = {
            "bankruptcy_filing": item_seen("1.03", window),
            "debt_acceleration": item_seen("2.04", window),
            "delisting_notice": item_seen("3.01", window),
            "unregistered_sale": item_seen("3.02", window),
            "auditor_change": item_seen("4.01", window),
            "restatement": item_seen("4.02", restate_window),
            "officer_departure": item_seen("5.02", window),
            "late_filing": form_seen(("NT 10-K", "NT 10-Q"), window),
            "shelf_registration": form_seen(("S-3", "S-3ASR"), window),
        }

        # Going concern needs the filing text, so it reuses whatever
        # `fetch_filing_sections` already pulled for this ticker this run.
        concern = 0
        for form, meta in _pick_latest(cik, cfg, forms).items():
            try:
                text, _ = _document_text(cik, meta, cfg)
                concern = max(concern, going_concern_hits(text))
            except Exception as exc:  # noqa: BLE001 - one bad doc must not sink the rest
                log_step("SEC", "failed", f"going-concern scan {form} {ticker}: {exc}")
        out["going_concern"] = int(concern > 0)

        tripped = [k for k, v in out.items() if v]
        log_step("SEC", "ok", f"flags for {ticker}: "
                              f"{', '.join(tripped) if tripped else 'none tripped'}")
        return out
    except Exception as exc:  # noqa: BLE001 - flags are optional, always
        log_step("SEC", "failed", f"flags for {ticker}: {exc}")
        return {}


def fetch_filing_sections(ticker: str, cfg: dict | None = None) -> dict | None:
    """Latest 10-Q + 10-K narrative sections + curated XBRL facts, or None."""
    cfg = cfg or load_config()
    sec = cfg.get("research", {}).get("sec", {})
    forms = sec.get("forms", ["10-Q", "10-K"])
    maxc = sec.get("max_section_chars", 24000)
    try:
        cik = ticker_to_cik(ticker, cfg)
        if not cik:
            log_step("SEC", "miss", f"no CIK for {ticker}")
            return None
        log_step("SEC", "ok", f"CIK {cik} for {ticker}")
        picked = _pick_latest(cik, cfg, forms)
        log_step("SEC", "ok", f"latest filings: {', '.join(picked) or 'none'}")
        result = {"cik": cik, "filings": {}, "financials": _xbrl_facts(cik, cfg)}
        for form, meta in picked.items():
            url = _doc_url(cik, meta["accession"], meta["primary_doc"])
            entry = {"url": url, "filing_date": meta["filing_date"],
                     "period": meta["report_date"], "sections": {}}
            try:
                t0 = time.perf_counter()
                text, raw_bytes = _document_text(cik, meta, cfg)
                entry["text_len"] = len(text)
                if form == "10-K":
                    entry["sections"] = {
                        "business": _extract_between(
                            text, r"item\s*1\.?\s*business",
                            [r"item\s*1a\.?\s*risk\s*factors"], maxc),
                        "risk_factors": _extract_between(
                            text, r"item\s*1a\.?\s*risk\s*factors",
                            [r"item\s*1b\.?\s*unresolved", r"item\s*2\.?\s*propert"], maxc),
                        "mdna": _extract_between(
                            text, r"item\s*7\.?\s*management.{0,4}discussion",
                            [r"item\s*7a\.?\s*quantitat", r"item\s*8\.?\s*financial\s*statement"], maxc),
                    }
                else:  # 10-Q
                    entry["sections"] = {
                        "mdna": _extract_between(
                            text, r"item\s*2\.?\s*management.{0,4}discussion",
                            [r"item\s*3\.?\s*quantitat", r"item\s*4\.?\s*controls"], maxc),
                        "risk_factors": _extract_between(
                            text, r"item\s*1a\.?\s*risk\s*factors",
                            [r"item\s*2\.?\s*unregist", r"item\s*5\.?\s*other", r"item\s*6\.?\s*exhibit"], maxc),
                    }
                log_step("SEC", "ok",
                         f"{form} {fmt_bytes(raw_bytes)} "
                         f"{_sections_summary(entry['sections'])}",
                         ms=(time.perf_counter() - t0) * 1000)
            except Exception as exc:  # noqa: BLE001 - one bad doc must not sink the rest
                log_step("SEC", "failed", f"{form} {ticker}: {exc}")
                entry["error"] = str(exc)
            result["filings"][form] = entry
        return result
    except Exception as exc:  # noqa: BLE001
        log_step("SEC", "failed", f"{ticker}: {exc}")
        return None


if __name__ == "__main__":
    import sys

    cfg = load_config()
    for sym in sys.argv[1:] or ["AAPL"]:
        data = fetch_filing_sections(sym, cfg)
        if not data:
            print(f"{sym}: filings n/a")
            continue
        print(f"\n=== {sym}  CIK {data['cik']}  ===")
        print("XBRL concepts:", {k: v["val"] for k, v in data["financials"].items()})
        for form, f in data["filings"].items():
            secs = {k: (f"{len(v)} chars" if v else "n/a") for k, v in f["sections"].items()}
            print(f"  {form} {f['filing_date']} (period {f['period']}): {secs}")
