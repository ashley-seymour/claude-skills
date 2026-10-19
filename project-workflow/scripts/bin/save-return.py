"""Save a subagent's final message to a file without passing it through the
orchestrator's output.

Usage:
    python <skill>/scripts/bin/save-return.py <agent-id|transcript> <dest> [--json]

`<agent-id>` is the id the Agent tool's launch acknowledgement names; it
resolves to `<config>/projects/*/*/subagents/agent-<id>.jsonl` (config is
`$CLAUDE_CONFIG_DIR`, else `~/.claude`; the newest match wins). A path to a
transcript also works. The ack's `output_file` is not used: it is empty once
the agent completes. The text of the last assistant message is written to
`<dest>`. With `--json`, the text must parse as one JSON
object (one surrounding ``` fence is stripped) and is written pretty-printed;
a reviewer's `deliverable_markdown` field, when present, is also written
beside it as `<dest stem>.md`.

Re-typing a return with the Write tool costs its whole length again as output
tokens; this copies it from disk instead. Exit codes: 0 saved, 1 no assistant
text or invalid JSON, 2 bad arguments or unreadable file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*)\n\s*```\s*$", re.S)


def resolve_source(source: str, config_dir: Path | None = None) -> Path | None:
    """A transcript path as given, else the newest transcript for an agent id."""
    p = Path(source)
    if p.is_file():
        return p
    if not re.fullmatch(r"[A-Za-z0-9_-]+", source):
        return None
    if config_dir is None:
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    hits = list(config_dir.glob(f"projects/*/*/subagents/agent-{source}.jsonl"))
    return max(hits, key=lambda h: h.stat().st_mtime) if hits else None


def last_assistant_text(path: Path) -> str | None:
    text = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "assistant":
                continue
            parts = [b.get("text", "") for b in (rec.get("message") or {}).get("content") or []
                     if isinstance(b, dict) and b.get("type") == "text"]
            if any(p.strip() for p in parts):
                text = "\n".join(parts)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", help="agent id, or a subagent transcript path")
    ap.add_argument("dest", type=Path)
    ap.add_argument("--json", action="store_true", help="require and pretty-print one JSON object")
    a = ap.parse_args(argv)
    src = resolve_source(a.source)
    if src is None:
        print(f"FAIL:unreadable:{a.source}")
        return 2
    text = last_assistant_text(src)
    if text is None:
        print(f"FAIL:no-assistant-text:{src}")
        return 1
    a.dest.parent.mkdir(parents=True, exist_ok=True)
    if not a.json:
        a.dest.write_text(text, encoding="utf-8")
        print(f"saved {a.dest} ({len(text)} chars)")
        return 0
    m = _FENCE.match(text)
    try:
        obj = json.loads(m.group(1) if m else text)
    except json.JSONDecodeError as exc:
        print(f"FAIL:invalid-json:{exc}")
        return 1
    if not isinstance(obj, dict):
        print("FAIL:invalid-json:not an object")
        return 1
    a.dest.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"saved {a.dest}")
    md = obj.get("deliverable_markdown")
    if isinstance(md, str) and md.strip():
        md_path = a.dest.with_suffix(".md")
        md_path.write_text(md, encoding="utf-8")
        print(f"saved {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
