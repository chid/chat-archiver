"""End-to-end checks for Codex rollout discovery and incremental archiving."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ARCHIVER = Path(os.environ.get("ARCHIVER", Path(__file__).with_name("archive.py")))
SESSION_ID = "01abcde0-1234-5678-90ab-cdef12345678"


def record(kind, payload, timestamp):
    return json.dumps({"type": kind, "payload": payload, "timestamp": timestamp}) + "\n"


def write_session(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        record("session_meta", {
            "id": SESSION_ID,
            "timestamp": "2026-10-17T00:00:00.000Z",
            "cwd": "/tmp/example-project",
            "source": "cli",
        }, "2026-10-17T00:00:00.000Z")
        + record("response_item", {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Completed prompt"}],
        }, "2026-10-17T00:01:00.000Z")
        + record("response_item", {
            "type": "custom_tool_call", "call_id": "call-1",
            "name": "shell_command", "input": {"command": "printf done"},
        }, "2026-10-17T00:01:01.000Z")
        + record("response_item", {
            "type": "custom_tool_call_output", "call_id": "call-1", "output": "tool result",
        }, "2026-10-17T00:01:02.000Z")
        + record("response_item", {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "Completed answer"}],
        }, "2026-10-17T00:01:03.000Z")
        + record("event_msg", {"type": "task_complete"}, "2026-10-17T00:01:04.000Z")
        + record("response_item", {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Unfinished prompt"}],
        }, "2026-10-17T00:02:00.000Z")
        + record("response_item", {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "Unfinished answer"}],
        }, "2026-10-17T00:02:01.000Z")
    )


def run(home, out):
    subprocess.run(
        [sys.executable, str(ARCHIVER), "--out", str(out), "--quiet", "--tools", "full"],
        env={**os.environ, "HOME": str(home)}, check=True,
    )


def main():
    with tempfile.TemporaryDirectory(prefix="chat-archiver-codex-") as temp:
        root = Path(temp)
        home = root / "home"
        out = root / "archive"
        active = home / ".codex/sessions/2026/10/17" / f"rollout-{SESSION_ID}.jsonl"
        index = home / ".codex/session_index.jsonl"
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({"id": SESSION_ID, "thread_name": "Indexed title"}) + "\n")
        write_session(active)

        run(home, out)
        files = list((out / "codex").rglob("*.md"))
        assert len(files) == 1, "first completed Codex conversation was not archived"
        archived = files[0]
        text = archived.read_text()
        assert "### 👤 User" in text and "### 🤖 Codex" in text
        assert "Completed prompt" in text and "Completed answer" in text
        assert "`shell_command`" in text and "tool result" in text
        assert "Unfinished prompt" not in text and "Unfinished answer" not in text

        before = archived.read_text()
        run(home, out)
        assert archived.read_text() == before, "unchanged session output changed on a repeat run"

        with active.open("a") as fh:
            fh.write(record("event_msg", {"type": "task_complete"}, "2026-10-17T00:02:02.000Z"))
        run(home, out)
        assert "Unfinished prompt" in archived.read_text()
        assert "Unfinished answer" in archived.read_text()

        moved = home / ".codex/archived_sessions" / active.name
        moved.parent.mkdir(parents=True)
        active.replace(moved)
        index.write_text(json.dumps({"id": SESSION_ID, "thread_name": "Renamed title"}) + "\n")
        run(home, out)
        files = list((out / "codex").rglob("*.md"))
        assert len(files) == 1, "moving the session or updating its title duplicated the archive"
        assert files[0].name.startswith("renamed-title-")
        final = files[0].read_text()
        assert final.count("Completed answer") == 1
        assert final.count("Unfinished answer") == 1
        print("Codex discovery, tool rendering, completion offsets, title refresh, and move: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
