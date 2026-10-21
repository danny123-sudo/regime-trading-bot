"""Strategy council intake — the adaptive layer.

The Grok Bot council (SCOUT / ANALYST / RED TEAM / STRATEGIST) writes proposals
as JSON. This module is the airlock between that advice and the running bot.

Four things stand between a proposal and live trading:
  1. signature + schema validation
  2. bounds clamping and forbidden-key rejection
  3. a backtest gate the candidate must beat the incumbent on
  4. an auto-revert watchdog that rolls back anything that degrades expectancy

The council writes data, never code. The bot must always be able to run
indefinitely with no council input at all: every artifact fails safe.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

from core import levers as L, settings as settings_mod

log = logging.getLogger(__name__)

ADAPTIVE_STATE_PATH = L.ADAPTIVE_STATE_PATH

# A backtest evaluator: (levers) -> Sharpe over the evaluation window.
Evaluator = Callable[[dict], Awaitable[float]]


@dataclass
class AppliedChange:
    """A change set applied to the live config, watched for auto-revert."""

    applied_at: float
    levers: list[str]
    rationale: str
    baseline_expectancy: float
    trades_at_apply: int
    previous: dict = field(default_factory=dict)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Merge overlay into a copy of base, recursing into nested dicts."""
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _find_forbidden(obj: Any, path: str = "") -> list[str]:
    """Recursively locate any forbidden key anywhere in a proposal."""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in L.FORBIDDEN_LEVER_KEYS:
                found.append(f"{path}{key}")
            found.extend(_find_forbidden(value, f"{path}{key}."))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            found.extend(_find_forbidden(item, f"{path}[{index}]."))
    return found


class AdaptiveConfig:
    """Owns the live lever set and the proposal pipeline."""

    def __init__(
        self,
        settings: settings_mod.Settings,
        alerter: Any | None = None,
        evaluator: Evaluator | None = None,
    ) -> None:
        self._settings = settings
        self._alerter = alerter
        self._evaluator = evaluator
        self._levers: dict = copy.deepcopy(L.DEFAULT_LEVERS)
        self._mtime: float = 0.0
        self._state: dict = {"last_change_at": 0.0, "consecutive_reverts": 0, "frozen": False}
        self._pending: AppliedChange | None = None
        self._load_state()
        self.reload()

    # -- live levers ------------------------------------------------------
    @property
    def levers(self) -> dict:
        """The current lever set. Always valid, even if every file is missing."""
        return self._levers

    @property
    def frozen(self) -> bool:
        return bool(self._state.get("frozen", False))

    def get(self, name: str, default: Any = None) -> Any:
        return self._levers.get(name, default)

    def weights_for(self, regime_name: str) -> dict[str, float]:
        """Indicator weights for a regime, falling back to code defaults."""
        weights = self._levers.get("regime_weights", {}).get(regime_name)
        if not isinstance(weights, dict) or not weights:
            return dict(L.DEFAULT_LEVERS["regime_weights"][regime_name])
        return {k: float(v) for k, v in weights.items()}

    def regime_enabled(self, regime_name: str) -> bool:
        return bool(self._levers.get("regime_enabled", {}).get(regime_name, True))

    def reload(self) -> bool:
        """Hot-reload strategy_config.json if it changed. Returns True if reloaded.

        A missing, malformed, or unreadable file leaves the last-known-good
        levers in place — the bot keeps trading on code defaults if need be.
        """
        path: Path = L.STRATEGY_CONFIG_PATH
        try:
            if not path.exists():
                return False
            mtime = path.stat().st_mtime
            if mtime == self._mtime:
                return False
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.error("strategy_config.json unreadable (%s); keeping last-known-good", exc)
            return False

        sanitised, _ = self._sanitise(raw)
        self._levers = _deep_merge(L.DEFAULT_LEVERS, sanitised)
        self._mtime = mtime
        log.info("Reloaded strategy_config.json")
        return True

    # -- proposal pipeline -------------------------------------------------
    async def process_proposal(self) -> str | None:
        """Validate and possibly apply a pending council proposal.

        Returns:
            A human-readable outcome string if a proposal was seen, else None.
        """
        path = L.PROPOSAL_PATH
        if not path.exists():
            return None

        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            await self._reject(path, f"unreadable proposal: {exc}")
            return "rejected: unreadable"

        outcome = await self._evaluate_proposal(raw)
        # Consume the proposal either way so it is never applied twice.
        try:
            path.unlink()
        except OSError:
            log.warning("Could not remove consumed proposal at %s", path)
        return outcome

    async def _evaluate_proposal(self, raw: dict) -> str:
        if self.frozen:
            await self._alert("🧊 Adaptive layer is FROZEN after repeated reverts. Proposal ignored.")
            return "rejected: frozen"

        # 1. signature ----------------------------------------------------
        if self._settings.council_secret:
            signature = str(raw.pop("signature", ""))
            body = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
            expected = hmac.new(
                self._settings.council_secret.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                await self._alert("🚨 Strategy proposal REJECTED: bad signature. Not written by the council.")
                return "rejected: bad signature"

        levers = raw.get("levers")
        rationale = str(raw.get("rationale", ""))[:200]
        if not isinstance(levers, dict) or not levers:
            await self._alert("Strategy proposal rejected: no levers block.")
            return "rejected: no levers"

        # 2. forbidden keys -----------------------------------------------
        forbidden = _find_forbidden(levers)
        if forbidden:
            await self._alert(
                "🚨 Strategy proposal REJECTED WHOLESALE — attempted to change "
                f"protected values: {', '.join(forbidden[:5])}"
            )
            return "rejected: forbidden keys"

        # 3. budgets -------------------------------------------------------
        changed = [k for k in levers if k != "regime_weights"] + (
            ["regime_weights"] if "regime_weights" in levers else []
        )
        if len(changed) > L.ADAPTIVE_MAX_LEVERS_PER_CHANGE:
            await self._alert(
                f"Strategy proposal rejected: {len(changed)} levers exceeds the "
                f"{L.ADAPTIVE_MAX_LEVERS_PER_CHANGE}-lever change budget."
            )
            return "rejected: too many levers"

        since_last = time.time() - float(self._state.get("last_change_at", 0.0))
        if since_last < L.ADAPTIVE_CHANGE_COOLDOWN_SEC:
            hours = (L.ADAPTIVE_CHANGE_COOLDOWN_SEC - since_last) / 3600
            await self._alert(f"Strategy proposal deferred: {hours:.1f}h left in change cooldown.")
            return "rejected: cooldown"

        # 4. bounds --------------------------------------------------------
        sanitised, clamps = self._sanitise(levers)
        for message in clamps:
            log.warning("Clamped council proposal: %s", message)
        if clamps:
            await self._alert("⚠️ Council proposal clamped to bounds:\n" + "\n".join(clamps[:5]))

        candidate = _deep_merge(self._levers, sanitised)
        if candidate == self._levers:
            return "no-op: proposal matches live config"

        # 5. backtest gate --------------------------------------------------
        if self._evaluator is not None:
            try:
                incumbent = await self._evaluator(self._levers)
                challenger = await self._evaluator(candidate)
            except Exception:  # noqa: BLE001 - a failed backtest must not apply a change
                log.exception("Backtest gate errored; proposal rejected")
                await self._alert("Strategy proposal rejected: backtest gate errored.")
                return "rejected: backtest error"
            if not challenger > incumbent:
                await self._alert(
                    f"Strategy proposal rejected by backtest gate "
                    f"(Sharpe {challenger:.2f} vs incumbent {incumbent:.2f}).\n{rationale}"
                )
                return "rejected: failed backtest"
        else:
            log.warning("No backtest evaluator wired; applying on bounds validation alone")

        # 6. apply ----------------------------------------------------------
        self._apply(candidate, changed, rationale)
        await self._alert(
            "✅ Strategy config updated by council.\n"
            f"Levers: {', '.join(changed)}\nRationale: {rationale}\n"
            f"Auto-revert watch: {L.ADAPTIVE_REVERT_WINDOW_TRADES} trades / 72h."
        )
        return f"applied: {', '.join(changed)}"

    def _sanitise(self, levers: dict) -> tuple[dict, list[str]]:
        """Clamp every lever into its bounds. Returns (clean_levers, clamp_messages)."""
        clean: dict = {}
        messages: list[str] = []

        for name, value in levers.items():
            if name in L.LEVER_BOUNDS:
                lo, hi = L.LEVER_BOUNDS[name]
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    messages.append(f"{name}: non-numeric {value!r}, ignored")
                    continue
                clamped = max(lo, min(hi, numeric))
                if clamped != numeric:
                    messages.append(f"{name}: {numeric} -> {clamped} (bounds {lo}-{hi})")
                clean[name] = clamped

            elif name == "regime_weights":
                if not isinstance(value, dict):
                    messages.append("regime_weights: not an object, ignored")
                    continue
                lo, hi = L.INDICATOR_WEIGHT_BOUNDS
                weights: dict = {}
                for regime_name, profile in value.items():
                    if regime_name not in L.DEFAULT_LEVERS["regime_weights"]:
                        messages.append(f"regime_weights.{regime_name}: unknown regime, ignored")
                        continue
                    if not isinstance(profile, dict):
                        continue
                    known = L.DEFAULT_LEVERS["regime_weights"][regime_name]
                    cleaned: dict[str, float] = {}
                    for indicator, weight in profile.items():
                        if indicator not in known:
                            messages.append(
                                f"regime_weights.{regime_name}.{indicator}: unknown indicator, ignored"
                            )
                            continue
                        try:
                            numeric = float(weight)
                        except (TypeError, ValueError):
                            continue
                        clamped = max(lo, min(hi, numeric))
                        if clamped != numeric:
                            messages.append(
                                f"regime_weights.{regime_name}.{indicator}: {numeric} -> {clamped}"
                            )
                        cleaned[indicator] = clamped
                    if cleaned:
                        weights[regime_name] = cleaned
                if weights:
                    clean["regime_weights"] = weights

            elif name == "regime_enabled":
                if isinstance(value, dict):
                    clean["regime_enabled"] = {
                        k: bool(v)
                        for k, v in value.items()
                        if k in L.DEFAULT_LEVERS["regime_enabled"]
                    }

            elif name == "pair_blacklist":
                # The council may disable a pair but never enable one: the
                # tradeable universe is freqtrade's VolumePairList, not a lever.
                if isinstance(value, list):
                    clean["pair_blacklist"] = [str(p) for p in value]

            else:
                messages.append(f"{name}: not a recognised lever, ignored")

        return clean, messages

    def _apply(self, candidate: dict, changed: list[str], rationale: str) -> None:
        """Atomically write the new config, retaining the previous version."""
        try:
            if L.STRATEGY_CONFIG_PATH.exists():
                L.STRATEGY_CONFIG_PREV_PATH.write_text(L.STRATEGY_CONFIG_PATH.read_text())
            tmp = L.STRATEGY_CONFIG_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(candidate, indent=2, sort_keys=True))
            tmp.replace(L.STRATEGY_CONFIG_PATH)
        except OSError:
            log.exception("Failed to write strategy config; change not applied")
            return

        self._pending = AppliedChange(
            applied_at=time.time(), levers=changed, rationale=rationale,
            baseline_expectancy=0.0, trades_at_apply=0, previous=copy.deepcopy(self._levers),
        )
        self._levers = candidate
        self._mtime = L.STRATEGY_CONFIG_PATH.stat().st_mtime
        self._state["last_change_at"] = time.time()
        self._save_state()

    # -- auto-revert watchdog ---------------------------------------------
    def arm_watchdog(self, baseline_expectancy: float, trades_so_far: int) -> None:
        """Record the performance baseline a freshly applied change must beat."""
        if self._pending is not None:
            self._pending.baseline_expectancy = baseline_expectancy
            self._pending.trades_at_apply = trades_so_far

    async def check_watchdog(self, current_expectancy: float, trades_so_far: int) -> bool:
        """Revert the pending change if it has underperformed its baseline.

        Returns True if a revert occurred.
        """
        pending = self._pending
        if pending is None:
            return False

        elapsed = time.time() - pending.applied_at
        trades = trades_so_far - pending.trades_at_apply
        window_complete = (
            trades >= L.ADAPTIVE_REVERT_WINDOW_TRADES
            or elapsed >= L.ADAPTIVE_REVERT_WINDOW_SEC
        )
        if not window_complete:
            return False

        if current_expectancy >= pending.baseline_expectancy:
            log.info("Adaptive change held: expectancy %.4f >= baseline %.4f",
                     current_expectancy, pending.baseline_expectancy)
            self._pending = None
            self._state["consecutive_reverts"] = 0
            self._save_state()
            await self._alert(
                f"✅ Council change confirmed after {trades} trades "
                f"(expectancy {current_expectancy:.4f} vs {pending.baseline_expectancy:.4f})."
            )
            return False

        # Underperformed — roll back.
        self._levers = pending.previous
        try:
            tmp = L.STRATEGY_CONFIG_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._levers, indent=2, sort_keys=True))
            tmp.replace(L.STRATEGY_CONFIG_PATH)
            self._mtime = L.STRATEGY_CONFIG_PATH.stat().st_mtime
        except OSError:
            log.exception("Revert write failed; in-memory levers already rolled back")

        self._pending = None
        self._state["consecutive_reverts"] = int(self._state.get("consecutive_reverts", 0)) + 1
        message = (
            f"↩️ AUTO-REVERTED council change ({', '.join(pending.levers)}): "
            f"expectancy {current_expectancy:.4f} < baseline {pending.baseline_expectancy:.4f} "
            f"over {trades} trades."
        )
        if self._state["consecutive_reverts"] >= L.ADAPTIVE_FREEZE_AFTER_REVERTS:
            self._state["frozen"] = True
            message += "\n🧊 Adaptive layer FROZEN after 2 consecutive reverts. Human unfreeze required."
        self._save_state()
        await self._alert(message)
        return True

    def unfreeze(self) -> None:
        """Human-initiated unfreeze after investigating repeated reverts."""
        self._state["frozen"] = False
        self._state["consecutive_reverts"] = 0
        self._save_state()

    # -- daily brief -------------------------------------------------------
    def daily_brief(self) -> dict:
        """Load SCOUT's daily brief, ignoring it if missing or stale."""
        path = L.DAILY_BRIEF_PATH
        try:
            if not path.exists():
                return {}
            if time.time() - path.stat().st_mtime > L.DAILY_BRIEF_MAX_AGE_SEC:
                log.info("Daily brief is stale; ignoring")
                return {}
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Daily brief unreadable (%s); ignoring", exc)
            return {}
        return data if isinstance(data, dict) else {}

    # -- persistence -------------------------------------------------------
    def _load_state(self) -> None:
        try:
            if ADAPTIVE_STATE_PATH.exists():
                self._state.update(json.loads(ADAPTIVE_STATE_PATH.read_text()))
        except (OSError, json.JSONDecodeError):
            log.warning("Adaptive state unreadable; starting fresh")

    def _save_state(self) -> None:
        try:
            ADAPTIVE_STATE_PATH.write_text(json.dumps(self._state, indent=2))
        except OSError:
            log.warning("Could not persist adaptive state")

    async def _alert(self, message: str) -> None:
        log.info("ADAPTIVE: %s", message.replace("\n", " | "))
        if self._alerter:
            await self._alerter.send(message)

    async def _reject(self, path: Path, reason: str) -> None:
        await self._alert(f"Strategy proposal rejected: {reason}")
        try:
            path.unlink()
        except OSError:
            pass
