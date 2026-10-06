"""The shared frame of the four single-ticker backtests.

Each `backtest_<screen>.py` keeps its own step-by-step log of the condition
math -- that narrative is the point of the script. Everything around it is the
same for all four and lives here: the CLI, the download with the screen's own
warm-up, the production `compute`, the per-day table and the CSV + chart.
"""

import argparse

import pandas as pd

from scanner_common import download_history, load_config, output_dir


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def run(module, log_run, plot, *, ticker: str, start: str, end: str,
        description: str, prefix: str, argv: list[str] | None = None) -> int:
    """Backtest `module`'s production logic on one ticker over one window.

    `ticker`, `start` and `end` are the defaults -- the documented validation
    case for this screen. Writes `<prefix>_<TICKER>.csv` and `.png` under
    output/.
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--ticker", default=ticker)
    parser.add_argument("--start", default=start, help="analysis window start")
    parser.add_argument("--end", default=end, help="analysis window end")
    args = parser.parse_args(argv)

    ticker = args.ticker.upper()
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    strategy = load_config()[module.CONFIG_KEY]

    data = download_history(ticker, start, end, module.required_history(strategy))
    signals = module.compute(data, strategy)
    table = module.build_calc_table(data, signals, ticker).loc[start:end]

    log_run(table, strategy, ticker)

    out_dir = output_dir()
    csv_path = out_dir / f"{prefix}_{ticker}.csv"
    table.round(4).to_csv(csv_path)
    print(f"\nFull per-day calculation table saved to {csv_path}")
    plot(table, strategy, ticker, out_dir / f"{prefix}_{ticker}.png")
    return 0
