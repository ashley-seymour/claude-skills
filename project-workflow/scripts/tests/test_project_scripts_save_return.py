"""save-return.py: copy a subagent's final message to disk."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"


@pytest.fixture(scope="module")
def sr():
    spec = importlib.util.spec_from_file_location("save_return", BIN / "save-return.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _transcript(path: Path, *texts: str) -> Path:
    rows = [{"type": "user", "message": {"content": "go"}}]
    for t in texts:
        rows.append({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}})
        rows.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_last_text_message_wins_over_earlier_and_tool_only_records(sr, tmp_path):
    t = _transcript(tmp_path / "a.jsonl", "thinking aloud", "final answer")
    assert sr.last_assistant_text(t) == "final answer"


def test_agent_id_resolves_to_newest_subagent_transcript(sr, tmp_path):
    t = _transcript(tmp_path / "projects" / "p" / "s1" / "subagents" / "agent-abc123.jsonl", "x")
    assert sr.resolve_source("abc123", config_dir=tmp_path) == t
    assert sr.resolve_source("missing", config_dir=tmp_path) is None
    assert sr.resolve_source("../etc", config_dir=tmp_path) is None


def test_json_mode_strips_fence_and_splits_deliverable(sr, tmp_path):
    body = {"mode": "brief", "verdict": "go", "deliverable_markdown": "# Critique\n"}
    t = _transcript(tmp_path / "r.jsonl", "```json\n" + json.dumps(body) + "\n```")
    dest = tmp_path / "out" / "S-01.json"
    assert sr.main([str(t), str(dest), "--json"]) == 0
    assert json.loads(dest.read_text(encoding="utf-8"))["verdict"] == "go"
    assert (tmp_path / "out" / "S-01.md").read_text(encoding="utf-8") == "# Critique\n"


def test_json_mode_refuses_prose(sr, tmp_path):
    t = _transcript(tmp_path / "r.jsonl", "not json")
    assert sr.main([str(t), str(tmp_path / "x.json"), "--json"]) == 1
    assert not (tmp_path / "x.json").exists()
