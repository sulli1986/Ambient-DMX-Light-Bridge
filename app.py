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
import threading
import logging
import numpy as np
from pathlib import Path
from flask import Flask, render_template, request, jsonify

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
    "kick_strobe": {
        "device": "",        # input device name substring, e.g. "Dante"
        "channel": 1,        # 1-based input channel carrying the kick mic
        "threshold": 0.5,    # peak level (0-1) that counts as a hit
        "debounce_ms": 150,  # minimum gap between hits
        "flash_ms": 60       # how long fixtures hold the flash
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
    """

    def __init__(self, on_kick):
        self.on_kick = on_kick
        self.stream = None
        self.active = False
        self.error = None
        self.level = 0.0      # decaying peak, for the UI meter
        self.hits = 0
        self.threshold = 0.5
        self.debounce_s = 0.15
        self.channel = 1
        self._armed = True
        self._last_hit = 0.0
        self._hit_event = threading.Event()

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

    def start(self, device, channel, threshold, debounce_ms):
        self.stop()
        self.error = None
        self.threshold = float(threshold)
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
        peak = float(np.max(np.abs(indata[:, self.channel - 1])))
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
        self.osc = None
        self.capture = None
        self._thread = None
        self.status = "stopped"
        self.fps_actual = 0
        self._last_bright = (200, 180, 120)
        self.kick_enabled = False
        self.flash_until = 0.0
        self.kick = KickDetector(on_kick=self._on_kick)

    def _load_config(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    cfg = DEFAULT_CONFIG.copy()
                    cfg.update(json.load(f))
                    return cfg
            except Exception:
                pass
        return DEFAULT_CONFIG.copy()

    def save_config(self, cfg):
        self.config = cfg
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
        # Restart the kick detector so device/threshold changes apply live
        if self.kick_enabled:
            self.set_kick_strobe(True)

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
            )
        else:
            self.kick.stop()
            self.kick_enabled = False
            self.flash_until = 0.0
        return self.kick_enabled

    def _on_kick(self):
        """Fired per kick hit (from the detector's dispatch thread):
        flash all fixtures white immediately, without waiting for the
        frame loop, then let ambient colours resume after flash_ms."""
        if not (self.running and self.enabled and self.kick_enabled and self.osc):
            return
        flash_ms = self.config.get("kick_strobe", {}).get("flash_ms", 60)
        self.flash_until = time.time() + flash_ms / 1000.0
        for fx in self._get_all_fixtures():
            self.osc.send_fixture(
                fx["name"], rgb_to_channels(255, 255, 255, fx["type"])
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

            # Smooth colour values
            prev = self.current_colours.get(name, (0, 0, 0, 0, 0))
            smooth_r = int(prev[0] + smoothing * (r - prev[0]))
            smooth_g = int(prev[1] + smoothing * (g - prev[1]))
            smooth_b = int(prev[2] + smoothing * (b - prev[2]))
            self.current_colours[name] = (smooth_r, smooth_g, smooth_b, 0, 0)

            # Convert smoothed values to fixture channels
            channels = rgb_to_channels(smooth_r, smooth_g, smooth_b, fx_type)

            # During a kick flash the white values were already sent from the
            # detector thread — hold off so ambient doesn't overwrite them.
            if self.enabled and self.osc and time.time() >= self.flash_until:
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
        }


# ---------------------------------------------------------------------------
# GLOBAL ENGINE INSTANCE
# ---------------------------------------------------------------------------

engine = BridgeEngine()


# ---------------------------------------------------------------------------
# FLASK ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html",
                           fixture_types=FIXTURE_TYPES,
                           config=engine.config)


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
# WEBHOOK ROUTES — for Stream Deck / external triggers
# These accept GET so they can be hit from a simple URL button.
# Example Stream Deck "Website" or "HTTP Request" action:
#   http://<bridge-ip>:5000/hook/pause
#   http://<bridge-ip>:5000/hook/resume
#   http://<bridge-ip>:5000/hook/toggle
#   http://<bridge-ip>:5000/hook/start
#   http://<bridge-ip>:5000/hook/stop
#   http://<bridge-ip>:5000/hook/fog-on
#   http://<bridge-ip>:5000/hook/fog-off
#   http://<bridge-ip>:5000/hook/fog-toggle
#   http://<bridge-ip>:5000/hook/kick-strobe-on
#   http://<bridge-ip>:5000/hook/kick-strobe-off
#   http://<bridge-ip>:5000/hook/kick-strobe-toggle
# ---------------------------------------------------------------------------

@app.route("/hook/pause", methods=["GET", "POST"])
def hook_pause():
    """Pause ambient control (clears overrides, hands control to LD)."""
    if engine.enabled:
        engine.toggle()  # toggle off
    return jsonify({"enabled": engine.enabled, "action": "pause"})


@app.route("/hook/resume", methods=["GET", "POST"])
def hook_resume():
    """Resume ambient control."""
    if not engine.enabled:
        engine.toggle()  # toggle on
    # If the bridge was stopped entirely, start it
    if not engine.running:
        engine.start()
    return jsonify({"enabled": engine.enabled, "running": engine.running, "action": "resume"})


@app.route("/hook/toggle", methods=["GET", "POST"])
def hook_toggle():
    """Toggle pause/resume — single button that flips state."""
    enabled = engine.toggle()
    return jsonify({"enabled": enabled, "action": "toggle"})


@app.route("/hook/start", methods=["GET", "POST"])
def hook_start():
    """Start the bridge."""
    engine.start()
    return jsonify({"running": engine.running, "action": "start"})


@app.route("/hook/stop", methods=["GET", "POST"])
def hook_stop():
    """Stop the bridge and clear all overrides."""
    engine.stop()
    return jsonify({"running": engine.running, "action": "stop"})


@app.route("/hook/fog-on", methods=["GET", "POST"])
def hook_fog_on():
    """Turn hazer/fog static controls on."""
    enabled = engine.set_fog(True)
    return jsonify({"fog_enabled": enabled, "action": "fog-on"})


@app.route("/hook/fog-off", methods=["GET", "POST"])
def hook_fog_off():
    """Turn hazer/fog static controls off."""
    enabled = engine.set_fog(False)
    return jsonify({"fog_enabled": enabled, "action": "fog-off"})


@app.route("/hook/fog-toggle", methods=["GET", "POST"])
def hook_fog_toggle():
    """Flip hazer/fog on/off — single button that toggles state."""
    enabled = engine.toggle_fog()
    return jsonify({"fog_enabled": enabled, "action": "fog-toggle"})


@app.route("/hook/kick-strobe-on", methods=["GET", "POST"])
def hook_kick_on():
    """Enable kick-triggered strobe (opens the audio input)."""
    enabled = engine.set_kick_strobe(True)
    return jsonify({"kick_enabled": enabled, "error": engine.kick.error,
                    "action": "kick-strobe-on"})


@app.route("/hook/kick-strobe-off", methods=["GET", "POST"])
def hook_kick_off():
    """Disable kick-triggered strobe (closes the audio input)."""
    enabled = engine.set_kick_strobe(False)
    return jsonify({"kick_enabled": enabled, "action": "kick-strobe-off"})


@app.route("/hook/kick-strobe-toggle", methods=["GET", "POST"])
def hook_kick_toggle():
    """Flip kick strobe on/off — single button that toggles state."""
    enabled = engine.toggle_kick_strobe()
    return jsonify({"kick_enabled": enabled, "error": engine.kick.error,
                    "action": "kick-strobe-toggle"})


@app.route("/hook/status", methods=["GET"])
def hook_status():
    """Return current state — useful for Stream Deck multi-state buttons."""
    return jsonify(engine.get_state())


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
    out = {}
    for name, c in engine.current_colours.items():
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
        "red": (255,0,0), "green": (0,255,0), "blue": (0,0,255),
        "white": (255,255,255), "amber": (255,140,0), "off": (0,0,0),
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
    # Re-scan for new devices unless the detector's stream is live
    # (re-initialising PortAudio would kill it)
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
    # Update in-memory config so the run loop uses the new values immediately
    # (without this the loop overwrites the slider value with the stale saved config)
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
    import webbrowser
    print("\n" + "="*50)
    print("  Pixel Mapping to OSC")
    print("  Open: http://localhost:5000")
    print("="*50 + "\n")
    # Open browser after short delay
    threading.Timer(1.2, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
