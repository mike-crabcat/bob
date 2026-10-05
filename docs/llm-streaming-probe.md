# LLM streaming & reasoning capability probe — 2026-10-03

Phase 0 of the trace uplift (plan: reasoning capture + live turn watching).
Run with `uv run python scripts/llm_streaming_probe.py [model ...]` against
the live rails: `gpt-5.6-sol` + `gpt-6-astra` (OpenAI-direct) and
`z-ai/glm-5.3-flash` (OpenRouter, fp8 filter active).

## Results matrix

| Question | OpenAI-direct (sol, astra) | OpenRouter (glm-5.3-flash) |
|---|---|---|
| (a) `reasoning={"effort", "summary":"auto"}` non-streaming | accepted; reasoning item present; **summary text appears when the model thinks hard** (374–408 chars at effort high); at effort low `summary: []` ("auto" = model's choice) | accepted (no 400); reasoning item carries **raw reasoning text in `content` (reasoning_text parts)** — OpenRouter maps GLM's native reasoning into the Responses shape; at effort medium on trivial tool rounds no reasoning item at all |
| (b) streaming event inventory (with tools) | `response.output_item.added/done`, `response.function_call_arguments.delta/done`, `response.completed`; on thinking text rounds additionally `response.reasoning_summary_part.added/done`, `response.reasoning_summary_text.delta/done`, `response.content_part.*`, `response.output_text.delta/done` | same lifecycle, but reasoning arrives as **`response.reasoning_text.delta/done`** (raw), not summary parts; `response.output_text.delta/done` present |
| (c) `response.completed` completeness | ✅ carries full `output` items + `usage` — streamed rounds are consumable exactly like buffered ones | ✅ same |
| (c) small-cap truncation (`max_output_tokens=120`) | completed (no `response.incomplete`); function_call intact — reasoning appears exempt from the cap | same |
| (d) `tool_choice="required"` + `stream=True` | ✅ works (function_call streamed, usage present) | ✅ works |
| (e) reasoning in Responses shape | summary-based | raw-text-based (`content` parts + `reasoning_text` events) |

## Quirks found

1. **OpenRouter 400s on `tools: null`** (`invalid_prompt`, expected array).
   Direct OpenAI accepts null. We already omit the key when no tools
   (`_merge_tools` + `if tools:`) — keep it that way.
2. **`encrypted_content` rides OpenAI reasoning items unrequested** (~0.5–2 KB
   per item). Today the generic fallback serializer already persists it into
   `llm_call_log.messages_json`; the wire copy must stay intact (reasoning
   continuity), but the trace table stores only extracted text.
3. **Two reasoning dialects, one request param**: `summary:"auto"` is accepted
   by both rails and ignored-or-honoured per rail. No capability map is
   needed on the request side — send it whenever `reasoning` is sent, then
   extract BOTH shapes (`item.summary[].text` and `item.content[]
   type=="reasoning_text"`; streaming: `reasoning_summary_text.delta` and
   `reasoning_text.delta`). This simplifies the original plan: no
   `reasoning_summary:` models.yaml map required.
4. GLM at effort medium on trivial tool rounds surfaced **no reasoning item**
   (straight to function_call). Production prompts are heavier; whatever
   arrives is captured — the UI shows thinking blocks when present, nothing
   when the model didn't think on the wire.
5. Neither rail emitted `response.incomplete` under a tiny cap — handle the
   event defensively anyway.
6. **OpenAI requires `summary` PRESENT on replayed reasoning items** when the
   generating round used `summary: "auto"` — replaying `{type, id,
   encrypted_content}` without the summary field 400s with `Missing required
   parameter: 'input[N].summary'` (found live 2026-10-03, end-to-end smoke).
   The explicit reasoning serializer keeps `summary` (plain dicts, `[]` when
   empty) and `encrypted_content`, but never the raw `content` text (GLM's
   ~1k-token thinking would bloat every later round).
7. End-to-end smoke (our turn loop + `_round`, 2026-10-03): sol
   streamed 76 reasoning deltas → 1 summary part (421 chars captured) →
   function_call args → final text; GLM streamed tool args + 13 text deltas
   with usage intact on both rails.

## Implementation consequences

- `_round()` streaming consumes both dialects; `on_round_complete` extraction
  reads both shapes; trace rows record `meta.source: summary|raw`.
- Summary request is gated only by the small-cap floor (`max_output_tokens`
  < ~3000 → omit `summary` key, keep effort) so capped background passes keep
  their full output budget.
- One-shot retry-without-summary on a 400 mentioning `summary` stays as armor
  for provider drift.
