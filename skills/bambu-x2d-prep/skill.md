---
name: bambu-x2d-prep
description: Make ready-to-print files for the Bambu Lab X2D — intake checks, orientation, supports, headless slicing with Bambu Studio CLI, and the mandatory multi-view visual inspection gate before anything is called print-ready.
trigger: when asked to prepare/slice/print a model on the X2D ("print this", "make this printable", "prep this for the printer", "add supports"), or when about to send any 3mf/gcode to the X2D
---

# bambu-x2d-prep

Machine facts (verified on this box 2026-09-28, Bambu Studio v02.08.02.61):
Bambu Lab X2D — 256×256×261 mm main nozzle; aux/dual nozzle area 235.5×256×256;
dual 0.4 nozzles (0.2/0.6/0.8 profiles also shipped). Deliverable is a sliced
project **3mf** (plate gcode embedded) — the file Bambu Handy/the printer wants.

## The rule

A file is ready to print ONLY after you have LOOKED at renders of the actual
sliced toolpaths — top, front, back, left, right, both isos — **from this
run's slice**, and every view passes. "Sliced without errors" is not ready:
the slicer happily slices floating regions into spaghetti, and that failure
is visible in the renders and nowhere else.

## Pipeline

0. **One-time setup**: `bash skills/bamboo-x2d-prep/scripts/bootstrap.sh`
   (render venv + Bambu Studio AppImage into workspace/tools/bambu-studio).

1. **Intake** — `python3 skills/bamboo-x2d-prep/scripts/model_report.py MODEL.stl|.obj|.3mf`
   (system python, no venv). Gives dims + X2D bed fit, watertight check, and
   support-needing downface area per z-band — the input for orientation.
   - SCALE: STL has no units. If any real-world dimension is known, verify it
     here BEFORE slicing. A 10× part is the classic silent failure.
   - Small non-manifold counts on unions of overlapping solids are fine (the
     slicer unions them); many OPEN edges = broken source, fix or reject.

2. **Orient + supports** — copy `presets/process_supported.json`, edit the copy.
   It is the verified base (0.20mm Standard @BBL X2D, auto supports on,
   threshold 30°, brim 5mm). Levers:
   - `support_type` MUST be `normal(auto)` or `tree(auto)` — plain `"normal"`
     silently means *manual* (zero supports). This already bit us once.
   - tree(auto) for organic shapes (less scarring); normal(auto) for flat ceilings.
   - Orientation usually beats supports: flip so the report's big downface
     bands land on the bed. Load-bearing parts: keep stress along, not across,
     layer lines.
   - Per-print: layer height (start from another stock preset name in
     resources/profiles/BBL/process/), `brim_width`, infill density/pattern.

3. **Slice** (1–3 min → run_bg_process, not inline bash):
   ```
   TOOLS=workspace/tools/bambu-studio
   $TOOLS/squashfs-root/AppRun --datadir $TOOLS/data MODEL.stl \
     --load-settings "$TOOLS/squashfs-root/resources/profiles/BBL/machine/Bambu Lab X2D 0.4 nozzle.json;<your preset>" \
     --load-filaments "$TOOLS/squashfs-root/resources/profiles/BBL/filament/Bambu PLA Basic @BBL X2D 0.4 nozzle.json" \
     --arrange 1 --ensure-on-bed --slice 0 \
     --export-3mf NAME.3mf --outputdir OUTDIR
   ```
   - `--export-3mf` takes a BARE filename — outputdir is prepended to absolute
     paths and the export fails (verified bug).
   - Grep the log for `NON_CRITICAL slicing warnings` — "floating regions"
     means something got no support; fix, don't ship.
   - Multi-material/AMS: `--load-filaments "a.json;b.json"` + `--load-filament-ids "1,2"`;
     parts using the aux nozzle must fit the 235.5×256×256 area.
   - Nonzero exit + `result.json` `return_code` is the machine verdict; read it.

4. **Render** — `skills/bamboo-x2d-prep/.venv/bin/python skills/bambu-x2d-prep/scripts/render_views.py OUTDIR/NAME.3mf -o OUTDIR/views`
   (accepts 3mf or raw plate gcode). Draws the real toolpaths per view, zoomed
   to the part, with the bed outline. Legend: blue walls, gray sparse infill,
   green solid/top, yellow bridge, **orange support**, red skirt/brim.
   Exit 1 = automatic red flag (bed overflow, or overhang/floating features
   printed with no supports at all).

5. **Inspect every view** (vision, one by one — this is the gate):
   - every ceiling/overhang sits on orange support, is a short yellow bridge,
     or doesn't exist — nothing extrudes over air
   - support isn't wasted on faces that didn't need it (scarring on show faces)
   - footprint + brim/skirt sane for the part size; nothing clipped by the
     bed outline; nothing parked in the aux strip (dual-material prints)
   - scale spot-check: a known feature measures right in the render
   - the shape is what the requester described — mirrored text or a rotated
     part shows up HERE, not in the logs
6. **Fail loop**: a view fails → change ONE thing (orientation / support
   params / brim) → re-slice → re-render → re-inspect all views again. Never
   declare from stale renders or from memory of the previous run.
7. **Deliver**: `OUTDIR/NAME.3mf` + the `views/` PNGs (they are the evidence).
   Big files (>~20MB) go via the share-files skill. Report material, layer
   height, support type, est. time (gcode header "model printing time"),
   filament length (header) — grams ≈ mm × 2.405 × density_g_cm3 / 1000.

## Verified gotchas

- Studio's own `--export-png` / thumbnails need a display (glfw/Wayland fail
  headless on this box). The gcode renderer IS the view source — don't wait
  on Studio PNGs.
- Preset JSONs must keep their metadata (`"from": "system"` etc.) or the CLI
  rejects them with `from unsupported`.
- Machine/process/filament presets live under
  `$TOOLS/squashfs-root/resources/profiles/BBL/{machine,process,filament}/` —
  semicolon-separated absolute paths in `--load-settings`/`--load-filaments`.
- `--datadir` keeps Studio's config inside the workspace (sandbox-clean);
  first run writes ~100MB there.
- Bambu gcode is M83 relative-E with G2/G3 arcs and `; FEATURE:` markers —
  the renderer handles all of it; don't hand-parse.
- Bed origin is 0..256 (not centred); the renderer draws it so you can see
  arrange position at a glance.
