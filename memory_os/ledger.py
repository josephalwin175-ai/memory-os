"""Cost ledger: what each memory costs to carry and whether it gets used.

Cost is counted when a memory's text is injected into a session brief. Use is
counted when the agent fetches it (memory_get), when it comes back from a
search, or when its guard fires. This is bookkeeping, not proof of benefit: a
memory can be read from the brief and help without ever being fetched. The
report therefore only suggests, and never changes a memory by itself.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

from .store import Store

UNUSED_AFTER = 5  # injections with no recorded use before a memory is flagged


def _path(root: Path) -> Path:
    home = Path(os.environ.get("MOS_HOME") or Path.home() / ".memory-os")
    key = hashlib.sha1(os.path.normcase(str(root.resolve())).encode()).hexdigest()[:12]
    return home / "ledger" / f"{key}.json"


def _load(root: Path) -> dict:
    try:
        data = json.loads(_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(root: Path, data: dict) -> None:
    path = _path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def record_injection(root: Path, costs: dict[str, int]) -> None:
    if not costs:
        return
    data = _load(root)
    for mem_id, tokens in costs.items():
        row = data.setdefault(mem_id, {"injected": 0, "tokens": 0, "used": 0, "last_used": ""})
        row["injected"] += 1
        row["tokens"] += int(tokens)
    _save(root, data)


def record_use(root: Path, ids: list[str]) -> None:
    if not ids:
        return
    data = _load(root)
    today = date.today().isoformat()
    for mem_id in ids:
        row = data.setdefault(mem_id, {"injected": 0, "tokens": 0, "used": 0, "last_used": ""})
        row["used"] += 1
        row["last_used"] = today
    _save(root, data)


def report(store: Store) -> dict:
    data = _load(store.root)
    rows, suggestions = [], []
    for mem in store.all():
        row = data.get(mem.id, {"injected": 0, "tokens": 0, "used": 0, "last_used": ""})
        rows.append({"id": mem.id, "type": mem.type, "pin": mem.pin, "guarded": bool(mem.guards), **row})
        if mem.status != "active" or row["injected"] < UNUSED_AFTER or row["used"]:
            continue
        if mem.type in ("failure", "preference", "procedure") and not mem.guards:
            suggestions.append(f"[{mem.id}] injected {row['injected']} times ({row['tokens']} tokens), never fetched. "
                               f"If it is a rule about certain files or commands, promote it to a guard so it costs "
                               f"nothing until it applies: mos promote {mem.id} --edit \"<glob>\"")
        elif mem.pin:
            suggestions.append(f"[{mem.id}] is pinned, injected {row['injected']} times ({row['tokens']} tokens), "
                               f"never fetched. Consider unpinning it.")
        else:
            suggestions.append(f"[{mem.id}] injected {row['injected']} times ({row['tokens']} tokens), never fetched. "
                               f"Consider archiving it: mos status {mem.id} archived")
    rows.sort(key=lambda r: -r["tokens"])
    return {"rows": rows, "suggestions": suggestions,
            "total_tokens": sum(r["tokens"] for r in rows), "total_uses": sum(r["used"] for r in rows)}


def render(rep: dict) -> str:
    if not rep["rows"]:
        return "No memories yet."
    lines = ["Memory cost ledger (tokens are estimates at 4 chars/token)", "",
             f"{'memory':<44}{'injected':>9}{'tokens':>8}{'used':>6}  last used"]
    for r in rep["rows"]:
        flag = " (guard)" if r["guarded"] else (" (pin)" if r["pin"] else "")
        lines.append(f"{(r['id'] + flag)[:43]:<44}{r['injected']:>9}{r['tokens']:>8}{r['used']:>6}  {r['last_used'] or '-'}")
    lines += ["", f"Total carried: {rep['total_tokens']} tokens across all sessions; {rep['total_uses']} recorded uses."]
    if rep["suggestions"]:
        lines += ["", "Suggestions:"] + [f"  {s}" for s in rep["suggestions"]]
    lines += ["", "A memory can help from the brief without ever being fetched, so treat these as prompts to review."]
    return "\n".join(lines)
