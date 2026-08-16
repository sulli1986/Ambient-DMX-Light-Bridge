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
import logging

log = logging.getLogger("pixel-mapping-osc")


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
            "width": {"min": 0.05, "max": 1.0, "default": 0.35},
        },
        "blend": "replace",
    },
    "pulse": {
        "label": "Pulse / Breathe",
        "params": {
            "speed": {"min": 0.05, "max": 3.0, "default": 1.0},
            "intensity": {"min": 0.0, "max": 1.5, "default": 1.35},
            "depth": {"min": 0.0, "max": 1.0, "default": 0.45},
        },
        "blend": "htp",
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
        "blend": "htp",
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
    "alternate": {
        "label": "Alternate",
        "params": {
            "speed": {"min": 0.25, "max": 4.0, "default": 1.0},
            "intensity": {"min": 0.0, "max": 1.5, "default": 1.2},
        },
        "blend": "replace",
    },
    "bounce": {
        "label": "Bounce",
        "params": {
            "speed": {"min": 0.1, "max": 4.0, "default": 1.0},
            "intensity": {"min": 0.0, "max": 1.0, "default": 1.0},
            "size": {"min": 0.05, "max": 0.8, "default": 0.3},
        },
        "blend": "replace",
    },
}


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, _clamp(s), _clamp(v))
    return (int(r * 255), int(g * 255), int(b * 255))


def _boost_rgb(rgb, amount=1.0):
    r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
    peak = max(r, g, b)
    if peak <= 0:
        return (0, 0, 0)
    scale = (255.0 / peak) * max(0.0, amount)
    return (
        min(255, int(r * scale)),
        min(255, int(g * scale)),
        min(255, int(b * scale)),
    )


def _scale_rgb(rgb, amount=1.0):
    a = max(0.0, amount)
    return (
        min(255, int(rgb[0] * a)),
        min(255, int(rgb[1] * a)),
        min(255, int(rgb[2] * a)),
    )


def _src_colour(name, ambient: dict, names: list[str]) -> tuple[int, int, int]:
    """Fixture screen colour, or the brightest colour in the group."""
    c = ambient.get(name)
    if c and max(c[:3]) > 12:
        return (int(c[0]), int(c[1]), int(c[2]))
    best = (255, 180, 80)
    best_v = 0
    for n in names:
        cc = ambient.get(n)
        if cc:
            v = max(cc[:3])
            if v > best_v:
                best_v = v
                best = (int(cc[0]), int(cc[1]), int(cc[2]))
    return best


def _shift_hue(rgb, delta):
    r, g, b = [x / 255.0 for x in rgb[:3]]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return _hsv(h + delta, max(0.35, s), max(0.35, v))


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

    def _gen(self, inst: EffectInstance, names: list[str], now: float, ambient: dict) -> dict:
        eid = inst.effect_id
        p = inst.params
        n = max(1, len(names))
        beats = self.tempo.continuous_beats(now)
        phase = self.tempo.beat_phase(now)
        speed = p.get("speed", 1.0)
        t = beats * speed if inst.bpm_sync else (now - inst.started_at) * speed
        kick_age = self.tempo.kick_age(now) if hasattr(self.tempo, "kick_age") else 999.0
        kick_punch = max(0.0, 1.0 - kick_age / 0.14)

        out = {}
        if eid == "chase":
            pos = t % n
            size = p.get("size", 0.25) * n
            inten = p.get("intensity", 1.0)
            for i, name in enumerate(names):
                dist = min((i - pos) % n, (pos - i) % n)
                fall = max(0.0, 1.0 - dist / max(0.5, size))
                src = _src_colour(name, ambient, names)
                out[name] = _boost_rgb(src, (fall * inten) + 0.25 * kick_punch * fall) if fall > 0 else (0, 0, 0)

        elif eid == "rainbow":
            spread = p.get("spread", 1.0)
            inten = p.get("intensity", 1.0)
            for i, name in enumerate(names):
                src = _src_colour(name, ambient, names)
                delta = (t * 0.15 + (i / n) * spread) % 1.0
                shifted = _shift_hue(src, delta)
                out[name] = _boost_rgb(shifted, inten)

        elif eid == "wipe":
            width = p.get("width", 0.35)
            inten = p.get("intensity", 1.0)
            center = (t * 0.5) % (1.0 + width) - width * 0.5
            for i, name in enumerate(names):
                x = i / max(1, n - 1)
                d = abs(x - center)
                fall = max(0.0, 1.0 - d / max(0.05, width))
                src = _src_colour(name, ambient, names)
                out[name] = _boost_rgb(src, fall * inten) if fall > 0 else (0, 0, 0)

        elif eid == "pulse":
            depth = p.get("depth", 0.45)
            inten = p.get("intensity", 1.35)
            if inst.bpm_sync:
                wave = 0.5 + 0.5 * math.cos(phase * math.pi * 2)
            else:
                wave = 0.5 + 0.5 * math.sin(t * math.pi * 2)
            wave = min(1.0, wave + 0.55 * kick_punch)
            level = (1.0 - depth) + depth * wave
            for name in names:
                src = _src_colour(name, ambient, names)
                out[name] = _boost_rgb(src, inten * level)

        elif eid == "strobe":
            duty = p.get("duty", 0.15)
            inten = p.get("intensity", 1.0)
            flash_phase = (t % 1.0) if inst.bpm_sync else ((now * speed) % 1.0)
            on = flash_phase < duty or kick_punch > 0.4
            for name in names:
                if on:
                    src = _src_colour(name, ambient, names)
                    out[name] = _boost_rgb(src, inten)
                else:
                    out[name] = (0, 0, 0)

        elif eid == "fire":
            inten = p.get("intensity", 0.9)
            warmth = p.get("warmth", 0.85)
            fire_state = inst._fire_state
            for name in names:
                prev = fire_state.get(name, 0.6)
                target = 0.35 + 0.65 * inst._rng.random()
                level = prev * 0.55 + target * 0.45
                fire_state[name] = level
                src = _src_colour(name, ambient, names)
                wr, wg, wb = src
                wr = int(wr * (0.6 + 0.4 * warmth) + 255 * 0.25 * warmth)
                wg = int(wg * (0.5 + 0.3 * warmth))
                wb = int(wb * (1.0 - 0.5 * warmth))
                out[name] = _scale_rgb((min(255, wr), min(255, wg), min(255, wb)), level * inten)

        elif eid == "sparkle":
            dens = p.get("density", 0.12)
            inten = p.get("intensity", 1.0)
            sparkle_state = inst._sparkle_state
            spd = max(0.2, float(speed) if speed else 1.0)
            for name in names:
                life = sparkle_state.get(name, 0.0)
                if life <= 0 and inst._rng.random() < dens * 0.15 * spd:
                    life = 1.0
                life = max(0.0, life - 0.08 * spd)
                sparkle_state[name] = life
                src = _src_colour(name, ambient, names)
                out[name] = _boost_rgb(src, life * inten) if life > 0 else (0, 0, 0)

        elif eid == "bump":
            hold = max(0.02, float(p.get("hold", 0.08) or 0.08))
            inten = p.get("intensity", 1.0)
            age = phase
            if kick_age < 1.0:
                age = min(phase, kick_age)
            punch = kick_punch > 0.05
            if age < hold or punch:
                fall = kick_punch if punch else max(0.0, 1.0 - (age / hold))
                for name in names:
                    src = _src_colour(name, ambient, names)
                    out[name] = _boost_rgb(src, fall * inten)
            else:
                for name in names:
                    out[name] = (0, 0, 0)

        elif eid == "spectrum":
            bands = self.get_spectrum()
            inten = p.get("intensity", 1.0)
            sens = p.get("sensitivity", 1.5)
            nb = max(1, len(bands))
            for i, name in enumerate(names):
                bi = int(i / n * nb) if n else 0
                bi = min(nb - 1, bi)
                level = _clamp((bands[bi] if bands else 0.0) * sens)
                src = _src_colour(name, ambient, names)
                out[name] = _boost_rgb(src, level * inten)

        elif eid == "alternate":
            inten = p.get("intensity", 1.2)
            side = int(t) % 2
            if kick_punch > 0.5:
                side = 1 - side
            for i, name in enumerate(names):
                src = _src_colour(name, ambient, names)
                on = (i % 2) == side
                out[name] = _boost_rgb(src, inten if on else inten * 0.15)

        elif eid == "bounce":
            size = p.get("size", 0.3) * n
            inten = p.get("intensity", 1.0)
            tri = abs((t % 2.0) - 1.0)
            pos = tri * (n - 1)
            for i, name in enumerate(names):
                dist = abs(i - pos)
                fall = max(0.0, 1.0 - dist / max(0.5, size))
                src = _src_colour(name, ambient, names)
                out[name] = _boost_rgb(src, fall * inten) if fall > 0 else (0, 0, 0)

        else:
            for name in names:
                out[name] = (0, 0, 0)

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
            try:
                overlay = self._gen(inst, names, now, result)
            except Exception as e:
                log.warning(f"Effect {inst.effect_id} failed: {e}")
                continue
            for name, rgb in overlay.items():
                base = result.get(name, (0, 0, 0))
                result[name] = _blend(base, rgb, inst.blend, 1.0)

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
