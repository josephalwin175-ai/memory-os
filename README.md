# MEMORY OS

**Your AI's memory, with tests.**

Most memory tools for AI agents remember everything and never check any of it. MEMORY OS treats a memory like code: it carries a check that can be re-run against your repo, and when the check fails the memory is flagged as stale instead of being handed to the agent as fact.

It also ships a profiler that reads your Claude Code transcripts and shows where your tokens actually went, so you can see what is worth reducing before you change anything.

- No dependencies. Python 3.10+ standard library only.
- No API key, no server, no background process spending tokens.
- Memories are plain Markdown files in `.memory/`, so they diff and review like code.

Status: early (v0.1). See [What is not built yet](#what-is-not-built-yet).

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

A `PostToolUse` hook on `Bash` replaces output longer than about 3K tokens with its first 40 lines, its last 80 lines, and any lines in between that look like errors or warnings. The full output is saved to a file whose path is given in the trimmed text, so the agent can grep or read it when needed. If the full text cannot be saved, the output is left untouched.

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

### Switches

| Variable | Effect |
|---|---|
| `MOS_ROTATE_AT` | context size that triggers the handoff nudge (default 100000; `0` turns it off) |
| `MOS_DEDUP=0` | turn off read dedup |
| `MOS_TRIM=0` | turn off output trimming |
| `MOS_TRIM_CHARS` | output length that triggers trimming (default 12000) |
| `MOS_HOME` | where per-session state is kept (default `~/.memory-os`) |

## Install

### Claude Code plugin

Once this repository is on GitHub:

```
/plugin marketplace add <owner>/memory-os
/plugin install memory-os@memory-os
```

This adds the MCP server, six hooks (session start, prompt, pre-compact, session end, before Read, after Bash) and three skills: `memory`, `handoff` and `meter`.

To try it from a local checkout without publishing:

```bash
claude --plugin-dir /path/to/memory-os
```

The plugin runs `python`. On systems where the command is `python3`, edit `.claude-plugin/plugin.json` and `hooks/hooks.json` accordingly.

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
| `mos map [query] [--path text]` | outline the codebase with line numbers |
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

- Automatic extraction of decisions and facts from transcripts (only "where you left off" is captured automatically).
- Trimming for tools other than Bash.
- Promotion of memories into hooks or lint rules, and the per-memory cost ledger.
- A GitHub Action that comments when a pull request invalidates a memory.
- Importers for other assistants, and a benchmark.

## Tests

```bash
python -m unittest discover -s tests
```

## License

MIT
