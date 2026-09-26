"""Reading textures out of a cabinet zip and checking them."""

import io
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from PIL import Image, ImageChops, ImageFilter, ImageStat

from . import rules, sources

TEXTURE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
# Every video ships with a "<video>.mp4.png" thumbnail. It is not a cabinet texture.
THUMBNAIL_SUFFIXES = (".mp4.png", ".mp4.jpg", ".mp4.jpeg")


@dataclass
class TextureReport:
    name: str  # path inside the zip
    width: int = 0
    height: int = 0
    file_bytes: int = 0  # compressed size on disk (the PNG/JPEG as stored in the zip)
    has_alpha: bool = False
    referenced: bool | None = None  # None when there is no description.yaml to compare against
    issues: list[rules.Issue] = field(default_factory=list)
    unreadable: bool = False
    flat_color: bool = False  # a near-single-color texture; 8x8 is plenty
    # Smallest power-of-two size the texture's *content* actually justifies, from the
    # detail probe (see _recommended_size). None means "no reduction found -- keep it".
    # Purely advisory: shown in its own column, never fed into the resize target.
    recommended_size: tuple[int, int] | None = None

    @property
    def ingame_bytes(self) -> int:
        """Uncompressed footprint the GPU holds: raw RGB, or RGBA when there's alpha.

        PNG/JPEG are compressed on disk but decode to raw pixels in memory, so this
        is what the texture actually costs in the game, ignoring mipmaps.
        """
        if self.unreadable:
            return 0
        return self.width * self.height * (4 if self.has_alpha else 3)

    @property
    def resized_ingame_bytes(self) -> int:
        """In-game footprint after resizing: the target size for textures that need
        it, the original size for ones that are already fine."""
        if self.unreadable:
            return 0
        w, h = self.target_size if self.needs_resize else (self.width, self.height)
        return w * h * (4 if self.has_alpha else 3)

    @property
    def severity(self) -> str | None:
        if not self.issues:
            return None
        return min((i.severity for i in self.issues), key=rules.SEVERITY_ORDER.__getitem__)

    @property
    def needs_resize(self) -> bool:
        if self.unreadable:
            return False
        if self.flat_color:
            return True  # shrink it down to 8x8
        return (
            not rules.is_power_of_two(self.width)
            or not rules.is_power_of_two(self.height)
            or max(self.width, self.height) > rules.MAX_SIZE
        )

    @property
    def target_size(self) -> tuple[int, int]:
        if self.flat_color:
            return (rules.FLAT_COLOR_SIZE, rules.FLAT_COLOR_SIZE)
        return rules.suggested_size(self.width, self.height)


def is_texture_name(name: str) -> bool:
    lower = name.lower()
    return (
        PurePosixPath(lower).suffix in TEXTURE_EXTENSIONS
        and not lower.endswith(THUMBNAIL_SUFFIXES)
    )


def referenced_files(src: "sources.ZipCabinet | sources.FolderCabinet") -> set[str] | None:
    """Lower-cased file names mentioned in description.yaml, or None if there isn't one.

    description.yaml is scanned as text, not parsed, so the tool needs no YAML dependency
    and still works when the file has odd formatting.
    """
    yaml_name = next((n for n in src.namelist() if n.rsplit("/", 1)[-1].lower() == "description.yaml"), None)
    if yaml_name is None:
        return None
    text = src.read(yaml_name).decode("utf-8", errors="replace")
    found = re.findall(r"^\s*file:\s*['\"]?(.+?)['\"]?\s*$", text, flags=re.MULTILINE)
    return {f.lower() for f in found}


def _part_chunks(src: "sources.ZipCabinet | sources.FolderCabinet") -> list[tuple[str, str]]:
    """(part name, its yaml text) for each entry under `parts:` in description.yaml.

    Parsed as text (like referenced_files) rather than via a YAML library, so a
    malformed file degrades to a partial result instead of crashing.
    """
    yaml_name = next((n for n in src.namelist() if n.rsplit("/", 1)[-1].lower() == "description.yaml"), None)
    if yaml_name is None:
        return []
    text = src.read(yaml_name).decode("utf-8", errors="replace")
    parts_match = re.search(r"^parts:\s*$(.*?)(?=^\S)", text, flags=re.MULTILINE | re.DOTALL)
    section = parts_match.group(1) if parts_match else text
    chunks = re.split(r"^\s*-\s*name:", section, flags=re.MULTILINE)
    out = []
    for chunk in chunks[1:]:
        name = chunk.splitlines()[0].strip().strip("'\"")
        if name:
            out.append((name, chunk))
    return out


def crt_custom_mesh(src: "sources.ZipCabinet | sources.FolderCabinet") -> str | None:
    """The screen-mesh node name for a `crt: type: custom` cabinet, else None.

    Age of Joy lets a cabinet ship its own CRT screen via
    `crt: { type: custom, mesh: <node> }`. Built-in screen types (e.g. `19i`)
    use the engine's own screen, so there is nothing of the author's to check --
    only `type: custom` returns a name. Read as text, like the other yaml helpers
    here, so no YAML dependency is needed and a malformed file still degrades.
    """
    yaml_name = next((n for n in src.namelist() if n.rsplit("/", 1)[-1].lower() == "description.yaml"), None)
    if yaml_name is None:
        return None
    text = src.read(yaml_name).decode("utf-8", errors="replace")
    block_match = re.search(r"^crt:\s*$(.*?)(?=^\S|\Z)", text, flags=re.MULTILINE | re.DOTALL)
    if not block_match:
        return None
    block = block_match.group(1)
    if not re.search(r"^\s*type:\s*['\"]?custom\b", block, flags=re.MULTILINE | re.IGNORECASE):
        return None
    mesh_match = re.search(r"^\s*mesh:\s*['\"]?(.+?)['\"]?\s*$", block, flags=re.MULTILINE)
    return mesh_match.group(1).strip() if mesh_match else None


def part_art_map(src: "sources.ZipCabinet | sources.FolderCabinet") -> dict[str, str]:
    """Map each part name (as it appears in the GLB) to its `art.file` texture.

    In description.yaml a `parts:` entry looks like `- name: main` with an
    `art: { file: main.png }` under it; that texture overrides whatever is baked
    into the mesh. Keys are lower-cased part names; values keep the texture's
    original casing.
    """
    mapping: dict[str, str] = {}
    for name, chunk in _part_chunks(src):
        file_match = re.search(r"^\s*file:\s*['\"]?(.+?)['\"]?\s*$", chunk, flags=re.MULTILINE)
        if file_match:
            mapping[name.lower()] = file_match.group(1).strip()
    return mapping


def part_style_map(src: "sources.ZipCabinet | sources.FolderCabinet") -> dict[str, dict]:
    """Map each part name to its yaml appearance: {'color': (r,g,b)|None, 'visible': bool}.

    Age of Joy parts can be given a flat `color: {r, g, b}` (0-255, optional
    `intensity` multiplier) instead of a texture, and `visible: false` hides a
    part entirely. Used by the 3D preview so colored parts don't all show as grey.
    Textures still take precedence over color where a part has both.
    """
    styles: dict[str, dict] = {}
    for name, chunk in _part_chunks(src):
        visible = re.search(r"^\s*visible:\s*false\b", chunk, flags=re.MULTILINE | re.IGNORECASE) is None
        color = None
        cm = re.search(r"\bcolor:", chunk)
        if cm:
            window = chunk[cm.end():cm.end() + 200]  # r/g/b sit right under `color:`
            r = re.search(r"\br:\s*([\d.]+)", window)
            g = re.search(r"\bg:\s*([\d.]+)", window)
            b = re.search(r"\bb:\s*([\d.]+)", window)
            if r and g and b:
                mult = re.search(r"\bintensity:\s*([\d.]+)", window)
                k = float(mult.group(1)) if mult else 1.0
                color = tuple(
                    max(0, min(255, round(float(m.group(1)) * k))) for m in (r, g, b))
        styles[name.lower()] = {"color": color, "visible": visible}
    return styles


def check_texture(name: str, data: bytes) -> TextureReport:
    report = TextureReport(name, file_bytes=len(data))
    try:
        with Image.open(io.BytesIO(data)) as img:
            report.width, report.height = img.size
            report.has_alpha = _has_alpha(img)
            # A flat-color texture just needs to be 8x8; that supersedes any
            # power-of-two / oversize note, since 8x8 satisfies those too.
            flat = rules.flat_color_issue(*img.size) if _is_flat_color(img) else None
            if flat is not None:
                report.flat_color = True
                report.issues = [flat]
                # A flat color needs nothing beyond the 8x8 the standard allows.
                report.recommended_size = (rules.FLAT_COLOR_SIZE, rules.FLAT_COLOR_SIZE)
            else:
                report.issues = rules.check_dimensions(*img.size)
                report.recommended_size = _recommended_size(img)
            # A huge texture says so first, flat color or not.
            huge = rules.huge_texture_issue(*img.size)
            if huge is not None:
                report.issues.insert(0, huge)
    except Exception as exc:  # corrupt or unsupported image; report it, keep going
        report.unreadable = True
        report.issues = [rules.Issue(rules.ERROR, f"Could not read image ({exc}).")]
    return report


def _has_alpha(img: Image.Image) -> bool:
    return "A" in img.getbands() or (img.mode == "P" and "transparency" in img.info)


def _is_flat_color(img: Image.Image) -> bool:
    rgba = img.convert("RGBA")
    return rgba.getcolors(maxcolors=rules.FLAT_COLOR_MAX_COLORS) is not None


def _mse(a: Image.Image, b: Image.Image) -> float:
    """Mean squared error between two same-size RGB images, averaged over channels.

    Uses ImageChops/ImageStat (Pillow's `.rms` is sqrt(mean of squares) per band) so
    the checker keeps needing only Pillow -- no numpy, which is a 3D-preview-only dep.
    """
    diff = ImageChops.difference(a, b)
    rms = ImageStat.Stat(diff).rms
    return sum(r * r for r in rms) / len(rms)


def _is_low_color(rgb: Image.Image) -> bool:
    """True if the image is essentially a handful of flat colors (a shape, a button,
    an icon), even when anti-aliased alpha gives it many distinct RGBA values.

    getcolors() is fooled by anti-aliasing, so we quantize to a small palette and ask
    whether that reproduces the image almost exactly. If it does, the texture's real
    content is tiny no matter how many pixels it ships at."""
    quantized = rgb.quantize(colors=rules.LOW_COLOR_MAX_COLORS).convert("RGB")
    return _mse(rgb, quantized) <= rules.LOW_COLOR_MSE


def _round_trip_size(rgb: Image.Image, blur: float, threshold: float) -> tuple[int, int] | None:
    """Smallest power-of-two size that reconstructs `rgb` under `threshold` (mean sq err).

    Downscales to each candidate and bilinearly scales it back (roughly what the GPU
    shows sampling a smaller texture across the same surface). `blur` Gaussian-blurs
    both sides before comparing: 0 is the exact test (real art); a small radius forgives
    sub-pixel edge shifts so high-contrast flat art can shrink. Returns None if nothing
    smaller is faithful, or the image is already tiny."""
    w, h = rgb.size
    longest = max(w, h)
    if longest <= rules.FLAT_COLOR_SIZE:
        return None
    # Compare at DETAIL_CAP_SIZE at most, so a 4096 texture stays affordable and is
    # never recommended below what its detail up to that cap supports.
    if longest > rules.DETAIL_CAP_SIZE:
        s = rules.DETAIL_CAP_SIZE / longest
        ref = rgb.resize((max(1, round(w * s)), max(1, round(h * s))), Image.BOX)
    else:
        ref = rgb
    rw, rh = ref.size
    ref_longest = max(rw, rh)
    ref_cmp = ref.filter(ImageFilter.GaussianBlur(blur)) if blur else ref
    for size in rules.power_of_two_options():
        if size >= ref_longest:  # can't vouch for detail finer than the reference
            break
        cw, ch = max(1, round(rw * size / ref_longest)), max(1, round(rh * size / ref_longest))
        recon = ref.resize((cw, ch), Image.BOX).resize((rw, rh), Image.BILINEAR)
        if blur:
            recon = recon.filter(ImageFilter.GaussianBlur(blur))
        if _mse(ref_cmp, recon) <= threshold:
            # Report power-of-two dimensions in the ORIGINAL image's scale.
            if w >= h:
                return (size, rules.round_down_power_of_two(round(h * size / longest)))
            return (rules.round_down_power_of_two(round(w * size / longest)), size)
    return None


def _recommended_size(img: Image.Image) -> tuple[int, int] | None:
    """Smallest power-of-two size the texture's content justifies, or None to keep it.

    Two independent signals, whichever shrinks further:
      1. The exact round-trip -- catches soft / low-frequency / upscaled textures
         stored far larger than their detail needs.
      2. A perceptual (blurred) round-trip applied ONLY to low-color art -- catches a
         few flat colors shipped at high resolution (e.g. a 2-color 1024 button), which
         signal 1 refuses to shrink because its hard edges score huge error everywhere.

    Advisory only: shown in its own column, never fed into the resize target."""
    rgb = img.convert("RGB")
    sizes = [_round_trip_size(rgb, 0.0, rules.DETAIL_MSE_THRESHOLD)]
    if _is_low_color(rgb):
        sizes.append(_round_trip_size(rgb, rules.LOW_COLOR_BLUR, rules.LOW_COLOR_DETAIL_MSE))
    sizes = [s for s in sizes if s is not None]
    if not sizes:
        return None
    return min(sizes, key=lambda wh: wh[0] * wh[1])


def check_cabinet(path: str) -> list[TextureReport]:
    """Check every texture in a cabinet, given a .zip file OR a folder of loose files."""
    reports = []
    with sources.open_source(path) as src:
        referenced = referenced_files(src)
        for name in src.namelist():
            if not is_texture_name(name):
                continue
            report = check_texture(name, src.read(name))
            if referenced is not None:
                report.referenced = PurePosixPath(name).name.lower() in referenced
                if not report.referenced:
                    report.issues.append(rules.Issue(
                        rules.INFO, "Unreferenced in description.yaml."))
            reports.append(report)
    reports.sort(key=lambda r: (rules.SEVERITY_ORDER.get(r.severity, 3), r.name.lower()))
    return reports


def aoj_cache_files(path: str) -> dict[str, int]:
    """Age of Joy cache files (`*.aojv1`) in a cabinet, name -> size in bytes.

    The game writes these next to an installed cabinet's textures, so they turn up when
    a builder opens the cabinet folder straight out of the game's cabinetsdb. They are
    not the builder's art and must not be shared; the export leaves them out.
    """
    with sources.open_source(path) as src:
        return {n: src.size(n) for n in src.namelist() if sources.is_aoj_cache(n)}
