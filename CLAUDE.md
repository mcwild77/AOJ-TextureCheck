# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python 3.10+ and Tkinter (ships with Python). Runtime dependencies (see `requirements.txt`): Pillow (texture reading/preview), and for the 3D cabinet preview numpy, trimesh (GLB loading) and moderngl (GPU rendering). The checker/resize logic needs only Pillow; the extra three are the 3D preview's cost.

```
pip install -r requirements.txt
python run.py                                                         # launch the GUI
python -m unittest discover -s tests -t . -v                          # all tests (stdlib unittest, no pytest)
python -m unittest tests.test_texturecheck.RulesTest.test_suggested_size   # a single test
build_windows.bat                                                     # Windows only: builds dist\TextureChecker.exe via PyInstaller
```

On macOS use `python3` and `python3 -m pip`, since `python` and `pip` are often not on PATH. If pip reports an externally-managed environment, use a venv (`python3 -m venv .venv`).

`build_windows.bat` builds inside a private venv (`buildenv`) so only `requirements.txt` is bundled. Building from the main Python instead drags in whatever trimesh can optionally import (OpenCV, scipy, pandas...) and gave a 153 MB exe versus ~29 MB. Because the build venv has no scipy/networkx, don't rely on trimesh features that need them (e.g. `mesh.split()`); `preview3d._connected_vertex_sets` exists for exactly that reason. PyInstaller does not cross-compile, so the exe must be built on Windows. The script keeps CRLF line endings via `.gitattributes`.

## Purpose

A texture checker for user-generated arcade cabinets in **Age of Joy**, a VR arcade collection. It validates cabinet textures against the powers-of-two sizing standard and can resize non-compliant textures. The standard is in [docs/texture-standards.md](docs/texture-standards.md). Its upstream source is the Age of Joy "Cabinet building best practices" page, which the user authored, so treat the doc as the source of truth for rules and thresholds.

## Constraints that drive design decisions

- **Users are cabinet builders who are not very technical.** The tool must be easy to use, and error messages must say what is wrong and how to fix it, not just report a failure.
- **Runs on Windows with as few dependencies as possible.** Ideally a single double-clickable executable with no runtime or installer required. Development is happening on macOS, so the stack needs to be developable and testable here even though the target is Windows. The 3D preview (trimesh + moderngl) is a deliberate exception the user signed off on; it must degrade gracefully so the checker still works where 3D does not, and its Windows/PyInstaller packaging has been verified (frozen exe checks, loads and renders all sample cabinets).
- **A GUI is required.**
- **Resizing is likely required**, not just reporting. Because resizing modifies user art, never overwrite originals silently. (The resize UI is currently shelved in the GUI — see Architecture.)

## Cabinet format (what the tool ingests)

Cabinets are distributed as `.zip` files, and `Cabinets/` holds two deliberately bad samples. Each zip is flat (no subfolders) and contains:

- `description.yaml`: the cabinet definition. Under `parts:`, each part can have `art: { file: <name>.png }`, which is the texture applied to that part. The same PNG may be referenced by several parts.
- `metadata.yaml`: model hash and size bookkeeping.
- One or more `.glb` models, an `.mp4` game video, `.bas` scripts, and the texture `.png` files.
- `<video>.mp4.png`: a video thumbnail, not a cabinet texture. Its dimensions (e.g. 498x380) do not follow the standard and it should not be flagged.

Things learned from the samples that affect the checker:

- `description.yaml` is not reliable. In `virtua cop 2.zip` it references `hotd.glb` and `hotd.mp4`, but the zip contains `hotdgun.glb`, `vc2.glb` and `vcop2.mp4`. In `Ridge Racer (SIT DOWN).zip` it references `num.png`, which is not in the zip. The tool should tolerate missing or mismatched referenced files and must not crash on them.
- Not every PNG in a zip is referenced by `description.yaml`, and textures can also be embedded inside the `.glb` models. The tool checks every loose PNG/JPEG except `*.mp4.png`, and flags the ones `description.yaml` does not use as an info note. The `parts:` entries map a part name to `art: { file: <name>.png }`; those part names match the GLB scene-graph node names, and the assigned texture overrides whatever is baked into that mesh (this is what the 3D preview renders).
- The samples include a mix of failures: non-power-of-two sizes (e.g. 773x262, 1698x1412), oversized textures (2048x2303, 3000x1026) and oversized flat-color buttons. Use them as the first regression fixtures.
- PNG dimensions can be read on macOS with `sips -g pixelWidth -g pixelHeight <file>`, which is useful for sanity-checking whatever the tool reports.

## Architecture

Logic is kept out of the GUI so it can be tested without Tk:

- `texturecheck/rules.py`: pure functions and constants for the standard (power-of-two test, 2048 cap, nearest-power-of-two suggestion, `Issue` severities). This is the only place thresholds live, and it must match [docs/texture-standards.md](docs/texture-standards.md).
- `texturecheck/cabinet.py`: opens a cabinet zip, skips `*.mp4.png` thumbnails, reads each texture's dimensions with Pillow, and returns `TextureReport`s sorted worst-first. It finds `description.yaml` references by regex rather than a YAML parser, to avoid a dependency and survive malformed files. Corrupt images become an error row, never an exception. `TextureReport` also carries `file_bytes` (compressed on-disk size) and `has_alpha`, and exposes `ingame_bytes` (uncompressed GPU footprint: w*h*3, or *4 with alpha). `part_art_map()` parses the `parts:` → `art.file` assignments (also regex, no YAML dep) for the 3D preview.
- `texturecheck/resize.py`: re-encodes textures and duplicates a cabinet. `export_folder`/`export_zip` copy a whole cabinet (from a zip **or** a folder, via `sources.open_source`) into a new folder or zip, re-encoding only the textures named in the caller's `targets` map and copying everything else unchanged; both are nondestructive and refuse to write over the source (`export_folder` also refuses an existing destination). The older `write_resized_cabinet` (zip→zip, driven by `targets_for`) remains. Resizing intentionally does not preserve aspect ratio, because UV mapping stretches the texture across the model anyway.
- `texturecheck/sources.py`: reads a cabinet from a `.zip` or a plain folder behind one `namelist()`/`read()` interface (`open_source`), so the checker, preview and export all work with either. `default_export_name()` gives the `<cabinet>_optimized` default the GUI's name field seeds.
- `texturecheck/preview3d.py`: the 3D cabinet preview. `build_model(zip)` is pure CPU (trimesh loads the GLB, Pillow resolves textures) and is safe on a worker thread; it picks the cabinet GLB by which one's scene-graph node names best match the yaml part names (since `model.file` is unreliable) and bakes the yaml `art` overrides onto the matching nodes, embedded GLB textures being the fallback. `Renderer` owns an offscreen moderngl GL context and **must be created/used only on the main thread** (GL is single-threaded); frames are ~1-2ms so the GUI renders synchronously on drag. Chosen over pyrender, which is unmaintained and broke against current numpy/PyOpenGL. `build_model` also runs the **custom-screen 4:3 check** and attaches a `ScreenCheck` to the model: for a `crt: type: custom` cabinet (built-in types like `19i` use the engine's own screen and are skipped), it measures the named `mesh:` node's aspect via PCA per connected component — split first, because a TWIN packs both screen quads into one node — and flags anything off 4:3 (the tolerance lives in `rules.py`). Because it uses trimesh it degrades with the rest of the 3D preview when the dep is absent.
- `texturecheck/gui.py`: Tkinter layer. Top bar with the open buttons on the left; on the right, per-cabinet totals plus a **Save as** name field (seeded with `sources.default_export_name`) and two nondestructive export buttons, **Export to New Folder** / **Export to Zip** — each duplicates the cabinet with the user's chosen resizes applied (`_export_targets` collects every texture whose effective size differs from its original). Below the byte totals, a red/bold line ("Warning: Custom Screentype may not be 4:3 aspect ratio!") appears when the loaded cabinet's `ScreenCheck` failed, and is blank otherwise (filled in once the async 3D-model load finishes, so it needs the trimesh path). A vertical split with the 3D preview (drag to rotate) and the selected-texture image on top, the results table below. Starts maximized and grabs focus. The older standalone **resize button is shelved** (created so `_populate` can still toggle it, but not packed) pending a later pass.
- `run.py`: the launcher for both `python run.py` and PyInstaller. `texturecheck/__main__.py` uses relative imports, so it cannot be the frozen entry point.

Tests in `tests/` build synthetic zips with Pillow. `SampleCabinetsTest` also runs against the real zips in `Cabinets/` and skips if they are missing.

## Working notes

- Extract samples into a scratch directory rather than into `Cabinets/`, so the sample zips stay pristine.
- Known gaps: textures embedded inside `.glb` models are not inspected by the checker (only loose PNG/JPEG files in the zip) — though the 3D preview does render them as the fallback; and `.astc` files, which the standards say must never be used, are not flagged.
- Verified headlessly (unit tests, and preview3d rendering both sample cabinets to PNG), but the live GUI window has **not** been launched/driven by hand this session: the file dialogs, the drag-to-rotate 3D interaction, focus-grab, and the (shelved) resize flow are all unexercised interactively.
- 3D preview `build_model()` matches yaml parts to GLB nodes case-insensitively by exact name; `model.matched` / `model.unmatched` record which yaml assignments landed. Surfacing that in the GUI (and click-a-texture-to-spotlight-its-part) are natural next steps.
- The moderngl GPU path replaced an earlier pure-numpy software rasterizer that was ~1000x slower (a per-triangle Python loop); don't reintroduce that or pyrender.
