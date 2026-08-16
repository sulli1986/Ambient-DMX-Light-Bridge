#!/usr/bin/env python3
"""
Pixel Mapping to OSC
=============================
Reads a screen/monitor, samples colour zones, sends RGBW/RGBA/RGB/RGBWW/RGBAW
values to Lightkey via OSC in real time.

Run:
    python app.py
Then open: http://localhost:5000
"""

import os
import json
import time
import copy
import threading
import logging
import numpy as np
from pathlib import Path
from flask import Flask, render_template, request, jsonify

from tempo import TempoClock
from effects import EffectEngine, EFFECT_DEFS, resolve_group_fixtures
from actions import build_actions
from midi_control import MidiController

app = Flask(__name__)
log = logging.getLogger("pixel-mapping-osc")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CONFIG_FILE = Path("config.json")

# ---------------------------------------------------------------------------
# DEFAULT CONFIG
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "lightkey_host": "192.168.0.14",
    "lightkey_port": 21600,
    "monitor": 4,
    "sample_rate_fps": 15,
    "smoothing": 0.12,
    "min_brightness": 30,
    "colour_boost": 4.0,
    "white_mode": "min",
    "master_brightness": 255,
    "min_output": 40,
    "stage_floor_pct": 50,
    "fixtures": [],
    "bars": [],
    "static_controls": [],
    "groups": [],
    "scenes": [],
    "palettes": [],
    "tempo": {"bpm": 120},
    "midi": {
        "device": "",
        "enabled": False,
        "mappings": []
    },
    "kick_strobe": {
        "device": "",        # input device name substring, e.g. "Dante"
        "channel": 1,        # 1-based input channel carrying the kick mic
        "threshold": 0.5,    # peak level (0-1) that counts as a hit
        "gain": 1.0,         # input boost (×) applied before the meter/trigger
        "debounce_ms": 150,  # minimum gap between hits
        "flash_ms": 60,      # how long fixtures hold the flash
        "target": "bottom"   # all | bottom | group name
    }
}

# ---------------------------------------------------------------------------
# FIXTURE TYPE DEFINITIONS
# Defines how each fixture type converts RGB → channel values
# and which OSC properties to send
# ---------------------------------------------------------------------------

FIXTURE_TYPES = {
    "RGB": {
        "label": "RGB (3ch)",
        "channels": 3,
        "description": "Red, Green, Blue"
    },
    "RGBW": {
        "label": "RGBW (4ch)",
        "channels": 4,
        "description": "Red, Green, Blue, White (neutral)"
    },
    "RGBA": {
        "label": "RGBA (4ch)",
        "channels": 4,
        "description": "Red, Green, Blue, Amber"
    },
    "RGBWW": {
        "label": "RGBWW (5ch)",
        "channels": 5,
        "description": "Red, Green, Blue, Warm White, Cool White"
    },
    "RGBAW": {
        "label": "RGBAW (5ch)",
        "channels": 5,
        "description": "Red, Green, Blue, Amber, White"
    },
}

# ---------------------------------------------------------------------------
# COLOUR CONVERSION
# ---------------------------------------------------------------------------

def colour_temperature(r, g, b):
    """
    Returns a value 0.0 (cool/blue) to 1.0 (warm/orange) based on RGB.
    Used to split warm/cool white channels on RGBWW fixtures.
    """
    if r + g + b == 0:
        return 0.5
    warmth = (r * 1.0 + g * 0.5) / (r + g + b + 1e-6)
    return min(1.0, max(0.0, warmth))


def rgb_to_channels(r, g, b, fixture_type):
    """
    Convert RGB (0-255) to a dict of OSC property values (0.0-1.0 floats)
    appropriate for the fixture type.
    """
    rf, gf, bf = r/255.0, g/255.0, b/255.0

    # How much colour cast does this have? (0 = neutral grey/white, 1 = saturated)
    cmax = max(rf, gf, bf)
    cmin = min(rf, gf, bf)
    sat = (cmax - cmin) / (cmax + 1e-6) if cmax > 0 else 0.0

    # White channel scaling: when there's a clear colour cast, pull back the
    # white so the colour shows through. Only near-neutral frames get full white.
    # sat 0.0 → full white,  sat 0.15+ → little to no white (aggressive pullback)
    white_scale = max(0.0, 1.0 - sat * 5.0)

    if fixture_type == "RGB":
        return {"color": [rf, gf, bf]}

    elif fixture_type == "RGBW":
        w = min(rf, gf, bf) * white_scale
        return {
            "color": [rf, gf, bf],
            "warmWhite": [w]
        }

    elif fixture_type == "RGBA":
        # Amber adds warmth on top of full RGB colour
        amber = min(rf, max(0.0, rf * 0.7 + gf * 0.3 - bf))
        return {
            "color": [rf, gf, bf],
            "amber": [amber]
        }

    elif fixture_type == "RGBWW":
        # Full RGB colour + warm/cool white split by colour temperature
        w = min(rf, gf, bf) * white_scale
        temp = colour_temperature(r, g, b)
        warm_w = w * temp
        cool_w = w * (1.0 - temp)
        return {
            "color": [rf, gf, bf],
            "warmWhite": [warm_w],
            "coolWhite": [cool_w]
        }

    elif fixture_type == "RGBAW":
        # Full RGB colour + amber + white on top
        amber = min(rf, max(0.0, rf * 0.7 + gf * 0.3 - bf))
        w = min(rf, gf, bf) * white_scale
        return {
            "color": [rf, gf, bf],
            "amber": [amber],
            "warmWhite": [w]
        }

    return {"color": [rf, gf, bf]}



# ---------------------------------------------------------------------------
# BAR HELPERS
# ---------------------------------------------------------------------------

BAR_POSITIONS = {
    "top":    {"label": "Top",    "axis": "horizontal", "y1": 0.03, "y2": 0.25},
    "bottom": {"label": "Bottom", "axis": "horizontal", "y1": 0.75, "y2": 0.97},
    "left":   {"label": "Left",   "axis": "vertical",   "x1": 0.03, "x2": 0.22},
    "right":  {"label": "Right",  "axis": "vertical",   "x1": 0.78, "x2": 0.97},
    "custom": {"label": "Custom", "axis": "horizontal"},
}


def generate_bar_segments(bar):
    """
    Expand a bar definition into a list of individual fixture dicts,
    each with its own name and zone.

    Bar definition:
      name_prefix  e.g. "B1-"   → segments named B1-1, B1-2 ... B1-N
      segments     int           number of individually addressable segments
      type         str           fixture type per segment (RGB/RGBW/RGBA etc)
      position     str           top/bottom/left/right/custom
      zone         dict          full bar zone (used for custom, and as
                                 the bounding box to divide into segments)

    Returns list of fixture dicts compatible with the main fixture list.
    """
    segments = bar.get("segments", 8)
    prefix = bar.get("name_prefix", "B-")
    fx_type = bar.get("type", "RGB")
    position = bar.get("position", "top")
    zone = bar.get("zone", {"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0})

    # Apply preset position defaults if not custom
    if position != "custom":
        pos = BAR_POSITIONS.get(position, BAR_POSITIONS["top"])
        if pos["axis"] == "horizontal":
            zone = {
                "x1": zone.get("x1", 0.03),
                "y1": pos["y1"],
                "x2": zone.get("x2", 0.97),
                "y2": pos["y2"],
            }
        else:  # vertical
            zone = {
                "x1": pos["x1"],
                "y1": zone.get("y1", 0.03),
                "x2": pos["x2"],
                "y2": zone.get("y2", 0.97),
            }

    fixtures = []
    pos_data = BAR_POSITIONS.get(position, BAR_POSITIONS["top"])
    axis = pos_data.get("axis", "horizontal") if position != "custom" else bar.get("axis", "horizontal")

    for i in range(segments):
        t = i / segments
        t2 = (i + 1) / segments

        if axis == "horizontal":
            # Divide zone horizontally — each segment is a vertical slice
            seg_x1 = zone["x1"] + t * (zone["x2"] - zone["x1"])
            seg_x2 = zone["x1"] + t2 * (zone["x2"] - zone["x1"])
            seg_zone = {"x1": seg_x1, "y1": zone["y1"],
                        "x2": seg_x2, "y2": zone["y2"]}
        else:
            # Divide zone vertically — each segment is a horizontal slice
            seg_y1 = zone["y1"] + t * (zone["y2"] - zone["y1"])
            seg_y2 = zone["y1"] + t2 * (zone["y2"] - zone["y1"])
            seg_zone = {"x1": zone["x1"], "y1": seg_y1,
                        "x2": zone["x2"], "y2": seg_y2}

        fixtures.append({
            "name": f"{prefix}{i + 1}",
            "type": fx_type,
            "zone": seg_zone,
            "_bar": bar.get("name_prefix", ""),  # tag so UI can group them
        })

    return fixtures

# ---------------------------------------------------------------------------
# OSC CLIENT
# ---------------------------------------------------------------------------

class LightkeyOSC:
    def __init__(self, host, port):
        from pythonosc import udp_client
        self.client = udp_client.SimpleUDPClient(host, port)
        self.host = host
        self.port = port

    def send_fixture(self, name, channels):
        """Send all channel values for a fixture."""
        try:
            self.client.send_message(
                f"/fixture/{name}/overrides/dimmer", [1.0]
            )
            for prop, values in channels.items():
                self.client.send_message(
                    f"/fixture/{name}/overrides/{prop}", values
                )
        except Exception as e:
            log.warning(f"OSC error ({name}): {e}")

    def clear_all(self, fixture_names):
        """Clear all overrides — restores Lightkey cue control."""
        try:
            # Wildcard clear — works on Lightkey 3.x+
            self.client.send_message("/fixture/*/overrides/clear", [])
            log.info("Wildcard override clear sent.")
        except Exception as e:
            log.warning(f"Wildcard clear failed: {e}")
        # Also clear each fixture individually as a fallback
        for name in fixture_names:
            try:
                self.client.send_message(f"/fixture/{name}/overrides/clear", [])
            except Exception:
                pass
        log.info(f"Cleared overrides on {len(fixture_names)} fixtures.")

    def send_static(self, name, channels):
        """Send constant values for a static control.
        channels: list of {"property": str, "value": int 0-255}
        """
        for ch in channels:
            try:
                self.client.send_message(
                    f"/fixture/{name}/overrides/{ch['property']}",
                    [ch['value'] / 255.0]
                )
            except Exception as e:
                log.warning(f"OSC static error ({name}/{ch['property']}): {e}")

    def test_fixture(self, name, r, g, b, fixture_type):
        channels = rgb_to_channels(r, g, b, fixture_type)
        self.send_fixture(name, channels)


# ---------------------------------------------------------------------------
# SCREEN CAPTURE
# ---------------------------------------------------------------------------

class ScreenCapture:
    """
    Uses PIL ImageGrab for screen capture — handles monitors with negative
    coordinates (screens positioned left of primary on Windows) correctly.
    mss BitBlt fails on these; ImageGrab does not.
    """
    def __init__(self, monitor_index):
        self.monitor_index = monitor_index
        self._monitors = []
        self._bbox = None

    def start(self):
        self._monitors = self._get_monitors()
        self._bbox = self._monitor_bbox(self.monitor_index)
        log.info(f"Capturing monitor {self.monitor_index}: {self._bbox}")

    def _get_monitors(self):
        try:
            import mss
            with mss.MSS() as sct:
                return [m for i, m in enumerate(sct.monitors) if i > 0]
        except Exception as e:
            log.warning(f"Monitor detection error: {e}")
            return []

    def _monitor_bbox(self, idx):
        """Return (left, top, right, bottom) for the given monitor index."""
        if self._monitors and 1 <= idx <= len(self._monitors):
            m = self._monitors[idx - 1]
            return (m["left"], m["top"],
                    m["left"] + m["width"], m["top"] + m["height"])
        # Fallback: primary monitor
        from PIL import ImageGrab
        img = ImageGrab.grab()
        return (0, 0, img.width, img.height)

    def grab_frame(self):
        if not self._bbox:
            return None
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab(bbox=self._bbox, all_screens=True)
            # Resize to small working size for performance
            img = img.resize((320, 180))
            return np.array(img.convert("RGB"))
        except Exception as e:
            log.warning(f"Capture error: {e}")
            return None

    def list_monitors(self):
        monitors = self._get_monitors()
        return [
            {"index": i + 1, "width": m["width"], "height": m["height"],
             "left": m["left"], "top": m["top"]}
            for i, m in enumerate(monitors)
        ]

    def stop(self):
        self._bbox = None
        self._monitors = []


# ---------------------------------------------------------------------------
# COLOUR SAMPLING
# ---------------------------------------------------------------------------

def sample_zone(img_array, zone, grid_x=16, grid_y=10):
    """
    Sample colour from a zone using a weighted grid.
    Weights toward vivid/saturated pixels, but falls back to a plain
    brightness average when the zone is low-saturation (e.g. white/pastel
    backgrounds) so bright pale frames still produce bright output.
    Returns (r, g, b) 0-255.
    """
    h, w = img_array.shape[:2]
    x1 = int(zone["x1"] * w); y1 = int(zone["y1"] * h)
    x2 = int(zone["x2"] * w); y2 = int(zone["y2"] * h)
    x1, x2 = max(0, x1), min(w - 1, x2)
    y1, y2 = max(0, y1), min(h - 1, y2)

    if x2 <= x1 or y2 <= y1:
        return (0, 0, 0)

    xs = np.linspace(x1, x2, grid_x, dtype=int)
    ys = np.linspace(y1, y2, grid_y, dtype=int)
    samples = img_array[np.ix_(ys, xs)].reshape(-1, 3).astype(np.float32)

    s_max = samples.max(axis=1)
    s_min = samples.min(axis=1)
    saturation = np.where(s_max > 0, (s_max - s_min) / (s_max + 1e-6), 0)
    brightness = s_max / 255.0

    # Plain brightness-weighted average — the baseline colour of the zone.
    # This preserves the true brightness of pale/white frames.
    plain_weight = brightness + 0.1
    plain_total = plain_weight.sum()
    plain_avg = (samples * (plain_weight / plain_total)[:, np.newaxis]).sum(axis=0)

    # Saturation-weighted average — pulls toward vivid colours when present.
    sat_weight = saturation * brightness
    sat_total = sat_weight.sum()

    # How saturated is the zone overall? Blend between the two approaches.
    avg_saturation = float(saturation.mean())

    if sat_total < 0.01 or avg_saturation < 0.08:
        # Low saturation (white/pastel) — use the plain brightness average
        # so the fixtures track the actual brightness of the screen.
        result = plain_avg
    else:
        sat_avg = (samples * (sat_weight / sat_total)[:, np.newaxis]).sum(axis=0)
        # Blend: more saturated zones lean toward the vivid colour
        blend = min(1.0, avg_saturation * 4.0)
        result = sat_avg * blend + plain_avg * (1.0 - blend)

        # Gently lift saturation toward full only for genuinely colourful zones
        peak = result.max()
        if peak > 10 and avg_saturation > 0.15:
            # Partial normalisation — boost but don't fully saturate
            lift = 1.0 + (avg_saturation * 0.8)
            result = np.clip(result * lift, 0, 255)

    return (int(result[0]), int(result[1]), int(result[2]))


# ---------------------------------------------------------------------------
# KICK DETECTOR — audio input (e.g. Dante Virtual Soundcard channel)
# ---------------------------------------------------------------------------

class KickDetector:
    """
    Listens to one channel of an audio input device (e.g. the kick mic
    arriving on a Dante Virtual Soundcard channel) and fires a callback on
    each hit. A simple peak detector with hysteresis + debounce — reliable
    on an isolated kick channel, not a general beat tracker.

    Also maintains a lightweight FFT spectrum (8 bands) for the spectrum effect.
    """

    SPECTRUM_BANDS = 8

    def __init__(self, on_kick):
        self.on_kick = on_kick
        self.stream = None
        self.active = False
        self.error = None
        self.level = 0.0      # decaying peak, for the UI meter
        self.hits = 0
        self.threshold = 0.5
        self.gain = 1.0
        self.debounce_s = 0.15
        self.channel = 1
        self._armed = True
        self._last_hit = 0.0
        self._hit_event = threading.Event()
        self.spectrum = [0.0] * self.SPECTRUM_BANDS
        self._fft_buf = np.zeros(1024, dtype=np.float32)
        self._fft_pos = 0

    @staticmethod
    def _rescan():
        """PortAudio snapshots the device list when it initialises, so inputs
        that appeared since (e.g. Dante Virtual Soundcard started after this
        app) are invisible until it re-initialises. Only call while no
        stream is open — re-init kills active streams."""
        try:
            import sounddevice as sd
            sd._terminate()
            sd._initialize()
        except Exception:
            pass

    @staticmethod
    def list_devices(rescan=False):
        """Return available audio input devices, or an error message."""
        try:
            import sounddevice as sd
        except Exception:
            return {"error": "sounddevice not installed — run: pip install sounddevice",
                    "devices": []}
        if rescan:
            KickDetector._rescan()
        try:
            devices = [
                {"index": i, "name": d["name"],
                 "channels": d["max_input_channels"]}
                for i, d in enumerate(sd.query_devices())
                if d.get("max_input_channels", 0) > 0
            ]
            return {"error": None, "devices": devices}
        except Exception as e:
            return {"error": str(e), "devices": []}

    def start(self, device, channel, threshold, debounce_ms, gain=1.0):
        self.stop()
        self.error = None
        self.threshold = float(threshold)
        self.gain = max(1.0, float(gain))
        self.debounce_s = debounce_ms / 1000.0
        self.channel = max(1, int(channel))
        try:
            import sounddevice as sd
        except Exception:
            self.error = "sounddevice not installed — run: pip install sounddevice"
            return False
        # Safe here — stop() above means no stream is open
        self._rescan()
        try:
            # Resolve device: index, name substring, or default input
            dev = None
            if isinstance(device, (int, float)):
                dev = int(device)
            elif isinstance(device, str) and device.strip():
                if device.strip().isdigit():
                    dev = int(device.strip())
                else:
                    for i, d in enumerate(sd.query_devices()):
                        if (d.get("max_input_channels", 0) > 0
                                and device.lower() in d["name"].lower()):
                            dev = i
                            break
                    if dev is None:
                        self.error = f'No input device matching "{device}"'
                        return False
            info = sd.query_devices(dev, "input") if dev is not None \
                else sd.query_devices(kind="input")
            max_ch = info.get("max_input_channels", 0)
            if self.channel > max_ch:
                self.error = (f'Channel {self.channel} not available — '
                              f'"{info["name"]}" has {max_ch} input channels')
                return False
            # Open channels 1..N so the selected channel is the last column
            self.stream = sd.InputStream(
                device=dev, channels=self.channel,
                blocksize=256, callback=self._audio_cb
            )
            self.stream.start()
        except Exception as e:
            self.error = f"Audio input failed: {e}"
            self.stream = None
            return False
        self.active = True
        threading.Thread(target=self._dispatch, daemon=True).start()
        log.info(f'Kick detector listening on "{info["name"]}" ch {self.channel}')
        return True

    def stop(self):
        self.active = False
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.level = 0.0
        self._armed = True

    def _audio_cb(self, indata, frames, t, status):
        ch = indata[:, self.channel - 1]
        peak = min(1.0, float(np.max(np.abs(ch))) * self.gain)
        # Decaying peak so the UI meter is readable
        self.level = max(peak, self.level * 0.92)
        now = time.monotonic()
        if (self._armed and peak >= self.threshold
                and (now - self._last_hit) >= self.debounce_s):
            self._armed = False
            self._last_hit = now
            self.hits += 1
            self._hit_event.set()
        elif not self._armed and peak < self.threshold * 0.5:
            self._armed = True

        # Rolling FFT buffer → 8-band spectrum
        try:
            samples = (ch.astype(np.float32) * self.gain).ravel()
            n = len(samples)
            if n > 0:
                end = self._fft_pos + n
                if end <= len(self._fft_buf):
                    self._fft_buf[self._fft_pos:end] = samples
                    self._fft_pos = end
                else:
                    # wrap / refill
                    self._fft_buf[:] = 0
                    take = min(n, len(self._fft_buf))
                    self._fft_buf[:take] = samples[-take:]
                    self._fft_pos = take
                if self._fft_pos >= len(self._fft_buf):
                    windowed = self._fft_buf * np.hanning(len(self._fft_buf))
                    mag = np.abs(np.fft.rfft(windowed))
                    mag = mag / (np.max(mag) + 1e-9)
                    # Log-ish band split across spectrum
                    bands = self.SPECTRUM_BANDS
                    edges = np.logspace(0, np.log10(len(mag)), bands + 1).astype(int)
                    edges = np.clip(edges, 0, len(mag) - 1)
                    new_spec = []
                    for i in range(bands):
                        a, b = edges[i], max(edges[i] + 1, edges[i + 1])
                        new_spec.append(float(np.mean(mag[a:b])))
                    # Smooth
                    self.spectrum = [
                        0.65 * old + 0.35 * new
                        for old, new in zip(self.spectrum, new_spec)
                    ]
                    self._fft_pos = 0
        except Exception:
            pass

    def _dispatch(self):
        # OSC sends happen here, off the PortAudio callback thread
        while self.active:
            if self._hit_event.wait(0.2):
                self._hit_event.clear()
                try:
                    self.on_kick()
                except Exception as e:
                    log.warning(f"Kick flash failed: {e}")


# ---------------------------------------------------------------------------
# BRIDGE ENGINE
# ---------------------------------------------------------------------------

class BridgeEngine:
    def __init__(self):
        self.running = False
        self.enabled = True
        self.fog_enabled = True
        self.config = self._load_config()
        self.current_colours = {}
        self.output_colours = {}
        self.osc = None
        self.capture = None
        self._thread = None
        self.status = "stopped"
        self.fps_actual = 0
        self._last_bright = (200, 180, 120)
        self.kick_enabled = False
        self.flash_until = 0.0
        self.kick = KickDetector(on_kick=self._on_kick)
        self.tempo = TempoClock(self.config.get("tempo", {}).get("bpm", 120))
        self.effects = EffectEngine(
            self.tempo,
            get_config=lambda: self.config,
            get_spectrum=lambda: list(self.kick.spectrum),
        )
        self.effects.scenes.load_from_config(self.config.get("scenes", []))
        self.midi = MidiController(
            on_action=self._midi_action,
            get_config=lambda: self.config,
            save_mappings=self._save_midi_config,
        )
        self.actions = None  # set after engine global exists via init_actions()

    def init_actions(self):
        self.actions = build_actions(self, self.effects, self.tempo, self.midi)
        self.ensure_midi()

    def ensure_midi(self):
        """Open MIDI on launch and keep retrying until the controller appears."""
        midi = self.config.setdefault("midi", {})
        should = bool(
            midi.get("enabled")
            or midi.get("device")
            or midi.get("mappings")
        )
        # Always watch on Windows so a controller plugged in later is picked up
        if should or self.midi._winmm_available():
            self.midi.start_watchdog()
            log.info("MIDI auto-connect armed")

    def _midi_action(self, action_id, value=None, **kwargs):
        if not self.actions:
            return
        return self.actions.invoke(action_id, value=value, **kwargs)

    def _save_midi_config(self, cfg):
        """Persist config after MIDI learn/unmap without restarting MIDI I/O."""
        self.config = cfg
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)

    def _merge_midi_preserve(self, incoming: dict) -> dict:
        """Keep learned mappings / device if the client sent a stale empty midi block."""
        existing = (self.config or {}).get("midi") or {}
        midi = dict(incoming.get("midi") or {})
        existing_maps = existing.get("mappings") or []
        incoming_maps = midi.get("mappings") or []
        # Stale UI save often has mappings: [] while the server still has learns
        if existing_maps and not incoming_maps:
            midi["mappings"] = copy.deepcopy(existing_maps)
        if existing.get("device") and not midi.get("device"):
            midi["device"] = existing["device"]
        # Don't let a stale enabled:false kill a live connection / saved preference
        # unless the client explicitly included midi.enabled (always present in our UI).
        # Prefer existing enabled when incoming disables but mappings were also wiped.
        if existing.get("enabled") and not midi.get("enabled") and existing_maps and not incoming_maps:
            midi["enabled"] = True
        incoming = dict(incoming)
        incoming["midi"] = midi
        return incoming

    def _load_config(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    cfg = copy.deepcopy(DEFAULT_CONFIG)
                    loaded = json.load(f)
                    cfg.update(loaded)
                    # Deep-merge nested dicts we care about
                    for key in ("kick_strobe", "tempo", "midi"):
                        if key in loaded and isinstance(loaded[key], dict):
                            base = copy.deepcopy(DEFAULT_CONFIG.get(key, {}))
                            base.update(loaded[key])
                            # Ensure mappings list survives
                            if key == "midi" and "mappings" in loaded[key]:
                                base["mappings"] = copy.deepcopy(loaded[key]["mappings"])
                            cfg[key] = base
                    return cfg
            except Exception:
                pass
        return copy.deepcopy(DEFAULT_CONFIG)

    def save_config(self, cfg):
        cfg = self._merge_midi_preserve(cfg)
        self.config = cfg
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
        # Sync tempo / scenes / midi from saved config
        bpm = cfg.get("tempo", {}).get("bpm")
        if bpm:
            self.tempo.set_bpm(bpm)
        self.effects.scenes.load_from_config(cfg.get("scenes", []))
        if self.actions and hasattr(self.actions, "refresh_scene_actions"):
            self.actions.refresh_scene_actions()
        # Restart the kick detector so device/threshold changes apply live
        if self.kick_enabled:
            self.set_kick_strobe(True)
        midi_cfg = cfg.get("midi", {})
        if midi_cfg.get("enabled") is False and not midi_cfg.get("mappings"):
            self.midi.stop()
        else:
            want = midi_cfg.get("device", "")
            if self.midi.active:
                pass
            else:
                self.midi.start_watchdog()
                if want or midi_cfg.get("enabled"):
                    self.midi.start(want)

    def start(self):
        if self.running:
            return
        self.running = True
        self.fog_enabled = True
        self.status = "running"
        self.osc = LightkeyOSC(self.config["lightkey_host"], self.config["lightkey_port"])
        self.capture = ScreenCapture(self.config["monitor"])
        self.capture.start()
        all_fx = self._get_all_fixtures()
        self.current_colours = {
            f["name"]: (0, 0, 0, 0, 0) for f in all_fx
        }
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Bridge started.")

    def stop(self):
        self.running = False
        self.status = "stopped"
        # Always create a fresh OSC client for the clear — don't rely on
        # self.osc which may have been closed or never initialised
        try:
            clear_osc = LightkeyOSC(
                self.config["lightkey_host"],
                self.config["lightkey_port"]
            )
            all_names = [f["name"] for f in self._get_all_fixtures()]
            clear_osc.clear_all(all_names)
        except Exception as e:
            log.warning(f"Stop clear failed: {e}")
        if self.capture:
            self.capture.stop()
        self.osc = None
        log.info("Bridge stopped, overrides cleared.")

    def toggle_fog(self):
        return self.set_fog(not self.fog_enabled)

    def set_fog(self, enabled):
        self.fog_enabled = enabled
        try:
            osc = LightkeyOSC(self.config["lightkey_host"], self.config["lightkey_port"])
            for sc in self.config.get("static_controls", []):
                channels = sc.get("channels") or [
                    {"property": "dimmer", "value": sc.get("value", 0)}
                ]
                if not self.fog_enabled:
                    channels = [{**ch, "value": 0} for ch in channels]
                osc.send_static(sc["name"], channels)
        except Exception as e:
            log.warning(f"Fog toggle send failed: {e}")
        return self.fog_enabled

    def toggle_kick_strobe(self):
        return self.set_kick_strobe(not self.kick_enabled)

    def set_kick_strobe(self, enabled):
        ks = self.config.get("kick_strobe", {})
        if enabled:
            self.kick_enabled = self.kick.start(
                device=ks.get("device", ""),
                channel=ks.get("channel", 1),
                threshold=ks.get("threshold", 0.5),
                debounce_ms=ks.get("debounce_ms", 150),
                gain=ks.get("gain", 1.0),
            )
        else:
            self.kick.stop()
            self.kick_enabled = False
            self.flash_until = 0.0
        return self.kick_enabled

    def _flash_colour_for(self, name: str) -> tuple[int, int, int]:
        """
        Primary colour for a kick flash: that fixture's current screen colour,
        boosted to full intensity so the hit still punches. Falls back to the
        stage-wide brightest colour, then warm white.
        """
        src = self.output_colours.get(name) or self.current_colours.get(name)
        if src and max(src[:3]) > 8:
            r, g, b = src[0], src[1], src[2]
        else:
            # Stage primary — brightest recent ambient sample
            br = self._brightest()
            r, g, b = br[0], br[1], br[2]
            if max(r, g, b) < 8:
                return (255, 220, 180)

        peak = max(r, g, b)
        if peak <= 0:
            return (255, 220, 180)
        # Boost to full brightness, keep hue ratio
        scale = 255.0 / peak
        return (
            min(255, int(r * scale)),
            min(255, int(g * scale)),
            min(255, int(b * scale)),
        )

    def _fixture_is_bottom(self, fx: dict) -> bool:
        zone = fx.get("zone") or {}
        y1 = float(zone.get("y1", 0.0))
        y2 = float(zone.get("y2", 1.0))
        return ((y1 + y2) / 2.0) >= 0.55

    def _kick_flash_fixtures(self):
        target = (self.config.get("kick_strobe") or {}).get("target", "bottom")
        all_fx = self._get_all_fixtures()
        if not target or target in ("all", "*"):
            return all_fx
        if target == "bottom":
            bottom = [fx for fx in all_fx if self._fixture_is_bottom(fx)]
            return bottom or all_fx
        names = set(resolve_group_fixtures(self.config, target))
        matched = [fx for fx in all_fx if fx["name"] in names]
        return matched or all_fx

    def _on_kick(self):
        """Fired per kick hit (from the detector's dispatch thread):
        snap effect tempo to the kick, flash bottom fixtures with screen colour."""
        if not (self.running and self.enabled and self.kick_enabled and self.osc):
            return
        try:
            bpm = self.tempo.kick()
            self.config.setdefault("tempo", {})["bpm"] = bpm
        except Exception:
            pass
        flash_ms = self.config.get("kick_strobe", {}).get("flash_ms", 60)
        self.flash_until = time.time() + flash_ms / 1000.0
        for fx in self._kick_flash_fixtures():
            r, g, b = self._flash_colour_for(fx["name"])
            self.osc.send_fixture(
                fx["name"], rgb_to_channels(r, g, b, fx["type"])
            )

    def toggle(self):
        self.enabled = not self.enabled
        # When pausing, clear all overrides so Lightkey cues/manual control
        # take over immediately. When resuming, the loop starts sending again.
        if not self.enabled:
            try:
                clear_osc = LightkeyOSC(
                    self.config["lightkey_host"],
                    self.config["lightkey_port"]
                )
                all_names = [f["name"] for f in self._get_all_fixtures()]
                clear_osc.clear_all(all_names)
                log.info("Paused — overrides cleared, manual control active.")
            except Exception as e:
                log.warning(f"Pause clear failed: {e}")
        else:
            log.info("Resumed — ambient control active.")
        return self.enabled

    def _brightest(self):
        best = self._last_bright
        best_v = max(best)
        for c in self.current_colours.values():
            v = max(c[:3])
            if v > best_v:
                best_v = v
                best = c[:3]
        if best_v > 30:
            self._last_bright = best[:3]
        return self._last_bright

    def _get_all_fixtures(self):
        """Return combined list of regular fixtures + expanded bar segments."""
        fixtures = list(self.config.get("fixtures", []))
        for bar in self.config.get("bars", []):
            fixtures.extend(generate_bar_segments(bar))
        return fixtures

    def _process_frame(self, frame):
        fixtures = self._get_all_fixtures()
        if not fixtures:
            return

        boost = self.config["colour_boost"]
        master = self.config["master_brightness"] / 255.0
        smoothing = self.config["smoothing"]
        min_bright = self.config["min_brightness"]
        min_output = self.config.get("min_output", 40)

        # Sample all zones
        raw = {}
        for fx in fixtures:
            r, g, b = sample_zone(frame, fx["zone"])
            raw[fx["name"]] = (r, g, b)

        # Find brightest raw sample this frame
        brightest_raw = max(raw.values(), key=lambda c: max(c))
        all_dark = max(brightest_raw) < min_bright

        # STAGE-WIDE BRIGHTNESS PROTECTION
        # Count how many fixtures would be dark. If more than the allowed
        # percentage are dark at once, lift the whole-stage output floor so
        # the stage never drops too far, regardless of background.
        stage_floor_pct = self.config.get("stage_floor_pct", 50)
        dark_count = sum(1 for c in raw.values() if max(c) < min_bright)
        dark_ratio = dark_count / max(1, len(raw))
        # If more than (100 - stage_floor_pct)% are dark, boost the floor
        dynamic_floor = min_output
        if dark_ratio > (1.0 - stage_floor_pct / 100.0):
            # Scale the floor up based on how dark the stage is
            severity = (dark_ratio - (1.0 - stage_floor_pct / 100.0)) / (stage_floor_pct / 100.0 + 1e-6)
            dynamic_floor = int(min_output + severity * (90 - min_output))
            dynamic_floor = max(min_output, min(90, dynamic_floor))

        for fx in fixtures:
            name = fx["name"]
            fx_type = fx["type"]
            r, g, b = raw[name]
            brightness = max(r, g, b)

            if brightness < min_bright:
                if all_dark:
                    br = self._brightest()
                    r, g, b = br[0], br[1], br[2]
                else:
                    r, g, b = brightest_raw

            # RATIO-PRESERVING BOOST + SATURATION
            # The old per-channel boost pushed bright colours to (255,255,255)
            # white, destroying hues like yellow. Instead:
            #   1. Scale brightness up while keeping the R:G:B ratio intact
            #   2. Enhance saturation so colours read as their true hue on stage
            peak = max(r, g, b)
            if peak > 0:
                # Brightness lift — scale toward 255 by the boost amount,
                # but uniformly so the colour ratio is preserved
                target_peak = min(255, peak * boost)
                lift = target_peak / peak
                r = r * lift
                g = g * lift
                b = b * lift

                # Saturation enhancement — push channels away from their average
                # so e.g. yellow (high R, high G, lower B) gets more distinct.
                # sat_amount scales with the configured boost so one slider drives both.
                sat_amount = min(1.2, (boost - 1.0) * 0.4)
                avg = (r + g + b) / 3.0
                r = r + (r - avg) * sat_amount
                g = g + (g - avg) * sat_amount
                b = b + (b - avg) * sat_amount

                r = int(max(0, min(255, r)))
                g = int(max(0, min(255, g)))
                b = int(max(0, min(255, b)))

            # Scale by master
            r = int(r * master)
            g = int(g * master)
            b = int(b * master)

            # Enforce minimum output — lights never go fully dark.
            # Uses the dynamic floor which rises when much of the stage is dark.
            peak = max(r, g, b)
            if peak < dynamic_floor and dynamic_floor > 0:
                if peak == 0:
                    # No colour at all — use warm white at floor level
                    r, g, b = dynamic_floor, int(dynamic_floor * 0.85), int(dynamic_floor * 0.6)
                else:
                    # Scale up existing colour to meet floor
                    scale = dynamic_floor / peak
                    r = min(255, int(r * scale))
                    g = min(255, int(g * scale))
                    b = min(255, int(b * scale))

            # Smooth colour values (ambient base — never overwritten by effects)
            prev = self.current_colours.get(name, (0, 0, 0, 0, 0))
            smooth_r = int(prev[0] + smoothing * (r - prev[0]))
            smooth_g = int(prev[1] + smoothing * (g - prev[1]))
            smooth_b = int(prev[2] + smoothing * (b - prev[2]))
            self.current_colours[name] = (smooth_r, smooth_g, smooth_b, 0, 0)

        # Composite effects / scenes / solo / blackout over ambient base.
        # When nothing is active the compositor returns ambient unchanged.
        ambient = {n: c[:3] for n, c in self.current_colours.items()}
        composed = self.effects.composite(ambient)
        self.output_colours = composed

        # During a kick flash the colour values were already sent from the
        # detector thread — hold off so ambient doesn't overwrite them.
        if self.enabled and self.osc and time.time() >= self.flash_until:
            for fx in fixtures:
                name = fx["name"]
                rgb = composed.get(name, ambient.get(name, (0, 0, 0)))
                channels = rgb_to_channels(rgb[0], rgb[1], rgb[2], fx["type"])
                self.osc.send_fixture(name, channels)

    def _run(self):
        interval = 1.0 / self.config["sample_rate_fps"]
        frame_count = 0
        last_fps = time.time()

        while self.running:
            t0 = time.time()
            frame = self.capture.grab_frame()
            if frame is not None:
                self._process_frame(frame)

            # Static controls — off when paused or fog disabled
            if self.enabled and self.fog_enabled:
                for sc in self.config.get("static_controls", []):
                    if self.osc:
                        channels = sc.get("channels") or [
                            {"property": "dimmer", "value": sc.get("value", 0)}
                        ]
                        self.osc.send_static(sc["name"], channels)

            frame_count += 1
            now = time.time()
            if now - last_fps >= 5.0:
                self.fps_actual = round(frame_count / (now - last_fps), 1)
                frame_count = 0
                last_fps = now

            elapsed = time.time() - t0
            sleep = interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

    def get_state(self):
        return {
            "running": self.running,
            "enabled": self.enabled,
            "fog_enabled": self.fog_enabled,
            "status": self.status,
            "fps": self.fps_actual,
            "fixture_count": len(self.config.get("fixtures", [])),
            "bar_count": len(self.config.get("bars", [])),
            "total_count": len(self._get_all_fixtures()) if hasattr(self, "capture") else 0,
            "static_count": len(self.config.get("static_controls", [])),
            "kick_enabled": self.kick_enabled,
            "kick_hits": self.kick.hits,
            "kick_error": self.kick.error,
            "tempo": self.tempo.state(),
            "effects": self.effects.state(),
            "midi": self.midi.state(),
            "group_count": len(self.config.get("groups", [])),
            "scene_count": len(self.config.get("scenes", [])),
        }


# ---------------------------------------------------------------------------
# GLOBAL ENGINE INSTANCE
# ---------------------------------------------------------------------------

engine = BridgeEngine()
engine.init_actions()


# ---------------------------------------------------------------------------
# FLASK ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html",
                           fixture_types=FIXTURE_TYPES,
                           config=engine.config,
                           effect_defs=EFFECT_DEFS)


@app.route("/api/state")
def api_state():
    return jsonify(engine.get_state())


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(engine.config)


@app.route("/api/config", methods=["POST"])
def api_config_save():
    cfg = request.json
    engine.save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/start", methods=["POST"])
def api_start():
    engine.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    engine.stop()
    return jsonify({"ok": True})


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    enabled = engine.toggle()
    return jsonify({"enabled": enabled})


# ---------------------------------------------------------------------------
# ACTION / WEBHOOK ROUTES — shared registry for UI, Stream Deck, MIDI
# Legacy URLs (/hook/pause, /hook/fog-on, …) keep working via aliases.
# ---------------------------------------------------------------------------

def _action_kwargs():
    """Merge query args + JSON body into kwargs for action invoke."""
    kwargs = {}
    for k, v in request.args.items():
        kwargs[k] = v
    if request.is_json and isinstance(request.json, dict):
        kwargs.update(request.json)
    if "effect" in kwargs and "effect_id" not in kwargs:
        kwargs["effect_id"] = kwargs["effect"]
    if "name" in kwargs and "scene" not in kwargs:
        kwargs["scene"] = kwargs["name"]
    return kwargs


@app.route("/api/actions", methods=["GET"])
def api_actions_list():
    return jsonify(engine.actions.list_actions() if engine.actions else [])


@app.route("/api/action/<action_id>", methods=["GET", "POST"])
def api_action(action_id):
    kwargs = _action_kwargs()
    value = kwargs.pop("value", None)
    if value is not None:
        try:
            value = float(value)
        except (TypeError, ValueError):
            pass
    result = engine.actions.invoke(action_id, value=value, **kwargs)
    return jsonify(result)


@app.route("/hook/<path:action_id>", methods=["GET", "POST"])
def hook_action(action_id):
    """Universal webhook — any registered action id, plus legacy aliases."""
    aliases = {
        "fog-on": "fog_on",
        "fog-off": "fog_off",
        "fog-toggle": "fog_toggle",
        "kick-strobe-on": "kick_on",
        "kick-strobe-off": "kick_off",
        "kick-strobe-toggle": "kick_toggle",
        "tap-tempo": "tap_tempo",
        "effect-clear": "effect_clear",
        "blackout-fade": "blackout_fade",
        "solo-clear": "solo_clear",
        "scene-clear": "scene_clear",
    }
    if action_id == "status":
        return jsonify(engine.get_state())

    mapped = aliases.get(action_id, action_id.replace("-", "_"))
    kwargs = _action_kwargs()
    value = kwargs.pop("value", None)
    if value is not None:
        try:
            value = float(value)
        except (TypeError, ValueError):
            pass
    result = engine.actions.invoke(mapped, value=value, **kwargs)
    status = 200 if result.get("ok") else 404
    return jsonify(result), status


# ---------------------------------------------------------------------------
# EFFECTS / SCENES / GROUPS / TEMPO / MIDI
# ---------------------------------------------------------------------------

@app.route("/api/effects", methods=["GET"])
def api_effects_get():
    return jsonify(engine.effects.state())


@app.route("/api/effects/start", methods=["POST"])
def api_effects_start():
    data = request.json or {}
    inst = engine.effects.start_effect(
        data.get("effect_id", "chase"),
        group=data.get("group"),
        params=data.get("params"),
        bpm_sync=data.get("bpm_sync", True),
    )
    return jsonify({"ok": True, "effect": inst.to_dict()})


@app.route("/api/effects/stop", methods=["POST"])
def api_effects_stop():
    data = request.json or {}
    n = engine.effects.stop_effect(data.get("effect_id"), group=data.get("group"))
    return jsonify({"ok": True, "stopped": n})


@app.route("/api/effects/clear", methods=["POST"])
def api_effects_clear():
    return jsonify({"ok": True, "cleared": engine.effects.clear_effects()})


@app.route("/api/effects/param", methods=["POST"])
def api_effects_param():
    data = request.json or {}
    ok = engine.effects.set_param(
        data.get("effect_id"), data.get("param"), data.get("value"),
        group=data.get("group"),
    )
    return jsonify({"ok": ok})


@app.route("/api/tempo", methods=["GET"])
def api_tempo_get():
    return jsonify(engine.tempo.state())


@app.route("/api/tempo/tap", methods=["POST"])
def api_tempo_tap():
    bpm = engine.tempo.tap()
    engine.config.setdefault("tempo", {})["bpm"] = bpm
    return jsonify({"bpm": bpm})


@app.route("/api/tempo/bpm", methods=["POST"])
def api_tempo_bpm():
    data = request.json or {}
    bpm = engine.tempo.set_bpm(data.get("bpm", 120))
    engine.config.setdefault("tempo", {})["bpm"] = bpm
    return jsonify({"bpm": bpm})


@app.route("/api/groups", methods=["GET"])
def api_groups_get():
    return jsonify(engine.config.get("groups", []))


@app.route("/api/groups", methods=["POST"])
def api_groups_save():
    groups = request.json if isinstance(request.json, list) else (request.json or {}).get("groups", [])
    engine.config["groups"] = groups
    engine.save_config(engine.config)
    return jsonify({"ok": True, "groups": groups})


@app.route("/api/scenes", methods=["GET"])
def api_scenes_get():
    return jsonify(engine.effects.scenes.list_scenes())


@app.route("/api/scenes", methods=["POST"])
def api_scenes_save_all():
    scenes = request.json if isinstance(request.json, list) else (request.json or {}).get("scenes", [])
    engine.config["scenes"] = scenes
    engine.effects.scenes.load_from_config(scenes)
    if hasattr(engine.actions, "refresh_scene_actions"):
        engine.actions.refresh_scene_actions()
    engine.save_config(engine.config)
    return jsonify({"ok": True})


@app.route("/api/scenes/snapshot", methods=["POST"])
def api_scenes_snapshot():
    data = request.json or {}
    name = data.get("name") or f"Look {len(engine.effects.scenes.scenes) + 1}"
    fade_ms = int(data.get("fade_ms", 500))
    src = engine.output_colours or {k: v[:3] for k, v in engine.current_colours.items()}
    scene = engine.effects.scenes.save_scene(name, src, group=data.get("group"), fade_ms=fade_ms)
    engine.config["scenes"] = engine.effects.scenes.list_scenes()
    if hasattr(engine.actions, "refresh_scene_actions"):
        engine.actions.refresh_scene_actions()
    engine.save_config(engine.config)
    return jsonify({"ok": True, "scene": scene})


@app.route("/api/scenes/recall", methods=["POST"])
def api_scenes_recall():
    data = request.json or {}
    name = data.get("name")
    current = {k: v[:3] for k, v in (engine.output_colours or engine.current_colours).items()}
    ok = engine.effects.scenes.recall(name, current, fade_ms=data.get("fade_ms"))
    return jsonify({"ok": ok, "scene": name})


@app.route("/api/scenes/<name>", methods=["DELETE"])
def api_scenes_delete(name):
    engine.effects.scenes.delete_scene(name)
    engine.config["scenes"] = engine.effects.scenes.list_scenes()
    if hasattr(engine.actions, "refresh_scene_actions"):
        engine.actions.refresh_scene_actions()
    engine.save_config(engine.config)
    return jsonify({"ok": True})


@app.route("/api/solo", methods=["POST"])
def api_solo():
    data = request.json or {}
    if data.get("toggle"):
        g = engine.effects.toggle_solo(data.get("group", ""))
    elif data.get("clear"):
        g = engine.effects.set_solo(None)
    else:
        g = engine.effects.set_solo(data.get("group"))
    return jsonify({"solo_group": g})


@app.route("/api/blackout", methods=["POST"])
def api_blackout():
    data = request.json or {}
    fade_ms = int(data.get("fade_ms", 0))
    if data.get("restore"):
        engine.effects.clear_master_fade()
        return jsonify({"blackout": False})
    engine.effects.start_master_fade(
        engine.output_colours or engine.current_colours,
        to_rgb=(0, 0, 0),
        fade_ms=fade_ms,
    )
    if fade_ms <= 0:
        engine.effects.blackout = True
    return jsonify({"blackout": True, "fade_ms": fade_ms})


@app.route("/api/spectrum")
def api_spectrum():
    return jsonify({"bands": engine.kick.spectrum, "level": engine.kick.level})


@app.route("/api/midi/devices")
def api_midi_devices():
    return jsonify(MidiController.list_inputs())


@app.route("/api/midi/state")
def api_midi_state():
    return jsonify(engine.midi.state())


@app.route("/api/midi/start", methods=["POST"])
def api_midi_start():
    data = request.json or {}
    device = data.get("device", engine.config.get("midi", {}).get("device", ""))
    ok = engine.midi.start(device)
    midi = engine.config.setdefault("midi", {})
    midi["enabled"] = ok
    midi["device"] = engine.midi.port_name or device
    # Preserve mappings; only write midi fields
    engine._save_midi_config(engine.config)
    return jsonify({"ok": ok, **engine.midi.state()})


@app.route("/api/midi/stop", methods=["POST"])
def api_midi_stop():
    engine.midi.stop()
    engine.config.setdefault("midi", {})["enabled"] = False
    engine._save_midi_config(engine.config)
    return jsonify({"ok": True, **engine.midi.state()})


@app.route("/api/midi/learn", methods=["POST"])
def api_midi_learn():
    data = request.json or {}
    if data.get("cancel"):
        return jsonify(engine.midi.cancel_learn())
    action_id = data.get("action")
    if not action_id:
        return jsonify({"ok": False, "error": "action required"}), 400
    return jsonify(engine.midi.start_learn(action_id))


@app.route("/api/midi/unmap", methods=["POST"])
def api_midi_unmap():
    data = request.json or {}
    if data.get("clear_all"):
        return jsonify(engine.midi.clear_mappings())
    return jsonify(engine.midi.unmap(data.get("action", "")))


@app.route("/api/local-ip")
def api_local_ip():
    """Return this machine's LAN IP so the UI can show webhook URLs."""
    import socket
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    return jsonify({"ip": ip, "port": 5000})


@app.route("/api/monitors")
def api_monitors():
    try:
        cap = ScreenCapture(1)
        monitors = cap.list_monitors()
        return jsonify(monitors)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/fixture-types")
def api_fixture_types():
    return jsonify(FIXTURE_TYPES)


@app.route("/api/test/<fixture_name>", methods=["POST"])
def api_test_fixture(fixture_name):
    data = request.json
    colour = data.get("colour", "red")
    fx_type = data.get("type", "RGBW")

    colours = {
        "red":    (255, 0, 0),
        "green":  (0, 255, 0),
        "blue":   (0, 0, 255),
        "white":  (255, 255, 255),
        "amber":  (255, 140, 0),
        "off":    (0, 0, 0),
    }
    r, g, b = colours.get(colour, (255, 0, 0))

    osc = LightkeyOSC(engine.config["lightkey_host"], engine.config["lightkey_port"])
    osc.test_fixture(fixture_name, r, g, b, fx_type)
    return jsonify({"ok": True})


@app.route("/api/test-all", methods=["POST"])
def api_test_all():
    data = request.json
    colour = data.get("colour", "red")
    colours = {
        "red":    (255, 0, 0),
        "green":  (0, 255, 0),
        "blue":   (0, 0, 255),
        "white":  (255, 255, 255),
        "amber":  (255, 140, 0),
        "off":    (0, 0, 0),
    }
    r, g, b = colours.get(colour, (255, 0, 0))
    osc = LightkeyOSC(engine.config["lightkey_host"], engine.config["lightkey_port"])
    for fx in engine.config.get("fixtures", []):
        osc.test_fixture(fx["name"], r, g, b, fx["type"])
    return jsonify({"ok": True})


@app.route("/api/clear-overrides", methods=["POST"])
def api_clear():
    osc = LightkeyOSC(engine.config["lightkey_host"], engine.config["lightkey_port"])
    all_names = [f["name"] for f in engine._get_all_fixtures()]
    osc.clear_all(all_names)
    return jsonify({"ok": True})


@app.route("/api/live-colours")
def api_live_colours():
    """Return current output colour per fixture for live UI preview."""
    src = engine.output_colours or engine.current_colours
    out = {}
    for name, c in src.items():
        out[name] = {"r": c[0], "g": c[1], "b": c[2]}
    return jsonify(out)


@app.route("/api/bars", methods=["GET"])
def api_bars_get():
    return jsonify(engine.config.get("bars", []))


@app.route("/api/bar-preview", methods=["POST"])
def api_bar_preview():
    """Return the generated segment fixtures for a bar definition (for UI preview)."""
    bar = request.json
    segments = generate_bar_segments(bar)
    return jsonify(segments)


@app.route("/api/test-bar", methods=["POST"])
def api_test_bar():
    """Test all segments of a bar with a colour."""
    data = request.json
    bar = data.get("bar", {})
    colour = data.get("colour", "red")
    colours = {
        "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
        "white": (255, 255, 255), "amber": (255, 140, 0), "off": (0, 0, 0),
    }
    r, g, b = colours.get(colour, (255, 0, 0))
    osc = LightkeyOSC(engine.config["lightkey_host"], engine.config["lightkey_port"])
    for seg in generate_bar_segments(bar):
        osc.test_fixture(seg["name"], r, g, b, seg["type"])
    return jsonify({"ok": True})


@app.route("/api/bar-positions")
def api_bar_positions():
    return jsonify({k: v["label"] for k, v in BAR_POSITIONS.items()})


@app.route("/api/fog-toggle", methods=["POST"])
def api_fog_toggle():
    enabled = engine.toggle_fog()
    return jsonify({"fog_enabled": enabled})


@app.route("/api/kick-toggle", methods=["POST"])
def api_kick_toggle():
    enabled = engine.toggle_kick_strobe()
    return jsonify({"kick_enabled": enabled, "error": engine.kick.error})


@app.route("/api/audio-devices")
def api_audio_devices():
    return jsonify(KickDetector.list_devices(rescan=not engine.kick.active))


@app.route("/api/kick-meter")
def api_kick_meter():
    """Live input level + hit count — used by the UI to calibrate the threshold."""
    ks = engine.config.get("kick_strobe", {})
    return jsonify({
        "enabled": engine.kick_enabled,
        "level": round(engine.kick.level, 4),
        "threshold": ks.get("threshold", 0.5),
        "hits": engine.kick.hits,
        "error": engine.kick.error,
        "spectrum": engine.kick.spectrum,
    })


@app.route("/api/static-controls", methods=["GET"])
def api_static_get():
    return jsonify(engine.config.get("static_controls", []))


@app.route("/api/static-controls", methods=["POST"])
def api_static_save():
    controls = request.json
    engine.config["static_controls"] = controls
    engine.save_config(engine.config)
    return jsonify({"ok": True})


@app.route("/api/static-send", methods=["POST"])
def api_static_send():
    data = request.json
    name = data.get("name")
    channels = data.get("channels") or [
        {"property": "dimmer", "value": data.get("value", 0)}
    ]
    for sc in engine.config.get("static_controls", []):
        if sc.get("name") == name:
            sc["channels"] = channels
            break
    try:
        osc = LightkeyOSC(engine.config["lightkey_host"], engine.config["lightkey_port"])
        osc.send_static(name, channels)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    import subprocess
    import sys

    print("\n" + "=" * 50)
    print("  Pixel Mapping to OSC")
    print("  Companion strip launching…")
    print("  Full UI: http://localhost:5000")
    print("=" * 50 + "\n")

    def _start_companion():
        companion = Path(__file__).resolve().parent / "companion.py"
        if not companion.exists():
            log.warning("companion.py not found — skip mini control")
            return
        try:
            # Separate process so the Tk UI has its own main thread
            flags = 0
            if sys.platform == "win32":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(
                [sys.executable, str(companion)],
                cwd=str(companion.parent),
                creationflags=flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("Companion control strip started")
        except Exception as e:
            log.warning(f"Could not start companion: {e}")

    # Wait briefly so Flask is accepting connections before the strip polls
    threading.Timer(1.0, _start_companion).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
