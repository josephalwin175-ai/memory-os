"""Per-tool-call hooks: skip re-reads of unchanged files, trim huge tool output,
and surface saved rules when an edit or command matches one.

All of them fail open. If anything is missing or unexpected, the hook prints
nothing and the tool call proceeds exactly as it would without MEMORY OS.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from . import checks, ledger
from .store import Store
from .transcript import _state_path

MIN_DEDUP_BYTES = 2000      # below this a re-read is cheaper than the refusal round trip
DEFAULT_TRIM_CHARS = 12_000  # roughly 3K tokens
HEAD_LINES, TAIL_LINES, SIGNAL_LINES, MAX_LINE = 40, 80, 40, 400
SIGNAL = re.compile(r"(?i)\b(error|errors|fail|failed|failure|exception|traceback|fatal|panic|warning|denied|"
                    r"not found|cannot|undefined|assert)")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Which string fields hold the bulky text, per tool. MCP tools return a list of text blocks.
TRIM_FIELDS = {"Bash": ("stdout", "stderr"), "PowerShell": ("stdout", "stderr"), "Grep": ("content",)}


def _off(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() in ("0", "false", "off", "no")


def _load(session_id: str) -> tuple[Path, dict]:
    path = _state_path(session_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    return path, state if isinstance(state, dict) else {}


def _save(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def forget_reads(payload: dict) -> None:
    """After compaction or /clear the earlier file contents are gone, so start over."""
    path, state = _load(str(payload.get("session_id") or ""))
    if state.pop("reads", None) is not None:
        _save(path, state)


def read_guard(payload: dict) -> dict | None:
    """PreToolUse on Read: refuse a repeat read of a file that has not changed.

    The refusal happens once. If the model asks again straight away (for
    example because it no longer has the content), the read goes through.
    """
    if _off("MOS_DEDUP") or payload.get("tool_name") != "Read":
        return None
    tool_input = payload.get("tool_input") or {}
    raw = tool_input.get("file_path")
    if not raw:
        return None
    try:
        info = Path(raw).stat()
    except OSError:
        return None
    if info.st_size < MIN_DEDUP_BYTES:
        return None
    # Subagents have their own context, so their reads are tracked separately.
    key = "|".join(str(x) for x in (payload.get("agent_id") or "main", os.path.normcase(os.path.abspath(raw)),
                                    tool_input.get("offset"), tool_input.get("limit"), tool_input.get("pages")))
    path, state = _load(str(payload.get("session_id") or ""))
    reads = state.setdefault("reads", {})
    stamp = [info.st_mtime_ns, info.st_size]
    entry = reads.get(key)
    if entry and entry.get("stamp") == stamp and not entry.get("refused"):
        entry["refused"] = True
        _save(path, state)
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"MEMORY OS: {Path(raw).name} has not changed since you read it earlier in this session, so its "
                f"content is already in your context. Use that. If you no longer have it, call Read again and it "
                f"will go through."),
        }}
    reads[key] = {"stamp": stamp, "refused": False}
    _save(path, state)
    return None


def _glob_match(rel: str, pattern: str) -> bool:
    """Path glob where `**` spans folders and `*` stays inside one path segment."""
    regex, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            regex += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            regex += ".*"
            i += 2
        elif pattern[i] == "*":
            regex += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            regex += "[^/]"
            i += 1
        else:
            regex += re.escape(pattern[i])
            i += 1
    return re.fullmatch(regex, rel, re.IGNORECASE if os.name == "nt" else 0) is not None


def memory_guard(payload: dict) -> dict | None:
    """PreToolUse on edits and shell commands: surface a saved rule or lesson when it applies.

    Each guard interrupts once per session, with the memory's text. A retry
    goes through, so a guard informs the decision and never blocks the work.
    """
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if _off("MOS_GUARDS") or (tool not in EDIT_TOOLS and tool != "Bash"):
        return None
    store = Store(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if not store.exists():
        return None
    guarded = [m for m in store.all() if m.guards and m.status == "active"]
    if not guarded:
        return None
    rel = ""
    if tool in EDIT_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        try:
            rel = Path(raw).resolve().relative_to(store.root.resolve()).as_posix()
        except (ValueError, OSError):
            return None
    command = str(tool_input.get("command") or "")
    path, state = _load(str(payload.get("session_id") or ""))
    shown = state.setdefault("guards", {})
    hits = []
    for mem in guarded:
        if shown.get(mem.id):
            continue
        for spec in mem.guards:
            try:
                kind, pattern = checks.parse_guard(spec)
            except ValueError:
                continue
            if kind == "edit" and rel and _glob_match(rel, pattern):
                hits.append(mem)
                break
            if kind == "bash" and tool == "Bash" and re.search(pattern, command):
                hits.append(mem)
                break
    if not hits:
        return None
    for mem in hits:
        shown[mem.id] = True
    _save(path, state)
    ledger.record_use(store.root, [m.id for m in hits])
    notes = " ".join(f"[{m.id}] {' '.join(m.text.split())}" for m in hits)
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"MEMORY OS guard, saved for this project: {notes} -- Take this into account. If the action is still "
            f"right, retry it and it will go through."),
    }}


def pre_tool(payload: dict) -> dict | None:
    if payload.get("tool_name") == "Read":
        return read_guard(payload)
    return memory_guard(payload)


def trim_text(text: str, limit: int) -> tuple[str, dict] | None:
    """Keep the head, the tail and any error-looking lines in between. None if not worth it."""
    if len(text) <= limit:
        return None
    lines = text.splitlines()
    total = len(lines)
    if total <= HEAD_LINES + TAIL_LINES:
        # Few but enormous lines: cut each line down instead.
        kept = [ln if len(ln) <= MAX_LINE else ln[:MAX_LINE] + f" ...[{len(ln) - MAX_LINE} more chars]" for ln in lines]
        body, shown = "\n".join(kept), total
    else:
        clip = lambda ln: ln if len(ln) <= MAX_LINE else ln[:MAX_LINE] + " ..."  # noqa: E731
        head = [clip(ln) for ln in lines[:HEAD_LINES]]
        tail = [clip(ln) for ln in lines[-TAIL_LINES:]]
        middle = range(HEAD_LINES, total - TAIL_LINES)
        signal = [(i, lines[i]) for i in middle if SIGNAL.search(lines[i])][:SIGNAL_LINES]
        parts = head + [f"... [{len(middle)} lines omitted here"
                        + (f"; {len(signal)} that look like errors or warnings are shown below" if signal else "") + "] ..."]
        parts += [f"{i + 1}: {clip(ln)}" for i, ln in signal]
        if signal:
            parts.append("... [end of picked lines; the output's last lines follow] ...")
        parts += tail
        body, shown = "\n".join(parts), len(head) + len(tail) + len(signal)
    if len(body) > len(text) * 0.7:
        return None
    return body, {"lines": total, "shown": shown, "chars": len(text), "kept_chars": len(body)}


def _ledger_dir(payload: dict) -> Path:
    base = payload.get("scratchpad_dir")
    if base and Path(base).is_dir():
        return Path(base) / "memory-os-outputs"
    home = Path(os.environ.get("MOS_HOME") or Path.home() / ".memory-os")
    return home / "outputs"


def _trimmed(text, limit: int, payload: dict, label: str):
    """Trimmed replacement for one string, or None to leave it alone. Raises OSError if it cannot be saved."""
    if not isinstance(text, str):
        return None
    result = trim_text(text, limit)
    if not result:
        return None
    body, stats = result
    folder = _ledger_dir(payload)
    folder.mkdir(parents=True, exist_ok=True)
    full = folder / f"{hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest()[:12]}-{label}.txt"
    full.write_text(text, encoding="utf-8", errors="replace")
    return (f"{body}\n[MEMORY OS: showing {stats['shown']} of {stats['lines']} lines, about "
            f"{stats['kept_chars'] // 4} of {stats['chars'] // 4} tokens. Full output saved at {full} ; "
            f"Grep or Read that file if you need the rest.]")


def trim_output(payload: dict) -> dict | None:
    """PostToolUse: replace very long tool output with a trimmed view and keep the full text on disk.

    Covers Bash, PowerShell, Grep and MCP tools. The replacement keeps the
    tool's own output shape and only shortens the text inside it.
    """
    tool = str(payload.get("tool_name") or "")
    if _off("MOS_TRIM") or "memory-os" in tool:
        return None
    response = payload.get("tool_response")
    try:
        limit = int(os.environ.get("MOS_TRIM_CHARS", DEFAULT_TRIM_CHARS))
    except ValueError:
        limit = DEFAULT_TRIM_CHARS
    changed = False
    try:
        if tool in TRIM_FIELDS and isinstance(response, dict) and not response.get("isImage"):
            updated = dict(response)
            for field in TRIM_FIELDS[tool]:
                new = _trimmed(response.get(field), limit, payload, field)
                if new is not None:
                    updated[field] = new
                    changed = True
        elif tool.startswith("mcp__") and isinstance(response, list):
            updated = []
            for block in response:
                if isinstance(block, dict) and block.get("type") == "text":
                    new = _trimmed(block.get("text"), limit, payload, "mcp")
                    if new is not None:
                        block = {**block, "text": new}
                        changed = True
                updated.append(block)
        else:
            return None
    except OSError:
        return None  # never drop output that could not be saved in full
    if not changed:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": updated}}
