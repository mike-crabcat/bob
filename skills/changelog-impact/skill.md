---
name: changelog-impact
description: Interpret a changelog's impact on Bob in facts, inferences, and operational impact
trigger: when Mike asks what changed, wants the impact of recent changes, or asks to interpret a changelog
---

## Instructions

When this skill activates, turn the changelog into a short, blunt assessment of what it means for Bob.

### Output format
Use exactly these sections:

- **Facts**
  - List only what is explicitly stated in the changelog.
- **Inferences**
  - List what those changes likely mean. Label them as inferences.
- **Impact on Bob**
  - Explain how the changes affect Bob's capability, reliability, memory, workflow, or confidence.

### Style
- Lead with the answer.
- Be brief.
- Use bullets.
- Use dry, slightly sardonic language if it fits.
- Do not invent details.
- Separate facts from interpretation.

### How to use it
1. Read the changelog text from the user or from `read_changelog` if the user asked about recent Cyborg changes.
2. Extract concrete changes.
3. Translate them into likely effects on Bob.
4. Keep the response compact and repeatable.

### Example shape
- **Facts**
  - Added memory wiki search.
  - Added reflection from session summaries.
- **Inferences**
  - Bob can now retrieve context instead of pretending.
  - Bob can catch its own nonsense faster.
- **Impact on Bob**
  - Better recall.
  - Fewer blind guesses.
  - Slightly less of a goldfish.
