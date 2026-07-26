# Tests

- **`tests/` asserts invariants, never snapshots.** Anything comparing against
  recorded counts goes stale the moment the user retunes `config.json`, which is
  constantly — so a check has to hold at *any* thresholds (e.g. "`fires_mask` is
  `signal` or exactly `signal | partial`", not "breakout fires 2709 times"). Test
  fixtures come from the **cached** panel via `_harness.cached_panel_or_skip()`
  and a `busiest_day()` found from the data, so no test downloads, hardcodes a
  date, or sends to Discord. Exit codes are the interface: 0 pass, 1 fail,
  **2 skip** (a missing cache is a skip with instructions, not a failure).
  Add new checks to the existing file that owns that concern rather than making
  another script; `run_all.py` lists them in dependency order.
