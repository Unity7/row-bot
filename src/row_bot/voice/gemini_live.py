"""Gemini Live Talk: the server mints a one-use auth token and the browser connects.

The Google key never leaves this process: the token endpoint locks the model,
audio replies, the voice, Row-Bot's instructions, both transcriptions, session
resumption, context window compression and the three hand-off tools into one
short-lived ``auth_tokens/...`` secret.  The browser sends it as the WebSocket's
``access_token`` and can do nothing else with it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any

import requests

from row_bot.api_keys import get_key
from row_bot.voice.agent_bridge import gemini_bridge_tool_declarations
from row_bot.voice.provider_base import VoiceProviderStatus

logger = logging.getLogger("row_bot.voice.talk")

AUTH_TOKENS_URL = "https://generativelanguage.googleapis.com/v1beta/auth_tokens"
DEFAULT_GEMINI_MODEL = "gemini-3.8-live"
DEFAULT_GEMINI_VOICE = "Kore"
# The prebuilt Gemini Live voices.  Row-Bot's saved realtime_voice holds OpenAI
# names, so it is used only when it is one of these.
GEMINI_VOICES = frozenset({
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird",
    "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
})
_ENGINE_MODEL = f"models/{DEFAULT_GEMINI_MODEL}"
# The fields the token locks.  sessionResumption is deliberately not named: the
# browser must be able to send the latest handle in a resumed connection's
# setup, and a constrained sessionResumption would replace it.
LOCKED_FIELDS = (
    "model",
    "generationConfig",
    "systemInstruction",
    "tools",
    "inputAudioTranscription",
    "outputAudioTranscription",
    "contextWindowCompression",
)

ROW_BOT_GEMINI_INSTRUCTIONS = """You are Row-Bot in realtime voice mode.
Speak as Row-Bot directly, in the same helpful style and level of substance as
normal chat, in at most three short sentences per answer.

For every substantive request, current information, memory, file or workspace
question, browser or computer control, tool use, approval-sensitive action, or
status about work in progress, call row_bot_agent_consult. Do not answer those
from your own knowledge. Do not claim that you used tools, changed files,
browsed, remembered something, or received approval unless a tool response says
so. Wait for the tool response before speaking the answer, then say it
naturally and faithfully without framing it as another assistant's work.

While Row-Bot is working (a row_bot_agent_consult call has no response yet), if
the user says stop, cancel, or never mind, you must call row_bot_agent_control
with action cancel and wait for its response. Do not answer conversationally
first. Say that it stopped only from that tool's response, never before it. Use
row_bot_agent_control status when the user asks what Row-Bot is doing and steer
or follow_up to add to the current work.

If the latest audio is silence, background noise, hold music, TV audio, side
conversation, assistant echo, or speech not addressed to Row-Bot, call
wait_for_user. Do not respond conversationally after calling wait_for_user."""


def gemini_voice(saved: str = "", override: str = "") -> str:
    """The locked Gemini voice: an explicit override, then a real saved one, then Kore."""
    for candidate in (override, saved):
        text = str(candidate or "").strip()
        if text and text.lower() in {voice.lower() for voice in GEMINI_VOICES}:
            return next(voice for voice in GEMINI_VOICES if voice.lower() == text.lower())
    return DEFAULT_GEMINI_VOICE


def _timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class GeminiLiveProvider:
    provider_id = "gemini_live"
    display_name = "Gemini Live"
    model = DEFAULT_GEMINI_MODEL

    def __init__(self, *, api_key: str | None = None, model: str = DEFAULT_GEMINI_MODEL,
                 voice: str = DEFAULT_GEMINI_VOICE) -> None:
        self.api_key = api_key
        self.model = model if str(model or "").startswith("gemini-") else DEFAULT_GEMINI_MODEL
        self.voice = gemini_voice(voice)

    def _api_key(self) -> str:
        if self.api_key is not None:
            return str(self.api_key or "")
        return str(get_key("GOOGLE_API_KEY") or "")

    def status(self) -> VoiceProviderStatus:
        configured = bool(self._api_key())
        return VoiceProviderStatus(
            provider_id=self.provider_id,
            display_name=self.display_name,
            ready=configured,
            reason="Google API key configured." if configured else "Connect Google in Providers.",
            local=False,
        )

    def setup(self, *, instructions: str = "") -> dict[str, Any]:
        """The locked session setup: audio replies, the voice, tools and resumption."""
        return {
            "model": _ENGINE_MODEL,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": self.voice},
                    },
                },
            },
            "systemInstruction": {
                "parts": [{"text": instructions or ROW_BOT_GEMINI_INSTRUCTIONS}],
            },
            "tools": [{"functionDeclarations": gemini_bridge_tool_declarations()}],
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "sessionResumption": {},
            "contextWindowCompression": {"slidingWindow": {}},
        }

    def token_payload(self, *, instructions: str = "", expires_after_seconds: int = 60) -> dict[str, Any]:
        now = datetime.now(UTC)
        session_window = max(1, int(expires_after_seconds))
        return {
            "uses": 1,
            "expireTime": _timestamp(now + timedelta(minutes=30)),
            "newSessionExpireTime": _timestamp(now + timedelta(seconds=session_window)),
            "bidiGenerateContentSetup": self.setup(instructions=instructions),
            "fieldMask": ",".join(LOCKED_FIELDS),
        }

    def create_client_secret(self, *, instructions: str = "", expires_after_seconds: int = 60,
                             reason: str = "start") -> dict[str, Any]:
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError("Google API key is not configured.")
        payload = self.token_payload(instructions=instructions, expires_after_seconds=expires_after_seconds)
        response = requests.post(
            AUTH_TOKENS_URL,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        value = response.json()
        name = str(value.get("name") or "") if isinstance(value, dict) else ""
        if not name.startswith("auth_tokens/"):
            raise RuntimeError("Gemini did not return a usable auth token.")
        expires_at = self._expiry(value.get("expireTime"), payload["expireTime"])
        logger.info(
            "gemini token minted: model=%s voice=%s tools=%s non_blocking=yes transcription=in,out "
            "resumption=on uses=1 reason=%s",
            self.model,
            self.voice,
            ",".join(declaration["name"] for declaration in gemini_bridge_tool_declarations()),
            "resume" if reason == "resume" else "start",
        )
        return {"value": name, "expires_at": expires_at}

    @staticmethod
    def _expiry(served: Any, requested: str) -> float:
        text = str(served or requested)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(UTC) + timedelta(minutes=30)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()

    def map_server_event(self, event: dict[str, Any]):  # pragma: no cover - the browser reads the wire
        return None


def minted_line(model: str, voice: str, reason: str) -> str:
    """The exact H2 mint line, also used by tests and diagnostics."""
    tools = ",".join(declaration["name"] for declaration in gemini_bridge_tool_declarations())
    return (f"gemini token minted: model={model} voice={voice} tools={tools} non_blocking=yes "
            f"transcription=in,out resumption=on uses=1 reason={reason}")


def canonical_json(value: dict[str, Any]) -> str:  # pragma: no cover - small helper for diagnostics
    return json.dumps(value, sort_keys=True)
