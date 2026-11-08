"""H2 S2: Gemini Live's server token, browser hand-offs, cancel, resume and cap."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from uuid import uuid4

import pytest

from row_bot.voice.client_realtime import RealtimeEvent
from row_bot.voice.client_transport import DictationError
from tests.subsystem.voice.test_client_realtime import runtime, start  # noqa: F401

pytestmark = pytest.mark.subsystem


class _Response:
    def __init__(self, value: dict):
        self.value = value

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.value


def _gemini():
    from row_bot.voice.gemini_live import GeminiLiveProvider

    return GeminiLiveProvider(api_key="synthetic-google-key")


def test_the_mint_body_locks_the_session_and_names_the_voice(monkeypatch, caplog):
    from row_bot.voice import gemini_live

    calls: list[tuple[str, dict]] = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response({"name": "auth_tokens/synthetic", "expireTime": "2099-01-01T00:00:00Z"})

    monkeypatch.setattr(gemini_live.requests, "post", post)
    caplog.set_level(logging.INFO, logger="row_bot.voice.talk")
    provider = _gemini()
    value = provider.create_client_secret(expires_after_seconds=60, reason="start")

    assert value["value"] == "auth_tokens/synthetic"
    url, kwargs = calls[0]
    assert url == "https://generativelanguage.googleapis.com/v1beta/auth_tokens"
    assert kwargs["headers"]["x-goog-api-key"] == "synthetic-google-key"
    assert "synthetic-google-key" not in json.dumps(kwargs["json"])
    body = kwargs["json"]
    assert body["uses"] == 1
    setup = body["bidiGenerateContentSetup"]
    assert setup["model"] == "models/gemini-3.8-live"
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    assert setup["inputAudioTranscription"] == {}
    assert setup["outputAudioTranscription"] == {}
    assert setup["contextWindowCompression"] == {"slidingWindow": {}}
    declarations = setup["tools"][0]["functionDeclarations"]
    names = [item["name"] for item in declarations]
    assert names == ["row_bot_agent_consult", "row_bot_agent_control", "wait_for_user"]
    assert all(item["behavior"] == "NON_BLOCKING" for item in declarations)
    consult = declarations[0]["parameters"]
    assert consult["type"] == "OBJECT"
    assert consult["properties"]["request"]["type"] == "STRING"
    assert consult["required"] == ["request"]
    assert "additionalProperties" not in consult
    assert "additionalProperties" not in consult["properties"]["request"]
    mask = body["fieldMask"].split(",")
    assert "sessionResumption" not in mask, "a locked sessionResumption would drop the resume handle"
    assert "sessionResumption" not in setup, "the browser, not the token, carries resumption"
    assert set(setup) == set(mask), "every setup field must be named in the field mask, or Google refuses the token"
    assert {"model", "generationConfig", "systemInstruction", "tools", "inputAudioTranscription",
            "outputAudioTranscription", "contextWindowCompression"} <= set(mask)
    assert caplog.records[-1].getMessage() == (
        "gemini token minted: model=gemini-3.8-live voice=Kore "
        "tools=row_bot_agent_consult,row_bot_agent_control,wait_for_user "
        "non_blocking=yes transcription=in,out resumption=on uses=1 reason=start"
    )

    provider.create_client_secret(expires_after_seconds=60, reason="resume")
    assert caplog.records[-1].getMessage().endswith("reason=resume")


def test_a_saved_openai_voice_is_not_sent_to_gemini(monkeypatch):
    from row_bot.voice import gemini_live

    posted = []
    monkeypatch.setattr(gemini_live.requests, "post",
                        lambda url, **kwargs: posted.append(kwargs["json"]) or _Response({"name": "auth_tokens/x"}))
    provider = gemini_live.GeminiLiveProvider(api_key="synthetic", voice="marin")
    provider.create_client_secret()
    assert posted[0]["bidiGenerateContentSetup"]["generationConfig"]["speechConfig"][
        "voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    assert gemini_live.gemini_voice("Kore") == "Kore"


def test_a_saved_gemini_model_is_the_one_locked_and_logged(monkeypatch, caplog):
    from row_bot.voice import gemini_live

    posted = []
    monkeypatch.setattr(gemini_live.requests, "post",
                        lambda url, **kwargs: posted.append(kwargs["json"]) or _Response({"name": "auth_tokens/x"}))
    caplog.set_level(logging.INFO, logger="row_bot.voice.talk")
    provider = gemini_live.GeminiLiveProvider(api_key="synthetic", model="gemini-3.8-flash")

    assert provider.token_payload()["bidiGenerateContentSetup"]["model"] == "models/gemini-3.8-flash"
    provider.create_client_secret()
    assert posted[0]["bidiGenerateContentSetup"]["model"] == "models/gemini-3.8-flash"
    assert caplog.records[-1].getMessage().startswith("gemini token minted: model=gemini-3.8-flash ")


def test_the_start_answer_names_gemini_and_openai(runtime, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ROWBOT_GEMINI_CONNECTION_SECONDS", raising=False)
    r = runtime
    r.provider.provider_id = "gemini_live"
    r.provider.model = "gemini-3.8-live"
    gemini = start(r)
    assert gemini.snapshot.transport == "gemini_live"
    assert gemini.model == "gemini-3.8-live"
    assert gemini.client_secret == "synthetic-ephemeral"
    assert gemini.connection_seconds == 540

    r.adapter.stop(r.owner, gemini.snapshot.handle, validate=lambda: None)
    r.provider.provider_id = "openai_realtime"
    r.provider.model = "gpt-realtime-2.1"
    openai = start(r)
    assert openai.snapshot.transport == "openai_realtime"
    assert openai.model == "gpt-realtime-2.1"
    assert openai.connection_seconds == 0


def test_the_resume_token_mints_again_and_logs_the_same_session(runtime, caplog):  # noqa: F811
    r = runtime
    r.provider.provider_id = "gemini_live"
    r.provider.model = "gemini-3.8-live"
    answers = iter([
        {"value": "auth_tokens/one", "expires_at": 1060},
        {"value": "auth_tokens/two", "expires_at": 1060},
        {"value": "auth_tokens/three", "expires_at": 1060},
    ])
    r.provider.create_client_secret = lambda **kwargs: next(answers)
    handle = start(r).snapshot.handle
    asyncio.run(r.adapter.event(r.owner, handle, RealtimeEvent(str(uuid4()), "connected"), validate=lambda: None))
    caplog.set_level(logging.INFO, logger="row_bot.voice.talk")
    resumed = r.adapter.token(r.owner, handle, validate=lambda: None)
    assert resumed.client_secret == "auth_tokens/two"
    assert resumed.snapshot.transport == "gemini_live"
    line = caplog.records[-1].getMessage()
    assert line == f"talk resumed: voice=gemini_live session={handle.lease_id} connection=2"

    r.adapter.stop(r.owner, handle, validate=lambda: None)
    r.provider.provider_id = "openai_realtime"
    r.provider.model = "gpt-realtime-2.1"
    openai = start(r).snapshot.handle
    with pytest.raises(DictationError, match="voice_provider_unavailable"):
        r.adapter.token(r.owner, openai, validate=lambda: None)


def test_only_gemini_keeps_talking_past_ten_minutes(runtime):  # noqa: F811
    r = runtime
    r.provider.provider_id = "gemini_live"
    r.provider.model = "gemini-3.8-live"
    handle = start(r).snapshot.handle
    r.coordinator._dictation_lease.expires = 700
    r.clock[0] = 590
    r.adapter.snapshot(r.owner, handle, validate=lambda: None, heartbeat=True)
    r.clock[0] = 650
    assert r.adapter.snapshot(r.owner, handle, validate=lambda: None).expires_in_ms == 60_000
    r.adapter.stop(r.owner, handle, validate=lambda: None)
    r.clock[0] = 0
    r.provider.provider_id = "openai_realtime"
    r.provider.model = "gpt-realtime-2.1"
    other = start(r).snapshot.handle
    r.coordinator._dictation_lease.expires = 700
    r.clock[0] = 590
    r.adapter.snapshot(r.owner, other, validate=lambda: None, heartbeat=True)
    r.clock[0] = 650
    assert r.adapter.snapshot(r.owner, other, validate=lambda: None).expires_in_ms == 0


def test_a_handover_line_carries_the_call_id_and_request_size(runtime, caplog):  # noqa: F811
    r = runtime
    r.provider.provider_id = "gemini_live"
    r.provider.model = "gemini-3.8-live"
    handle = start(r).snapshot.handle
    caplog.set_level(logging.INFO, logger="row_bot.voice.talk")
    request = "What is waiting on me?"
    asyncio.run(r.adapter.event(
        r.owner,
        handle,
        RealtimeEvent(str(uuid4()), "function_call_ready", call_id="call-9",
                      name="row_bot_agent_consult", arguments=json.dumps({"request": request})),
        validate=lambda: None,
    ))
    assert caplog.records[-1].getMessage() == (
        f"talk handover: voice=gemini_live call=call-9 tool=row_bot_agent_consult "
        f"request_chars={len(request)}"
    )


def test_cancel_says_stopped_only_after_the_confirmed_line(runtime, caplog):  # noqa: F811
    r = runtime
    r.provider.provider_id = "gemini_live"
    r.provider.model = "gemini-3.8-live"
    handle = start(r).snapshot.handle
    asyncio.run(r.adapter.event(
        r.owner, handle,
        RealtimeEvent(str(uuid4()), "function_call_ready", call_id="consult",
                      name="row_bot_agent_consult", arguments='{"request":"Go through every project"}'),
        validate=lambda: None,
    ))
    caplog.set_level(logging.INFO, logger="row_bot.voice.talk")
    seen: list[str] = []

    def control(owner, expected, action, text, event_id, validate):
        validate()
        seen.append(action)
        assert not [record for record in caplog.records if "talk cancel confirmed" in record.getMessage()]
        expected.stop_event.set()
        expected.status = "stopped"
        expected.producer_done.set()
        return {"control": "cancel", "status": "cancelled"}

    r.adapter._control = control
    result = asyncio.run(r.adapter.event(
        r.owner, handle,
        RealtimeEvent(str(uuid4()), "function_call_ready", generation_id="run-1", call_id="cancel",
                      name="row_bot_agent_control", arguments='{"action":"cancel"}'),
        validate=lambda: None,
    ))
    assert seen == ["cancel"]
    assert json.loads(result.function_output)["speakable"] == "Stopped."
    line = [record.getMessage() for record in caplog.records if "talk cancel confirmed" in record.getMessage()]
    assert line == [
        "talk cancel confirmed: voice=gemini_live run=run-1 via=conversation.stop status=stopped"
    ]


def _consult(r, handle):
    asyncio.run(r.adapter.event(
        r.owner, handle,
        RealtimeEvent(str(uuid4()), "function_call_ready", call_id="consult",
                      name="row_bot_agent_consult", arguments='{"request":"Go through every project"}'),
        validate=lambda: None,
    ))


def _cancel(r, handle):
    return asyncio.run(r.adapter.event(
        r.owner, handle,
        RealtimeEvent(str(uuid4()), "function_call_ready", generation_id="run-1", call_id="cancel",
                      name="row_bot_agent_control", arguments='{"action":"cancel"}'),
        validate=lambda: None,
    ))


def test_cancel_still_stopping_when_the_run_ends_late_but_in_time(runtime, caplog, monkeypatch):  # noqa: F811
    from row_bot.voice import client_realtime

    monkeypatch.setattr(client_realtime, "CANCEL_CONFIRM_SECONDS", 2.0)
    r = runtime
    r.provider.provider_id = "gemini_live"
    r.provider.model = "gemini-3.8-live"
    handle = start(r).snapshot.handle
    _consult(r, handle)
    caplog.set_level(logging.INFO, logger="row_bot.voice.talk")

    def control(owner, expected, action, text, event_id, validate):
        validate()
        expected.stop_event.set()

        def finish():
            expected.status = "stopped"
            expected.producer_done.set()

        threading.Timer(0.2, finish).start()
        return {"control": "cancel", "status": "cancelled"}

    r.adapter._control = control
    result = _cancel(r, handle)
    assert json.loads(result.function_output)["status"] == "cancelled"
    assert json.loads(result.function_output)["speakable"] == "Stopped."
    assert [record.getMessage() for record in caplog.records if "talk cancel" in record.getMessage()] == [
        "talk cancel confirmed: voice=gemini_live run=run-1 via=conversation.stop status=stopped"
    ]


def test_cancel_says_still_stopping_when_the_run_never_ends_in_time(runtime, caplog, monkeypatch):  # noqa: F811
    from row_bot.voice import client_realtime

    monkeypatch.setattr(client_realtime, "CANCEL_CONFIRM_SECONDS", 0.15)
    r = runtime
    r.provider.provider_id = "gemini_live"
    r.provider.model = "gemini-3.8-live"
    handle = start(r).snapshot.handle
    _consult(r, handle)
    caplog.set_level(logging.INFO, logger="row_bot.voice.talk")

    def control(owner, expected, action, text, event_id, validate):
        validate()
        expected.stop_event.set()
        return {"control": "cancel", "status": "cancel_requested"}

    r.adapter._control = control
    result = _cancel(r, handle)
    assert json.loads(result.function_output)["status"] == "stopping"
    assert json.loads(result.function_output)["speakable"] == "Still stopping."
    lines = [record.getMessage() for record in caplog.records if "talk cancel" in record.getMessage()]
    assert lines == [
        "talk cancel pending: voice=gemini_live run=run-1 via=conversation.stop status=stopping"
    ]


def test_cancel_on_a_run_that_is_already_gone_says_stopped(runtime, caplog):  # noqa: F811
    r = runtime
    r.provider.provider_id = "gemini_live"
    r.provider.model = "gemini-3.8-live"
    handle = start(r).snapshot.handle
    _consult(r, handle)
    r.generation[0] = None
    caplog.set_level(logging.INFO, logger="row_bot.voice.talk")

    def control(owner, expected, action, text, event_id, validate):
        validate()
        assert expected is None
        return {"control": "cancel", "status": "cancel_requested"}

    r.adapter._control = control
    result = _cancel(r, handle)
    assert json.loads(result.function_output)["status"] == "cancelled"
    assert json.loads(result.function_output)["speakable"] == "Stopped."
    assert [record.getMessage() for record in caplog.records if "talk cancel" in record.getMessage()] == [
        "talk cancel confirmed: voice=gemini_live run=run-1 via=conversation.stop status=cancelled"
    ]


ROOT = "/api/v1/conversations/conversation-A/voice/realtime"


@pytest.fixture
def api(runtime, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    from row_bot.runtime import admissions
    from tests.subsystem.client_protocol.test_protocol_security import bootstrap, client_app

    monkeypatch.setattr(admissions, "deletion_state", lambda _: "active")
    monkeypatch.delenv("ROWBOT_GEMINI_CONNECTION_SECONDS", raising=False)
    client, service, active = client_app(remote=True)
    service.realtime = runtime.adapter
    service._metadata = lambda _: {"thread_id": "conversation-A"}
    service.voice_admission = SimpleNamespace(
        prepare_context=lambda owner, context, validate: (validate(), dict(context))[1],
        validate_context=lambda owner, context, validate: validate(),
        active=lambda owner: None,
        submit=lambda *args, **kwargs: "run-1",
        control=lambda *args, **kwargs: {},
    )
    with client:
        handshake, headers = bootstrap(client)
        yield SimpleNamespace(client=client, headers=headers, runtime=runtime)


def _talk_body() -> dict:
    return {
        "request_id": str(uuid4()),
        "conversation_revision": "0",
        "model_selection": {"provider_id": "fixture", "model_ref": "model:fixture:chat"},
        "write_targets": [],
    }


@pytest.mark.parametrize(("transport", "secret", "exchange"), [
    ("gemini_live", "synthetic-ephemeral", False),
    ("openai_realtime", None, True),
])
def test_the_start_route_keeps_only_geminis_token(api, transport, secret, exchange):
    api.runtime.provider.provider_id = transport
    api.runtime.provider.model = "gemini-3.8-live" if transport == "gemini_live" else "gpt-realtime-2.1"
    response = api.client.post(ROOT, headers=api.headers, json=_talk_body())
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["snapshot"]["transport"] == transport
    assert value["model"] == api.runtime.provider.model
    assert value["client_secret"] == secret
    assert value.get("exchange_available", False) is exchange
    assert value["connection_seconds"] == (540 if transport == "gemini_live" else 0)


def test_the_token_route_answers_a_fresh_secret(api):
    api.runtime.provider.provider_id = "gemini_live"
    api.runtime.provider.model = "gemini-3.8-live"
    answers = iter([
        {"value": "auth_tokens/one", "expires_at": 1060},
        {"value": "auth_tokens/two", "expires_at": 1060},
    ])
    api.runtime.provider.create_client_secret = lambda **kwargs: next(answers)
    started = api.client.post(ROOT, headers=api.headers, json=_talk_body())
    assert started.status_code == 200, started.text
    snapshot = started.json()["snapshot"]
    token = api.client.post(
        f"{ROOT}/{snapshot['handle']['lease_id']}/token",
        headers=api.headers,
        json={"voice_session_id": snapshot["handle"]["voice_session_id"],
              "server_epoch": snapshot["handle"]["server_epoch"]},
    )
    assert token.status_code == 200, token.text
    value = token.json()
    assert value["client_secret"] == "auth_tokens/two"
    assert value["snapshot"]["transport"] == "gemini_live"
    assert value["connection_seconds"] == 540


def test_a_cloud_pick_remembers_the_local_engine_and_local_restores_it(tmp_path, monkeypatch):
    from row_bot.application.settings_commands import _write_json_setting
    from row_bot.voice.runtime import load_voice_runtime_settings

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    (tmp_path / "voice_runtime_settings.json").write_text(
        json.dumps({"talk_provider": "local", "talk_model": "local-funasr-sensevoice"}),
        encoding="utf-8",
    )
    _write_json_setting(tmp_path, "voice", "runtime.talk_provider", "gemini_live")
    cloud = json.loads((tmp_path / "voice_runtime_settings.json").read_text(encoding="utf-8"))
    assert cloud["talk_model"] == "gemini-3.8-live"
    assert cloud["talk_local_model"] == "local-funasr-sensevoice"

    _write_json_setting(tmp_path, "voice", "runtime.talk_provider", "local")
    local = json.loads((tmp_path / "voice_runtime_settings.json").read_text(encoding="utf-8"))
    assert local["talk_model"] == "local-funasr-sensevoice"
    settings = load_voice_runtime_settings()
    assert settings.talk_provider == "local"
    assert settings.talk_model == "local-funasr-sensevoice"


def test_the_token_route_is_promised_by_the_contract_generator():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "client_contract_generator", root / "scripts/generate_client_platform_contracts.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    promised = {(method, path) for method, path, _, _ in module.OPERATIONS}
    assert ("post", "/conversations/{conversation_id}/voice/realtime/{lease_id}/token") in promised
