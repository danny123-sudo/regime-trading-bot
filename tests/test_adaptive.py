"""Adaptive layer tests — the airlock between council advice and live trading.

The critical property, asserted several ways: no proposal can move a risk
constant, and the bot keeps running with sane levers when every council
artifact is missing or corrupt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from core import levers as config
from core import settings as settings_mod
from core.adaptive import AdaptiveConfig


class FakeAlerter:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Redirect every council artifact path into a temp directory."""
    context = tmp_path / "context"
    context.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "CONTEXT_DIR", context)
    monkeypatch.setattr(config, "PROPOSAL_PATH", context / "strategy_proposal.json")
    monkeypatch.setattr(config, "DAILY_BRIEF_PATH", context / "daily_brief.json")
    monkeypatch.setattr(config, "STRATEGY_CONFIG_PATH", tmp_path / "strategy_config.json")
    monkeypatch.setattr(config, "STRATEGY_CONFIG_PREV_PATH", tmp_path / "strategy_config.prev.json")
    from core import adaptive
    monkeypatch.setattr(adaptive, "ADAPTIVE_STATE_PATH", data / "adaptive_state.json")
    return tmp_path


def _write_proposal(secret: str, levers: dict, rationale: str = "test") -> None:
    body = {"levers": levers, "rationale": rationale}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    body["signature"] = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    config.PROPOSAL_PATH.write_text(json.dumps(body))


def _make(settings, workspace, evaluator=None, secret="test-secret"):
    object.__setattr__(settings, "council_secret", secret)
    return AdaptiveConfig(settings, alerter=FakeAlerter(), evaluator=evaluator)


async def _always_better(levers):
    """Evaluator that passes anything — isolates the non-backtest gates."""
    return 1.0 if levers.get("composite_threshold") != 0.65 else 0.0


# -- defaults and fail-safe behaviour ---------------------------------------

def test_defaults_load_when_no_config_file_exists(settings, workspace) -> None:
    adaptive = _make(settings, workspace)
    assert adaptive.get("composite_threshold") == 0.65
    assert adaptive.get("atr_stop_mult") == 1.5


def test_corrupt_config_keeps_last_known_good(settings, workspace) -> None:
    adaptive = _make(settings, workspace)
    config.STRATEGY_CONFIG_PATH.write_text("{not json at all")
    adaptive.reload()
    assert adaptive.get("composite_threshold") == 0.65


def test_missing_context_directory_is_survivable(settings, workspace) -> None:
    adaptive = _make(settings, workspace)
    assert adaptive.daily_brief() == {}
    assert adaptive.weights_for("TRENDING")["ema"] == 1.0


def test_stale_daily_brief_is_ignored(settings, workspace) -> None:
    adaptive = _make(settings, workspace)
    config.DAILY_BRIEF_PATH.write_text(json.dumps({"macro": "CPI today"}))
    import os
    old = time.time() - config.DAILY_BRIEF_MAX_AGE_SEC - 60
    os.utime(config.DAILY_BRIEF_PATH, (old, old))
    assert adaptive.daily_brief() == {}


def test_fresh_daily_brief_is_read(settings, workspace) -> None:
    adaptive = _make(settings, workspace)
    config.DAILY_BRIEF_PATH.write_text(json.dumps({"macro": "CPI today"}))
    assert adaptive.daily_brief()["macro"] == "CPI today"


# -- validation gates --------------------------------------------------------

@pytest.mark.asyncio
async def test_forbidden_key_rejects_the_whole_proposal(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    _write_proposal(settings.council_secret, {
        "composite_threshold": 0.60, "MAX_POSITION_PCT": 0.5,
    })
    outcome = await adaptive.process_proposal()
    assert outcome == "rejected: forbidden keys"
    # The legitimate lever in the same proposal must not have been applied.
    assert adaptive.get("composite_threshold") == 0.65


@pytest.mark.asyncio
async def test_nested_forbidden_key_is_also_caught(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    _write_proposal(settings.council_secret, {
        "regime_weights": {"TRENDING": {"ema": 0.5}}, "nested": {"MAX_LEVERAGE": 10},
    })
    assert await adaptive.process_proposal() == "rejected: forbidden keys"


@pytest.mark.asyncio
async def test_bad_signature_is_rejected(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    config.PROPOSAL_PATH.write_text(json.dumps({
        "levers": {"composite_threshold": 0.60}, "signature": "forged",
    }))
    assert await adaptive.process_proposal() == "rejected: bad signature"


@pytest.mark.asyncio
async def test_out_of_bounds_value_is_clamped_not_applied_raw(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    _write_proposal(settings.council_secret, {"composite_threshold": 0.10})
    await adaptive.process_proposal()
    # 0.10 is below the floor, so it must land exactly on the bound.
    lo, _ = config.LEVER_BOUNDS["composite_threshold"]
    assert adaptive.get("composite_threshold") == lo


@pytest.mark.asyncio
async def test_min_confidence_can_be_raised_but_not_lowered(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    _write_proposal(settings.council_secret, {"min_confidence": 0.50})
    await adaptive.process_proposal()
    assert adaptive.get("min_confidence") >= config.MIN_CONFIDENCE_FLOOR


@pytest.mark.asyncio
async def test_too_many_levers_exceeds_the_change_budget(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    _write_proposal(settings.council_secret, {
        "composite_threshold": 0.60, "atr_stop_mult": 2.0,
        "volume_filter_mult": 1.5, "size_multiplier": 0.5,
    })
    assert await adaptive.process_proposal() == "rejected: too many levers"


@pytest.mark.asyncio
async def test_pair_universe_is_not_a_lever(settings, workspace) -> None:
    """The tradeable universe belongs to freqtrade's pairlist, not the council."""
    adaptive = _make(settings, workspace, evaluator=_always_better)
    _write_proposal(settings.council_secret, {"active_pairs": ["BTC/USDT", "DOGE/USDT"]})
    await adaptive.process_proposal()
    assert "active_pairs" not in adaptive.levers


@pytest.mark.asyncio
async def test_failed_backtest_blocks_the_change(settings, workspace) -> None:
    async def worse(levers):
        return 0.5 if levers.get("composite_threshold") == 0.65 else 0.1

    adaptive = _make(settings, workspace, evaluator=worse)
    _write_proposal(settings.council_secret, {"composite_threshold": 0.75})
    assert await adaptive.process_proposal() == "rejected: failed backtest"
    assert adaptive.get("composite_threshold") == 0.65


@pytest.mark.asyncio
async def test_good_proposal_is_applied_and_persisted(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    _write_proposal(settings.council_secret, {"composite_threshold": 0.70})
    outcome = await adaptive.process_proposal()
    assert outcome.startswith("applied")
    assert adaptive.get("composite_threshold") == 0.70
    assert json.loads(config.STRATEGY_CONFIG_PATH.read_text())["composite_threshold"] == 0.70


@pytest.mark.asyncio
async def test_cooldown_blocks_a_second_change(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    _write_proposal(settings.council_secret, {"composite_threshold": 0.70})
    await adaptive.process_proposal()
    _write_proposal(settings.council_secret, {"composite_threshold": 0.72})
    assert await adaptive.process_proposal() == "rejected: cooldown"


@pytest.mark.asyncio
async def test_proposal_is_consumed_so_it_cannot_apply_twice(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    _write_proposal(settings.council_secret, {"composite_threshold": 0.70})
    await adaptive.process_proposal()
    assert not config.PROPOSAL_PATH.exists()
    assert await adaptive.process_proposal() is None


# -- watchdog ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_underperformance_triggers_auto_revert(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    _write_proposal(settings.council_secret, {"composite_threshold": 0.70})
    await adaptive.process_proposal()
    adaptive.arm_watchdog(baseline_expectancy=1.0, trades_so_far=0)

    reverted = await adaptive.check_watchdog(
        current_expectancy=0.2, trades_so_far=config.ADAPTIVE_REVERT_WINDOW_TRADES
    )
    assert reverted
    assert adaptive.get("composite_threshold") == 0.65


@pytest.mark.asyncio
async def test_watchdog_holds_a_change_that_performs(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    _write_proposal(settings.council_secret, {"composite_threshold": 0.70})
    await adaptive.process_proposal()
    adaptive.arm_watchdog(baseline_expectancy=1.0, trades_so_far=0)

    reverted = await adaptive.check_watchdog(
        current_expectancy=1.5, trades_so_far=config.ADAPTIVE_REVERT_WINDOW_TRADES
    )
    assert not reverted
    assert adaptive.get("composite_threshold") == 0.70


@pytest.mark.asyncio
async def test_watchdog_waits_for_the_window_to_complete(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    _write_proposal(settings.council_secret, {"composite_threshold": 0.70})
    await adaptive.process_proposal()
    adaptive.arm_watchdog(baseline_expectancy=1.0, trades_so_far=0)
    assert not await adaptive.check_watchdog(current_expectancy=0.0, trades_so_far=3)
    assert adaptive.get("composite_threshold") == 0.70


@pytest.mark.asyncio
async def test_two_reverts_freeze_the_layer(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    for threshold in (0.70, 0.72):
        adaptive._state["last_change_at"] = 0.0  # bypass cooldown for the test
        _write_proposal(settings.council_secret, {"composite_threshold": threshold})
        await adaptive.process_proposal()
        adaptive.arm_watchdog(baseline_expectancy=1.0, trades_so_far=0)
        await adaptive.check_watchdog(0.0, config.ADAPTIVE_REVERT_WINDOW_TRADES)

    assert adaptive.frozen
    adaptive._state["last_change_at"] = 0.0
    _write_proposal(settings.council_secret, {"composite_threshold": 0.75})
    assert await adaptive.process_proposal() == "rejected: frozen"


@pytest.mark.asyncio
async def test_unfreeze_restores_the_layer(settings, workspace) -> None:
    adaptive = _make(settings, workspace, evaluator=_always_better)
    adaptive._state["frozen"] = True
    adaptive.unfreeze()
    assert not adaptive.frozen
