"""Offline -- the enrichment record and how tier 4 reads it.

The agent's judgment reaches the ledger as an *attribute*, never as an
adjustment. Everything below defends one of the four properties that makes that
true, and every one of them fails silently if it breaks:

  * **No field can move a score.** `validate` rejects `conviction`, `tier`,
    `score` and `narrative_adj` by name. The predecessor of this table was a
    bounded number the model wrote into the recorded conviction; the whole point
    of the replacement is that there is nothing here to write.
  * **An invalid row raises rather than records.** Almost everything else in
    this project fails open, because a missing measurement is honest. A wrong
    *categorical* value is not missing -- it is a cohort of one that `analyze`
    will faithfully report on, and nothing downstream can tell the difference.
  * **Absent enrichment yields no `en_*` columns at all.** "Never enriched" is a
    legitimate cohort, not a neutral reading -- the same distinction
    `quality.has_values` draws, and why `qr_*` is absent rather than False for a
    signal the quality layer never graded.
  * **`en_*` must never be protected by `mark_columns()`.** An enrichment is
    normally written *after* the position opened, so a protected column would
    inherit the empty value and could never learn about it. Exactly the trap
    `vt_*` and `qr_*` already carry a test for.

Fixtures are synthetic and every path is redirected into a temp directory:
nothing here reads or writes the real record.
"""

import copy
import json
import tempfile
from pathlib import Path

import pandas as pd

from _harness import Checks, config

import enrichment
import scanner_common
from portfolio_sim import ledger
from portfolio_sim.marking import ENRICH_PREFIX, _enrichment_columns

c = Checks("enrichment record")

TMP = Path(tempfile.mkdtemp(prefix="enrich_test_"))
cfg = copy.deepcopy(config())
cfg["enrichment"] = {"enabled": True, "dir": str(TMP),
                     "csv": str(TMP / "enrichment.csv")}
# History lives in the temp tree too, or `record` reads the real signals.csv
# while deciding whether anything can join to the row.
cfg.setdefault("research", {}).setdefault("history", {}).update(
    dir=str(TMP), csv=str(TMP / "signals.csv"),
    on_demand_csv=str(TMP / "on_demand.csv"))

ROW = {"scan_date": "2026-01-02", "ticker": "aaa", "stance": "Bull",
       "moat_view": "Stable", "social_sentiment": "thin",
       "conviction_note": "the off-price model keeps working",
       "concerns": ["tariff exposure", "comps"],
       "catalysts": ["Q4 print"], "rule_disputes": ["current_ratio"],
       "sources": ["https://example.com/a", "https://example.com/b"],
       "report": "AAA_2026-01-02.md", "model": "opus"}


# --------------------------------------------------------------------------
c.section("nothing here can move a score")

for banned in ("narrative_adj", "conviction", "tier", "score"):
    problems = enrichment.validate({**ROW, banned: 12})
    c.ok(f"a {banned!r} field is rejected outright",
         any(banned in p for p in problems),
         "the record is an attribute, never an adjustment")

c.ok("no column in the schema is a score",
     not ({"conviction", "tier", "score", "narrative_adj"}
          & set(enrichment.COLUMNS)),
     str(enrichment.COLUMNS))


# --------------------------------------------------------------------------
c.section("an invalid row raises instead of recording")

c.ok("an unknown stance is rejected",
     any("stance" in p for p in enrichment.validate({**ROW, "stance": "bullish"})),
     "silently it becomes a one-member cohort the analysis then grades")
c.ok("an unknown moat_view is rejected",
     any("moat_view" in p for p in enrichment.validate({**ROW,
                                                        "moat_view": "wide"})))
c.ok("a missing scan_date is rejected",
     any("scan_date" in p for p in enrichment.validate(
         {k: v for k, v in ROW.items() if k != "scan_date"})),
     "a row nothing can join to")
c.ok("a list field given a bare string is rejected",
     any("concerns" in p for p in enrichment.validate({**ROW,
                                                       "concerns": "tariffs"})))
c.ok("an unknown field is rejected",
     any("unknown" in p for p in enrichment.validate({**ROW, "target": 200})),
     "a typo must not become a column nothing reads")
c.ok("a valid row has no problems", enrichment.validate(ROW) == [])

raised = False
try:
    enrichment.record({**ROW, "stance": "moon"}, cfg)
except ValueError:
    raised = True
c.ok("record() raises on an invalid row rather than writing it", raised)
c.ok("...and wrote nothing", not enrichment.enrichment_csv_path(
    cfg, create=False).exists())


# --------------------------------------------------------------------------
c.section("the round trip")

path = enrichment.record(ROW, cfg)
got = enrichment.read_one("AAA", "2026-01-02", cfg)
c.ok("the ticker is normalized to upper case", got.get("ticker") == "AAA")
c.ok("vocabulary values are normalized to lower case",
     got.get("stance") == "bull" and got.get("moat_view") == "stable")
c.ok("counts are derived, not asked for",
     got.get("sources_n") == 2 and got.get("concerns_n") == 2)
c.ok("list fields survive the CSV round trip",
     enrichment.decode_lists(got)["concerns"] == ["tariff exposure", "comps"],
     "stored as JSON, exactly as history_rows stores its series")

enrichment.record({**ROW, "stance": "bear"}, cfg)
frame = pd.read_csv(path, dtype={"scan_date": str})
c.ok("re-recording the same (scan_date, ticker) replaces the row",
     len(frame) == 1 and frame.loc[0, "stance"] == "bear",
     f"{len(frame)} row(s)")

enrichment.record({**ROW, "scan_date": "2026-01-09"}, cfg)
c.ok("a different date is a different row",
     len(pd.read_csv(path, dtype={"scan_date": str})) == 2)
c.ok("reading a date that was never enriched returns {}",
     enrichment.read_one("AAA", "2026-02-02", cfg) == {})
c.ok("reading a ticker that was never enriched returns {}",
     enrichment.read_one("ZZZ", "2026-01-02", cfg) == {})

off = copy.deepcopy(cfg)
off["enrichment"]["enabled"] = False
c.ok("the section switch is a full no-op",
     enrichment.record(ROW, off) is None)


# --------------------------------------------------------------------------
c.section("what tier 4 reads")

cols = _enrichment_columns("AAA", "2026-01-02", cfg)
c.ok("every recorded field arrives under the en_ prefix",
     all(k.startswith(ENRICH_PREFIX) for k in cols) and cols,
     str(sorted(cols)))
c.ok("the categorical judgments are carried as text",
     cols.get("en_stance") == "bear" and cols.get("en_moat_view") == "stable"
     and cols.get("en_social_sentiment") == "thin")
c.ok("the evidence counts are carried as numbers",
     cols.get("en_sources_n") == 2.0 and cols.get("en_concerns_n") == 2.0,
     "graded as ordinary numeric predictors, not as a split")

c.ok("a position that was never enriched gets NO en_ columns at all",
     _enrichment_columns("ZZZ", "2026-01-02", cfg) == {},
     "absent is a cohort; a neutral default would invent a reading")

minimal = copy.deepcopy(cfg)
minimal["enrichment"]["csv"] = str(TMP / "minimal.csv")
enrichment.record({"scan_date": "2026-01-02", "ticker": "BBB",
                   "stance": "neutral"}, minimal)
thin = _enrichment_columns("BBB", "2026-01-02", minimal)
c.ok("an enrichment with only a stance carries only what it has",
     thin.get("en_stance") == "neutral" and "en_moat_view" not in thin,
     str(sorted(thin)))

# The trap `vt_*` and `qr_*` already carry a test for. An enrichment is written
# days AFTER the position opened, so a protected column would inherit the empty
# value from the row on file and could never learn about it.
mark_cols = set(ledger.mark_columns(ledger.horizons_of(cfg["portfolio"])))
c.ok("no en_ column is protected by mark_columns()",
     not any(k.startswith(ENRICH_PREFIX) for k in mark_cols),
     "protecting them would freeze 'never enriched' forever")

c.ok("enrichment is not carried as a verdict column",
     not any(str(k).startswith(ENRICH_PREFIX)
             for k in scanner_common.VERDICT_COLS),
     "the verdict is the registry's; these are a separate record")


# --------------------------------------------------------------------------
c.section("the CLI is the only write path")

payload = TMP / "row.json"
payload.write_text(json.dumps({**ROW, "ticker": "CCC"}), encoding="utf-8")
c.ok("`record <file.json>` writes the row",
     enrichment._cmd_record([str(payload)], cfg) == 0
     and enrichment.read_one("CCC", "2026-01-02", cfg).get("stance") == "bull")

payload.write_text(json.dumps([{**ROW, "ticker": "DDD"},
                               {**ROW, "ticker": "EEE"}]), encoding="utf-8")
c.ok("...and accepts a batch",
     enrichment._cmd_record([str(payload)], cfg) == 0
     and enrichment.read_one("EEE", "2026-01-02", cfg).get("stance") == "bull")

c.ok("the module offers no way to edit the analyzer's own tables",
     not any(hasattr(enrichment, n) for n in
             ("record_verdict", "update_verdict", "post_summary")),
     "the agent records judgment; the verdict stays the registry's")

raise SystemExit(c.finish())
