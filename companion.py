#!/usr/bin/env python3
"""
Compact always-on-top control strip for beside ProPresenter.

Requires the main bridge running (python app.py), then:
    python companion.py

Talks to http://127.0.0.1:5000 — no browser chrome.
"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import font as tkfont
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:5000"
POLL_MS = 500
WIN_W = 168
WIN_H = 460


def api(method: str, path: str, body: dict | None = None) -> dict:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(BASE + path, data=data, headers=headers, method=method)
    with urlopen(req, timeout=1.5) as resp:
        raw = resp.read().decode("utf-8") or "{}"
        return json.loads(raw)


class CompanionApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Lights")
        self.root.geometry(f"{WIN_W}x{WIN_H}+40+80")
        self.root.minsize(140, 300)
        self.root.configure(bg="#0e0f11")
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-toolwindow", True)  # Windows: compact title bar
        except tk.TclError:
            pass

        self._busy = False
        self._build()
        self.root.after(200, self._poll)

    def _build(self):
        f_title = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        f_small = tkfont.Font(family="Segoe UI", size=8)
        f_btn = tkfont.Font(family="Segoe UI", size=9, weight="bold")

        tk.Label(
            self.root, text="LIGHTS", font=f_title,
            fg="#e8673a", bg="#0e0f11"
        ).pack(pady=(10, 2))

        self.status = tk.Label(
            self.root, text="Connecting…", font=f_small,
            fg="#6b7280", bg="#0e0f11", wraplength=WIN_W - 16, justify="center"
        )
        self.status.pack(padx=8, pady=2)

        self.detail = tk.Label(
            self.root, text="", font=f_small,
            fg="#6b7280", bg="#0e0f11", wraplength=WIN_W - 16, justify="center"
        )
        self.detail.pack(padx=8, pady=(0, 8))

        self.btns = {}

        def add(key, label, cmd, color="#1e2026", fg="#e8eaf0"):
            b = tk.Button(
                self.root, text=label, command=cmd,
                font=f_btn, bg=color, fg=fg,
                activebackground="#2a2d35", activeforeground=fg,
                relief="flat", bd=0, highlightthickness=0,
                padx=6, pady=7, cursor="hand2",
            )
            b.pack(fill="x", padx=10, pady=3)
            self.btns[key] = b

        add("start", "Start", lambda: self._post("/api/start"), "#3ddc84", "#000")
        add("stop", "Stop", lambda: self._post("/api/stop"), "#ff5c5c", "#fff")
        add("pause", "Pause", lambda: self._post("/api/toggle"), "#e8673a", "#fff")
        add("fog", "Fog", lambda: self._post("/api/fog-toggle"))
        add("kick", "Kick Strobe", lambda: self._post("/api/kick-toggle"))
        add("fxclear", "Clear Effects", lambda: self._post("/api/effects/clear"))
        add("blackout", "Blackout", lambda: self._post("/api/blackout", {"fade_ms": 0}), "#ff5c5c", "#fff")
        add("restore", "Restore", lambda: self._post("/api/blackout", {"restore": True}), "#3ddc84", "#000")

        self.topmost_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            self.root, text="Always on top", variable=self.topmost_var,
            command=lambda: self.root.attributes("-topmost", bool(self.topmost_var.get())),
            font=f_small, fg="#6b7280", bg="#0e0f11",
            selectcolor="#1e2026", activebackground="#0e0f11",
            activeforeground="#e8eaf0", highlightthickness=0,
        ).pack(pady=(10, 2))

        tk.Label(
            self.root, text="Resize · stays on top", font=f_small,
            fg="#3a3d45", bg="#0e0f11"
        ).pack(side="bottom", pady=6)

    def _post(self, path, body=None):
        if self._busy:
            return
        self._busy = True

        def work():
            try:
                api("POST", path) if body is None else api("POST", path, body)
            except Exception as e:
                self.root.after(0, lambda: self.status.config(text=f"Error", fg="#ff5c5c"))
                self.root.after(0, lambda err=e: self.detail.config(text=str(err)[:80]))
            finally:
                self._busy = False
                self.root.after(50, self._fetch_state)

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        self._fetch_state()
        self.root.after(POLL_MS, self._poll)

    def _fetch_state(self):
        def work():
            try:
                s = api("GET", "/api/state")
                self.root.after(0, lambda: self._apply(s, None))
            except Exception as e:
                self.root.after(0, lambda: self._apply(None, e))

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, s, err):
        if err is not None:
            self.status.config(text="Bridge offline", fg="#ff5c5c")
            self.detail.config(text="Run python app.py first")
            return

        running = s.get("running")
        enabled = s.get("enabled")
        if not running:
            self.status.config(text="Stopped", fg="#ff5c5c")
        elif not enabled:
            self.status.config(text="Paused", fg="#ffb830")
        else:
            self.status.config(text="Running", fg="#3ddc84")

        bits = [f"{s.get('fps', 0)} fps"]
        if s.get("fog_enabled"):
            bits.append("fog")
        if s.get("kick_enabled"):
            bits.append("kick")
        fx = (s.get("effects") or {}).get("effects") or []
        if fx:
            bits.append(f"{len(fx)} fx")
        if (s.get("midi") or {}).get("active"):
            bits.append("midi")
        if (s.get("effects") or {}).get("blackout"):
            bits.append("BO")
        self.detail.config(text=" · ".join(bits))

        self.btns["pause"].config(text="Resume" if (running and not enabled) else "Pause")
        self.btns["fog"].config(text="Fog Off" if s.get("fog_enabled") else "Fog On")
        self.btns["kick"].config(text="Kick Off" if s.get("kick_enabled") else "Kick On")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    CompanionApp().run()
