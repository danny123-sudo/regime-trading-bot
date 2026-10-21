"""Export what the council needs to do its job, as files git can carry.

The council reads the repo over GitHub; it has no access to the VPS, the
databases, or the exchange. So anything it must reason about has to be exported
here and committed.

Deliberately exports no secrets: trade rows, performance summaries, and the
current lever values. Nothing that could move money.

    python research/export_for_council.py --db user_data/trend.sqlite ...
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "council" / "data"

# Note: trade_duration is NOT a freqtrade sqlite column. It was listed here
# originally and silently never exported, leaving median_duration_min null
# forever. Duration is computed below instead.
TRADE_COLUMNS = [
    "id", "pair", "strategy", "timeframe", "is_open", "open_date", "close_date",
    "open_rate", "close_rate", "amount", "stake_amount", "close_profit",
    "close_profit_abs", "exit_reason", "enter_tag",
    "max_rate", "min_rate", "fee_open", "fee_close",
    # Stop fields answer the question ANALYST actually needs at Phase 2:
    # did this trade go wrong immediately, or reverse after being in profit?
    "initial_stop_loss", "initial_stop_loss_abs", "stop_loss", "stop_loss_abs",
    "is_stop_loss_trailing",
]


def read_trades(db: Path) -> list[dict]:
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    have = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
    cols = [c for c in TRADE_COLUMNS if c in have]
    rows = [dict(r) for r in con.execute(f"SELECT {', '.join(cols)} FROM trades")]
    con.close()
    for r in rows:
        _derive(r)
    return rows


DERIVED_KEYS = ("mfe_pct", "mae_pct", "ever_in_profit", "r_multiple", "duration_min")


def _derive(r: dict) -> None:
    """Add the path fields. Aggregates cannot answer 'how did this lose'.

    MFE/MAE plus the stop state distinguish the two loss shapes that matter:
    a trade that went wrong immediately (MFE near zero, stop never trailed)
    from one that was in profit and reversed (MFE well above entry).
    """
    from datetime import datetime

    # Always present, even when not computable. r_multiple was claimed in the
    # response and absent from the header because no row could populate it —
    # a field that appears only sometimes is a schema that changes shape.
    for k in DERIVED_KEYS:
        r.setdefault(k, None)

    def _f(key):
        v = r.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    open_rate = _f("open_rate")
    hi, lo = _f("max_rate"), _f("min_rate")

    if open_rate and hi is not None:
        r["mfe_pct"] = round((hi - open_rate) / open_rate, 6)
    if open_rate and lo is not None:
        r["mae_pct"] = round((lo - open_rate) / open_rate, 6)
    r["ever_in_profit"] = int(bool(r.get("mfe_pct") and r["mfe_pct"] > 0))

    # R-multiple: profit measured in units of the risk originally taken.
    init_stop = _f("initial_stop_loss_abs") or _f("initial_stop_loss")
    profit_abs = _f("close_profit_abs")
    if open_rate and init_stop and profit_abs is not None and open_rate != init_stop:
        amount = _f("amount") or 0.0
        risk = abs(open_rate - init_stop) * amount
        if risk > 0:
            r["r_multiple"] = round(profit_abs / risk, 4)

    for a, b, out in (("open_date", "close_date", "duration_min"),):
        try:
            start = datetime.fromisoformat(str(r.get(a)))
            end = datetime.fromisoformat(str(r.get(b)))
            r[out] = round((end - start).total_seconds() / 60, 1)
        except (TypeError, ValueError):
            r[out] = None


def summarise(rows: list[dict]) -> dict:
    closed = [r for r in rows if not r.get("is_open") and r.get("close_profit") is not None]
    open_rows = [r for r in rows if r.get("is_open")]
    # Open capital at risk. A bare count hides that four opens can be the whole
    # wallet, which is how "4 open" reads as moderate when it is fully deployed.
    open_stake = sum(float(r.get("stake_amount") or 0) for r in open_rows)

    if not closed:
        # Emit the full key set even at n=0, so the schema does not change shape
        # the moment trades start closing.
        return {"closed_trades": 0, "open_trades": len(open_rows),
                "open_stake_usdt": round(open_stake, 2),
                "win_rate": None, "mean_profit": None, "avg_win": None,
                "avg_loss": None, "profit_factor": None, "total_profit_pct": None,
                "median_duration_min": None, "by_exit_reason": {}, "by_pair": {}}

    profits = [float(r["close_profit"]) for r in closed]
    wins = [p for p in profits if p > 0]
    losses = [-p for p in profits if p <= 0]

    by_reason: dict[str, dict] = {}
    for r in closed:
        key = r.get("exit_reason") or "unknown"
        b = by_reason.setdefault(key, {"n": 0, "total": 0.0})
        b["n"] += 1
        b["total"] += float(r["close_profit"])
    for b in by_reason.values():
        b["mean"] = round(b["total"] / b["n"], 5)
        b["total"] = round(b["total"], 5)

    by_pair: dict[str, dict] = {}
    for r in closed:
        key = r.get("pair") or "?"
        b = by_pair.setdefault(key, {"n": 0, "total": 0.0})
        b["n"] += 1
        b["total"] += float(r["close_profit"])
    for b in by_pair.values():
        b["mean"] = round(b["total"] / b["n"], 5)
        b["total"] = round(b["total"], 5)

    gross_win = sum(wins)
    gross_loss = sum(losses)
    return {
        "closed_trades": len(closed),
        "open_trades": len(open_rows),
        "open_stake_usdt": round(open_stake, 2),
        "win_rate": round(len(wins) / len(closed), 4),
        "mean_profit": round(sum(profits) / len(profits), 5),
        "avg_win": round(sum(wins) / len(wins), 5) if wins else 0.0,
        "avg_loss": round(-sum(losses) / len(losses), 5) if losses else 0.0,
        # The number that decides whether a strategy is working at all.
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "total_profit_pct": round(sum(profits), 5),
        "median_duration_min": (lambda d: d[len(d) // 2] if d else None)(
            sorted(r["duration_min"] for r in closed if r.get("duration_min") is not None)),
        "by_exit_reason": by_reason,
        "by_pair": dict(sorted(by_pair.items(), key=lambda kv: kv[1]["total"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", nargs="*", default=None, help="sqlite files to read")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    dbs = [Path(d) for d in args.db] if args.db else sorted((ROOT / "user_data").glob("*.sqlite"))
    if not dbs:
        print("no databases found — nothing to export")
        return 0

    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "instances": {},
    }

    all_rows: list[dict] = []
    for db in dbs:
        rows = read_trades(db)
        name = db.stem
        report["instances"][name] = summarise(rows)
        for r in rows:
            r["_instance"] = name
        all_rows.extend(rows)

    # Current lever values, so the council proposes deltas from reality rather
    # than from what it assumes the config says.
    try:
        sys.path.insert(0, str(ROOT))
        from core import council
        report["current_levers"] = council.load()
        report["lever_bounds"] = {
            s: {k: list(v) for k, v in b.items()} for s, b in council.BOUNDS.items()
        }
    except Exception as exc:  # noqa: BLE001
        report["current_levers"] = {"error": str(exc)}

    # Identity + integrity. A wiped database must not be silently stitched onto
    # the previous book, and the summary must agree with the tape it came from.
    report["export_generation"] = {
        db.stem: (db.stat().st_mtime_ns if db.exists() else None) for db in dbs
    }
    counted = sum(v.get("closed_trades", 0) + v.get("open_trades", 0)
                  for v in report["instances"].values())
    report["integrity"] = {
        "rows_in_csv": len(all_rows),
        "rows_in_summary": counted,
        "counts_agree": counted == len(all_rows),
        "instances_present": sorted(report["instances"]),
    }
    if counted != len(all_rows):
        print(f"WARNING: summary counts {counted} != csv rows {len(all_rows)}", file=sys.stderr)

    (OUT / "performance.json").write_text(json.dumps(report, indent=2) + "\n")

    if all_rows:
        cols = sorted({k for r in all_rows for k in r})
        with (OUT / "trades.csv").open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=cols)
            writer.writeheader()
            writer.writerows(all_rows)

    total = sum(v.get("closed_trades", 0) for v in report["instances"].values())
    print(f"exported {len(all_rows)} rows ({total} closed) from {len(dbs)} database(s)")
    for name, s in report["instances"].items():
        print(f"  {name}: {s.get('closed_trades',0)} closed, {s.get('open_trades',0)} open"
              + (f", PF {s['profit_factor']}" if s.get("profit_factor") else ""))
    print(f"→ {OUT.relative_to(ROOT)}/performance.json and trades.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
