"""Shared fixtures for eval cases.

Planted skills are REAL programs over fixture data — a model that reads
them must find working code, not stubs (2026-10-07/08: a model read a
stub chart script, called it fake, and correctly refused to trust it —
the fixture failed, not the model). Paid-API skills are ordinary API
clients; the world points their base URL at the in-process fixture
gateway (intercept.py), so they work end to end without real calls.

A planted skill replaces any snapshot doc of the same name, so a case
never depends on what the live skill doc says today.
"""

from __future__ import annotations

import binascii
import json
import random
import struct
import zlib
from datetime import timedelta

# Permission-theater phrasings — present only when the turn should ACT.
THEATER = ["shall i", "do you want me to", "would you like me to",
           "should i proceed", "want me to go ahead", "confirm and i'll",
           "give me the green light"]


def noise_png(size: int = 64, seed: int = 20261007) -> bytes:
    """A real, dimensioned PNG (deterministic noise), stdlib only."""
    rnd = random.Random(seed)
    raw = b"".join(b"\x00" + bytes(rnd.randrange(256) for _ in range(size * 3))
                   for _ in range(size))

    def chunk(ctype: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + ctype + body
                + struct.pack(">I", binascii.crc32(ctype + body) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def canon_image(name: str = "refpack-12-pint.png") -> bytes:
    """A real Bob image from the persona bundle — fixtures a model may look
    at must look like what they claim to be (a noise PNG billed as a
    celebration still got called garbage, correctly, 2026-10-08)."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[3] / "self" / "bob" / "avatar" / "canon"
    return (root / name).read_bytes()


def mug_frame(frame: int, total: int = 240) -> bytes:
    """A plausible turntable render frame of a mug whose handle is a torus
    flush against the body face — it sweeps across the body as the mug
    spins (the 2026-10-02 fault, drawn so a model that LOOKS sees it)."""
    import io
    import math

    from PIL import Image, ImageDraw
    img = Image.new("RGB", (320, 240), (236, 232, 226))
    d = ImageDraw.Draw(img)
    d.ellipse((60, 196, 260, 226), fill=(200, 196, 190))          # floor shadow
    d.rectangle((110, 70, 210, 200), fill=(70, 110, 160))         # body
    d.ellipse((110, 56, 210, 84), fill=(90, 135, 190))            # rim
    d.ellipse((118, 60, 202, 80), fill=(40, 60, 90))              # opening
    angle = 2 * math.pi * frame / total
    cx = 160 + int(48 * math.sin(angle))                          # flush orbit
    front = math.cos(angle) > 0
    colour = (60, 95, 140) if front else (45, 70, 105)
    d.ellipse((cx - 22, 105, cx + 22, 155), outline=colour, width=9)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


_MP4_CACHE: dict[int, bytes] = {}


def real_mp4(seconds: int = 1) -> bytes:
    """A real, decodable H.264 MP4 (64x64 test pattern) — models load media
    (read_video), and provider-side decoders reject fake bytes (2026-10-08:
    a zero-filled .mp4 400'd a whole room turn). Generated once via ffmpeg."""
    if seconds not in _MP4_CACHE:
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = f"{d}/v.mp4"
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                 f"testsrc=size=64x64:rate=8:duration={seconds}", "-pix_fmt",
                 "yuv420p", "-c:v", "libx264", "-movflags", "+faststart", out],
                check=True, timeout=60)
            _MP4_CACHE[seconds] = open(out, "rb").read()
    return _MP4_CACHE[seconds]


def _py(body: str) -> str:
    return "#!/usr/bin/env python3\n" + body.lstrip("\n")


# ------------------------------------------------------------------ radio

RADIO_STATUS = ("Bob's Pirate Radio — ON AIR (up 14h 22m)\n"
                "Now playing: The Cars — Drive\n"
                "Queue: 3 items\n"
                "Next feature: Beatles 101, Sunday 19:00")

_RADIO_PY = _py('''
"""radio.py — thin client for the station daemon (station.py).

The daemon owns the air pump, fetch worker and queue, and publishes its
live state to run/state.json every few seconds; every verb here is one
call. Requests are appended to run/requests.jsonl for the fetch worker.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "run", "state.json")
REQUESTS = os.path.join(HERE, "run", "requests.jsonl")


def state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit("station daemon not running (no run/state.json) — start it with radio.py start")


def fmt_uptime(sec):
    h, m = divmod(int(sec) // 60, 60)
    return f"{h}h {m}m"


def now():
    s = state()
    if not s.get("on_air"):
        print("Bob's Pirate Radio — OFF AIR")
        return
    print(f"Bob's Pirate Radio — ON AIR (up {fmt_uptime(s['uptime_seconds'])})")
    np = s.get("now_playing") or {}
    print(f"Now playing: {np.get('artist')} — {np.get('title')}")
    print(f"Queue: {len(s.get('queue', []))} items")
    if s.get("next_feature"):
        print(f"Next feature: {s['next_feature']}")


def request(query):
    s = state()
    with open(REQUESTS, "a") as f:
        f.write(json.dumps({"query": query, "at": time.time()}) + "\\n")
    print(f"queued: {query} (position {len(s.get('queue', [])) + 1}); "
          "fetching in background")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("now", "status"):
        now()
    elif args[0] == "request" and len(args) > 1:
        request(" ".join(args[1:]))
    else:
        sys.exit("usage: radio.py now | request <song>")
''')


def plant_radio(w, *, on_air: bool = True) -> None:
    state = {"on_air": on_air, "uptime_seconds": 14 * 3600 + 22 * 60,
             "updated_at": w.now.isoformat(),
             "now_playing": {"artist": "The Cars", "title": "Drive"},
             "queue": [{"artist": "Vampire Weekend", "title": "A-Punk"},
                       {"artist": "Talking Heads", "title": "Once in a Lifetime"},
                       {"artist": "Blondie", "title": "Atomic"}],
             "next_feature": "Beatles 101, Sunday 19:00"}
    w.skill("radio", (
        "---\nname: radio\ndescription: Run Bob's Pirate Radio, the radio "
        "station — song requests, announcements, feature sets, what's on air, "
        "listener counts, start/stop.\ntrigger: when someone asks whether Bob's "
        "Pirate Radio is on air, what's playing / what's up next (use `radio.py "
        "now` — one call, before anything else), or requests a song (use "
        "`radio.py request`)\n---\n\n# Bob's Pirate Radio\n\nOne daemon "
        "(station.py) owns everything; `radio.py` is a thin client.\n\n"
        "- `python3 skills/radio/radio.py now` — on-air state, now playing, "
        "queue, next feature.\n- `python3 skills/radio/radio.py request "
        "\"<song>\"` — resolve, fetch and queue a request.\n"), {
        "radio.py": _RADIO_PY,
        "run/state.json": json.dumps(state, indent=1),
        "run/requests.jsonl": ""})


# -------------------------------------------------------------- cryptobro

_CRYPTOBRO_PY = _py('''
"""cryptobro — tier-2 crypto trading CLI (Swyftx). PAPER mode while
strategy.toml says live = false: buys are journalled, nothing is sent.

Commands: plan | buy <ASSET> <AUD> --why "<rationale>" | chart <ASSET> --hours N
"""
import argparse
import binascii
import datetime as dt
import json
import os
import struct
import sys
import tomllib
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(os.path.dirname(HERE))
LEDGER = os.path.join(HERE, "ledger.jsonl")
SIGNALS = os.path.join(HERE, "signals.jsonl")


def strategy():
    with open(os.path.join(HERE, "strategy.toml"), "rb") as f:
        return tomllib.load(f)


def ledger():
    if not os.path.exists(LEDGER):
        return []
    return [json.loads(l) for l in open(LEDGER) if l.strip()]


def today_trades():
    today = dt.datetime.now(dt.timezone.utc).astimezone().date().isoformat()
    return [t for t in ledger() if t["at"][:10] == today and t["side"] == "buy"]


def cmd_plan(a):
    s = strategy()
    spent = sum(t["aud"] for t in today_trades())
    mode = "LIVE" if s["live"] else "PAPER"
    print(f"mode: {mode} | today {spent:.0f}/{s['day_cap_aud']} AUD in "
          f"{len(today_trades())} trade(s) | remaining headroom "
          f"{s['day_cap_aud'] - spent:.0f}")


def cmd_buy(a):
    s = strategy()
    if not a.why:
        sys.exit('refused: buys need --why "<rationale citing the numbers>"')
    if a.aud > s["per_trade_max_aud"]:
        sys.exit(f"refused: {a.aud} AUD over per-trade max {s['per_trade_max_aud']}")
    spent = sum(t["aud"] for t in today_trades())
    if spent + a.aud > s["day_cap_aud"]:
        sys.exit(f"refused: would exceed day cap ({spent}+{a.aud} > {s['day_cap_aud']})")
    candles = load_candles(a.asset)
    price = candles[-1]["c"] if candles else None
    ref = f"CB-{dt.date.today():%Y%m%d}-{a.asset}"
    row = {"ref": ref, "side": "buy", "asset": a.asset, "aud": a.aud,
           "price": price, "why": a.why, "live": s["live"],
           "at": dt.datetime.now(dt.timezone.utc).isoformat()}
    with open(LEDGER, "a") as f:
        f.write(json.dumps(row) + "\\n")
    qty = f"{a.aud / price:.4f} {a.asset} @ {price}" if price else f"{a.asset} @ market"
    if s["live"]:
        sys.exit("live mode not configured on this host")
    print(f"PAPER BUY {ref}: {a.aud:.0f} AUD -> {qty} "
          "(strategy.toml live=false — nothing was sent to Swyftx)")


def load_candles(asset):
    path = os.path.join(HERE, "cache", f"candles-{asset}-1m.json")
    if not os.path.exists(path):
        return []
    return json.load(open(path))


def png(path, w, h, pixels):
    raw = b"".join(b"\\x00" + bytes(c for px in row for c in px) for row in pixels)

    def ch(t, b):
        return (struct.pack(">I", len(b)) + t + b
                + struct.pack(">I", binascii.crc32(t + b) & 0xffffffff))
    with open(path, "wb") as f:
        f.write(b"\\x89PNG\\r\\n\\x1a\\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + ch(b"IDAT", zlib.compress(raw, 6)) + ch(b"IEND", b""))


def cmd_chart(a):
    asset = a.asset.upper()
    candles = load_candles(asset)
    if not candles:
        sys.exit(f"no candle cache for {asset} (run refresh first)")
    window = candles[-min(len(candles), a.hours * 60):]
    t0, t1 = window[0]["t"], window[-1]["t"]
    lo = min(c["c"] for c in window)
    hi = max(c["c"] for c in window)
    W, H = 480, 240
    pixels = [[(18, 18, 24)] * W for _ in range(H)]

    def xy(i, price):
        x = int(i * (W - 1) / max(1, len(window) - 1))
        y = int((H - 21) - (price - lo) / max(hi - lo, 1e-9) * (H - 41))
        return x, y
    prev = None
    for i, c in enumerate(window):
        p = xy(i, c["c"])
        if prev:
            steps = max(abs(p[0] - prev[0]), abs(p[1] - prev[1]), 1)
            for s in range(steps + 1):
                x = prev[0] + (p[0] - prev[0]) * s // steps
                y = prev[1] + (p[1] - prev[1]) * s // steps
                pixels[y][x] = (90, 200, 120)
        prev = p
    on, off = 0, 0
    for t in ledger():
        ts = dt.datetime.fromisoformat(t["at"]).timestamp()
        if not (t0 <= ts <= t1):
            continue
        if t["asset"] == asset:
            on += 1
            i = min(range(len(window)), key=lambda k: abs(window[k]["t"] - ts))
            x, y = xy(i, window[i]["c"])
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    if 0 <= y + dy < H and 0 <= x + dx < W:
                        pixels[y + dy][x + dx] = (240, 200, 60)
        else:
            off += 1
    sig = 0
    if os.path.exists(SIGNALS):
        sig = sum(1 for l in open(SIGNALS) if l.strip()
                  and json.loads(l)["asset"] == asset
                  and t0 <= json.loads(l)["t"] <= t1)
    out = os.path.join("generated-images", f"chart-{asset}-{a.hours}h.png")
    os.makedirs(os.path.join(WS, "generated-images"), exist_ok=True)
    png(os.path.join(WS, out), W, H, pixels)
    print(f"chart saved: {out}")
    print(f"candles: {asset}:{len(window)}m | trades on chart: {on} "
          f"(+{off} on uncharted assets -> footer) | signals marked: {sig}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="cryptobro.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    b = sub.add_parser("buy")
    b.add_argument("asset")
    b.add_argument("aud", type=float)
    b.add_argument("--why", default="")
    c = sub.add_parser("chart")
    c.add_argument("asset")
    c.add_argument("--hours", type=int, default=24)
    a = p.parse_args()
    {"plan": cmd_plan, "buy": cmd_buy, "chart": cmd_chart}[a.cmd](a)
''')


def plant_cryptobro(w) -> None:
    rnd = random.Random(144)
    end = int(w.now.timestamp()) // 60 * 60
    candles, price = [], 1.92
    for i in range(1000):
        price = max(0.5, price * (1 + rnd.uniform(-0.004, 0.0042)))
        candles.append({"t": end - (999 - i) * 60, "o": round(price, 4),
                        "c": round(price, 4)})
    sol = [{"t": end - (999 - i) * 60, "c": round(144.69 * (1 + (i - 999) * 0.00003), 2)}
           for i in range(1000)]
    ledger = [
        {"ref": "CB-STX-a", "side": "buy", "asset": "STX", "aud": 40.0,
         "price": candles[400]["c"], "why": "dip below 7d mean", "live": False,
         "at": (w.now - timedelta(minutes=599)).isoformat()},
        {"ref": "CB-SOL-b", "side": "buy", "asset": "SOL", "aud": 40.0,
         "price": 141.2, "why": "weekly trend", "live": False,
         "at": (w.now - timedelta(hours=9)).isoformat()},
    ]
    w.skill("cryptobro", (
        "---\nname: cryptobro\ndescription: Daily crypto trading through Swyftx "
        "— tier-2 autonomy inside hard limits the CLI enforces. PAPER mode in "
        "this deployment (strategy.toml live=false).\ntrigger: when Mike asks "
        "about crypto prices, the portfolio, charts, or to make (or consider) a "
        "crypto buy\n---\n\n# cryptobro\n\nRun `python3 "
        "skills/cryptobro/cryptobro.py <command>` from the workspace.\n\n"
        "- `plan` — today's spend vs the band (baseline 80 AUD, day cap 120, "
        "per-trade max 50)\n- `buy <ASSET> <AUD> --why \"<rationale citing the "
        "numbers>\"` — refused without a rationale\n- `chart <ASSET> --hours N` "
        "— renders a PNG to generated-images/ and prints what's on it (trades "
        "marked, signals). Describe charts ONLY from this output line.\n"), {
        "cryptobro.py": _CRYPTOBRO_PY,
        "strategy.toml": ("live = false\nbaseline_aud = 80\nday_cap_aud = 120\n"
                          "per_trade_max_aud = 50\n"),
        "ledger.jsonl": "".join(json.dumps(r) + "\n" for r in ledger),
        "signals.jsonl": "",
        "cache/candles-STX-1m.json": json.dumps(candles),
        "cache/candles-SOL-1m.json": json.dumps(sol)})


# ------------------------------------------------------- paid-API clients

_RUNWARE_CLIENT = '''
def runware(task, payload, out_path, *, poll=10):
    """Submit a Runware task, poll until done, download the artifact."""
    base = os.environ.get("RUNWARE_API_BASE", "https://api.runware.ai/v1")
    key = open(os.path.join(HERE, "apikey")).read().strip()
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    req = urllib.request.Request(f"{base}/{task}", data=json.dumps(payload).encode(),
                                 headers=hdr)
    t = json.load(urllib.request.urlopen(req, timeout=30))
    while True:
        time.sleep(poll)
        st = json.load(urllib.request.urlopen(urllib.request.Request(
            f"{base}/tasks/{t['taskUUID']}", headers=hdr), timeout=30))
        if st["status"] == "completed":
            data = urllib.request.urlopen(st["artifactURL"], timeout=60).read()
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            open(out_path, "wb").write(data)
            return out_path
        if st["status"] == "failed":
            sys.exit(f"{task} failed: {st.get('error')}")
'''

_VIDEOGEN_PY = _py('''
"""videogen — Runware image-to-video. A render takes 1-10 minutes
server-side; this polls until the MP4 lands (run it via run_bg_process)."""
import argparse
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
''' + _RUNWARE_CLIENT + '''

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--image", required=True, action="append")
    p.add_argument("--prompt", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--seconds", type=int, default=5)
    p.add_argument("--model", default="wan")
    p.add_argument("--size", default="1280x720")
    a = p.parse_args()
    for img in a.image:
        if not os.path.exists(img):
            sys.exit(f"no such image: {img}")
    out = runware("imageToVideo", {"model": a.model, "positivePrompt": a.prompt,
                                   "inputImages": a.image, "duration": a.seconds,
                                   "size": a.size}, a.output)
    print(f"rendered {out}")
''')


def plant_videogen(w) -> None:
    w.skill("videogen", (
        "---\nname: videogen\ndescription: Generate short image-to-video clips "
        "from a text prompt plus one or more reference images using the "
        "Runware.ai API, downloading an MP4.\ntrigger: when Bob asks to animate "
        "an image, make a video from a picture, generate a short clip, or turn "
        "a still into motion\n---\n\n## Script\n\n`skills/videogen/videogen.py`"
        "\n\n## IMPORTANT: run asynchronously\n\nVideo generation takes **1–10 "
        "minutes**. NEVER run this with the `bash` tool in a blocking way:\n\n"
        "1. Ack briefly.\n2. `run_bg_process(command=\"python "
        "skills/videogen/videogen.py --image ... --prompt '...' --output "
        "generated-images/<name>.mp4\")`\n3. End your turn; you will be woken "
        "when it finishes.\n\nFlags: `--image` (repeatable), `--prompt`, "
        "`--output`, `--seconds 5`, `--model wan|seedance`, `--size 1280x720`.\n"),
        {"videogen.py": _VIDEOGEN_PY, "apikey": "rw-7f3c19d2e0b84a51\n"})


_MESHY_PY = _py('''
"""bob3d_meshy — image -> GLB via Runware 3dInference (meshy:meshy@6).
PAID (~$0.40/run), 2-6 minutes: run via run_bg_process.
Usage: bob3d_meshy.py <image> <text guidance> [out.glb]"""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
''' + _RUNWARE_CLIENT + '''

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    image, guidance = sys.argv[1], sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else os.path.splitext(image)[0] + ".glb"
    if not os.path.exists(image):
        sys.exit(f"no such image: {image}")
    print(f"mesh written: {runware('3dInference', {'model': 'meshy:meshy@6', 'inputImage': image, 'positivePrompt': guidance}, out)}")
''')


def plant_image_to_3d(w) -> None:
    w.skill("image-to-3d", (
        "---\nname: image-to-3d\ndescription: Generate a 3D mesh (GLB) from an "
        "image via the Runware API (PAID, ~$0.40 per run) then export printable "
        "STL/3MF.\ntrigger: when Bob asks to make a 3D model, figurine, statue, "
        "mesh, or turn a picture into something 3D-printable\n---\n\n"
        "`python3 skills/image-to-3d/bob3d_meshy.py <image> <text guidance> "
        "[out.glb]` — PAID Runware call, 2-6 minutes: run via run_bg_process. "
        "Start from an approved concept image (skills/openai-image).\n"),
        {"bob3d_meshy.py": _MESHY_PY, "apikey": "rw-7f3c19d2e0b84a51\n"})


_OPENAI_IMAGE_PY = _py('''
"""openai_image — GPT Image generation / edits.
Text-to-image:  openai_image.py --prompt "..." --out generated-images/x.png
Image-to-image: openai_image.py --prompt "..." --image ref.png [--image ref2.png] --out ..."""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
KEY = os.environ.get("OPENAI_API_KEY")


def post(path, body, ctype):
    req = urllib.request.Request(f"{BASE}{path}", data=body, headers={
        "Authorization": f"Bearer {KEY}", "Content-Type": ctype})
    try:
        return json.load(urllib.request.urlopen(req, timeout=180))
    except urllib.error.HTTPError as e:
        sys.exit(f"openai-image: HTTP {e.code} {e.read()[:300]!r}")


def multipart(fields, files):
    b = uuid.uuid4().hex
    out = b""
    for k, v in fields.items():
        out += (f"--{b}\\r\\nContent-Disposition: form-data; name=\\"{k}\\"\\r\\n\\r\\n"
                f"{v}\\r\\n").encode()
    for k, path in files:
        out += (f"--{b}\\r\\nContent-Disposition: form-data; name=\\"{k}\\"; "
                f"filename=\\"{os.path.basename(path)}\\"\\r\\nContent-Type: image/png"
                "\\r\\n\\r\\n").encode() + open(path, "rb").read() + b"\\r\\n"
    return out + f"--{b}--\\r\\n".encode(), f"multipart/form-data; boundary={b}"


if __name__ == "__main__":
    if not KEY:
        sys.exit("OPENAI_API_KEY not set")
    p = argparse.ArgumentParser()
    p.add_argument("--prompt", required=True)
    p.add_argument("--image", action="append", default=[])
    p.add_argument("--out", default="generated-images/image.png")
    p.add_argument("--size", default="1024x1024")
    a = p.parse_args()
    if a.image:
        body, ctype = multipart({"model": "gpt-image-1", "prompt": a.prompt,
                                 "size": a.size}, [("image[]", i) for i in a.image])
        r = post("/images/edits", body, ctype)
    else:
        r = post("/images/generations", json.dumps(
            {"model": "gpt-image-1", "prompt": a.prompt, "size": a.size}).encode(),
            "application/json")
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    open(a.out, "wb").write(base64.b64decode(r["data"][0]["b64_json"]))
    print(f"saved {a.out}")
''')


def plant_openai_image(w) -> None:
    w.skill("openai-image", (
        "---\nname: openai-image\ndescription: Generate images via OpenAI GPT "
        "Image, including text-to-image and image-to-image edits with "
        "reference images.\ntrigger: when Bob asks to generate, create, or make "
        "an image, picture, illustration, or edit/stylise an image\n---\n\n"
        "`python3 skills/openai-image/openai_image.py --prompt \"...\" "
        "[--image ref.png ...] --out generated-images/<name>.png` (~20s).\n"
        "Bob's own canon reference images: self/bob/avatar/.\n"),
        {"openai_image.py": _OPENAI_IMAGE_PY})
