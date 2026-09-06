"""The gateway path must report context metering, not just raw token counts.

The WebUI talks to the agent through the Hermes gateway, and that path was
dropping everything the context indicator needs. ``_gateway_stream_usage``
kept three fields — ``input_tokens``, ``output_tokens``, ``estimated_cost`` —
and the success writeback persisted none of them, so:

* the ring had no numerator (``last_prompt_tokens`` was never sent) and drew
  "no data" for every gateway-backed turn, and
* a reload found every counter at zero and hid the indicator entirely.

The numerator was in the payload all along: an OpenAI-shaped response reports
``prompt_tokens``, which *is* the size of the context submitted for the turn.
The denominator is not in any gateway response — no OpenAI-compatible API
returns a model's context window — so it is resolved locally from model
metadata, the same source the in-process path uses.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_CHAT = ROOT / "api" / "gateway_chat.py"


@pytest.fixture()
def gateway_chat():
    import api.gateway_chat as module

    return module


class _StubSession:
    """The subset of Session the usage fold touches."""

    def __init__(self, **overrides):
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.context_length = 0
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.post_compression_context_tokens_estimate = None
        for key, value in overrides.items():
            setattr(self, key, value)


# ── The numerator: prompt_tokens must reach the indicator ────────────────────

def test_stream_usage_carries_last_prompt_tokens(gateway_chat):
    usage = gateway_chat._gateway_stream_usage(
        {"usage": {"prompt_tokens": 160_000, "completion_tokens": 4_000}}
    )

    assert usage["last_prompt_tokens"] == 160_000, (
        "the ring divides last_prompt_tokens by the window; without it every "
        "gateway turn renders the no-data dot"
    )
    assert usage["input_tokens"] == 160_000
    assert usage["output_tokens"] == 4_000


def test_stream_usage_accepts_openai_and_anthropic_cache_shapes(gateway_chat):
    openai_shaped = gateway_chat._gateway_stream_usage(
        {"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 40}}}
    )
    anthropic_shaped = gateway_chat._gateway_stream_usage(
        {"usage": {
            "prompt_tokens": 100,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 12,
        }}
    )

    assert openai_shaped["cache_read_tokens"] == 40
    assert anthropic_shaped["cache_read_tokens"] == 40
    assert anthropic_shaped["cache_write_tokens"] == 12


def test_stream_usage_survives_unparseable_counts(gateway_chat):
    """A malformed count must not abort a turn that otherwise completed."""
    usage = gateway_chat._gateway_stream_usage(
        {"usage": {"prompt_tokens": "not-a-number", "completion_tokens": 12.7}}
    )

    assert usage["input_tokens"] == 0
    assert usage["last_prompt_tokens"] == 0
    assert usage["output_tokens"] == 12


def test_stream_usage_ignores_payloads_without_usage(gateway_chat):
    assert gateway_chat._gateway_stream_usage({"choices": []}) == {}
    assert gateway_chat._gateway_stream_usage({"usage": None}) == {}


# ── The denominator: resolved locally, never reported by the gateway ─────────

def test_context_length_resolves_from_model_metadata(gateway_chat):
    fake_metadata = MagicMock()
    fake_metadata.get_model_context_length = MagicMock(return_value=1_000_000)

    with patch.dict(sys.modules, {"agent.model_metadata": fake_metadata}):
        resolved = gateway_chat._gateway_context_length({}, "deepseek-v4-pro", "deepseek")

    assert resolved == 1_000_000
    called_model = fake_metadata.get_model_context_length.call_args.args[0]
    assert called_model == "deepseek-v4-pro"


def test_context_length_falls_back_to_zero_without_the_agent_bundle(gateway_chat):
    """A WebUI running without the agent bundle must not break the turn."""
    fake_metadata = MagicMock()
    fake_metadata.get_model_context_length = MagicMock(side_effect=RuntimeError("no bundle"))

    with patch.dict(sys.modules, {"agent.model_metadata": fake_metadata}):
        assert gateway_chat._gateway_context_length({}, "some-model", "") == 0


def test_context_length_skips_lookup_for_an_empty_model(gateway_chat):
    fake_metadata = MagicMock()
    fake_metadata.get_model_context_length = MagicMock(return_value=256_000)

    with patch.dict(sys.modules, {"agent.model_metadata": fake_metadata}):
        assert gateway_chat._gateway_context_length({}, "", "") == 0

    fake_metadata.get_model_context_length.assert_not_called()


# ── Folding a turn into the session ──────────────────────────────────────────

def _fold(gateway_chat, session, usage, context_length=200_000):
    fake_metadata = MagicMock()
    fake_metadata.get_model_context_length = MagicMock(return_value=context_length)
    with patch.dict(sys.modules, {"agent.model_metadata": fake_metadata}):
        return gateway_chat._apply_gateway_usage_to_session(
            session, usage, cfg={}, model="test-model", model_provider="test",
        )


def test_turn_usage_is_persisted_so_a_reload_still_has_a_meter(gateway_chat):
    session = _StubSession(threshold_tokens=180_000)
    usage = gateway_chat._gateway_stream_usage(
        {"usage": {"prompt_tokens": 160_000, "completion_tokens": 4_000, "estimated_cost": 0.42}}
    )

    out = _fold(gateway_chat, session, usage)

    assert session.last_prompt_tokens == 160_000
    assert session.context_length == 200_000
    assert session.input_tokens == 160_000
    assert session.output_tokens == 4_000
    assert session.estimated_cost == 0.42
    # …and the same numbers ride the done event for the live indicator.
    assert out["last_prompt_tokens"] == 160_000
    assert out["context_length"] == 200_000
    assert out["threshold_tokens"] == 180_000


def test_session_totals_accumulate_while_last_prompt_tracks_the_latest_turn(gateway_chat):
    """Gateway responses are per-request; the session stores cumulative totals."""
    session = _StubSession()

    _fold(gateway_chat, session, gateway_chat._gateway_stream_usage(
        {"usage": {"prompt_tokens": 160_000, "completion_tokens": 4_000}}))
    second = _fold(gateway_chat, session, gateway_chat._gateway_stream_usage(
        {"usage": {"prompt_tokens": 172_000, "completion_tokens": 1_200}}))

    assert session.input_tokens == 332_000
    assert session.output_tokens == 5_200
    # The window share is about the *last* request, not the running total —
    # dividing a cumulative counter by the window is the #1436 bug.
    assert session.last_prompt_tokens == 172_000
    assert second["last_prompt_tokens"] == 172_000
    assert second["input_tokens"] == 332_000


def test_cache_counters_produce_a_hit_percentage(gateway_chat):
    session = _StubSession()
    usage = gateway_chat._gateway_stream_usage({"usage": {
        "prompt_tokens": 100_000,
        "prompt_tokens_details": {"cached_tokens": 50_000},
    }})

    out = _fold(gateway_chat, session, usage)

    assert session.cache_read_tokens == 50_000
    assert out["cache_hit_percent"] == 50


def test_unknown_window_leaves_the_session_value_alone(gateway_chat):
    """An unresolvable model must not wipe a window we already knew."""
    session = _StubSession(context_length=200_000)
    usage = gateway_chat._gateway_stream_usage({"usage": {"prompt_tokens": 1_000}})

    out = _fold(gateway_chat, session, usage, context_length=0)

    assert session.context_length == 200_000
    assert out["context_length"] == 200_000


# ── Wiring ───────────────────────────────────────────────────────────────────

def test_success_writeback_folds_usage_before_saving():
    src = GATEWAY_CHAT.read_text(encoding="utf-8")
    fold_at = src.index("usage = _apply_gateway_usage_to_session(")
    save_at = src.index("success_writeback_committed = True")
    done_at = src.index('put_gateway_event("done"')

    assert fold_at < save_at, (
        "the fold must run before the session is written, or the persisted "
        "counters stay at zero and a reload shows an empty meter"
    )
    assert fold_at < done_at, (
        "the done event carries the enriched usage the indicator reads"
    )
