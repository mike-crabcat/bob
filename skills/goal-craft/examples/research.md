# research goals — investigate, branch, prune

The shape: a question that needs multiple approaches tried in parallel or
sequence, evidence gathered, and an answer with receipts.

## Bad

> objective: "Understand the WA eCourts system"

No artefact, no proof, no end state. This goal can never complete because
nothing distinguishes "understood" from "not yet" — it will burn budget,
stall, and get pruned by an operator.

## Good

> objective: "Reverse-engineer the WA eCourts case-search endpoint: the
> exact HTTP request (URL, params, headers) that returns docket JSON for a
> case number, proven by a saved curl transcript replayed twice from a
> clean session."
>
> state block: plan "capture traffic from the public search UI, hypothesise
> the XHR shape, verify with clean replays". strategies: s1 "replay captured
> XHR directly" (candidate), s2 "headless browser drives the form, log the
> request" (candidate).

Done is checkable: the transcript exists and replays. The strategies tree
makes "we considered driving the form" a recorded branch, not a hidden
thought. Each branch is a task; each result lands via strategy_result.

## Cues you're writing one

"find out", "is it possible", "why does", "what would it take". If the
output is knowledge, it's research — and the artefact is the knowledge
*written down* (transcript, endpoint spec, comparison table), not a
feeling of understanding.

## Failure pattern to avoid (real: 2026-09-16 WFH stall)

A research goal whose next_actions say "keep looking into it" with no
branches stalls silently: every round is a no-op, the stall frame fires,
nothing changes. If you can't name the next experiment, the round's job is
to NAME it (strategy_open) — that alone is progress.
