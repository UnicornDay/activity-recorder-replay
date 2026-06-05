"""
Windows Activity Recorder
Records mouse clicks, keyboard input, active window changes, and screenshots.
"""

import os
import sys
import json
import time
import threading
from datetime import datetime
from pathlib import Path

try:
    from pynput import mouse, keyboard
except ImportError:
    print("Missing pynput. Install with: py -3 -m pip install pynput")
    sys.exit(1)

try:
    import win32gui
    import win32con
    import win32api
    import win32process
except ImportError:
    print("Missing pywin32. Install with: py -3 -m pip install pywin32")
    sys.exit(1)

try:
    from PIL import ImageGrab
except ImportError:
    print("Missing Pillow. Install with: py -3 -m pip install pillow")
    sys.exit(1)

# ─── Configuration ───────────────────────────────────────────────────────────

LOG_DIR = Path.home() / "activity_logs"
LOG_DIR.mkdir(exist_ok=True)
SESSION_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
EVENT_LOG = LOG_DIR / f"events_{SESSION_ID}.jsonl"
SCREENSHOT_DIR = LOG_DIR / f"screenshots_{SESSION_ID}"
SCREENSHOT_DIR.mkdir(exist_ok=True)

RECORD_MOUSE = True
RECORD_KEYBOARD = True
RECORD_SCREENSHOTS = True
SCREENSHOT_INTERVAL = 30  # seconds between periodic screenshots
LOG_EVENTS_TO_CONSOLE = True

# ─── State ────────────────────────────────────────────────────────────────────

active_window = ""
last_screenshot_time = 0
stop_event = threading.Event()

# ─── Helpers ──────────────────────────────────────────────────────────────────


def get_active_window_title():
    """Return the title of the currently focused window."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        return win32gui.GetWindowText(hwnd) or "(unnamed window)"
    except Exception:
        return "(unknown)"


def get_active_window_exe():
    """Return the executable path of the currently focused window."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
            False, pid
        )
        exe = win32process.GetModuleFileNameEx(handle, 0)
        win32api.CloseHandle(handle)
        return exe or "(unknown)"
    except Exception:
        return "(unknown)"


def log_event(event_type, data):
    """Write a JSON event to the log file and optionally print it."""
    record = {
        "timestamp": datetime.now().isoformat(),
        "type": event_type,
        "data": data,
    }
    with open(EVENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    if LOG_EVENTS_TO_CONSOLE:
        print(f"[{record['timestamp']}] {event_type}: {json.dumps(data, ensure_ascii=False)}")


def take_screenshot():
    """Capture the primary screen and save to disk."""
    global last_screenshot_time
    now = time.time()
    if now - last_screenshot_time < 1:
        return  # debounce
    last_screenshot_time = now
    try:
        img = ImageGrab.grab()
        filename = f"screenshot_{datetime.now().strftime('%H%M%S_%f')}.png"
        path = SCREENSHOT_DIR / filename
        img.save(path)
        log_event("screenshot", {"path": str(path)})
    except Exception as e:
        log_event("screenshot_error", {"error": str(e)})


def focus_checker():
    """Background thread that logs active-window changes."""
    global active_window
    while not stop_event.is_set():
        current = get_active_window_title()
        exe = get_active_window_exe()
        if current != active_window:
            active_window = current
            log_event("focus_change", {"title": current, "exe": exe})
        stop_event.wait(0.5)


def periodic_screenshot():
    """Background thread for periodic screenshots."""
    while not stop_event.is_set():
        stop_event.wait(SCREENSHOT_INTERVAL)
        if not stop_event.is_set() and RECORD_SCREENSHOTS:
            take_screenshot()


# ─── Callbacks ────────────────────────────────────────────────────────────────


def on_click(x, y, button, pressed):
    if not RECORD_MOUSE:
        return
    action = "press" if pressed else "release"
    log_event("mouse_click", {
        "x": x,
        "y": y,
        "button": str(button),
        "action": action,
        "window": get_active_window_title(),
    })


def on_scroll(x, y, dx, dy):
    if not RECORD_MOUSE:
        return
    log_event("mouse_scroll", {
        "x": x,
        "y": y,
        "dx": dx,
        "dy": dy,
        "window": get_active_window_title(),
    })


def on_press(key):
    if not RECORD_KEYBOARD:
        return
    try:
        key_str = key.char if key.char else str(key)
    except AttributeError:
        key_str = str(key)
    log_event("key_press", {
        "key": key_str,
        "window": get_active_window_title(),
    })


def on_release(key):
    if not RECORD_KEYBOARD:
        return
    try:
        key_str = key.char if key.char else str(key)
    except AttributeError:
        key_str = str(key)
    log_event("key_release", {
        "key": key_str,
        "window": get_active_window_title(),
    })
    # Escape to quit
    if key == keyboard.Key.esc:
        log_event("system", {"message": "ESC pressed, stopping recorder"})
        stop()
        return False


# ─── Start / Stop ─────────────────────────────────────────────────────────────


def stop():
    stop_event.set()
    # Returning False from on_release stops the keyboard listener,
    # but we also stop the mouse listener explicitly.
    mouse_listener.stop()


def start():
    global mouse_listener

    print(f"Activity Recorder started")
    print(f"  Events log:  {EVENT_LOG}")
    if RECORD_SCREENSHOTS:
        print(f"  Screenshots: {SCREENSHOT_DIR}\\")
    print(f"  Press ESC to stop.")
    print()

    # Record startup
    log_event("session_start", {
        "session_id": SESSION_ID,
        "record_mouse": RECORD_MOUSE,
        "record_keyboard": RECORD_KEYBOARD,
        "record_screenshots": RECORD_SCREENSHOTS,
    })

    # Background threads
    threading.Thread(target=focus_checker, daemon=True).start()
    if RECORD_SCREENSHOTS:
        threading.Thread(target=periodic_screenshot, daemon=True).start()

    # Listeners (blocking)
    mouse_listener = mouse.Listener(on_click=on_click, on_scroll=on_scroll)
    mouse_listener.start()

    with keyboard.Listener(on_press=on_press, on_release=on_release) as kbl:
        kbl.join()

    mouse_listener.join()

    log_event("session_end", {"session_id": SESSION_ID})
    print(f"\nSession ended. Log saved to {EVENT_LOG}")


# ─── Entry Point ──────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("Windows Activity Recorder")
    print("=" * 50)
    print("This script records mouse clicks, keystrokes, and active windows.")
    print("Logs are saved to:", LOG_DIR)
    print("Press Ctrl+C in this terminal to abort startup.")
    print("Press ESC during recording to stop cleanly.")
    print()

    try:
        start()
    except KeyboardInterrupt:
        stop()
        print("\nInterrupted by user.")
