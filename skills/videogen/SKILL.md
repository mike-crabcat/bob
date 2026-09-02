---
name: videogen
description: Generate short image-to-video clips from a text prompt plus one or more reference images using the Runware.ai API, downloading an MP4 and a WhatsApp-sized GIF.
trigger: when Bob asks to animate an image, make a video from a picture or reference image, generate a short clip, turn a still into motion, or produce a GIF version of a video
---

## Purpose

Animate one or more reference images into a short video with Runware.ai
image-to-video, then save both a full-quality MP4 and a small looping GIF
suitable for WhatsApp.

## Script

`skills/videogen/videogen.py`

## IMPORTANT: run asynchronously

Video generation takes **1–10 minutes**. NEVER run this with the `bash` tool in a
blocking way. Follow the same pattern as `skills/openai-image`:

1. Ack briefly ("On it — video rendering, few minutes").
2. `create_subagent(task="python skills/videogen/videogen.py --image ... --prompt '...' --output /home/bob/workspace/generated-images/<name>.mp4", agent_type='script')`
3. End your turn; you will be woken when it finishes.

## Basic usage

```bash
python skills/videogen/videogen.py \
  --image /home/bob/workspace/generated-images/refpack-07-hero-pose.png \
  --prompt "The robot bows politely to the camera then stands back up, gentle motion, static camera, warm lighting" \
  --output /home/bob/workspace/generated-images/videogen-test.mp4 \
  --seconds 5 --model wan --size 1280x720
```

Writes `videogen-test.mp4` **and** `videogen-test.gif` next to it.

### Multiple reference images

```bash
python skills/videogen/videogen.py \
  --image base.png --image style-ref.png \
  --prompt "Keep the character identical, camera orbits slowly" \
  --output out.mp4 --model wan
```

The first image is the animation source (`inputImage`); extras are passed as
`inputImages`. Only some models honour extra references — Wan3.0, Seedance 2.5
and MiniMax H3 advertise multi-reference support. Treat multi-reference behaviour
as **unverified** until confirmed on a real run.

## Arguments

Required: `--image` (repeatable), `--prompt`, `--output` (must end `.mp4`).

| Flag | Default | Notes |
|---|---|---|
| `--seconds` | `5` | clip duration |
| `--model` | `wan` | alias or raw AIR id |
| `--size` | `1280x720` | `WxH` |
| `--seed` | random | for reproducibility |
| `--no-gif` | off | skip GIF extraction |
| `--gif-width` | `480` | GIF target width in px |
| `--dry-run` | off | validate model + args, upload nothing, bill nothing |
| `--list-models` | — | list reachable video models, exit |
| `--balance` | — | print wallet balance/usage, exit |
| `--from-existing-video MP4` | — | rebuild the GIF for an existing video, **no API call** |

## Model options

`kling` and `veo` are **not** on this account. Aliases → AIR ids:

| Alias | AIR id | Notes |
|---|---|---|
| `wan` (default) | `alibaba:wan@3.0` | up to 30s, multi-reference, edit/extend |
| `wan-prime` | `alibaba:wan@3.0-prime` | lower latency, same quality |
| `ltx`, `ltx-fast` | `lightricks:ltx@2.5-fast` | fast, t2v + i2v |
| `ltx-pro` | `lightricks:ltx@2.5-pro` | up to 4K, audio, editing |
| `seedance` | `bytedance:seedance@2.5` | 30s, large-scale reference control |
| `flux`, `flux-video` | `bfl:flux@3-video` | synced audio |
| `minimax`, `minimax-h3` | `minimax:h3@0` | multi-reference consistency |
| `minimax-h3-max` | `minimax:h3@max` | higher throughput, first/last-frame |

Any raw AIR id or display name also works. A full current list:

```bash
python skills/videogen/videogen.py --list-models
```

Asking for an unavailable model exits 1 with the list of what *is* available.

## API key — never print it

Preferred: set `RUNWARE_API_KEY` in the instance environment (`.env`). A file at `skills/videogen/apikey` (chmod `600`, raw key) also works locally. Resolution order: `--apikey` → `$RUNWARE_API_KEY` → `skills/videogen/apikey`.
Resolution order: `--apikey` → `$RUNWARE_API_KEY` → `skills/videogen/apikey`.

**Never** hardcode the key, echo it, paste it into a prompt or a commit, or log
it. The script redacts it from error output, but the rule is: don't put it
anywhere in the first place. `--balance` prints only the numeric balance and
usage counters — Runware returns API keys pre-masked and the script does not
print even those.

## Cost notes

- Runware bills video **per second generated**, and the rate differs per model
  (Pro/4K tiers cost more than Fast tiers). `modelSearch` does **not** expose
  prices, so the script cannot quote a cost before running.
- Check the live rate on the Runware pricing page or the dashboard, and check
  remaining credit any time with `python skills/videogen/videogen.py --balance`.
- Cost-control habits: prefer `--seconds 5`, prefer `wan`/`ltx-fast` over Pro
  tiers, and use `--dry-run` to validate before spending.
- Use `--from-existing-video` to re-make a GIF for free instead of regenerating.

## BLOCKER: minimum $5 balance

**Video inference is gated.** Any `videoInference` call returns HTTP 400
`videoInferenceInsufficientCredits` unless the account has a paid invoice **or a
balance of at least $5**. At time of writing the balance was `2`, so every
generation fails with a clear message pointing at
`https://my.runware.ai/wallet`. Run `--balance` before promising a video to Bob.

Image upload (`imageUpload`) and model listing (`modelSearch`) are *not* gated
and work fine at low balance.

## Gotchas

- Runware is a **task-array** API: `POST https://api.runware.ai/v1` with a JSON
  *array* of task objects, each carrying `taskType` and `taskUUID`. The response
  is `{"data": [...], "errors": [...]}` — always check `errors`.
- Valid `taskType` values include `videoInference`, `imageUpload`,
  `modelSearch`, `accountManagement`, `getResponse`. `accountDetails` and
  `listModels` are **not** valid and 400 immediately.
- `accountManagement` needs `operation` ∈ `getDetails`, `getUsageActivity`,
  `getUsagePerformance`, `getUsageErrors` — not `accountAction`/`balance`.
- Images must be uploaded first (`imageUpload` with a `data:` URI) to get an
  `imageURL`; you cannot hand Runware a local path.
- Generation may complete inline on the POST or may need `getResponse` polling
  by `taskUUID`. The script does both, with a 15-minute overall budget.
- `zoompan`-style local renders are handy for testing, but a real end-to-end
  test requires the balance gate to be lifted.
- GIFs are capped at 8 MB (480px @ 12fps by default); if a clip is too busy the
  script automatically steps down fps/width/colours until it fits.
- The ffmpeg-less PIL fallback needs `imageio` to decode H.264 — PIL alone
  cannot read MP4. ffmpeg is installed on this box, so this rarely matters.

## Dependencies

`python` (shared venv), `requests`, `pillow`, and `ffmpeg`/`ffprobe` for GIF
extraction and duration probing. All present.
