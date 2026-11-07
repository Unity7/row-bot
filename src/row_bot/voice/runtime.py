from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from row_bot.data_paths import get_row_bot_data_dir
from row_bot.voice.openai_realtime import (
    DEFAULT_REALTIME_VOICE,
    REALTIME_VOICE_OPTIONS,
)

# The four Talk voices, in the order Settings offers them.  A value names the
# saved setting; the label is what a person reads; the model is what the saved
# talk_model holds (Local has no cloud model); the key is what readiness needs.
TALK_VOICES: dict[str, dict[str, str]] = {
    "gemini_live": {
        "label": "Gemini Live",
        "model": "gemini-3.8-live",
        "key": "GOOGLE_API_KEY",
    },
    "openai_realtime": {
        "label": "OpenAI Realtime",
        "model": "gpt-realtime-2.1",
        "key": "OPENAI_API_KEY",
    },
    "gpt_live": {
        "label": "GPT-Live",
        "model": "gpt-live-1",
        "key": "OPENAI_API_KEY",
    },
    "local": {"label": "Local", "model": "", "key": ""},
}
DEFAULT_TALK_PROVIDER = "gemini_live"
DEFAULT_TALK_MODEL = TALK_VOICES[DEFAULT_TALK_PROVIDER]["model"]


def talk_provider_label(provider: str) -> str:
    return TALK_VOICES.get(str(provider), {}).get("label", str(provider))


def talk_model_for(provider: str) -> str:
    """The model saved beside a Talk voice (empty for Local)."""
    return TALK_VOICES.get(str(provider), {}).get("model", "")


def talk_provider_key_name(provider: str) -> str:
    """The API key a Talk voice needs, or empty when it needs none."""
    return TALK_VOICES.get(str(provider), {}).get("key", "")


def _settings_path():
    return get_row_bot_data_dir() / "voice_runtime_settings.json"


@dataclass
class VoiceRuntimeSettings:
    talk_provider: str = DEFAULT_TALK_PROVIDER
    talk_model: str = DEFAULT_TALK_MODEL
    dictation_provider: str = "local"
    dictation_model: str = "local-whisper"
    speech_output_provider: str = "local"
    speech_output_model: str = "local-kokoro"
    speech_output_voice: str = "af_heart"
    realtime_voice: str = DEFAULT_REALTIME_VOICE
    captions_enabled: bool = True
    talk_auto_start: bool = False
    realtime_fallback_to_local: bool = False


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _coerce_realtime_voice(value: Any) -> str:
    voice = str(value or DEFAULT_REALTIME_VOICE)
    if voice in REALTIME_VOICE_OPTIONS:
        return voice
    return DEFAULT_REALTIME_VOICE


def load_voice_runtime_settings() -> VoiceRuntimeSettings:
    settings_path = _settings_path()
    try:
        if settings_path.exists():
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                provider = str(data.get("talk_provider") or DEFAULT_TALK_PROVIDER)
                return VoiceRuntimeSettings(
                    talk_provider=provider,
                    talk_model=str(data.get("talk_model") or talk_model_for(provider)),
                    dictation_provider=str(data.get("dictation_provider") or "local"),
                    dictation_model=str(data.get("dictation_model") or "local-whisper"),
                    speech_output_provider=str(data.get("speech_output_provider") or "local"),
                    speech_output_model=str(data.get("speech_output_model") or "local-kokoro"),
                    speech_output_voice=str(data.get("speech_output_voice") or "af_heart"),
                    realtime_voice=_coerce_realtime_voice(data.get("realtime_voice")),
                    captions_enabled=_coerce_bool(data.get("captions_enabled"), True),
                    talk_auto_start=_coerce_bool(data.get("talk_auto_start"), False),
                    realtime_fallback_to_local=_coerce_bool(data.get("realtime_fallback_to_local"), False),
                )
    except Exception:
        pass
    return VoiceRuntimeSettings()
