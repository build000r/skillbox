#!/usr/bin/env bash
# make-og-card.sh — regenerate assets/og-card.png (1200x630) from assets/og-card.svg
#
# The OG social-preview card for the skillbox GitHub repo. Tries a series of SVG
# rasterizers in order of fidelity, then falls back to a native Pillow renderer
# that reproduces the same design when no SVG rasterizer is installed.
#
# NOTE: committing the PNG is NOT enough to change the share card on GitHub.
# A human must upload assets/og-card.png at:
#   GitHub repo -> Settings -> General -> Social preview -> Upload an image
# (The repo README cannot set the OG image; it lives in repo settings.)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVG="$ROOT/assets/og-card.svg"
PNG="$ROOT/assets/og-card.png"
W=1200
H=630

if [[ ! -f "$SVG" ]]; then
  echo "error: missing $SVG" >&2
  exit 1
fi

render_with_rsvg()   { rsvg-convert -w "$W" -h "$H" -o "$PNG" "$SVG"; }
render_with_resvg()  { resvg -w "$W" -h "$H" "$SVG" "$PNG"; }
render_with_inkscape() { inkscape "$SVG" --export-type=png --export-filename="$PNG" -w "$W" -h "$H"; }
render_with_convert() { convert -background none -density 144 "$SVG" -resize "${W}x${H}" "$PNG"; }
render_with_chromium() {
  local bin="$1"
  "$bin" --headless --no-sandbox --disable-gpu \
    --screenshot="$PNG" --window-size="${W},${H}" --default-background-color=00000000 \
    "file://$SVG"
}
render_with_cairosvg() {
  python3 - "$SVG" "$PNG" "$W" "$H" <<'PY'
import sys, cairosvg
svg, png, w, h = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
cairosvg.svg2png(url=svg, write_to=png, output_width=w, output_height=h)
PY
}

# Native Pillow fallback: draws the card to match og-card.svg exactly.
render_with_pillow() {
  python3 "$ROOT/scripts/make_og_card_pillow.py" "$PNG" "$W" "$H"
}

used=""
if   command -v rsvg-convert >/dev/null 2>&1; then render_with_rsvg   && used="rsvg-convert"
elif command -v resvg        >/dev/null 2>&1; then render_with_resvg  && used="resvg"
elif command -v inkscape     >/dev/null 2>&1; then render_with_inkscape && used="inkscape"
elif command -v convert      >/dev/null 2>&1; then render_with_convert && used="ImageMagick convert"
elif command -v chromium     >/dev/null 2>&1; then render_with_chromium chromium && used="chromium"
elif command -v chromium-browser >/dev/null 2>&1; then render_with_chromium chromium-browser && used="chromium-browser"
elif command -v google-chrome >/dev/null 2>&1; then render_with_chromium google-chrome && used="google-chrome"
elif python3 -c "import cairosvg" >/dev/null 2>&1; then render_with_cairosvg && used="cairosvg"
elif python3 -c "import PIL" >/dev/null 2>&1; then render_with_pillow && used="Pillow (native renderer)"
else
  echo "error: no SVG rasterizer and no Pillow available." >&2
  echo "Commit assets/og-card.svg and render the PNG on a machine with one of:" >&2
  echo "  rsvg-convert | resvg | inkscape | ImageMagick | chromium | python3+cairosvg|Pillow" >&2
  exit 2
fi

echo "rendered $PNG via: $used"
if command -v identify >/dev/null 2>&1; then
  identify "$PNG"
elif command -v file >/dev/null 2>&1; then
  file "$PNG"
else
  python3 -c "from PIL import Image; im=Image.open('$PNG'); print('$PNG', im.size, im.mode)"
fi
