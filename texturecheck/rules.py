"""Texture size rules. Keep in sync with docs/texture-standards.md."""

import math
from dataclasses import dataclass

MAX_SIZE = 4096
# Over this many pixels on either side, a texture leads its Issues with HUGE_MESSAGE.
HUGE_SIZE = 2100
HUGE_MESSAGE = "Huge texture."
FLAT_COLOR_SIZE = 8
FLAT_COLOR_MAX_COLORS = 4

# "Recommended size": the detail probe (cabinet._recommended_size) shrinks a texture
# to the smallest power of two that still reconstructs it under this mean-squared error
# (0-255 per channel, averaged over RGB). Higher = more aggressive shrinking. Tuned
# against Cabinets/ so genuinely detailed art keeps its size while soft / low-frequency
# / upscaled textures get flagged as reducible. Advisory only -- never auto-applied.
DETAIL_MSE_THRESHOLD = 12.0
# The probe compares at this longest-edge at most, to bound cost on 4096 textures. It
# also caps how large a recommendation can be, so a texture is never told to shrink
# below what its detail up to this size justifies (the safe direction).
DETAIL_CAP_SIZE = 2048

# Low-color art (a few flat colors forming a shape, e.g. a 2-color button shipped at
# 1024) defeats the plain round-trip: its high-contrast edges score huge error at any
# downscale, so nothing looks "faithful". We detect it separately -- an image that
# quantizes to this few colors with essentially no error -- and size it with a small
# perceptual blur, so a sub-pixel edge shift stops dominating and the real (tiny)
# information content shows through. Blur is applied ONLY to these, never to real art.
LOW_COLOR_MAX_COLORS = 8
LOW_COLOR_MSE = 1.0     # quantization error below which the palette is "exact"
LOW_COLOR_BLUR = 1.0    # Gaussian radius for the perceptual comparison
# Sizing a low-color shape has no objective answer -- its error is all edge sharpness,
# which slides smoothly with resolution (no "knee"), so the size is whatever this
# threshold cuts. It is looser than DETAIL_MSE_THRESHOLD on purpose: flat art tolerates
# a slightly softer edge (it is bilinear-filtered in-game anyway), so a simple button
# lands ~128 rather than an over-cautious 256. Advisory only.
LOW_COLOR_DETAIL_MSE = 20.0

# Custom CRT screens (crt: type: custom) supply their own screen mesh. Most arcade
# games are 4:3, so a screen quad whose measured aspect (longer side / shorter side)
# strays past this tolerance from 4:3 is almost always a mistake (people ship 1:1 or
# 16:9). The tolerance accepts ~1.27..1.39, so a real 4:3 with modeling slop passes
# while 5:4 (1.25), 3:2 (1.5), 16:10 (1.6) and 16:9 (1.78) are all flagged.
SCREEN_TARGET_ASPECT = 4 / 3
SCREEN_ASPECT_TOLERANCE = 0.06


def screen_aspect_ok(ratio: float) -> bool:
    """True if a screen's longer/shorter aspect is close enough to 4:3."""
    return abs(ratio - SCREEN_TARGET_ASPECT) <= SCREEN_ASPECT_TOLERANCE


ERROR = "error"
WARNING = "warning"
INFO = "info"

# Whole-cabinet polygon budget: the total triangles of the cabinet model's visible
# meshes. Worst tier first; the first threshold the total is over wins. At or below the
# last one the cabinet is fine and nothing is flagged.
POLY_TIERS = (
    (300_000, ERROR, "Dangerously high polygon count", "This risks crashing the game."),
    (200_000, ERROR, "Extremely high polygon count", "This will cause a huge performance hit."),
    (100_000, ERROR, "Very high polygon count", ""),
    (25_000, WARNING, "High polygon count", ""),
)

# UV usage: the percent of a texture's map that its mesh's UVs cover. The rest costs
# memory in game but is never seen. Worst tier first; the first threshold the usage is
# under wins. At or above the last one nothing is flagged.
UV_USAGE_TIERS = (
    (10, ERROR, "Critically bad UV usage"),
    (25, ERROR, "Extremely bad UV usage"),
    (50, WARNING, "Bad UV usage"),
)
# Under this usage, a texture over UV_MEMORY_SIZE on either side also gets a memory warning.
UV_MEMORY_USAGE = 25
UV_MEMORY_SIZE = 1024
# UVs this far past the 0..1 map mean the texture repeats (tiles) across its mesh, so
# every pixel is on show and "unused space" doesn't apply. Anything closer is modeling slop.
UV_TILE_TOLERANCE = 0.02

SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


@dataclass(frozen=True)
class Issue:
    severity: str
    message: str


def is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def nearest_power_of_two(n: int, max_size: int = MAX_SIZE) -> int:
    """Nearest power of two in log space (773 -> 1024, 1412 -> 1024), capped at max_size."""
    if n <= 1:
        return 1
    return min(2 ** round(math.log2(n)), max_size)


def round_down_power_of_two(n: int, max_size: int = MAX_SIZE) -> int:
    """Largest power of two that is <= n (773 -> 512, 1120 -> 1024), capped at max_size.

    Used as the resize target so the tool only ever shrinks a texture, never upscales
    it (upscaling adds no detail and grows the in-game footprint).
    """
    if n < 1:
        return 1
    return min(2 ** int(math.floor(math.log2(n))), max_size)


def suggested_size(width: int, height: int) -> tuple[int, int]:
    return round_down_power_of_two(width), round_down_power_of_two(height)


def power_of_two_options(min_size: int = FLAT_COLOR_SIZE, max_size: int = MAX_SIZE) -> list[int]:
    """Powers of two offered as resize targets, e.g. [8, 16, 32, ... 2048]."""
    options, n = [], 1
    while n <= max_size:
        if n >= min_size:
            options.append(n)
        n *= 2
    return options


def check_dimensions(width: int, height: int) -> list[Issue]:
    issues = []
    bad = [
        f"{label} {value}"
        for label, value in (("width", width), ("height", height))
        if not is_power_of_two(value)
    ]
    if bad:
        issues.append(Issue(
            ERROR,
            f"{width}x{height} is not a power of two.",
        ))
    elif max(width, height) > MAX_SIZE:
        issues.append(Issue(
            WARNING,
            f"Texture > {MAX_SIZE} pixels. Only use for densely packed texture "
            f"sheets or extreme detailing.",
        ))
    return issues


def huge_texture_issue(width: int, height: int) -> Issue | None:
    if max(width, height) > HUGE_SIZE:
        return Issue(WARNING, HUGE_MESSAGE)
    return None


def flat_color_issue(width: int, height: int) -> Issue | None:
    if max(width, height) > FLAT_COLOR_SIZE:
        return Issue(INFO, "Flat color. Optimal: set part via color in yaml.")
    return None


def poly_count_issue(total: int) -> Issue | None:
    """The cabinet-wide polygon warning for `total` triangles, or None when within budget."""
    for threshold, severity, label, consequence in POLY_TIERS:
        if total > threshold:
            parts = [f"{label}: {total:,} polygons (over {threshold:,}).", consequence,
                     "Simplify the model in your 3D editor."]
            return Issue(severity, " ".join(p for p in parts if p))
    return None


def uv_usage_issues(usage: float, width: int, height: int) -> list[Issue]:
    """Warnings for a width x height texture whose UVs cover only `usage` percent of it:
    the tier message, then a memory warning when it is big as well. Empty when fine."""
    for threshold, severity, label in UV_USAGE_TIERS:
        if usage < threshold:
            break
    else:
        return []
    unused = 100 - round(usage)  # matches the rounded "UV usage" column
    issues = [Issue(severity, f"{label}: {unused}% of this texture is unused (the area that "
                              "flashes red). Crop the texture to the part the model uses and "
                              "scale the UVs up to fill it.")]
    if usage < UV_MEMORY_USAGE and max(width, height) > UV_MEMORY_SIZE:
        issues.append(Issue(ERROR, f"This texture is over {UV_MEMORY_SIZE} pixels, so all that "
                                   "unused space will result in extremely high memory usage."))
    return issues
