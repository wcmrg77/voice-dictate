#!/usr/bin/env python3
"""
Voice Dictate
Shortcut: Ctrl + Shift (hold → release to transcribe)
Transcription: Mistral Voxtral Mini
"""

import os
import sys
import json
import math
import time
import queue
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

try:
    import tkinter as tk
except ImportError:
    print("❌ tkinter not found. Install with: brew install python-tk@3.13")
    sys.exit(1)

import numpy as np
import scipy.io.wavfile as wavfile
import sounddevice as sd
import pyperclip
from pynput import keyboard


# ── Config ────────────────────────────────────────────────────────────────────

SAMPLE_RATE = 16000
CHANNELS = 1
ENV_PATH = Path(__file__).parent / ".env"


# ── .env ──────────────────────────────────────────────────────────────────────

def load_env():
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

load_env()
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")


# ── State ─────────────────────────────────────────────────────────────────────

recording = False
audio_frames: list = []
lock = threading.Lock()
shift_down = False
ctrl_down = False
first_paste = True          # first transcription gets no leading space
ui_queue: queue.Queue = queue.Queue()


# ── Transcription ─────────────────────────────────────────────────────────────

def transcribe_audio(audio_path: Path) -> str:
    boundary = "----VoxtralBoundary9876543210"
    file_bytes = audio_path.read_bytes()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{audio_path.name}"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + file_bytes + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\n'
        f"voxtral-mini-latest"
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="language"\r\n\r\n'
        f"de"
        f"\r\n--{boundary}--\r\n"
    ).encode()

    req = urllib.request.Request(
        "https://api.mistral.ai/v1/audio/transcriptions",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result.get("text", "").strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"❌ API {e.code}: {body}", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        return ""


# ── Paste ─────────────────────────────────────────────────────────────────────

def paste_text(text: str):
    global first_paste
    if not first_paste:
        text = " " + text
    first_paste = False

    pyperclip.copy(text)
    time.sleep(0.15)
    subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to keystroke "v" using {command down}'],
        check=False,
        stderr=subprocess.DEVNULL,
    )


# ── Recording ─────────────────────────────────────────────────────────────────

def start_recording():
    global recording, audio_frames
    with lock:
        if recording:
            return
        recording = True
        audio_frames = []
    ui_queue.put("recording")
    print("🎙️  Recording...")


def stop_and_transcribe():
    global recording, audio_frames
    with lock:
        if not recording:
            return
        recording = False
        frames = list(audio_frames)

    if not frames:
        ui_queue.put("hide")
        return

    ui_queue.put("transcribing")
    audio_data = np.concatenate(frames, axis=0)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = Path(f.name)

    try:
        wavfile.write(tmp_path, SAMPLE_RATE, audio_data)
        text = transcribe_audio(tmp_path)
        if text:
            print(f"✅ {text}")
            paste_text(text)
        else:
            print("⚠️  Empty transcript.")
    finally:
        tmp_path.unlink(missing_ok=True)
        ui_queue.put("hide")


def audio_callback(indata, frames, time_info, status):
    if recording:
        audio_frames.append(indata.copy())


# ── Keyboard ──────────────────────────────────────────────────────────────────

def on_press(key):
    global shift_down, ctrl_down
    if key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
        shift_down = True
    elif key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
        ctrl_down = True

    if shift_down and ctrl_down:
        start_recording()


def on_release(key):
    global shift_down, ctrl_down
    was_recording = recording

    if key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
        shift_down = False
    elif key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
        ctrl_down = False
    elif key == keyboard.Key.esc:
        ui_queue.put("quit")
        return False

    if was_recording and (not shift_down or not ctrl_down):
        threading.Thread(target=stop_and_transcribe, daemon=True).start()


# ── Widget ────────────────────────────────────────────────────────────────────

class RecordingWidget:
    N_BARS  = 5
    BAR_W   = 5
    BAR_GAP = 4
    CVS_W   = N_BARS * BAR_W + (N_BARS - 1) * BAR_GAP
    CVS_H   = 20

    RED    = "#ff3b30"
    YELLOW = "#ffd60a"
    BG     = "#111111"

    def __init__(self):
        self.root = tk.Tk()
        self.root.wm_overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        self.root.wm_attributes("-alpha", 0.93)
        self.root.configure(bg=self.BG)

        outer = tk.Frame(self.root, bg=self.BG, padx=16, pady=9)
        outer.pack()

        self.canvas = tk.Canvas(
            outer, width=self.CVS_W, height=self.CVS_H,
            bg=self.BG, highlightthickness=0,
        )
        self.canvas.pack(side="left", padx=(0, 10))

        self.lbl = tk.Label(
            outer, text="Aufnahme",
            fg="white", bg=self.BG,
            font=("Helvetica Neue", 13, "bold"),
        )
        self.lbl.pack(side="left")

        self._bars: list = []
        self._init_bars(self.RED)
        self._tick = 0
        self._animating = False

        # Position: bottom-center, 28 px above dock
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w  = self.root.winfo_reqwidth()
        h  = self.root.winfo_reqheight()
        self.root.geometry(f"+{(sw - w) // 2}+{sh - h - 28}")
        self.root.withdraw()

        self.root.after(80, self._poll)

    # ── bar helpers ───────────────────────────────────────────────────────────

    def _init_bars(self, color: str):
        self.canvas.delete("all")
        self._bars = []
        for i in range(self.N_BARS):
            x = i * (self.BAR_W + self.BAR_GAP)
            self._bars.append(
                self.canvas.create_rectangle(
                    x, self.CVS_H // 2,
                    x + self.BAR_W, self.CVS_H,
                    fill=color, outline="",
                )
            )

    def _reset_bars(self, color: str):
        for i, bar in enumerate(self._bars):
            x = i * (self.BAR_W + self.BAR_GAP)
            self.canvas.coords(bar, x, self.CVS_H // 2, x + self.BAR_W, self.CVS_H)
            self.canvas.itemconfig(bar, fill=color)

    # ── animation ─────────────────────────────────────────────────────────────

    def _animate(self):
        if not self._animating:
            return
        self._tick += 1
        for i, bar in enumerate(self._bars):
            h = int(4 + 8 * abs(math.sin(self._tick * 0.3 + i * 0.9)))
            x = i * (self.BAR_W + self.BAR_GAP)
            self.canvas.coords(bar, x, self.CVS_H - h, x + self.BAR_W, self.CVS_H)
        self.root.after(80, self._animate)

    # ── queue poll ────────────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                msg = ui_queue.get_nowait()
                if msg == "recording":
                    self._reset_bars(self.RED)
                    self.lbl.config(text="Aufnahme", fg="white")
                    self._animating = True
                    self.root.deiconify()
                    self._animate()
                elif msg == "transcribing":
                    self._animating = False
                    self._reset_bars(self.YELLOW)
                    self.lbl.config(text="Transkribiere…", fg=self.YELLOW)
                elif msg == "hide":
                    self._animating = False
                    self.root.withdraw()
                elif msg == "quit":
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def run(self):
        self.root.mainloop()


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not MISTRAL_API_KEY:
        print("❌ MISTRAL_API_KEY not set in .env")
        sys.exit(1)

    print("🎤 Voice Dictate — Mistral Voxtral Mini")
    print("Shortcut: Ctrl + Shift (hold → release = transcribe)")
    print("ESC to quit\n")

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        callback=audio_callback,
    )
    stream.start()

    def _run_listener():
        with keyboard.Listener(on_press=on_press, on_release=on_release) as lst:
            lst.join()

    threading.Thread(target=_run_listener, daemon=True).start()

    widget = RecordingWidget()
    widget.run()

    stream.stop()
