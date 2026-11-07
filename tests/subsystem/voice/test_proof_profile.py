"""H2: the proof profile's setup, key and remove steps."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "cockpit" / "proof_profile.py"


def _module():
    spec = importlib.util.spec_from_file_location("h2_proof_profile", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeKeys:
    def __init__(self) -> None:
        self.set: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def set_key(self, name: str, value: str) -> None:
        self.set.append((name, value))

    def delete_key(self, name: str) -> None:
        self.deleted.append(name)


class _FakeSkills:
    def __init__(self) -> None:
        self.loaded = 0
        self.pinned: list[tuple[str, bool]] = []

    def load_skills(self) -> None:
        self.loaded += 1

    def set_pinned(self, name: str, value: bool) -> None:
        self.pinned.append((name, value))


class _FakeThreads:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def create_thread(self, name: str, *, model_override: str = ""):
        self.created.append({"name": name, "model_override": model_override})
        return "conversation-1"


@pytest.fixture
def fakes(monkeypatch):
    import row_bot

    keys, skills, threads = _FakeKeys(), _FakeSkills(), _FakeThreads()
    for name, value in (
        ("api_keys", keys),
        ("skills", skills),
        ("threads", threads),
    ):
        monkeypatch.setitem(sys.modules, f"row_bot.{name}", value)
        monkeypatch.setattr(row_bot, name, value, raising=False)
    return keys, skills, threads


def _skill(tmp_path: Path) -> Path:
    path = tmp_path / "kit" / "row_bot" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: cockpit-door\ndescription: Ask the door first.\n---\n\n# Door\n",
        encoding="utf-8",
    )
    return path


def test_setup_stores_the_key_adds_an_enabled_door_pins_the_skill_and_makes_the_conversation(
    tmp_path, fakes, monkeypatch, capsys
):
    keys, skills, threads = fakes
    monkeypatch.setenv("H2_DOOR_TOKEN", "door-token-synthetic")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key-synthetic")
    data = tmp_path / "data"

    code = _module().main(
        [
            "setup",
            "--data-dir",
            str(data),
            "--door-url",
            "http://127.0.0.1:8123/mcp",
            "--title",
            "Cockpit",
            "--skill",
            str(_skill(tmp_path)),
            "--agent-model",
            "model:google:gemini-3.8-flash",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert json.loads(out.strip().splitlines()[-1]) == {
        "conversation_id": "conversation-1"
    }
    assert "google-key-synthetic" not in out
    assert keys.set == [("GOOGLE_API_KEY", "google-key-synthetic")]

    config = json.loads((data / "mcp_servers.json").read_text(encoding="utf-8"))
    server = config["servers"]["dev-kit-door"]
    assert config["enabled"] is True and server["enabled"] is True
    assert server["transport"] == "streamable_http"
    assert server["url"] == "http://127.0.0.1:8123/mcp"
    assert server["headers"] == {"Authorization": "Bearer door-token-synthetic"}
    assert "plan" in server["tools"]["run_without_asking"]

    assert (
        data / "skills" / "cockpit-door" / "SKILL.md"
    ).read_text(encoding="utf-8").startswith("---\nname: cockpit-door")
    assert skills.loaded == 1 and skills.pinned == [("cockpit-door", True)]
    assert threads.created == [
        {
            "name": "Cockpit",
            "model_override": "model:google:gemini-3.8-flash",
        }
    ]


def test_key_stores_the_named_environment_value_without_printing_it(
    tmp_path, fakes, monkeypatch, capsys
):
    keys, _, _ = fakes
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key-synthetic")
    data = tmp_path / "data"

    code = _module().main(
        ["key", "--data-dir", str(data), "--name", "OPENAI_API_KEY"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "openai-key-synthetic" not in out
    assert keys.set == [("OPENAI_API_KEY", "openai-key-synthetic")]
    manifest = json.loads((data / "proof_profile.json").read_text(encoding="utf-8"))
    assert manifest == {"secrets": ["OPENAI_API_KEY"]}


def test_remove_deletes_every_secret_the_profile_stored(
    tmp_path, fakes, capsys
):
    keys, _, _ = fakes
    data = tmp_path / "data"
    data.mkdir()
    (data / "proof_profile.json").write_text(
        json.dumps({"secrets": ["GOOGLE_API_KEY", "OPENAI_API_KEY"]}),
        encoding="utf-8",
    )

    code = _module().main(["remove", "--data-dir", str(data)])

    out = capsys.readouterr().out
    assert code == 0
    assert json.loads(out.strip()) == {
        "removed": ["GOOGLE_API_KEY", "OPENAI_API_KEY"]
    }
    assert keys.deleted == ["GOOGLE_API_KEY", "OPENAI_API_KEY"]
    assert json.loads(
        (data / "proof_profile.json").read_text(encoding="utf-8")
    ) == {"secrets": []}
