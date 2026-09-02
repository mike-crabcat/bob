# openai-image skill

Generate or edit images with OpenAI GPT Image.

## Features

- Text-to-image generation.
- Image-to-image edits/stylisation using `--image`.
- Multiple reference images by repeating `--image`.
- Optional mask-based partial edits via `--mask`.
- Dry-run validation.

## Examples

### Text only

```bash
python skills/openai-image/openai_image.py \
  --prompt 'A friendly dinosaur wearing a party hat' \
  --output generated-images/dinosaur.png
```

### Stylise an existing map

```bash
python skills/openai-image/openai_image.py \
  --prompt 'Edit this into a clear illustrated tourist map. Preserve road layout, landmark positions, and overall geography. Do not invent landmarks.' \
  --image trips/trip-france-holiday-june-2026/cadillac-local-map/cadillac-walking-map-v2.png \
  --output trips/trip-france-holiday-june-2026/cadillac-local-map/cadillac-tourist-map-edit.png \
  --size 1024x1024
```

### Use a style reference

```bash
python skills/openai-image/openai_image.py \
  --prompt 'Edit the first image to use the warm watercolour style of the second image while preserving the first image layout.' \
  --image base-map.png \
  --image style-reference.png \
  --output result.png
```

### Validate without spending money

```bash
python skills/openai-image/openai_image.py \
  --prompt 'Test' \
  --image base-map.png \
  --output out.png \
  --dry-run
```

## Implementation

The script chooses mode automatically:

- no `--image`: `client.images.generate(...)`
- one or more `--image`: `client.images.edit(...)`

Environment variable required for real calls:

```bash
OPENAI_API_KEY=...
```

## Accuracy warning

Image models are not GIS systems. For anything navigational, use generated maps as decorative companions and verify labels/pins against a real map. If exact placement matters, post-process with SVG/HTML overlays.
