#!/bin/bash
# Remove VibeFX and restore normal audio routing.
set -uo pipefail
data="${XDG_DATA_HOME:-$HOME/.local/share}"
conf="${XDG_CONFIG_HOME:-$HOME/.config}"

pkill -f "$HOME/.local/bin/vibefx" 2>/dev/null
systemctl --user disable --now vibefx.service 2>/dev/null
rm -f "$conf/systemd/user/vibefx.service" "$HOME/.local/bin/vibefx" \
      "$data/applications/vibefx.desktop" "$conf/autostart/vibefx.desktop" \
      "$data/icons/hicolor/scalable/apps/"vibefx{,-tray,-tray-off}.svg \
      "$data/icons/hicolor/"{32x32,48x48,64x64,128x128,256x256,512x512}/apps/vibefx.png
systemctl --user daemon-reload
if [ -d "$conf/omarchy/plugins/dev.tayters.vibefx" ]; then
  command -v omarchy >/dev/null && omarchy plugin disable dev.tayters.vibefx >/dev/null 2>&1
  rm -rf "$conf/omarchy/plugins/dev.tayters.vibefx"
fi
echo "Removed. Settings remain in $conf/vibefx (delete it to forget them)."
echo "If audio has no output, pick your device again in the volume menu."
