"""
Parameter tuning for any screen: signal quality vs the cases you refuse to miss.

Answers "which threshold should I change, and what does it cost me?" by
re-running a screen over the cached universe with one value swapped at a time,
scoring each candidate against the random-entry baseline **and** against a list
of protected cases that must keep firing. A config that scores well by dropping
the setups you actually wanted is not an improvement, which is the whole reason
the protected-case column exists.

Nothing here re-implements condition math or statistics: candidates run through
the production `compute_*` / `fires_mask` / `partial_mask`, and the numbers come
from `backtest_universe`'s `forward_trades` / `cohort_values` / `stats_from`.
It never downloads -- it reads the panel `backtest_universe.py` already cached,
so run that once first.

Modes:
  sensitivity  one parameter at a time over its configured values (start here:
               it shows which knobs matter at all)
  grid         cross product of the 2-3 knobs named in `tuning.grid`, for the
               interactions one-at-a-time cannot see (e.g. two thresholds that
               each block the same protected case)
  delay        for a few candidate configs, the wait x hold surface -- does the
               config only work if you don't buy the signal day?

Every parameter range, protected case and grid axis lives in `config.json`
(`tuning`), so tuning is a config edit, not a code edit.

Usage:
    python tune_screen.py sensitivity reclaim_strategy
    python tune_screen.py grid reclaim_strategy --holding 60
    python tune_screen.py delay breakout_strategy --csv
"""

import argparse
import itertools
import sys

import pandas as pd

from backtest_universe import (
    SCREENS,
    baseline_stats,
    cached_panel,
    cohort_values,
    forward_trades,
    stats_from,
)
from scanner_common import load_config, output_dir

# Columns shown for every candidate, in this order.
COLUMNS = ["candidate", "n_full", "n_partial", "n_fires", "mean_full",
           "win_full", "excess_full", "mean_fires", "win_fires",
           "excess_fires", "protected", "note"]


def section(title: str) -> None:
    print(f"\n{'=' * 118}\n{title}\n{'=' * 118}")


def find_screen(key: str):
    """The (module, compute) pair for a config key, or a clear error."""
    for module, compute in SCREENS:
        if module.CONFIG_KEY == key:
            return module, compute
    known = sorted(m.CONFIG_KEY for m, _ in SCREENS)
    raise SystemExit(f"unknown screen {key!r} -- known: {known}")


# --------------------------------------------------------------------------
# Evaluating one candidate configuration
# --------------------------------------------------------------------------

def evaluate(module, compute, strategy: dict, ctx: dict) -> dict:
    """Signal counts, quality and protected-case status for one config.

    Reports both tiers: `full` (the strict signal) and `fires` (everything the
    screen would actually alert on). For a screen that stays strict those are
    the same, and `n_partial` is then a control cohort it never sends.
    """
    panel, start, universe = ctx["panel"], ctx["start"], ctx["universe"]
    signals = compute(panel, strategy)
    full = signals["signal"].fillna(False)[universe]
    partial = module.partial_mask(panel, signals, strategy).fillna(False)[universe]
    fires = module.fires_mask(panel, signals, strategy).fillna(False)[universe]

    row = {"n_partial": int(partial.loc[start:].sum().sum())}
    for label, mask in (("full", full), ("fires", fires)):
        st = stats_from(cohort_values(mask, ctx["fwd"], start))
        row[f"n_{label}"] = st["signals"]
        row[f"mean_{label}"] = st["mean_%"]
        row[f"win_{label}"] = st["win_rate_%"]
        row[f"excess_{label}"] = st["mean_%"] - ctx["baseline"]

    # Does this config still catch what we refuse to miss?
    alerts_partials = row["n_fires"] > row["n_full"]
    tiers = []
    for case in ctx["protected"]:
        ts = pd.Timestamp(case["date"])
        ticker = case["ticker"]
        if ts not in full.index or ticker not in full.columns:
            tiers.append("?")           # outside the panel / not in the universe
        elif bool(full.at[ts, ticker]):
            tiers.append("FULL")
        elif bool(partial.at[ts, ticker]):
            tiers.append("partial" if alerts_partials else "MISSED(partial)")
        else:
            tiers.append("MISSED")
    row["protected"] = "/".join(tiers) if tiers else "-"

    # A swept lookback can exceed the warm-up the cached panel actually has.
    needed = module.required_history(strategy)
    row["note"] = "" if needed <= ctx["warmup_days"] else \
        f"needs {needed}d warm-up, panel has {ctx['warmup_days']}d"
    return row


def show(rows: list[dict], title: str) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df = df[[c for c in COLUMNS if c in df.columns]]
    shown = df.copy()
    for c in shown.columns:
        if shown[c].dtype.kind == "f":
            shown[c] = shown[c].map(lambda v: f"{v:+.2f}" if pd.notna(v) else "n/a")
    section(title)
    print(shown.to_string(index=False))
    return df


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def sensitivity(module, compute, base: dict, sweeps: dict, ctx: dict) -> list[dict]:
    rows = [{"candidate": "CURRENT CONFIG", **evaluate(module, compute, base, ctx)}]
    show(rows, f"Baseline: {module.CONFIG_KEY} as configured today")
    out = list(rows)
    for param, values in sweeps.items():
        if param not in base:
            print(f"\n(skipping {param!r}: not a key of {module.CONFIG_KEY})")
            continue
        block = []
        for v in values:
            mark = "  <- current" if base.get(param) == v else ""
            block.append({"candidate": f"{param}={v}{mark}",
                          **evaluate(module, compute, {**base, param: v}, ctx)})
        show(block, f"Sensitivity: {param} (everything else as configured)")
        out += block
    return out


def grid(module, compute, base: dict, sweeps: dict, axes: list[str],
         ctx: dict) -> list[dict]:
    axes = [a for a in axes if a in base]
    if not axes:
        raise SystemExit(f"no usable grid axes for {module.CONFIG_KEY} -- set "
                         f"tuning.grid.{module.CONFIG_KEY} to parameter names")
    values = [sweeps.get(a, [base[a]]) for a in axes]
    rows = [{"candidate": "CURRENT CONFIG", **evaluate(module, compute, base, ctx)}]
    for combo in itertools.product(*values):
        over = dict(zip(axes, combo))
        label = " ".join(f"{k.replace('_pct', '')}={v}" for k, v in over.items())
        rows.append({"candidate": label,
                     **evaluate(module, compute, {**base, **over}, ctx)})
    # Best first, but the protected column is what decides acceptability.
    rows.sort(key=lambda r: -(r["excess_fires"] if pd.notna(r["excess_fires"])
                              else -99))
    show(rows, f"Grid over {', '.join(axes)} -- best excess first "
               f"({len(rows) - 1} combinations)")
    return rows


def delay(module, compute, base: dict, sweeps: dict, ctx: dict,
          holds: list[int]) -> list[dict]:
    """Wait x hold surface per candidate -- does a config's edge depend on NOT
    buying the signal day?

    Shows both tiers, because a screen's confirmation thresholds often leave
    the alerted (`fires`) set untouched and only move the full/partial
    boundary -- the effect then shows up in the full column alone.
    """
    candidates = {"CURRENT CONFIG": {}}
    # One loosened variant per swept parameter's extreme, to bracket the space.
    for param, values in sweeps.items():
        if param in base and values and base.get(param) != values[0]:
            candidates[f"{param}={values[0]}"] = {param: values[0]}

    # Pre-compute the shared forward frames once (6 waits x holds), not per
    # candidate -- they don't depend on the strategy at all.
    fwd = {(d, h): forward_trades(ctx["panel"], h, ctx["entry"], False,
                                  delay=d, dates=False)
           for d in range(6) for h in holds}
    base_mean = {k: baseline_stats(f, ctx["start"], ctx["universe"], False)["mean_%"]
                 for k, f in fwd.items()}

    rows, seen = [], {}
    for label, over in candidates.items():
        strategy = {**base, **over}
        signals = compute(ctx["panel"], strategy)
        full = signals["signal"].fillna(False)[ctx["universe"]]
        fires = module.fires_mask(ctx["panel"], signals, strategy) \
            .fillna(False)[ctx["universe"]]

        # Skip a candidate that changes neither cohort -- e.g. a slope lookback
        # while the slope floor is null, or a confirmation threshold on a screen
        # whose partial band already absorbs that failure.
        finger = (int(full.loc[ctx["start"]:].to_numpy().sum()),
                  int(fires.loc[ctx["start"]:].to_numpy().sum()),
                  float(full.loc[ctx["start"]:].to_numpy().argmax()))
        if finger in seen:
            print(f"\n{label}: identical cohorts to '{seen[finger]}' -- skipped")
            continue
        seen[finger] = label

        print(f"\n{label}  (n_full={finger[0]}, n_fires={finger[1]})")
        print("  mean return %: alerted / [full tier]   (excess vs random entry)")
        print("  wait |" + "".join(f"{f'hold {h}':>30}" for h in holds))
        for d in range(6):
            cells = []
            for h in holds:
                f, bl = fwd[(d, h)], base_mean[(d, h)]
                mf = stats_from(cohort_values(fires, f, ctx["start"]))["mean_%"]
                ml = stats_from(cohort_values(full, f, ctx["start"]))["mean_%"]
                cells.append(f"{mf:+.2f}/[{ml:+.2f}] ({mf - bl:+.2f})")
                rows.append({"candidate": label, "wait": d, "hold": h,
                             "mean_fires": mf, "mean_full": ml,
                             "excess_fires": mf - bl})
            print(f"  {d:>4} |" + "".join(f"{c:>30}" for c in cells))
    return rows


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    cfg = load_config()
    tune_cfg = cfg.get("tuning", {})
    bt_cfg = cfg.get("backtest", {})

    p = argparse.ArgumentParser(
        description="Tune one screen's thresholds against signal quality and "
                    "protected cases.")
    p.add_argument("mode", choices=["sensitivity", "grid", "delay"])
    p.add_argument("screen", help="config key, e.g. reclaim_strategy")
    p.add_argument("--years", type=int, default=tune_cfg.get("years", 5),
                   help="length of the analysis window")
    p.add_argument("--holding", type=int,
                   default=(tune_cfg.get("holding_days") or [30])[0],
                   help="holding period scored in the table (trading days)")
    p.add_argument("--entry", default=bt_cfg.get("entry", "next_open"),
                   choices=["next_open", "signal_close"])
    p.add_argument("--csv", action="store_true",
                   help="also write the table to output/tune_<screen>_<mode>.csv")
    args = p.parse_args()

    module, compute = find_screen(args.screen)
    base = cfg.get(module.CONFIG_KEY)
    if not base:
        raise SystemExit(f"no '{module.CONFIG_KEY}' section in config.json")
    sweeps = tune_cfg.get("sweeps", {}).get(module.CONFIG_KEY, {})
    protected = [c for c in tune_cfg.get("protected_cases", [])
                 if c.get("screen") == module.CONFIG_KEY]

    panel = cached_panel(bt_cfg)
    benchmark = bt_cfg.get("benchmark_ticker")
    universe = [t for t in panel["Close"].columns if t != benchmark]
    start = panel.index[-1] - pd.DateOffset(years=args.years)
    warmup_days = len(panel.loc[:start])
    fwd = forward_trades(panel, args.holding, args.entry, False, delay=0,
                         dates=False)
    baseline = baseline_stats(fwd, start, universe, False)["mean_%"]

    ctx = {"panel": panel, "start": start, "universe": universe, "fwd": fwd,
           "baseline": baseline, "protected": protected, "entry": args.entry,
           "warmup_days": warmup_days}

    print(f"Screen {module.CONFIG_KEY} · analysis window {start.date()} .. "
          f"{panel.index[-1].date()} ({len(panel.loc[start:])} trading days, "
          f"{warmup_days} days of warm-up before it)")
    print(f"Scored at hold {args.holding} trading days, entry {args.entry}; "
          f"random-entry baseline {baseline:+.2f}%")
    if protected:
        print("Protected cases (must keep firing, ideally as a full setup):")
        for c in protected:
            print(f"  {c['ticker']} {c['date']} -- {c.get('note', '')}")
    else:
        print("No protected cases configured for this screen -- add some to "
              "tuning.protected_cases before trusting a tightening.")
    print("\nInherits the backtest's caveats: survivorship-biased universe, no "
          "costs, no dividends,\nand clustered overlapping trades -- so treat "
          "small differences as noise.")

    if args.mode == "sensitivity":
        rows = sensitivity(module, compute, base, sweeps, ctx)
    elif args.mode == "grid":
        axes = tune_cfg.get("grid", {}).get(module.CONFIG_KEY, [])
        rows = grid(module, compute, base, sweeps, axes, ctx)
    else:
        rows = delay(module, compute, base, sweeps, ctx,
                     tune_cfg.get("holding_days") or [args.holding])

    if args.csv:
        path = output_dir() / f"tune_{module.CONFIG_KEY}_{args.mode}.csv"
        pd.DataFrame(rows).round(4).to_csv(path, index=False)
        print(f"\nTable -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
