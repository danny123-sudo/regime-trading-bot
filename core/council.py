"""Council levers for the live strategies.

The lever board is per-strategy and namespaced, because CoreTrend and
MomentumCore have almost nothing in common beyond their exits.

Bounds here are deliberately tighter than a naive reading would suggest, for a
reason we measured: Supertrend at (period 14, multiplier 4.0) returned +74%
over 3.3 years, while (10, 3.0) returned -88% on identical data. Parameter
sensitivity in this system is violent, so the council gets a narrow band around
each validated value rather than the full plausible range. It can nudge; it
cannot wander.

What the council is genuinely useful for, given the research:

  * **Standing down.** `size_multiplier` and `enabled` let it reduce or halt a
    strategy's risk. Nothing forces trades, so "do less" is always available
    and is the single most valuable thing it can decide.
  * **Widening stops** in high-volatility regimes.
  * **Raising entry bars** when conditions look poor.

It cannot raise leverage, lift position caps, enable an unvalidated strategy,
or touch source code. Those live in code and change only through git.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Final

log = logging.getLogger(__name__)

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
CONFIG_PATH: Final[Path] = ROOT / "user_data" / "strategy_config.json"

# Validated defaults — the configs that survived out-of-sample testing.
DEFAULTS: Final[dict[str, dict[str, Any]]] = {
    "_global": {
        "size_multiplier": 1.0,   # scales every position; the main risk dial
    },
    "CoreTrend": {
        "enabled": True,
        "st_period": 14,
        "st_mult": 4.0,
        "donchian": 48,
        "stop_atr": 5.0,
        "trail_atr": 6.0,
        "trail_after_r": 3.0,
        "vol_max": 0.08,
    },
    "MomentumCore": {
        "enabled": True,
        "mom_lookback": 12,
        "mom_min": 0.02,
        "btc_lookback": 24,
        "stop_atr": 4.0,
        "trail_atr": 5.0,
        "trail_after_r": 1.5,
        "vol_max": 0.12,
    },
}

# (min, max) per lever. Narrow by design — see the module docstring.
BOUNDS: Final[dict[str, dict[str, tuple[float, float]]]] = {
    "_global": {
        # Can only ever reduce risk: 1.0 is the ceiling, not a midpoint.
        "size_multiplier": (0.20, 1.0),
    },
    "CoreTrend": {
        "st_period": (11, 18),      # validated at 14
        "st_mult": (3.4, 4.6),      # validated at 4.0 — tight, this is the killer
        "donchian": (30, 70),
        "stop_atr": (3.5, 7.0),
        "trail_atr": (4.0, 8.0),
        "trail_after_r": (1.5, 4.0),
        "vol_max": (0.05, 0.15),
    },
    "MomentumCore": {
        "mom_lookback": (8, 24),
        "mom_min": (0.01, 0.06),
        "btc_lookback": (12, 40),
        "stop_atr": (3.0, 6.0),
        "trail_atr": (3.5, 7.0),
        "trail_after_r": (0.5, 3.0),
        "vol_max": (0.06, 0.20),
    },
}

# Anything resembling these in a proposal rejects the whole thing.
FORBIDDEN: Final[frozenset[str]] = frozenset({
    "dry_run", "stake_amount", "max_open_trades", "available_capital",
    "dry_run_wallet", "leverage", "margin_mode", "trading_mode", "stoploss",
    "api_key", "secret", "password", "token", "chat_id", "exchange",
    "pair_whitelist", "timeframe",
})


def load() -> dict:
    """Read the council's config. Missing or corrupt file falls back to defaults."""
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text())
            if isinstance(data, dict):
                return data
    except (OSError, ValueError) as exc:
        log.warning("strategy_config.json unreadable (%s); using validated defaults", exc)
    return {}


def lever(strategy: str, name: str, config: dict | None = None) -> Any:
    """Resolve one lever: council value if in bounds, else the validated default.

    Out-of-bounds values are clamped rather than rejected, so a bad proposal
    degrades to the nearest safe setting instead of stopping the bot.
    """
    default = DEFAULTS.get(strategy, {}).get(name)
    config = load() if config is None else config
    raw = config.get(strategy, {}).get(name, default)

    if name == "enabled":
        return bool(raw)

    bounds = BOUNDS.get(strategy, {}).get(name)
    if bounds is None or raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default

    lo, hi = bounds
    clamped = max(lo, min(hi, value))
    if clamped != value:
        log.warning("council lever %s.%s=%s clamped to %s", strategy, name, value, clamped)
    return int(clamped) if isinstance(default, int) else clamped


def size_multiplier(config: dict | None = None) -> float:
    """Global risk dial. Reduces position size; can never increase it."""
    config = load() if config is None else config
    try:
        value = float(config.get("_global", {}).get("size_multiplier", 1.0))
    except (TypeError, ValueError):
        return 1.0
    lo, hi = BOUNDS["_global"]["size_multiplier"]
    return max(lo, min(hi, value))


def is_enabled(strategy: str, config: dict | None = None) -> bool:
    """Whether the council currently allows this strategy to open trades.

    Disabling does not close open positions — those keep their own exit logic.
    A regime call is a weaker signal than a strategy's own stop.
    """
    config = load() if config is None else config
    return bool(config.get(strategy, {}).get("enabled", True))


def validate_proposal(proposal: dict) -> tuple[dict, list[str]]:
    """Check a council proposal. Returns (clean_config, messages).

    A proposal touching anything in FORBIDDEN is rejected entirely rather than
    filtered, so an attempt to change position limits cannot be partially
    applied.
    """
    messages: list[str] = []

    def scan(obj: Any, path: str = "") -> list[str]:
        found = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key.lower() in FORBIDDEN:
                    found.append(f"{path}{key}")
                found.extend(scan(value, f"{path}{key}."))
        return found

    forbidden = scan(proposal)
    if forbidden:
        return {}, [f"REJECTED — proposal touches protected settings: {', '.join(forbidden[:5])}"]

    clean: dict = {}
    for strategy, levers in proposal.items():
        if strategy not in DEFAULTS:
            messages.append(f"unknown strategy '{strategy}' ignored")
            continue
        if not isinstance(levers, dict):
            continue
        section: dict = {}
        for name, value in levers.items():
            if name == "enabled":
                section["enabled"] = bool(value)
                continue
            if name not in BOUNDS.get(strategy, {}):
                messages.append(f"{strategy}.{name}: not a lever, ignored")
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                messages.append(f"{strategy}.{name}: non-numeric, ignored")
                continue
            lo, hi = BOUNDS[strategy][name]
            clamped = max(lo, min(hi, numeric))
            if clamped != numeric:
                messages.append(f"{strategy}.{name}: {numeric} clamped to {clamped} (bounds {lo}-{hi})")
            default = DEFAULTS[strategy].get(name)
            section[name] = int(clamped) if isinstance(default, int) else clamped
        if section:
            clean[strategy] = section
    return clean, messages
