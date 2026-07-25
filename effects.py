"""
Generative lighting effects, scenes, and compositor.

Effects produce per-fixture RGB overlays that composite over the ambient
screen-follow base layer. Existing ambient behaviour is unchanged when no
effects are active.
"""

from __future__ import annotations

import colorsys
import math
import random
import time
from copy import deepcopy
from typing import Callable


EFFECT_DEFS = {
    "chase": {
        "label": "Color Chase",
        "params": {
            "speed": {"min": 0.1, "max": 4.0, "default": 1.0},
            "intensity": {"min": 0.0, "max": 1.0, "default": 1.0},
            "size": {"min": 0.05, "max": 0.8, "default": 0.25},
            "hue": {"min": 0.0, "max": 1.0, "default": 0.08},
        },
        "blend": "replace",
    },
    "rainbow": {
        "label": "Rainbow",
        "params": {
            "speed": {"min": 0.05, "max": 3.0, "default": 0.4},
            "intensity": {"min": 0.0, "max": 1.0, "default": 1.0},
            "spread": {"min": 0.0, "max": 2.0, "default": 1.0},
        },
        "blend": "replace",
    },
    "wipe": {
        "label": "Wipe / Sweep",
        "params": {
            "speed": {"min": 0.1, "max": 4.0, "default": 1.0},
            "intensity": {"min": 0.0, "max": 1.0, "default": 1.0},
            "hue": {"min": 0.0, "max": 1.0, "default": 0.55},
            "width": {"min": 0.05, "max": 1.0, "default": 0.35},
        },
        "blend": "replace",
    },
    "pulse": {
        "label": "Pulse / Breathe",
        "params": {
            "speed": {"min": 0.05, "max": 3.0, "default": 0.5},
            "intensity": {"min": 0.0, "max": 1.0, "default": 0.8},
            "depth": {"min": 0.0, "max": 1.0, "default": 0.7},
            "hue": {"min": 0.0, "max": 1.0, "default": 0.08},
        },
        "blend": "multiply",
    },
    "strobe": {
        "label": "Strobe",
        "params": {
            "speed": {"min": 0.5, "max": 16.0, "default": 8.0},
            "intensity": {"min": 0.0, "max": 1.0, "default": 1.0},
            "duty": {"min": 0.05, "max": 0.5, "default": 0.15},
        },
        "blend": "replace",
    },
    "fire": {
        "label": "Fire / Flicker",
        "params": {
            "speed": {"min": 0.5, "max": 8.0, "default": 3.0},
            "intensity": {"min": 0.0, "max": 1.0, "default": 0.9},
            "warmth": {"min": 0.0, "max": 1.0, "default": 0.85},
        },
        "blend": "replace",
    },
    "sparkle": {
        "label": "Sparkle",
        "params": {
            "speed": {"min": 0.2, "max": 6.0, "default": 2.0},
            "intensity": {"min": 0.0, "max": 1.0, "default": 1.0},
            "density": {"min": 0.02, "max": 0.5, "default": 0.12},
        },
        "blend": "add",
    },
    "bump": {
        "label": "Bump on Beat",
        "params": {
            "intensity": {"min": 0.0, "max": 1.0, "default": 1.0},
            "hue": {"min": 0.0, "max": 1.0, "default": 0.0},
            "hold": {"min": 0.02, "max": 0.4, "default": 0.08},
        },
        "blend": "replace",
    },
    "spectrum": {
        "label": "Spectrum Analyzer",
        "params": {
            "intensity": {"min": 0.0, "max": 1.0, "default": 1.0},
            "sensitivity": {"min": 0.2, "max": 4.0, "default": 1.5},
        },
        "blend": "replace",
    },
}


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, _clamp(s), _clamp(v))
    return (int(r * 255), int(g * 255), int(b * 255))


def _blend(base, overlay, mode, amount=1.0):
    """Blend two RGB tuples. amount scales overlay contribution (0-1)."""
    if overlay is None or amount <= 0:
        return base
    br, bg, bb = base
    or_, og, ob = overlay
    a = _clamp(amount)
    if mode == "replace":
        return (
            int(br + (or_ - br) * a),
            int(bg + (og - bg) * a),
            int(bb + (ob - bb) * a),
        )
    if mode == "add":
        return (
            min(255, int(br + or_ * a)),
            min(255, int(bg + og * a)),
            min(255, int(bb + ob * a)),
        )
    if mode == "multiply":
        # overlay acts as a dimmer-ish multiplier via luminance
        lum = (or_ + og + ob) / (3 * 255.0)
        m = 1.0 - a + a * lum
        return (int(br * m), int(bg * m), int(bb * m))
    if mode == "htp":
        return (max(br, int(or_ * a)), max(bg, int(og * a)), max(bb, int(ob * a)))
    return base


def resolve_group_fixtures(config: dict, group_name: str | None) -> list[str]:
    """Return ordered fixture names for a group, or all fixtures if None/'*'/'all'."""
    all_names = []
    for fx in config.get("fixtures", []):
        all_names.append(fx["name"])
    for bar in config.get("bars", []):
        prefix = bar.get("name_prefix", "B-")
        segs = int(bar.get("segments", 8))
        for i in range(segs):
            all_names.append(f"{prefix}{i + 1}")

    if not group_name or group_name in ("*", "all"):
        return all_names

    for g in config.get("groups", []):
        if g.get("name") == group_name:
            names = list(g.get("fixtures", []))
            if g.get("mirror"):
                # already ordered; mirror is applied in generators
                pass
            return [n for n in names if n in set(all_names)] or names
    return all_names


class EffectInstance:
    def __init__(self, effect_id: str, group: str | None = None, params: dict | None = None,
                 blend: str | None = None, bpm_sync: bool = True):
        if effect_id not in EFFECT_DEFS:
            raise ValueError(f"Unknown effect: {effect_id}")
        self.effect_id = effect_id
        self.group = group
        self.bpm_sync = bpm_sync
        self.blend = blend or EFFECT_DEFS[effect_id]["blend"]
        self.params = {
            k: v["default"] for k, v in EFFECT_DEFS[effect_id]["params"].items()
        }
        if params:
            self.params.update(params)
        self.enabled = True
        self.started_at = time.monotonic()
        self._rng = random.Random(hash(effect_id) ^ int(self.started_at * 1000))
        self._sparkle_state = {}
        self._fire_state = {}

    def to_dict(self):
        return {
            "effect_id": self.effect_id,
            "label": EFFECT_DEFS[self.effect_id]["label"],
            "group": self.group,
            "params": dict(self.params),
            "blend": self.blend,
            "bpm_sync": self.bpm_sync,
            "enabled": self.enabled,
        }


class SceneStore:
    """Named looks: per-fixture RGB targets with optional fade."""

    def __init__(self):
        self.scenes: dict[str, dict] = {}
        self.active: str | None = None
        self._fade_from: dict[str, tuple] = {}
        self._fade_to: dict[str, tuple] = {}
        self._fade_start = 0.0
        self._fade_ms = 0
        self._fading = False

    def load_from_config(self, scenes: list):
        self.scenes = {}
        for s in scenes or []:
            name = s.get("name")
            if name:
                self.scenes[name] = deepcopy(s)

    def list_scenes(self):
        return [deepcopy(s) for s in self.scenes.values()]

    def save_scene(self, name: str, colours: dict, group: str | None = None, fade_ms: int = 500):
        self.scenes[name] = {
            "name": name,
            "group": group,
            "fade_ms": fade_ms,
            "colours": {k: list(v[:3]) for k, v in colours.items()},
        }
        return self.scenes[name]

    def delete_scene(self, name: str):
        self.scenes.pop(name, None)
        if self.active == name:
            self.active = None

    def recall(self, name: str, current_colours: dict, fade_ms: int | None = None):
        scene = self.scenes.get(name)
        if not scene:
            return False
        self.active = name
        fade = fade_ms if fade_ms is not None else scene.get("fade_ms", 0)
        targets = {k: tuple(v[:3]) for k, v in scene.get("colours", {}).items()}
        if fade <= 0:
            self._fading = False
            self._fade_to = targets
            return True
        self._fade_from = {k: tuple(current_colours.get(k, (0, 0, 0))[:3]) for k in targets}
        self._fade_to = targets
        self._fade_start = time.monotonic()
        self._fade_ms = fade
        self._fading = True
        return True

    def clear_active(self):
        self.active = None
        self._fading = False
        self._fade_to = {}

    def apply(self, colours: dict) -> dict:
        """Overlay active scene onto colour dict (mutates and returns)."""
        if not self.active and not self._fading:
            return colours
        if self._fading:
            elapsed = (time.monotonic() - self._fade_start) * 1000.0
            t = _clamp(elapsed / max(1, self._fade_ms))
            for name, target in self._fade_to.items():
                src = self._fade_from.get(name, (0, 0, 0))
                colours[name] = (
                    int(src[0] + (target[0] - src[0]) * t),
                    int(src[1] + (target[1] - src[1]) * t),
                    int(src[2] + (target[2] - src[2]) * t),
                )
            if t >= 1.0:
                self._fading = False
            return colours
        for name, target in self._fade_to.items():
            colours[name] = target
        return colours


class EffectEngine:
    def __init__(self, tempo, get_config: Callable[[], dict], get_spectrum: Callable[[], list] | None = None):
        self.tempo = tempo
        self.get_config = get_config
        self.get_spectrum = get_spectrum or (lambda: [])
        self.instances: list[EffectInstance] = []
        self.scenes = SceneStore()
        self.solo_group: str | None = None
        self.solo_dim = 0.15  # others dimmed to this fraction
        self.master_fade = None  # dict: {to: (r,g,b)|None black, start, ms, from_colours}
        self.blackout = False

    # ---- instance management ----

    def start_effect(self, effect_id: str, group: str | None = None, params: dict | None = None,
                     bpm_sync: bool = True, replace_group: bool = True):
        if replace_group:
            self.instances = [
                i for i in self.instances
                if not (i.group == group and i.effect_id == effect_id)
            ]
        inst = EffectInstance(effect_id, group=group, params=params, bpm_sync=bpm_sync)
        self.instances.append(inst)
        return inst

    def stop_effect(self, effect_id: str | None = None, group: str | None = None):
        before = len(self.instances)
        self.instances = [
            i for i in self.instances
            if not (
                (effect_id is None or i.effect_id == effect_id)
                and (group is None or i.group == group)
            )
        ]
        return before - len(self.instances)

    def clear_effects(self):
        n = len(self.instances)
        self.instances.clear()
        return n

    def set_param(self, effect_id: str, param: str, value: float, group: str | None = None):
        for i in self.instances:
            if i.effect_id == effect_id and (group is None or i.group == group):
                if param in i.params:
                    spec = EFFECT_DEFS[effect_id]["params"].get(param, {})
                    lo = spec.get("min", 0.0)
                    hi = spec.get("max", 1.0)
                    i.params[param] = _clamp(float(value), lo, hi)
                    return True
        return False

    def set_solo(self, group: str | None):
        self.solo_group = group if group else None
        return self.solo_group

    def toggle_solo(self, group: str):
        if self.solo_group == group:
            self.solo_group = None
        else:
            self.solo_group = group
        return self.solo_group

    def start_master_fade(self, current_colours: dict, to_rgb=(0, 0, 0), fade_ms=1000):
        self.master_fade = {
            "from": {k: tuple(v[:3]) for k, v in current_colours.items()},
            "to": tuple(to_rgb[:3]) if to_rgb is not None else (0, 0, 0),
            "start": time.monotonic(),
            "ms": max(0, int(fade_ms)),
        }
        if fade_ms <= 0:
            self.blackout = (to_rgb is None) or (max(to_rgb[:3]) == 0)
            self.master_fade = None

    def clear_master_fade(self):
        self.master_fade = None
        self.blackout = False

    def state(self):
        return {
            "effects": [i.to_dict() for i in self.instances],
            "scenes": self.scenes.list_scenes(),
            "active_scene": self.scenes.active,
            "solo_group": self.solo_group,
            "blackout": self.blackout,
            "master_fading": self.master_fade is not None,
            "effect_defs": {
                k: {"label": v["label"], "params": v["params"], "blend": v["blend"]}
                for k, v in EFFECT_DEFS.items()
            },
        }

    # ---- generators ----

    def _gen(self, inst: EffectInstance, names: list[str], now: float) -> dict:
        eid = inst.effect_id
        p = inst.params
        n = max(1, len(names))
        beats = self.tempo.continuous_beats(now)
        phase = self.tempo.beat_phase(now)
        speed = p.get("speed", 1.0)
        t = beats * speed if inst.bpm_sync else (now - inst.started_at) * speed

        out = {}
        if eid == "chase":
            pos = t % n
            size = p.get("size", 0.25) * n
            hue = p.get("hue", 0.08)
            inten = p.get("intensity", 1.0)
            for i, name in enumerate(names):
                dist = min((i - pos) % n, (pos - i) % n)
                fall = max(0.0, 1.0 - dist / max(0.5, size))
                out[name] = _hsv(hue, 0.95, fall * inten) if fall > 0 else (0, 0, 0)

        elif eid == "rainbow":
            spread = p.get("spread", 1.0)
            inten = p.get("intensity", 1.0)
            for i, name in enumerate(names):
                h = (t * 0.15 + (i / n) * spread) % 1.0
                out[name] = _hsv(h, 1.0, inten)

        elif eid == "wipe":
            width = p.get("width", 0.35)
            hue = p.get("hue", 0.55)
            inten = p.get("intensity", 1.0)
            center = (t * 0.5) % (1.0 + width) - width * 0.5
            for i, name in enumerate(names):
                x = i / max(1, n - 1)
                d = abs(x - center)
                fall = max(0.0, 1.0 - d / max(0.05, width))
                out[name] = _hsv(hue, 0.9, fall * inten)

        elif eid == "pulse":
            depth = p.get("depth", 0.7)
            hue = p.get("hue", 0.08)
            inten = p.get("intensity", 0.8)
            wave = 0.5 + 0.5 * math.sin(t * math.pi * 2)
            level = inten * ((1.0 - depth) + depth * wave)
            colour = _hsv(hue, 0.7, level)
            for name in names:
                out[name] = colour

        elif eid == "strobe":
            duty = p.get("duty", 0.15)
            inten = p.get("intensity", 1.0)
            # speed is flashes per beat when bpm_sync, else Hz-ish via t
            flash_phase = (t % 1.0) if inst.bpm_sync else ((now * speed) % 1.0)
            on = flash_phase < duty
            colour = (int(255 * inten), int(255 * inten), int(255 * inten)) if on else (0, 0, 0)
            for name in names:
                out[name] = colour

        elif eid == "fire":
            inten = p.get("intensity", 0.9)
            warmth = p.get("warmth", 0.85)
            for name in names:
                prev = self._fire_state.get(name, 0.6)
                target = 0.35 + 0.65 * inst._rng.random()
                level = prev * 0.55 + target * 0.45
                self._fire_state[name] = level
                level *= inten
                r = int(255 * level)
                g = int(80 * level * (0.4 + 0.6 * warmth))
                b = int(10 * level * (1.0 - warmth))
                out[name] = (r, g, b)

        elif eid == "sparkle":
            dens = p.get("density", 0.12)
            inten = p.get("intensity", 1.0)
            for name in names:
                life = self._sparkle_state.get(name, 0.0)
                if life <= 0 and inst._rng.random() < dens * 0.15 * speed:
                    life = 1.0
                life = max(0.0, life - 0.08 * speed)
                self._sparkle_state[name] = life
                v = life * inten
                out[name] = (int(255 * v), int(255 * v), int(240 * v)) if v > 0 else (0, 0, 0)

        elif eid == "bump":
            hold = p.get("hold", 0.08)
            hue = p.get("hue", 0.0)
            inten = p.get("intensity", 1.0)
            # bright near start of beat
            age = phase  # 0 at downbeat
            if age < hold:
                fall = 1.0 - (age / hold)
                out_c = _hsv(hue, 0.2 if hue < 0.02 else 0.9, fall * inten)
            else:
                out_c = (0, 0, 0)
            for name in names:
                out[name] = out_c

        elif eid == "spectrum":
            bands = self.get_spectrum()
            inten = p.get("intensity", 1.0)
            sens = p.get("sensitivity", 1.5)
            nb = max(1, len(bands))
            for i, name in enumerate(names):
                bi = int(i / n * nb) if n else 0
                bi = min(nb - 1, bi)
                level = _clamp((bands[bi] if bands else 0.0) * sens)
                # cool→warm across bar
                h = 0.65 - 0.55 * (i / max(1, n - 1))
                out[name] = _hsv(h, 0.95, level * inten)

        else:
            for name in names:
                out[name] = (0, 0, 0)

        # optional mirror: average with mirrored index
        cfg = self.get_config()
        for g in cfg.get("groups", []):
            if g.get("name") == inst.group and g.get("mirror"):
                mirrored = {}
                for i, name in enumerate(names):
                    j = n - 1 - i
                    a = out.get(name, (0, 0, 0))
                    b = out.get(names[j], (0, 0, 0))
                    mirrored[name] = ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2, (a[2] + b[2]) // 2)
                return mirrored
        return out

    # ---- compositor ----

    def composite(self, ambient: dict) -> dict:
        """
        ambient: name -> (r,g,b[,...])
        returns name -> (r,g,b)
        """
        now = time.monotonic()
        result = {k: tuple(v[:3]) for k, v in ambient.items()}

        if self.blackout and self.master_fade is None:
            return {k: (0, 0, 0) for k in result}

        # Scene layer (replace)
        if self.scenes.active or self.scenes._fading:
            result = self.scenes.apply(result)

        # Effect layers
        for inst in self.instances:
            if not inst.enabled:
                continue
            names = resolve_group_fixtures(self.get_config(), inst.group)
            # Only affect fixtures present in result; add missing as black base
            for name in names:
                if name not in result:
                    result[name] = (0, 0, 0)
            overlay = self._gen(inst, names, now)
            amount = inst.params.get("intensity", 1.0)
            # intensity already baked into most gens; use full blend amount
            blend_amt = 1.0 if inst.blend != "multiply" else 1.0
            for name, rgb in overlay.items():
                base = result.get(name, (0, 0, 0))
                result[name] = _blend(base, rgb, inst.blend, blend_amt)

        # Solo / highlight
        if self.solo_group:
            solo_names = set(resolve_group_fixtures(self.get_config(), self.solo_group))
            dim = self.solo_dim
            for name, rgb in list(result.items()):
                if name not in solo_names:
                    result[name] = (int(rgb[0] * dim), int(rgb[1] * dim), int(rgb[2] * dim))

        # Master fade
        if self.master_fade:
            mf = self.master_fade
            elapsed = (now - mf["start"]) * 1000.0
            t = _clamp(elapsed / max(1, mf["ms"]))
            target = mf["to"]
            for name in list(result.keys()):
                src = mf["from"].get(name, result[name])
                result[name] = (
                    int(src[0] + (target[0] - src[0]) * t),
                    int(src[1] + (target[1] - src[1]) * t),
                    int(src[2] + (target[2] - src[2]) * t),
                )
            if t >= 1.0:
                self.blackout = max(target) == 0
                self.master_fade = None

        return result
