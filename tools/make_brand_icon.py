#!/usr/bin/env python3
"""Build the integration's brand images from the Nespresso app icon.

Takes the 512x512 store icon shipped in the XAPK and reshapes it into an
iOS-style squircle, then writes the sizes Home Assistant wants:

    custom_components/nespresso_smart/brand/icon.png      256x256
    custom_components/nespresso_smart/brand/icon@2x.png   512x512

The squircle is Apple's icon shape: a rounded rect with continuous curvature,
the one Figma calls corner smoothing. Not a superellipse, whose edges bow
outward. The mask is rendered with rsvg-convert (`brew install librsvg`) from
the same path the other integrations draw their tiles with, so every icon
shares one outline.

The source icon's own corners are only slightly rounded, so the squircle is
strictly inside them and masking never clips the artwork. The N and the ring's
dots sit at the centre and at the edge midpoints, away from the corners.

    python3 tools/make_brand_icon.py
"""

from __future__ import annotations

import io
import math
import pathlib
import subprocess
import sys

from PIL import Image, ImageChops

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "work" / "xapk" / "icon.png"
OUT = REPO / "custom_components" / "nespresso_smart" / "brand"

#: Apple's app-icon corner, as a fraction of the width, with Figma's equivalent
#: corner-smoothing value. These two numbers *are* the iOS icon shape; see squircle().
CORNER_RADIUS_RATIO = 0.2237
CORNER_SMOOTHING = 0.6


def squircle(size: float) -> str:
    """Return the iOS app-icon shape as an SVG path, inscribed in a `size` square.

    Not a superellipse. The obvious implementation — `|x|^n + |y|^n = 1` with n
    around 5 — is the shape most people mean by "squircle", but it is not the one
    Apple uses, and the difference is visible: a superellipse curves continuously
    everywhere, so the edges that should be flat bow outward. Side by side against
    a real app icon it reads as pillowed.

    Apple's mask is a *rounded rectangle with continuous curvature*, as produced by
    `UIBezierPath(roundedRect:cornerRadius:)`: genuinely straight edges, with the
    corner easing curvature in over a longer run than a circular arc would. This is
    the construction Figma exposes as "corner smoothing", at the iOS values.

    A circular corner is still wrong for the reason you would expect — curvature
    jumps from 0 to 1/r at the join — but the fix is smoothing the transition, not
    abandoning the straight edge.
    """
    radius = size * CORNER_RADIUS_RATIO
    budget = size / 2

    # Keep the corner inside the space available to it; without the cap a large
    # radius yields a self-intersecting path rather than a clipped one.
    smoothing = min(CORNER_SMOOTHING, budget / radius - 1)
    p = min((1 + smoothing) * radius, budget)

    arc_measure = math.radians(90 * (1 - smoothing))
    arc = math.sin(arc_measure / 2) * radius * math.sqrt(2)

    angle_alpha = (math.pi / 2 - arc_measure) / 2
    angle_beta = math.radians(45 * smoothing)
    c = radius * math.tan(angle_alpha / 2) * math.cos(angle_beta)
    d = c * math.tan(angle_beta)

    # The straight run into each corner splits 2:1 between the two off-curve
    # control points. That ratio is what ramps curvature smoothly.
    b = (p - arc - c - d) / 3
    a = 2 * b

    def f(value: float) -> str:
        return f"{value:.4f}"

    return " ".join(
        [
            f"M {f(size - p)} 0",
            f"c {f(a)} 0 {f(a + b)} 0 {f(a + b + c)} {f(d)}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(arc)} {f(arc)}",
            f"c {f(d)} {f(c)} {f(d)} {f(b + c)} {f(d)} {f(a + b + c)}",
            f"L {f(size)} {f(size - p)}",
            f"c 0 {f(a)} 0 {f(a + b)} {f(-d)} {f(a + b + c)}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(-arc)} {f(arc)}",
            f"c {f(-c)} {f(d)} {f(-(b + c))} {f(d)} {f(-(a + b + c))} {f(d)}",
            f"L {f(p)} {f(size)}",
            f"c {f(-a)} 0 {f(-(a + b))} 0 {f(-(a + b + c))} {f(-d)}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(-arc)} {f(-arc)}",
            f"c {f(-d)} {f(-c)} {f(-d)} {f(-(b + c))} {f(-d)} {f(-(a + b + c))}",
            f"L 0 {f(p)}",
            f"c 0 {f(-a)} 0 {f(-(a + b))} {f(d)} {f(-(a + b + c))}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(arc)} {f(-arc)}",
            f"c {f(c)} {f(-d)} {f(b + c)} {f(-d)} {f(a + b + c)} {f(-d)}",
            "Z",
        ]
    )


def squircle_mask(size: int) -> Image.Image:
    """Return an 'L' mask of the iOS icon shape filling a size x size square."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}"><path d="{squircle(size)}"/></svg>'
    )
    png = subprocess.run(
        ["rsvg-convert"], input=svg.encode(), capture_output=True, check=True
    ).stdout
    return Image.open(io.BytesIO(png)).getchannel("A")


def main() -> None:
    if not SRC.is_file():
        sys.exit(
            f"Source icon not found at {SRC}\n"
            "Unpack the XAPK first:  unzip -o *.xapk -d work/xapk"
        )

    src = Image.open(SRC).convert("RGBA")
    if src.width != src.height:
        sys.exit(f"Source icon must be square, got {src.width}x{src.height}")

    OUT.mkdir(parents=True, exist_ok=True)

    for name, size in (("icon@2x.png", 512), ("icon.png", 256)):
        img = (
            src.resize((size, size), Image.LANCZOS) if src.width != size else src.copy()
        )
        # Multiply into the existing alpha so any transparency in the source
        # survives instead of being overwritten.
        img.putalpha(ImageChops.multiply(img.getchannel("A"), squircle_mask(size)))
        dest = OUT / name
        img.save(dest, "PNG", optimize=True)
        print(f"wrote {dest.relative_to(REPO)}  {size}x{size}")


if __name__ == "__main__":
    main()
