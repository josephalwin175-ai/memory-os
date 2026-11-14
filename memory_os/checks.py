"""Machine-checkable claims attached to memories.

A check is one line, `kind: arguments`, with arguments separated by ` :: `.
Checks are declarative on purpose: they only read files inside the project and
never run commands, so a memory received from someone else is safe to verify.

    file_exists:   path
    file_absent:   path
    file_contains: path :: regex
    file_lacks:    path :: regex
    json_has:      path :: dotted.key
    json_equals:   path :: dotted.key :: expected value
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MAX_BYTES = 2_000_000
MAX_PATTERN = 300
KINDS = ("file_exists", "file_absent", "file_contains", "file_lacks", "json_has", "json_equals")


class CheckError(ValueError):
    pass


def parse(spec: str) -> tuple[str, list[str]]:
    kind, sep, rest = spec.partition(":")
    kind = kind.strip()
    if not sep or kind not in KINDS:
        raise CheckError(f"unknown check kind in {spec!r}; use one of {', '.join(KINDS)}")
    args = [a.strip() for a in rest.split(" :: ")]
    if kind == "json_equals":
        args = [a.strip() for a in rest.split(" :: ", 2)]
    wanted = {"file_exists": 1, "file_absent": 1, "file_contains": 2, "file_lacks": 2, "json_has": 2, "json_equals": 3}[kind]
    if len(args) != wanted or not all(args):
        raise CheckError(f"{kind} takes {wanted} argument(s) separated by ' :: '")
    return kind, args


GUARD_KINDS = ("edit", "bash")


def parse_guard(spec: str) -> tuple[str, str]:
    """A guard is `edit: <path glob>` or `bash: <regex>`; it fires before a matching tool call."""
    kind, sep, pattern = spec.partition(":")
    kind, pattern = kind.strip(), pattern.strip()
    if not sep or kind not in GUARD_KINDS or not pattern or len(pattern) > MAX_PATTERN:
        raise CheckError(f"guard must be 'edit: <path glob>' or 'bash: <regex>', got {spec!r}")
    if kind == "bash":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise CheckError(f"bad regex in guard: {exc}") from None
    return kind, pattern


def _resolve(root: Path, rel: str) -> Path:
    root = root.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise CheckError(f"path escapes the project: {rel}")
    return target


def _read(path: Path) -> str:
    if path.stat().st_size > MAX_BYTES:
        raise CheckError(f"file too large to check: {path.name}")
    return path.read_text(encoding="utf-8", errors="replace")


def _dig(data, dotted: str):
    for part in dotted.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            raise KeyError(dotted)
    return data


def run(root: Path, spec: str) -> tuple[bool, str]:
    """Return (passed, detail). A malformed or unreadable check counts as failed."""
    try:
        kind, args = parse(spec)
        path = _resolve(root, args[0])
        if kind == "file_exists":
            return path.exists(), "" if path.exists() else f"{args[0]} is missing"
        if kind == "file_absent":
            return not path.exists(), "" if not path.exists() else f"{args[0]} exists"
        if not path.is_file():
            return False, f"{args[0]} is missing"
        text = _read(path)
        if kind in ("file_contains", "file_lacks"):
            if len(args[1]) > MAX_PATTERN:
                raise CheckError("pattern too long")
            found = re.search(args[1], text, re.MULTILINE) is not None
            if kind == "file_contains":
                return found, "" if found else f"{args[0]} no longer matches /{args[1]}/"
            return not found, "" if not found else f"{args[0]} now matches /{args[1]}/"
        data = json.loads(text)
        try:
            value = _dig(data, args[1])
        except KeyError:
            return False, f"{args[0]} has no key {args[1]}"
        if kind == "json_has":
            return True, ""
        actual = value if isinstance(value, str) else json.dumps(value)
        ok = actual == args[2]
        return ok, "" if ok else f"{args[0]} {args[1]} is {actual!r}, expected {args[2]!r}"
    except (CheckError, OSError, ValueError, re.error) as exc:
        return False, str(exc)


def run_all(root: Path, specs: list[str]) -> list[tuple[str, bool, str]]:
    return [(spec, *run(root, spec)) for spec in specs]
