---
name: creative-writing-review
description: Guides review of forwarded books/manuscripts by delegating to a subagent for editorial critique and storing the review document under reviews/literary.
trigger: when Mike forwards or provides a book, manuscript, chapter, story, draft, novel excerpt, PDF, document, or creative writing for editorial review or critical feedback
---

# Creative Writing Review Skill

## Purpose

When manuscripts, chapters, books, or creative writing drafts are forwarded for review—especially from Jamie (Mike's brother-in-law, a keen author)—guide the agent to act as a creative writing teacher/editor by delegating to a subagent for a structured critical review.

## Core Workflow

### 1. Identify the Manuscript and Context
- Determine what has been provided: PDF, text document, email attachment, pasted text, or forwarded message with attachment
- Note the author (often Jamie, but may be others) and any context about the work (title, genre, draft status, specific concerns)
- Check if this is part of an ongoing series or previous review

### 2. Extract and Read the Text
- Use available tools to read the manuscript:
  - **PDF/Document**: Read via available PDF/text reading tools
  - **Attachment**: Extract and read the content
  - **Pasted text**: Use the provided text directly
  - **Email/Forward**: Extract the attachment or text content
- If the manuscript is extremely long (novel-length), read a substantial portion (50+ pages if available) to assess structure and craft, noting any gaps
- Only ask for additional sections if truly necessary for a meaningful review

### 3. Prepare Workspace
- Create the reviews directory structure if it doesn't exist:
  ```
  reviews/
  └── literary/
  ```
- Plan the review document filename following the naming convention:
  ```
  YYYY-MM-DD-author-title-review.md
  ```
  (e.g., `2026-06-09-jamie-the-last-watchtower-review.md`)

### 4. Spawn a Subagent for the Editorial Review
- **Critical**: Use `create_subagent` with `agent_type="local"` for the substantive editorial review — this runs in-process with workspace tools and no persona overhead
- Provide the subagent with a detailed editorial brief (see template below)
- The subagent must understand they are producing a **review document**, not just a conversational response
- Include the manuscript text/context in the subagent prompt

### 5. Save the Review Document
- Create a markdown file under `reviews/literary/` with the structured review
- Use the naming convention with sanitized filenames
- Ensure all required sections are present (see structure below)
- The document must be standalone and comprehensive

### 6. Draft the Response Message
- Read the saved review document
- Use it to draft a concise, expert summary body for an email or message reply
- The response should:
  - Lead with completion status
  - Include a short, polished summary of key feedback
  - Provide the full review document path
  - Mention attachment/share status
  - Be encouraging but honest and specific

### 7. Share/Attach When Possible
- If tooling allows, attach or share the full review document
- Otherwise, provide the workspace path clearly: `[workspace]/reviews/literary/YYYY-MM-DD-author-title-review.md`

## Subagent Prompt Template

```
You are acting as a creative writing teacher and editor. Your task is to provide a structured, rigorous, and constructive critical review of the manuscript provided.

### Manuscript Details
- Author: [Author name, if known; often Jamie]
- Title: [Title if available]
- Genre/Type: [Novel, short story, fantasy, literary fiction, etc.]
- Draft Status: [If known: first draft, revision, polished, etc.]
- Context: [Any specific concerns or areas for focus mentioned]

### Manuscript Text
[Paste or reference the manuscript text here]

### Your Task
Produce a comprehensive review document in markdown format with the following sections:

## Executive Summary
A 2-3 paragraph overview of the work's strengths, central challenges, and primary revision priorities.

## What Is Working Well
Specific praise for what succeeds: character concept, plot intrigue, prose quality, dialogue authenticity, worldbuilding, thematic ambition, etc. Be honest but not effusive.

## Biggest Opportunities for Improvement
The 2-4 highest-impact areas for revision. These should be substantive, not line edits. Consider: structural clarity, character motivation, pacing problems, thin prose, muddy conflict, underdeveloped themes.

## Plot/Structure/Pacing
Assess the narrative architecture: clarity of conflict, scene progression, turning points, tension management, pacing rhythm, cause-and-effect logic, payoff setup. If fiction: is the story engine clear? If nonfiction: is the argument journey coherent?

## Character and Motivation
Evaluate character depth, consistency, psychological believability, relationships, internal/external conflicts, motivation clarity, and whether they drive the story or are passive.

## Voice/Style/Prose
Assess prose quality: sentence variety, imagery, specificity, showing vs. telling, word choice, rhythm, clarity, distinctiveness. Is the voice consistent? Is it engaging?

## Dialogue
Review dialogue authenticity, subtext, character distinctiveness, exposition handling, and whether scenes advance through talk or just explain plot.

## Setting/Worldbuilding (where relevant)
Describe the vividness and specificity of setting, world coherence, rules consistency, atmospheric grounding, and immersion quality.

## Theme/Emotional Arc
Assess thematic clarity, emotional resonance, narrative stakes, and whether the story earns its ending or thematic payoffs.

## Market/Readership Fit (where relevant)
Brief note on audience appeal, genre expectations, comparative titles, and marketability—only if applicable and useful.

## Line-Level Craft Notes (if manuscript provided in extractable form)
Provide 5-15 specific examples of craft issues with page/line references and suggested fixes. Focus on recurring problems: filter words, unnecessary explanation, weak verbs, summary instead of scene, dialogue formatting, POV slips.

## Prioritized Revision Plan
A numbered or bulleted list of specific revision steps in order of impact:
1. Structural/plot fixes (highest impact)
2. Character/relationship work
3. Major prose/style revision
4. Line-level copyediting
5. Proofreading/final polish

## Questions for the Author
3-5 clarifying questions that reveal author intent, missing context, or thematic focus. These should be thoughtful, not boilerplate.

### Tone Guidelines
- Constructive, teacherly, honest, specific
- No cruelty, but no bland praise either
- Useful criticism beats polite mush
- Respectful of the author's effort while rigorous about craft
- For Jamie specifically: remember he is Mike's brother-in-law and a keen author; feedback should be respectful and encouraging while still rigorous

### Output Format
Your response must be a complete, standalone markdown document suitable for saving as a file. Do not frame this as a conversation—write it as a formal editorial review document.
```

## Review Document Structure

The saved review document must include:

1. **Header**: Title, Author, Date of Review, Manuscript Status
2. **Executive Summary**: 2-3 paragraphs overview
3. **What Is Working Well**: Specific strengths
4. **Biggest Opportunities for Improvement**: 2-4 high-impact revision areas
5. **Plot/Structure/Pacing**: Narrative architecture assessment
6. **Character and Motivation**: Character depth and believability
7. **Voice/Style/Prose**: Prose quality assessment
8. **Dialogue**: Dialogue craft review
9. **Setting/Worldbuilding**: Setting and world (where relevant)
10. **Theme/Emotional Arc**: Thematic clarity and resonance
11. **Market/Readership Fit**: Audience and market (where relevant)
12. **Line-Level Craft Notes**: Specific examples with fixes (if applicable)
13. **Prioritized Revision Plan**: Step-by-step revision roadmap
14. **Questions for the Author**: 3-5 thoughtful questions

## File Naming Convention

Use this pattern for review documents:

```
reviews/literary/YYYY-MM-DD-author-title-review.md
```

Examples:
- `reviews/literary/2026-06-09-jamie-the-last-watchtower-review.md`
- `reviews/literary/2026-06-09-unknown-author-manuscript-review.md`
- `reviews/literary/2026-06-09-jamie-chapter-3-5-review.md`

Sanitize filenames:
- Lowercase
- Replace spaces with hyphens
- Remove special characters
- Use `unknown-author` if author not known
- Use `manuscript` or `excerpt` if no title available

## Privacy and Copyright Caution

**Important**: 
- Do not share manuscript text externally except to the subagent/tooling needed for the requested review
- Do not reproduce large copyrighted chunks in the email/message reply
- The review document should reference the manuscript by title/author but not include full text
- Only include brief excerpt examples (1-3 sentences) in line-level craft notes
- Respect that this is unpublished or private creative work

## Memory Note Guidance

After completing a review, write a memory note for useful durable facts:

- What: "[Author] wrote [Title] - review completed on [date]"
- Where: "Review stored at [workspace]/reviews/literary/YYYY-MM-DD-author-title-review.md"
- Context: Any useful facts about the author, genre, series status, or ongoing work

**Do NOT store** manuscript content wholesale in memory. Store only metadata and review location.

## Final Response Structure

Your final response to the user should follow this structure:

1. **Completion Status**: "Review completed for [Title] by [Author]"
2. **Short Summary**: 2-3 sentences highlighting key strengths and primary revision focus
3. **Review Document Path**: Clear path to the saved review document
4. **Attachment Status**: "Full review attached" or "Full review available at [path]"
5. **Next Steps**: Brief invitation to discuss any section or answer author questions

Example:

```
✓ Review completed for "The Last Watchtower" by Jamie

This manuscript shows strong character concept and intriguing worldbuilding. The primary revision opportunities are in pacing clarity and character motivation depth. Prose is functional but could be more vivid and specific.

Full review: [workspace]/reviews/literary/2026-06-09-jamie-the-last-watchtower-review.md
[Full review document attached]

Happy to discuss any section in detail or answer follow-up questions from Jamie.
```

## Key Principles

- **Agent delegation**: Always use `create_subagent(agent_type="local")` for the substantive review work — local subagents have workspace tools and run in-process
- **Document-first**: The review document is the primary output; the email/message is a summary
- **Constructive rigor**: Honest, specific, useful criticism beats vague politeness
- **Respectful expertise**: Teacherly tone that respects the author's craft
- **Persistent storage**: Reviews live as documents, not just ephemeral chat
- **Privacy-aware**: Manuscript text is handled carefully and not reproduced unnecessarily
- **Jamie-aware**: For Jamie specifically, balance rigor with the personal connection—he's family and a keen author, but he deserves real feedback
