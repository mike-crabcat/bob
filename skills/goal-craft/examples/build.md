# build goals — artefacts with proofs

The shape: a thing that must exist AND demonstrably work. Verification is
part of the objective, not an afterthought.

## Bad

> objective: "Fix the headless WebGL renderer"

Fix = vague; nothing says what working means. This goal invites the model
to declare victory from vibes.

## Good

> objective: "The /perth WebGL scene renders correctly in Bob's headless
> Chrome: a CDP screenshot script captures the harbour view, and the image
> diff against the reference render is under 2% — script + screenshot
> committed to the repo."
>
> state block: plan "isolate whether the failure is context loss or
> shader init; fix; verify by capture". strategies: s1 "preserve context on
> swapchain rebuild" (candidate), s2 "software GL fallback for CI"
> (candidate).

Done is checkable: the script runs, the diff is a number, the commit
exists. Visual artefacts need visual proof — a screenshot, not a claim
(house rule: nothing visual is done from code alone).

## Autonomy limits (D14 — hard rule)

Everything up to green tests on a branch is in-goal. Merge to master,
`systemctl restart`, and production deploys are NOT: request them in the
ORIGIN conversation and finish your round. A build goal whose proof
includes "deployed" must phrase completion as "ready to deploy — requested".

## Cues you're writing one

"make X work", "build", "fix", "add a feature to". If the output is a
changed system, it's build. Research sub-questions inside a build goal
("why does it fail?") are branches (strategy_open + task), not separate
goals — unless they'd outlive the build.
