"""Glue between the freqtrade strategy and our own layers.

Holds the council's levers, the cached sentiment reading, and the Grok
confirmation call, and keeps all three out of the strategy class so the
strategy stays readable and testable.

Everything here fails safe. If the council file is missing, levers fall back to
code defaults. If sentiment is stale or absent, it reads 0. If Grok is
unreachable, the entry is refused rather than waved through — a missing veto
must never become an implicit approval.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable

from core import levers as L
from core.levers import DEFAULT_LEVERS

log = logging.getLogger(__name__)

BACKTEST_MODES = {"backtest", "hyperopt", "edge", "plot", "util_exchange", "other"}


class StrategyState:
    """Lever access, sentiment cache, and the Grok veto, in one place."""

    def __init__(self, runmode: str = "") -> None:
        self.runmode = str(runmode).lower()
        self.is_backtest = any(mode in self.runmode for mode in BACKTEST_MODES)
        self._adaptive: Any | None = None
        self._sentiment: dict[str, float] = {}
        self._grok: Any | None = None
        self._lock = threading.Lock()

        # Levers are read in every mode, but differently: live hot-reloads so
        # the council can act between candles, while a backtest takes one
        # frozen snapshot so an A/B comparison cannot move underneath itself.
        self._frozen_levers = self._load_lever_file()
        if not self.is_backtest:
            self._init_live_layers()

    # Hyperopt fans the strategy out to worker processes with joblib, which
    # pickles it. Locks and live clients are neither picklable nor meaningful
    # in a worker, so drop them and rebuild the lock on arrival.
    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state["_lock"] = None
        state["_adaptive"] = None
        state["_grok"] = None
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._lock = threading.Lock()

    @staticmethod
    def _load_lever_file() -> dict:
        """Read strategy_config.json once. Missing or corrupt file = defaults."""
        import json

        try:
            if L.STRATEGY_CONFIG_PATH.exists():
                data = json.loads(L.STRATEGY_CONFIG_PATH.read_text())
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            log.warning("strategy_config.json unreadable; using code defaults")
        return {}

    def _init_live_layers(self) -> None:
        """Wire the council and Grok. Failures degrade to code defaults."""
        try:
            from core.adaptive import AdaptiveConfig
            from core.grok_agent import GrokAgent
            from core.settings import load_settings

            settings = load_settings(require_xai=False)
            self._adaptive = AdaptiveConfig(settings, alerter=None, evaluator=None)
            if settings.xai_api_key:
                self._grok = GrokAgent(settings, alerter=None)
            else:
                log.warning("No XAI_API_KEY set — Grok confirmation disabled, "
                            "entries will proceed on the deterministic layer alone")
        except Exception:  # noqa: BLE001 - never let optional layers stop trading
            log.exception("Could not initialise adaptive/Grok layers; using code defaults")

    # -- levers ------------------------------------------------------------
    def lever(self, name: str, default: Any = None) -> Any:
        """Read a lever, hot-reloading the council's file when it changes."""
        if self._adaptive is None:
            if name in self._frozen_levers:
                return self._frozen_levers[name]
            return DEFAULT_LEVERS.get(name, default)
        try:
            self._adaptive.reload()
            return self._adaptive.get(name, DEFAULT_LEVERS.get(name, default))
        except Exception:  # noqa: BLE001
            log.exception("Lever read failed for %r; using default", name)
            return DEFAULT_LEVERS.get(name, default)

    def regime_weights(self) -> dict:
        """Full weight map for all regimes."""
        if self._adaptive is None:
            return self._frozen_levers.get("regime_weights", DEFAULT_LEVERS["regime_weights"])
        return {
            name: self._adaptive.weights_for(name)
            for name in DEFAULT_LEVERS["regime_weights"]
        }

    def regime_enabled_map(self) -> dict[str, bool]:
        """Which regime strategies the council currently allows."""
        enabled = self.lever("regime_enabled", DEFAULT_LEVERS["regime_enabled"])
        if not isinstance(enabled, dict):
            enabled = DEFAULT_LEVERS["regime_enabled"]
        return {name: bool(enabled.get(name, True)) for name in DEFAULT_LEVERS["regime_enabled"]}

    # -- sentiment ---------------------------------------------------------
    def sentiment_for(self, pair: str) -> float:
        """Cached X-sentiment for a pair. Always 0.0 in backtests.

        Sentiment has no historical series we can replay, so using it in a
        backtest would be lookahead bias. It is a live-only input.
        """
        if self.is_backtest:
            return 0.0
        with self._lock:
            return float(self._sentiment.get(pair, 0.0))

    def update_sentiment(self, scores: dict[str, float]) -> None:
        """Called by the sentiment refresh task."""
        with self._lock:
            self._sentiment = dict(scores)

    # -- Grok confirmation --------------------------------------------------
    def confirm_entry(self, pair: str, candle: Any, rate: float, lever: Callable) -> bool:
        """Ask Grok to confirm a candidate entry.

        Returns True to allow the trade. A configured-but-unreachable Grok
        returns False: when the confirmation layer is meant to be running and
        is not, the safe reading is "unconfirmed", not "approved".
        """
        if self._grok is None:
            return True  # deliberately not configured; deterministic layer stands alone

        context = {
            "pair": pair,
            "proposed_side": "long",
            "entry_rate": rate,
            "regime": int(candle.get("regime", 0)),
            "composite_score": round(float(candle.get("composite", 0.0)), 3),
            "adx": round(float(candle.get("adx", 0.0)), 2),
            "rsi": round(float(candle.get("rsi", 0.0)), 2),
            "bb_position": round(float(candle.get("bb_position", 0.0)), 3),
            "cloud_position": int(candle.get("cloud_position", 0)),
            "volume_ratio": round(float(candle.get("volume_ratio", 0.0)), 2),
            "obv_divergence": int(candle.get("obv_divergence", 0)),
            "x_sentiment": self.sentiment_for(pair),
            "daily_brief": self._daily_brief(),
        }

        try:
            decision = _run_sync(self._grok.decide(context))
        except Exception:  # noqa: BLE001
            log.exception("Grok confirmation failed for %s; refusing entry", pair)
            return False

        min_confidence = float(lever("min_confidence", L.MIN_CONFIDENCE_FLOOR))
        allowed = decision.action == "buy" and decision.confidence >= min_confidence
        log.info(
            "Grok %s %s: action=%s confidence=%.2f — %s",
            "confirmed" if allowed else "vetoed", pair,
            decision.action, decision.confidence, decision.rationale,
        )
        return allowed

    def _daily_brief(self) -> dict:
        if self._adaptive is None:
            return {}
        try:
            return self._adaptive.daily_brief()
        except Exception:  # noqa: BLE001
            return {}


def _run_sync(coro):
    """Run a coroutine from freqtrade's synchronous strategy callbacks.

    Freqtrade calls strategy hooks from a normal thread with no running loop,
    so a fresh loop is correct here; the fallback covers the case where one
    does exist.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict = {}

    def _worker() -> None:
        result["value"] = asyncio.run(coro)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=L.GROK_TIMEOUT_SEC + 5)
    if "value" not in result:
        raise TimeoutError("Grok call did not complete in time")
    return result["value"]
