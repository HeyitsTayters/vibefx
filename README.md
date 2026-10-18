# VibeFX

FxSound-style audio enhancement for Linux desktops running PipeWire. A small
GTK4/libadwaita app with a tray icon: turn it on, pick a preset, and everything
you play gets clearer, wider and punchier.

![VibeFX window](docs/screenshot.png)

## Features

- **Effects** (0–10, like FxSound): Clarity, Ambience, Surround, Dynamic Boost,
  Bass Boost, Fidelity
- **10-band equalizer** (31 Hz – 16 kHz, ±12 dB)
- **Presets**: Flat, Music, Movies, TV, Gaming, Voice, Bass Boost, Volume Boost,
  tuned from FxSound's own factory presets and common EQ practice
- **Headphone correction**: import any profile from [autoeq.app](https://autoeq.app)
  (Parametric, Fixed-band or Graphic EQ), as a file or pasted text
- **Limiter** for loudness without clipping (with `swh-plugins`)
- **Tray icon**: left-click opens, middle-click toggles effects, right-click for
  presets and quit; closing the window keeps it running in the tray
- **Launch at startup**, optionally straight to the tray
- **Update checks** against GitHub releases, with one-click install
- Changes apply live, with no audio dropouts
- **Built-in help** (F1, the ? button, or the tray menu) covering every control
- **Omarchy bar widget** and a scriptable `vibefx ctl` command (see below)

Sliders: drag, or hold **Ctrl** and scroll over one. The scroll wheel alone
scrolls the window, so values never change by accident.

## How it works

VibeFX runs a PipeWire filter-chain (`vibefx.service`, a user unit) that
creates a virtual **VibeFX** output, applies the effects with PipeWire's
built-in filters, and plays into your real device. VibeFX is your default
output while the app runs; turning effects off switches the chain to a neutral
bypass instead of changing devices, so playback is never paused. Quitting hands
audio back to your device.

## Requirements

- PipeWire with WirePlumber (`pw-cli`, `pw-dump`, `pw-metadata`, `wpctl`)
- Python 3.12+ with PyGObject, GTK 4 and libadwaita 1.4+
- systemd (user session)
- Optional: `swh-plugins` for the limiter (enables Dynamic Boost)

On Arch: `sudo pacman -S python-gobject gtk4 libadwaita swh-plugins`

## Install

```bash
git clone https://github.com/HeyitsTayters/vibefx.git
cd vibefx
./install.sh
```

This installs to `~/.local` (no root): the `vibefx` command, an app launcher
entry, icons, and a login startup entry. Launch **VibeFX** from your app
launcher.

## Omarchy bar widget

On [Omarchy](https://omarchy.org), `install.sh` also installs a bar widget
(`dev.tayters.vibefx`) and places it next to the volume control.

<img src="docs/omarchy-widget.png" alt="VibeFX bar widget" width="360">

- **Click** opens the panel: effects switch, presets, effect sliders, and
  shortcuts to open or quit the app
- **Right-click** toggles effects, **middle-click** opens the app,
  **scroll** cycles presets
- Keyboard: arrows move, ←/→ adjust a slider, Enter activates, `p` toggles
  effects, `o` opens the app
- When VibeFX isn't running, the panel offers to start it

Move or remove it like any widget (`omarchy bar move dev.tayters.vibefx …`,
`omarchy plugin disable dev.tayters.vibefx`); reinstalling keeps your choice.
Keybindings can drive it through the shell, for example
`omarchy-shell dev.tayters.vibefx toggleEffects` (also `on`, `off`,
`preset <name>`, `nextPreset`, `previousPreset`, `open`).

## Command line

The running app has a D-Bus API (`dev.tayters.VibeFX.Control` at
`/dev/tayters/VibeFX/Control`), wrapped by `vibefx ctl`:

```bash
vibefx ctl status            # state as JSON
vibefx ctl toggle            # also: on, off
vibefx ctl preset Movies
vibefx ctl effect bass 6     # clarity ambience surround boost bass fidelity, 0-10
vibefx ctl show              # open the window (starts VibeFX if needed)
vibefx ctl start | quit
vibefx ctl watch             # JSON line per change; reads commands on stdin
```

## Uninstall

```bash
./uninstall.sh
```

Settings stay in `~/.config/vibefx` until you delete them.

## Releasing

Update checks read the latest GitHub release. To publish one, bump `VERSION` in
`vibefx.py`, commit, then:

```bash
gh release create v1.2.3 --generate-notes
```

## License

MIT
