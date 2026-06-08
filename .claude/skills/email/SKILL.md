---
name: email
description: Use when the user says /email or asks to check email — polls AgentMail inboxes for new messages and reports results
---

# Email Poll

Polls all registered AgentMail inboxes for new messages and reports what was found.

## Process

1. Run:
   ```bash
   curl -s -X POST http://localhost:8420/api/v1/email/poll
   ```

2. Report the result to the user — how many new messages were found. If messages were found, they will have been dispatched through the patience system for processing.
