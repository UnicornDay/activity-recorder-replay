"""
Activity Recorder & Replayer GUI
"""

import ctypes

# ── Windows DPI awareness ─────────────────────────────────────────────────────
# Must be set BEFORE any window/UI is created. Without this, on high-DPI
# displays Windows will virtualize coordinates and clicks/positions get
# reported in the wrong scale, so the recorded position won't match where
# the user actually clicked.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor v2 (Win10 1703+)
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Per-monitor (Win8.1+)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # System DPI (legacy)
        except Exception:
            pass

import json
import os
import re
import sys
import base64
import io
import time
import threading
import numpy as np
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
from datetime import datetime
from pathlib import Path

try:
    from pynput import mouse, keyboard
except ImportError:
    messagebox.showerror("Missing dependency", "Install: py -3 -m pip install pynput")
    sys.exit(1)

try:
    import win32gui, win32con, win32api, win32process
except ImportError:
    messagebox.showerror("Missing dependency", "Install: py -3 -m pip install pywin32")
    sys.exit(1)

try:
    from PIL import ImageGrab
except ImportError:
    messagebox.showerror("Missing dependency", "Install: py -3 -m pip install pillow")
    sys.exit(1)

# ─── Paths ────────────────────────────────────────────────────────────────────

LOG_DIR = Path.home() / "activity_logs"
LOG_DIR.mkdir(exist_ok=True)
PROCEDURES_FILE = LOG_DIR / "procedures.jsonl"
PROCEDURES_DIR = LOG_DIR / "procedures"
PROCEDURES_DIR.mkdir(exist_ok=True)

# ─── Hotkey callbacks (set when GUI starts) ──────────────────────────────────

_hotkey_record_cb = None
_hotkey_replay_cb = None
_hotkey_load_cb = None
_hotkey_procedures_cb = None


def set_hotkey_callbacks(record=None, replay=None, load=None, procedures=None):
    """Set callbacks (called on main thread) for the four hotkeys."""
    global _hotkey_record_cb, _hotkey_replay_cb, _hotkey_load_cb, _hotkey_procedures_cb
    _hotkey_record_cb = record
    _hotkey_replay_cb = replay
    _hotkey_load_cb = load
    _hotkey_procedures_cb = procedures


# ─── Dedicated hotkey listener (always on, separate from recorder) ────────────
# The recorder's keyboard listener only runs while recording, so hotkeys
# would be dead if the user hasn't clicked Record yet. This listener runs
# from GUI startup, so Ctrl+Shift+R / P / O always work.

_hk_mod_ctrl = False
_hk_mod_shift = False
_hk_mod_alt = False
_hotkey_listener = None


def start_hotkey_listener():
    """Start the single always-on keyboard listener (used for hotkey detection
    AND key logging during recording)."""
    global _hotkey_listener
    if _hotkey_listener is not None:
        return
    _hotkey_listener = keyboard.Listener(
        on_press=_on_press, on_release=_on_release, suppress=False
    )
    _hotkey_listener.daemon = True
    _hotkey_listener.start()


def stop_hotkey_listener():
    global _hotkey_listener
    if _hotkey_listener and _hotkey_listener.running:
        _hotkey_listener.stop()
    _hotkey_listener = None


# Modifier key sets (used by both hotkey listener and recorder)
_CTRL_KEYS = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}
_SHIFT_KEYS = {keyboard.Key.shift, keyboard.Key.shift_r}
_ALT_KEYS = {keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r}


# ─── Core Logic (non-GUI, same as before) ─────────────────────────────────────

_active_window = ""
_last_screenshot_time = 0
_stop_event = threading.Event()
_mouse_listener = None
_keyboard_listener = None
_session_id = None
_event_log_path = None
_screenshot_dir = None
_replay_abort = threading.Event()

# Callbacks to update GUI
_on_log = None
_on_status = None


def _get_window_title():
    try:
        hwnd = win32gui.GetForegroundWindow()
        return win32gui.GetWindowText(hwnd) or "(unnamed)"
    except Exception:
        return "(unknown)"


def _get_screen_info():
    """Return (primary_w, primary_h, virtual_w, virtual_h) in pixels."""
    try:
        primary = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
        vw = win32api.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        vh = win32api.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        return {"primary": primary, "virtual": (vw, vh)}
    except Exception:
        return {}


def _get_cursor_pos():
    """Cross-check cursor position via Win32."""
    try:
        return win32api.GetCursorPos()
    except Exception:
        return None


def _get_window_exe():
    try:
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        h = win32api.OpenProcess(0x0400 | 0x0010, False, pid)
        exe = win32process.GetModuleFileNameEx(h, 0)
        win32api.CloseHandle(h)
        return exe or "(unknown)"
    except Exception:
        return "(unknown)"


# ─── AI activity analysis (Ollama) ───────────────────────────────────────────

DEFAULT_AI_MODEL = "ministral-3:3b"


def _format_key(key_str):
    """Convert pynput key string to a human-readable form."""
    mapping = {
        "Key.enter": "[Enter]", "Key.tab": "[Tab]", "Key.space": " ",
        "Key.backspace": "[Backspace]", "Key.delete": "[Delete]",
        "Key.shift": "[Shift]", "Key.ctrl": "[Ctrl]", "Key.alt": "[Alt]",
        "Key.esc": "[Esc]", "Key.up": "[Up]", "Key.down": "[Down]",
        "Key.left": "[Left]", "Key.right": "[Right]",
        "Key.home": "[Home]", "Key.end": "[End]",
        "Key.page_up": "[PageUp]", "Key.page_down": "[PageDown]",
        "Key.caps_lock": "[CapsLock]",
    }
    if key_str in mapping:
        return mapping[key_str]
    if key_str.startswith("Key.f") and key_str[5:].isdigit():
        return f"[{key_str[4:].upper()}]"
    return key_str


def _summarize_log(log_path, max_events=400):
    """Convert a recorded log into a human-readable text summary."""
    events = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    if not events:
        return "No events recorded.", events

    lines = []
    typed_buffer = ""
    last_window = ""
    start_time = events[0]["timestamp"]
    end_time = events[-1]["timestamp"]

    def flush_typed():
        nonlocal typed_buffer
        if typed_buffer:
            lines.append(f"  Typed: {typed_buffer!r}")
            typed_buffer = ""

    for ev in events:
        typ = ev["type"]
        data = ev["data"]

        if typ == "focus_change":
            flush_typed()
            win = data.get("title", "?")
            exe = data.get("exe", "?").split("\\")[-1]
            if win != last_window:
                lines.append(f"Switched to: {win} ({exe})")
                last_window = win

        elif typ == "mouse_click":
            flush_typed()
            x, y = data.get("x"), data.get("y")
            btn = str(data.get("button", "")).split(".")[-1]
            lines.append(f"  Clicked {btn} at ({x}, {y})")

        elif typ == "mouse_scroll":
            dy = data.get("dy", 0)
            direction = "up" if dy > 0 else "down"
            lines.append(f"  Scrolled {direction}")

        elif typ == "key_press":
            ks = data.get("key", "")
            # Skip modifier-only presses
            if ks.startswith("Key.") and ks in (
                "Key.shift", "Key.shift_l", "Key.shift_r",
                "Key.ctrl", "Key.ctrl_l", "Key.ctrl_r",
                "Key.alt", "Key.alt_l", "Key.alt_r",
            ):
                continue
            text = _format_key(ks)
            if text.startswith("[") and text.endswith("]"):
                flush_typed()
                lines.append(f"  Pressed {text}")
            else:
                typed_buffer += text

        elif typ == "mouse_move":
            # Already at low rate, but skip in summary to keep it compact
            continue

    flush_typed()
    header = f"Recorded session from {start_time} to {end_time} ({len(events)} events)"
    return header + "\n" + "\n".join(lines), events


def analyze_with_ai(log_path, model=DEFAULT_AI_MODEL, on_chunk=None):
    """Stream a description of the recorded activity from Ollama.

    on_chunk(text) is called repeatedly with incremental text from the model.
    Returns the final full text.
    """
    import ollama

    summary, _ = _summarize_log(log_path)
    prompt = (
        "You are an assistant that reads a log of user activity on a computer "
        "and explains in plain English what the user was doing. Be concise "
        "(3-6 sentences). Mention the apps used and the main actions.\n\n"
        f"Activity log:\n{summary}\n\n"
        "What was the user doing?"
    )
    full = ""
    for chunk in ollama.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        stream=True,
    ):
        piece = chunk.get("message", {}).get("content", "")
        if piece:
            full += piece
            if on_chunk:
                on_chunk(piece)
    return full


def generate_procedure(log_path, model=DEFAULT_AI_MODEL, on_chunk=None):
    """Have the AI generate a title + description in one call.

    Returns (title, description). Streams the description via on_chunk
    (the title is usually produced first and is short, so it's not streamed).
    """
    import ollama
    import re

    summary, _ = _summarize_log(log_path)
    prompt = (
        "You are an assistant that reads a log of user activity on a computer. "
        "Return a JSON object with exactly two fields:\n"
        '  "title": a short, specific name (3-7 words, like "Book flight to '
        'Tokyo" or "Edit vacation photos"),\n'
        '  "description": a 3-6 sentence summary of what the user did, '
        'mentioning the apps used and main actions.\n\n'
        f"Activity log:\n{summary}\n"
    )
    full = ""
    for chunk in ollama.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        stream=True,
        format="json",
    ):
        piece = chunk.get("message", {}).get("content", "")
        if piece:
            full += piece
            if on_chunk:
                on_chunk(piece)

    # Parse JSON
    title, description = "", ""
    try:
        data = json.loads(full)
        title = (data.get("title") or "").strip().strip('"').strip()
        description = (data.get("description") or "").strip()
    except Exception:
        # Fallback: try to extract title from a "title:" line, else use first line
        m = re.search(r'"title"\s*:\s*"([^"]+)"', full)
        if m:
            title = m.group(1).strip()
        m = re.search(r'"description"\s*:\s*"([^"]+)"', full)
        if m:
            description = m.group(1).strip()
        if not description:
            description = full.strip()

    return title, description


def _safe_filename(name):
    """Sanitize a string for use as a filename."""
    bad = '<>:"/\\|?*\0'
    for ch in bad:
        name = name.replace(ch, "_")
    return name.strip().strip(".")[:120] or "procedure"


def save_procedure(log_path, description, model, title=""):
    """Append a procedure record to procedures.jsonl and write a named file."""
    record = {
        "timestamp": datetime.now().isoformat(),
        "title": title.strip(),
        "log_path": str(log_path),
        "log_name": Path(log_path).name,
        "description": description.strip(),
        "model": model,
    }
    with open(PROCEDURES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # Also write a human-readable file named after the title (or timestamp)
    if title.strip():
        fname = _safe_filename(title) + ".md"
    else:
        fname = datetime.now().strftime("%Y%m%d_%H%M%S") + ".md"
    md_path = PROCEDURES_DIR / fname
    body = (
        f"# {title or 'Procedure'}\n\n"
        f"- **Saved:** {record['timestamp']}\n"
        f"- **Log file:** `{record['log_name']}`\n"
        f"- **Full log path:** `{record['log_path']}`\n"
        f"- **Model:** `{record['model']}`\n\n"
        f"## Description\n\n{record['description']}\n"
    )
    # Avoid overwriting: if a file with the same name already exists, suffix -2, -3, ...
    if md_path.exists():
        stem = md_path.stem
        i = 2
        while True:
            candidate = PROCEDURES_DIR / f"{stem}-{i}.md"
            if not candidate.exists():
                md_path = candidate
                break
            i += 1
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(body)
    record["file_path"] = str(md_path)
    return record


def list_procedures():
    """Return all stored procedures, newest first."""
    if not PROCEDURES_FILE.exists():
        return []
    out = []
    with open(PROCEDURES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return list(reversed(out))


def _log_path_is_referenced(log_path, exclude_proc):
    """Check if any procedure (other than exclude_proc) still references log_path."""
    if not PROCEDURES_FILE.exists():
        return False
    with open(PROCEDURES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if (entry.get("log_path") == log_path and
                    not (entry.get("timestamp") == exclude_proc.get("timestamp") and
                         entry.get("log_path") == exclude_proc.get("log_path"))):
                return True
    return False


def delete_procedure(proc):
    """Delete a procedure and (if no other procedure references it) its log
    file and screenshot folder. Returns dict of what was removed."""
    result = {"md": False, "jsonl": False, "log": False, "screenshots": False}

    # 1) Delete the .md file
    md_path = None
    if proc.get("title"):
        md_path = PROCEDURES_DIR / f"{_safe_filename(proc['title'])}.md"
    if md_path and md_path.exists():
        try:
            md_path.unlink()
            result["md"] = True
        except Exception:
            pass
    else:
        # Try to find the file by timestamp
        if proc.get("timestamp"):
            ts_name = proc["timestamp"].replace(":", "").replace("-", "").split(".")[0]
            for candidate in PROCEDURES_DIR.glob(f"{ts_name}*.md"):
                try:
                    candidate.unlink()
                    result["md"] = True
                    break
                except Exception:
                    pass

    # 2) Remove the entry from procedures.jsonl (do this BEFORE the log check
    #    so the proc itself doesn't count as a reference)
    if PROCEDURES_FILE.exists():
        remaining = []
        with open(PROCEDURES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    remaining.append(line)
                    continue
                if (entry.get("timestamp") == proc.get("timestamp") and
                        entry.get("log_path") == proc.get("log_path")):
                    result["jsonl"] = True
                    continue
                remaining.append(json.dumps(entry, ensure_ascii=False))
        with open(PROCEDURES_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(remaining) + ("\n" if remaining else ""))

    # 3) If the log file is no longer referenced by any other procedure,
    #    delete the log file and its sibling screenshots folder.
    log_path = proc.get("log_path")
    if log_path and not _log_path_is_referenced(log_path, proc):
        lp = Path(log_path)
        if lp.exists():
            try:
                lp.unlink()
                result["log"] = True
            except Exception:
                pass
        # Sibling screenshots folder: same parent dir, name based on log name
        # events_20260605_112233.jsonl -> screenshots_20260605_112233/
        if lp.stem.startswith("events_"):
            ss_name = "screenshots_" + lp.stem[len("events_"):]
            ss_dir = lp.parent / ss_name
            if ss_dir.exists() and ss_dir.is_dir():
                try:
                    import shutil
                    shutil.rmtree(ss_dir)
                    result["screenshots"] = True
                except Exception:
                    pass

    return result


def parse_procedure_md(md_path):
    """Parse a saved procedure .md file. Returns a dict with the metadata
    fields and a 'description' key for the body. Returns None on failure."""
    try:
        text = Path(md_path).read_text(encoding="utf-8")
    except Exception:
        return None

    rec = {"file_path": str(md_path), "title": "", "log_path": "", "log_name": "",
           "timestamp": "", "model": "", "description": ""}

    # Title from first H1
    for line in text.splitlines():
        if line.startswith("# "):
            rec["title"] = line[2:].strip()
            break

    # Metadata list items: - **Key:** value
    in_desc = False
    desc_lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## Description"):
            in_desc = True
            continue
        if in_desc:
            if s.startswith("## "):
                break
            desc_lines.append(line)
            continue
        m = re.match(r"-\s+\*\*([^*]+):\*\*\s+(.+)", s)
        if m:
            key = m.group(1).strip().lower()
            val = m.group(2).strip().strip("`")
            if key == "saved":
                rec["timestamp"] = val
            elif key == "log file":
                rec["log_name"] = val
            elif key == "full log path":
                rec["log_path"] = val
            elif key == "model":
                rec["model"] = val

    rec["description"] = "\n".join(desc_lines).strip()
    return rec if rec["log_path"] else None


def _log_event(typ, data):
    record = {"timestamp": datetime.now().isoformat(), "type": typ, "data": data}
    with open(_event_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    if _on_log:
        _on_log(f"[{typ}] {json.dumps(data, ensure_ascii=False)}")


def _take_screenshot():
    global _last_screenshot_time
    now = time.time()
    if now - _last_screenshot_time < 1:
        return
    _last_screenshot_time = now
    try:
        img = ImageGrab.grab()
        name = f"screenshot_{datetime.now().strftime('%H%M%S_%f')}.png"
        path = _screenshot_dir / name
        img.save(path)
        _log_event("screenshot", {"path": str(path)})
    except Exception as e:
        _log_event("screenshot_error", {"error": str(e)})


def _focus_loop(interval=0.5):
    global _active_window
    while not _stop_event.is_set():
        cur = _get_window_title()
        exe = _get_window_exe()
        if cur != _active_window:
            _active_window = cur
            _log_event("focus_change", {"title": cur, "exe": exe})
        _stop_event.wait(interval)


def _screenshot_loop(interval=30):
    while not _stop_event.is_set():
        _stop_event.wait(interval)
        if not _stop_event.is_set():
            _take_screenshot()


# Track which mouse button is currently held (so we can record drags)
_mouse_held_button = None
_last_move_log = 0

# Whether to capture a small image around each click for visual matching
_capture_click_images = True
_click_image_half = 50  # captures a 100x100 region around the click


def _capture_image_around(cx, cy, half=None):
    """Capture a (2*half x 2*half) region centered on (cx, cy) and return
    it as a base64-encoded PNG. Returns None on failure."""
    import cv2
    if half is None:
        half = _click_image_half
    try:
        screen = ImageGrab.grab()
        screen_w, screen_h = screen.size
        x1 = max(0, cx - half)
        y1 = max(0, cy - half)
        x2 = min(screen_w, cx + half)
        y2 = min(screen_h, cy + half)
        crop = screen.crop((x1, y1, x2, y2))
        # Encode as PNG
        buf = io.BytesIO()
        crop.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return b64
    except Exception:
        return None


def _decode_image(b64):
    """Decode a base64 PNG to an OpenCV BGR numpy array."""
    import cv2
    import numpy as np
    raw = base64.b64decode(b64)
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


def _find_image_on_screen(template_b64, threshold=0.8):
    """Find the template image on screen. Returns (x, y, similarity) or None."""
    import cv2
    try:
        template = _decode_image(template_b64)
        if template is None:
            return None
        th, tw = template.shape[:2]
        screen = ImageGrab.grab()
        screen_bgr = cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)
        result = cv2.matchTemplate(screen_bgr, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < threshold:
            return None
        # Center of the matched region
        cx = max_loc[0] + tw // 2
        cy = max_loc[1] + th // 2
        return (cx, cy, float(max_val))
    except Exception:
        return None


def _on_click(x, y, button, pressed):
    """Log both press and release. A drag is press -> moves -> release."""
    global _mouse_held_button
    cursor = _get_cursor_pos()
    data = {
        "x": x, "y": y, "button": str(button),
        "action": "press" if pressed else "release",
        "window": _get_window_title(),
    }
    if cursor:
        data["win32_pos"] = list(cursor)
        data["match"] = (abs(cursor[0] - x) <= 1 and abs(cursor[1] - y) <= 1)
    # Capture a small image around the click for visual-matching replay
    if _capture_click_images:
        img = _capture_image_around(x, y)
        if img:
            data["image"] = img
    _log_event("mouse_click", data)
    _mouse_held_button = str(button) if pressed else None


def _on_scroll(x, y, dx, dy):
    _log_event("mouse_scroll", {
        "x": x, "y": y, "dx": dx, "dy": dy, "window": _get_window_title(),
    })


def _on_move(x, y):
    """Log mouse moves. While a button is held, log as drag. While idle, throttle."""
    global _last_move_log
    if _mouse_held_button is not None:
        # During drag: log every move so replay is smooth
        _log_event("mouse_drag", {
            "x": x, "y": y, "button": _mouse_held_button,
            "window": _get_window_title(),
        })
        return
    # Idle: throttle to avoid log spam
    now = time.time()
    if now - _last_move_log < 0.2:
        return
    _last_move_log = now
    _log_event("mouse_move", {"x": x, "y": y, "window": _get_window_title()})


def _on_press(key):
    """Single keyboard listener: handles both hotkey detection (always) and
    key logging (only when recording is on)."""
    global _hk_mod_ctrl, _hk_mod_shift, _hk_mod_alt

    # Modifier tracking (always on)
    if key in _CTRL_KEYS:
        _hk_mod_ctrl = True
    elif key in _SHIFT_KEYS:
        _hk_mod_shift = True
    elif key in _ALT_KEYS:
        _hk_mod_alt = True

    # Hotkey detection (always on)
    if _hk_mod_ctrl and _hk_mod_shift and not _hk_mod_alt:
        vk = getattr(key, "vk", None)
        if vk == 0x52 and _hotkey_record_cb:  # R
            _hotkey_record_cb()
            return
        elif vk == 0x50 and _hotkey_replay_cb:  # P
            _hotkey_replay_cb()
            return
        elif vk == 0x4F and _hotkey_load_cb:  # O
            _hotkey_load_cb()
            return
        elif vk == 0x4C and _hotkey_procedures_cb:  # L
            _hotkey_procedures_cb()
            return

    # Key logging (only when recording is on)
    if is_recording():
        try:
            ks = key.char if key.char else str(key)
        except AttributeError:
            ks = str(key)
        _log_event("key_press", {"key": ks, "window": _get_window_title()})


def _on_release(key):
    global _hk_mod_ctrl, _hk_mod_shift, _hk_mod_alt
    if key in _CTRL_KEYS:
        _hk_mod_ctrl = False
    elif key in _SHIFT_KEYS:
        _hk_mod_shift = False
    elif key in _ALT_KEYS:
        _hk_mod_alt = False

    if is_recording():
        try:
            ks = key.char if key.char else str(key)
        except AttributeError:
            ks = str(key)
        _log_event("key_release", {"key": ks, "window": _get_window_title()})

        if key == keyboard.Key.esc:
            _log_event("system", {"message": "ESC pressed, stopping"})
            if _hotkey_record_cb:
                _hotkey_record_cb()


def start_recording(screenshot_interval=30, record_mouse_move=False, on_log=None, on_status=None):
    global _session_id, _event_log_path, _screenshot_dir, _on_log, _on_status, _stop_event
    global _mouse_listener, _keyboard_listener

    _stop_event.clear()
    _replay_abort.clear()
    _session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    _event_log_path = LOG_DIR / f"events_{_session_id}.jsonl"
    _screenshot_dir = LOG_DIR / f"screenshots_{_session_id}"
    _screenshot_dir.mkdir(exist_ok=True)
    _on_log = on_log
    _on_status = on_status

    _log_event("session_start", {
        "session_id": _session_id, "screenshot_interval": screenshot_interval,
        "record_mouse_move": record_mouse_move, "screen": _get_screen_info(),
    })

    if on_status:
        on_status("Recording... (ESC to stop, Ctrl+Shift+R toggles)")

    t = threading.Thread(target=_focus_loop, daemon=True)
    t.start()

    t2 = threading.Thread(target=_screenshot_loop, args=(screenshot_interval,), daemon=True)
    t2.start()

    # Always enable on_move — _on_move filters by mouse_held_button state
    # (drags are logged at full rate, idle moves are throttled).
    _mouse_listener = mouse.Listener(on_click=_on_click, on_scroll=_on_scroll, on_move=_on_move)
    _mouse_listener.start()

    # Note: keyboard listener is already running (started by the GUI), it
    # only logs keys when is_recording() returns True. No need to start it here.

    return _event_log_path


def stop_recording():
    global _session_id
    if not is_recording():
        return
    _stop_event.set()
    # Only stop the mouse listener; the keyboard listener stays running
    # so it can continue detecting hotkeys.
    if _mouse_listener and _mouse_listener.running:
        _mouse_listener.stop()
    if _session_id:
        sid = _session_id
        _session_id = None
        _log_event("session_end", {"session_id": sid})


def is_recording():
    return _mouse_listener is not None and _mouse_listener.running


def _find_window(title):
    def cb(hwnd, results):
        if title in win32gui.GetWindowText(hwnd):
            results.append(hwnd)
    results = []
    win32gui.EnumWindows(cb, results)
    return results[0] if results else None


def _activate_window(title):
    hwnd = _find_window(title)
    if hwnd:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.3)
        except Exception:
            pass


def _parse_key(key_str):
    special = {
        "Key.esc": keyboard.Key.esc, "Key.enter": keyboard.Key.enter,
        "Key.tab": keyboard.Key.tab, "Key.space": keyboard.Key.space,
        "Key.backspace": keyboard.Key.backspace, "Key.delete": keyboard.Key.delete,
        "Key.shift": keyboard.Key.shift, "Key.shift_r": keyboard.Key.shift_r,
        "Key.ctrl": keyboard.Key.ctrl, "Key.ctrl_l": keyboard.Key.ctrl_l,
        "Key.ctrl_r": keyboard.Key.ctrl_r, "Key.alt": keyboard.Key.alt,
        "Key.alt_gr": keyboard.Key.alt_gr, "Key.cmd": keyboard.Key.cmd,
        "Key.caps_lock": keyboard.Key.caps_lock,
        "Key.up": keyboard.Key.up, "Key.down": keyboard.Key.down,
        "Key.left": keyboard.Key.left, "Key.right": keyboard.Key.right,
        "Key.home": keyboard.Key.home, "Key.end": keyboard.Key.end,
        "Key.page_up": keyboard.Key.page_up, "Key.page_down": keyboard.Key.page_down,
        "Key.insert": keyboard.Key.insert,
        "Key.f1": keyboard.Key.f1, "Key.f2": keyboard.Key.f2,
        "Key.f3": keyboard.Key.f3, "Key.f4": keyboard.Key.f4,
        "Key.f5": keyboard.Key.f5, "Key.f6": keyboard.Key.f6,
        "Key.f7": keyboard.Key.f7, "Key.f8": keyboard.Key.f8,
        "Key.f9": keyboard.Key.f9, "Key.f10": keyboard.Key.f10,
        "Key.f11": keyboard.Key.f11, "Key.f12": keyboard.Key.f12,
        "Key.print_screen": keyboard.Key.print_screen,
        "Key.scroll_lock": keyboard.Key.scroll_lock,
        "Key.pause": keyboard.Key.pause, "Key.menu": keyboard.Key.menu,
    }
    if key_str in special:
        return special[key_str]
    if len(key_str) == 1:
        return keyboard.KeyCode.from_char(key_str)
    return None


def _wait_for_image(template_b64, timeout=10.0, threshold=0.8, poll=0.3):
    """Poll the screen for a template image until found or timeout. Returns
    (x, y, similarity) on match, None on timeout/abort."""
    deadline = time.time() + timeout
    while not _replay_abort.is_set():
        result = _find_image_on_screen(template_b64, threshold=threshold)
        if result:
            return result
        if time.time() >= deadline:
            return None
        time.sleep(poll)
    return None


# Visual matching settings (set by GUI before calling replay_log)
_replay_visual_match = True
_replay_match_timeout = 2.0
_replay_match_threshold = 0.8
_replay_fail_on_no_match = False


def replay_log(log_path, speed=1.0, on_log=None, on_status=None, on_done=None,
                on_replay_error=None):
    _replay_abort.clear()
    path = Path(log_path)
    if not path.exists():
        if on_log:
            on_log(f"File not found: {log_path}")
        return

    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    if not events:
        if on_log:
            on_log("No events to replay.")
        return

    mc = mouse.Controller()
    kc = keyboard.Controller()
    _replay_btn_held = {}  # track which buttons are still held

    if on_status:
        on_status(f"Replaying {len(events)} events ({speed}x)...")
    if on_log:
        on_log(f"Starting replay ({speed}x speed)")

    time.sleep(1)
    prev_ts = None
    count = 0
    last_win = ""

    for ev in events:
        if _replay_abort.is_set():
            if on_log:
                on_log("Replay aborted.")
            break

        typ = ev["type"]
        data = ev["data"]

        # Timing
        ts = ev["timestamp"]
        if prev_ts and speed > 0:
            try:
                d = (datetime.fromisoformat(ts) - datetime.fromisoformat(prev_ts)).total_seconds() / speed
                if d > 0:
                    time.sleep(min(d, 2.0))
            except Exception:
                pass
        prev_ts = ts

        if typ == "focus_change":
            last_win = data.get("title", "")
            _activate_window(last_win)

        elif typ == "mouse_click":
            btn_str = data.get("button", "Button.left")
            btn = {"Button.left": mouse.Button.left, "Button.right": mouse.Button.right,
                   "Button.middle": mouse.Button.middle}.get(btn_str, mouse.Button.left)
            if data.get("window") and data["window"] != last_win:
                _activate_window(data["window"])
                last_win = data["window"]
            # Determine click position: visual match takes priority over coords
            click_x, click_y = data.get("x"), data.get("y")
            if data.get("image") and _replay_visual_match:
                # Single quick visual-match try (fast path). If not found in
                # one pass, fall back to the recorded coordinates. The
                # timeout/polling variant is used only if a "wait for image"
                # behaviour is explicitly requested by setting timeout > 1.0.
                quick = _find_image_on_screen(data["image"], threshold=_replay_match_threshold)
                if quick:
                    click_x, click_y, sim = quick
                    if on_log:
                        on_log(f"  [visual match @ {click_x},{click_y} sim={sim:.2f}]")
                elif _replay_match_timeout > 1.0:
                    target = _wait_for_image(data["image"], timeout=_replay_match_timeout,
                                             threshold=_replay_match_threshold)
                    if target:
                        click_x, click_y, sim = target
                        if on_log:
                            on_log(f"  [visual match @ {click_x},{click_y} sim={sim:.2f}]")
                    else:
                        if on_log:
                            on_log(f"  [visual match failed, using coords {click_x},{click_y}]")
                        if _replay_fail_on_no_match and on_replay_error:
                            _replay_abort.set()
                            on_replay_error(
                                f"Visual match timed out after "
                                f"{_replay_match_timeout:.0f}s — the target element "
                                f"was not found on screen."
                            )
                            return
                else:
                    if on_log:
                        on_log(f"  [no visual match, using coords {click_x},{click_y}]")
                    if _replay_fail_on_no_match and on_replay_error:
                        _replay_abort.set()
                        on_replay_error(
                            f"Visual match failed — the target element was not "
                            f"found on screen. The application or web page may have "
                            f"changed since this procedure was recorded."
                        )
                        return
            if click_x is None or click_y is None:
                continue
            mc.position = (click_x, click_y)
            time.sleep(0.02)
            if data.get("action") == "press":
                mc.press(btn)  # hold the button (for drag); release comes later
                _replay_btn_held[btn_str] = True
            else:  # release
                mc.release(btn)
                _replay_btn_held[btn_str] = False
            count += 1

        elif typ == "mouse_drag":
            # Mouse moved while a button was held — just move the cursor.
            mc.position = (data.get("x", 0), data.get("y", 0))
            count += 1

        elif typ == "mouse_move":
            # Legacy idle move events (old logs) — move the cursor
            mc.position = (data.get("x", 0), data.get("y", 0))

        elif typ == "mouse_scroll":
            mc.scroll(data.get("dx", 0), data.get("dy", 0))
            count += 1

        elif typ == "key_press":
            k = _parse_key(data["key"])
            if k:
                kc.press(k)
                count += 1

        elif typ == "key_release":
            k = _parse_key(data["key"])
            if k:
                kc.release(k)

    # Safety net: release any buttons that might still be held
    # (e.g. from old logs that only logged press without release)
    btn_map = {"Button.left": mouse.Button.left, "Button.right": mouse.Button.right,
               "Button.middle": mouse.Button.middle}
    for btn_str, held in _replay_btn_held.items():
        if held and btn_str in btn_map:
            try:
                mc.release(btn_map[btn_str])
            except Exception:
                pass

    if on_status:
        on_status(f"Replay finished ({count} actions)")
    if on_log:
        on_log(f"Replay complete. {count} actions replayed.")
    if on_done:
        on_done()


def stop_replay():
    _replay_abort.set()


# ─── GUI ──────────────────────────────────────────────────────────────────────


class ActivityRecorderApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Activity Recorder")
        self.root.geometry("800x600")
        self.root.minsize(600, 400)

        self.last_log_path = None
        self.replay_thread = None

        self._build_ui()
        self._bind_shortcuts()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Register hotkeys — fired from the dedicated hotkey listener thread
        set_hotkey_callbacks(
            record=lambda: self.root.after(0, self.toggle_record),
            replay=lambda: self.root.after(0, self.toggle_replay),
            load=lambda: self.root.after(0, self.load_log),
            procedures=lambda: self.root.after(0, self.show_procedures),
        )
        # Start the always-on hotkey listener so Ctrl+Shift+R works even
        # before the user has clicked Record.
        start_hotkey_listener()

        # Check if Ollama is available (for AI procedure summaries).
        # The app works without it — recording/replay don't need it —
        # but warn the user once at startup if it's missing.
        self.root.after(500, self._check_ollama)

    def _on_close(self):
        if is_recording():
            stop_recording()
        stop_hotkey_listener()
        self.root.destroy()

    def _check_ollama(self):
        """Check if Ollama is reachable and the default model is pulled.
        If not, show a one-time info dialog. The app is fully usable
        without Ollama — only AI procedure summaries need it."""
        def probe():
            try:
                import ollama
                resp = ollama.list()
                # ollama-python returns a ListResponse object with .models
                models_list = getattr(resp, "models", None) or resp.get("models", [])
                names = []
                for m in models_list:
                    # m may be a pydantic Model or a dict
                    name = getattr(m, "model", None) or m.get("model") or m.get("name", "")
                    names.append(name.split(":")[0])
                model = DEFAULT_AI_MODEL.split(":")[0]
                if model in names:
                    return ("ok", None)
                else:
                    return ("model_missing", model)
            except ImportError:
                return ("not_installed", None)
            except Exception as e:
                msg = str(e)
                if "Connection" in msg or "refused" in msg or "11434" in msg:
                    return ("not_running", None)
                return ("error", msg)

        def show():
            status, detail = probe()
            if status == "ok":
                return  # everything's fine
            if status == "not_installed":
                messagebox.showinfo(
                    "Ollama not installed (optional)",
                    "AI procedure summaries are an optional feature.\n\n"
                    "The app works fully without Ollama — record, replay, "
                    "hotkeys, and the procedures library all work as-is.\n\n"
                    "To enable AI-generated procedure titles & descriptions:\n"
                    "  1. Install Ollama: https://ollama.com/download\n"
                    "  2. Run: ollama pull ministral-3:3b\n"
                    "  3. Restart this app",
                    parent=self.root,
                )
            elif status == "not_running":
                messagebox.showinfo(
                    "Ollama not running (optional)",
                    "AI procedure summaries are an optional feature.\n\n"
                    "The app works fully without Ollama running — record, "
                    "replay, hotkeys, and the procedures library all work.\n\n"
                    "To enable AI summaries, start Ollama:\n"
                    "  • Open the Ollama app, or\n"
                    "  • Run in a terminal: ollama serve",
                    parent=self.root,
                )
            elif status == "model_missing":
                messagebox.showinfo(
                    f"Ollama model '{detail}' not installed (optional)",
                    "AI procedure summaries are an optional feature.\n\n"
                    f"To pull the default model, run:\n"
                    f"  ollama pull {DEFAULT_AI_MODEL}\n\n"
                    "The app works fully without it.",
                    parent=self.root,
                )
            # status == "error" — silent, don't bother the user

        threading.Thread(target=lambda: self.root.after(0, show), daemon=True).start()

    def _build_ui(self):
        # ── Top bar: two rows so action buttons + model are always visible ──
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=tk.X)

        # Row 1: main actions (always visible, no need to maximize)
        row1 = ttk.Frame(top)
        row1.pack(fill=tk.X, pady=(0, 6))

        self.record_btn = ttk.Button(row1, text="Record", command=self.toggle_record)
        self.record_btn.pack(side=tk.LEFT, padx=(0, 4))

        self.replay_btn = ttk.Button(row1, text="Replay Last", command=self.toggle_replay, state=tk.DISABLED)
        self.replay_btn.pack(side=tk.LEFT, padx=4)

        self.load_btn = ttk.Button(row1, text="Load Log...", command=self.load_log)
        self.load_btn.pack(side=tk.LEFT, padx=4)

        self.analyze_btn = ttk.Button(row1, text="Analyze with AI", command=self.analyze_last, state=tk.DISABLED)
        self.analyze_btn.pack(side=tk.LEFT, padx=4)

        self.procedures_btn = ttk.Button(row1, text="Show All Procedures", command=self.show_procedures)
        self.procedures_btn.pack(side=tk.LEFT, padx=4)

        ttk.Separator(row1, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Label(row1, text="Model:").pack(side=tk.LEFT, padx=(0, 2))
        self.model_var = tk.StringVar(value=DEFAULT_AI_MODEL)
        ttk.Entry(row1, textvariable=self.model_var, width=18).pack(side=tk.LEFT, padx=2)

        # Row 2: settings (less critical, can wrap if window is narrow)
        row2 = ttk.Frame(top)
        row2.pack(fill=tk.X)

        ttk.Label(row2, text="Screenshot interval (s):").pack(side=tk.LEFT)
        self.screenshot_var = tk.StringVar(value="30")
        ttk.Entry(row2, textvariable=self.screenshot_var, width=6).pack(side=tk.LEFT, padx=4)

        ttk.Label(row2, text="  Replay speed:").pack(side=tk.LEFT)
        self.speed_var = tk.StringVar(value="1.0")
        ttk.Entry(row2, textvariable=self.speed_var, width=6).pack(side=tk.LEFT, padx=4)

        self.record_moves_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="Record mouse moves", variable=self.record_moves_var).pack(side=tk.LEFT, padx=6)

        ttk.Separator(row2, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=4)

        self.fast_replay_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="Fast (coords only)", variable=self.fast_replay_var).pack(side=tk.LEFT, padx=2)
        self.visual_match_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="Smart click (visual)", variable=self.visual_match_var).pack(side=tk.LEFT, padx=2)
        ttk.Label(row2, text="  Wait:").pack(side=tk.LEFT)
        self.match_timeout_var = tk.StringVar(value="2")
        ttk.Entry(row2, textvariable=self.match_timeout_var, width=4).pack(side=tk.LEFT, padx=2)
        ttk.Label(row2, text="s").pack(side=tk.LEFT)
        self.fail_on_no_match_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="Fail if no match", variable=self.fail_on_no_match_var).pack(side=tk.LEFT, padx=2)

        # Shortcut hints in row 2 (right side)
        ttk.Separator(row2, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        hints = ttk.Label(row2, text="Ctrl+Shift+R/P/O/L",
                          foreground="gray")
        hints.pack(side=tk.LEFT, padx=6)

        # ── Status bar ──
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=4)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        # ── Main area: event log (top) + procedure description (bottom) ──
        main = ttk.Frame(self.root, padding=4)
        main.pack(fill=tk.BOTH, expand=True)

        # Event log (top, takes 2/3 of space)
        log_frame = ttk.Frame(main)
        log_frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(log_frame, text="Event Log:").pack(anchor=tk.W)
        self.log_area = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.log_area.pack(fill=tk.BOTH, expand=True)

        # Procedure / AI description (bottom)
        proc_frame = ttk.LabelFrame(main, text="Last procedure (AI summary)", padding=4)
        proc_frame.pack(fill=tk.BOTH, expand=False, pady=(6, 0))
        self.proc_area = scrolledtext.ScrolledText(proc_frame, wrap=tk.WORD, font=("Segoe UI", 10), height=8)
        self.proc_area.pack(fill=tk.BOTH, expand=True)
        self.proc_area.tag_configure("title", font=("Segoe UI", 12, "bold"))
        self.proc_area.tag_configure("saved", foreground="gray")

        proc_btns = ttk.Frame(proc_frame)
        proc_btns.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(proc_btns, text="Clear", command=lambda: self.proc_area.delete(1.0, tk.END)).pack(side=tk.LEFT)

    def _bind_shortcuts(self):
        self.root.bind("<Control-o>", lambda e: self.load_log())

    # ── Logging ──

    def log(self, msg):
        self.log_area.insert(tk.END, msg + "\n")
        self.log_area.see(tk.END)

    def set_status(self, msg):
        self.status_var.set(msg)

    def _on_replay_error_popup(self, detail):
        # Called from the replay thread (replay_log sets the abort flag
        # before calling this, so the loop exits). Schedule the popup on
        # the main thread.
        def show():
            # Restore window before showing the modal
            try:
                self.root.deiconify()
            except Exception:
                pass
            self.set_status("Replay stopped — target not found")
            messagebox.showerror(
                "Procedure cannot run",
                "The procedure cannot run because the application or web page "
                "appears to have changed since it was recorded.\n\n"
                f"{detail}\n\n"
                "The replay has been stopped.\n\n"
                "Tips:\n"
                "  • Open the target app/page so the recorded UI is visible\n"
                "  • Re-record the procedure against the current UI\n"
                "  • Or disable 'Fail if no match' to fall back to recorded coordinates",
                parent=self.root,
            )
        self.root.after(0, show)

    # ── Recording ──

    def toggle_record(self):
        if is_recording():
            stop_recording()
            self.record_btn.config(text="Record")
            self.replay_btn.config(text="Replay Last", state=tk.NORMAL if self.last_log_path else tk.DISABLED)
            self.load_btn.config(state=tk.NORMAL)
            self.analyze_btn.config(state=tk.NORMAL if self.last_log_path else tk.DISABLED)
            # Restore the window and bring to front so the user sees the AI summary
            self.root.after(0, lambda: self.root.deiconify())
            self.root.after(50, lambda: self.root.lift())
            self.root.after(100, lambda: self.root.focus_force())
            self.set_status("Recording stopped - generating procedure...")
            # Auto-analyze and save as a procedure
            self.proc_area.delete(1.0, tk.END)
            self.proc_area.insert(tk.END, "Asking AI to summarize what you just did...\n")
            self._auto_analyze_and_save()
        else:
            try:
                interval = int(self.screenshot_var.get())
            except ValueError:
                interval = 30
            self.record_btn.config(text="Stop (ESC or Ctrl+Shift+R)", state=tk.NORMAL)
            self.replay_btn.config(state=tk.DISABLED)
            self.load_btn.config(state=tk.DISABLED)
            self.analyze_btn.config(state=tk.DISABLED)
            self.log_area.delete(1.0, tk.END)
            self.last_log_path = start_recording(
                screenshot_interval=interval,
                record_mouse_move=self.record_moves_var.get(),
                on_log=self.log,
                on_status=self.set_status,
            )
            # Minimize the window so it doesn't get in the way of the recording
            # (or accidentally appear in screenshots)
            self.root.iconify()
            self.set_status("Recording... (Ctrl+Shift+R or ESC to stop)")

    # ── Replay ──

    def toggle_replay(self):
        if self.replay_thread and self.replay_thread.is_alive():
            stop_replay()
            return

        path = self.last_log_path
        if not path:
            path = filedialog.askopenfilename(
                title="Select log file", initialdir=LOG_DIR,
                filetypes=[("JSONL files", "*.jsonl"), ("All files", "*.*")]
            )
            if not path:
                return

        try:
            speed = float(self.speed_var.get())
        except ValueError:
            speed = 1.0

        self.replay_btn.config(text="Stop Replay")
        self.record_btn.config(state=tk.DISABLED)
        self.load_btn.config(state=tk.DISABLED)

        # Hide the main window during replay so it doesn't get matched/clicked
        # by visual search. We'll restore it in `done()`.
        self._replay_previous_geometry = self.root.geometry()
        self._replay_was_iconified = self.root.state() == "iconic"
        self.root.withdraw()
        # Close any stray toplevels (like the procedures window) so they
        # don't appear in screenshots during replay
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel) and child.winfo_exists():
                try:
                    child.withdraw()
                except Exception:
                    pass

        def done():
            # Restore the main window
            def restore():
                self.root.deiconify()
                if self._replay_previous_geometry:
                    self.root.geometry(self._replay_previous_geometry)
                self.root.lift()
                self.replay_btn.config(text="Replay Last")
                self.record_btn.config(state=tk.NORMAL)
                self.load_btn.config(state=tk.NORMAL)
                self.set_status("Replay done")
            self.root.after(0, restore)
            self.replay_thread = None

        # Apply visual-match settings
        global _replay_visual_match, _replay_match_timeout, _replay_fail_on_no_match
        if self.fast_replay_var.get():
            _replay_visual_match = False
            _replay_match_timeout = 0.0
            _replay_fail_on_no_match = False
        else:
            _replay_visual_match = self.visual_match_var.get()
            try:
                _replay_match_timeout = float(self.match_timeout_var.get())
            except ValueError:
                _replay_match_timeout = 2.0
            _replay_fail_on_no_match = self.fail_on_no_match_var.get()

        self.replay_thread = threading.Thread(
            target=replay_log,
            args=(path, speed, self.log, self.set_status, done,
                  self._on_replay_error_popup),
            daemon=True,
        )
        self.replay_thread.start()

    def load_log(self):
        path = filedialog.askopenfilename(
            title="Select log file", initialdir=LOG_DIR,
            filetypes=[("JSONL files", "*.jsonl"), ("All files", "*.*")]
        )
        if path:
            self.last_log_path = path
            self.log_area.delete(1.0, tk.END)
            self.log(f"Loaded: {path}")
            self.replay_btn.config(text="Replay Loaded", state=tk.NORMAL)
            self.analyze_btn.config(state=tk.NORMAL)

    # ── AI Analysis ──

    def analyze_last(self):
        if not self.last_log_path:
            messagebox.showinfo("No log", "Record a session or load a log file first.")
            return

        win = tk.Toplevel(self.root)
        win.title(f"AI Analysis - {Path(self.last_log_path).name}")
        win.geometry("700x500")

        ttk.Label(win, text=f"Analyzing: {Path(self.last_log_path).name}",
                  font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, padx=8, pady=(8, 2))
        ttk.Label(win, text=f"Model: {self.model_var.get()}",
                  foreground="gray").pack(anchor=tk.W, padx=8)

        out = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=("Segoe UI", 10))
        out.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        out.insert(tk.END, "Asking the AI model...\n\n")

        model_name = self.model_var.get().strip() or DEFAULT_AI_MODEL

        def stream_chunk(piece):
            out.insert(tk.END, piece)
            out.see(tk.END)

        def run():
            try:
                analyze_with_ai(self.last_log_path, model=model_name, on_chunk=stream_chunk)
                out.insert(tk.END, "\n\n[done]")
            except ImportError:
                out.delete(1.0, tk.END)
                out.insert(tk.END,
                    "AI summaries require Ollama.\n\n"
                    "Install: https://ollama.com/download\n"
                    "Then run: ollama pull ministral-3:3b")
            except Exception as e:
                msg = str(e)
                if "Connection" in msg or "refused" in msg or "11434" in msg:
                    out.delete(1.0, tk.END)
                    out.insert(tk.END,
                        "Ollama is not running.\n\n"
                        "Start it with: ollama serve\n"
                        "Or open the Ollama app from the Start menu.")
                elif "model" in msg.lower() and "not found" in msg.lower():
                    out.delete(1.0, tk.END)
                    out.insert(tk.END,
                        f"Model '{model_name}' is not installed.\n\n"
                        f"Run: ollama pull {model_name}")
                else:
                    out.insert(tk.END, f"\n\n[error: {e}]")

        threading.Thread(target=run, daemon=True).start()

    # ── Auto-analyze on stop and save as a procedure ──

    def _auto_analyze_and_save(self):
        if not self.last_log_path:
            return
        model_name = self.model_var.get().strip() or DEFAULT_AI_MODEL

        # Show a header line so the user can see the title when it lands
        self.proc_area.delete(1.0, tk.END)
        self.proc_area.insert(tk.END, "Generating procedure...\n\n")

        def stream_chunk(piece):
            # Skip the raw JSON tokens; just show the description once extracted.
            # The full text is also shown at the end for transparency.
            pass

        def run():
            try:
                title, desc = generate_procedure(self.last_log_path, model=model_name)
                # Update the bottom panel with a clean view
                self.proc_area.delete(1.0, tk.END)
                if title:
                    self.proc_area.insert(tk.END, f"# {title}\n\n", "title")
                self.proc_area.insert(tk.END, desc)
                self.proc_area.insert(tk.END, "\n")

                # Auto-save as .md file with the AI-generated title
                rec = save_procedure(self.last_log_path, desc, model_name, title=title)
                self.proc_area.insert(
                    tk.END, f"\n[saved: {rec['file_path']}]\n", "saved")
                self.set_status(f"Procedure saved: {Path(rec['file_path']).name}")
            except ImportError:
                self.proc_area.delete(1.0, tk.END)
                self.proc_area.insert(tk.END,
                    "AI summaries are optional. To enable them:\n\n"
                    "  1. Install Ollama: https://ollama.com/download\n"
                    "  2. Run: ollama pull ministral-3:3b\n"
                    "  3. Click 'Analyze with AI' after recording\n\n"
                    "Your recording was saved. Replay works without Ollama.")
                self.set_status("AI unavailable — install Ollama for summaries")
            except Exception as e:
                msg = str(e)
                if "Connection" in msg or "refused" in msg or "11434" in msg:
                    self.proc_area.delete(1.0, tk.END)
                    self.proc_area.insert(tk.END,
                        "Ollama is not running. To enable AI summaries:\n\n"
                        "  1. Install Ollama: https://ollama.com/download\n"
                        "  2. Start it: ollama serve\n"
                        "  3. Pull a model: ollama pull ministral-3:3b\n"
                        "  4. Click 'Analyze with AI' after recording\n\n"
                        "Your recording was saved. Replay works without Ollama.")
                    self.set_status("Ollama not running — recording saved")
                elif "model" in msg.lower() and "not found" in msg.lower():
                    self.proc_area.delete(1.0, tk.END)
                    self.proc_area.insert(tk.END,
                        f"Model '{model_name}' is not installed.\n\n"
                        f"Run: ollama pull {model_name}\n\n"
                        "Your recording was saved. Replay works without Ollama.")
                    self.set_status(f"Model missing — run: ollama pull {model_name}")
                else:
                    self.proc_area.delete(1.0, tk.END)
                    self.proc_area.insert(tk.END, f"AI error: {e}")
                    self.set_status(f"AI error: {e}")

        threading.Thread(target=run, daemon=True).start()

    def show_procedures(self):
        procs = list_procedures()
        win = tk.Toplevel(self.root)
        win.title(f"Procedures ({len(procs)})")
        win.geometry("900x650")
        win.minsize(700, 500)

        if not procs:
            ttk.Label(win, text="No procedures saved yet.\nRecord something to create one!",
                      padding=20).pack()
            return

        top = ttk.Frame(win, padding=4)
        top.pack(fill=tk.X)
        ttk.Label(top, text=f"{len(procs)} saved procedures. Click one to view, then Replay.",
                  foreground="gray").pack(side=tk.LEFT)

        # Split: list on left, details on right
        body = ttk.Frame(win, padding=4)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # Left column: list + a stacked "Replay" button below it
        left_col = ttk.Frame(body)
        left_col.grid(row=0, column=0, sticky="nsw")
        left_col.rowconfigure(0, weight=1)
        left_col.columnconfigure(0, weight=1)

        listbox = tk.Listbox(left_col, font=("Segoe UI", 9), width=40)
        listbox.grid(row=0, column=0, sticky="nsew")
        for p in procs:
            title = p.get("title", "")
            label = f"{p['timestamp'][:19]}  {p['log_name']}"
            if title:
                label = f"{title}  ({p['timestamp'][:19]})"
            listbox.insert(tk.END, label)

        detail = scrolledtext.ScrolledText(body, wrap=tk.WORD, font=("Segoe UI", 10))
        detail.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        def do_replay(p):
            if not Path(p['log_path']).exists():
                messagebox.showerror("Missing", f"Log file not found:\n{p['log_path']}")
                return False
            self.last_log_path = p['log_path']
            self.replay_btn.config(state=tk.NORMAL, text="Replay Last")
            # Show in bottom panel for context
            self.proc_area.delete(1.0, tk.END)
            if p.get("title"):
                self.proc_area.insert(tk.END, f"# {p['title']}\n\n", "title")
            self.proc_area.insert(tk.END, p.get("description", ""))
            win.destroy()
            self.toggle_replay()
            return True

        def on_select(_evt=None):
            sel = listbox.curselection()
            if not sel:
                return
            p = procs[sel[0]]
            detail.delete(1.0, tk.END)
            if p.get("title"):
                detail.insert(tk.END, f"Title:   {p['title']}\n")
            detail.insert(tk.END, f"Time:    {p['timestamp']}\n")
            detail.insert(tk.END, f"Log:     {p['log_name']}\n")
            detail.insert(tk.END, f"Model:   {p['model']}\n\n")
            detail.insert(tk.END, "What you did:\n")
            detail.insert(tk.END, p['description'])
            detail.insert(tk.END, "\n")

        listbox.bind("<<ListboxSelect>>", on_select)
        # Double-click also replays; Delete key deletes
        listbox.bind("<Double-Button-1>", lambda _e: replay_btn.invoke())
        listbox.bind("<Delete>", lambda _e: delete_selected())
        listbox.focus_set()

        # Action buttons - placed on left below the list AND at bottom
        left_btns = ttk.Frame(left_col)
        left_btns.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        replay_btn = ttk.Button(left_btns, text="▶ Replay",
                                command=lambda: do_replay(procs[listbox.curselection()[0]])
                                if listbox.curselection() else None)
        replay_btn.pack(fill=tk.X)

        # Bottom button bar
        btns = ttk.Frame(win, padding=4)
        btns.pack(fill=tk.X)

        def replay_from_md():
            path = filedialog.askopenfilename(
                title="Select a procedure .md file",
                initialdir=PROCEDURES_DIR,
                filetypes=[("Procedure files", "*.md"), ("All files", "*.*")],
            )
            if not path:
                return
            rec = parse_procedure_md(path)
            if not rec or not rec.get("log_path"):
                messagebox.showerror("Invalid file",
                                     "Could not read log path from this .md file.")
                return
            if not Path(rec["log_path"]).exists():
                messagebox.showerror("Missing log",
                                     f"Log file no longer exists:\n{rec['log_path']}\n\n"
                                     f"Was the original recording deleted or moved?")
                return
            self.last_log_path = rec["log_path"]
            self.replay_btn.config(state=tk.NORMAL, text="Replay Last")
            self.proc_area.delete(1.0, tk.END)
            if rec.get("title"):
                self.proc_area.insert(tk.END, f"# {rec['title']}\n\n", "title")
            self.proc_area.insert(tk.END, rec.get("description", ""))
            win.destroy()
            self.toggle_replay()

        ttk.Button(btns, text="Replay from .md file...", command=replay_from_md).pack(side=tk.LEFT)

        def delete_selected():
            sel = listbox.curselection()
            if not sel:
                return
            idx = sel[0]
            p = procs[idx]
            label = p.get("title") or p.get("log_name") or p.get("timestamp")
            # Check if log is shared with other procedures
            shared = _log_path_is_referenced(p.get("log_path", ""), p)
            extra = ""
            if not shared and p.get("log_path"):
                extra = (
                    f"\n\nThe matching log file and screenshot folder will also be deleted "
                    f"(no other procedure uses them):\n  {Path(p['log_path']).name}\n  "
                    f"{Path(p['log_path']).stem.replace('events_', 'screenshots_', 1) if Path(p['log_path']).stem.startswith('events_') else 'screenshots folder'}/"
                )
            else:
                extra = "\n\n(Log file is shared with another procedure, so it will be kept.)"
            if not messagebox.askyesno(
                "Delete procedure?",
                f"Permanently delete this procedure?\n\n{label}{extra}",
                parent=win,
            ):
                return
            res = delete_procedure(p)
            # Remove from the live list and listbox
            del procs[idx]
            listbox.delete(idx)
            detail.delete(1.0, tk.END)
            if not procs:
                win.destroy()
                self.show_procedures()  # re-open to show "no procedures" message
                return
            # Re-select next item
            new_idx = min(idx, len(procs) - 1)
            listbox.selection_set(new_idx)
            on_select()
            parts = []
            if res["md"]:
                parts.append(".md")
            if res["log"]:
                parts.append("log")
            if res["screenshots"]:
                parts.append("screenshots")
            what = ", ".join(parts) if parts else "index only"
            self.set_status(f"Deleted: {label} ({what})")

        ttk.Button(btns, text="Delete selected", command=delete_selected).pack(side=tk.LEFT, padx=4)

        ttk.Button(btns, text="Open procedures folder",
                   command=lambda: __import__('subprocess').Popen(["explorer", str(PROCEDURES_DIR)])
                   ).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Open log folder",
                   command=lambda: __import__('subprocess').Popen(["explorer", str(LOG_DIR)])
                   ).pack(side=tk.LEFT, padx=4)

        if procs:
            listbox.selection_set(0)
            on_select()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = ActivityRecorderApp()
    app.run()
