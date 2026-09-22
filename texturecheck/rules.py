"""Texture size rules. Keep in sync with docs/texture-standards.md."""

import math
from dataclasses import dataclass

MAX_SIZE = 4096
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

ERROR = "error"
WARNING = "warning"
INFO = "info"

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


def flat_color_issue(width: int, height: int) -> Issue | None:
    if max(width, height) > FLAT_COLOR_SIZE:
        return Issue(INFO, "Flat color. Optimal: set part via color in yaml.")
    return None
