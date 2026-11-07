"""Set up, key and remove the throwaway Row-Bot profile the H2 proof uses.

The proof starts Row-Bot on a fresh data folder and then needs that folder to
hold the door's MCP server, one pinned skill, a conversation using the proof's
agent model, and a Google key in the folder's secret store.  Every command sets
``ROW_BOT_DATA_DIR`` before importing Row-Bot, because several modules read the
folder once at import time.  A key is written to secure storage by Row-Bot's own
key store and is never printed, echoed or put in this process's environment for
a child program to inherit.

Usage:
  python scripts/cockpit/proof_profile.py setup --data-dir D --door-url URL \
      --title Cockpit --skill SKILL.md --agent-model REF
  python scripts/cockpit/proof_profile.py key --data-dir D --name OPENAI_API_KEY
  python scripts/cockpit/proof_profile.py remove --data-dir D
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

DOOR_SERVER = "dev-kit-door"
DOOR_TOOLS = (
    "status",
    "work",
    "messages",
    "project",
    "feature",
    "search",
    "lookup",
    "progress",
    "plan",
)
_MANIFEST = "proof_profile.json"


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _read_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return value if isinstance(value, dict) else default


def _remember_secret(data: Path, name: str) -> None:
    """Record a name this profile asked Row-Bot to store, so remove deletes it."""
    path = data / _MANIFEST
    manifest = _read_json(path, {"secrets": []})
    names = [str(item) for item in manifest.get("secrets", []) if isinstance(item, str)]
    if name not in names:
        names.append(name)
    manifest["secrets"] = names
    _write_json(path, manifest)


def _stored_secrets(data: Path) -> list[str]:
    manifest = _read_json(data / _MANIFEST, {"secrets": []})
    return [str(item) for item in manifest.get("secrets", []) if isinstance(item, str)]


def _skill_name(source: Path) -> str:
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        text = ""
    match = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL | re.MULTILINE)
    if match:
        name = re.search(r"^name:\s*[\"']?([A-Za-z0-9_.-]+)", match.group(1), re.MULTILINE)
        if name:
            return name.group(1)
    return source.parent.name


def _write_mcp(data: Path, door_url: str, token: str) -> None:
    server = {
        "name": DOOR_SERVER,
        "enabled": True,
        "transport": "streamable_http",
        "command": "",
        "args": [],
        "cwd": None,
        "env": {},
        "url": door_url,
        "headers": {"Authorization": f"Bearer {token}"},
        "connect_timeout": 30.0,
        "tool_timeout": 120.0,
        "output_limit": 24000,
        "trust_level": "standard",
        "requirements": [],
        "tools": {
            "enabled": {},
            "require_approval": [],
            "run_without_asking": list(DOOR_TOOLS),
            "include": [],
            "exclude": [],
            "resources_enabled": False,
            "prompts_enabled": False,
        },
        "source": {},
    }
    _write_json(data / "mcp_servers.json", {
        "version": 1,
        "enabled": True,
        "servers": {DOOR_SERVER: server},
    })


def _setup(args: argparse.Namespace, data: Path) -> int:
    token = str(os.environ.get("H2_DOOR_TOKEN") or "")
    google_key = str(os.environ.get("GOOGLE_API_KEY") or "")
    if not token:
        raise SystemExit("H2_DOOR_TOKEN is not set")
    if not google_key:
        raise SystemExit("GOOGLE_API_KEY is not set")

    from row_bot import api_keys

    api_keys.set_key("GOOGLE_API_KEY", google_key)
    _remember_secret(data, "GOOGLE_API_KEY")
    _write_mcp(data, args.door_url, token)

    source = Path(args.skill)
    if not source.is_file():
        raise SystemExit(f"skill file not found: {source}")
    name = _skill_name(source)
    target = data / "skills" / name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())

    from row_bot import skills, threads

    skills.load_skills()
    skills.set_pinned(name, True)
    conversation = threads.create_thread(str(args.title or "Cockpit"), model_override=str(args.agent_model or ""))
    print(json.dumps({"conversation_id": conversation}))
    return 0


def _key(args: argparse.Namespace, data: Path) -> int:
    name = str(args.name or "")
    value = str(os.environ.get(name) or "")
    if not name or not value:
        raise SystemExit(f"{name or 'the named variable'} is not set")
    from row_bot import api_keys

    api_keys.set_key(name, value)
    _remember_secret(data, name)
    print(json.dumps({"stored": name}))
    return 0


def _remove(_args: argparse.Namespace, data: Path) -> int:
    from row_bot import api_keys

    removed: list[str] = []
    for name in _stored_secrets(data):
        try:
            api_keys.delete_key(name)
        except Exception:  # secure storage may already be gone with the folder
            pass
        removed.append(name)
    _write_json(data / _MANIFEST, {"secrets": []})
    print(json.dumps({"removed": removed}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", required=True, help="the throwaway Row-Bot data folder")
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("setup", parents=[common], help="store the key, add the door, pin the skill and make the conversation")
    setup.add_argument("--door-url", required=True)
    setup.add_argument("--title", default="Cockpit")
    setup.add_argument("--skill", required=True)
    setup.add_argument("--agent-model", default="")

    key = commands.add_parser("key", parents=[common], help="store the value of an environment variable")
    key.add_argument("--name", required=True)

    commands.add_parser("remove", parents=[common], help="delete every secret this profile stored")

    args = parser.parse_args(argv)
    data = Path(args.data_dir).expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True)
    # Every Row-Bot module below reads the folder once, at import.
    os.environ["ROW_BOT_DATA_DIR"] = str(data)
    handler = {"setup": _setup, "key": _key, "remove": _remove}[args.command]
    return handler(args, data)


if __name__ == "__main__":
    sys.exit(main())
