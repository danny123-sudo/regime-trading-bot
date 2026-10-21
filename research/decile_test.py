"""Turn an information coefficient into money.

An IC says a signal ranks forward returns better than chance. It does not say
the spread is large enough to trade after fees. This sorts observations into
quintiles by signal value and reports the mean forward return of each, so the
question becomes concrete: does the top quintile beat the bottom by more than
the ~0.16% round-trip cost of acting on it?
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from freqtrade.data.history import load_pair_history  # noqa: E402
from research.signal_study import DATA_DIR, build_signals  # noqa: E402

ROUND_TRIP_COST = 0.0016  # KuCoin spot maker+taker with KCS discount


def main() -> int:
    timeframe = sys.argv[1] if len(sys.argv) > 1 else "4h"
    horizon = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    wanted = sys.argv[3:] or ["rel_strength_24", "zscore_rev_24", "bb_pctb_rev",
                              "mom_12", "vol_ratio_short_long"]

    files = sorted(DATA_DIR.glob(f"*-{timeframe}.feather"))[:25]
    pairs = [f.name.replace(f"-{timeframe}.feather", "").replace("_", "/") for f in files]
    btc = load_pair_history("BTC/USDT", timeframe, DATA_DIR)
    btc_close = btc.set_index("date")["close"] if len(btc) else None

    frames: dict[str, list[pd.DataFrame]] = {w: [] for w in wanted}
    for pair in pairs:
        df = load_pair_history(pair, timeframe, DATA_DIR)
        if len(df) < 300:
            continue
        df = df.set_index("date")
        sigs = build_signals(df, btc_close)
        fwd = df["close"].shift(-horizon) / df["close"] - 1
        for name in wanted:
            if name in sigs:
                frames[name].append(pd.DataFrame({"sig": sigs[name], "fwd": fwd}))

    print(f"timeframe={timeframe}  horizon={horizon} candles  pairs={len(pairs)}")
    print(f"round-trip cost assumed: {ROUND_TRIP_COST:.2%}\n")
    print(f"{'signal':22s} {'Q1(low)':>9s} {'Q3':>9s} {'Q5(high)':>9s} {'spread':>9s} "
          f"{'net':>9s}  tradeable?")
    print("-" * 84)

    for name in wanted:
        if not frames[name]:
            continue
        data = pd.concat(frames[name]).replace([np.inf, -np.inf], np.nan).dropna()
        if len(data) < 1000:
            continue
        data["q"] = pd.qcut(data["sig"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
        means = data.groupby("q", observed=True)["fwd"].mean()
        spread = means.get(5, np.nan) - means.get(1, np.nan)
        # Trading the spread means paying costs on both legs of the comparison.
        net = abs(spread) - ROUND_TRIP_COST
        verdict = "yes" if net > 0 else "no — eaten by fees"
        print(f"{name:22s} {means.get(1,np.nan):>+8.3%} {means.get(3,np.nan):>+8.3%} "
              f"{means.get(5,np.nan):>+8.3%} {spread:>+8.3%} {net:>+8.3%}  {verdict}")

    print("\nQ1 = lowest signal values, Q5 = highest.")
    print("A negative spread means the signal works INVERTED: buy Q1, not Q5.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
