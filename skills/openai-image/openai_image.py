#!/usr/bin/env python3
"""Generate or edit images with OpenAI GPT Image and save to a specified path.

Text-to-image:
    python openai_image.py --prompt "A cat wearing sunglasses" --output /tmp/cat.png

Image-to-image / reference edit:
    python openai_image.py --prompt "Turn this into a tourist map" \
      --image map.png --output tourist-map.png

Multiple reference images:
    python openai_image.py --prompt "Combine these into one design" \
      --image base.png --image style.png --output result.png

Masked edit:
    python openai_image.py --prompt "Replace the sky with sunset" \
      --image photo.png --mask mask.png --output edited.png

Environment:
    OPENAI_API_KEY must be set unless --dry-run is used.
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from openai import OpenAI

SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def validate_image_path(path: str, label: str) -> Path:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"{label} not found: {p}")
    if not p.is_file():
        raise ValueError(f"{label} is not a file: {p}")
    if p.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
        raise ValueError(
            f"{label} must be one of {sorted(SUPPORTED_IMAGE_SUFFIXES)}: {p}"
        )
    return p


def output_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": args.model,
        "prompt": args.prompt,
    }
    if args.size:
        kwargs["size"] = args.size
    if args.quality:
        kwargs["quality"] = args.quality
    if args.output_format:
        kwargs["output_format"] = args.output_format
    if args.output_compression is not None:
        kwargs["output_compression"] = args.output_compression
    if args.moderation:
        kwargs["moderation"] = args.moderation
    if args.n is not None:
        kwargs["n"] = args.n
    return kwargs


def write_first_image(result: Any, out_path: Path) -> None:
    if not getattr(result, "data", None):
        raise RuntimeError("No image data returned")
    image_b64 = getattr(result.data[0], "b64_json", None)
    if not image_b64:
        raise RuntimeError("No base64 image payload returned")
    out_path.write_bytes(base64.b64decode(image_b64))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate or edit images with OpenAI GPT Image.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--prompt", required=True, help="Generation/edit instruction")
    parser.add_argument("--output", required=True, help="Output image path")
    parser.add_argument(
        "--image",
        "--input-image",
        dest="images",
        action="append",
        default=[],
        help="Input/reference image path. Repeat for multiple images. If omitted, runs text-to-image.",
    )
    parser.add_argument(
        "--mask",
        help="Optional mask image for inpainting/partial edits. Applies to the first input image.",
    )
    parser.add_argument("--model", default="gpt-image-2", help="GPT Image model")
    parser.add_argument(
        "--size",
        default="1024x1024",
        help="Output size, e.g. 1024x1024, 1536x1024, 1024x1536, auto",
    )
    parser.add_argument(
        "--quality",
        choices=["auto", "low", "medium", "high"],
        help="Output quality if supported by the selected model",
    )
    parser.add_argument(
        "--output-format",
        choices=["png", "jpeg", "webp"],
        help="Output file format if supported by the selected model",
    )
    parser.add_argument(
        "--output-compression",
        type=int,
        choices=range(0, 101),
        metavar="0-100",
        help="Compression level for jpeg/webp output if supported",
    )
    parser.add_argument(
        "--moderation",
        choices=["auto", "low"],
        help="Moderation strictness if supported",
    )
    parser.add_argument("--n", type=int, help="Number of images to request")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate arguments and print planned API mode without calling OpenAI",
    )
    args = parser.parse_args()

    try:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        image_paths = [validate_image_path(p, "input image") for p in args.images]
        mask_path = validate_image_path(args.mask, "mask") if args.mask else None
        if mask_path and not image_paths:
            raise ValueError("--mask requires at least one --image")

        mode = "edit" if image_paths else "generate"
        if args.dry_run:
            print(f"mode={mode}")
            print(f"model={args.model}")
            print(f"images={len(image_paths)}")
            if mask_path:
                print(f"mask={mask_path}")
            print(f"output={out_path}")
            return 0

        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set")

        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"), timeout=900.0)
        kwargs = output_kwargs(args)

        if mode == "generate":
            result = client.images.generate(**kwargs)
        else:
            with ExitStack() as stack:
                files = [stack.enter_context(p.open("rb")) for p in image_paths]
                kwargs["image"] = files if len(files) > 1 else files[0]
                if mask_path:
                    kwargs["mask"] = stack.enter_context(mask_path.open("rb"))
                result = client.images.edit(**kwargs)

        write_first_image(result, out_path)
        print(str(out_path.resolve()))
        return 0

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
