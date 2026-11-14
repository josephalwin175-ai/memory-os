# MEMORY OS

**Your AI's memory, with tests.**

Most memory tools for AI coding agents remember everything and never check any of it. MEMORY OS treats a memory like code: it carries a check that can be re-run against your repo, and when the check fails the memory is flagged stale instead of being handed to the agent as fact.

Around that core it adds the things that make long sessions cheaper: a small session brief instead of re-explaining, a nudge to hand off before a session gets expensive, no re-reading of unchanged files, trimmed command output, a code map, and a profiler that shows where your Claude Code tokens actually go.

- No dependencies. Python 3.10+ standard library only.
- No API key, no server, no background process spending tokens.
- Memories are plain Markdown files in `.memory/`, so they diff and review like code and outlive any subscription.
- Works as a Claude Code plugin, a standalone skill, a command line tool, and an MCP server for other clients.

Status: early (v0.2). Read [Known limits](#known-limits) before relying on any number.

## Quick start

You need Python 3.10 or newer and Claude Code.

**1. Install** (once). In Claude Code:

```
/plugin marketplace add josephalwin175-ai/memory-os
/plugin install memory-os@memory-os
```

Then, in a terminal, install the `mos` command:

```bash
pip install git+https://github.com/josephalwin175-ai/memory-os.git
```

If `mos` is not found afterwards, your Python scripts folder is not on PATH; `python -m memory_os` does the same thing.

**2. See where your tokens go.** This works immediately and changes nothing:

```bash
mos meter
```

**3. Turn memory on for a project.** Memory is opt-in per project. In the project folder:

```bash
mos init
```

From then on, new Claude Code sessions in that project start with a short brief of what is saved. Nothing else to do.

**4. Let memories build up.** Ask Claude ("remember that we chose PostgreSQL because we need joins"), or save one yourself:

```bash
mos save "Database is PostgreSQL via pg. Chosen over MongoDB for joins." --type decision \
  --check "json_has: package.json :: dependencies.pg"
```

The `--check` is the point. It is a claim about your repo that can be re-tested. If someone later removes `pg`, the memory is flagged stale instead of being believed.

**5. Keep it honest.** From time to time:

```bash
mos verify --fix    # re-test every memory against the repo
mos inbox           # review what MEMORY OS noticed; accept or reject each
mos ledger          # which memories cost tokens and never get used
```

**6. Hand off long sessions.** When Claude suggests it, or a session feels slow, say "hand off". Claude saves where the work stands, and the next session starts from a small brief instead of the whole history.

**Update** with `/plugin marketplace update memory-os` in Claude Code and `pip install --upgrade git+https://github.com/josephalwin175-ai/memory-os.git` in a terminal.

## What happens in a session

| When | What MEMORY OS does |
|---|---|
| Session starts | Injects the brief: pinned memories, then the ones relevant or recent within a token budget, then any whose check failed, then a titles-only index. Silent in projects without `.memory/`. |
| You send a prompt | Checks the session's context size. Suggests a handoff once at 100K tokens and at each doubling, and once after a break of over an hour when the prompt cache has likely expired. Otherwise silent. |
| Claude reads a file | Refuses a repeat read of an unchanged file once, because its content is already in context. |
| Claude edits a file or runs a command | If a saved rule applies (a guard), shows it once. |
| A tool returns very long output | Replaces it with the head, the tail and any error-looking lines; the full text is saved to a file. |
| Before compaction and at session end | Records where you left off and queues candidate memories in the inbox. |

Every one of these fails open: on any error or unexpected input the hook prints nothing and Claude Code carries on exactly as it would without MEMORY OS.

## Features

### Memories that know when they are wrong

```bash
mos save "Backend is Express on port 4001" --check 'file_contains: package.json :: "express"'
mos verify
```

After someone swaps Express for Fastify:

```
STALE [backend-is-express-on-port-4001] Backend is Express on port 4001
        file_contains: package.json :: "express" (package.json no longer matches /"express"/)
```

Checks are declarative and read-only. They never run commands, and paths cannot leave the project, so verifying a memory written by someone else is safe.

| Check | Passes when |
|---|---|
| `file_exists: path` | the file is there |
| `file_absent: path` | the file is gone |
| `file_contains: path :: regex` | the regex matches |
| `file_lacks: path :: regex` | the regex does not match |
| `json_has: path :: dotted.key` | the key exists |
| `json_equals: path :: dotted.key :: value` | the value is exactly this |

`mos verify` exits non-zero when a memory fails, so it can run in CI. When a decision is replaced, save the new one with `--supersedes <old id>`; the old one is kept for history and never reaches a brief.

### The session brief

`mos brief "task"` prints what a session would start with. Pinned memories always appear. The rest are chosen by relevance to the task (or recency with no task) until the token budget is used. Memories whose check failed are listed as stale, at most eight, and only those matching the task when there is one. The brief is ordered from most stable to least stable so its leading part stays byte-identical between sessions and can hit the provider's prompt cache. It may run a little past its budget so there is always a pointer to what else exists.

### Trust levels

Every memory records where it came from: `user` (saved from the command line), `agent` (saved through MCP) or `external` (an imported or inbox candidate). External memories are never placed in a brief as facts. They appear by id only, labelled as data and not instructions, and their text is returned only when the agent asks for them by id.

### An inbox for things found automatically

Nothing found by a machine goes straight into memory. It lands in an inbox, is never placed in a brief, and becomes a memory only when you accept it.

```bash
mos inbox              # list candidates
mos accept <id>        # or: mos accept --all
mos reject <id>        # remembered, so it is not proposed again
```

Candidates come from three places:

- **Session extraction.** At session end and before compaction, a free offline pass reads your own messages for sentences that read like a decision ("let's use X instead of Y"), a standing preference ("never commit to main") or a project fact ("the API listens on port 4001"). It is deliberately simple and will propose some noise.
- **A model, if you want one.** Set `MOS_EXTRACT_CMD` to any program that reads a prompt on stdin, then run `mos extract --llm`. `ollama run llama3.2` is local and free; `claude -p --model haiku` uses your plan. It is never run automatically.
- **Importers**, below.

### Importers

```bash
mos import CLAUDE.md
mos import AGENTS.md
mos import .cursorrules
mos import conversations.json    # a ChatGPT data export
```

Bullets and short paragraphs from instruction files, and decision-like sentences from your side of ChatGPT conversations, are queued in the inbox. A MEMORY OS JSON export can be imported the same way.

### Guards: memories that cost nothing until they apply

Some memories only matter at one moment: "do not edit generated files by hand", "never force push". Carrying them in every session's prompt is waste. Promote one to a guard and it leaves the brief; a hook shows it only when a matching edit or command is attempted.

```bash
mos promote gen-rule --edit "src/gen/**"
mos promote no-force --bash "git\s+push\s+.*(--force|-f)\b"
```

`--edit` takes a path pattern relative to the project (`**` spans folders, `*` stays within one). `--bash` takes a regular expression matched against the command. A guard interrupts once per session with the memory's text. A retry goes through, so it informs the decision and never blocks the work.

`mos ledger` shows what each memory has cost to carry and whether it was ever fetched, and suggests which to promote, unpin or archive. It counts injections and fetches; it cannot prove a memory helped, so it only suggests.

### Where tokens go, and what to do about it

```bash
mos meter                     # all projects
mos meter --project myapp     # one project
mos meter --threshold 60000   # context size treated as "time to rotate"
```

Reports fresh input, cache writes, cache reads and output; context size per call; which tools produce the most carried output; re-reads of unchanged files; and an estimate of how much is reducible. It reads local transcripts and prints aggregates only.

The reducible figure is an estimate built on stated assumptions, not a measured saving. Weighted units use public API price ratios, and subscription limits may count tokens differently.

### No re-reading of unchanged files

A `PreToolUse` hook on `Read` notices a repeat read of a file that has not changed on disk. It refuses once, with a short note that the content is already in context.

- An immediate retry goes through, so the agent is never locked out of a file.
- Files under 2 KB are ignored, because the refusal would cost about as much as the read.
- Different line ranges, edited files and subagents are treated as new reads.
- The record is cleared on compaction and `/clear`, when the earlier content is gone.

### Trimmed tool output

A `PostToolUse` hook on `Bash`, `PowerShell`, `Grep` and MCP tools replaces output longer than about 3K tokens with its first 40 lines, its last 80 lines, and any lines in between that look like errors or warnings. The full output is saved to a file named in the trimmed text, so the agent can grep or read it when needed. If the full text cannot be saved, the output is left untouched. This relies on the `updatedToolOutput` hook field; on a Claude Code version without it, the hook has no effect.

### A code map instead of exploration

```bash
mos map                       # outline of the whole codebase
mos map "token auth"          # where matching functions, classes or files are
mos map --around src/auth.ts  # what a file imports, what imports it, memories about it
```

The map lists each source file with its functions and classes and their line numbers, so the agent can read the 30 lines it needs instead of opening whole files to look around. It is also an MCP tool (`code_map`).

Python is parsed properly. JavaScript, TypeScript, Go, Rust, Java, Kotlin, C#, Swift, Ruby, PHP and C/C++ use line patterns, which find most top-level definitions but can miss unusual ones. Files ignored by git are left out. When the outline would exceed the token budget, large files are outlined in part and the rest are listed by name. Import links (`--around`) cover Python, JavaScript and TypeScript.

### Works with Graphify

[Graphify](https://github.com/Graphify-Labs/graphify) is a separate tool that builds a knowledge graph of a codebase, including documents and concepts, and writes it to `graphify-out/`. The two answer different questions: Graphify tells you how the codebase hangs together, MEMORY OS tells you what was decided and whether it is still true. MEMORY OS never installs, runs or requires Graphify. When a graph is present it uses it in four places:

| Where | What Graphify adds |
|---|---|
| `mos map --around <file>` | the file's relationships from the graph, next to its imports and memories |
| `mos map "<words>"` | matching documents and concepts, which a code outline cannot see |
| the session brief | one line pointing the agent to `graphify-out/GRAPH_REPORT.md` |
| `mos graph` | whether the graph is out of date: commits behind, and source files changed since it was written |

Links that Graphify marks as inferred or ambiguous are labelled that way, so a guess is not read as a fact. Build the graph with Graphify's own instructions (for example `pip install graphifyy`, then `graphify extract` in your project). Both tools add Claude Code hooks; they act independently and either can be turned off.

The reader follows the field names in Graphify's exporter (checked against its source at version 0.9) and is tested on a fixture in that format. It has not been run against a graph produced by a live Graphify install.

### A pull-request check

When a pull request makes a saved memory false, comment on it. Add `.github/workflows/memory.yml`:

```yaml
name: memory
on: pull_request
permissions:
  contents: read
  pull-requests: write
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: josephalwin175-ai/memory-os@main
```

It runs every memory's checks against the pull request's code and comments with a table of the ones that no longer hold, updating its previous comment on later pushes. Set `fail: "true"` to fail the job as well. It reports memories that are false on the branch, including any that were already false before the change. On pull requests from forks the token is read-only, so the comment is skipped and the result appears in the job summary instead. The comment text is tested locally through `mos verify --markdown`; the workflow itself has not yet run on a live pull request.

### Your data outlives any subscription

Nothing in MEMORY OS depends on an AI provider, an account or a paid plan.

- Memories are plain Markdown files in your project, readable and editable with any text editor.
- Every command runs locally with no network access. The only exception is `mos extract --llm`, which runs the program you name.
- The MCP server works with any MCP client, so the same memory serves a different assistant if you switch.
- `mos export` writes everything to one file you can paste into any assistant, including free ones:

```bash
mos export --out project-knowledge.md
mos export --format json --out memories.json
```

### A benchmark

```bash
mos bench
```

A seeded generator builds a synthetic project history with reversed decisions, facts that have since become false on disk, and unrelated distractors, then compares three ways of filling the same budget.

| Method | Recall of needed facts | Wrong memories included | Tokens |
|---|---|---|---|
| MEMORY OS brief | 100% | 0 | 724 |
| Most recent memories | 26% | 9.0 | 591 |
| Everything | 100% | 24.0 | 1827 |

Seed 0, 600-token budget, averages over 12 tasks. This measures what reaches the context on synthetic data, with token counts estimated at 4 characters per token. No model is in the loop, so it does not show that tasks then succeed.

## Install

### Claude Code plugin (everything)

```
/plugin marketplace add josephalwin175-ai/memory-os
/plugin install memory-os@memory-os
```

Adds the MCP server, the hooks described above and three skills: `memory` (when and how to save), `handoff` (wrap up a long session) and `meter` (explain the usage profile).

To try it from a local checkout: `claude --plugin-dir /path/to/memory-os`.

The plugin runs `python`. On systems where the command is `python3`, edit `.claude-plugin/plugin.json` and `hooks/hooks.json`.

### The `mos` command

```bash
pip install git+https://github.com/josephalwin175-ai/memory-os.git
mos --help
```

Or run it from a checkout with `python bin/mos.py`.

### Standalone skill (commands only)

For agents that support skills but not plugins, or if you only want the commands:

1. Download [`dist/memory-os-skill.zip`](dist/memory-os-skill.zip).
2. Unzip it into your skills folder, for example `~/.claude/skills/`.

The skill is one instruction file plus `mos.pyz`, the whole tool as a single runnable file. It has every command but no hooks and no MCP server, so nothing happens automatically. Use either this or the plugin, not both. Rebuild it with `python scripts/build_skill.py`.

### Any MCP client (Cursor, Codex and others)

Add a stdio server that runs `python /path/to/memory-os/bin/mos.py mcp`, with `CLAUDE_PROJECT_DIR` set to the project folder or started from inside it. You get the six tools below. The hooks are specific to Claude Code.

### Uninstall

`/plugin uninstall memory-os@memory-os`, `pip uninstall memory-os`, then delete `~/.memory-os`. Memories stay in each project's `.memory/` folder until you delete it.

## Commands

Global option: `--root <folder>` to act on a project other than the current one.

| Command | What it does |
|---|---|
| `mos meter [--project text] [--threshold n] [--json]` | profile token usage from Claude Code transcripts |
| `mos init` | create `.memory/` in the current project |
| `mos save "text" [--type] [--id] [--check] [--pin] [--supersedes id]` | save a memory |
| `mos search "query" [--all]` | search memories |
| `mos list`, `mos get <id>` | browse memories |
| `mos verify [--fix] [--markdown]` | re-run all checks; `--fix` marks failures stale |
| `mos brief [task] [--budget n] [--receipt]` | print the compiled brief |
| `mos status <id> <status>` | set active, stale, superseded or archived |
| `mos promote <id> --edit glob \| --bash regex [--clear]` | turn a memory into a guard |
| `mos inbox`, `mos accept <id\|--all>`, `mos reject <id\|--all>` | review candidate memories |
| `mos extract [--llm] [--transcript file]` | find candidates in a session transcript |
| `mos import <file>` | queue knowledge from other tools |
| `mos capture [--transcript file]` | record what the last session was working on |
| `mos ledger [--json]` | cost and use of each memory |
| `mos map [query] [--path text] [--around file] [--budget n]` | outline the codebase, or show a file's connections |
| `mos graph [--json]` | status of a Graphify graph, if the project has one |
| `mos export [--format markdown\|json] [--out file] [--all]` | write all memories to one portable file |
| `mos bench [--seed n] [--budget n] [--json]` | run the retrieval benchmark |
| `mos mcp` | run the MCP server on stdio |

## MCP tools

`memory_search`, `memory_get`, `memory_save`, `context_compile`, `code_map`, `memory_verify`. Six tools with short descriptions, because every tool schema is paid for on every turn. `memory_save` accepts `checks`, `guards`, `supersedes` and `pin`; `code_map` accepts `query`, `path` and `around`.

## Switches

| Variable | Effect |
|---|---|
| `MOS_ROTATE_AT` | context size that triggers the handoff nudge (default 100000; `0` turns it off) |
| `MOS_DEDUP=0` | turn off read dedup |
| `MOS_TRIM=0` | turn off output trimming |
| `MOS_TRIM_CHARS` | output length that triggers trimming (default 12000) |
| `MOS_GUARDS=0` | turn off guards |
| `MOS_EXTRACT_CMD` | program used by `mos extract --llm` |
| `MOS_HOME` | where per-session state is kept (default `~/.memory-os`) |
| `CLAUDE_PROJECT_DIR` | project folder for the MCP server and hooks, set by Claude Code |

## What is stored, and where

In your project, in `.memory/`:

- `<id>.md`: one file per memory. Commit these; they are your shared project knowledge.
- `last-session.md` and `inbox/`: per-machine and unreviewed, so MEMORY OS adds them to `.memory/.gitignore` for you.

In `~/.memory-os` (or `MOS_HOME`), per machine: small per-session state for the hooks, the cost ledger, and the full text of trimmed tool output when Claude Code gives no scratch folder. None of it is synced anywhere, and trimmed outputs are not cleaned up automatically.

A memory file:

```markdown
---
id: database-is-postgresql
type: decision
status: active
trust: user
pin: false
created: 2026-11-13
verified: 2026-11-13
checks:
  - json_has: package.json :: dependencies.pg
  - file_lacks: package.json :: mongodb
guards:
  - edit: db/migrations/**
---
Database is PostgreSQL via pg
Replaced MongoDB because we need joins.
```

## Security notes

- **`.memory/` is like `CLAUDE.md`.** Anyone who can commit to it can put text in front of your agent, and a memory file can claim `trust: user`. Review changes to `.memory/` in pull requests as you would changes to `CLAUDE.md`.
- **Checks cannot run commands or leave the project.** They read files inside the project folder only, so verifying someone else's memories is safe.
- **Candidates are untrusted** until you accept them, and credentials-looking text is redacted before anything captured from a transcript is written.
- **The hooks run local Python** under your user account, like any Claude Code hook. The code is short and has no dependencies; read it before installing.

## Known limits

Read these before relying on the numbers.

- **Savings are estimates.** `mos meter` reports where tokens went and an estimate of what is reducible. It is not a measured saving, and subscription limits may weigh cached tokens differently from API prices.
- **Hooks are tested, not proven in live use.** The unit and end-to-end tests drive every hook with realistic input, and the plugin loads in a real Claude Code session. Read dedup, output trimming, guards and the rotation nudge have not been observed firing inside a live model session.
- **Output trimming depends on Claude Code.** It uses the `updatedToolOutput` hook field. The result shapes for PowerShell, Grep and MCP tools were taken from recorded transcripts, not confirmed in hook payloads; a mismatch means the hook does nothing.
- **Extraction is simple.** The offline pass uses patterns and will propose some noise. That is why its results go to the inbox and never straight into memory.
- **The benchmark is synthetic** and has no model in the loop.
- **Tested on Python 3.14 on Windows.** The code parses as Python 3.10 and uses only the standard library, but it has not been run on 3.10 to 3.13, macOS or Linux.
- **Pull-request action and Graphify reader** have not been run against a live pull request or a live Graphify graph.

## Not built yet

- A model-in-the-loop benchmark that measures task success, not only retrieval.
- Turning a memory into a lint rule or a test in the repo (guards are the only promotion target).
- Measured benefit per memory: the ledger counts cost and fetches, not causal effect.
- Import links for languages other than Python, JavaScript and TypeScript.
- Importers for exports other than ChatGPT's.
- Cleanup of old trimmed-output files.

## Tests

```bash
python -m unittest discover -s tests
```

The suite includes an end-to-end test that runs every command, hook event and MCP tool as a real subprocess, and a check that the committed skill archive matches the source.

## License

MIT
