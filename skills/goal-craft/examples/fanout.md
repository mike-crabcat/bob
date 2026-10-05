# fan-out goals — one objective across many people or items

The shape: the same work repeated per person/item, ending in one combined
deliverable. One outcome with a child per item — never a goal per item, and
never a long run of separate calls.

## Bad

> Nine separate add_goal calls — "Sam's figurine", "Alex's figurine", … —
> then straight into 3D generation for each.

Nine goals means nine rooms and no single "is the set done?". A long run of
near-identical calls skips or duplicates people. And going straight to 3D
spends nine paid generations on designs nobody has seen.

## Good

> add_goal(
>   text="9 D&D figurines for the AI Doom group — one per member, based on
>         what Bob knows about each; done when the owner has approved the
>         concepts and all 9 sliced STL/3MF files are delivered in Mike's DM",
>   label="build",
>   children=[
>     {"text": "Approve the 9 figurine concept images",
>      "completer": "agent:main:whatsapp:dm:<owner's number>",
>      "instruction": "Show the 9 concepts; wait for their yes/edits"},
>     {"text": "Sam: concept → 3D model → sliced file", "due_minutes": 2880},
>     {"text": "Alex: concept → 3D model → sliced file", "due_minutes": 2880},
>     …one per member…
>   ])
>
> state block: plan "concepts first (memory profile + face gallery per
> person) → owner approval → 3D generation per approved concept, a few at a
> time → slice → deliver the set".

One call, one room, one tracked set. The approval child gates the paid
step: no 3D generation starts until it closes. Each person's child closes
on its own with its file paths, so a failed generation is one retry, not a
restart. When the last child closes, the room reviews and delivers.

## Cues you're writing one

"for each of us", "for everyone in the group", "one per…", "all 9…",
anything where the same work repeats per person or item. If a step costs
money or can't be undone, the approval child goes first.
