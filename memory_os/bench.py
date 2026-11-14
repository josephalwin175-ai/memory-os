"""ContinuityBench (retrieval edition): does the right memory reach the context?

A seeded generator builds a synthetic project history: facts across a dozen
areas, decisions that were later reversed, facts that have since become false
on disk, and unrelated distractors. Each task names an area and needs that
area's current facts.

It measures three ways of filling the same token budget:

* compiled: the MEMORY OS brief for the task
* recent:   the most recently saved memories, with no checks and no task
* everything: all memories ever saved, with no budget

Reported per method: recall of the needed facts, how many wrong memories
(reversed or no longer true) were included, and tokens used.

This measures retrieval and staleness handling only. It does not run a model,
so it says nothing about task success, which needs a model-in-the-loop run.
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

from .brief import compile_brief, estimate_tokens
from .store import Store

AREAS = {
    "database": ("postgres", "schema", "migration", "index", "query"),
    "auth": ("login", "token", "session", "password", "oauth"),
    "api": ("endpoint", "route", "handler", "request", "response"),
    "deploy": ("docker", "container", "pipeline", "release", "rollback"),
    "cache": ("redis", "ttl", "invalidate", "eviction", "warmup"),
    "queue": ("worker", "job", "retry", "backoff", "consumer"),
    "frontend": ("component", "render", "stylesheet", "layout", "bundle"),
    "testing": ("fixture", "mock", "coverage", "snapshot", "assertion"),
    "logging": ("trace", "metric", "alert", "dashboard", "sampling"),
    "payments": ("invoice", "refund", "webhook", "currency", "ledger"),
    "search": ("ranking", "tokenizer", "facet", "synonym", "reindex"),
    "email": ("template", "bounce", "smtp", "digest", "unsubscribe"),
}
FILLER = ("Agreed after review.", "Documented for the team.", "Confirmed during the last sprint.",
          "Raised in planning and settled.", "Checked against production behaviour.")


def build(root: Path, seed: int = 0, facts_per_area: int = 4) -> dict:
    """Create the synthetic project in `root` and return its ground truth."""
    rng = random.Random(seed)
    store = Store(root)
    store.init()
    (root / "config").mkdir(exist_ok=True)
    truth = {"needed": {}, "wrong": set(), "order": []}
    plans = []
    for area, words in AREAS.items():
        for n in range(facts_per_area):
            plans.append((area, words, n))
    rng.shuffle(plans)
    for area, words, n in plans:
        word = words[n % len(words)]
        other = words[(n + 2) % len(words)]
        marker = f"{area}-{word}-{n}"
        flag = root / "config" / f"{marker}.conf"
        flag.write_text(f"setting={marker}\n", encoding="utf-8")
        roll = rng.random()
        text = (f"The {area} {word} setting is {marker}\n"
                f"It affects {other} behaviour in the {area} area. {rng.choice(FILLER)}")
        check = [f"file_contains: config/{marker}.conf :: setting={marker}"]
        if roll < 0.2:
            # A decision that was later reversed: only the replacement is current.
            old = store.save(f"The {area} {word} setting is legacy-{marker}\nOriginal choice for {other}.",
                             type="decision", mem_id=f"{marker}-old")
            new = store.save(text, type="decision", mem_id=marker, checks=check, supersedes=old.id)
            truth["wrong"].add(old.id)
            truth["needed"].setdefault(area, []).append(new.id)
            truth["order"] += [old.id, new.id]
        elif roll < 0.4:
            # True when saved, false now: the file changed underneath it.
            mem = store.save(text, type="fact", mem_id=marker, checks=check)
            flag.write_text("setting=changed-since\n", encoding="utf-8")
            truth["wrong"].add(mem.id)
            truth["order"].append(mem.id)
        else:
            mem = store.save(text, type="fact", mem_id=marker, checks=check)
            truth["needed"].setdefault(area, []).append(mem.id)
            truth["order"].append(mem.id)
    return truth


def _score(included: list[str], needed: list[str], wrong: set[str]) -> tuple[float, int]:
    recall = len(set(included) & set(needed)) / len(needed) if needed else 1.0
    return recall, len(set(included) & wrong)


def run(seed: int = 0, budget: int = 600, facts_per_area: int = 4) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        truth = build(root, seed, facts_per_area)
        store = Store(root)
        by_id = {m.id: m for m in store.all()}
        everything_tokens = sum(estimate_tokens(m.text) for m in by_id.values())
        totals = {name: {"recall": 0.0, "wrong": 0, "tokens": 0} for name in ("compiled", "recent", "everything")}
        tasks = 0
        for area, words in AREAS.items():
            needed = truth["needed"].get(area, [])
            if not needed:
                continue
            tasks += 1
            task = f"fix the {area} {words[0]} and {words[1]} problem"
            brief = compile_brief(store, task=task, budget=budget)
            recent, used = [], 0
            for mem_id in reversed(truth["order"]):
                cost = estimate_tokens(by_id[mem_id].text) + 1
                if used + cost > budget:
                    break
                recent.append(mem_id)
                used += cost
            for name, included, tokens in (("compiled", brief["included"], brief["tokens"]),
                                           ("recent", recent, used),
                                           ("everything", list(by_id), everything_tokens)):
                recall, wrong = _score(included, needed, truth["wrong"])
                totals[name]["recall"] += recall
                totals[name]["wrong"] += wrong
                totals[name]["tokens"] += tokens
        for row in totals.values():
            row["recall"] = row["recall"] / tasks if tasks else 0.0
            row["wrong"] = row["wrong"] / tasks if tasks else 0.0
            row["tokens"] = round(row["tokens"] / tasks) if tasks else 0
        return {"seed": seed, "budget": budget, "tasks": tasks, "memories": len(by_id),
                "wrong_in_store": len(truth["wrong"]), "methods": totals}


def render(result: dict) -> str:
    lines = [
        f"ContinuityBench, retrieval edition (seed {result['seed']}, budget {result['budget']} tokens)",
        f"{result['memories']} memories, of which {result['wrong_in_store']} are reversed or no longer true; "
        f"{result['tasks']} tasks",
        "",
        f"{'method':<12}{'recall of needed':>18}{'wrong included':>16}{'tokens':>9}",
    ]
    for name, row in result["methods"].items():
        lines.append(f"{name:<12}{row['recall'] * 100:>17.0f}%{row['wrong']:>16.1f}{row['tokens']:>9}")
    lines += ["", "Averages per task. 'wrong included' counts reversed or stale memories handed over as if true.",
              "Synthetic data, token counts estimated at 4 chars/token, no model in the loop:",
              "this shows what reaches the context, not whether a task then succeeds."]
    return "\n".join(lines)
