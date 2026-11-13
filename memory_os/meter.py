"""Usage profiler: reads Claude Code transcripts and shows where tokens went.

Everything runs locally and only aggregates are reported.

Weighted units use public API price ratios (fresh input 1, cache write 1.25,
cache read 0.1, output 5). Subscription limits may weigh these differently, so
treat the weighted column as an approximation of cost, not of your limit.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

W_IN, W_CW, W_CR, W_OUT = 1.0, 1.25, 0.1, 5.0
CHARS_PER_TOKEN = 4  # heuristic; tool-output figures are estimates

EXPLORE_TOOLS = {"Read", "Grep", "Glob"}
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# Share of each addressable amount a lever is assumed to capture.
CAPTURE = {"rotation": 0.5, "big_outputs": 0.6, "dup_reads": 0.9, "orientation": 0.5, "cold_resume": 0.5}


def default_transcript_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _result_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    total = 0
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                total += len(block.get("text") or "")
    return total


def parse_session(path: Path, seen: set | None = None) -> dict | None:
    """Parse one transcript file into calls and tool results.

    `seen` holds message ids already counted in other files, so resumed or
    forked sessions that repeat earlier messages are not counted twice.
    """
    seen = seen if seen is not None else set()
    calls: list[dict] = []
    by_id: dict[str, dict] = {}
    tool_uses: dict[str, dict] = {}
    results: list[dict] = []
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if kind == "assistant":
                usage = msg.get("usage")
                mid = msg.get("id") or rec.get("uuid")
                if isinstance(usage, dict) and mid:
                    call = by_id.get(mid)
                    if call is None and mid in seen:
                        continue
                    if call is None:
                        seen.add(mid)
                        call = {"ts": _ts(rec.get("timestamp")), "in": 0, "cw": 0, "cr": 0, "out": 0}
                        by_id[mid] = call
                        calls.append(call)
                    # One API response spans several lines; keep the largest figures.
                    for key, field in (("in", "input_tokens"), ("cw", "cache_creation_input_tokens"),
                                       ("cr", "cache_read_input_tokens"), ("out", "output_tokens")):
                        call[key] = max(call[key], int(usage.get(field) or 0))
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_uses[block.get("id")] = {
                                "name": block.get("name") or "?",
                                "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                                "seq": len(tool_uses),
                            }
            elif kind == "user" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        results.append({
                            "tool_use_id": block.get("tool_use_id"),
                            "tokens": _result_chars(block.get("content")) / CHARS_PER_TOKEN,
                            "enters_at": len(calls),  # index of the next API call
                        })
    if not calls:
        return None
    return {"path": path, "calls": calls, "tool_uses": tool_uses, "results": results}


def _weighted_input(call: dict) -> float:
    return call["in"] * W_IN + call["cw"] * W_CW + call["cr"] * W_CR


def analyze_session(sess: dict, threshold: int) -> dict:
    calls, tool_uses = sess["calls"], sess["tool_uses"]
    n = len(calls)
    out = defaultdict(float)
    out["calls"] = n
    contexts = []
    prev_ts = None
    for call in calls:
        ctx = call["in"] + call["cw"] + call["cr"]
        contexts.append(ctx)
        out["in"] += call["in"]
        out["cw"] += call["cw"]
        out["cr"] += call["cr"]
        out["out"] += call["out"]
        if ctx > threshold:
            out["calls_over"] += 1
            out["rotation"] += _weighted_input(call) * (ctx - threshold) / ctx
        if prev_ts and call["ts"] and (call["ts"] - prev_ts).total_seconds() > 3600:
            out["cold_resumes"] += 1
            out["cold_resume"] += call["cw"] * W_CW
        prev_ts = call["ts"] or prev_ts
    out["weighted"] = out["in"] * W_IN + out["cw"] * W_CW + out["cr"] * W_CR + out["out"] * W_OUT

    # A tool result is written to cache once, then re-read by every later call.
    # Compaction can drop it earlier, so carried cost is an upper bound.
    def carried(tokens: float, enters_at: int) -> float:
        later = max(n - enters_at - 1, 0)
        return tokens * (W_CW + W_CR * later) if enters_at < n else 0.0

    first_edit = min((u["seq"] for u in tool_uses.values() if u["name"] in EDIT_TOOLS), default=None)
    seen_reads: set[tuple] = set()
    by_tool: dict[str, float] = defaultdict(float)
    ordered = sorted(sess["results"], key=lambda r: tool_uses.get(r["tool_use_id"], {}).get("seq", 0))
    edits_by_seq = sorted((u["seq"], u["input"].get("file_path")) for u in tool_uses.values()
                          if u["name"] in EDIT_TOOLS)
    edit_ptr = 0
    for res in ordered:
        use = tool_uses.get(res["tool_use_id"])
        if not use:
            continue
        cost = carried(res["tokens"], res["enters_at"])
        by_tool[use["name"]] += cost
        out["tool_output"] += cost
        if res["tokens"] > 2000:
            out["big_outputs"] += cost * (res["tokens"] - 2000) / res["tokens"]
        if use["name"] in EXPLORE_TOOLS and (first_edit is None or use["seq"] < first_edit):
            out["orientation"] += cost
        # An edit to a file makes the next read of it legitimate again.
        while edit_ptr < len(edits_by_seq) and edits_by_seq[edit_ptr][0] < use["seq"]:
            edited = edits_by_seq[edit_ptr][1]
            seen_reads = {key for key in seen_reads if key[0] != edited}
            edit_ptr += 1
        if use["name"] == "Read":
            key = (use["input"].get("file_path"), use["input"].get("offset"), use["input"].get("limit"))
            if key in seen_reads:
                out["dup_reads"] += cost
                out["dup_read_count"] += 1
            seen_reads.add(key)
    out["contexts"] = contexts
    out["by_tool"] = dict(by_tool)
    return out


def profile(base: Path | None = None, threshold: int = 80_000, project: str | None = None) -> dict:
    base = base or default_transcript_dir()
    total = defaultdict(float)
    contexts: list[int] = []
    by_tool: dict[str, float] = defaultdict(float)
    by_project: dict[str, float] = defaultdict(float)
    sessions = 0
    first = last = None
    seen: set = set()
    for path in sorted(base.rglob("*.jsonl")) if base.exists() else []:
        try:
            proj = path.relative_to(base).parts[0]
        except (ValueError, IndexError):
            proj = "?"
        if project and project.lower() not in proj.lower():
            continue
        sess = parse_session(path, seen)
        if not sess:
            continue
        sessions += 1
        stats = analyze_session(sess, threshold)
        for key, value in stats.items():
            if isinstance(value, (int, float)):
                total[key] += value
        contexts.extend(stats["contexts"])
        for tool, cost in stats["by_tool"].items():
            by_tool[tool] += cost
        by_project[proj] += stats["weighted"]
        for call in sess["calls"]:
            if call["ts"]:
                first = call["ts"] if first is None or call["ts"] < first else first
                last = call["ts"] if last is None or call["ts"] > last else last

    weighted = total["weighted"] or 1.0
    levers = {}
    remaining = 1.0
    for name, capture in CAPTURE.items():
        share = min(total[name] / weighted, 1.0)
        levers[name] = {"addressable_share": share, "assumed_capture": capture}
        remaining *= 1 - share * capture
    contexts.sort()
    return {
        "sessions": sessions,
        "calls": int(total["calls"]),
        "first": first.date().isoformat() if first else None,
        "last": last.date().isoformat() if last else None,
        "threshold": threshold,
        "tokens": {k: int(total[k]) for k in ("in", "cw", "cr", "out")},
        "weighted": {
            "in": total["in"] * W_IN, "cw": total["cw"] * W_CW,
            "cr": total["cr"] * W_CR, "out": total["out"] * W_OUT, "total": total["weighted"],
        },
        "context": {
            "median": int(statistics.median(contexts)) if contexts else 0,
            "p90": contexts[int(len(contexts) * 0.9)] if contexts else 0,
            "peak": contexts[-1] if contexts else 0,
            "calls_over_share": total["calls_over"] / (total["calls"] or 1),
        },
        "tool_output_share": min(total["tool_output"] / weighted, 1.0),
        "by_tool": sorted(((t, c / weighted) for t, c in by_tool.items()), key=lambda x: -x[1])[:8],
        "by_project": sorted(((p, c / weighted) for p, c in by_project.items()), key=lambda x: -x[1])[:6],
        "dup_read_count": int(total["dup_read_count"]),
        "cold_resumes": int(total["cold_resumes"]),
        "levers": levers,
        "combined_estimate": 1 - remaining,
    }


def _n(value: float) -> str:
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= size:
            return f"{value / size:.1f}{unit}"
    return f"{value:.0f}"


def _pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


LEVER_LABELS = {
    "rotation": "Context above the rotation threshold",
    "big_outputs": "Tool output beyond 2K tokens per result",
    "dup_reads": "Re-reads of unchanged files",
    "orientation": "Exploration before the first edit",
    "cold_resume": "Cache rebuilt after a 1h+ break",
}


def render(p: dict) -> str:
    if not p["sessions"]:
        return "No Claude Code transcripts with usage data were found."
    w = p["weighted"]
    t = p["tokens"]
    total = w["total"] or 1.0
    lines = [
        f"MEMORY OS usage profile: {p['sessions']} sessions, {p['calls']} API calls, {p['first']} to {p['last']}",
        "",
        "Where the tokens went          raw      weighted   share",
    ]
    for label, key in (("Fresh input", "in"), ("Cache writes", "cw"), ("Cache reads (re-sent history)", "cr"),
                       ("Model output", "out")):
        lines.append(f"  {label:<30}{_n(t[key]):>7}  {_n(w[key]):>9}  {_pct(w[key] / total)}")
    c = p["context"]
    lines += [
        "",
        f"Context per call: median {_n(c['median'])}, p90 {_n(c['p90'])}, peak {_n(c['peak'])}",
        f"Calls above {_n(p['threshold'])} of context: {_pct(c['calls_over_share']).strip()}",
        "",
        f"Tool output, including being carried in later turns: about {_pct(p['tool_output_share']).strip()} of weighted usage",
    ]
    for tool, share in p["by_tool"]:
        lines.append(f"  {tool[:28]:<30}{_pct(share)}")
    lines += ["", "Addressable usage (upper bounds; they overlap)        share   assumed capture"]
    for name, lever in p["levers"].items():
        lines.append(f"  {LEVER_LABELS[name]:<48}{_pct(lever['addressable_share'])}   {lever['assumed_capture'] * 100:.0f}%")
    lines += [
        "",
        f"Re-reads of unchanged files: {p['dup_read_count']}.  Resumes after a 1h+ break: {p['cold_resumes']}.",
        f"Rough combined estimate of reducible usage: {_pct(p['combined_estimate']).strip()}",
        "",
        "Top projects by weighted usage",
    ]
    for proj, share in p["by_project"]:
        lines.append(f"  {proj[:44]:<46}{_pct(share)}")
    lines += [
        "",
        "Weighted = API price ratios (input 1, cache write 1.25, cache read 0.1, output 5).",
        "Tool-output figures are estimated at 4 characters per token. The combined figure",
        "is an estimate built on the assumed capture rates, not a measured saving.",
    ]
    return "\n".join(lines)
