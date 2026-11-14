"""Inbox: candidate memories waiting for review.

Anything found automatically (extraction, imports) lands here first. Nothing
in the inbox is ever placed in a session brief. A candidate becomes a memory
only when it is accepted, and a rejected one is remembered so it is not
proposed again.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

from .store import Memory, Store, slugify

SUB = "inbox"
REJECTED = ".rejected"


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _digest(text: str) -> str:
    return hashlib.sha1(_norm(text).encode()).hexdigest()[:16]


def box(store: Store) -> Store:
    return Store(store.root, sub=SUB)


def _rejected(store: Store) -> set[str]:
    try:
        return set((box(store).dir / REJECTED).read_text(encoding="utf-8").split())
    except OSError:
        return set()


def add(store: Store, text: str, type: str = "fact", source: str = "", checks: list[str] | None = None) -> Memory | None:
    """Queue a candidate. Returns None when it duplicates a memory, a candidate or a past rejection."""
    text = (text or "").strip()
    if len(_norm(text)) < 12:
        return None
    inbox = box(store)
    title = _norm(text.splitlines()[0])
    known = {_norm(m.title) for m in store.all()} | {_norm(m.title) for m in inbox.all()}
    if title in known or _digest(text) in _rejected(store):
        return None
    base = slugify(text.splitlines()[0])
    mem_id, n = base, 2
    while (inbox.dir / f"{mem_id}.md").exists() or (store.dir / f"{mem_id}.md").exists():
        mem_id, n = f"{base[:44]}-{n}", n + 1
    today = date.today().isoformat()
    mem = Memory(id=mem_id, text=text, type=type, trust="external", created=today, source=source, checks=list(checks or []))
    inbox.write(mem)
    store.ignore(f"{SUB}/")  # candidates are unreviewed and personal until accepted
    return mem


def accept(store: Store, mem_id: str, trust: str = "user") -> Memory:
    inbox = box(store)
    cand = inbox.get(mem_id)
    if cand is None:
        raise ValueError(f"no candidate {mem_id!r} in the inbox")
    mem = store.save(text=cand.text, type=cand.type, checks=cand.checks, trust=trust, source=cand.source,
                     mem_id="" if store.get(cand.id) else cand.id)
    (inbox.dir / f"{mem_id}.md").unlink(missing_ok=True)
    return mem


def reject(store: Store, mem_id: str) -> None:
    inbox = box(store)
    cand = inbox.get(mem_id)
    if cand is None:
        raise ValueError(f"no candidate {mem_id!r} in the inbox")
    with open(inbox.dir / REJECTED, "a", encoding="utf-8") as handle:
        handle.write(_digest(cand.text) + "\n")
    (inbox.dir / f"{mem_id}.md").unlink(missing_ok=True)
