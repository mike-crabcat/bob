#!/usr/bin/env python3
"""Render multi-view PNGs of a sliced print (gcode or Bambu sliced 3mf).

Every view is drawn from the ACTUAL extrusion toolpaths, so supports, brim,
skirt and floating-region failures are all visible — this is what the plate
will really look like, not a preview of the model mesh.

Usage:
    render_views.py PLATE.gcode|PROJECT.3mf [-o OUTDIR] [--views ...] [--bed ...]

Input may be a .gcode file or a sliced .3mf (the largest Metadata/plate_N.gcode
inside is used). Requires numpy + matplotlib (see bootstrap.sh).

Output: one PNG per view plus a text summary on stdout. Exit code 1 if the
summary finds a red flag (bed overflow / suspected unsupported overhang).
"""

from __future__ import annotations

import argparse
import math
import sys
import zipfile
from pathlib import Path

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

# X2D geometry (from Bambu Studio v02.08.02.61 machine profile "Bambu Lab X2D 0.4 nozzle")
BEDS = {
    "x2d": dict(name="Bambu Lab X2D main nozzle", x=256.0, y=256.0, z=261.0, aux_x=235.5),
    "x2d-aux": dict(name="Bambu Lab X2D aux/dual nozzle", x=235.5, y=256.0, z=256.0),
    "none": None,
}

# ; FEATURE: <name>  ->  colour (supports read hot so they jump out)
FEATURE_COLORS = {
    "Outer wall": "#4da3ff",
    "Inner wall": "#2f6fd0",
    "Sparse infill": "#77839a",
    "Internal solid infill": "#35b06a",
    "Top surface": "#7de0a3",
    "Bottom surface": "#cfd6df",
    "Bridge": "#e6c84a",
    "Overhang wall": "#e08a3c",
    "Floating vertical shell": "#b0b0ff",
    "Support": "#ff8c1a",
    "Support interface": "#ff4d00",
    "Support transition": "#c76a00",
    "Skirt": "#e04a4a",
    "Brim": "#e04a4a",
    "Prime tower": "#b06adf",
    "Custom": "#8f8f8f",
}
DEFAULT_COLOR = "#9aa0a6"

# name -> (azimuth deg, elevation deg); py is always "up" for side views
VIEWS = {
    "top": (-90, 90),
    "front": (-90, 0),
    "back": (90, 0),
    "left": (180, 0),
    "right": (0, 0),
    "iso": (-60, 32),
    "iso2": (120, 32),
}
DEFAULT_VIEWS = "top,front,back,left,right,iso,iso2"

# features that print over air unless supported
RISKY_FEATURES = {"Overhang wall", "Floating vertical shell", "Bridge"}
SUPPORT_FEATURES = {"Support", "Support interface", "Support transition"}
ARC_CHORD = 0.8  # mm max chord when expanding G2/G3 arcs


def load_gcode_bytes(path: Path) -> tuple[bytes, str]:
    """Return (gcode bytes, stem) from a .gcode file or sliced .3mf."""
    if path.suffix.lower() == ".3mf":
        with zipfile.ZipFile(path) as z:
            plates = [n for n in z.namelist()
                      if n.startswith("Metadata/plate_") and n.endswith(".gcode")]
            if not plates:
                sys.exit(f"error: {path} is a 3mf with no embedded plate gcode (not sliced?)")
            # largest plate = the real one
            pick = max(plates, key=lambda n: z.getinfo(n).file_size)
            return z.read(pick), f"{path.stem}_{Path(pick).stem}"
    return path.read_bytes(), path.stem


def parse_gcode(data: bytes):
    """Parse gcode into {feature: np.ndarray of segments (N,2,3)} and stats."""
    rel_e = True
    x = y = z = 0.0
    cur_e = 0.0
    feature = "Unknown"
    segs: dict[str, list] = {}
    zs = set()

    def add(feat, x0, y0, z0, x1, y1, z1):
        segs.setdefault(feat, []).append((x0, y0, z0, x1, y1, z1))

    for raw in data.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            if line.startswith("; FEATURE:"):
                feature = line.split(":", 1)[1].strip() or "Unknown"
            continue
        parts = line.split()
        cmd = parts[0]
        if cmd in ("G0", "G1", "G2", "G3"):
            params = {}
            for p in parts[1:]:
                if len(p) >= 2 and p[0] in "XYZEFP IJ".replace(" ", ""):
                    try:
                        params[p[0]] = float(p[1:])
                    except ValueError:
                        pass
            nx, ny, nz = params.get("X", x), params.get("Y", y), params.get("Z", z)
            de = 0.0
            if "E" in params:
                if rel_e:
                    de = params["E"]
                else:
                    de = params["E"] - cur_e
                    cur_e = params["E"]
            extruding = de > 1e-5
            if cmd in ("G2", "G3") and extruding:
                # arc: expand from I/J centre (G17 XY plane)
                if "I" in params or "J" in params:
                    cx, cy = x + params.get("I", 0.0), y + params.get("J", 0.0)
                    r = math.hypot(x - cx, y - cy)
                    a0 = math.atan2(y - cy, x - cx)
                    a1 = math.atan2(ny - cy, nx - cx)
                    full = "P" in params and params.get("P", 0) >= 1
                    sweep = 2 * math.pi if full else None
                    if sweep is None:
                        if cmd == "G3":  # CCW
                            sweep = (a1 - a0) % (2 * math.pi)
                        else:  # G2 CW
                            sweep = -((a0 - a1) % (2 * math.pi))
                    n = max(2, int(abs(sweep) * r / ARC_CHORD) + 1)
                    px, py, pz = x, y, z
                    for i in range(1, n + 1):
                        t = i / n
                        a = a0 + sweep * t
                        ax = cx + r * math.cos(a)
                        ay = cy + r * math.sin(a)
                        az = z + (nz - z) * t
                        add(feature, px, py, pz, ax, ay, az)
                        px, py, pz = ax, ay, az
                else:
                    add(feature, x, y, z, nx, ny, nz)
            elif extruding and (abs(nx - x) > 1e-6 or abs(ny - y) > 1e-6 or abs(nz - z) > 1e-6):
                add(feature, x, y, z, nx, ny, nz)
            if abs(nz - z) > 1e-9:
                zs.add(round(nz, 3))
            x, y, z = nx, ny, nz
        elif cmd == "M83":
            rel_e = True
        elif cmd == "M82":
            rel_e = False
        elif cmd == "G92":
            for p in parts[1:]:
                if p.startswith("E"):
                    try:
                        cur_e = float(p[1:])
                    except ValueError:
                        pass
                elif p.startswith("X"):
                    x = float(p[1:])
                elif p.startswith("Y"):
                    y = float(p[1:])
                elif p.startswith("Z"):
                    z = float(p[1:])
        elif cmd == "G90":
            pass  # absolute XYZ (only mode Bambu emits)
    return {k: np.array(v, dtype=np.float64).reshape(-1, 6) for k, v in segs.items()}, zs


def basis(az_deg: float, el_deg: float):
    az, el = math.radians(az_deg), math.radians(el_deg)
    u = np.array([-math.sin(az), math.cos(az), 0.0])
    v = np.array([-math.sin(el) * math.cos(az), -math.sin(el) * math.sin(az), math.cos(el)])
    return u, v


def project(segs: np.ndarray, u, v) -> np.ndarray:
    """(N,2,3) xyz segments -> (N,2,2) projected."""
    pts = segs.reshape(-1, 3)
    px = pts @ u
    py = pts @ v
    return np.stack([px, py], axis=1).reshape(-1, 2, 2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", help=".gcode or sliced .3mf")
    ap.add_argument("-o", "--outdir", default=None, help="output dir (default: alongside input)")
    ap.add_argument("--views", default=DEFAULT_VIEWS,
                    help=f"comma-separated from: {','.join(VIEWS)}")
    ap.add_argument("--bed", default="x2d", choices=list(BEDS),
                    help="bed outline to draw (default x2d)")
    ap.add_argument("--max-segments", type=int, default=900_000,
                    help="decimate above this many total segments")
    ap.add_argument("--dpi", type=int, default=140)
    args = ap.parse_args()

    src = Path(args.file).expanduser()
    data, stem = load_gcode_bytes(src)
    seg_by_feat, zs = parse_gcode(data)
    total = sum(len(v) for v in seg_by_feat.values())
    if total == 0:
        sys.exit("error: no extrusion segments parsed — is this a Bambu gcode/3mf?")

    # decimate if huge
    if total > args.max_segments:
        keep = args.max_segments / total
        for k, v in list(seg_by_feat.items()):
            idx = np.random.default_rng(42).random(len(v)) < keep
            seg_by_feat[k] = v[idx]
        print(f"note: decimated to <= {args.max_segments} segments for rendering")

    outdir = Path(args.outdir).expanduser() if args.outdir else src.parent / "views"
    outdir.mkdir(parents=True, exist_ok=True)

    everything = np.concatenate(list(seg_by_feat.values()))
    allpts = everything.reshape(-1, 3)
    mn, mx = allpts.min(axis=0), allpts.max(axis=0)
    bed = BEDS[args.bed]

    # summary -------------------------------------------------------------
    flags = []
    feats = set(seg_by_feat)
    print(f"== {stem} ==")
    print(f"layers: {len(zs)}   z range: {mn[2]:.1f}..{mx[2]:.1f} mm")
    print(f"plate bbox: X {mn[0]:.1f}..{mx[0]:.1f}  Y {mn[1]:.1f}..{mx[1]:.1f}  "
          f"Z {mn[2]:.1f}..{mx[2]:.1f} mm   ({total} segments)")
    if bed:
        over_x = mx[0] > bed["x"] or mn[0] < 0
        over_y = mx[1] > bed["y"] or mn[1] < 0
        over_z = mx[2] > bed["z"]
        if over_x or over_y or over_z:
            flags.append(f"EXCEEDS {bed['name']} build volume "
                         f"({bed['x']:.0f}x{bed['y']:.0f}x{bed['z']:.0f})")
    for f in sorted(feats, key=lambda k: -len(seg_by_feat[k])):
        print(f"  {f:<24} {len(seg_by_feat[f]):>7} segments")
    if (feats & RISKY_FEATURES) and not (feats & SUPPORT_FEATURES):
        flags.append("overhang/floating features printed but NO Support features — "
                     "expect sagging or spaghetti in mid-air")
    print("views written:")
    for name in [v.strip() for v in args.views.split(",") if v.strip()]:
        u, v = basis(*VIEWS[name])
        fig, ax = plt.subplots(figsize=(7.2, 7.2), facecolor="#101317")
        ax.set_facecolor("#101317")
        # draw in z order: infill first, supports on top of nothing but under walls? keep type order
        for feat in sorted(feats, key=lambda k: -len(seg_by_feat[k])):
            seg2d = project(seg_by_feat[feat], u, v)
            lc = LineCollection(
                seg2d, colors=FEATURE_COLORS.get(feat, DEFAULT_COLOR),
                linewidths=0.42, alpha=0.95, rasterized=True)
            ax.add_collection(lc)
        if bed and name == "top":
            ax.add_patch(Rectangle((0, 0), bed["x"], bed["y"], fill=False,
                                   ec="#39424e", lw=1.0, ls="--"))
            if "aux_x" in bed:
                ax.plot([bed["aux_x"]] * 2, [0, bed["y"]], color="#5a4a2a", lw=0.8, ls=":")
        elif bed:
            w = bed["x"] if name in ("front", "back") else bed["y"]
            ax.add_patch(Rectangle((0, 0), w, bed["z"], fill=False,
                                   ec="#39424e", lw=1.0, ls="--"))
            if name in ("front", "back") and "aux_x" in bed:
                ax.plot([bed["aux_x"]] * 2, [0, bed["z"]], color="#5a4a2a", lw=0.8, ls=":")
        # zoom to the part (bed outline clips); keep a small margin
        pmin = (allpts @ np.stack([u, v], axis=1)).min(axis=0)
        pmax = (allpts @ np.stack([u, v], axis=1)).max(axis=0)
        m = max((pmax - pmin).max() * 0.09, 2.0)
        ax.set_xlim(pmin[0] - m, pmax[0] + m)
        ax.set_ylim(pmin[1] - m, pmax[1] + m)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])
        for s in ax.spines.values():
            s.set_color("#2a3038")
        fig.text(0.01, 0.005,
                 "■ walls  ■ infill  ■ solid/top  ■ support  ■ skirt/brim"
                 "   (dotted line = aux-nozzle 235.5 mm limit)",
                 color="#6f7a87", fontsize=6.5)
        ax.set_title(f"{stem} — {name}\n"
                     f"part {mx[0]-mn[0]:.0f}x{mx[1]-mn[1]:.0f}x{mx[2]-mn[2]:.0f} mm "
                     f"at ({mn[0]:.0f},{mn[1]:.0f}) on bed "
                     f"{(bed or {}).get('x', 0):.0f}x{(bed or {}).get('y', 0):.0f}x"
                     f"{(bed or {}).get('z', 0):.0f}",
                     color="#c8d0da", fontsize=10)
        fig.tight_layout()
        out = outdir / f"{stem}_{name}.png"
        fig.savefig(out, dpi=args.dpi, facecolor=fig.get_facecolor())
        plt.close(fig)
        print(f"  {out}")

    if flags:
        print("\nRED FLAGS:")
        for f in flags:
            print(f"  !! {f}")
        return 1
    print("\nno automatic red flags — now LOOK at every PNG before calling it ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
