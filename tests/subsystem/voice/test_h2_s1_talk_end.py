"""H2 S1: the one Talk session-end line (R6), its cost and token rounding."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
import threading
from uuid import uuid4

import pytest

from row_bot.voice.client_realtime import (
    ClientRealtimeTransport,
    RealtimeEvent,
    _RealtimeState,
    _ended_line,
    _format_tokens,
    _voice_cost,
)
from row_bot.voice.client_transport import ClientDictationTransport, DictationOwner
from row_bot.voice.coordinator import VoiceSessionCoordinator


@pytest.mark.parametrize(
    ("count", "shown"),
    [
        (0, "0"),
        (7, "7"),
        (999, "999"),
        (1000, "1k"),
        (1499, "1k"),
        (1500, "2k"),
        (65_500, "66k"),
        (66_000, "66k"),
        (1_234_567, "1235k"),
    ],
)
def test_agent_tokens_are_rounded_half_up_to_thousands(count, shown):
    assert _format_tokens(count) == shown


@pytest.mark.parametrize(
    ("voice", "minutes", "spoken", "reported", "cost"),
    [
        ("gemini_live", 20.0, 600.0, None, 0.28),
        ("gemini_live", 10.0, 0.0, None, 0.05),
        ("gpt_live", 10.0, 600.0, None, 0.50),
        ("openai_realtime", 20.0, 600.0, 0.42, 0.42),
        ("local", 20.0, 600.0, None, 0.0),
    ],
)
def test_cost_follows_the_listed_price_for_each_voice(
    voice, minutes, spoken, reported, cost
):
    assert _voice_cost(voice, minutes, spoken, reported) == pytest.approx(cost)


def test_the_session_line_reads_exactly_as_the_proof_expects():
    state = _RealtimeState(
        "",
        0.0,
        voice="gemini_live",
        model="gemini-3.8-live",
        handovers=14,
    )

    line = _ended_line(
        state, minutes=21.0, spoken_seconds=600.0, tokens=66_000, reported_cost=None
    )

    assert line == (
        "talk ended: gemini_live, gemini-3.8-live, 21.0 min, 14 handovers, "
        "66k agent tokens, voice about $0.29"
    )


def test_gpt_live_and_openai_realtime_lines_name_their_own_voice_and_model():
    gpt = _ended_line(
        _RealtimeState("", 0.0, voice="gpt_live", model="gpt-live-1", handovers=2),
        minutes=10.0,
        spoken_seconds=600.0,
        tokens=1_500,
        reported_cost=None,
    )
    assert gpt == (
        "talk ended: gpt_live, gpt-live-1, 10.0 min, 2 handovers, "
        "2k agent tokens, voice about $0.50"
    )

    openai = _ended_line(
        _RealtimeState(
            "", 0.0, voice="openai_realtime", model="gpt-realtime-2.1", handovers=3
        ),
        minutes=20.0,
        spoken_seconds=600.0,
        tokens=999,
        reported_cost=0.42,
    )
    assert openai == (
        "talk ended: openai_realtime, gpt-realtime-2.1, 20.0 min, 3 handovers, "
        "999 agent tokens, voice about $0.42"
    )


class _Runtime:
    def __init__(self) -> None:
        self.clock = [0.0]
        self.generation = [None]
        self.calls: list[tuple] = []

        def secret(**kwargs):
            self.calls.append(("secret", kwargs))
            return {"value": "synthetic-ephemeral", "expires_at": 1060}

        provider = SimpleNamespace(
            provider_id="openai_realtime",
            model="gpt-realtime-2.1",
            create_client_secret=secret,
        )
        self.provider = provider
        self.coordinator = VoiceSessionCoordinator(
            SimpleNamespace(is_running=False)
        )
        self.transport = ClientDictationTransport(
            coordinator=self.coordinator,
            browser_service=lambda: pytest.fail("no local audio"),
            clock=lambda: self.clock[0],
        )

        def submit(owner, text, event_id, validate):
            validate()
            self.generation[0] = SimpleNamespace(
                generation_id="run-1", status="running", stop_event=threading.Event()
            )
            return "run-1"

        def control(owner, expected, action, text, event_id, validate):
            validate()
            return {"control": action, "status": "running"}

        self.adapter = ClientRealtimeTransport(
            dictation=self.transport,
            provider_factory=lambda: provider,
            submit=submit,
            active_generation=lambda owner: self.generation[0],
            control_generation=control,
            wall_clock=lambda: 1000.0 + self.clock[0],
        )
        self.owner = DictationOwner(str(uuid4()), "local:synthetic", "A", "epoch")

    def start(self):
        return self.adapter.start(
            self.owner, request_id=str(uuid4()), validate=lambda: None
        )

    def event(self, handle, kind, **kwargs):
        return asyncio.run(
            self.adapter.event(
                self.owner,
                handle,
                RealtimeEvent(str(uuid4()), kind, **kwargs),
                validate=lambda: None,
            )
        )


def test_stop_writes_one_line_with_minutes_handovers_and_usage_costs(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    from row_bot.prompt_cache import ModelUsage
    from row_bot.providers.usage_ledger import UsageRecord, record_model_usage

    runtime = _Runtime()
    handle = runtime.start().snapshot.handle

    runtime.event(
        handle,
        "function_call_ready",
        call_id="call-1",
        name="row_bot_agent_consult",
        arguments='{"request":"What is waiting on me?"}',
    )
    runtime.event(
        handle,
        "function_call_ready",
        generation_id="run-1",
        call_id="call-2",
        name="row_bot_agent_control",
        arguments='{"action":"status"}',
    )
    # Speak for 600 s, then let 21 minutes pass before Stop.
    runtime.coordinator._realtime_spoken_seconds = 600.0
    runtime.clock[0] = 21 * 60
    assert record_model_usage(
        UsageRecord(
            purpose="other",
            provider="openai",
            model="gpt-6-luna",
            usage=ModelUsage(
                input_tokens=60_000, output_tokens=6_000, cost=0.29
            ),
            created_at=1000.0,
        )
    )

    caplog.set_level(logging.INFO, logger="row_bot.voice.talk")
    runtime.adapter.stop(runtime.owner, handle, validate=lambda: None)

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "row_bot.voice.talk"
    ]
    assert lines == [
        "talk ended: openai_realtime, gpt-realtime-2.1, 21.0 min, "
        "2 handovers, 66k agent tokens, voice about $0.29"
    ]


def test_stop_without_a_provider_session_writes_no_line(caplog):
    runtime = _Runtime()
    runtime.provider.create_client_secret = lambda **kwargs: {
        "value": "synthetic-ephemeral",
        "expires_at": 1060,
    }
    handle = runtime.start().snapshot.handle
    # A lease whose provider never named a voice (e.g. an interrupted start)
    # must not print a half-empty line.
    runtime.coordinator._dictation_lease.realtime.voice = ""

    caplog.set_level(logging.INFO, logger="row_bot.voice.talk")
    runtime.adapter.stop(runtime.owner, handle, validate=lambda: None)

    assert not [r for r in caplog.records if r.name == "row_bot.voice.talk"]
