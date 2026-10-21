"""Grok X-sentiment feed.

Grok's live access to X is the one thing it offers that a pure price feed
cannot. It enters the composite as ONE bounded input (weight capped at 0.15 by
the lever constitution) and is never a standalone trigger.

Scores decay to neutral once stale, and every failure path yields 0.0 — a
sentiment outage must never stop the bot or bias it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import aiohttp

from core import levers, settings as settings_mod
from core.grok_agent import _extract_json

# Sentiment uses the Responses endpoint rather than chat completions: live
# search moved to the Agent Tools API, and the older search_parameters field
# now returns HTTP 410.
XAI_RESPONSES_ENDPOINT = "https://api.x.ai/v1/responses"

log = logging.getLogger(__name__)

SENTIMENT_PROMPT = """You score crypto market sentiment from live X/Twitter activity.

For each asset given, search recent posts (last 6 hours) from credible crypto
accounts and score sentiment from -1.0 (extreme fear / heavy bearish flow) to
+1.0 (extreme greed / heavy bullish flow). 0.0 means neutral or insufficient signal.

Be conservative: return 0.0 when chatter is thin, generic, or dominated by
promotional spam. Do not score based on price action alone — you are measuring
crowd positioning and narrative, not the chart.

Respond with ONLY JSON: {"BTC/USDT": 0.2, "ETH/USDT": -0.1, "note": "<=80 chars"}"""


def _responses_text(data: dict) -> str | None:
    """Pull assistant text out of a /v1/responses payload.

    The Responses shape differs from chat completions and carries tool-call
    items alongside the message, so this walks the output list for the message
    content rather than assuming a fixed index. Tolerates the convenience
    ``output_text`` field when present.
    """
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    for item in data.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") not in (None, "message"):
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
        for part in content or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    # Fall back to the chat-completions shape in case the endpoint is proxied.
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None


@dataclass
class SentimentReading:
    """A per-pair sentiment score with the timestamp it was taken at."""

    scores: dict[str, float]
    taken_at: float
    note: str = ""

    def score_for(self, pair: str, now: float | None = None) -> float:
        """Return the score for ``pair``, decayed to 0.0 once stale.

        Staleness is linear over the max-age window: a reading half past its
        shelf life counts for half. This avoids a cliff edge where a delayed
        refresh would abruptly change the composite.
        """
        now = now if now is not None else time.time()
        age = now - self.taken_at
        if age >= levers.SENTIMENT_MAX_AGE_SEC:
            return 0.0
        decay = 1.0 - (age / levers.SENTIMENT_MAX_AGE_SEC)
        return float(self.scores.get(pair, 0.0)) * decay


EMPTY_READING = SentimentReading(scores={}, taken_at=0.0, note="no reading yet")


class SentimentFeed:
    """Periodically refreshes X sentiment for the active pairs."""

    def __init__(self, settings: settings_mod.Settings) -> None:
        self._settings = settings
        self._reading = EMPTY_READING
        self._lock = asyncio.Lock()

    @property
    def reading(self) -> SentimentReading:
        return self._reading

    async def refresh(self, pairs: list[str]) -> SentimentReading:
        """Fetch a fresh sentiment reading. Never raises; failures keep the old one."""
        payload = {
            "model": self._settings.xai_model,
            "input": [
                {"role": "system", "content": SENTIMENT_PROMPT},
                {"role": "user", "content": f"Score these assets: {', '.join(pairs)}"},
            ],
            # x_search is the whole reason sentiment runs through Grok rather
            # than being computed from price. web_search adds context when X is
            # thin. Tool invocations are billed on top of tokens, which is why
            # this runs half-hourly and not per candle.
            "tools": [{"type": "x_search"}, {"type": "web_search"}],
        }
        headers = {
            "Authorization": f"Bearer {self._settings.xai_api_key}",
            "Content-Type": "application/json",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=90)  # tool calls are slower
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(XAI_RESPONSES_ENDPOINT, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning("Sentiment refresh HTTP %s: %s", resp.status, body[:200])
                        return self._reading
                    data = await resp.json()
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            log.warning("Sentiment refresh failed: %s", exc)
            return self._reading

        content = _responses_text(data)
        if content is None:
            log.warning("Malformed sentiment envelope: %s", str(data)[:200])
            return self._reading

        parsed = _extract_json(content)
        if parsed is None:
            log.warning("Unparseable sentiment response: %s", str(content)[:200])
            return self._reading

        scores: dict[str, float] = {}
        for pair in pairs:
            try:
                value = float(parsed.get(pair, 0.0))
            except (TypeError, ValueError):
                value = 0.0
            scores[pair] = max(-1.0, min(1.0, value))

        async with self._lock:
            self._reading = SentimentReading(
                scores=scores, taken_at=time.time(), note=str(parsed.get("note", ""))[:80]
            )
        log.info("Sentiment updated: %s", json.dumps(scores))
        return self._reading

    async def run(self, pairs_provider, interval_sec: int = 1800) -> None:
        """Background loop: refresh sentiment on an interval, forever."""
        while True:
            try:
                await self.refresh(pairs_provider())
            except Exception:  # noqa: BLE001 - a background task must never die
                log.exception("Unexpected error in sentiment loop")
            await asyncio.sleep(interval_sec)
