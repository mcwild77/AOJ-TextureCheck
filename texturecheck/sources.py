"""Reading a cabinet from either a .zip or a plain folder, and duplicating one.

A cabinet is normally distributed as a flat .zip, but builders also work with the
same files loose in a folder. Both expose the two calls the rest of the tool needs
-- `namelist()` and `read(name)` -- so `cabinet.py` and `preview3d.py` work with
either without caring which it is (a ZipFile already has these methods; the folder
adapter mimics them).
"""

import zipfile
from pathlib import Path


class ZipCabinet:
    """A cabinet read from a .zip file."""

    def __init__(self, path):
        self._zf = zipfile.ZipFile(path)

    def namelist(self) -> list[str]:
        return [n for n in self._zf.namelist() if not n.endswith("/")]

    def read(self, name: str) -> bytes:
        return self._zf.read(name)

    def close(self):
        self._zf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FolderCabinet:
    """A cabinet read from a folder of loose files."""

    def __init__(self, path):
        self._root = Path(path)

    def namelist(self) -> list[str]:
        return [p.relative_to(self._root).as_posix()
                for p in self._root.rglob("*") if p.is_file()]

    def read(self, name: str) -> bytes:
        return (self._root / name).read_bytes()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def open_source(path):
    """Open a cabinet path as a ZipCabinet (file) or FolderCabinet (directory)."""
    return FolderCabinet(path) if Path(path).is_dir() else ZipCabinet(path)


def default_export_name(path) -> str:
    """The name an export defaults to: `<CABINETNAME>_optimized` (no extension).

    Uses the folder name for a folder cabinet, or the zip name without `.zip` for a
    zip. The user can override it in the toolbar's name field before exporting.
    """
    src = Path(path)
    stem = src.name if src.is_dir() else src.stem  # folder name, or zip name without .zip
    return f"{stem}_optimized"
