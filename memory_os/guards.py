"""Per-tool-call savers: skip re-reads of unchanged files and trim huge command output.

Both fail open. If anything is missing or unexpected, the hook prints nothing
and the tool call proceeds exactly as it would without MEMORY OS.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .transcript import _state_path

MIN_DEDUP_BYTES = 2000      # below this a re-read is cheaper than the refusal round trip
DEFAULT_TRIM_CHARS = 12_000  # roughly 3K tokens
HEAD_LINES, TAIL_LINES, SIGNAL_LINES, MAX_LINE = 40, 80, 40, 400
SIGNAL = re.compile(r"(?i)\b(error|errors|fail|failed|failure|exception|traceback|fatal|panic|warning|denied|"
                    r"not found|cannot|undefined|assert)")


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


def trim_output(payload: dict) -> dict | None:
    """PostToolUse on Bash: replace very long output with a trimmed view and keep the full text on disk."""
    if _off("MOS_TRIM") or payload.get("tool_name") != "Bash":
        return None
    response = payload.get("tool_response")
    if not isinstance(response, dict) or response.get("isImage"):
        return None
    try:
        limit = int(os.environ.get("MOS_TRIM_CHARS", DEFAULT_TRIM_CHARS))
    except ValueError:
        limit = DEFAULT_TRIM_CHARS
    updated = dict(response)
    notes = []
    for field in ("stdout", "stderr"):
        text = response.get(field)
        if not isinstance(text, str):
            continue
        result = trim_text(text, limit)
        if not result:
            continue
        body, stats = result
        folder = _ledger_dir(payload)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            full = folder / f"{hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest()[:12]}-{field}.txt"
            full.write_text(text, encoding="utf-8", errors="replace")
        except OSError:
            return None  # never drop output that could not be saved in full
        updated[field] = (f"{body}\n[MEMORY OS: showing {stats['shown']} of {stats['lines']} lines, about "
                          f"{stats['kept_chars'] // 4} of {stats['chars'] // 4} tokens. Full output saved at {full} ; "
                          f"Grep or Read that file if you need the rest.]")
        notes.append(field)
    if not notes:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": updated}}
