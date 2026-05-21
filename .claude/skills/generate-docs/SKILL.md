---
name: generate-docs
description: Use when the user asks to rebuild, regenerate, or generate documentation from DOCS.yaml, or says "run DOCS.yaml" or "rebuild docs"
---

# Generate Docs

Reads `DOCS.yaml` from the project root and regenerates each target document in parallel using subagents.

## DOCS.yaml Format

```yaml
- path: ./docs/outreach.md
  prompt: |
    Description of what this document should contain...
- path: ./docs/other.md
  prompt: |
    Description of what this document should contain...
```

Each entry has:
- **path** — target file path relative to project root
- **prompt** — instructions describing what the document should contain

## Process

1. Read and parse `DOCS.yaml` from the project root
2. For each entry, dispatch a general-purpose Agent in parallel (all in a single message) with this prompt:

```
You are regenerating a documentation file at {path}.

PROMPT:
{prompt}

INSTRUCTIONS:
1. Read the existing file at {path} if it exists — use it for context about structure and coverage
2. Explore the codebase to understand the current implementation relevant to this document
3. Write the complete new file at {path}, overwriting any existing content
4. The document must be accurate to the current codebase state

The output is a markdown file written to {path}.
```

3. After all agents complete, report a summary: which files were written and any failures

## Key Rules

- Dispatch ALL agents in a single message for parallel execution
- Each agent is self-contained — it reads, researches, and writes independently
- Agents must read the existing file first (if present) before exploring code
- The `prompt` from DOCS.yaml is the authoritative guide for what each document should contain
- If DOCS.yaml is missing or empty, report that and stop
