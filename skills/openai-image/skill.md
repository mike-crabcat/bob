---
name: openai-image
description: Generate images via OpenAI GPT Image, including text-to-image and image-to-image edits with one or more input/reference images.
trigger: when Bob asks to generate, create, or make an image, picture, illustration, edit an image, stylise an image, transform an existing image, or use one/multiple images as visual references
---

## Purpose

Use this skill to create images with OpenAI GPT Image.

It supports:
- text-to-image generation;
- image-to-image edits/stylisation;
- multiple input/reference images;
- optional mask-based edits for inpainting/partial edits.

Prefer image-to-image when the user cares about preserving structure, layout, or content from an existing image. Text-only image generation is charming, but it also lies like a cartographer paid by the landmark.

## REAL PEOPLE: always use their reference photo

Before generating an image **of a real person** (a group member, contact, or
the owner — "photo of me", "put Ryan in a Dockers beanie", etc.):

1. Check the people galleries for their reference photo — `ls people/` shows
   one directory per person (`people/mike-cleaver/`, `people/ryan/`, …), or
   run `skills/faces/.venv/bin/python skills/faces/faces.py gallery` to list
   every gallery and how each reference was attested.
2. If a reference exists (`selfie.jpg`, `portrait.jpg`, …), pass it as
   `--image people/<slug>/<file>` and phrase the prompt as an edit:
   "keep this exact person's face fully recognisable, …".
3. Never render a real person from a text description when a reference
   exists — that produces a generic stranger, and the group will (rightly)
   roast you for it. If no reference exists, say so and ask them to send a
   photo — don't guess their face.
4. Never use a generated/edited image as the reference for a later
   generation (see the faces skill: originals only).

## Script

`skills/openai-image/openai_image.py`

## IMPORTANT: run asynchronously

Image generation takes 30–90 seconds. NEVER run this script with the `bash`
tool — it blocks the whole conversation. Instead:

1. Send the user a short ack first ("On it — image coming shortly").
2. `create_subagent(task="python skills/openai-image/openai_image.py --prompt '...' --output /home/bob/workspace/generated-images/<name>.png --size 1024x1024", agent_type='script')`
3. End your turn. You will be woken automatically when it finishes — send the
   generated image to the user then, with a short comment.


## Basic usage

### Text-to-image

```bash
python skills/openai-image/openai_image.py \
  --prompt 'A warm watercolour tourist map of a French village' \
  --output /home/bob/workspace/generated-images/tourist-map.png \
  --size 1024x1024
```

### Edit / stylise one existing image

```bash
python skills/openai-image/openai_image.py \
  --prompt 'Edit this map into a hand-drawn tourist brochure style while preserving the street layout and landmark positions.' \
  --image trips/example/map.png \
  --output trips/example/map-stylised.png \
  --size 1024x1024
```

### Multiple input/reference images

```bash
python skills/openai-image/openai_image.py \
  --prompt 'Edit the first image into the visual style of the second image, preserving labels and geometry from the first.' \
  --image trips/example/base-map.png \
  --image trips/example/style-reference.png \
  --output trips/example/stylised-map.png \
  --size 1536x1024
```

### Masked edit

```bash
python skills/openai-image/openai_image.py \
  --prompt 'Replace only the highlighted area with a small illustrated café icon.' \
  --image trips/example/map.png \
  --mask trips/example/mask.png \
  --output trips/example/map-edited.png
```

## Arguments

Required:
- `--prompt`: image generation/edit instruction.
- `--prompt-file`: read the instruction from a file instead of `--prompt` (give exactly one of the two).
- `--output`: output file path.

Optional:
- `--image` / `--input-image`: input/reference image. Repeat this argument for multiple images.
- `--mask`: mask image for partial edits. If multiple images are supplied, the mask applies to the first image.
- `--model`: default `gpt-image-2`.
- `--size`: default `1024x1024`; common values include `1024x1024`, `1536x1024`, `1024x1536`, `auto`.
- `--quality`: `auto`, `low`, `medium`, `high`.
- `--output-format`: `png`, `jpeg`, `webp`.
- `--output-compression`: `0-100`, for jpeg/webp where supported.
- `--moderation`: `auto` or `low`.
- `--dry-run`: validate mode/inputs without calling the API.

## Shell quoting: prefer --prompt-file

Prompts are prose and constantly contain apostrophes ("keep this man's
face…"), which silently break single-quoted shell commands — the subagent
task dies with `unexpected EOF while looking for matching '` and the retry
drops the reference image. For any prompt with an apostrophe (or quotes,
backticks, dollar signs), write the prompt to a file and use `--prompt-file`:

```bash
cat > scratch/img-prompt.txt <<'EOF'
Edit this photo: keep this exact man's face fully recognisable, but he is
dressed as Morpheus from The Matrix — long black leather coat, small round
mirror sunglasses, photorealistic film still.
EOF
python skills/openai-image/openai_image.py \
  --prompt-file scratch/img-prompt.txt \
  --image people/mike-cleaver/selfie.png \
  --output generated-images/mike-morpheus.png
```

Short apostrophe-free prompts may still use `--prompt '…'`.

## Operational rules

- If the output must be accurate — maps, timetables, forms, labels — use the real source image as `--image` and ask the model to preserve layout.
- For maps, still treat generated output as decorative unless verified against the real map.
- For exact text, labels, and pin placement, prefer manual SVG/HTML overlays after generation.
- Send a short WhatsApp status before starting; image generation can be slow.
- After generation, inspect with `read_image` before sending.
- Use `send_whatsapp_message` with `media_path` to deliver the result.

## Notes

The OpenAI Image API supports edits with one or more image inputs. Masked edits are supported; with multiple input images, the mask applies to the first image.
