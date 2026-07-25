"""
MIDI input controller with learn mode and soft takeover.

Backend order:
  1. Windows winmm.dll (ctypes) — no pip build, works on any Python
  2. mido + python-rtmidi
  3. mido + pygame
  4. mido + rtmidi_python
"""

from __future__ import annotations

import logging
import os
import platform
import threading
import time
from typing import Callable

log = logging.getLogger("pixel-mapping-osc")

_BACKENDS = (
    "mido.backends.rtmidi",
    "mido.backends.pygame",
    "mido.backends.rtmidi_python",
)

_backend_name: str | None = None


class MidiController:
    def __init__(self, on_action: Callable, get_config: Callable, save_mappings: Callable):
        self.on_action = on_action
        self.get_config = get_config
        self.save_mappings = save_mappings
        self.port = None
        self.port_name = None
        self.active = False
        self.error = None
        self.learning = None
        self.last_event = None
        self.backend = None
        self._thread = None
        self._stop = threading.Event()
        self._pickup: dict[str, float] = {}
        self._pickup_armed: dict[str, bool] = {}
        self._use_winmm = False

    @staticmethod
    def _winmm_available() -> bool:
        return platform.system() == "Windows"

    @staticmethod
    def list_inputs(rescan=False):
        # Prefer native Windows MIDI — never needs compiling
        if MidiController._winmm_available():
            try:
                from winmm_midi import WinMMInput
                names = WinMMInput.list_names()
                return {
                    "error": None,
                    "backend": "winmm",
                    "devices": [{"index": i, "name": n} for i, n in enumerate(names)],
                }
            except Exception as e:
                log.warning(f"WinMM MIDI list failed: {e}")

        try:
            mido = MidiController._ensure_mido_backend()
            names = mido.get_input_names()
            return {
                "error": None,
                "backend": _backend_name,
                "devices": [{"index": i, "name": n} for i, n in enumerate(names)],
            }
        except Exception as e:
            hint = (
                "No MIDI backend available. "
                "On Windows this app uses built-in WinMM (no extra install). "
                f"Detail: {e}"
            )
            return {"error": hint, "backend": None, "devices": []}

    @staticmethod
    def _ensure_mido_backend():
        global _backend_name
        if _backend_name:
            import mido
            return mido

        import mido
        forced = os.environ.get("MIDO_BACKEND", "").strip()
        candidates = (forced,) if forced else _BACKENDS
        errors = []
        for backend in candidates:
            if not backend:
                continue
            try:
                mido.set_backend(backend)
                mido.get_input_names()
                _backend_name = backend
                log.info(f"MIDI backend: {backend}")
                return mido
            except Exception as e:
                errors.append(f"{backend}: {e}")
        raise RuntimeError(" | ".join(errors) if errors else "No mido backend")

    def start(self, device_name: str | None = None):
        self.stop()
        self.error = None
        cfg = self.get_config().get("midi", {})
        name = device_name if device_name is not None else cfg.get("device", "")

        # 1) Windows native
        if self._winmm_available():
            try:
                from winmm_midi import WinMMInput
                port = WinMMInput()
                port.open(name if name is not None else "")
                self.port = port
                self.port_name = port.name
                self.backend = "winmm"
                self._use_winmm = True
                self.active = True
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
                return True
            except Exception as e:
                log.warning(f"WinMM MIDI open failed, trying mido: {e}")
                self.error = str(e)

        # 2) mido backends
        self._use_winmm = False
        try:
            mido = self._ensure_mido_backend()
            self.backend = _backend_name
        except Exception as e:
            self.error = (
                f"MIDI open failed: {self.error or e}. "
                "On Windows, plug in a controller and try again — WinMM needs no pip packages."
            )
            return False

        try:
            target = None
            names = mido.get_input_names()
            if isinstance(name, int) or (isinstance(name, str) and str(name).strip().isdigit()):
                idx = int(name)
                if 0 <= idx < len(names):
                    target = names[idx]
            elif name and str(name).strip():
                needle = str(name).lower()
                for n in names:
                    if needle in n.lower():
                        target = n
                        break
                if target is None:
                    self.error = f'No MIDI input matching "{name}"'
                    return False
            else:
                if not names:
                    self.error = "No MIDI input devices found — plug in a controller and click Refresh"
                    return False
                target = names[0]

            self.port = mido.open_input(target)
            self.port_name = target
            self.active = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            log.info(f'MIDI listening on "{target}" via {_backend_name}')
            return True
        except Exception as e:
            self.error = f"MIDI open failed: {e}"
            self.port = None
            return False

    def stop(self):
        self.active = False
        self._stop.set()
        if self.port:
            try:
                if self._use_winmm:
                    self.port.close()
                else:
                    self.port.close()
            except Exception:
                pass
            self.port = None
        self.port_name = None
        self._use_winmm = False

    def start_learn(self, action_id: str):
        self.learning = action_id
        return {"learning": action_id}

    def cancel_learn(self):
        self.learning = None
        return {"learning": None}

    def unmap(self, action_id: str):
        cfg = self.get_config()
        midi = cfg.setdefault("midi", {})
        mappings = midi.setdefault("mappings", [])
        midi["mappings"] = [m for m in mappings if m.get("action") != action_id]
        self.save_mappings(cfg)
        return {"mappings": midi["mappings"]}

    def clear_mappings(self):
        cfg = self.get_config()
        midi = cfg.setdefault("midi", {})
        midi["mappings"] = []
        self.save_mappings(cfg)
        return {"mappings": []}

    def state(self):
        cfg = self.get_config().get("midi", {})
        return {
            "active": self.active,
            "device": self.port_name,
            "error": self.error,
            "learning": self.learning,
            "last_event": self.last_event,
            "mappings": cfg.get("mappings", []),
            "backend": self.backend or ("winmm" if self._winmm_available() else _backend_name),
        }

    def _msg_key(self, msg) -> dict | None:
        if msg.type == "note_on" and msg.velocity > 0:
            return {"type": "note", "channel": msg.channel, "number": msg.note}
        if msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            return {"type": "note_off", "channel": msg.channel, "number": msg.note}
        if msg.type == "control_change":
            return {"type": "cc", "channel": msg.channel, "number": msg.control}
        return None

    def _run(self):
        while self.active and not self._stop.is_set():
            try:
                for msg in self.port.iter_pending():
                    self._handle(msg)
            except Exception as e:
                self.error = str(e)
                log.warning(f"MIDI read error: {e}")
                break
            time.sleep(0.005)
        self.active = False

    def _handle(self, msg):
        key = self._msg_key(msg)
        if not key:
            return
        self.last_event = {
            **key,
            "value": getattr(msg, "velocity", None) if key["type"].startswith("note")
            else getattr(msg, "value", None),
            "t": time.time(),
        }

        if self.learning and key["type"] in ("note", "cc"):
            action_id = self.learning
            self.learning = None
            mapping = {
                "action": action_id,
                "type": key["type"],
                "channel": key["channel"],
                "number": key["number"],
                "mode": "toggle" if key["type"] == "note" else "absolute",
                "invert": False,
            }
            cfg = self.get_config()
            midi = cfg.setdefault("midi", {})
            mappings = [m for m in midi.get("mappings", []) if m.get("action") != action_id]
            conflict = next(
                (m for m in mappings
                 if m.get("type") == mapping["type"]
                 and m.get("channel") == mapping["channel"]
                 and m.get("number") == mapping["number"]),
                None,
            )
            if conflict:
                mappings = [m for m in mappings if m is not conflict]
            mappings.append(mapping)
            midi["mappings"] = mappings
            if self.port_name:
                midi["device"] = self.port_name
            self.save_mappings(cfg)
            self.last_event["learned"] = action_id
            self.last_event["conflict"] = conflict["action"] if conflict else None
            log.info(f"MIDI learned {action_id} <- {mapping}")
            return

        if key["type"] == "note_off":
            return

        cfg = self.get_config().get("midi", {})
        mappings = cfg.get("mappings", [])
        for m in mappings:
            if (m.get("type") == key["type"]
                    and int(m.get("channel", 0)) == key["channel"]
                    and int(m.get("number", 0)) == key["number"]):
                self._dispatch(m, msg)

    def _dispatch(self, mapping: dict, msg):
        action_id = mapping["action"]
        mode = mapping.get("mode", "toggle")
        invert = bool(mapping.get("invert", False))

        if mapping["type"] == "note":
            try:
                self.on_action(action_id)
            except Exception as e:
                log.warning(f"MIDI action {action_id}: {e}")
            return

        raw = float(msg.value) / 127.0
        if invert:
            raw = 1.0 - raw

        if mode == "absolute" and mapping.get("pickup", True):
            pk = f"{mapping['channel']}:{mapping['number']}:{action_id}"
            if pk not in self._pickup_armed:
                self._pickup_armed[pk] = False
                self._pickup[pk] = raw
            if not self._pickup_armed[pk]:
                prev = self._pickup.get(pk, raw)
                if abs(raw - prev) < 0.02 or abs(raw - prev) > 0.15:
                    self._pickup[pk] = raw
                    if abs(raw - prev) >= 0.01:
                        self._pickup_armed[pk] = True
                    else:
                        return
                else:
                    return
            self._pickup[pk] = raw

        threshold = float(mapping.get("threshold", 0.5))
        try:
            if mode in ("trigger", "toggle"):
                if raw >= threshold:
                    self.on_action(action_id)
            else:
                self.on_action(action_id, value=raw)
        except Exception as e:
            log.warning(f"MIDI action {action_id}: {e}")
