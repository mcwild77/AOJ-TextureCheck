"""Write a resized copy of a cabinet zip. The original zip is never modified."""

import io
import zipfile
from pathlib import Path

from PIL import Image

from . import sources
from .cabinet import TextureReport


def default_output_path(zip_path: str) -> Path:
    p = Path(zip_path)
    return p.with_name(f"{p.stem} (resized){p.suffix}")


def resize_image(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Resize a PIL image to exactly `size` with Lanczos, preserving RGB vs RGBA.

    Aspect ratio is deliberately not kept (UVs stretch the texture 0..1 either way).
    Lanczos is Pillow's highest-quality resampler. Note: this does not yet do
    premultiplied-alpha handling, so RGBA edges can bleed slightly -- fine for the
    preview; revisit when we write files.
    """
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    mode = "RGBA" if has_alpha else "RGB"
    if img.mode != mode:
        img = img.convert(mode)
    return img.resize(size, Image.Resampling.LANCZOS)


def resize_image_bytes(data: bytes, size: tuple[int, int]) -> bytes:
    """Resize to exactly `size`, keeping the source format.

    The aspect ratio is deliberately not preserved. A texture is mapped onto the model's UVs
    as 0..1 in both directions, so stretching to a power-of-two size looks identical in-game.
    """
    with Image.open(io.BytesIO(data)) as img:
        fmt = img.format or "PNG"
        resized = img.resize(size, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        if fmt == "JPEG":
            resized.save(out, format="JPEG", quality=95)
        else:
            resized.save(out, format=fmt, optimize=True)
        return out.getvalue()


def write_resized_cabinet(
    src_zip: str,
    dest_zip: str,
    targets: dict[str, tuple[int, int]],
) -> list[str]:
    """Copy src_zip to dest_zip, resizing the textures named in `targets`.

    Everything else is copied byte-for-byte. Returns the names actually resized.
    """
    if Path(src_zip).resolve() == Path(dest_zip).resolve():
        raise ValueError("Output must be a different file than the original cabinet.")
    resized = []
    with zipfile.ZipFile(src_zip) as src, zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename in targets:
                data = resize_image_bytes(data, targets[info.filename])
                resized.append(info.filename)
            dst.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    return resized


def targets_for(reports: list[TextureReport]) -> dict[str, tuple[int, int]]:
    return {r.name: r.target_size for r in reports if r.needs_resize}


def _names_to_copy(names: list[str]) -> list[str]:
    """The cabinet files an export copies: all of them except ones that must not ship.

    Left out: Age of Joy cache files (`*.aojv1`); any `.zip` (a cabinet never contains
    one, so it is an earlier export saved into the cabinet folder); and any nested
    cabinet, i.e. a subfolder with its own description.yaml inside the cabinet's
    (an earlier "Export to New Folder" saved into the cabinet folder).
    """
    def inside(name, folder):  # folder "" is the source's root
        return not folder or name.startswith(folder + "/")

    yaml_dirs = {n.rpartition("/")[0] for n in names
                 if n.rsplit("/", 1)[-1].lower() == "description.yaml"}
    nested = [d for d in yaml_dirs if any(e != d and inside(d, e) for e in yaml_dirs)]
    return [n for n in names
            if not sources.is_aoj_cache(n)
            and not n.lower().endswith(".zip")
            and not any(inside(n, d) for d in nested)]


def export_folder(src_path, dest_dir, targets: dict[str, tuple[int, int]]) -> Path:
    """Duplicate a cabinet (zip or folder) into a NEW folder, resizing `targets`.

    Every file is copied across (bar the leftovers `_names_to_copy` drops); the
    textures named in `targets` are re-encoded at their new size, everything else
    byte-for-byte. Nondestructive: the source is never touched, and this refuses to
    write into an existing folder or the source. Returns the created folder.
    """
    src, dest = Path(src_path), Path(dest_dir)
    if dest.exists():
        raise ValueError(f"“{dest.name}” already exists here. Choose a different name.")
    if src.is_dir() and src.resolve() == dest.resolve():
        raise ValueError("The export folder must be different from the source.")
    with sources.open_source(src_path) as source:
        # Read the list before we create anything under dest, which may sit inside src.
        names = _names_to_copy(source.namelist())
        dest.mkdir(parents=True)
        for name in names:
            data = source.read(name)
            if name in targets:
                data = resize_image_bytes(data, targets[name])
            out = dest / name
            out.parent.mkdir(parents=True, exist_ok=True)  # flat cabinets, but be safe
            out.write_bytes(data)
    return dest


def export_zip(src_path, dest_zip, targets: dict[str, tuple[int, int]]) -> Path:
    """Duplicate a cabinet (zip or folder) into a NEW zip, resizing `targets`.

    Like `export_folder`, but the destination is a single .zip. Refuses to overwrite
    the source zip; overwriting any other existing zip is left to the caller (the
    Save-As dialog already confirms that). Returns the written zip path.
    """
    src, dest = Path(src_path), Path(dest_zip)
    if not src.is_dir() and src.resolve() == dest.resolve():
        raise ValueError("The export zip must be a different file than the source.")
    with sources.open_source(src_path) as source:
        # Read the list BEFORE creating the zip: when it is saved inside a folder
        # cabinet, listing afterwards would pack the half-written zip into itself.
        names = _names_to_copy(source.namelist())
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as dst:
            for name in names:
                data = source.read(name)
                if name in targets:
                    data = resize_image_bytes(data, targets[name])
                dst.writestr(name, data)
    return dest
