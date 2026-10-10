# Reaction clips — how to add one

Reaction clips are the STAND_DOWN tier of the attention probe: when the probe
decides Bob stays silent on unaddressed group chatter, it may *additionally*
recommend one short avatar clip — "a decoration of silence", for the rare
perfect beat (a win, a glorious failure, real drama). A clip never rides
ACT/WAIT and never substitutes for a reply. Live since 2026-09-03.

Adding a new clip touches **two things, shipped in one commit**: the asset
and its registry entry. (Since 2026-10-11 the registry is a single file read
by both the probe and Bob's `avatar` skill — the old hard-coded
`REACTION_CLIPS` dict and the identity.md manifest are gone.)

## 1. The asset — `self/bob/avatar/reactions/<name>.mp4`

- **MP4, not GIF.** The send path constructs `{clip}.mp4` literally
  (`server/services/attention/coordinator.py`, `_send_probe_reaction`), so a
  `.gif` file will never be found even with the right name. Convert first:
  ```bash
  ffmpeg -i bob-new-reaction.gif -movflags faststart -pix_fmt yuv420p -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" bob-new-reaction.mp4
  ```
  (even dimensions keep some players happy; `faststart` makes WhatsApp render
  a poster frame).
- Name it `bob-<cue>.mp4` — the slug doubles as the registry key.
- Keep it a few seconds and small: existing clips run ~230 KB to 4.7 MB.
  Multi-MB is tolerated, not encouraged.
- This directory is the repo-side **self bundle**; at boot
  `services/self_bundle.py` heals it into `workspace/self/bob/` (changed
  files restored, extras pruned). A clip dropped only into the workspace
  copy is an *extra* and will be pruned at the next boot — it must land in
  the repo to be permanent.

## 2. The registry — `self/bob/avatar/reactions/clips.yaml`

```yaml
bob-new-reaction:
  cue: one-line moment description the probe matches against
  from: who commissioned it, date        # provenance; the probe never sees it
```

The key must equal the file's slug (minus `.mp4`), and the cue is load
bearing: the probe model sees exactly `- bob-new-reaction — <cue>` and fires
the clip only when the moment genuinely hits the cue. Write the cue as a
moment description, not a label — "something failed, epically", not "fail".
`server/services/attention/tier2.py` loads it into `REACTION_CLIPS` at
import; the `avatar` skill tells Bob to read the same file — so the probe and
Bob's own clip posts can't disagree.

## Enforcement (why the three places matter)

- The probe prompt offers only registry entries (`_reactions_block` in
  tier2.py) — an unregistered clip can never be recommended.
- `test_attention_reactions.py::test_registry_matches_clip_files` requires
  registry entries and `.mp4` files to match exactly, each with a cue.
- `_send_probe_reaction` refuses any name not in `REACTION_CLIPS`, then
  requires the `.mp4` to exist on disk, then applies the per-chat cooldown
  (default 180 min, `BOB_PROBE_REACTION_COOLDOWN_MIN`) — so a stale prompt
  or a hallucinated name sends nothing.
- `BOB_PROBE_REACTIONS=off` kills the whole tier (prompt stops offering,
  sends refused).

## Gates before shipping

1. **Re-run the probe matrix** — adding a registry entry changes the probe
   prompt (the clip list is in it), and the standing rule is
   *probe-matrix before any prompt change*:
   ```bash
   uv run bob replay probe-matrix
   ```
   The golden matrix must stay 10/10.
2. `uv run pytest tests -q` (deploy gate).
3. `systemctl --user restart bob.service` — the restart also runs the boot
   heal that seeds the new mp4 into the workspace.
4. Optional live check: the clip's first real firing logs
   `attention: probe reaction bob-new-reaction sent to <session>`; a missing
   file logs `attention: reaction clip missing: <path>` at info.

## Replacing or retiring a clip

Both places, reversed: remove the `.mp4` and its `clips.yaml` entry in the
same commit (the registry test fails if only one goes).
