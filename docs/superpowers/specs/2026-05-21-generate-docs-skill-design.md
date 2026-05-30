# generate-docs Skill Design

**Date:** 2026-05-21

## Purpose

A Claude Code skill that reads `DOCS.yaml` from the project root and regenerates all target documents in parallel using subagents. Each entry in the YAML defines a target path and a prompt describing what the document should contain.

## Decisions

- **Location:** `.claude/skills/generate-docs/SKILL.md` (project-level, shared via git)
- **Subagent type:** General-purpose Agent — each agent reads the codebase and writes the file independently
- **Existing files:** Overwritten with context — subagent reads existing content first, then explores codebase, then writes fresh
- **Parallelism:** All agents dispatched in a single message for concurrent execution

## DOCS.yaml Format

```yaml
- path: ./docs/outreach.md
  prompt: |
    Description of what this document should contain...
```

Fields:
- `path` — target file path relative to project root
- `prompt` — instructions for what the document should contain

## Flow

1. Read and parse `DOCS.yaml` from project root
2. For each entry, dispatch one general-purpose Agent in parallel
3. Each agent: reads existing file → explores codebase → writes new file
4. Report summary of results

## Skill Trigger

User says anything like "rebuild docs", "regenerate documentation", "run DOCS.yaml", or "generate docs".
