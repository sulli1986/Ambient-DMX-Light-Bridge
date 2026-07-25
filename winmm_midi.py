"""
Windows MIDI input via winmm.dll (ctypes) — no pygame / python-rtmidi build needed.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

log = logging.getLogger("pixel-mapping-osc")

# ---- winmm types ----

MMRESULT = wt.UINT
HMIDIIN = wt.HANDLE
DWORD_PTR = ctypes.c_size_t

CALLBACK_FUNCTION = 0x00030000
MIM_DATA = 0x3C3
MIM_OPEN = 0x3C1
MIM_CLOSE = 0x3C2
MIM_LONGDATA = 0x3C4
MIM_ERROR = 0x3C5

MAXPNAMELEN = 32


class MIDIINCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wt.WORD),
        ("wPid", wt.WORD),
        ("vDriverVersion", wt.DWORD),
        ("szPname", wt.WCHAR * MAXPNAMELEN),
        ("dwSupport", wt.DWORD),
    ]


MidiInProc = ctypes.WINFUNCTYPE(
    None,
    HMIDIIN,
    wt.UINT,
    DWORD_PTR,
    DWORD_PTR,
    DWORD_PTR,
)


@dataclass
class MidiMsg:
    type: str
    channel: int = 0
    note: int = 0
    velocity: int = 0
    control: int = 0
    value: int = 0


def _decode_short(dw_param1: int) -> MidiMsg | None:
    status = dw_param1 & 0xFF
    data1 = (dw_param1 >> 8) & 0xFF
    data2 = (dw_param1 >> 16) & 0xFF
    cmd = status & 0xF0
    channel = status & 0x0F

    if cmd == 0x90:  # note on
        if data2 == 0:
            return MidiMsg("note_off", channel=channel, note=data1, velocity=0)
        return MidiMsg("note_on", channel=channel, note=data1, velocity=data2)
    if cmd == 0x80:  # note off
        return MidiMsg("note_off", channel=channel, note=data1, velocity=data2)
    if cmd == 0xB0:  # CC
        return MidiMsg("control_change", channel=channel, control=data1, value=data2)
    return None


class WinMMInput:
    """Open a Windows MIDI input device and queue short messages."""

    def __init__(self):
        self._winmm = ctypes.WinDLL("winmm")
        self._setup_apis()
        self._handle = HMIDIIN()
        self._queue: deque[MidiMsg] = deque(maxlen=2048)
        self._lock = threading.Lock()
        self._callback = MidiInProc(self._midi_proc)
        self._open = False
        self.name = None

    def _setup_apis(self):
        self._winmm.midiInGetNumDevs.restype = wt.UINT
        self._winmm.midiInGetDevCapsW.argtypes = [wt.UINT, ctypes.POINTER(MIDIINCAPS), wt.UINT]
        self._winmm.midiInGetDevCapsW.restype = MMRESULT
        self._winmm.midiInOpen.argtypes = [
            ctypes.POINTER(HMIDIIN), wt.UINT, MidiInProc, DWORD_PTR, wt.DWORD
        ]
        self._winmm.midiInOpen.restype = MMRESULT
        self._winmm.midiInStart.argtypes = [HMIDIIN]
        self._winmm.midiInStart.restype = MMRESULT
        self._winmm.midiInStop.argtypes = [HMIDIIN]
        self._winmm.midiInStop.restype = MMRESULT
        self._winmm.midiInReset.argtypes = [HMIDIIN]
        self._winmm.midiInReset.restype = MMRESULT
        self._winmm.midiInClose.argtypes = [HMIDIIN]
        self._winmm.midiInClose.restype = MMRESULT

    @staticmethod
    def list_names() -> list[str]:
        winmm = ctypes.WinDLL("winmm")
        winmm.midiInGetNumDevs.restype = wt.UINT
        winmm.midiInGetDevCapsW.argtypes = [wt.UINT, ctypes.POINTER(MIDIINCAPS), wt.UINT]
        winmm.midiInGetDevCapsW.restype = MMRESULT
        n = winmm.midiInGetNumDevs()
        names = []
        for i in range(n):
            caps = MIDIINCAPS()
            if winmm.midiInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                names.append(caps.szPname)
            else:
                names.append(f"MIDI In {i}")
        return names

    def open(self, name_or_index) -> bool:
        self.close()
        names = self.list_names()
        if not names:
            raise RuntimeError("No MIDI input devices found")

        idx = None
        if isinstance(name_or_index, int) or (
            isinstance(name_or_index, str) and name_or_index.strip().isdigit()
        ):
            idx = int(name_or_index)
        elif name_or_index and str(name_or_index).strip():
            needle = str(name_or_index).lower()
            for i, n in enumerate(names):
                if needle in n.lower():
                    idx = i
                    break
            if idx is None:
                raise RuntimeError(f'No MIDI input matching "{name_or_index}"')
        else:
            idx = 0

        if idx < 0 or idx >= len(names):
            raise RuntimeError(f"MIDI device index out of range: {idx}")

        result = self._winmm.midiInOpen(
            ctypes.byref(self._handle),
            wt.UINT(idx),
            self._callback,
            0,
            CALLBACK_FUNCTION,
        )
        if result != 0:
            raise RuntimeError(f"midiInOpen failed (code {result})")

        result = self._winmm.midiInStart(self._handle)
        if result != 0:
            self._winmm.midiInClose(self._handle)
            raise RuntimeError(f"midiInStart failed (code {result})")

        self._open = True
        self.name = names[idx]
        log.info(f'WinMM MIDI listening on "{self.name}"')
        return True

    def close(self):
        if not self._open:
            return
        try:
            self._winmm.midiInStop(self._handle)
            self._winmm.midiInReset(self._handle)
            self._winmm.midiInClose(self._handle)
        except Exception:
            pass
        self._open = False
        self.name = None
        with self._lock:
            self._queue.clear()

    def iter_pending(self):
        with self._lock:
            while self._queue:
                yield self._queue.popleft()

    def _midi_proc(self, handle, msg, instance, param1, param2):
        if msg != MIM_DATA:
            return
        decoded = _decode_short(int(param1))
        if decoded is None:
            return
        with self._lock:
            self._queue.append(decoded)
