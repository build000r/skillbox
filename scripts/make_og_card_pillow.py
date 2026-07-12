#!/usr/bin/env python3
"""Native Pillow renderer for the skillbox OG card.

Reproduces assets/og-card.svg pixel-for-purpose when no SVG rasterizer is
installed. Design + palette are kept in sync with the SVG by hand.

Palette (~3 colors):
  BG   #242424  near-black background (Tailscale dark accent)
  FG   #e6edf3  off-white foreground text
  ACC  #2ea44f  green accent

Usage: make_og_card_pillow.py OUT.png [WIDTH HEIGHT]
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = (36, 36, 36)          # #242424
PANEL = (27, 27, 27)       # #1b1b1b
BAR = (48, 48, 48)         # #303030
FG = (230, 237, 243)       # #e6edf3
ACC = (46, 164, 79)        # #2ea44f
GREY1 = (90, 90, 90)
GREY2 = (122, 122, 122)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/Library/Fonts/Menlo.ttc",
]
FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
]


def _font(paths: list[str], size: int) -> ImageFont.FreeTypeFont:
    for p in paths:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _blend(fg: tuple[int, int, int], bg: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    return tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))


def render(out: str, w: int = 1200, h: int = 630) -> None:
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)

    reg = lambda s: _font(FONT_CANDIDATES, s)
    bold = lambda s: _font(FONT_BOLD_CANDIDATES, s)

    # terminal window frame
    d.rounded_rectangle([80, 70, 1120, 560], radius=18, fill=PANEL,
                        outline=_blend(ACC, PANEL, 0.35), width=2)
    # title bar
    d.rounded_rectangle([80, 70, 1120, 126], radius=18, fill=BAR)
    d.rectangle([80, 104, 1120, 126], fill=BAR)
    # traffic-light dots
    for cx, col in ((116, GREY1), (144, GREY2), (172, ACC)):
        d.ellipse([cx - 8, 90, cx + 8, 106], fill=col)
    # window title (centered)
    title = "skillbox — agent workstation"
    tf = reg(20)
    tw = d.textlength(title, font=tf)
    d.text((600 - tw / 2, 92), title, font=tf, fill=_blend(FG, BAR, 0.55))

    # headline (verbatim hero)
    hf = bold(52)
    d.text((120, 172), "Give your coding agents", font=hf, fill=FG)
    prefix = "a real "
    d.text((120, 240), prefix, font=hf, fill=FG)
    pw = d.textlength(prefix, font=hf)
    d.text((120 + pw, 240), "computer", font=hf, fill=ACC)
    cw = d.textlength("computer", font=hf)
    d.text((120 + pw + cw, 240), ".", font=hf, fill=FG)

    # install line (real one-liner, protocol elided so it fits legibly)
    cf = reg(20)
    d.text((120, 350), "$ ", font=cf, fill=ACC)
    dw = d.textlength("$ ", font=cf)
    d.text((120 + dw, 350),
           "curl -fsSL raw.githubusercontent.com/build000r/skillbox/main/install.sh | bash",
           font=cf, fill=FG)
    # static cursor block
    d.rectangle([120, 388, 134, 414], fill=_blend(ACC, BG, 0.85))

    # project name + tagline
    d.text((120, 474), "skillbox", font=bold(34), fill=FG)
    d.text((120, 516), "private · durable · agent-first", font=reg(20),
           fill=_blend(FG, BG, 0.6))

    # dated capture stamp (bottom-right)
    stamp = "first-box demo captured 2026-07-05"
    sf = reg(18)
    sw = d.textlength(stamp, font=sf)
    d.text((1080 - sw, 516), stamp, font=sf, fill=_blend(ACC, BG, 0.9))

    img.save(out, "PNG")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: make_og_card_pillow.py OUT.png [WIDTH HEIGHT]", file=sys.stderr)
        return 1
    out = sys.argv[1]
    w = int(sys.argv[2]) if len(sys.argv) > 2 else 1200
    h = int(sys.argv[3]) if len(sys.argv) > 3 else 630
    render(out, w, h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
