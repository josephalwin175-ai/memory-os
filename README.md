# MEMORY OS

**Your AI's memory, with tests.**

Most memory tools for AI agents remember everything and never check any of it. MEMORY OS treats a memory like code: it carries a check that can be re-run against your repo, and when the check fails the memory is flagged as stale instead of being handed to the agent as fact.

It also ships a profiler that reads your Claude Code transcripts and shows where your tokens actually went, so you can see what is worth reducing before you change anything.

- No dependencies. Python 3.10+ standard library only.
- No API key, no server, no background process spending tokens.
- Memories are plain Markdown files in `.memory/`, so they diff and review like code.

Status: early (v0.2). See [What is not built yet](#what-is-not-built-yet).

## What it does

### 1. Shows where your tokens went

```bash
python bin/mos.py meter
```

It reports fresh input, cache writes, cache reads and output; context size per call; which tools produce the most carried output; re-reads of unchanged files; and an estimate of how much is reducible. Everything runs locally and only aggregates are printed.

The reducible figure is an estimate built on stated assumptions, not a measured saving. Weighted units use public API price ratios, and subscription limits may count tokens differently.

### 2. Memories that know when they are wrong

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

`mos verify` exits non-zero when a memory fails, so it can run in CI.

### 3. A small brief at session start

A `SessionStart` hook injects a compiled brief: pinned memories, recent ones within a token budget, anything stale, and a titles-only index of the rest. The agent fetches the rest on demand through MCP.

The brief is ordered from most stable to least stable, so its leading part stays byte-identical between sessions and can hit the provider's prompt cache. In a project with no `.memory/` folder the hook prints nothing.

### 4. Trust levels

Every memory records who it came from: `user` (saved from the CLI), `agent` (saved through MCP) or `external`. External memories are never placed in the brief as facts; they appear by title only, labelled as data and not instructions.

### 5. A nudge when a session gets expensive

Every turn re-sends the whole conversation, so long sessions cost far more per turn than short ones. A `UserPromptSubmit` hook reads the context size from the transcript and suggests a handoff:

- once when context crosses 100K tokens, and once at each doubling after that;
- once when a session is resumed after a break of more than an hour, when the prompt cache has likely expired.

It stays silent otherwise. Set `MOS_ROTATE_AT` to change the threshold, or to `0` to turn it off. The `handoff` skill then saves the state of the work so a new session can start from the brief.

### 6. Automatic capture of where you left off

At session end and before compaction, a hook records the last few requests and the project files that were changed as a `last-session` memory, with no model call. The next session's brief includes it.

- It only runs in projects that already have a `.memory/` folder.
- Text that looks like a credential is redacted before it is written.
- The file is added to `.memory/.gitignore`, because it is per-machine state and not reviewed knowledge.

Decisions and facts are not extracted automatically. The agent saves those, guided by the `memory` and `handoff` skills.

### 7. No re-reading of unchanged files

A `PreToolUse` hook on `Read` notices when the agent asks for a file it already read in this session and that has not changed on disk. It refuses once, with a short note that the content is already in context.

- An immediate retry goes through, so the agent is never locked out of a file.
- Files under 2 KB are ignored, because the refusal would cost about as much as the read.
- Different line ranges, edited files and subagents are treated as new reads.
- The record is cleared on compaction and `/clear`, when the earlier content is gone.

### 8. Trimmed command output

A `PostToolUse` hook on `Bash`, `PowerShell`, `Grep` and MCP tools replaces output longer than about 3K tokens with its first 40 lines, its last 80 lines, and any lines in between that look like errors or warnings. The full output is saved to a file whose path is given in the trimmed text, so the agent can grep or read it when needed. If the full text cannot be saved, the output is left untouched.

This relies on the `updatedToolOutput` hook field. On Claude Code versions that do not support it, the hook has no effect and output is unchanged.

### 9. A code map instead of exploration

```bash
mos map                 # outline of the whole codebase
mos map "token auth"    # where matching functions, classes or files are
```

The map lists each source file with its functions and classes and their line numbers, so the agent can read the 30 lines it needs instead of opening whole files to look around. It is also an MCP tool (`code_map`).

Python is parsed properly. JavaScript, TypeScript, Go, Rust, Java, Kotlin, C#, Swift, Ruby, PHP and C/C++ use line patterns, which find most top-level definitions but can miss unusual ones. Files ignored by git are left out. When the outline would exceed the token budget, the remaining files are listed by name with a symbol count.

### 10. Your data outlives any subscription

Nothing in MEMORY OS depends on an AI provider, an account or a paid plan.

- Memories are plain Markdown files in your project. They stay readable and editable with any text editor, forever.
- Every command (`save`, `search`, `verify`, `brief`, `map`, `meter`) runs locally with no network access and no model call.
- The MCP server works with any MCP client, so the same memory serves a different assistant if you switch.
- `mos export` writes everything to one file you can paste into any assistant, including free ones:

```bash
mos export --out project-knowledge.md
mos export --format json --out memories.json
```

### 11. An inbox for things found automatically

Nothing found by a machine goes straight into memory. It lands in an inbox, is never placed in a brief, and becomes a memory only when you accept it.

```bash
mos inbox              # list candidates
mos accept <id>        # or: mos accept --all
mos reject <id>        # remembered, so it is not proposed again
```

Candidates come from two places:

- **Session extraction.** At session end, a free offline pass looks through your own messages for sentences that read like a decision ("let's use X instead of Y"), a standing preference ("never commit to main") or a project fact ("the API listens on port 4001"). It is deliberately simple and will propose some noise.
- **A model, if you want one.** Set `MOS_EXTRACT_CMD` to any program that reads a prompt on stdin, then run `mos extract --llm`. For example `ollama run llama3.2` is local and free, and `claude -p --model haiku` uses your plan. It is never run automatically.

### 12. Importers

```bash
mos import CLAUDE.md
mos import AGENTS.md
mos import .cursorrules
mos import conversations.json    # a ChatGPT data export
```

Bullets and short paragraphs from instruction files, and decision-like sentences from your side of ChatGPT conversations, are queued in the inbox. A MEMORY OS JSON export can be imported the same way.

### 13. Guards: memories that cost nothing until they apply

Some memories only matter at one moment: "do not edit generated files by hand", "never force push". Carrying them in every session's prompt is waste. Promote one to a guard and it leaves the brief; a hook shows it only when a matching edit or command is attempted.

```bash
mos promote gen-rule --edit "src/gen/**"
mos promote no-force --bash "git\s+push\s+.*(--force|-f)\b"
```

A guard interrupts once per session with the memory's text. A retry goes through, so it informs the decision and never blocks the work.

`mos ledger` shows what each memory has cost to carry and whether it was ever fetched, and suggests which ones to promote, unpin or archive. It counts injections and fetches; it cannot prove a memory helped, so it only suggests.

### 14. A pull-request check

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

It runs every memory's checks against the pull request's code and comments with a table of the ones that no longer hold. Set `fail: "true"` to fail the job as well. It reports memories that are false on the branch, including any that were already false before the change. This action has been tested locally through `mos verify --markdown`, not yet on a live pull request.

### 15. Connections around a file

```bash
mos map --around src/auth.ts
```

This shows what the file imports, what imports it, and which saved memories are about it. Import links cover Python, JavaScript and TypeScript.

### Works with Graphify

[Graphify](https://github.com/Graphify-Labs/graphify) is a separate tool that builds a knowledge graph of a codebase, including documents and concepts, and writes it to `graphify-out/`. The two answer different questions: Graphify tells you how the codebase hangs together, MEMORY OS tells you what was decided and whether it is still true. MEMORY OS never installs, runs or requires Graphify. When a graph is present it uses it in four places:

| Where | What Graphify adds |
|---|---|
| `mos map --around <file>` | the file's relationships from the graph, next to its imports and memories |
| `mos map "<words>"` | matching documents and concepts, which a code outline cannot see |
| the session brief | one line pointing the agent to `graphify-out/GRAPH_REPORT.md` |
| `mos graph` | whether the graph is out of date: commits behind, and source files changed since it was written |

Links that Graphify marks as inferred or ambiguous are labelled that way, so a guess is not read as a fact.

To use both: install Graphify (`pip install graphifyy`), run `graphify extract` in your project, and carry on using MEMORY OS as before. Both tools add Claude Code hooks; they act independently and either can be turned off.

The reader follows the field names in Graphify's exporter (checked against its source at version 0.9) and is tested on a fixture in that format. It has not been run against a graph produced by a live Graphify install.

### 16. A benchmark

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

### Switches

| Variable | Effect |
|---|---|
| `MOS_ROTATE_AT` | context size that triggers the handoff nudge (default 100000; `0` turns it off) |
| `MOS_DEDUP=0` | turn off read dedup |
| `MOS_TRIM=0` | turn off output trimming |
| `MOS_GUARDS=0` | turn off memory guards |
| `MOS_EXTRACT_CMD` | program used by `mos extract --llm` |
| `MOS_TRIM_CHARS` | output length that triggers trimming (default 12000) |
| `MOS_HOME` | where per-session state is kept (default `~/.memory-os`) |

## Install

### Claude Code plugin

In Claude Code:

```
/plugin marketplace add josephalwin175-ai/memory-os
/plugin install memory-os@memory-os
```

This adds the MCP server, hooks (session start, prompt, pre-compact, session end, before reads, edits and commands, after tool output) and three skills: `memory`, `handoff` and `meter`.

To try it from a local checkout without publishing:

```bash
claude --plugin-dir /path/to/memory-os
```

The plugin runs `python`. On systems where the command is `python3`, edit `.claude-plugin/plugin.json` and `hooks/hooks.json` accordingly.

### As a standalone skill

For agents that support skills but not plugins, or if you only want the commands:

1. Download [`dist/memory-os-skill.zip`](dist/memory-os-skill.zip).
2. Unzip it into your skills folder, for example `~/.claude/skills/`.

The skill is one instruction file plus `mos.pyz`, the whole tool as a single runnable file. It has every command, but no hooks and no MCP server, so nothing happens automatically. Rebuild it with `python scripts/build_skill.py`.

### Any MCP client (Cursor, Codex and others)

Add a stdio server that runs:

```
python /path/to/memory-os/bin/mos.py mcp
```

Set `CLAUDE_PROJECT_DIR` to the project folder, or start the server from inside it.

### Command line

```bash
pip install .
mos --help
```

Or run it straight from a checkout with `python bin/mos.py`.

## Commands

| Command | What it does |
|---|---|
| `mos meter` | profile token usage from Claude Code transcripts |
| `mos init` | create `.memory/` in the current project |
| `mos save "text" [--type] [--check] [--pin] [--supersedes id]` | save a memory |
| `mos search "query"` | search memories |
| `mos list`, `mos get <id>` | browse memories |
| `mos verify [--fix]` | re-run all checks; `--fix` marks failures stale |
| `mos brief [task] [--receipt]` | print the compiled brief |
| `mos status <id> <status>` | set active, stale, superseded or archived |
| `mos capture [--transcript file]` | record what the last session was working on |
| `mos map [query] [--path text] [--around file]` | outline the codebase, or show a file's connections |
| `mos graph` | status of a Graphify graph, if the project has one |
| `mos inbox`, `mos accept`, `mos reject` | review candidate memories |
| `mos extract [--llm]` | find candidates in a session transcript |
| `mos import <file>` | queue knowledge from other tools |
| `mos promote <id> --edit glob \| --bash regex` | turn a memory into a guard |
| `mos ledger` | cost and use of each memory |
| `mos bench` | run the retrieval benchmark |
| `mos export [--format markdown\|json] [--out file]` | write all memories to one portable file |
| `mos mcp` | run the MCP server on stdio |

## MCP tools

`memory_search`, `memory_get`, `memory_save`, `context_compile`, `code_map`, `memory_verify`. Six tools with short descriptions, because every tool schema is paid for on every turn.

## A memory file

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
---
Database is PostgreSQL via pg
Replaced MongoDB because we need joins.
```

## What is not built yet

- A model-in-the-loop benchmark that measures task success, not only retrieval.
- Turning a memory into a lint rule or a test in the repo (guards are the only promotion target).
- Measured benefit per memory: the ledger counts cost and fetches, not causal effect.
- Import links for languages other than Python, JavaScript and TypeScript.
- Importers for exports other than ChatGPT's.

## Tests

```bash
python -m unittest discover -s tests
```

## License

MIT
