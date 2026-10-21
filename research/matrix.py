"""Backtest every candidate strategy across timeframes, with a train/test split.

Prints one table. Anything that looks good on train and collapses on test is
overfit; the test column is the only one that means anything.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FT = ROOT / ".venv" / "bin" / "freqtrade"

WINDOWS = {"train": "20230601-20250101", "test": "20250101-20260830"}
SUMMARY = re.compile(
    r"│\s+TOTAL\s+│\s+(\d+)\s+│\s+(-?[\d.]+)\s+│\s+(-?[\d.]+)\s+│\s+(-?[\d.]+)\s+│"
    r"\s+([^│]+?)\s+│\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+│")
PF = re.compile(r"Profit factor\s+│\s+(-?[\d.]+)")
MKT = re.compile(r"Market change\s+│\s+(-?[\d.]+)%")


def run(strategy: str, timeframe: str, timerange: str, config: Path) -> dict | None:
    cmd = [str(FT), "backtesting", "-c", str(config), "-s", strategy,
           "--strategy-path", str(ROOT / "user_data" / "strategies"),
           "--timeframe", timeframe, "--timerange", timerange, "--cache", "none"]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=3600)
    out = p.stdout + p.stderr
    m = SUMMARY.search(out)
    if not m:
        return None
    pf = PF.search(out)
    mkt = MKT.search(out)
    return {"trades": int(m.group(1)), "profit": float(m.group(4)),
            "win": float(m.group(9)),
            "pf": float(pf.group(1)) if pf else float("nan"),
            "market": float(mkt.group(1)) if mkt else float("nan")}


def main() -> int:
    config = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/core.json")
    strategies = sys.argv[2:] or ["MomentumCore", "CoreTrend", "Supertrend", "ClucMay72018"]
    timeframes = ["4h", "1d"]

    results = {}
    for strategy in strategies:
        for tf in timeframes:
            for label, tr in WINDOWS.items():
                key = (strategy, tf, label)
                print(f"  running {strategy:14s} {tf:3s} {label} ...", flush=True)
                results[key] = run(strategy, tf, tr, config)
            (ROOT / "research" / "matrix_results.json").write_text(
                json.dumps({f"{k[0]}|{k[1]}|{k[2]}": v for k, v in results.items()}, indent=2))

    print("\n" + "=" * 96)
    print(f"{'strategy':15s} {'tf':4s} | {'TRAIN 2023-06..2025-01':>32s} | {'TEST 2025-01..2026-08':>32s}")
    print(f"{'':15s} {'':4s} | {'ret':>8s} {'trades':>7s} {'win':>6s} {'PF':>6s} | "
          f"{'ret':>8s} {'trades':>7s} {'win':>6s} {'PF':>6s}")
    print("-" * 96)
    for strategy in strategies:
        for tf in timeframes:
            cells = []
            for label in WINDOWS:
                r = results.get((strategy, tf, label))
                cells.append(f"{r['profit']:>+7.2f}% {r['trades']:>7d} {r['win']:>5.1f}% {r['pf']:>6.2f}"
                             if r else f"{'no trades / failed':>31s}")
            print(f"{strategy:15s} {tf:4s} | {cells[0]} | {cells[1]}")
    for label, tr in WINDOWS.items():
        for r in results.values():
            if r and r.get("market") == r.get("market"):
                pass
    mk = {l: next((r["market"] for k, r in results.items() if r and k[2] == l), None) for l in WINDOWS}
    print("-" * 96)
    print(f"buy-and-hold: train {mk['train']}%   test {mk['test']}%")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
