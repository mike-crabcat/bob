#!/usr/bin/env python3
"""Generate short image-to-video clips with the Runware.ai REST API.

Takes one or more reference images plus a text prompt, animates them into a
short MP4, downloads it, and writes a WhatsApp-friendly GIF alongside.

Usage
-----
    # list video models the account can actually reach
    python skills/videogen/videogen.py --list-models

    # show wallet balance (Runware gates video on >= $5 credit)
    python skills/videogen/videogen.py --balance

    # generate
    python skills/videogen/videogen.py \
        --image ref1.png --image ref2.png \
        --prompt "The robot bows politely, gentle motion, static camera" \
        --output out.mp4 \
        --seconds 5 --model wan --size 1280x720

    # re-make a GIF from an existing video (no API call, no cost)
    python skills/videogen/videogen.py --from-existing-video old.mp4 --output old.mp4

API key is read from ./apikey next to this script (or $RUNWARE_API_KEY).
It is never hardcoded, logged, or echoed.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

API_URL = "https://api.runware.ai/v1"
API_KEY_FILE = Path(__file__).resolve().parent / "apikey"

# HTTP behaviour
REQUEST_TIMEOUT = 120          # per-call timeout (s)
SUBMIT_TIMEOUT = 300           # initial videoInference call (renders can be slow)
GEN_TIMEOUT = 900              # overall generation budget (s)
POLL_INTERVAL = 5              # seconds between getResponse polls

# GIF constraints (WhatsApp attachment limit is 16 MB for video, but GIFs are
# treated as images and are far more reliable under ~8 MB).
GIF_MAX_BYTES = 8 * 1024 * 1024
GIF_TARGET_WIDTH = 480
GIF_TARGET_FPS = 12

# Friendly aliases -> Runware AIR ids. `None` marks an alias that is known by
# name but NOT offered on this account, so we can say so precisely.
MODEL_ALIASES: dict[str, str | None] = {
    "wan": "alibaba:wan@3.0",
    "wan3": "alibaba:wan@3.0",
    "wan-prime": "alibaba:wan@3.0-prime",
    "ltx": "lightricks:ltx@2.5-fast",
    "ltx-fast": "lightricks:ltx@2.5-fast",
    "ltx-pro": "lightricks:ltx@2.5-pro",
    "seedance": "bytedance:seedance@2.5",
    "flux": "bfl:flux@3-video",
    "flux-video": "bfl:flux@3-video",
    "minimax": "minimax:h3@0",
    "minimax-h3": "minimax:h3@0",
    "minimax-h3-max": "minimax:h3@max",
    "kling": None,
    "veo": None,
}
DEFAULT_MODEL = "wan"

# Successive GIF recipes tried until one lands under GIF_MAX_BYTES.
# `width` is a scale factor applied to the requested --gif-width.
GIF_LADDER = [
    {"fps": GIF_TARGET_FPS, "scale": 1.00, "colors": 256, "dither": "bayer:bayer_scale=5"},
    {"fps": GIF_TARGET_FPS, "scale": 1.00, "colors": 128, "dither": "bayer:bayer_scale=4"},
    {"fps": 10, "scale": 0.85, "colors": 128, "dither": "bayer:bayer_scale=3"},
    {"fps": 8, "scale": 0.75, "colors": 96, "dither": "bayer:bayer_scale=3"},
    {"fps": 8, "scale": 0.65, "colors": 64, "dither": "bayer:bayer_scale=2"},
    {"fps": 6, "scale": 0.55, "colors": 64, "dither": "bayer:bayer_scale=1"},
]


def gif_ladder(target_width: int) -> list[dict]:
    """Materialise the ladder at a concrete target width."""
    out = []
    for step in GIF_LADDER:
        out.append({
            "fps": step["fps"],
            "width": max(160, int(round(target_width * step["scale"])) // 2 * 2),
            "colors": step["colors"],
            "dither": step["dither"],
        })
    return out


class VideogenError(RuntimeError):
    """Fatal, user-facing error."""


# --------------------------------------------------------------------------
# API plumbing
# --------------------------------------------------------------------------

def load_api_key(explicit: str | None = None) -> str:
    """Read the API key without ever printing it."""
    if explicit:
        key = explicit.strip()
        source = "--apikey"
    elif os.environ.get("RUNWARE_API_KEY", "").strip():
        key = os.environ["RUNWARE_API_KEY"].strip()
        source = "$RUNWARE_API_KEY"
    elif API_KEY_FILE.is_file():
        key = API_KEY_FILE.read_text(encoding="utf-8").strip()
        source = str(API_KEY_FILE)
    else:
        raise VideogenError(
            f"No API key found. Expected it at {API_KEY_FILE} "
            "(or set $RUNWARE_API_KEY, or pass --apikey)."
        )

    if not key:
        raise VideogenError(f"API key file {source} is empty.")
    return key


class Runware:
    def __init__(self, key: str) -> None:
        if requests is None:
            raise VideogenError("The 'requests' package is required. pip install requests")
        self._key = key
        self._s = requests.Session()
        self._s.headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})

    def post(self, tasks: list[dict], timeout: int = REQUEST_TIMEOUT,
             soft: bool = False) -> dict | None:
        """POST a task array; return the parsed body with the key redacted.

        With ``soft=True`` a transport failure returns ``None`` instead of
        raising, so callers can fall back to polling (video tasks run for
        minutes and the initial connection may be dropped mid-flight).
        """
        try:
            r = self._s.post(API_URL, json=tasks, timeout=timeout)
        except requests.RequestException as exc:
            if soft:
                return None
            raise VideogenError(f"Network error talking to {API_URL}: {exc}") from exc

        try:
            body = r.json()
        except ValueError:
            # Redact on the off-chance the key leaked into a plain-text error.
            if soft:
                return None
            raise VideogenError(
                f"HTTP {r.status_code}: non-JSON response "
                f"({r.text[:300].replace(self._key, '***')})"
            ) from None

        if body.get("errors"):
            raise self._to_error(body["errors"], r.status_code)
        if not body.get("data"):
            if soft:
                return None
            raise VideogenError(
                f"HTTP {r.status_code}: empty data payload: {json.dumps(body)[:400]}")
        return body

    @staticmethod
    def _to_error(errors: list[dict], status: int) -> VideogenError:
        parts = []
        for e in errors:
            code = e.get("code", "unknown")
            msg = e.get("message", "")
            parts.append(f"[{code}] {msg}".strip())
            if code == "videoInferenceInsufficientCredits":
                parts.append(
                    "\n  >> This Runware account cannot generate video yet. Video inference "
                    "requires a paid invoice or a balance of at least $5. "
                    "Top up at https://my.runware.ai/wallet then re-run."
                )
        return VideogenError(f"HTTP {status}: " + "\n".join(parts))


def get_balance(rw: Runware) -> dict:
    body = rw.post([{"taskType": "accountManagement", "taskUUID": str(uuid.uuid4()),
                     "operation": "getDetails"}])
    return body["data"][0]


def fetch_video_models(rw: Runware) -> list[dict]:
    """Every video model this account can address, image-to-video ones flagged."""
    body = rw.post([{
        "taskType": "modelSearch",
        "taskUUID": str(uuid.uuid4()),
        "filters": [{"field": "taskType", "operator": "equals", "value": "videoInference"}],
        "sorting": [{"field": "name", "direction": "asc"}],
    }])
    results = body["data"][0].get("results", []) if body.get("data") else []
    return [m for m in results if m.get("category") == "video" or
            any("video" in c for c in (m.get("capabilities") or []))]


def i2v_models(models: list[dict]) -> list[dict]:
    return [m for m in models if "io:image-to-video" in (m.get("capabilities") or [])]


def resolve_model(name: str, models: list[dict]) -> dict:
    """Map a user-supplied model name/alias onto a reachable video model."""
    avail = i2v_models(models)
    raw = (name or "").strip()
    lc = raw.lower()

    candidate = MODEL_ALIASES.get(lc, MODEL_ALIASES.get(raw, raw))
    if candidate is None:
        raise VideogenError(
            f"Model '{raw}' is not available on this Runware account.\n"
            f"  Available image-to-video models:\n"
            + "\n".join(f"    - {m['name']}  ({m['air']})" for m in avail)
        )

    for m in avail:
        if candidate.lower() in (m.get("air", "").lower(), m.get("name", "").lower(),
                                 m.get("nameContent", "").lower()):
            return m
    # last resort: substring match on name
    for m in avail:
        if candidate.lower() in m.get("name", "").lower():
            return m

    raise VideogenError(
        f"Model '{raw}' (looked for '{candidate}') is not available on this Runware account.\n"
        f"  Available image-to-video models:\n"
        + "\n".join(f"    - {m['name']}  ({m['air']})" for m in avail)
        + "\n  Run with --list-models to see the full list."
    )


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------

def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def upload_images(rw: Runware, paths: list[Path]) -> list[str]:
    """Upload local images; return their Runware URLs."""
    tasks = [{"taskType": "imageUpload", "taskUUID": str(uuid.uuid4()),
              "image": data_uri(p)} for p in paths]
    body = rw.post(tasks, timeout=300)
    data = body["data"]
    urls = [d.get("imageURL") for d in data if d.get("imageURL")]
    if len(urls) != len(paths):
        raise VideogenError(
            f"Only {len(urls)}/{len(paths)} images uploaded successfully "
            f"({json.dumps(data)[:300]})."
        )
    return urls


# --------------------------------------------------------------------------
# Generation + polling
# --------------------------------------------------------------------------

def generate_video(rw: Runware, *, model_air: str, prompt: str, image_urls: list[str],
                   duration: int, width: int, height: int, seed: int | None,
                   verbose: bool = True) -> dict:
    """Submit a videoInference task and poll until a video URL comes back."""
    task_uuid = str(uuid.uuid4())
    task: dict[str, Any] = {
        "taskType": "videoInference",
        "taskUUID": task_uuid,
        "model": model_air,
        "positivePrompt": prompt,
        "duration": duration,
        "outputType": "url",
        "outputFormat": "mp4",
    }
    # First reference is the animation source; extra references (where the model
    # supports them, e.g. Wan3.0 "reference-to-video") ride along as inputImages.
    # Wan3.0 et al. take reference frames via frameImages; inputImage is not
    # accepted by any current video model (per API validation error).
    task["frameImages"] = image_urls

    # Seedance rejects width/height; it uses "resolution" instead.
    if "seedance" in model_air.lower():
        task["resolution"] = "720p" if height <= 720 else "1080p"
    else:
        task["width"] = width
        task["height"] = height

    if seed is not None:
        task["seed"] = seed

    def log(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    log(f"  submitting videoInference: {model_air} {width}x{height} {duration}s "
        f"({len(image_urls)} ref image(s))")

    started = time.time()
    # The initial POST may stay open until the render finishes, or may be
    # dropped mid-render; either way the task lives on server-side under
    # taskUUID, so a soft failure just falls through to polling.
    body = rw.post([task], timeout=SUBMIT_TIMEOUT, soft=True)

    hit = _first_video(body) if body else None
    if hit:
        log(f"  completed inline in {time.time() - started:.0f}s")
        return hit

    # Otherwise poll getResponse until the task finishes.
    log("  queued; polling for completion...")
    while time.time() - started < GEN_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        try:
            body = rw.post([{"taskType": "getResponse", "taskUUID": task_uuid}],
                           soft=True)
        except VideogenError as exc:
            # Transient "not found yet" style errors are expected while queued.
            if "not found" in str(exc).lower() or "no data" in str(exc).lower():
                continue
            raise
        if body is None:
            continue
        hit = _first_video(body)
        if hit:
            log(f"  completed in {time.time() - started:.0f}s")
            return hit
        if verbose:
            print(f"    ... {time.time() - started:.0f}s elapsed", flush=True)

    raise VideogenError(
        f"Timed out after {GEN_TIMEOUT}s waiting for task {task_uuid}."
    )


def _first_video(body: dict) -> dict | None:
    for d in body.get("data", []):
        url = d.get("videoURL") or d.get("output") or d.get("resultURL")
        if isinstance(url, str) and url.startswith("http"):
            return d
    return None


def extract_video_url(result: dict) -> str:
    for key in ("videoURL", "output", "resultURL", "url"):
        v = result.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    raise VideogenError(f"No video URL in task result: {json.dumps(result)[:400]}")


def download(url: str, dest: Path) -> None:
    if requests is None:
        raise VideogenError("'requests' is required to download the video.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
        tmp.replace(dest)


# --------------------------------------------------------------------------
# GIF
# --------------------------------------------------------------------------

def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def probe_duration(path: Path) -> float | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def gif_path_for(mp4: Path) -> Path:
    return mp4.with_suffix(".gif")


def make_gif_ffmpeg(mp4: Path, out: Path, fps: int, width: int, colors: int, dither: str) -> None:
    filt = (
        f"fps={fps},scale={width}:-1:flags=lanczos,"
        f"split[a][b];[a]palettegen=max_colors={colors}:stats_mode=diff[p];"
        f"[b][p]paletteuse=dither={dither}"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(mp4),
         "-vf", filt, "-loop", "0", str(out)],
        check=True, capture_output=True, timeout=600,
    )


def make_gif_pil(mp4: Path, out: Path, fps: int, width: int) -> None:
    """ffmpeg-less fallback. PIL cannot decode H.264, so this needs imageio."""
    try:
        import imageio.v3 as iio  # type: ignore[import-not-found]
    except ImportError:
        raise VideogenError(
            "ffmpeg is not installed and imageio is unavailable, so a GIF could "
            "not be produced. The MP4 was saved; install ffmpeg to get the GIF."
        ) from None

    from PIL import Image  # type: ignore[import-not-found]

    frames: list[Image.Image] = []
    for frame in iio.imiter(str(mp4), plugin="pyav"):
        w, h = frame.size
        nh = max(1, round(h * width / w))
        frames.append(frame.convert("P", palette=Image.ADAPTIVE).resize((width, nh)))
    if not frames:
        raise VideogenError(f"Could not decode any frames from {mp4}")
    frames[0].save(out, save_all=True, append_images=frames[1:], loop=0,
                   duration=int(1000 / fps), optimize=True)


def make_gif(mp4: Path, target_width: int = GIF_TARGET_WIDTH, verbose: bool = True) -> Path:
    """Write a small looping GIF next to the MP4, under GIF_MAX_BYTES."""
    out = gif_path_for(mp4)

    if have_ffmpeg():
        for step in gif_ladder(target_width):
            try:
                make_gif_ffmpeg(mp4, out, step["fps"], step["width"], step["colors"], step["dither"])
            except subprocess.CalledProcessError as exc:
                raise VideogenError(
                    f"ffmpeg failed to build the GIF: "
                    f"{exc.stderr.decode(errors='replace')[:400] if exc.stderr else exc}"
                ) from exc
            size = out.stat().st_size
            if size <= GIF_MAX_BYTES:
                if verbose:
                    print(f"  gif: {out.name} {size / 1e6:.2f} MB "
                          f"({step['width']}px @ {step['fps']}fps)")
                return out
            if verbose:
                print(f"  gif too big ({size / 1e6:.2f} MB), retrying smaller...")
        # Even the smallest recipe overshot; keep the last (smallest) attempt.
        return out

    # No ffmpeg -> PIL/imageio fallback, single conservative recipe.
    make_gif_pil(mp4, out, GIF_TARGET_FPS, target_width)
    if verbose:
        print(f"  gif: {out.name} {out.stat().st_size / 1e6:.2f} MB (PIL fallback)")
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_size(size: str) -> tuple[int, int]:
    try:
        w, h = size.lower().split("x")
        w, h = int(w), int(h)
        if w <= 0 or h <= 0:
            raise ValueError
        return w, h
    except Exception:
        raise VideogenError(f"Bad --size {size!r}; expected e.g. 1280x720.") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="videogen",
        description="Generate short image-to-video clips with Runware.ai.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--image", "-i", action="append", default=[], metavar="PATH",
                   help="reference image (repeat for multiple)")
    p.add_argument("--prompt", "-p", default="", help="motion / scene description")
    p.add_argument("--output", "-o", default="out.mp4", help="output .mp4 path")
    p.add_argument("--seconds", type=int, default=5, help="clip duration (default 5)")
    p.add_argument("--model", "-m", default=DEFAULT_MODEL,
                   help=f"model alias or AIR id (default {DEFAULT_MODEL})")
    p.add_argument("--size", default="1280x720", help="WxH (default 1280x720)")
    p.add_argument("--seed", type=int, default=None, help="fixed seed for reproducibility")
    p.add_argument("--no-gif", action="store_true", help="skip GIF extraction")
    p.add_argument("--gif-width", type=int, default=GIF_TARGET_WIDTH,
                   help=f"target GIF width in px (default {GIF_TARGET_WIDTH})")
    p.add_argument("--list-models", action="store_true",
                   help="list video models available to this account and exit")
    p.add_argument("--balance", action="store_true",
                   help="print wallet balance / usage and exit")
    p.add_argument("--from-existing-video", metavar="MP4",
                   help="skip generation; just (re)build the GIF for an existing video")
    p.add_argument("--apikey", default=None, help=argparse.SUPPRESS)
    p.add_argument("--dry-run", action="store_true",
                   help="validate everything, upload nothing, bill nothing")
    return p


def cmd_list_models(rw: Runware) -> int:
    models = fetch_video_models(rw)
    i2v = i2v_models(models)
    print(f"{len(models)} video models reachable; {len(i2v)} accept image-to-video:\n")
    for m in i2v:
        caps = ",".join(c.replace("io:", "").replace("form:", "")
                        for c in (m.get("capabilities") or []) if c != "form:checkpoint")
        print(f"  {m['name']:<20} air={m['air']:<28} {caps}")
        if m.get("comment"):
            print(f"      {m['comment']}")
    return 0


def cmd_balance(rw: Runware) -> int:
    d = get_balance(rw)
    bal = d.get("balance")
    usage = d.get("usage", {}).get("total", {})
    print(f"Balance : {bal} (video inference needs >= 5)")
    print(f"Usage   : {usage.get('credits')} credits over {usage.get('requests')} requests")
    gate = "OK" if (bal is not None and bal >= 5) else "BLOCKED"
    print(f"Video   : {gate}"
          + ("" if gate == "OK" else "  -> top up at https://my.runware.ai/wallet"))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.from_existing_video:
            src = Path(args.from_existing_video)
            if not src.is_file():
                raise VideogenError(f"No such video: {src}")
            out_mp4 = Path(args.output)
            if out_mp4.resolve() != src.resolve():
                shutil.copy2(src, out_mp4)
            if not args.no_gif:
                make_gif(out_mp4, target_width=args.gif_width)
            _report(out_mp4)
            return 0

        key = load_api_key(args.apikey)
        rw = Runware(key)

        if args.balance:
            return cmd_balance(rw)
        if args.list_models:
            return cmd_list_models(rw)

        if not args.image:
            raise VideogenError("At least one --image is required.")
        if not args.prompt:
            raise VideogenError("--prompt is required.")
        if args.seconds <= 0:
            raise VideogenError("--seconds must be > 0.")
        if not args.output.lower().endswith(".mp4"):
            raise VideogenError(f"--output should end in .mp4 (got {args.output!r}).")

        width, height = parse_size(args.size)
        images = [Path(p) for p in args.image]
        for p in images:
            if not p.is_file():
                raise VideogenError(f"Reference image not found: {p}")
        output = Path(args.output)

        print(f"videogen: model={args.model} {width}x{height} {args.seconds}s "
              f"refs={len(images)}")

        models = fetch_video_models(rw)
        model = resolve_model(args.model, models)
        print(f"  model resolved: {model['name']} ({model['air']})")

        if args.dry_run:
            print("  dry-run: stopping before upload/generation (nothing billed).")
            return 0

        urls = upload_images(rw, images)
        print(f"  uploaded {len(urls)} reference image(s)")

        result = generate_video(
            rw, model_air=model["air"], prompt=args.prompt, image_urls=urls,
            duration=args.seconds, width=width, height=height, seed=args.seed,
        )
        url = extract_video_url(result)
        download(url, output)
        size = output.stat().st_size
        print(f"  saved {output} ({size / 1e6:.2f} MB)")

        if not args.no_gif:
            make_gif(output, target_width=args.gif_width)

        _report(output)
        return 0

    except VideogenError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _report(mp4: Path) -> None:
    """Print a short verifiability summary for the produced file."""
    if not mp4.is_file():
        return
    size = mp4.stat().st_size
    dur = probe_duration(mp4)
    dur_s = f"{dur:.2f}s" if dur is not None else "unknown duration"
    gif = gif_path_for(mp4)
    gif_s = (f", gif {gif.stat().st_size / 1e6:.2f} MB"
             if gif.is_file() else ", no gif")
    print(f"  -> {mp4} : {size / 1e6:.2f} MB, {dur_s}{gif_s}")


if __name__ == "__main__":
    # Piping output into `head`/`less` closes stdout early; exit quietly
    # instead of spewing a BrokenPipeError traceback.
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0) from None
