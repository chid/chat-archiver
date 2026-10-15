#!/usr/bin/env python3
"""Archive Claude Desktop, Claude Code, and Codex chats to Markdown, incrementally.

Transcripts are append-only JSONL, so each run resumes from the byte offset it
reached last time and appends. Nothing is re-parsed and nothing is re-rendered.

  ./archive.py                    # incremental sync to ~/Documents/Claude Chats
  ./archive.py --tools full       # include full (truncated) tool results
  ./archive.py --thinking         # include reasoning blocks
  ./archive.py --rebuild          # discard state, re-render everything
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
APP = HOME / "Library/Application Support/Claude"
ROOTS = [
    ("chat", APP / "local-agent-mode-sessions"),   # Chat + Cowork
    ("code", HOME / ".claude/projects"),           # Claude Code (CLI and in-app)
]
CODE_SESSIONS = APP / "claude-code-sessions"

# Entry types that carry no conversation: UI state, reminders, telemetry.
SKIP_TYPES = {
    "attachment", "last-prompt", "mode", "queue-operation", "permission-mode",
    "bridge-session", "file-history-snapshot", "atis-latch", "cost-state",
    "pr-link", "ai-title", "custom-title", "summary",
}
# Argument worth showing for a tool call, in preference order.
TOOL_ARGS = ("file_path", "path", "command", "pattern", "url", "query",
             "prompt", "notebook_path", "description", "skill")
# Tools whose input *is* the assistant's reply (dispatch/agent surfaces), keyed
# to the field holding it. Rendered as prose, not as a tool call.
PROSE_TOOLS = {"SendUserMessage": "message", "mcp__dispatch__send_message": "message"}


def slugify(s: str, n: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", (s or "").lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:n].strip("-") or "untitled"


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def ts_iso(v) -> str:
    """Timestamps appear as ISO strings or epoch millis depending on surface."""
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, timezone.utc).astimezone().isoformat(timespec="seconds")
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
        except ValueError:
            return v
    return ""


def clip(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n].rstrip() + f" … (+{len(s) - n} chars)"


# ---------------------------------------------------------------- discovery

def discover(saved_sessions: dict | None = None, quiet: bool = False) -> list[dict]:
    """Locate every transcript and the metadata that names it."""
    saved_sessions = saved_sessions or {}
    # Claude Code sessions launched from the desktop app keep their state file
    # in the app dir but write transcripts to the normal CLI location, so index
    # them by cliSessionId to recover titles.
    by_cli: dict[str, dict] = {}
    for st in CODE_SESSIONS.glob("*/*/local_*.json"):
        try:
            d = json.loads(st.read_text())
        except (OSError, ValueError):
            continue
        if cid := d.get("cliSessionId"):
            by_cli[cid] = d

    out = []
    for surface, root in ROOTS:
        if not root.is_dir():
            continue
        pat = "**/.claude/projects/*/*.jsonl" if surface == "chat" else "*/*.jsonl"
        for js in root.glob(pat):
            try:
                sz = js.stat().st_size
            except OSError:
                continue
            if sz == 0:
                continue
            meta, kind = {}, surface
            if surface == "chat":
                # .../local_<uuid>/.claude/projects/<slug>/<cli>.jsonl
                sd = js.parents[3]
                sf = sd.parent / (sd.name + ".json")
                if sf.exists():
                    try:
                        meta = json.loads(sf.read_text())
                    except (OSError, ValueError):
                        pass
            else:
                meta = by_cli.get(js.stem, {})
                kind = "code-app" if meta else "code-cli"
            out.append({"path": js, "size": sz, "surface": kind, "meta": meta})

    # Codex keeps active rollouts in dated folders and moves completed sessions
    # into archived_sessions. session_meta.id stays stable across that move.
    codex_index = {}
    index_path = HOME / ".codex/session_index.jsonl"
    if index_path.exists():
        try:
            with index_path.open() as fh:
                for line in fh:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict) and item.get("id"):
                        codex_index[item["id"]] = item
        except OSError:
            pass

    by_session = defaultdict(list)
    for root in (HOME / ".codex/sessions", HOME / ".codex/archived_sessions"):
        if not root.is_dir():
            continue
        for js in root.glob("**/rollout-*.jsonl"):
            try:
                sz = js.stat().st_size
                with js.open("rb") as fh:
                    first = json.loads(fh.readline())
            except (OSError, ValueError):
                continue
            if not sz or not isinstance(first, dict) or first.get("type") != "session_meta":
                continue
            meta = first.get("payload")
            if not isinstance(meta, dict) or not meta.get("id"):
                continue
            sid = str(meta["id"])
            index_entry = codex_index.get(sid, {})
            meta = {
                "title": index_entry.get("thread_name") or "",
                "cwd": meta.get("cwd") or "",
                "created": meta.get("timestamp") or "",
                "model": meta.get("model") or "",
                "source": meta.get("source") or "",
            }
            by_session[sid].append({
                "path": js, "size": sz, "surface": "codex", "meta": meta,
                "key": f"codex:{sid}", "session_id": sid,
                "metadata_revision": meta["title"],
            })

    def is_prefix(shorter: Path, longer: Path) -> bool:
        """Whether the shorter duplicate transcript is a byte prefix."""
        try:
            with shorter.open("rb") as a, longer.open("rb") as b:
                while chunk := a.read(1024 * 1024):
                    if b.read(len(chunk)) != chunk:
                        return False
            return True
        except OSError:
            return False

    # Prefer a complete extension when duplicate active/archive copies exist.
    # Divergent copies are unsafe to merge: retain the prior source if possible,
    # otherwise skip the session so a different history is not appended.
    for sid, candidates in by_session.items():
        key = f"codex:{sid}"
        ordered = sorted(candidates, key=lambda r: (r["size"], str(r["path"]).startswith(str(HOME / ".codex/archived_sessions"))))
        compatible = all(is_prefix(a["path"], b["path"])
                         for a, b in zip(ordered, ordered[1:]))
        if compatible:
            out.append(ordered[-1])
        else:
            prior_path = saved_sessions.get(key, {}).get("source_path")
            prior = next((r for r in candidates if str(r["path"]) == prior_path), None)
            if prior is not None:
                out.append(prior)
                if not quiet:
                    print(f"warning: divergent Codex copies for {sid}; keeping indexed source", file=sys.stderr)
            else:
                if not quiet:
                    print(f"warning: divergent Codex copies for {sid}; skipping session", file=sys.stderr)
    # Sorted by path so that name-collision resolution is deterministic.
    return sorted(out, key=lambda r: str(r["path"]))


# ---------------------------------------------------------------- rendering

class Turn:
    """One speaker's contiguous run of blocks, flushed at a turn boundary."""

    def __init__(self, role: str, side: bool = False, assistant_label: str = "Claude"):
        self.role, self.side, self.events = role, side, []
        self.assistant_label = assistant_label


class Renderer:
    def __init__(self, opt):
        self.opt = opt

    @staticmethod
    def short(name: str) -> str:
        """mcp__claude-in-chrome__navigate -> chrome:navigate"""
        if not name or not name.startswith("mcp__"):
            return name or "?"
        parts = name.split("__")
        if len(parts) < 3:
            return name
        server = re.sub(r"[-_]?(in[-_])?chrome", "chrome", parts[1].lower())
        return f"{server.split('-')[-1] if '-' in server else server}:{parts[-1]}"

    def tool_line(self, name, inp) -> str:
        arg = ""
        if isinstance(inp, str):
            try:
                inp = json.loads(inp)
            except ValueError:
                arg = inp.replace("\n", " ")
        if isinstance(inp, dict):
            for k in TOOL_ARGS:
                if v := inp.get(k):
                    arg = str(v).replace("\n", " ")
                    break
            else:
                arg = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(inp.items())[:2])
        elif not arg and inp is not None:
            arg = str(inp).replace("\n", " ")
        return f"`{self.short(name)}` {clip(arg, self.opt.arg_chars)}".rstrip()

    def block_md(self, b: dict) -> str:
        """Render one content block. Binary payloads are described, never inlined."""
        t = b.get("type")
        if t == "text":
            return (b.get("text") or "").strip()
        if t == "thinking":
            if not self.opt.thinking:
                return ""
            body = (b.get("thinking") or "").strip()
            return f"> 🧠 {clip(body, self.opt.think_chars)}" if body else ""
        if t in ("image", "document"):
            src = b.get("source") or {}
            mt = src.get("media_type", t)
            n = len(src.get("data") or "") * 3 // 4
            return f"*[{mt}, {human(n)} — not inlined]*"
        if t == "fallback":
            return f"*[model fallback: {(b.get('from') or {}).get('model')} → {(b.get('to') or {}).get('model')}]*"
        return ""

    def turn_md(self, turn: Turn) -> str:
        parts = []
        for kind, payload in turn.events:
            if kind == "block":
                if s := self.block_md(payload):
                    parts.append(s)
            elif kind == "tool" and self.opt.tools != "none":
                name, inp, result = payload
                line = "- " + self.tool_line(name, inp)
                if self.opt.tools == "full" and result:
                    line += "\n" + "\n".join(
                        "  > " + ln for ln in clip(result, self.opt.result_chars).splitlines()[:12])
                parts.append(line)
        body = "\n\n".join(p for p in parts if p).strip()
        if not body:
            return ""
        who = {"user": "### 👤 User", "assistant": f"### 🤖 {turn.assistant_label}"}[turn.role]
        if turn.side:
            who += " *(subagent)*"
        return f"{who}\n\n{body}\n"


# ---------------------------------------------------------------- conversion

def convert(rec: dict, state: dict, opt, ren: Renderer) -> tuple[str, dict, str]:
    """Parse from the last safe offset. Returns (markdown, new state, title)."""
    path: Path = rec["path"]
    meta = rec["meta"]
    offset = state.get("offset", 0)
    title = state.get("title") or meta.get("title") or ""
    tools: dict[str, list] = {}
    turn: Turn | None = None
    chunks: list[str] = []
    counts = {"user": state.get("n_user", 0), "assistant": state.get("n_assistant", 0),
              "tool": state.get("n_tool", 0)}
    first, last = state.get("first_ts", ""), state.get("last_ts", "")
    model = state.get("model") or meta.get("model") or ""
    cwd = state.get("cwd") or meta.get("cwd") or ""
    safe = offset  # byte offset of the last completed turn boundary

    snap = dict(counts)  # counts as of `safe`; the re-read region must not double-count

    def flush(boundary: int):
        """Emit the open turn and mark `boundary` as fully written.

        `safe` is the offset of the first byte not yet rendered, so a run that
        stops mid-turn resumes at the start of that turn rather than re-emitting
        one already appended to the file.
        """
        nonlocal turn, safe
        if turn is not None:
            if md := ren.turn_md(turn):
                chunks.append(md)
            turn = None
        safe = boundary
        snap.clear()
        snap.update(counts)

    with path.open("rb") as fh:
        fh.seek(offset)
        while True:
            pos = fh.tell()
            line = fh.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break  # partial line: a session still being written
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("type")
            if t == "ai-title" and not title:
                title = e.get("aiTitle") or ""
            elif t == "custom-title":
                title = e.get("customTitle") or title
            if t in SKIP_TYPES or e.get("isMeta"):
                continue
            if ts := ts_iso(e.get("timestamp")):
                first, last = first or ts, ts
            cwd = cwd or e.get("cwd") or ""

            if t == "system":
                if e.get("subtype") == "compact_boundary":
                    flush(fh.tell())
                    chunks.append("---\n\n*[context compacted]*\n")
                continue

            m = e.get("message")
            if not isinstance(m, dict):
                continue
            role = m.get("role") or ("assistant" if t == "assistant" else "user")
            side = bool(e.get("isSidechain"))
            model = (m.get("model") or model) if role == "assistant" else model
            content = m.get("content")

            if isinstance(content, str):
                # A plain string is always a fresh user prompt: a turn boundary.
                flush(pos)
                turn = Turn("user", side)
                turn.events.append(("block", {"type": "text", "text": content}))
                counts["user"] += 1
                continue
            if not isinstance(content, list):
                continue

            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_result":
                    ev = tools.get(b.get("tool_use_id"))
                    if ev is not None:
                        c = b.get("content")
                        if isinstance(c, list):
                            c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                        ev[2] = c if isinstance(c, str) else json.dumps(c)[:500]
                    continue
                # A user text block after an assistant turn is a new prompt.
                if bt == "text" and role == "user" and (turn is None or turn.role != "user"):
                    flush(pos)
                if turn is None or turn.role != role:
                    flush(pos)
                    turn = Turn(role, side)
                    counts[role] += 1
                if bt == "tool_use" and b.get("name") in PROSE_TOOLS:
                    field = PROSE_TOOLS[b["name"]]
                    txt = (b.get("input") or {}).get(field)
                    if isinstance(txt, str) and txt.strip():
                        turn.events.append(("block", {"type": "text", "text": txt}))
                        continue
                if bt == "tool_use":
                    ev = [b.get("name"), b.get("input"), None]
                    tools[b.get("id")] = ev
                    turn.events.append(("tool", ev))
                    counts["tool"] += 1
                else:
                    turn.events.append(("block", b))

    # An unterminated turn is left for the next run, which resumes at `safe`.
    md = "\n".join(chunks)
    new = {"offset": safe, "title": title, "first_ts": first, "last_ts": last,
           "model": model, "cwd": cwd, "n_user": snap["user"],
           "n_assistant": snap["assistant"], "n_tool": snap["tool"]}
    return md, new, title


def convert_codex(rec: dict, state: dict, opt, ren: Renderer) -> tuple[str, dict, str]:
    """Convert completed Codex tasks, leaving the active task for the next run."""
    path: Path = rec["path"]
    meta = rec["meta"]
    offset = state.get("offset", 0)
    # A session-index title can appear after the first transcript records.
    title = meta.get("title") or state.get("title") or ""
    first = state.get("first_ts") or ts_iso(meta.get("created"))
    last = state.get("last_ts", "")
    model = state.get("model") or meta.get("model") or ""
    cwd = state.get("cwd") or meta.get("cwd") or ""
    counts = {"user": state.get("n_user", 0), "assistant": state.get("n_assistant", 0),
              "tool": state.get("n_tool", 0)}
    committed = dict(counts)
    committed_first, committed_last = first, last
    safe = offset
    chunks: list[str] = []
    pending: list[Turn] = []
    tools: dict[str, list] = {}
    turn: Turn | None = None

    def end_turn() -> None:
        nonlocal turn
        if turn is not None:
            pending.append(turn)
            turn = None

    def start_turn(role: str) -> Turn:
        nonlocal turn
        if turn is not None and turn.role != role:
            end_turn()
        if turn is None:
            turn = Turn(role, assistant_label="Codex")
        return turn

    def add_text(role: str, text: str) -> None:
        if text.strip():
            start_turn(role).events.append(("block", {"type": "text", "text": text.strip()}))

    def commit_task(boundary: int) -> None:
        nonlocal safe, committed, committed_first, committed_last
        end_turn()
        chunks.extend(md for item in pending if (md := ren.turn_md(item)))
        pending.clear()
        tools.clear()
        safe = boundary
        committed = dict(counts)
        committed_first, committed_last = first, last

    def result_text(value) -> str:
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False)

    with path.open("rb") as fh:
        fh.seek(offset)
        while True:
            pos = fh.tell()
            line = fh.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue

            if event_type == "event_msg":
                if payload.get("type") == "task_complete":
                    if ts := ts_iso(event.get("timestamp")):
                        first, last = first or ts, ts
                    commit_task(fh.tell())
                continue

            if event_type != "response_item":
                continue
            if ts := ts_iso(event.get("timestamp")):
                first, last = first or ts, ts

            item_type = payload.get("type")
            if item_type == "message":
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = payload.get("content")
                blocks = content if isinstance(content, list) else []
                texts = []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if (block_type in ("input_text", "output_text", "text")
                            and isinstance(block.get("text"), str)):
                        texts.append(block["text"])
                    elif block_type in ("input_image", "image"):
                        texts.append("*[image attachment — not inlined]*")
                    elif block_type in ("input_file", "file"):
                        name = block.get("filename") or "file"
                        texts.append(f"*[file attachment: {name} — not inlined]*")
                if isinstance(content, str):
                    texts = [content]
                if role == "user":
                    end_turn()
                    if not title and texts:
                        title = clip(next((t.strip().splitlines()[0] for t in texts if t.strip()), ""), 100)
                for text in texts:
                    add_text(role, text)
                if role == "user":
                    counts["user"] += 1
                else:
                    counts["assistant"] += 1
                continue

            if item_type == "reasoning":
                if opt.thinking:
                    summary = payload.get("summary") or []
                    if isinstance(summary, str):
                        text = summary
                    else:
                        text = "\n".join(b.get("text", "") for b in summary
                                          if isinstance(b, dict) and isinstance(b.get("text"), str))
                    if text:
                        start_turn("assistant").events.append(("block", {
                            "type": "thinking", "thinking": text,
                        }))
                continue

            if item_type in ("function_call", "custom_tool_call", "web_search_call"):
                call_id = payload.get("call_id") or payload.get("id")
                name = payload.get("name") or ("web_search" if item_type == "web_search_call" else "tool")
                arguments = payload.get("arguments", payload.get("input", payload.get("action", {})))
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        pass
                tool = [name, arguments, None]
                start_turn("assistant").events.append(("tool", tool))
                if call_id:
                    tools[str(call_id)] = tool
                counts["tool"] += 1
                continue

            if item_type in ("function_call_output", "custom_tool_call_output"):
                call = tools.get(str(payload.get("call_id") or ""))
                if call is not None:
                    call[2] = result_text(payload.get("output"))

    md = "\n".join(chunks)
    new = {"offset": safe, "title": title, "first_ts": committed_first,
           "last_ts": committed_last, "model": model, "cwd": cwd,
           "n_user": committed["user"], "n_assistant": committed["assistant"],
           "n_tool": committed["tool"]}
    return md, new, title


def source_marker(path: Path, offset: int) -> str:
    """Fingerprint the start and committed tail when a session file moves."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        digest.update(fh.read(min(offset, 4096)))
        if offset > 4096:
            fh.seek(max(0, offset - 4096))
            digest.update(fh.read(min(offset, 4096)))
    return digest.hexdigest()


def split_body(txt: str) -> str:
    """Strip the YAML header and H1 so both can be regenerated on append.

    Scanning for the *first* closing delimiter matters: a conversation can
    contain '---' of its own, and a later match would truncate the archive.
    """
    if txt.startswith("---\n"):
        i = txt.find("\n---\n", 3)
        if i != -1:
            txt = txt[i + 5:]
    txt = txt.lstrip("\n")
    if txt.startswith("# "):
        nl = txt.find("\n")
        txt = txt[nl + 1:] if nl != -1 else ""
    return txt.lstrip("\n")


def frontmatter(rec, st) -> str:
    def esc(v):
        v = str(v).replace(str(HOME), "~") if v else ""
        return json.dumps(v) if v else '""'
    return "\n".join([
        "---",
        f"title: {esc(st.get('title') or 'Untitled')}",
        f"surface: {rec['surface']}",
        f"session: {rec.get('session_id') or rec['path'].stem}",
        f"model: {esc(st.get('model'))}",
        f"cwd: {esc(st.get('cwd'))}",
        f"created: {esc(st.get('first_ts'))}",
        f"updated: {esc(st.get('last_ts'))}",
        f"messages: {st.get('n_user', 0) + st.get('n_assistant', 0)}",
        f"tool_calls: {st.get('n_tool', 0)}",
        f"source: {esc(rec['path'])}",
        "---", "",
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=HOME / "Documents/Claude Chats")
    ap.add_argument("--tools", choices=("none", "summary", "full"), default="summary",
                    help="how much tool traffic to keep (default: summary)")
    ap.add_argument("--thinking", action="store_true", help="include reasoning blocks")
    ap.add_argument("--result-chars", type=int, default=400)
    ap.add_argument("--think-chars", type=int, default=600)
    ap.add_argument("--arg-chars", type=int, default=120)
    ap.add_argument("--rebuild", action="store_true", help="ignore saved offsets")
    ap.add_argument("--quiet", action="store_true")
    opt = ap.parse_args()

    opt.out.mkdir(parents=True, exist_ok=True)
    idx_path = opt.out / ".archive-index.json"
    idx = {}
    if idx_path.exists() and not opt.rebuild:
        try:
            idx = json.loads(idx_path.read_text()).get("sessions", {})
        except ValueError:
            pass

    # A session id can appear under two project slugs when its cwd moved, so
    # destinations are claimed explicitly rather than assumed unique.
    claimed: dict[str, str] = {v["rel"]: k for k, v in idx.items() if v.get("rel")}
    ren, t0 = Renderer(opt), time.time()
    n_new = n_upd = n_skip = 0
    read_bytes = 0

    for rec in discover(idx, quiet=opt.quiet):
        key = rec.get("key", str(rec["path"]))
        st = dict(idx.get(key, {}))
        # Guard against a rewritten or truncated transcript.
        if st and rec["size"] < st.get("offset", 0):
            st = {}
        if st and rec.get("surface") == "codex":
            offset = st.get("offset", 0)
            if offset and st.get("source_marker") != source_marker(rec["path"], offset):
                st = {}
        same_path = st.get("source_path", str(rec["path"])) == str(rec["path"])
        same_metadata = st.get("metadata_revision", "") == rec.get("metadata_revision", "")
        if st and rec["size"] == st.get("size_seen") and same_path and same_metadata:
            n_skip += 1
            continue

        if rec.get("surface") == "codex":
            md, new, title = convert_codex(rec, st, opt, ren)
        else:
            md, new, title = convert(rec, st, opt, ren)
        read_bytes += max(0, rec["size"] - st.get("offset", 0))
        new["metadata_revision"] = rec.get("metadata_revision", "")
        new["source_path"] = str(rec["path"])
        if rec.get("surface") == "codex":
            new["source_marker"] = source_marker(rec["path"], new["offset"])

        # A transcript's title and first timestamp can both appear well after
        # its opening lines, so the destination is recomputed every run and any
        # existing file moved to it. Losing the old path would silently restart
        # the archive from the current offset and drop everything before it.
        stamp = (new.get("first_ts") or "")[:7] or "undated"
        session_token = (rec.get("session_id") or rec["path"].stem)[:8]
        rel = (Path(rec["surface"]) / stamp /
               f"{slugify(new.get('title') or rec['path'].stem)}-{session_token}.md")
        if claimed.get(str(rel), key) != key:
            rel = rel.with_name(f"{rel.stem}-{hashlib.sha1(key.encode()).hexdigest()[:4]}.md")
        claimed[str(rel)] = key
        dest = opt.out / rel
        if (prev := st.get("rel")) and prev != str(rel):
            old_path = opt.out / prev
            if old_path.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old_path, dest)
                for d in old_path.parents:
                    if d == opt.out or not d.is_dir() or any(d.iterdir()):
                        break
                    d.rmdir()

        if not md.strip() and not dest.exists():
            # Nothing has completed a turn yet: leave the offset for next time.
            n_skip += 1
            new["size_seen"] = rec["size"]
            new["rel"] = str(rel)
            idx[key] = new
            continue

        # The header carries running counts and a title that may only appear
        # later in the transcript, so it is regenerated on every run -- even one
        # that appends no new turns, which is how late metadata lands.
        head = frontmatter(rec, new) + f"# {new.get('title') or 'Untitled'}\n\n"
        old_text = dest.read_text() if dest.exists() else ""
        # Only carry the existing body forward when resuming; a rebuild starts clean.
        body = split_body(old_text) if (old_text and st.get("offset")) else ""
        text = head + (body.rstrip("\n") + "\n\n" if body else "") + md
        text = text.rstrip("\n") + "\n"  # one trailing newline, append or rebuild
        if text != old_text:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
            if old_text:
                n_upd += 1
            else:
                n_new += 1
        else:
            n_skip += 1

        new["size_seen"] = rec["size"]
        new["rel"] = str(rel)
        idx[key] = new

    idx_path.write_text(json.dumps({"version": 1, "updated": datetime.now().isoformat(),
                                    "sessions": idx}, indent=1))
    if not opt.quiet:
        total = sum(f.stat().st_size for f in opt.out.rglob("*.md"))
        print(f"{n_new} new, {n_upd} appended, {n_skip} unchanged  "
              f"| read {human(read_bytes)} | archive {human(total)} "
              f"| {time.time() - t0:.1f}s -> {opt.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
