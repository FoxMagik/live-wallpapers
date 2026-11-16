#!/bin/bash
# Render one frame of a scene to an image (theme background still / preview).
# usage: tools/render-still.sh scenes/<slug>.html out.png [sim-seconds] [seed] [WxH]
set -euo pipefail
scene=$(realpath "$1"); out=$(realpath -m "$2"); t=${3:-8}; seed=${4:-7}; size=${5:-1920x1080}
tmp=$(mktemp -d)
chromium --headless=new --no-sandbox --disable-gpu --hide-scrollbars --user-data-dir="$tmp" \
  --window-size="${size/x/,}" --virtual-time-budget=3000 \
  --screenshot="$tmp/shot.png" "file://$scene?still=1&t=$t&seed=$seed" >/dev/null 2>&1
case "$out" in
  *.png) mv "$tmp/shot.png" "$out" ;;
  *) magick "$tmp/shot.png" -quality 92 "$out" ;;
esac
rm -rf "$tmp"
echo "$out"
