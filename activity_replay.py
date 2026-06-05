"""
Replay recorded activity from a JSONL log file.
"""

import json
import sys
import time
from pathlib import Path

try:
    from pynput import mouse, keyboard
except ImportError:
    print("Missing pynput. Install with: py -3 -m pip install pynput")
    sys.exit(1)

try:
    import win32gui
    import win32con
except ImportError:
    print("Missing pywin32. Install with: py -3 -m pip install pywin32")
    sys.exit(1)


def find_window(title):
    """Find and return a window handle matching the given title."""
    def enum_callback(hwnd, results):
        if title in win32gui.GetWindowText(hwnd):
            results.append(hwnd)
    results = []
    win32gui.EnumWindows(enum_callback, results)
    return results[0] if results else None


def bring_window_to_foreground(title):
    """Activate the window whose title contains the given string."""
    hwnd = find_window(title)
    if hwnd:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.3)
            return True
        except Exception:
            pass
    return False


def replay(log_path, speed=1.0):
    log_path = Path(log_path)
    if not log_path.exists():
        print(f"Log file not found: {log_path}")
        return

    events = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    if not events:
        print("No events found in log.")
        return

    mouse_ctrl = mouse.Controller()
    kb_ctrl = keyboard.Controller()

    print(f"Replaying {len(events)} events from {log_path}")
    print(f"Speed multiplier: {speed}x")
    print("Move your mouse to a corner or press Ctrl+C to abort.")
    print("Starting in 3 seconds...")
    time.sleep(3)

    prev_ts = None
    replay_count = 0
    last_window = ""

    for ev in events:
        typ = ev["type"]
        data = ev["data"]

        # Timing
        ts = ev["timestamp"]
        if prev_ts and speed > 0:
            try:
                delay = (datetime.fromisoformat(ts) - datetime.fromisoformat(prev_ts)).total_seconds() / speed
                if delay > 0:
                    time.sleep(delay)
            except Exception:
                pass
        prev_ts = ts

        # Handle focus changes
        if typ == "focus_change":
            last_window = data.get("title", "")
            print(f"  Switching to: {last_window}")
            bring_window_to_foreground(last_window)

        # Mouse clicks
        elif typ == "mouse_click" and data.get("action") == "press":
            x, y = data["x"], data["y"]
            btn_str = data.get("button", "Button.left")
            btn = {
                "Button.left": mouse.Button.left,
                "Button.right": mouse.Button.right,
                "Button.middle": mouse.Button.middle,
            }.get(btn_str, mouse.Button.left)

            if data.get("window") and data["window"] != last_window:
                bring_window_to_foreground(data["window"])
                last_window = data["window"]

            mouse_ctrl.position = (x, y)
            time.sleep(0.05)
            mouse_ctrl.click(btn)
            replay_count += 1
            print(f"  [{replay_count}] Click at ({x}, {y}) [{btn}]")

        # Mouse scroll
        elif typ == "mouse_scroll":
            mouse_ctrl.scroll(data.get("dx", 0), data.get("dy", 0))
            replay_count += 1

        # Key presses
        elif typ == "key_press":
            key_str = data["key"]
            k = _parse_key(key_str)
            if k is not None:
                kb_ctrl.press(k)
                replay_count += 1

        # Key releases
        elif typ == "key_release":
            key_str = data["key"]
            k = _parse_key(key_str)
            if k is not None:
                kb_ctrl.release(k)

        # Ignore screenshots, session events
        elif typ in ("screenshot", "screenshot_error", "session_start", "session_end", "system"):
            continue

    print(f"\nReplay complete. {replay_count} actions replayed.")


def _parse_key(key_str):
    """Convert a pynput key string to a Key or KeyCode object."""
    SPECIAL_KEYS = {
        "Key.esc": keyboard.Key.esc,
        "Key.enter": keyboard.Key.enter,
        "Key.tab": keyboard.Key.tab,
        "Key.space": keyboard.Key.space,
        "Key.backspace": keyboard.Key.backspace,
        "Key.delete": keyboard.Key.delete,
        "Key.shift": keyboard.Key.shift,
        "Key.shift_r": keyboard.Key.shift_r,
        "Key.ctrl": keyboard.Key.ctrl,
        "Key.ctrl_l": keyboard.Key.ctrl_l,
        "Key.ctrl_r": keyboard.Key.ctrl_r,
        "Key.alt": keyboard.Key.alt,
        "Key.alt_gr": keyboard.Key.alt_gr,
        "Key.cmd": keyboard.Key.cmd,
        "Key.caps_lock": keyboard.Key.caps_lock,
        "Key.up": keyboard.Key.up,
        "Key.down": keyboard.Key.down,
        "Key.left": keyboard.Key.left,
        "Key.right": keyboard.Key.right,
        "Key.home": keyboard.Key.home,
        "Key.end": keyboard.Key.end,
        "Key.page_up": keyboard.Key.page_up,
        "Key.page_down": keyboard.Key.page_down,
        "Key.insert": keyboard.Key.insert,
        "Key.f1": keyboard.Key.f1,
        "Key.f2": keyboard.Key.f2,
        "Key.f3": keyboard.Key.f3,
        "Key.f4": keyboard.Key.f4,
        "Key.f5": keyboard.Key.f5,
        "Key.f6": keyboard.Key.f6,
        "Key.f7": keyboard.Key.f7,
        "Key.f8": keyboard.Key.f8,
        "Key.f9": keyboard.Key.f9,
        "Key.f10": keyboard.Key.f10,
        "Key.f11": keyboard.Key.f11,
        "Key.f12": keyboard.Key.f12,
        "Key.print_screen": keyboard.Key.print_screen,
        "Key.scroll_lock": keyboard.Key.scroll_lock,
        "Key.pause": keyboard.Key.pause,
        "Key.menu": keyboard.Key.menu,
    }

    if key_str in SPECIAL_KEYS:
        return SPECIAL_KEYS[key_str]

    # Regular character
    try:
        if len(key_str) == 1:
            return keyboard.KeyCode.from_char(key_str)
    except Exception:
        pass

    print(f"  Warning: unknown key '{key_str}', skipping")
    return None


if __name__ == "__main__":
    from datetime import datetime

    if len(sys.argv) < 2:
        print("Usage: py -3 activity_replay.py <log_file.jsonl> [speed_multiplier]")
        print("  speed_multiplier: 0.5 = half speed, 2.0 = double speed (default 1.0)")
        sys.exit(1)

    log_file = sys.argv[1]
    speed = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    replay(log_file, speed)
