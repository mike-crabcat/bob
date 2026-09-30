#!/usr/bin/env python3
"""Intake checks for a model file before slicing for the Bambu Lab X2D.

Reports: per-object bbox + bed fit (X2D 256x256x261, aux nozzle 235.5x256x256),
watertight/manifold check, volume, and down-facing area that will need support
bucketed by z band — the numbers behind an orientation decision.

Usage:
    model_report.py MODEL.stl|MODEL.obj|PROJECT.3mf [--threshold 30] [--json]

stdlib + numpy only (no venv needed). Exit 1 on red flags (not watertight or
doesn't fit the bed).
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np

BED_MAIN = (256.0, 256.0, 261.0)
BED_AUX = (235.5, 256.0, 256.0)
NS3MF = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"


def _f(s: str | None) -> float:
    return float(s or 0.0)


def _i(s: str | None) -> int:
    return int(s or 0)


def load_triangles(path: Path):
    """Return list of (name, tris (N,3,3)) — STL/OBJ single object, 3MF many."""
    suf = path.suffix.lower()
    if suf == ".stl":
        return [(path.stem, _load_stl(path))]
    if suf == ".obj":
        return [(path.stem, _load_obj(path))]
    if suf == ".3mf":
        return _load_3mf(path)
    sys.exit(f"error: unsupported input {path} (want .stl .obj .3mf)")


def _load_stl(path: Path) -> np.ndarray:
    data = path.read_bytes()
    # ascii?
    if data[:5].lower() == b"solid" and b"facet" in data[:512]:
        verts = []
        for line in data.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if parts and parts[0] == "vertex":
                verts.append([float(p) for p in parts[1:4]])
        return np.array(verts, dtype=np.float64).reshape(-1, 3, 3)
    n = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + n * 50
    if len(data) < expected:
        sys.exit("error: truncated binary STL")
    rec = np.frombuffer(data, dtype=np.uint8, count=n * 50, offset=84)
    rec = rec.reshape(-1, 50)[:, 12:48].copy()
    tris = rec.view("<f4").reshape(-1, 3, 3).astype(np.float64)
    return tris


def _load_obj(path: Path) -> np.ndarray:
    verts, faces = [], []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v":
            verts.append([float(p) for p in parts[1:4]])
        elif parts[0] == "f":
            idx = [int(p.split("/")[0]) - 1 for p in parts[1:4]]
            faces.append(idx)
    v = np.array(verts, dtype=np.float64)
    return v[np.array(faces, dtype=np.int64)]


def _mat4(transform: str | None) -> np.ndarray:
    """3MF 12-number transform -> 4x4 row-vector matrix ([p 1] @ M).
    Spec order: m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32."""
    m = np.eye(4)
    if transform:
        vals = np.array([float(x) for x in transform.split()], dtype=np.float64)
        m[0, :3], m[1, :3], m[2, :3] = vals[0:3], vals[3:6], vals[6:9]
        m[3, :3] = vals[9:12]
    return m


def _load_3mf(path: Path):
    """Resolve meshes through the build-item -> components chain with transforms."""
    objects: dict[str, dict] = {}
    build_items = []  # (objectid, mat4)
    with zipfile.ZipFile(path) as z:
        for mn in (n for n in z.namelist() if n.endswith(".model")):
            root = ET.fromstring(z.read(mn))
            for obj in root.iter(f"{NS3MF}object"):
                oid = obj.get("id")
                if oid is None:
                    continue
                verts, tris, comps = [], [], []
                for v in obj.iter(f"{NS3MF}vertex"):
                    verts.append([_f(v.get("x")), _f(v.get("y")),
                                  _f(v.get("z"))])
                for t in obj.iter(f"{NS3MF}triangle"):
                    tris.append([_i(t.get("v1")), _i(t.get("v2")),
                                 _i(t.get("v3"))])
                for c in obj.iter(f"{NS3MF}component"):
                    comps.append((c.get("objectid"), _mat4(c.get("transform"))))
                objects[oid] = dict(
                    name=obj.get("name") or f"object_{obj.get('id')}",
                    verts=np.array(verts, dtype=np.float64) if verts else None,
                    tris=np.array(tris, dtype=np.int64) if tris else None,
                    components=comps)
            for item in root.iter(f"{NS3MF}item"):
                build_items.append((item.get("objectid"), _mat4(item.get("transform"))))

    if not build_items:
        build_items = [(oid, np.eye(4)) for oid in objects if objects[oid]["tris"] is not None]

    out = []

    def emit(oid: str, mat: np.ndarray, tag: str):
        obj = objects.get(oid)
        if obj is None:
            return
        if obj["tris"] is not None:
            v = obj["verts"]
            vh = np.hstack([v, np.ones((len(v), 1))]) @ mat
            name = obj["name"] + tag
            out.append((name, vh[:, :3][obj["tris"]]))
        for child_oid, child_mat in obj["components"]:
            emit(child_oid, mat @ child_mat, tag)

    for i, (oid, mat) in enumerate(build_items):
        emit(oid, mat, f"[{i}]" if len(build_items) > 1 else "")
    if not out:
        sys.exit("error: no meshes found in 3mf")
    return out


def mesh_stats(tris: np.ndarray):
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    n = np.cross(b - a, c - a)
    areas = np.linalg.norm(n, axis=1) / 2.0
    unit = n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-30)
    volume = abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0
    # weld vertices for manifold check
    flat = tris.reshape(-1, 3)
    _, inv = np.unique(flat, axis=0, return_inverse=True)
    f = inv.reshape(-1, 3)
    edges = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    edges = np.sort(edges, axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    open_edges = int((counts == 1).sum())
    nonmanifold = int((counts > 2).sum())
    degenerate = int((areas < 1e-12).sum())
    return dict(volume=volume, area=float(areas.sum()), open_edges=open_edges,
                nonmanifold_edges=nonmanifold, degenerate=degenerate,
                nz=unit[:, 2], areas=areas)


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or __file__).splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--threshold", type=float, default=30.0,
                    help="support threshold angle in degrees from horizontal "
                         "(surfaces below this need support; default 30)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    path = Path(args.file).expanduser()
    objects = load_triangles(path)
    flags = []
    report = {"file": str(path), "objects": []}

    all_min, all_max = np.array([np.inf] * 3), np.array([-np.inf] * 3)
    for name, tris in objects:
        st = mesh_stats(tris)
        mn, mx = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
        dims = mx - mn
        # down-facing area below threshold: nz<0 and angle-from-horizontal
        # = degrees(acos(-nz)) < threshold  (0 = flat ceiling, 90 = wall)
        down = st["nz"] < -1e-9
        ang = np.degrees(np.arccos(np.clip(-st["nz"][down], -1, 1)))
        needs = down.copy()
        needs[np.where(down)[0]] = ang < args.threshold
        need_area = float(st["areas"][needs].sum())
        obj = {
            "name": name, "triangles": int(len(tris)),
            "bbox_min": mn.round(2).tolist(), "bbox_max": mx.round(2).tolist(),
            "dims_mm": dims.round(2).tolist(),
            "volume_mm3": round(st["volume"], 1),
            "open_edges": st["open_edges"], "nonmanifold_edges": st["nonmanifold_edges"],
            "degenerate_triangles": st["degenerate"],
            "downface_area_needing_support_mm2": round(need_area, 1),
        }
        # z bands of support need (10mm bands, absolute z)
        tri_z = tris.mean(axis=1)[:, 2]
        sel = needs
        if sel.any():
            bands = {}
            for z0 in range(int(np.floor(tri_z[sel].min())),
                            int(np.ceil(tri_z.max())) + 1, 10):
                m = sel & (tri_z >= z0) & (tri_z < z0 + 10)
                a = float(st["areas"][m].sum())
                if a > 1.0:
                    bands[f"{z0}-{z0+10}"] = round(a, 0)
            obj["support_area_by_z_band_mm2"] = bands
        if st["open_edges"] > 0 or st["nonmanifold_edges"] > 0:
            flags.append(f"{name}: not watertight "
                         f"({st['open_edges']} open, {st['nonmanifold_edges']} "
                         f"non-manifold edges) — slicer may mis-heal it")
        all_min = np.minimum(all_min, mn)
        all_max = np.maximum(all_max, mx)
        report["objects"].append(obj)

    dims = all_max - all_min
    fits_main = dims[0] <= BED_MAIN[0] and dims[1] <= BED_MAIN[1] and dims[2] <= BED_MAIN[2]
    fits_aux = dims[0] <= BED_AUX[0] and dims[1] <= BED_AUX[1] and dims[2] <= BED_AUX[2]
    report["combined_dims_mm"] = dims.round(2).tolist()
    report["fits_x2d_main"] = bool(fits_main)
    report["fits_x2d_aux"] = bool(fits_aux)
    if not fits_main:
        flags.append(f"combined size {dims[0]:.0f}x{dims[1]:.0f}x{dims[2]:.0f} mm "
                     f"exceeds X2D main nozzle {BED_MAIN[0]:.0f}x{BED_MAIN[1]:.0f}x"
                     f"{BED_MAIN[2]:.0f}")
    elif not fits_aux:
        report["note"] = ("fits main nozzle only — keep it clear of the x<20.5mm "
                          "aux-nozzle strip when printing dual-material")

    if args.json:
        print(json.dumps(report, indent=1))
    else:
        for o in report["objects"]:
            d = o["dims_mm"]
            print(f"{o['name']}: {d[0]:.1f} x {d[1]:.1f} x {d[2]:.1f} mm, "
                  f"{o['triangles']} tris, {o['volume_mm3']/1000:.1f} cm3")
            print(f"  watertight: {'YES' if o['open_edges']==0 and o['nonmanifold_edges']==0 else 'NO'}"
                  f"   support-needing downface: "
                  f"{o['downface_area_needing_support_mm2']:.0f} mm2")
            for band, area in o.get("support_area_by_z_band_mm2", {}).items():
                print(f"    z {band}mm: {area:.0f} mm2")
        print(f"combined: {dims[0]:.1f} x {dims[1]:.1f} x {dims[2]:.1f} mm — "
              f"X2D main {'FITS' if fits_main else 'DOES NOT FIT'}"
              f"{'' if fits_aux else ' (main-nozzle area only)'}")

    if flags:
        print("\nRED FLAGS:")
        for f in flags:
            print(f"  !! {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
