#!/usr/bin/env python3
"""VibeFX — a small FxSound-style audio enhancer for PipeWire.

The audio work is done by a PipeWire filter-chain running as a user service
(vibefx.service). It exposes a virtual "VibeFX" sink, applies the effects,
and plays into the real output device. This GUI only writes the config,
starts/stops the service, and pushes slider changes live with pw-cli.
"""

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

APP_ID = "dev.tayters.VibeFX"
SINK_NAME = "vibefx_input"
OUTPUT_NAME = "vibefx_output"
SERVICE = "vibefx.service"

VERSION = "1.0.0"
GITHUB_REPO = "OWNER/vibefx"  # owner/name on GitHub; releases drive update checks
UPDATE_INTERVAL = 24 * 3600  # automatic checks at most once a day
MAX_UPDATE_BYTES = 20 * 1024 * 1024

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "vibefx")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
CHAIN_PATH = os.path.join(CONFIG_DIR, "chain.conf")
UNIT_PATH = os.path.join(GLib.get_user_config_dir(), "systemd", "user", SERVICE)
AUTOSTART_PATH = os.path.join(GLib.get_user_config_dir(), "autostart", "vibefx.desktop")

# swh-plugins' look-ahead limiter. Optional: without it, Dynamic Boost is
# disabled and the chain uses automatic headroom instead to avoid clipping.
LIMITER_PATHS = [
    "/usr/lib/ladspa/fast_lookahead_limiter_1913.so",
    "/usr/lib64/ladspa/fast_lookahead_limiter_1913.so",
]

EQ_BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_RANGE = 12.0

# FxSound-style effects, each 0..10.
EFFECTS = [
    ("clarity", "Clarity", "Presence lift around 3 kHz for crisper vocals"),
    ("ambience", "Ambience", "Adds a subtle sense of room"),
    ("surround", "Surround", "Widens the stereo image"),
    ("boost", "Dynamic Boost", "Louder without clipping (needs the limiter)"),
    ("bass", "Bass Boost", "Low-shelf lift around 90 Hz"),
    ("fidelity", "Fidelity", "High-shelf sparkle above 8 kHz"),
]

# Preset tuning is grounded in FxSound's own factory presets (fxsound2/fxsound-app,
# Installer/Resources/Factsoft/*.fac, sliders rescaled 0-127 -> 0-10) and common
# EQ guidance: Harman listener-preference research for music, dialogue-band
# advice for movies, footstep-band advice for gaming, and podcast/voice EQ
# practice. EQ bands: 31 62 125 250 500 1k 2k 4k 8k 16k Hz.
PRESETS = {
    "Flat": {"effects": {}, "eq": [0] * 10},
    # Harman-style: a gentle bass shelf (most listeners prefer ~+4-6 dB below
    # ~150 Hz on headphones; the Bass effect adds a 90 Hz shelf on top), neutral
    # mids, a small dip at the 4-5 kHz harshness zone and a little air, as in
    # FxSound's Music preset (-1 dB @4.5k, +2 dB @16k).
    "Music": {
        "effects": {"clarity": 3, "ambience": 3, "surround": 3, "boost": 3, "bass": 4, "fidelity": 3},
        "eq": [1, 2, 1, 0, 0, 0, 0, -1, 0, 2],
    },
    # Dialogue lives roughly 250 Hz-4 kHz with intelligibility around 2 kHz;
    # voices carry little below ~200 Hz, so trim the 125 Hz boom while keeping
    # sub-bass for effects. Small dip near 8 kHz against sibilance. Dynamic
    # Boost evens out quiet dialogue vs. loud effects (FxSound Movies: 7/10).
    "Movies": {
        "effects": {"clarity": 5, "ambience": 2, "surround": 4, "boost": 7, "bass": 3, "fidelity": 2},
        "eq": [3, 2, -1, 0, 0, 1, 2, 2, -1, 1],
    },
    # Footsteps and reloads sit around 250 Hz-4 kHz with directional detail in
    # 6-9 kHz; cut the 60-250 Hz rumble of explosions, lift 2-4 kHz and a
    # little 8 kHz. No ambience (reverb smears positional cues); Dynamic Boost
    # raises quiet sounds (FxSound Gaming: 7/10).
    "Gaming": {
        "effects": {"clarity": 4, "ambience": 0, "surround": 2, "boost": 6, "bass": 0, "fidelity": 3},
        "eq": [-3, -2, -1, -1, 0, 1, 3, 3, 2, 1],
    },
    # Podcasts/calls: steep low cut (approximating an 80-100 Hz high-pass),
    # 150-400 Hz mud trimmed, +2-3 dB presence at 2-4 kHz, no 8 kHz lift
    # (sibilance) and a top-end cut against hiss, as FxSound's Voice preset
    # does (-4 dB @115 Hz, -11 dB @11.8 kHz, 6/10 clarity, 7/10 boost).
    "Voice": {
        "effects": {"clarity": 6, "ambience": 0, "surround": 0, "boost": 7, "bass": 0, "fidelity": 0},
        "eq": [-8, -6, -3, -2, 0, 1, 2, 3, 0, -3],
    },
    # Lift 60-100 Hz (the Bass effect's 90 Hz shelf plus EQ, ~+8 dB at 62 Hz),
    # keep the felt-not-heard <30 Hz region modest, and cut around 250 Hz so
    # the boost stays tight instead of muddy. FxSound Bass Boost: 6/10 bass.
    "Bass Boost": {
        "effects": {"clarity": 3, "ambience": 2, "surround": 2, "boost": 3, "bass": 6, "fidelity": 2},
        "eq": [1, 2, 1, -2, 0, 0, 0, 0, 0, 1],
    },
}

DEFAULTS = {
    "enabled": False,
    "device": "",
    "preset": "Music",
    "effects": {k: 0 for k, _, _ in EFFECTS},
    "eq": [0.0] * 10,
    # Headphone correction imported from autoeq.app:
    # {"name": str, "preamp": dB, "filters": [{"type", "freq", "gain", "q"}]}
    "correction": None,
    "correction_enabled": True,
    "autostart": True,             # launch at login
    "autostart_background": True,  # ...straight to the tray, no window
    "check_updates": True,
    "last_update_check": 0,
}

MAX_CORRECTION_FILTERS = 32
CORRECTION_LABELS = {"peak": "bq_peaking", "lowshelf": "bq_lowshelf", "highshelf": "bq_highshelf"}
DEFAULTS["effects"].update(PRESETS["Music"]["effects"])
DEFAULTS["eq"] = [float(v) for v in PRESETS["Music"]["eq"]]


def limiter_path():
    return next((p for p in LIMITER_PATHS if os.path.exists(p)), None)


# --------------------------------------------------------------- settings

def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        with open(SETTINGS_PATH) as f:
            saved = json.load(f)
        s.update({k: v for k, v in saved.items() if k in s})
        s["effects"] = {**DEFAULTS["effects"], **saved.get("effects", {})}
        eq = [float(v) for v in saved.get("eq", s["eq"])][:10]
        s["eq"] = eq + [0.0] * (10 - len(eq))
        s["correction"] = clean_correction(saved.get("correction"))
    except (OSError, ValueError, TypeError):
        pass
    return s


def save_settings(s):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


# ---------------------------------------------------- autoeq.app import

# Equalizer APO filter types AutoEQ emits, mapped to our biquads. Low/high
# pass are skipped: unlike these, they aren't neutral at 0 dB.
APO_TYPES = {"PK": "peak", "PEQ": "peak", "MODAL": "peak",
             "LS": "lowshelf", "LSC": "lowshelf", "HS": "highshelf", "HSC": "highshelf"}
NUM = r"[-+]?\d+(?:[.,]\d+)?"


def _num(v):
    return float(v.replace(",", "."))


def clean_correction(c):
    """Validate a stored/parsed correction; None if unusable."""
    if not isinstance(c, dict):
        return None
    filters = []
    for f in c.get("filters") or []:
        try:
            kind = f["type"] if f["type"] in CORRECTION_LABELS else None
            freq, gain, q = float(f["freq"]), float(f["gain"]), float(f["q"])
        except (KeyError, TypeError, ValueError):
            continue
        if kind and 10 <= freq <= 24000 and abs(gain) <= 30 and 0.05 <= q <= 20:
            filters.append({"type": kind, "freq": freq, "gain": gain, "q": q})
    if not filters:
        return None
    try:
        preamp = max(-30.0, min(0.0, float(c.get("preamp", 0))))
    except (TypeError, ValueError):
        preamp = 0.0
    return {"name": str(c.get("name") or "Imported profile")[:120],
            "preamp": preamp, "filters": filters[:MAX_CORRECTION_FILTERS]}


def parse_autoeq(text, name="Imported profile"):
    """Parse autoeq.app output: ParametricEQ / FixedBandEQ (Equalizer APO
    "Filter N: ON PK Fc 105 Hz Gain 2.4 dB Q 0.70" lines plus "Preamp:"), or
    GraphicEQ ("GraphicEQ: 20 -1.2; 21 -1.1; ..."). Returns (correction,
    skipped_count) or raises ValueError."""
    preamp, filters, skipped = 0.0, [], 0
    saw_preamp = graphic = False
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(rf"(?i)preamp\s*:\s*({NUM})\s*db", line)
        if m:
            preamp += _num(m.group(1))
            saw_preamp = True
            continue
        m = re.match(r"(?i)graphiceq\s*:\s*(.+)", line)
        if m:
            points = []
            for pair in m.group(1).split(";"):
                parts = pair.split()
                if len(parts) == 2:
                    try:
                        points.append((_num(parts[0]), _num(parts[1])))
                    except ValueError:
                        pass
            filters.extend(graphic_to_filters(points))
            graphic = True
            continue
        m = re.match(rf"(?i)filter\s*\d*\s*:\s*(on|off)\s+([a-z]+)\s+fc\s+({NUM})\s*hz"
                     rf"(?:\s+gain\s+({NUM})\s*db)?(?:\s+q\s+({NUM}))?", line)
        if not m:
            continue
        if m.group(1).lower() == "off":
            continue
        kind = APO_TYPES.get(m.group(2).upper())
        if not kind or m.group(4) is None:
            skipped += 1
            continue
        q = _num(m.group(5)) if m.group(5) else (1.41 if kind == "peak" else 0.71)
        filters.append({"type": kind, "freq": _num(m.group(3)), "gain": _num(m.group(4)), "q": q})

    if graphic and not saw_preamp:
        # GraphicEQ exports carry no preamp; leave room for the largest boost.
        preamp = -max([0.0] + [f["gain"] for f in filters])
    if len(filters) > MAX_CORRECTION_FILTERS:
        skipped += len(filters) - MAX_CORRECTION_FILTERS
    corr = clean_correction({"name": name, "preamp": preamp, "filters": filters})
    if not corr:
        raise ValueError("No AutoEQ filters found. Export \"Parametric EQ\" (Equalizer APO) "
                         "or \"Graphic EQ\" from autoeq.app.")
    return corr, skipped


def graphic_to_filters(points):
    """Approximate a GraphicEQ curve with one peaking filter per EQ band,
    sampling the curve at each band (log-frequency interpolation)."""
    points = sorted(p for p in points if p[0] > 0)
    if len(points) < 2:
        return []
    out = []
    for f in EQ_BANDS:
        if f <= points[0][0]:
            g = points[0][1]
        elif f >= points[-1][0]:
            g = points[-1][1]
        else:
            for (f0, g0), (f1, g1) in zip(points, points[1:]):
                if f0 <= f <= f1:
                    t = (math.log(f) - math.log(f0)) / (math.log(f1) - math.log(f0))
                    g = g0 + t * (g1 - g0)
                    break
        out.append({"type": "peak", "freq": float(f), "gain": round(g, 2), "q": 1.41})
    return out


def profile_name_from_path(path):
    base = os.path.splitext(os.path.basename(path))[0]
    base = re.sub(r"(?i)[\s_-]*(parametric|fixed\s*band|graphic)\s*eq$", "", base)
    return base.strip(" _-") or "Imported profile"


# --------------------------------------------------------- effect mapping

def controls(s, has_limiter):
    """Map UI values to filter-chain control values, per node name."""
    e = s["effects"]
    eq = s["eq"]
    bass_db = e["bass"] * 1.0          # 0..10 dB low shelf
    clarity_db = e["clarity"] * 0.6    # 0..6 dB at 3 kHz
    fidelity_db = e["fidelity"] * 0.6  # 0..6 dB high shelf
    width = 1.0 + e["surround"] * 0.08  # side gain 1.0..1.8
    amb = e["ambience"] * 0.035         # cross-feed echo 0..0.35

    # Worst-case boost the chain can add, used for headroom when there is
    # no limiter to catch overs.
    peak = max([0.0] + [bass_db + max(eq[0], eq[1], eq[2], 0),
                        clarity_db + max(eq[6], eq[7], 0),
                        fidelity_db + max(eq[8], eq[9], 0)] + list(eq))
    peak_lin = 10 ** (peak / 20)
    # Mid/side widening's worst-case per-channel gain is |(1+w)/2| + |(1-w)/2| = w.
    total = peak_lin * width * (1 + amb)

    corr = s.get("correction")
    corr_on = bool(corr) and s.get("correction_enabled", True)

    if has_limiter:
        pre = 1.0
    else:
        # AutoEQ's preamp already covers its own boosts.
        pre = 1.0 / total
        if corr_on:
            pre *= 10 ** (corr["preamp"] / 20)

    c = {}
    for ch in ("l", "r"):
        c[f"pre_{ch}"] = {"Mult": pre}
        for i, f in enumerate(corr["filters"] if corr else []):
            # Off = 0 dB, which leaves every allowed filter type neutral, so
            # the toggle works live without rebuilding the graph.
            c[f"corr{i}_{ch}"] = {"Freq": f["freq"], "Q": f["q"],
                                  "Gain": f["gain"] if corr_on else 0.0}
        c[f"bass_{ch}"] = {"Freq": 90.0, "Q": 0.7, "Gain": bass_db}
        for i, f in enumerate(EQ_BANDS):
            c[f"eq{i}_{ch}"] = {"Freq": float(f), "Q": 1.2, "Gain": float(eq[i])}
        c[f"clarity_{ch}"] = {"Freq": 3000.0, "Q": 0.9, "Gain": clarity_db}
        c[f"fid_{ch}"] = {"Freq": 8000.0, "Q": 0.7, "Gain": fidelity_db}
        # Mid/side widening: out = M + w*S, written as a 2-input mix.
        c[f"wide_{ch}"] = {"Gain 1": (1 + width) / 2, "Gain 2": (1 - width) / 2}
        c[f"amb_lp_{ch}"] = {"Freq": 4500.0, "Q": 0.7}
        c[f"out_{ch}"] = {"Gain 1": 1.0, "Gain 2": amb}
    if has_limiter:
        c["limit"] = {
            "Input gain (dB)": e["boost"] * 1.2,  # 0..12 dB into the limiter
            "Limit (dB)": -1.0,
            "Release time (s)": 0.15,
        }
    return c


def build_chain(s, device):
    lim = limiter_path()
    ctl = controls(s, lim is not None)
    nodes, links = [], []

    def node(name, label, kind="builtin", **extra):
        n = {"type": kind, "name": name, "label": label}
        if name in ctl:
            n["control"] = ctl[name]
        n.update(extra)
        nodes.append(n)

    def link(a, b):
        links.append({"output": a, "input": b})

    corr = s.get("correction")
    corr_nodes = len(corr["filters"]) if corr else 0
    for ch in ("l", "r"):
        node(f"pre_{ch}", "linear")
        for i in range(corr_nodes):
            node(f"corr{i}_{ch}", CORRECTION_LABELS[corr["filters"][i]["type"]])
        node(f"bass_{ch}", "bq_lowshelf")
        for i in range(len(EQ_BANDS)):
            node(f"eq{i}_{ch}", "bq_peaking")
        node(f"clarity_{ch}", "bq_peaking")
        node(f"fid_{ch}", "bq_highshelf")
        node(f"wide_{ch}", "mixer")
        node(f"amb_dly_{ch}", "delay", config={"max-delay": 0.1},
             control={"Delay (s)": 0.013 if ch == "l" else 0.019})
        node(f"amb_lp_{ch}", "bq_lowpass")
        node(f"out_{ch}", "mixer")

        chain = [f"pre_{ch}"] + [f"corr{i}_{ch}" for i in range(corr_nodes)] + [f"bass_{ch}"] \
            + [f"eq{i}_{ch}" for i in range(len(EQ_BANDS))] + [f"clarity_{ch}", f"fid_{ch}"]
        for a, b in zip(chain, chain[1:]):
            link(f"{a}:Out", f"{b}:In")

    for ch, other in (("l", "r"), ("r", "l")):
        link(f"fid_{ch}:Out", f"wide_{ch}:In 1")
        link(f"fid_{other}:Out", f"wide_{ch}:In 2")
        # Ambience: the opposite channel, delayed and darkened, mixed in.
        link(f"wide_{other}:Out", f"amb_dly_{ch}:In")
        link(f"amb_dly_{ch}:Out", f"amb_lp_{ch}:In")
        link(f"wide_{ch}:Out", f"out_{ch}:In 1")
        link(f"amb_lp_{ch}:Out", f"out_{ch}:In 2")

    if lim:
        node("limit", "fastLookaheadLimiter", kind="ladspa", plugin=lim)
        link("out_l:Out", "limit:Input 1")
        link("out_r:Out", "limit:Input 2")
        outputs = ["limit:Output 1", "limit:Output 2"]
    else:
        outputs = ["out_l:Out", "out_r:Out"]

    playback = {"node.name": OUTPUT_NAME, "node.passive": True,
                "node.description": "VibeFX Output"}
    if device:
        playback["target.object"] = device

    module = {
        "name": "libpipewire-module-filter-chain",
        "args": {
            "node.description": "VibeFX",
            "media.name": "VibeFX",
            "filter.graph": {
                "nodes": nodes,
                "links": links,
                "inputs": ["pre_l:In", "pre_r:In"],
                "outputs": outputs,
            },
            "audio.channels": 2,
            "audio.position": ["FL", "FR"],
            # Never suspend: a suspended filter-chain drops live Props
            # changes, so sliders moved during silence would be lost.
            "capture.props": {"node.name": SINK_NAME, "media.class": "Audio/Sink",
                              "node.description": "VibeFX",
                              "session.suspend-timeout-seconds": 0,
                              "node.always-process": True},
            "playback.props": playback,
        },
    }
    sections = {
        "context.properties": {"log.level": 0},
        "context.spa-libs": {"audio.convert.*": "audioconvert/libspa-audioconvert",
                             "support.*": "support/libspa-support"},
        "context.modules": [
            {"name": "libpipewire-module-rt", "args": {}, "flags": ["ifexists", "nofail"]},
            {"name": "libpipewire-module-protocol-native"},
            {"name": "libpipewire-module-client-node"},
            {"name": "libpipewire-module-adapter"},
            module,
        ],
    }
    return "# Generated by VibeFX. Edits are overwritten.\n" + "".join(
        f"{k} = {json.dumps(v, indent=2)}\n" for k, v in sections.items())


def write_chain(s):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CHAIN_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(build_chain(s, s.get("device", "")))
    os.replace(tmp, CHAIN_PATH)


def write_unit():
    os.makedirs(os.path.dirname(UNIT_PATH), exist_ok=True)
    unit = f"""[Unit]
Description=VibeFX audio enhancer (PipeWire filter-chain)
After=pipewire.service wireplumber.service
BindsTo=pipewire.service

[Service]
ExecStart={shutil.which("pipewire") or "/usr/bin/pipewire"} -c {CHAIN_PATH}
Restart=on-failure
"""
    try:
        with open(UNIT_PATH) as f:
            if f.read() == unit:
                return
    except OSError:
        pass
    with open(UNIT_PATH, "w") as f:
        f.write(unit)
    run(["systemctl", "--user", "daemon-reload"])


# ------------------------------------------------------ startup + updates

def launcher_path():
    installed = os.path.expanduser("~/.local/bin/vibefx")
    return installed if os.path.exists(installed) else os.path.realpath(sys.argv[0])


def apply_autostart(s):
    """Create or remove the XDG autostart entry to match the settings."""
    if not s.get("autostart", True):
        try:
            os.remove(AUTOSTART_PATH)
        except FileNotFoundError:
            pass
        return
    cmd = launcher_path() + (" --background" if s.get("autostart_background", True) else "")
    entry = f"""[Desktop Entry]
Type=Application
Name=VibeFX
Comment=FxSound-style audio enhancement for PipeWire
Exec={cmd}
Icon=vibefx
Terminal=false
X-GNOME-Autostart-enabled=true
"""
    os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
    tmp = AUTOSTART_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(entry)
    os.replace(tmp, AUTOSTART_PATH)


def parse_version(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


def updates_configured():
    return "OWNER" not in GITHUB_REPO


def fetch_latest_release():
    """(version, tarball_url, page_url) of the newest GitHub release."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"VibeFX/{VERSION}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read(1_000_000))
    tag = str(data.get("tag_name") or "")
    if not tag or not data.get("tarball_url"):
        raise ValueError("release has no tag or source archive")
    return tag.lstrip("vV"), data["tarball_url"], data.get("html_url", "")


def install_update(tarball_url):
    """Download a release's source archive and run its install.sh."""
    req = urllib.request.Request(tarball_url, headers={"User-Agent": f"VibeFX/{VERSION}"})
    with tempfile.TemporaryDirectory(prefix="vibefx-update-") as tmp:
        archive = os.path.join(tmp, "src.tar.gz")
        with urllib.request.urlopen(req, timeout=30) as r, open(archive, "wb") as f:
            total = 0
            while chunk := r.read(65536):
                total += len(chunk)
                if total > MAX_UPDATE_BYTES:
                    raise ValueError("update archive is unexpectedly large")
                f.write(chunk)
        src = os.path.join(tmp, "src")
        with tarfile.open(archive) as tf:
            tf.extractall(src, filter="data")
        tops = [os.path.join(src, d) for d in os.listdir(src)]
        root = next((d for d in tops if os.path.isfile(os.path.join(d, "install.sh"))
                     and os.path.isfile(os.path.join(d, "vibefx.py"))), None)
        if not root:
            raise ValueError("archive does not contain VibeFX")
        r = subprocess.run(["bash", os.path.join(root, "install.sh")], cwd=root,
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "install.sh failed").strip()[-300:])


# ----------------------------------------------------------- pipewire glue

def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def pw_nodes():
    r = run(["pw-dump"])
    if not r or r.returncode != 0:
        return []
    try:
        objs = json.loads(r.stdout)
    except ValueError:
        return []
    return [o for o in objs if o.get("type") == "PipeWire:Interface:Node"]


def node_props(o):
    return (o.get("info") or {}).get("props") or {}


def output_devices():
    """Real sinks as [(node.name, description)], excluding VibeFX itself."""
    out = []
    for o in pw_nodes():
        p = node_props(o)
        if p.get("media.class") == "Audio/Sink" and p.get("node.name") != SINK_NAME:
            out.append((p.get("node.name", ""), p.get("node.description") or p.get("node.name", "")))
    return out


def find_node_id(name):
    for o in pw_nodes():
        if node_props(o).get("node.name") == name:
            return o.get("id")
    return None


def default_sink_name():
    r = run(["pw-metadata", "0", "default.audio.sink"])
    if not r:
        return ""
    for line in r.stdout.splitlines():
        if "value:" in line and "name" in line:
            try:
                val = line.split("value:'", 1)[1].rsplit("' type:", 1)[0]
                return json.loads(val).get("name", "")
            except (IndexError, ValueError):
                pass
    return ""


def set_default_sink(name):
    nid = find_node_id(name)
    if nid is not None:
        run(["wpctl", "set-default", str(nid)])


def spa_value(v):
    return f"{float(v):.6f}"


# --------------------------------------------------------------------- UI

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="VibeFX")
        self.set_default_size(460, 760)
        self.s = load_settings()
        self.has_limiter = limiter_path() is not None
        self.sink_id = None
        self.pending = False
        self.updating = False
        self.sliders = {}
        self.eq_sliders = []

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.power = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Enable effects")
        header.pack_start(self.power)
        reset = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Reset to Flat")
        reset.connect("clicked", lambda *_: self.apply_preset("Flat"))
        header.pack_end(reset)
        tv.add_top_bar(header)
        self.update_banner = Adw.Banner(button_label="Update")
        self.update_banner.connect("button-clicked", self.on_install_update)
        tv.add_top_bar(self.update_banner)
        self.latest = None  # (version, tarball_url, page_url) when newer

        self.toasts = Adw.ToastOverlay()
        page = Adw.PreferencesPage()
        self.toasts.set_child(page)
        tv.set_content(self.toasts)
        self.set_content(tv)

        # Output + preset
        g = Adw.PreferencesGroup(title="Output")
        self.device_row = Adw.ComboRow(title="Play through", subtitle="Your speakers or headphones")
        self.devices = []
        self.refresh_devices()
        self.device_row.connect("notify::selected", self.on_device)
        g.add(self.device_row)

        self.preset_row = Adw.ComboRow(title="Preset")
        names = list(PRESETS) + ["Custom"]
        self.preset_row.set_model(Gtk.StringList.new(names))
        self.preset_row.set_selected(names.index(self.s["preset"]) if self.s["preset"] in names else len(names) - 1)
        self.preset_row.connect("notify::selected", self.on_preset)
        g.add(self.preset_row)
        page.add(g)

        # Headphone correction (autoeq.app)
        g = Adw.PreferencesGroup(
            title="Headphone Correction",
            description="From autoeq.app: export “Parametric EQ” (Equalizer APO), "
                        "then import the file or paste the text.")
        actions = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        imp = Gtk.Button(label="Import…")
        imp.connect("clicked", self.on_import_file)
        paste = Gtk.Button(icon_name="edit-paste-symbolic", tooltip_text="Paste AutoEQ text")
        paste.connect("clicked", self.on_paste)
        actions.append(imp)
        actions.append(paste)
        g.set_header_suffix(actions)

        self.corr_row = Adw.SwitchRow()
        self.corr_row.set_active(self.s.get("correction_enabled", True))
        self.corr_row.connect("notify::active", self.on_correction_toggle)
        self.corr_remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove profile",
                                      valign=Gtk.Align.CENTER)
        self.corr_remove.add_css_class("flat")
        self.corr_remove.connect("clicked", self.on_correction_remove)
        self.corr_row.add_suffix(self.corr_remove)
        g.add(self.corr_row)
        page.add(g)
        self.update_correction_row()

        # Effects
        g = Adw.PreferencesGroup(title="Effects")
        for key, label, desc in EFFECTS:
            row = Adw.ActionRow(title=label, subtitle=desc)
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 10, 1)
            sc.set_hexpand(True)
            sc.set_size_request(170, -1)
            sc.set_draw_value(True)
            sc.set_value_pos(Gtk.PositionType.RIGHT)
            sc.set_digits(0)
            sc.set_round_digits(0)
            sc.set_value(self.s["effects"].get(key, 0))
            sc.connect("value-changed", self.on_effect, key)
            if key == "boost" and not self.has_limiter:
                sc.set_sensitive(False)
                row.set_subtitle("Install swh-plugins to enable the limiter")
            row.add_suffix(sc)
            g.add(row)
            self.sliders[key] = sc
        page.add(g)

        # Equalizer
        g = Adw.PreferencesGroup(title="Equalizer", description=f"±{int(EQ_RANGE)} dB")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2, homogeneous=True,
                      margin_top=12, margin_bottom=12, margin_start=6, margin_end=6)
        box.add_css_class("card")
        for i, f in enumerate(EQ_BANDS):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=10, margin_bottom=10)
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -EQ_RANGE, EQ_RANGE, 0.5)
            sc.set_inverted(True)
            sc.set_vexpand(True)
            sc.set_size_request(-1, 180)
            sc.set_draw_value(False)
            sc.add_mark(0, Gtk.PositionType.RIGHT, None)
            sc.set_value(self.s["eq"][i])
            sc.connect("value-changed", self.on_eq, i)
            lbl = Gtk.Label(label=f"{f // 1000}k" if f >= 1000 else str(f))
            lbl.add_css_class("caption")
            col.append(sc)
            col.append(lbl)
            box.append(col)
            self.eq_sliders.append(sc)
        g.add(box)
        page.add(g)

        # General
        g = Adw.PreferencesGroup(title="General")
        self.autostart_row = Adw.SwitchRow(title="Launch at startup")
        self.autostart_row.set_active(self.s.get("autostart", True))
        self.autostart_row.connect("notify::active", self.on_autostart)
        g.add(self.autostart_row)
        self.bg_row = Adw.SwitchRow(title="Start in the background",
                                    subtitle="At login, only show the tray icon")
        self.bg_row.set_active(self.s.get("autostart_background", True))
        self.bg_row.set_sensitive(self.autostart_row.get_active())
        self.bg_row.connect("notify::active", self.on_autostart)
        g.add(self.bg_row)
        self.updates_row = Adw.SwitchRow(title="Check for updates automatically")
        self.updates_row.set_active(self.s.get("check_updates", True))
        self.updates_row.connect("notify::active", self.on_check_updates_toggle)
        g.add(self.updates_row)
        self.version_row = Adw.ActionRow(title=f"VibeFX {VERSION}")
        self.check_btn = Gtk.Button(label="Check now", valign=Gtk.Align.CENTER)
        self.check_btn.connect("clicked", lambda *_: self.check_updates(manual=True))
        self.version_row.add_suffix(self.check_btn)
        g.add(self.version_row)
        page.add(g)
        self.set_version_status()
        apply_autostart(self.s)
        # Automatic checks: shortly after start, then periodically while running.
        GLib.timeout_add_seconds(8, self.maybe_check_updates)
        GLib.timeout_add_seconds(6 * 3600, self.maybe_check_updates)

        self.power.set_active(self.s["enabled"])
        self.power.connect("notify::active", self.on_power)
        if self.s["enabled"]:
            GLib.idle_add(self.enable)

    # ---- helpers
    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=msg, timeout=3))

    def refresh_devices(self):
        self.devices = output_devices()
        if not self.s["device"]:
            cur = default_sink_name()
            self.s["device"] = cur if cur and cur != SINK_NAME else (self.devices[0][0] if self.devices else "")
        names = [d[1] for d in self.devices]
        idx = next((i for i, d in enumerate(self.devices) if d[0] == self.s["device"]), 0)
        self.updating = True
        self.device_row.set_model(Gtk.StringList.new(names))
        if names:
            self.device_row.set_selected(idx)
        self.updating = False

    def mark_custom(self):
        if self.updating:
            return
        changed = self.s["preset"] != "Custom"
        self.s["preset"] = "Custom"
        self.updating = True
        self.preset_row.set_selected(len(PRESETS))
        self.updating = False
        if changed:
            self.get_application().state_changed()

    # ---- signal handlers
    def on_effect(self, sc, key):
        self.s["effects"][key] = int(round(sc.get_value()))
        self.mark_custom()
        self.schedule_push()

    def on_eq(self, sc, i):
        self.s["eq"][i] = round(sc.get_value() * 2) / 2
        self.mark_custom()
        self.schedule_push()

    def on_preset(self, row, _):
        if self.updating:
            return
        names = list(PRESETS) + ["Custom"]
        name = names[row.get_selected()]
        if name != "Custom":
            self.apply_preset(name)

    def apply_preset(self, name):
        p = PRESETS[name]
        self.updating = True
        for key, _, _ in EFFECTS:
            self.s["effects"][key] = p["effects"].get(key, 0)
            self.sliders[key].set_value(self.s["effects"][key])
        for i, v in enumerate(p["eq"]):
            self.s["eq"][i] = float(v)
            self.eq_sliders[i].set_value(v)
        self.s["preset"] = name
        self.preset_row.set_selected(list(PRESETS).index(name))
        self.updating = False
        self.schedule_push()
        self.get_application().state_changed()

    def on_device(self, row, _):
        if self.updating or not self.devices:
            return
        self.s["device"] = self.devices[row.get_selected()][0]
        if self.s["enabled"]:
            self.restart_engine()
        else:
            save_settings(self.s)
            write_chain(self.s)
            set_default_sink(self.s["device"])

    # ---- startup + updates
    def on_autostart(self, *_):
        self.s["autostart"] = self.autostart_row.get_active()
        self.s["autostart_background"] = self.bg_row.get_active()
        self.bg_row.set_sensitive(self.s["autostart"])
        save_settings(self.s)
        try:
            apply_autostart(self.s)
        except OSError as e:
            self.toast(f"Could not update startup entry: {e}")

    def on_check_updates_toggle(self, row, _):
        self.s["check_updates"] = row.get_active()
        save_settings(self.s)

    def set_version_status(self, text=None):
        if text is None:
            if not updates_configured():
                text = "Update source not configured"
            elif self.s.get("last_update_check"):
                when = time.strftime("%b %-d, %-I:%M %p", time.localtime(self.s["last_update_check"]))
                text = f"Up to date · checked {when}"
            else:
                text = "Not checked yet"
        self.version_row.set_subtitle(text)

    def maybe_check_updates(self):
        if self.s.get("check_updates", True) and \
                time.time() - self.s.get("last_update_check", 0) >= UPDATE_INTERVAL:
            self.check_updates(manual=False)
        return True  # keep the periodic timer

    def check_updates(self, manual):
        if not updates_configured():
            if manual:
                self.toast("Update source not configured")
            return
        self.check_btn.set_sensitive(False)
        self.set_version_status("Checking…")

        def work():
            try:
                result = fetch_latest_release()
                err = None
            except Exception as e:  # network, HTTP 404 (no releases yet), bad JSON
                result, err = None, e
            GLib.idle_add(self._update_checked, result, err, manual)

        threading.Thread(target=work, daemon=True).start()

    def _update_checked(self, result, err, manual):
        self.check_btn.set_sensitive(True)
        if err is not None:
            self.set_version_status("Could not check for updates")
            if manual:
                self.toast(f"Update check failed: {err}")
            return False
        self.s["last_update_check"] = int(time.time())
        save_settings(self.s)
        version, tarball, page = result
        if parse_version(version) > parse_version(VERSION):
            self.latest = result
            self.set_version_status(f"Version {version} is available")
            self.update_banner.set_title(f"VibeFX {version} is available")
            self.update_banner.set_revealed(True)
            if not self.is_visible():
                self.get_application().notify_update(version)
        else:
            self.latest = None
            self.update_banner.set_revealed(False)
            self.set_version_status()
            if manual:
                self.toast("VibeFX is up to date")
        return False

    def on_install_update(self, _banner):
        if not self.latest:
            return
        version, tarball, _page = self.latest
        self.update_banner.set_button_label("")
        self.update_banner.set_title(f"Installing VibeFX {version}…")

        def work():
            try:
                install_update(tarball)
                err = None
            except Exception as e:
                err = e
            GLib.idle_add(self._update_installed, version, err)

        threading.Thread(target=work, daemon=True).start()

    def _update_installed(self, version, err):
        if err is not None:
            self.update_banner.set_title(f"Update failed: {err}")
            self.update_banner.set_button_label("Retry")
            return False
        self.update_banner.set_title(f"Updated to {version} — restarting…")
        GLib.timeout_add(800, self.get_application().restart)
        return False

    # ---- headphone correction
    def update_correction_row(self):
        c = self.s.get("correction")
        if c:
            self.corr_row.set_title(c["name"])
            self.corr_row.set_subtitle(f"{len(c['filters'])} filters · preamp {c['preamp']:+.1f} dB")
        else:
            self.corr_row.set_title("No profile loaded")
            self.corr_row.set_subtitle("Corrects your headphones toward a neutral target")
        self.corr_row.set_sensitive(bool(c))
        self.corr_remove.set_visible(bool(c))

    def load_correction(self, text, name):
        try:
            corr, skipped = parse_autoeq(text, name)
        except ValueError as e:
            self.toast(str(e))
            return
        self.s["correction"] = corr
        self.s["correction_enabled"] = True
        self.corr_row.set_active(True)
        self.update_correction_row()
        self.restart_engine()
        msg = f"Loaded {corr['name']}"
        if skipped:
            msg += f" ({skipped} unsupported filter{'s' if skipped != 1 else ''} skipped)"
        self.toast(msg)

    def on_import_file(self, _btn):
        dlg = Gtk.FileDialog(title="Import AutoEQ profile")
        filt = Gtk.FileFilter(name="AutoEQ / Equalizer APO text")
        filt.add_pattern("*.txt")
        filt.add_mime_type("text/plain")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(filt)
        dlg.set_filters(store)
        dlg.set_default_filter(filt)
        downloads = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if downloads:
            dlg.set_initial_folder(Gio.File.new_for_path(downloads))
        dlg.open(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dlg, result):
        try:
            f = dlg.open_finish(result)
        except GLib.Error:
            return  # cancelled
        try:
            ok, data, _ = f.load_contents(None)
        except GLib.Error as e:
            self.toast(f"Could not read file: {e.message}")
            return
        if len(data) > 1_000_000:
            self.toast("That file is too large to be an AutoEQ profile")
            return
        self.load_correction(data.decode("utf-8", "replace"), profile_name_from_path(f.get_path() or f.get_basename()))

    def on_paste(self, _btn):
        self.get_clipboard().read_text_async(None, self._on_pasted)

    def _on_pasted(self, clipboard, result):
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error:
            text = None
        if not text:
            self.toast("Clipboard has no text — copy the profile from autoeq.app first")
            return
        self.load_correction(text[:1_000_000], "Pasted AutoEQ profile")

    def on_correction_toggle(self, row, _):
        self.s["correction_enabled"] = row.get_active()
        self.schedule_push()

    def on_correction_remove(self, _btn):
        self.s["correction"] = None
        self.update_correction_row()
        self.restart_engine()
        self.toast("Headphone correction removed")

    def restart_engine(self):
        """Apply changes that alter the graph's shape (device, correction
        filters): rewrite the config and restart the chain if it's running."""
        save_settings(self.s)
        write_chain(self.s)
        if self.s["enabled"]:
            run(["systemctl", "--user", "restart", SERVICE])
            self.sink_id = None
            GLib.timeout_add(150, self.wait_for_sink, 0)

    def on_power(self, sw, _):
        self.s["enabled"] = sw.get_active()
        save_settings(self.s)
        if self.s["enabled"]:
            self.enable()
        else:
            self.disable()
        self.get_application().state_changed()

    # ---- engine control
    def enable(self):
        cur = default_sink_name()
        if cur and cur != SINK_NAME and not self.s["device"]:
            self.s["device"] = cur
        write_chain(self.s)
        write_unit()
        # Started by the app (not systemd at login): the tray app owns the
        # engine's lifetime, so Quit really stops the enhancement.
        r = run(["systemctl", "--user", "start", SERVICE])
        if not r or r.returncode != 0:
            self.toast("Could not start the audio engine")
            self.power.set_active(False)
            return False
        self.sink_id = None
        GLib.timeout_add(150, self.wait_for_sink, 0)
        return False

    def wait_for_sink(self, tries):
        nid = find_node_id(SINK_NAME)
        if nid is not None:
            self.sink_id = nid
            run(["wpctl", "set-default", str(nid)])
            self.push()
            return False
        if tries > 40:
            self.toast("Audio engine did not come up — see: journalctl --user -u vibefx")
            return False
        GLib.timeout_add(150, self.wait_for_sink, tries + 1)
        return False

    def disable(self):
        self.stop_engine()

    def stop_engine(self):
        """Hand audio back to the real device and stop the chain."""
        if self.s["device"]:
            set_default_sink(self.s["device"])
        run(["systemctl", "--user", "stop", SERVICE])
        self.sink_id = None

    def schedule_push(self):
        if self.pending:
            return
        self.pending = True
        GLib.timeout_add(40, self.push)

    def push(self):
        """Apply current settings: live to the running chain, and to disk so a
        restart comes back the same."""
        self.pending = False
        save_settings(self.s)
        write_chain(self.s)
        if not self.s["enabled"]:
            return False
        if self.sink_id is None:
            self.sink_id = find_node_id(SINK_NAME)
            if self.sink_id is None:
                return False
        params = []
        for node, ctl in controls(self.s, self.has_limiter).items():
            for k, v in ctl.items():
                params.append(f'"{node}:{k}" {spa_value(v)}')
        cmd = ["pw-cli", "set-param", str(self.sink_id), "Props",
               "{ params = [ " + " ".join(params) + " ] }"]
        r = run(cmd)
        if not r or r.returncode != 0:
            self.sink_id = None  # node may have been restarted; re-resolve next time
        return False


# ------------------------------------------------------------------- tray
# GTK4 has no tray API, so this speaks the StatusNotifierItem protocol (what
# the Omarchy bar's tray hosts) plus com.canonical.dbusmenu for its menu.

SNI_XML = """<node>
<interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewTitle"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
</interface>
</node>"""

MENU_XML = """<node>
<interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg name="parentId" type="i" direction="in"/>
    <arg name="recursionDepth" type="i" direction="in"/>
    <arg name="propertyNames" type="as" direction="in"/>
    <arg name="revision" type="u" direction="out"/>
    <arg name="layout" type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg name="ids" type="ai" direction="in"/>
    <arg name="propertyNames" type="as" direction="in"/>
    <arg name="properties" type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg name="id" type="i" direction="in"/>
    <arg name="name" type="s" direction="in"/>
    <arg name="value" type="v" direction="out"/>
  </method>
  <method name="Event">
    <arg name="id" type="i" direction="in"/>
    <arg name="eventId" type="s" direction="in"/>
    <arg name="data" type="v" direction="in"/>
    <arg name="timestamp" type="u" direction="in"/>
  </method>
  <method name="EventGroup">
    <arg name="events" type="a(isvu)" direction="in"/>
    <arg name="idErrors" type="ai" direction="out"/>
  </method>
  <method name="AboutToShow">
    <arg name="id" type="i" direction="in"/>
    <arg name="needUpdate" type="b" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
    <arg name="ids" type="ai" direction="in"/>
    <arg name="updatesNeeded" type="ai" direction="out"/>
    <arg name="idErrors" type="ai" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated">
    <arg name="updatedProps" type="a(ia{sv})"/>
    <arg name="removedProps" type="a(ias)"/>
  </signal>
  <signal name="LayoutUpdated">
    <arg name="revision" type="u"/>
    <arg name="parent" type="i"/>
  </signal>
</interface>
</node>"""

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
ICON_DIR = os.path.join(GLib.get_user_data_dir(), "icons", "hicolor", "scalable", "apps")

M_OPEN, M_POWER, M_SEP1, M_PRESETS, M_SEP2, M_QUIT = 1, 2, 3, 4, 5, 6
M_PRESET_BASE = 100


def _v(sig, val):
    return GLib.Variant(sig, val)


def icon_pixmap(name, sizes=(22, 32, 48, 64)):
    """ARGB32 (network byte order) pixmaps, one per size. The tray gets the
    icon as pixels because Quickshell's tray resolves IconName only through
    the system icon theme, which doesn't include our user-installed icons."""
    return [p for size in sizes for p in _icon_pixmap(name, size)]


def _icon_pixmap(name, size):
    try:
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_DIR, name + ".svg"), size, size)
    except (GLib.Error, ValueError, ImportError):
        return []
    if not pb.get_has_alpha():
        pb = pb.add_alpha(False, 0, 0, 0)
    w, h, stride = pb.get_width(), pb.get_height(), pb.get_rowstride()
    src = pb.get_pixels()
    out = bytearray()
    for y in range(h):
        row = src[y * stride:y * stride + w * 4]
        for x in range(0, w * 4, 4):
            r, g, b, a = row[x:x + 4]
            out += bytes((a, r, g, b))
    return [(w, h, bytes(out))]


class Tray:
    def __init__(self, app):
        self.app = app
        self.conn = app.get_dbus_connection()
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.revision = 1
        self.pixmaps = {}
        if not self.conn:
            return
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        register = getattr(self.conn, "register_object_with_closures2", None) or self.conn.register_object
        register(SNI_PATH, sni, self._sni_call, self._sni_get, None)
        register(MENU_PATH, menu, self._menu_call, self._menu_get, None)
        Gio.bus_own_name_on_connection(self.conn, self.bus_name, Gio.BusNameOwnerFlags.NONE, None, None)
        # (Re)register whenever a tray host appears — e.g. after `omarchy restart shell`.
        Gio.bus_watch_name_on_connection(self.conn, "org.kde.StatusNotifierWatcher",
                                         Gio.BusNameWatcherFlags.NONE, self._register, None)

    def _register(self, conn, _name, _owner):
        conn.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                  "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                  _v("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE, -1, None, None)

    # ---- state
    def _state(self):
        win = self.app.window
        s = win.s if win else load_settings()
        return s.get("enabled", False), s.get("preset", "Custom")

    def _icon_name(self):
        return "vibefx-tray" if self._state()[0] else "vibefx-tray-off"

    def _tooltip(self):
        on, preset = self._state()
        return f"{'On' if on else 'Off'} · {preset}"

    def refresh(self):
        """Call after enable/preset changes so the bar and menu catch up."""
        if not self.conn:
            return
        for sig in ("NewIcon", "NewToolTip"):
            self.conn.emit_signal(None, SNI_PATH, "org.kde.StatusNotifierItem", sig, None)
        self.revision += 1
        self.conn.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                              _v("(ui)", (self.revision, 0)))

    # ---- StatusNotifierItem
    def _sni_get(self, _conn, _sender, _path, _iface, prop):
        name = self._icon_name()
        if name not in self.pixmaps:
            self.pixmaps[name] = icon_pixmap(name)
        values = {
            "Category": _v("s", "Hardware"),
            "Id": _v("s", "vibefx"),
            "Title": _v("s", "VibeFX"),
            "Status": _v("s", "Active"),
            "WindowId": _v("i", 0),
            # Empty name makes hosts use IconPixmap instead of a theme lookup.
            "IconName": _v("s", "" if self.pixmaps[name] else name),
            "IconThemePath": _v("s", os.path.join(GLib.get_user_data_dir(), "icons")),
            "IconPixmap": _v("a(iiay)", self.pixmaps[name]),
            "OverlayIconName": _v("s", ""),
            "AttentionIconName": _v("s", ""),
            "ToolTip": _v("(sa(iiay)ss)", (name, [], "VibeFX", self._tooltip())),
            "ItemIsMenu": _v("b", False),
            "Menu": _v("o", MENU_PATH),
        }
        return values.get(prop)

    def _sni_call(self, _conn, _sender, _path, _iface, method, _params, inv):
        if method == "Activate":
            GLib.idle_add(self.app.show_window)
        elif method == "SecondaryActivate":
            GLib.idle_add(self.app.toggle_enabled)
        inv.return_value(None)

    # ---- dbusmenu
    def _items(self):
        on, preset = self._state()
        presets = [
            (M_PRESET_BASE + i, {"label": _v("s", name), "toggle-type": _v("s", "radio"),
                                 "toggle-state": _v("i", 1 if name == preset else 0)}, [])
            for i, name in enumerate(PRESETS)
        ]
        return [
            (M_OPEN, {"label": _v("s", "Open VibeFX")}, []),
            (M_POWER, {"label": _v("s", "Effects"), "toggle-type": _v("s", "checkmark"),
                       "toggle-state": _v("i", 1 if on else 0)}, []),
            (M_SEP1, {"type": _v("s", "separator")}, []),
            (M_PRESETS, {"label": _v("s", "Preset"), "children-display": _v("s", "submenu")}, presets),
            (M_SEP2, {"type": _v("s", "separator")}, []),
            (M_QUIT, {"label": _v("s", "Quit VibeFX")}, []),
        ]

    def _find(self, items, wanted):
        for iid, props, kids in items:
            if iid == wanted:
                return iid, props, kids
            hit = self._find(kids, wanted)
            if hit:
                return hit
        return None

    def _pack(self, item, depth):
        iid, props, kids = item
        children = [] if depth == 0 else [
            _v("(ia{sv}av)", self._pack(k, depth - 1)) for k in kids]
        return (iid, props, children)

    def _menu_get(self, _conn, _sender, _path, _iface, prop):
        return {"Version": _v("u", 3), "TextDirection": _v("s", "ltr"),
                "Status": _v("s", "normal"), "IconThemePath": _v("as", [])}.get(prop)

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, inv):
        items = self._items()
        if method == "GetLayout":
            parent, depth, _names = params.unpack()
            root = (0, {"children-display": _v("s", "submenu")}, items)
            node = root if parent == 0 else (self._find(items, parent) or (parent, {}, []))
            inv.return_value(_v("(u(ia{sv}av))", (self.revision, self._pack(node, depth if depth >= 0 else 99))))
        elif method == "GetGroupProperties":
            ids, _names = params.unpack()
            out = []
            for iid in ids:
                hit = self._find(items, iid)
                if hit:
                    out.append((iid, hit[1]))
            inv.return_value(_v("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            iid, name = params.unpack()
            hit = self._find(items, iid)
            inv.return_value(_v("(v)", ((hit[1].get(name) if hit else None) or _v("s", ""),)))
        elif method == "Event":
            iid, event, _data, _ts = params.unpack()
            if event == "clicked":
                GLib.idle_add(self._clicked, iid)
            inv.return_value(None)
        elif method == "EventGroup":
            (events,) = params.unpack()
            for iid, event, _data, _ts in events:
                if event == "clicked":
                    GLib.idle_add(self._clicked, iid)
            inv.return_value(_v("(ai)", ([],)))
        elif method == "AboutToShow":
            inv.return_value(_v("(b)", (False,)))
        elif method == "AboutToShowGroup":
            inv.return_value(_v("(aiai)", ([], [])))
        else:
            inv.return_value(None)

    def _clicked(self, iid):
        if iid == M_OPEN:
            self.app.show_window()
        elif iid == M_POWER:
            self.app.toggle_enabled()
        elif iid == M_QUIT:
            self.app.quit_all()
        elif iid >= M_PRESET_BASE:
            names = list(PRESETS)
            if iid - M_PRESET_BASE < len(names):
                self.app.window.apply_preset(names[iid - M_PRESET_BASE])
        return False


class App(Adw.Application):
    def __init__(self, background=False):
        super().__init__(application_id=APP_ID)
        self.background = background
        self.window = None
        self.tray = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.hold()  # keep running in the tray with the window closed
        show = Gio.SimpleAction.new("show-window", None)
        show.connect("activate", lambda *_: self.show_window())
        self.add_action(show)
        self.window = Window(self)
        self.window.set_hide_on_close(True)
        self.tray = Tray(self)

    def do_activate(self):
        # Launched with --background (login autostart): stay in the tray. Any
        # later activation (launcher, tray click) shows the window.
        if self.background:
            self.background = False
            return
        self.show_window()

    def show_window(self):
        self.window.present()
        return False

    def toggle_enabled(self):
        self.window.power.set_active(not self.window.power.get_active())
        return False

    def state_changed(self):
        if self.tray:
            self.tray.refresh()

    def notify_update(self, version):
        n = Gio.Notification.new("VibeFX update available")
        n.set_body(f"Version {version} is ready to install. Open VibeFX to update.")
        n.set_default_action("app.show-window")
        self.send_notification("update", n)

    def restart(self):
        """Relaunch the (just updated) app. The engine keeps running, so audio
        doesn't drop; the new instance picks it up."""
        visible = self.window.is_visible()
        launcher = launcher_path()
        args = "" if visible else " --background"
        subprocess.Popen(
            ["sh", "-c", 'sleep 1; if command -v uwsm >/dev/null; then exec uwsm app -- "$0"' + args +
             '; else exec "$0"' + args + "; fi", launcher],
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.release()
        self.quit()
        return False

    def quit_all(self):
        """Quit = stop enhancing, like FxSound. The saved on/off choice is kept,
        so the next launch (e.g. at login) resumes where it left off."""
        self.window.stop_engine()
        self.release()
        self.quit()


def main():
    for tool in ("pipewire", "pw-cli", "pw-dump", "pw-metadata", "wpctl", "systemctl"):
        if not shutil.which(tool):
            print(f"vibefx: missing required tool: {tool}", file=sys.stderr)
            return 1
    if len(sys.argv) > 1 and sys.argv[1] == "--write-config":
        write_chain(load_settings())
        print(CHAIN_PATH)
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--sync-autostart":
        apply_autostart(load_settings())
        return 0
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"VibeFX {VERSION}")
        return 0
    background = "--background" in sys.argv[1:]
    return App(background=background).run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
