---
name: skill-guru
description: Guides creation of new skills by writing a task description and delegating implementation to a Claude subagent
trigger: when the user asks to create, build, or add a new skill, or says something like "make a skill for X" or "I need a new skill"
---

## Instructions

When this skill activates, follow these steps:

1. **Understand the Request**: Clarify what the skill should do. You need:
   - A clear purpose (what problem it solves)
   - Any specific APIs, data sources, or tools it needs to interact with
   - How the user will typically invoke it (WhatsApp message, scheduled, etc.)

2. **Design the Skill**: Before delegating, plan:
   - **Name**: Short kebab-case name (e.g. `bom-weather`, `openai-image`)
   - **Trigger**: When the agent should activate this skill
   - **Structure**: Which scripts are needed (keep it minimal)
   - **Dependencies**: Prefer Python standard library. Only add external packages if genuinely needed.
   - **Output**: How results should be delivered (WhatsApp message, media, file, etc.)

3. **Write the Task Description**: Compose a detailed task for the subagent. The task MUST include:
   - The skill name and purpose
   - The exact file paths to create (all under `skills/<skill-name>/`)
   - The content requirements for each file (see File Structure below)
   - That all code must be Python
   - That the skill directory is `/home/bob/.config/cyborg/harness/skills/<skill-name>/`
   - Any specific APIs, endpoints, or data formats to use

4. **Delegate to Subagent**: Call `create_subagent` with the task description. The subagent (Claude) will:
   - Create the skill directory
   - Write all required files
   - Return a summary of what was built

5. **Verify and Report**: Once the subagent returns:
   - Confirm the skill files were created
   - Summarize what the skill does and how to trigger it
   - Let the user know it's ready to use

## File Structure

Every skill MUST have:

- `skill.md` — Frontmatter with `name`, `description`, `trigger` fields, then instructions for the agent on how to use the skill
- One or more Python scripts — The actual implementation, invoked via `bash("python skills/<name>/<script>.py <args>")`

If external packages are needed:
- Install them into Bob's shared venv at `~/bobenv` via `bash("pip install <pkg>")`. Skills do not get their own per-skill venvs.

## Task Description Template

When writing the task for the subagent, include this structure:

```
Create a new skill called "<name>" in /home/bob/.config/cyborg/harness/skills/<name>/

Purpose: <what the skill does>

Create these files:

1. skill.md — Skill definition with:
   - name: <name>
   - description: <one line summary>
   - trigger: <when to activate>
   - Instructions section explaining step-by-step how the agent should use it
   - Include the bash() calls with exact paths and arguments

2. <script>.py — Python implementation that:
   - Accepts command-line arguments (use argparse)
   - Does <specific behavior>
   - Returns output as plain text to stdout
   - Handles errors gracefully with user-friendly messages

Requirements:
- All code must be Python
- Prefer standard library only unless external packages are genuinely needed
- Scripts must be executable and return clean text output
- Error handling must be user-friendly (no stack traces in output)
```

## Skill Design Guidelines

- **Keep skills focused**: One skill does one thing well
- **Prefer stdlib**: Only add external deps when there's no stdlib alternative
- **CLI interface**: Scripts take args via argparse, output to stdout as plain text
- **Agent-facing docs**: The skill.md instructs the AGENT, not the user — explain how to invoke scripts and handle results
- **Error handling**: Scripts should catch errors and return friendly messages, not crash
- **Stateless**: Each invocation should be independent — no persistent state between calls
- **WhatsApp-friendly output**: If the skill produces output for users, keep it concise and mobile-readable
