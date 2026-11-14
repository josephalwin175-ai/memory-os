"""Memory store: one Markdown file per memory in `.memory/`.

The files are the source of truth, so they can be read, diffed and reviewed in
git. Search builds a throwaway in-memory index on each call, which keeps the
store free of stale index files.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import checks as checks_mod

TYPES = ("decision", "fact", "procedure", "preference", "failure", "status")
STATUSES = ("active", "stale", "superseded", "archived")
TRUSTS = ("user", "agent", "external")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DIR_NAME = ".memory"
STOPWORDS = frozenset("""a an and are as at be but by can do does for from has have how i in is it its of on or
that the their then there these this to was we were what when where which who why will with you your fix add make
use using please need want should would could problem issue""".split())


@dataclass
class Memory:
    id: str
    text: str
    type: str = "fact"
    status: str = "active"
    trust: str = "agent"
    pin: bool = False
    created: str = ""
    verified: str = ""
    supersedes: str = ""
    source: str = ""
    checks: list[str] = field(default_factory=list)
    guards: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        for line in self.text.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:120]
        return self.id

    def dump(self) -> str:
        lines = ["---", f"id: {self.id}", f"type: {self.type}", f"status: {self.status}",
                 f"trust: {self.trust}", f"pin: {'true' if self.pin else 'false'}",
                 f"created: {self.created}", f"verified: {self.verified}"]
        if self.supersedes:
            lines.append(f"supersedes: {self.supersedes}")
        if self.source:
            lines.append(f"source: {self.source}")
        if self.checks:
            lines.append("checks:")
            lines += [f"  - {c}" for c in self.checks]
        if self.guards:
            lines.append("guards:")
            lines += [f"  - {g}" for g in self.guards]
        lines += ["---", self.text.strip(), ""]
        return "\n".join(lines)


def parse(raw: str) -> Memory | None:
    lines = raw.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None
    meta: dict[str, str] = {}
    lists: dict[str, list[str]] = {"checks": [], "guards": []}
    current = None
    for line in lines[1:end]:
        if current and line.lstrip().startswith("- "):
            lists[current].append(line.lstrip()[2:].strip())
            continue
        current = None
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key in lists and not value:
            current = key
        else:
            meta[key] = value
    if not ID_RE.match(meta.get("id", "")):
        return None
    return Memory(
        id=meta["id"], text="\n".join(lines[end + 1:]).strip(),
        type=meta.get("type") if meta.get("type") in TYPES else "fact",
        status=meta.get("status") if meta.get("status") in STATUSES else "active",
        trust=meta.get("trust") if meta.get("trust") in TRUSTS else "external",
        pin=meta.get("pin", "").lower() == "true",
        created=meta.get("created", ""), verified=meta.get("verified", ""),
        supersedes=meta.get("supersedes", ""), source=meta.get("source", ""),
        checks=lists["checks"], guards=lists["guards"],
    )


def find_root(start: Path | str | None = None) -> Path:
    """Nearest ancestor holding `.memory`, else nearest git root, else `start`."""
    start = Path(start or Path.cwd()).resolve()
    chain = [start, *start.parents]
    for marker in (DIR_NAME, ".git"):
        for folder in chain:
            if (folder / marker).exists():
                return folder
    return start


def slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:6]
    return ("-".join(words)[:48].strip("-")) or "memory"


class Store:
    def __init__(self, root: Path | str | None = None, sub: str = ""):
        self.root = find_root(root)
        self.dir = self.root / DIR_NAME / sub if sub else self.root / DIR_NAME

    def exists(self) -> bool:
        return self.dir.is_dir()

    def init(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir

    def all(self) -> list[Memory]:
        if not self.exists():
            return []
        found = []
        for path in sorted(self.dir.glob("*.md")):
            try:
                mem = parse(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if mem:
                found.append(mem)
        return found

    def get(self, mem_id: str) -> Memory | None:
        if not ID_RE.match(mem_id or ""):
            return None
        path = self.dir / f"{mem_id}.md"
        if not path.is_file():
            return None
        return parse(path.read_text(encoding="utf-8", errors="replace"))

    def write(self, mem: Memory) -> None:
        self.init()
        (self.dir / f"{mem.id}.md").write_text(mem.dump(), encoding="utf-8", newline="\n")

    def save(self, text: str, type: str = "fact", mem_id: str = "", checks: list[str] | None = None,
             supersedes: str = "", pin: bool = False, trust: str = "agent", source: str = "",
             guards: list[str] | None = None) -> Memory:
        text = (text or "").strip()
        if not text:
            raise ValueError("memory text is empty")
        if type not in TYPES:
            raise ValueError(f"type must be one of {', '.join(TYPES)}")
        if trust not in TRUSTS:
            raise ValueError(f"trust must be one of {', '.join(TRUSTS)}")
        for spec in checks or []:
            checks_mod.parse(spec)
        for spec in guards or []:
            checks_mod.parse_guard(spec)
        today = date.today().isoformat()
        if mem_id:
            if not ID_RE.match(mem_id):
                raise ValueError("id must be lowercase letters, digits and dashes")
        else:
            base = slugify(text.splitlines()[0])
            mem_id, n = base, 2
            while (self.dir / f"{mem_id}.md").exists():
                mem_id, n = f"{base[:44]}-{n}", n + 1
        previous = self.get(mem_id)
        mem = Memory(id=mem_id, text=text, type=type, trust=trust, pin=pin,
                     created=previous.created if previous else today, verified=today,
                     supersedes=supersedes, source=source, checks=list(checks or []),
                     guards=list(guards if guards is not None else (previous.guards if previous else [])))
        if supersedes:
            old = self.get(supersedes)
            if old is None:
                raise ValueError(f"cannot supersede unknown memory {supersedes!r}")
            old.status = "superseded"
            self.write(old)
        self.write(mem)
        return mem

    def set_status(self, mem_id: str, status: str) -> Memory:
        mem = self.get(mem_id)
        if mem is None:
            raise ValueError(f"no memory {mem_id!r}")
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        mem.status = status
        if status == "active":
            mem.verified = date.today().isoformat()
        self.write(mem)
        return mem

    def search(self, query: str, limit: int = 8, include_inactive: bool = False) -> list[Memory]:
        pool = [m for m in self.all() if include_inactive or m.status == "active"]
        words = re.findall(r"[A-Za-z0-9_]+", query or "")
        # Common words match nearly every memory and bury the ones that matter.
        terms = [w for w in words if w.lower() not in STOPWORDS] or words
        if not pool or not terms:
            return pool[:limit]
        by_id = {m.id: m for m in pool}
        try:
            db = sqlite3.connect(":memory:")
            db.execute("create virtual table m using fts5(id unindexed, body, tokenize='porter unicode61')")
            db.executemany("insert into m values (?, ?)",
                           [(m.id, f"{m.id.replace('-', ' ')} {m.type} {m.text}") for m in pool])
            match = " OR ".join(f'"{t}"' for t in terms)
            rows = db.execute("select id from m where m match ? order by bm25(m) limit ?", (match, limit)).fetchall()
            db.close()
            return [by_id[r[0]] for r in rows]
        except sqlite3.Error:
            lowered = [t.lower() for t in terms]
            scored = [(sum(m.text.lower().count(t) for t in lowered), m) for m in pool]
            return [m for score, m in sorted(scored, key=lambda x: -x[0]) if score][:limit]

    def verify(self, apply: bool = False) -> list[dict]:
        """Run every active or stale memory's checks. With apply, update statuses."""
        report = []
        today = date.today().isoformat()
        for mem in self.all():
            if mem.status not in ("active", "stale") or not mem.checks:
                continue
            results = checks_mod.run_all(self.root, mem.checks)
            failures = [f"{spec} ({detail})" for spec, ok, detail in results if not ok]
            report.append({"id": mem.id, "title": mem.title, "ok": not failures, "failures": failures})
            if apply:
                new_status = "stale" if failures else "active"
                if new_status != mem.status or not failures:
                    mem.status = new_status
                    if not failures:
                        mem.verified = today
                    self.write(mem)
        return report
