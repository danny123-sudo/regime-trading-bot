"""Record entry signals that fired but could not become trades.

`trades.csv` is a survivor book. A signal blocked by a full slot table, an
active cooldown, or a council stand-down leaves no row anywhere, so neither
hyperopt nor the council can see it — and a filter that wrongly blocks trades
looks identical to a market with no opportunities.

Two things are worth separating. Strategy filters (`p_use_vol`, `p_use_ema`,
`p_use_btc_filter`, …) *are* hyperopt parameters and were searched: the
ablation turned each on and off and measured the result. What was never in the
search space is everything downstream of the signal — freqtrade's protections,
`max_open_trades`, and the council's `enabled` flag. Those are what this logs.

Live and dry-run only. In a backtest this would be millions of rows across
every pair and candle, and backtests already vary the searchable filters
directly.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SKIPLOG = ROOT / "council" / "data" / "skips.jsonl"
MAX_BYTES = 5 * 1024 * 1024   # rotate rather than grow without bound


# Last state recorded per (strategy, reason, pair). freqtrade polls every few
# seconds, so logging each poll would write thousands of identical rows a day
# and bury the transitions that actually carry information.
_LAST: dict[tuple[str, str, str], str] = {}


def record(
    *,
    strategy: str,
    pair: str,
    reason: str,
    slots_used: int | None = None,
    slots_max: int | None = None,
    detail: str = "",
) -> None:
    """Append one blocked-signal row. Never raises — logging must not stop trading."""
    key = (strategy, reason, pair)
    state = f"{slots_used}/{slots_max}|{detail}"
    if _LAST.get(key) == state:
        return   # unchanged since last poll; the transition is already on record
    _LAST[key] = state

    try:
        SKIPLOG.parent.mkdir(parents=True, exist_ok=True)
        if SKIPLOG.exists() and SKIPLOG.stat().st_size > MAX_BYTES:
            SKIPLOG.replace(SKIPLOG.with_suffix(".jsonl.1"))
        row = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "strategy": strategy,
            "pair": pair,
            "reason": reason,
            "slots_used": slots_used,
            "slots_max": slots_max,
        }
        if detail:
            row["detail"] = detail[:200]
        # Mark this as the moment the condition began, not a sample of it.
        row["event"] = "entered"
        with SKIPLOG.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def clear(strategy: str, reason: str, pair: str = "*") -> None:
    """Forget a condition so its next occurrence is recorded as a new event."""
    _LAST.pop((strategy, reason, pair), None)


def is_backtest(config: dict) -> bool:
    mode = str(config.get("runmode", "")).lower()
    return any(m in mode for m in ("backtest", "hyperopt", "plot", "edge", "util"))
