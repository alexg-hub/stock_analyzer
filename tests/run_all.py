"""Run every test script and summarise.

    python tests/run_all.py              # offline + cache-backed tests
    python tests/run_all.py --network    # also the Yahoo round-trip test

Exit codes from each script: 0 pass, 1 fail, 2 skipped (a prerequisite such as
the cached price panel was missing). Skips are reported, not treated as
failures -- a fresh clone has no cache yet.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# In dependency order: cheapest and most fundamental first, so a broken
# forward_trades shows up before the tests that build on it.
TESTS = [
    ("test_quality.py", "offline -- the quality registry: flags, gates, score"),
    ("test_derived.py", "offline -- Altman/Beneish, distress flags, moat proxies"),
    ("test_price_risk.py", "offline -- volatility, drawdown, beta, alignment"),
    ("test_universe_scan.py", "offline -- plane scoping, cache, quadrants"),
    ("test_ibkr.py", "offline -- IBKR client: no account surface, degrades"),
    ("test_mcp_server.py", "offline -- MCP tool contract, stdout purity, dry-run"),
    ("test_forward_trades.py", "offline -- synthetic panel arithmetic"),
    ("test_research_output.py", "offline -- tier-3 financials, chart, verdict cards"),
    ("test_portfolio_sim.py", "offline -- tier-4 ledger, marking, attribution"),
    ("test_signal_contract.py", "cached panel -- signal tiers, alert, hand-off"),
    ("test_combined_alert.py", "cached panel -- all four tiers in one message"),
    ("test_backtest_stats.py", "cached panel -- statistics and trade rows"),
]
NETWORK_TESTS = [
    ("test_path_equivalence.py", "NETWORK -- universe vs single-ticker download"),
]


def main() -> int:
    tests = list(TESTS)
    if "--network" in sys.argv:
        tests += NETWORK_TESTS
    else:
        for name, why in NETWORK_TESTS:
            print(f"[skip] {name:28s} {why} (pass --network to include)")

    results = {}
    for name, why in tests:
        print(f"\n{'=' * 78}\n>>> {name}  ({why})\n{'=' * 78}")
        rc = subprocess.run([sys.executable, str(HERE / name)]).returncode
        results[name] = rc

    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    labels = {0: "PASS", 1: "FAIL", 2: "SKIP"}
    for name, rc in results.items():
        print(f"  {labels.get(rc, f'EXIT {rc}'):5s} {name}")
    failed = [n for n, rc in results.items() if rc not in (0, 2)]
    skipped = [n for n, rc in results.items() if rc == 2]
    if skipped:
        print(f"\n{len(skipped)} skipped (missing prerequisites, not failures).")
    if failed:
        print(f"\n{len(failed)} FAILED: {failed}")
        return 1
    print("\nAll executed tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
