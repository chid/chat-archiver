# chat-archiver

Archives Claude Desktop, Claude Code, and Codex conversations to Markdown,
incrementally.

## Why it can be incremental

Every surface writes its transcript as append-only JSONL:

| Surface | Transcript |
|---|---|
| Chat / Cowork | `~/Library/Application Support/Claude/local-agent-mode-sessions/<acct>/<org>/local_<uuid>/.claude/projects/<slug>/<cliSessionId>.jsonl` |
| Code (in-app) | `~/.claude/projects/<cwd-slug>/<cliSessionId>.jsonl`, joined via `cliSessionId` in `claude-code-sessions/…/local_<uuid>.json` |
| Code (CLI) | `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl` |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` and `~/.codex/archived_sessions/rollout-*.jsonl` |

The Codex session index at `~/.codex/session_index.jsonl` supplies conversation
titles. Claude resumes from its last complete turn; Codex resumes from its last
`task_complete` event and rereads an unfinished task on the next run. Completed
Markdown turns are not re-rendered.

## Usage

    ./archive.py                 # incremental sync to ~/Documents/Claude Chats
    ./archive.py --tools full    # include tool results, truncated
    ./archive.py --tools none    # prose only
    ./archive.py --thinking      # include reasoning blocks
    ./archive.py --rebuild       # discard offsets, re-render everything

Output is `<surface>/<YYYY-MM>/<slug>-<id>.md` with YAML frontmatter (title,
model, cwd, timestamps, message and tool counts, source path). State lives in
`.archive-index.json` beside the archive; deleting it forces a rebuild.

## What it drops

Transcripts inline base64 PDFs and images, which is most of their bulk. Those
become `*[image/jpeg, 211KB — not inlined]*`. Also dropped: system reminders,
tool-listing deltas, skill listings, telemetry, and reasoning signatures.
Tool calls collapse to one line each by default. Result: ~200x smaller.

## Correctness

`test_incremental.py` feeds Claude transcripts in slices at varied cut points
and asserts the incrementally-built Markdown is byte-identical to a one-shot
rebuild — the property the offset resume has to satisfy.

    python3 test_incremental.py

`test_codex.py` exercises Codex discovery, tool rendering, completed-task
offsets, late title updates, and moving a session into the archived directory.

    python3 test_codex.py

## Known limits

- Supports Claude Desktop, Claude Code, and Codex sessions only. It does not
  archive regular ChatGPT conversations or other chat apps.
- Filenames use a normalized title slug, lowercased with punctuation removed
  and limited to 60 characters, followed by the first eight characters of the
  session ID. A short hash is added if two sessions would otherwise collide.
- A filename can change on a later run if the transcript reveals its title or
  date after the first archive pass.
- Codex turns are committed at `task_complete`; an unfinished final task is
  picked up on the next run. Codex tool results without a matching call are
  ignored, and sessions without explicit `task_complete` events are not
  committed.
- Claude offsets resume at turn boundaries, so a tool result whose call sits
  in an earlier chunk can render without its result under `--tools full`.
- Deleting a session leaves its Markdown behind; the archive only grows.
- These local paths are not a documented stable interface and may change
  between app versions. Claude Code app sessions join through `cliSessionId`;
  Codex sessions use their `session_meta.id`.
