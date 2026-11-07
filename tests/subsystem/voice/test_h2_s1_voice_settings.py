"""H2 S1: the four Talk voices, their readiness and the saved voice+model."""
from __future__ import annotations

import json

import pytest


def test_four_talk_voices_are_offered_in_proof_order_with_models():
    from row_bot.voice.runtime import TALK_VOICES

    assert list(TALK_VOICES) == [
        "gemini_live",
        "openai_realtime",
        "gpt_live",
        "local",
    ]
    assert [voice["label"] for voice in TALK_VOICES.values()] == [
        "Gemini Live",
        "OpenAI Realtime",
        "GPT-Live",
        "Local",
    ]
    assert TALK_VOICES["gemini_live"]["model"] == "gemini-3.8-live"
    assert TALK_VOICES["openai_realtime"]["model"] == "gpt-realtime-2.1"
    assert TALK_VOICES["gpt_live"]["model"] == "gpt-live-1"
    assert TALK_VOICES["local"]["model"] == ""


def test_a_fresh_profile_picks_gemini_live_and_never_falls_back():
    from row_bot.voice.runtime import VoiceRuntimeSettings

    settings = VoiceRuntimeSettings()
    assert settings.talk_provider == "gemini_live"
    assert settings.talk_model == "gemini-3.8-live"
    assert settings.realtime_fallback_to_local is False


def test_readiness_names_the_missing_key(monkeypatch):
    from row_bot.application import settings_snapshot

    configured = {"GOOGLE_API_KEY": True, "OPENAI_API_KEY": False}
    monkeypatch.setattr(
        settings_snapshot,
        "_credential_status",
        lambda name: {"configured": configured[name]},
    )

    rows = {row["value"]: row for row in settings_snapshot._talk_provider_rows()}

    assert rows["gemini_live"]["status"] == "Gemini Live: ready"
    assert rows["openai_realtime"]["status"] == (
        "OpenAI Realtime: needs an OpenAI API key"
    )
    assert rows["gpt_live"]["status"] == "GPT-Live: needs an OpenAI API key"
    assert rows["local"]["status"] == "Local: ready"
    assert rows["openai_realtime"]["ready"] is False
    assert rows["local"]["ready"] is True


def test_readiness_with_both_keys_says_ready(monkeypatch):
    from row_bot.application import settings_snapshot

    monkeypatch.setattr(
        settings_snapshot,
        "_credential_status",
        lambda name: {"configured": True},
    )

    rows = settings_snapshot._talk_provider_rows()

    assert all(row["ready"] is True for row in rows)
    assert [row["status"] for row in rows] == [
        "Gemini Live: ready",
        "OpenAI Realtime: ready",
        "GPT-Live: ready",
        "Local: ready",
    ]


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("gemini_live", "gemini-3.8-live"),
        ("openai_realtime", "gpt-realtime-2.1"),
        ("gpt_live", "gpt-live-1"),
        ("local", ""),
    ],
)
def test_the_picker_accepts_every_voice(tmp_path, provider, model):
    from row_bot.application.settings_commands import _normal_value
    from row_bot.voice.runtime import talk_model_for

    _, field, value, secret = _normal_value(
        "voice", "runtime.talk_provider", provider
    )

    assert (field, value, secret) == ("runtime.talk_provider", provider, False)
    assert talk_model_for(provider) == model


def test_an_unknown_talk_voice_is_refused():
    from row_bot.application.settings_commands import (
        SettingsCommandError,
        _normal_value,
    )

    with pytest.raises(SettingsCommandError, match="invalid_settings_command"):
        _normal_value("voice", "runtime.talk_provider", "some_other_voice")


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("gemini_live", "gemini-3.8-live"),
        ("openai_realtime", "gpt-realtime-2.1"),
        ("gpt_live", "gpt-live-1"),
        ("local", "local-whisper"),
    ],
)
def test_picking_a_voice_writes_its_provider_model_and_keeps_fallback_off(
    tmp_path, monkeypatch, provider, model
):
    from row_bot.application.settings_commands import _write_json_setting

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    _write_json_setting(tmp_path, "voice", "runtime.talk_provider", provider)

    saved = json.loads(
        (tmp_path / "voice_runtime_settings.json").read_text(encoding="utf-8")
    )
    assert saved["talk_provider"] == provider
    assert saved["talk_model"] == model
    assert saved.get("realtime_fallback_to_local") is False


def test_local_talk_still_uses_local_whisper():
    from row_bot.voice.providers import LocalWhisperProvider, speech_to_text_provider
    from row_bot.voice.runtime import VoiceRuntimeSettings

    class Voice:
        is_running = False
        state = "stopped"

        def whisper_model_available(self) -> bool:
            return True

        def sync_saved_models(self) -> None:
            pass

        def sensevoice_provider(self):  # pragma: no cover - not selected here
            raise AssertionError("Local picks Whisper")

        def transcribe_pcm16(self, audio_bytes: bytes, *, allow_download: bool) -> str:
            return "hello browser"

    provider = speech_to_text_provider(
        "talk",
        voice_service=Voice(),  # type: ignore[arg-type]
        settings=VoiceRuntimeSettings(
            talk_provider="local", talk_model="local-whisper"
        ),
    )
    assert isinstance(provider, LocalWhisperProvider)


def test_dictate_is_unchanged_when_talk_picks_a_cloud_voice():
    from row_bot.voice.providers import LocalWhisperProvider, speech_to_text_provider
    from row_bot.voice.runtime import VoiceRuntimeSettings

    class Voice:
        is_running = False
        state = "stopped"

        def whisper_model_available(self) -> bool:
            return True

        def sync_saved_models(self) -> None:
            pass

    provider = speech_to_text_provider(
        "dictate",
        voice_service=Voice(),  # type: ignore[arg-type]
        settings=VoiceRuntimeSettings(
            talk_provider="gemini_live",
            dictation_provider="local",
            dictation_model="local-whisper",
        ),
    )
    assert isinstance(provider, LocalWhisperProvider)


@pytest.mark.parametrize("provider", ["gemini_live", "gpt_live"])
def test_a_cloud_pick_before_s2_stops_instead_of_using_local(provider):
    from row_bot.voice.provider_base import VoiceProviderError
    from row_bot.voice.providers import speech_to_text_provider
    from row_bot.voice.runtime import VoiceRuntimeSettings

    class Voice:
        is_running = False
        state = "stopped"

        def sync_saved_models(self) -> None:  # pragma: no cover - must not run
            raise AssertionError("a cloud pick never reaches the local factory")

    with pytest.raises(VoiceProviderError, match="voice_provider_unavailable"):
        speech_to_text_provider(
            "talk",
            voice_service=Voice(),  # type: ignore[arg-type]
            settings=VoiceRuntimeSettings(talk_provider=provider),
        )


def test_realtime_factory_builds_gemini_and_openai_and_refuses_the_rest(monkeypatch):
    from row_bot.voice.client_transport import DictationError
    from row_bot.voice import runtime

    monkeypatch.setattr(
        runtime,
        "load_voice_runtime_settings",
        lambda: runtime.VoiceRuntimeSettings(
            talk_provider="openai_realtime",
            talk_model="gpt-realtime-2.1",
        ),
    )
    from row_bot.application.client_voice import realtime_provider

    provider = realtime_provider()
    assert provider.provider_id == "openai_realtime"
    assert provider.model == "gpt-realtime-2.1"

    monkeypatch.setattr(
        runtime,
        "load_voice_runtime_settings",
        lambda: runtime.VoiceRuntimeSettings(
            talk_provider="gemini_live", talk_model="gemini-3.8-live"
        ),
    )
    gemini = realtime_provider()
    assert gemini.provider_id == "gemini_live"
    assert gemini.model == "gemini-3.8-live"

    for provider_name in ("gpt_live", "local"):
        monkeypatch.setattr(
            runtime,
            "load_voice_runtime_settings",
            lambda provider_name=provider_name: runtime.VoiceRuntimeSettings(
                talk_provider=provider_name
            ),
        )
        with pytest.raises(DictationError, match="voice_provider_unavailable"):
            realtime_provider()
