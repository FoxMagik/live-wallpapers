#!/bin/bash
# Install the livewall desktop host on an Omarchy/Hyprland machine.
set -euo pipefail
REPO=$(cd "$(dirname "$0")" && pwd)
command -v pacman >/dev/null && ! pacman -Q gtk-layer-shell webkit2gtk-4.1 python-gobject >/dev/null 2>&1 &&
  sudo pacman -S --needed gtk-layer-shell webkit2gtk-4.1 python-gobject
mkdir -p ~/.local/bin ~/.config/systemd/user
ln -sf "$REPO/bin/livewall" ~/.local/bin/livewall
cp "$REPO/host/livewall.service" ~/.config/systemd/user/livewall.service
systemctl --user daemon-reload
systemctl --user enable livewall.service
if [[ -d ~/.config/omarchy/hooks ]]; then
  mkdir -p ~/.config/omarchy/hooks/theme-set.d
  install -m 755 "$REPO/host/theme-set-hook" ~/.config/omarchy/hooks/theme-set.d/livewall
fi
# Omarchy themes that pair with the scenes (never overwrite an existing one)
if [[ -d ~/.config/omarchy ]]; then
  mkdir -p ~/.config/omarchy/themes
  for t in "$REPO"/themes/*/; do
    name=$(basename "$t")
    [[ -e ~/.config/omarchy/themes/$name ]] || cp -r "$t" ~/.config/omarchy/themes/
  done
fi
echo "Installed. Try: livewall list; livewall set helldivers"
