"""Grok decision layer — the in-loop LLM confirmation step.

Called ONLY when the composite score crosses the conviction threshold, so at
3-8 calls per day this costs a few dollars a month. Everything it returns is
advisory: the hardcoded risk gate runs after this and can veto anything.

Failure is always safe. Timeout, malformed JSON, unknown action, low
confidence, or an oversized request all resolve to HOLD.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from core import levers, settings as settings_mod

log = logging.getLogger(__name__)

XAI_ENDPOINT = "https://api.x.ai/v1/chat/completions"

VALID_ACTIONS = {"buy", "short", "exit", "hold"}

SYSTEM_PROMPT = """You are the confirmation layer of a systematic crypto trading bot.

A deterministic indicator engine has already decided a setup is worth examining.
Your job is NOT to find trades — it is to veto weak ones. Default to "hold".

Rules you must respect:
- You are a confirmation step, not a strategist. If the evidence is merely
  adequate, return "hold". Only high-quality setups deserve confidence >= 0.70.
- Fees are ~0.16% round trip. A trade needs meaningful expected movement.
- Never recommend an action the mode forbids: in conservative mode the only
  valid actions are "buy", "exit", "hold".
- Confidence is your honest probability that this trade is positive-expectancy.
  Do not inflate it. Sub-0.70 responses are discarded, which is a fine outcome.

Respond with ONLY a JSON object, no prose, no markdown fence:
{"action": "buy"|"short"|"exit"|"hold", "size_pct": 0.0-0.08,
 "order_type": "limit", "limit_price": number, "rationale": "<=100 chars",
 "confidence": 0.0-1.0}"""


@dataclass
class Decision:
    """A validated decision from Grok."""

    action: str
    size_pct: float
    order_type: str
    limit_price: float
    rationale: str
    confidence: float

    @property
    def is_entry(self) -> bool:
        return self.action in {"buy", "short"}

    def confidence_size_factor(self) -> float:
        """Confidence tiering: 0.70-0.79 -> 50%, 0.80-0.89 -> 75%, 0.90+ -> 100%."""
        if self.confidence >= 0.90:
            return 1.0
        if self.confidence >= 0.80:
            return 0.75
        return 0.5


def _hold(reason: str) -> Decision:
    return Decision("hold", 0.0, "limit", 0.0, reason[:100], 0.0)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model response, tolerating stray formatting."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


class GrokAgent:
    """Thin, defensive client for the xAI chat completions API."""

    def __init__(self, settings: settings_mod.Settings, alerter: Any | None = None) -> None:
        self._settings = settings
        self._alerter = alerter
        self._consecutive_failures = 0

    async def decide(self, context: dict[str, Any]) -> Decision:
        """Ask Grok to confirm or veto a candidate setup.

        Args:
            context: Serialisable decision context (regime, scores, indicators,
                positions, performance, fees, counters, funding, daily brief).

        Returns:
            A validated Decision. Never raises — all failures return HOLD.
        """
        payload = {
            "model": self._settings.xai_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(context, default=str)},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._settings.xai_api_key}",
            "Content-Type": "application/json",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=levers.GROK_TIMEOUT_SEC)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(XAI_ENDPOINT, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return await self._fail(f"xAI HTTP {resp.status}: {body[:200]}")
                    data = await resp.json()
        except asyncio.TimeoutError:
            return await self._fail("xAI request timed out")
        except aiohttp.ClientError as exc:
            return await self._fail(f"xAI client error: {exc}")

        self._consecutive_failures = 0

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return _hold("malformed xAI envelope")

        parsed = _extract_json(content)
        if parsed is None:
            log.warning("Grok returned unparseable content: %s", content[:300])
            return _hold("unparseable JSON from Grok")

        return self._validate(parsed)

    def _validate(self, parsed: dict[str, Any]) -> Decision:
        """Enforce the response contract. Anything off-contract becomes HOLD."""
        action = str(parsed.get("action", "")).lower().strip()
        if action not in VALID_ACTIONS:
            log.warning("Grok returned unknown action %r", action)
            return _hold(f"unknown action {action!r}")

        # A short in conservative mode is a contract violation, not a trade.
        if action == "short" and self._settings.mode is settings_mod.Mode.CONSERVATIVE:
            log.warning("Grok proposed a short in conservative mode; holding")
            return _hold("short proposed in conservative mode")

        try:
            confidence = float(parsed.get("confidence", 0.0))
            size_pct = float(parsed.get("size_pct", 0.0))
            limit_price = float(parsed.get("limit_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            return _hold("non-numeric fields in Grok response")

        if not 0.0 <= confidence <= 1.0:
            return _hold(f"confidence out of range: {confidence}")

        # The confidence floor is a hard gate; the council may raise it but the
        # constitution's floor still applies here.
        if action != "exit" and confidence < levers.MIN_CONFIDENCE_FLOOR:
            return _hold(f"confidence {confidence:.2f} below floor")

        if size_pct < 0 or size_pct > levers.MAX_POSITION_PCT:
            log.warning("Grok requested size_pct %.4f; clamping", size_pct)
            size_pct = min(max(size_pct, 0.0), levers.MAX_POSITION_PCT)

        rationale = str(parsed.get("rationale", ""))[:100]
        order_type = str(parsed.get("order_type", "limit")).lower()
        if order_type not in {"limit", "market"}:
            order_type = "limit"

        return Decision(action, size_pct, order_type, limit_price, rationale, confidence)

    async def _fail(self, message: str) -> Decision:
        """Record a failure, alerting once a streak indicates a real outage."""
        self._consecutive_failures += 1
        log.error("Grok call failed (%d in a row): %s", self._consecutive_failures, message)
        if self._consecutive_failures >= levers.GROK_FAILURE_ALERT_THRESHOLD and self._alerter:
            await self._alerter.send(
                f"⚠️ Grok API failing: {self._consecutive_failures} consecutive errors.\n"
                f"Last: {message}\nBot continues in HOLD — no entries until this clears."
            )
        return _hold("grok unavailable")
