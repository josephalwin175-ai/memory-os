---
name: meter
description: Show where the user's Claude Code tokens went and what could be reduced. Use when the user asks about token usage, hitting limits, why sessions are expensive, or how much MEMORY OS could save.
---

# Usage profile

Run the profiler. It reads local Claude Code transcripts and prints aggregates only.

```bash
python "${CLAUDE_PLUGIN_ROOT}/bin/mos.py" meter
```

If that path is not available, use `mos meter` (when installed with pip) or `python -m memory_os meter`. Add `--project <text>` to limit it to one project.

Then explain the result in plain language:

- Which row dominates. Cache reads are the conversation being re-sent each turn; a high share means sessions run long.
- Median and peak context per call. Above roughly 80K, suggest the handoff skill.
- Which tools produce the most carried output.
- The addressable figures are upper bounds that overlap, and the combined figure is an estimate, not a measured saving. Say so.
- Weighted units use API price ratios. Subscription limits may count tokens differently.
