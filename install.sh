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

# Omarchy: add the bar widget (its own repo, so `omarchy plugin update` keeps
# it current) and put it next to the volume control the first time; later
# installs leave its place, or its removal, alone.
plugin_id="dev.tayters.vibefx"
plugin_url="https://github.com/HeyitsTayters/vibefx-omarchy.git"
plugin_dir="$conf/omarchy/plugins/$plugin_id"
if command -v omarchy >/dev/null && [ -d "$conf/omarchy" ]; then
  if [ -d "$plugin_dir/.git" ]; then
    omarchy plugin update "$plugin_id" --yes >/dev/null 2>&1 || true
    echo "Omarchy bar widget up to date ($plugin_id)."
  else
    first_install=0
    [ -d "$plugin_dir" ] || first_install=1
    if [ "$first_install" = 0 ]; then
      # A plain copy from an earlier install: swap it for the git-managed one.
      rm -rf "$plugin_dir"
      omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
    fi
    if omarchy plugin add "$plugin_url" --yes >/dev/null 2>&1; then
      if [ "$first_install" = 1 ]; then
        omarchy plugin enable "$plugin_id" --before omarchy.audio >/dev/null 2>&1 ||
          omarchy plugin enable "$plugin_id" >/dev/null 2>&1 || true
      fi
      echo "Omarchy bar widget installed ($plugin_id)."
    else
      echo "Couldn't add the Omarchy bar widget (offline?). Add it later with:"
      echo "  omarchy plugin add $plugin_url"
    fi
  fi
fi

echo "Installed VibeFX $("$HOME/.local/bin/vibefx" --version | cut -d' ' -f2): launcher entry, tray icons, startup entry."
if ! ls /usr/lib/ladspa/fast_lookahead_limiter_1913.so >/dev/null 2>&1; then
  echo "Tip: install swh-plugins for the limiter (enables Dynamic Boost and full loudness):"
  echo "  omarchy pkg add swh-plugins"
fi
