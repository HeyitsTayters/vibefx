#!/bin/bash
# Install VibeFX for the current user (no root needed).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
conf="${XDG_CONFIG_HOME:-$HOME/.config}"

# Migrate from the app's previous name (Sonance), keeping settings.
if [ -d "$conf/sonance" ] && [ ! -e "$conf/vibefx/settings.json" ] && [ -f "$conf/sonance/settings.json" ]; then
  install -Dm644 "$conf/sonance/settings.json" "$conf/vibefx/settings.json"
fi
if [ -f "$conf/systemd/user/sonance.service" ]; then
  systemctl --user disable --now sonance.service 2>/dev/null || true
  rm -f "$conf/systemd/user/sonance.service"
  systemctl --user daemon-reload
fi
rm -rf "$conf/sonance"
rm -f "$HOME/.local/bin/sonance" "$data/applications/sonance.desktop"

install -Dm755 "$here/vibefx.py" "$HOME/.local/bin/vibefx"
install -Dm644 "$here/vibefx.desktop" "$data/applications/vibefx.desktop"
for size in 32 48 64 128 256 512; do
  install -Dm644 "$here/icons/vibefx-$size.png" "$data/icons/hicolor/${size}x${size}/apps/vibefx.png"
done
rm -f "$data/icons/hicolor/scalable/apps/vibefx.svg"  # old vector app icon (pre-1.0.1)
for icon in vibefx-tray vibefx-tray-off; do
  install -Dm644 "$here/icons/$icon.svg" "$data/icons/hicolor/scalable/apps/$icon.svg"
done
gtk-update-icon-cache -q -t "$data/icons/hicolor" 2>/dev/null || true

# Login autostart follows the app's "Launch at startup" settings (default: on, in the tray).
"$HOME/.local/bin/vibefx" --sync-autostart

echo "Installed VibeFX $("$HOME/.local/bin/vibefx" --version | cut -d' ' -f2): launcher entry, tray icons, startup entry."
if ! ls /usr/lib/ladspa/fast_lookahead_limiter_1913.so >/dev/null 2>&1; then
  echo "Tip: install swh-plugins for the limiter (enables Dynamic Boost and full loudness):"
  echo "  omarchy pkg add swh-plugins"
fi
