"""
Shared action registry — one command table for UI, webhooks, and MIDI.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("pixel-mapping-osc")


class ActionRegistry:
    def __init__(self):
        self._actions: dict[str, dict] = {}

    def register(
        self,
        action_id: str,
        handler: Callable[..., Any],
        *,
        label: str,
        kind: str = "trigger",  # trigger | toggle | continuous
        min_v: float = 0.0,
        max_v: float = 1.0,
        description: str = "",
        category: str = "general",
    ):
        self._actions[action_id] = {
            "id": action_id,
            "label": label,
            "kind": kind,
            "min": min_v,
            "max": max_v,
            "description": description,
            "category": category,
            "handler": handler,
        }

    def list_actions(self) -> list[dict]:
        return [
            {k: v for k, v in a.items() if k != "handler"}
            for a in sorted(self._actions.values(), key=lambda x: (x["category"], x["id"]))
        ]

    def get(self, action_id: str) -> dict | None:
        return self._actions.get(action_id)

    def invoke(self, action_id: str, value: float | None = None, **kwargs) -> dict:
        meta = self._actions.get(action_id)
        if not meta:
            return {"ok": False, "error": f"Unknown action: {action_id}"}
        try:
            handler = meta["handler"]
            kind = meta["kind"]
            if kind == "continuous":
                if value is None:
                    return {"ok": False, "error": "continuous action requires value"}
                lo, hi = meta["min"], meta["max"]
                scaled = lo + float(value) * (hi - lo) if 0.0 <= float(value) <= 1.0 else float(value)
                scaled = max(lo, min(hi, scaled))
                result = handler(scaled, **kwargs)
            else:
                result = handler(value, **kwargs) if value is not None else handler(**kwargs)
            if isinstance(result, dict):
                out = {"ok": True, "action": action_id, **result}
            else:
                out = {"ok": True, "action": action_id, "result": result}
            return out
        except Exception as e:
            log.warning(f"Action {action_id} failed: {e}")
            return {"ok": False, "action": action_id, "error": str(e)}


def build_actions(engine, effects, tempo, midi=None) -> ActionRegistry:
    """Wire all bridge / effect / MIDI actions. Called after engine exists."""
    reg = ActionRegistry()

    # ---- bridge transport ----
    def start(**_):
        engine.start()
        return {"running": engine.running}

    def stop(**_):
        engine.stop()
        return {"running": engine.running}

    def pause(**_):
        if engine.enabled:
            engine.toggle()
        return {"enabled": engine.enabled}

    def resume(**_):
        if not engine.enabled:
            engine.toggle()
        if not engine.running:
            engine.start()
        return {"enabled": engine.enabled, "running": engine.running}

    def toggle(**_):
        enabled = engine.toggle()
        return {"enabled": enabled}

    def fog_on(**_):
        return {"fog_enabled": engine.set_fog(True)}

    def fog_off(**_):
        return {"fog_enabled": engine.set_fog(False)}

    def fog_toggle(**_):
        return {"fog_enabled": engine.toggle_fog()}

    def kick_on(**_):
        return {"kick_enabled": engine.set_kick_strobe(True), "error": engine.kick.error}

    def kick_off(**_):
        return {"kick_enabled": engine.set_kick_strobe(False)}

    def kick_toggle(**_):
        return {"kick_enabled": engine.toggle_kick_strobe(), "error": engine.kick.error}

    reg.register("start", start, label="Start", category="transport", description="Start the bridge")
    reg.register("stop", stop, label="Stop", category="transport", description="Stop and clear overrides")
    reg.register("pause", pause, label="Pause", category="transport", description="Clear overrides, LD takes over")
    reg.register("resume", resume, label="Resume", category="transport", description="Resume ambient control")
    reg.register("toggle", toggle, label="Toggle Pause/Resume", category="transport", kind="toggle")
    reg.register("fog_on", fog_on, label="Fog On", category="fog")
    reg.register("fog_off", fog_off, label="Fog Off", category="fog")
    reg.register("fog_toggle", fog_toggle, label="Toggle Fog", category="fog", kind="toggle")
    reg.register("kick_on", kick_on, label="Kick Strobe On", category="kick")
    reg.register("kick_off", kick_off, label="Kick Strobe Off", category="kick")
    reg.register("kick_toggle", kick_toggle, label="Toggle Kick Strobe", category="kick", kind="toggle")

    # ---- tempo ----
    def tap_tempo(**_):
        bpm = tempo.tap()
        engine.config["tempo"] = engine.config.get("tempo", {})
        engine.config["tempo"]["bpm"] = bpm
        return {"bpm": bpm}

    def set_bpm(value, **_):
        bpm = tempo.set_bpm(value)
        engine.config.setdefault("tempo", {})["bpm"] = bpm
        return {"bpm": bpm}

    reg.register("tap_tempo", tap_tempo, label="Tap Tempo", category="tempo")
    reg.register("set_bpm", set_bpm, label="BPM", category="tempo", kind="continuous",
                 min_v=40, max_v=240, description="Set BPM (40-240)")

    # ---- master / continuous ----
    def set_master(value, **_):
        engine.config["master_brightness"] = int(max(0, min(255, value)))
        return {"master_brightness": engine.config["master_brightness"]}

    def set_smoothing(value, **_):
        engine.config["smoothing"] = float(max(0.01, min(1.0, value)))
        return {"smoothing": engine.config["smoothing"]}

    def set_colour_boost(value, **_):
        engine.config["colour_boost"] = float(max(1.0, min(8.0, value)))
        return {"colour_boost": engine.config["colour_boost"]}

    reg.register("master_brightness", set_master, label="Master Brightness",
                 category="params", kind="continuous", min_v=0, max_v=255)
    reg.register("smoothing", set_smoothing, label="Smoothing",
                 category="params", kind="continuous", min_v=0.01, max_v=1.0)
    reg.register("colour_boost", set_colour_boost, label="Colour Boost",
                 category="params", kind="continuous", min_v=1.0, max_v=8.0)

    # ---- effects ----
    def effect_start(value=None, effect_id=None, group=None, **kwargs):
        eid = effect_id or kwargs.get("effect") or "chase"
        grp = group or kwargs.get("group")
        params = kwargs.get("params")
        inst = effects.start_effect(eid, group=grp, params=params)
        return {"effect": inst.to_dict()}

    def effect_stop(value=None, effect_id=None, group=None, **kwargs):
        eid = effect_id or kwargs.get("effect")
        n = effects.stop_effect(eid, group=group or kwargs.get("group"))
        return {"stopped": n}

    def effect_clear(**_):
        return {"cleared": effects.clear_effects()}

    def effect_param(value, effect_id=None, param=None, group=None, **kwargs):
        eid = effect_id or kwargs.get("effect")
        pname = param or kwargs.get("param")
        if not eid or not pname:
            return {"ok": False, "error": "effect_id and param required"}
        ok = effects.set_param(eid, pname, value, group=group)
        return {"updated": ok, "effect_id": eid, "param": pname, "value": value}

    # Register one start/stop per known effect for easy MIDI/webhook mapping
    from effects import EFFECT_DEFS
    for eid, meta in EFFECT_DEFS.items():
        def _make_start(e=eid):
            def _fn(value=None, group=None, **kwargs):
                inst = effects.start_effect(e, group=group or kwargs.get("group"))
                return {"effect": inst.to_dict()}
            return _fn

        def _make_stop(e=eid):
            def _fn(value=None, group=None, **kwargs):
                n = effects.stop_effect(e, group=group or kwargs.get("group"))
                return {"stopped": n}
            return _fn

        def _make_toggle(e=eid):
            def _fn(value=None, group=None, **kwargs):
                grp = group or kwargs.get("group")
                active = [i for i in effects.instances if i.effect_id == e and i.group == grp]
                if active:
                    effects.stop_effect(e, group=grp)
                    return {"active": False, "effect_id": e}
                inst = effects.start_effect(e, group=grp)
                return {"active": True, "effect": inst.to_dict()}
            return _fn

        reg.register(f"effect_{eid}_start", _make_start(), label=f"{meta['label']} Start",
                     category="effects")
        reg.register(f"effect_{eid}_stop", _make_stop(), label=f"{meta['label']} Stop",
                     category="effects")
        reg.register(f"effect_{eid}_toggle", _make_toggle(), label=f"{meta['label']} Toggle",
                     category="effects", kind="toggle")

    reg.register("effect_start", effect_start, label="Start Effect", category="effects",
                 description="Pass effect_id / group in body or query")
    reg.register("effect_stop", effect_stop, label="Stop Effect", category="effects")
    reg.register("effect_clear", effect_clear, label="Clear All Effects", category="effects")
    reg.register("effect_param", effect_param, label="Effect Parameter", category="effects",
                 kind="continuous", min_v=0, max_v=1)

    # Continuous shortcuts for active effect intensity/speed
    def set_fx_intensity(value, **_):
        for i in effects.instances:
            if "intensity" in i.params:
                effects.set_param(i.effect_id, "intensity", value, group=i.group)
        return {"intensity": value}

    def set_fx_speed(value, **_):
        for i in effects.instances:
            if "speed" in i.params:
                # map 0-1 to each effect's speed range
                from effects import EFFECT_DEFS as ED
                spec = ED[i.effect_id]["params"]["speed"]
                scaled = spec["min"] + float(value) * (spec["max"] - spec["min"])
                effects.set_param(i.effect_id, "speed", scaled, group=i.group)
        return {"speed": value}

    reg.register("fx_intensity", set_fx_intensity, label="Effect Intensity",
                 category="effects", kind="continuous", min_v=0, max_v=1)
    reg.register("fx_speed", set_fx_speed, label="Effect Speed",
                 category="effects", kind="continuous", min_v=0, max_v=1)

    # ---- scenes / solo / blackout ----
    def scene_recall(value=None, scene=None, fade_ms=None, **kwargs):
        name = scene or kwargs.get("name")
        if not name:
            return {"ok": False, "error": "scene name required"}
        # Build current colour map
        current = {k: v[:3] for k, v in engine.current_colours.items()}
        ok = effects.scenes.recall(name, current, fade_ms=fade_ms)
        return {"recalled": ok, "scene": name}

    def scene_clear(**_):
        effects.scenes.clear_active()
        return {"active_scene": None}

    def solo(value=None, group=None, **kwargs):
        g = group or kwargs.get("group")
        return {"solo_group": effects.set_solo(g)}

    def solo_toggle(value=None, group=None, **kwargs):
        g = group or kwargs.get("group")
        if not g:
            effects.set_solo(None)
            return {"solo_group": None}
        return {"solo_group": effects.toggle_solo(g)}

    def blackout(**_):
        effects.start_master_fade(engine.current_colours, to_rgb=(0, 0, 0), fade_ms=0)
        effects.blackout = True
        return {"blackout": True}

    def blackout_fade(value=None, fade_ms=1000, **kwargs):
        ms = int(kwargs.get("fade_ms", fade_ms) or 1000)
        effects.start_master_fade(engine.current_colours, to_rgb=(0, 0, 0), fade_ms=ms)
        return {"blackout": True, "fade_ms": ms}

    def restore(**_):
        effects.clear_master_fade()
        effects.scenes.clear_active()
        return {"blackout": False}

    reg.register("scene_recall", scene_recall, label="Recall Scene", category="scenes")
    reg.register("scene_clear", scene_clear, label="Clear Scene", category="scenes")
    reg.register("solo", solo, label="Solo Group", category="scenes")
    reg.register("solo_toggle", solo_toggle, label="Toggle Solo", category="scenes", kind="toggle")
    reg.register("solo_clear", lambda **_: {"solo_group": effects.set_solo(None)},
                 label="Clear Solo", category="scenes")
    reg.register("blackout", blackout, label="Blackout", category="scenes")
    reg.register("blackout_fade", blackout_fade, label="Fade to Black", category="scenes")
    reg.register("restore", restore, label="Restore from Blackout", category="scenes")

    # Dynamic scene_*_recall for each saved scene (refreshed via refresh_scene_actions)
    def refresh_scene_actions():
        # remove old dynamic scene actions
        to_del = [k for k in reg._actions if k.startswith("scene_") and k.endswith("_recall")
                  and k != "scene_recall"]
        for k in to_del:
            del reg._actions[k]
        for s in effects.scenes.list_scenes():
            name = s["name"]
            aid = f"scene_{_slug(name)}_recall"

            def _make(n=name):
                def _fn(**kwargs):
                    current = {k: v[:3] for k, v in engine.current_colours.items()}
                    ok = effects.scenes.recall(n, current, fade_ms=kwargs.get("fade_ms"))
                    return {"recalled": ok, "scene": n}
                return _fn

            reg.register(aid, _make(), label=f"Scene: {name}", category="scenes")

    reg.refresh_scene_actions = refresh_scene_actions
    refresh_scene_actions()

    return reg


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name).strip("_").lower() or "unnamed"
