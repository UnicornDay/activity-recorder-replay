# Activity Recorder & Replayer

A Windows GUI app that records mouse/keyboard activity and replays it later, with AI-generated procedure descriptions and smart visual-matching replay.

## Features

- **Record** mouse clicks, drags, scrolls, keyboard input, and active window changes
- **Replay** recordings with adjustable speed
- **Smart visual click** — captures a small image at each click and finds the target on the current screen before clicking (handles moved/resized windows)
- **Drag support** — press, drag, release are logged and replayed correctly
- **AI-generated procedures** — uses local Ollama models to summarize recordings into titled Markdown procedures
- **Global hotkeys** — control everything from any app:
  - `Ctrl+Shift+R` — toggle record
  - `Ctrl+Shift+P` — toggle replay
  - `Ctrl+Shift+O` — open/replay log
  - `Ctrl+Shift+L` — show all procedures
- **Fail-fast option** — if a recorded element can no longer be found on screen (UI changed), show a popup and stop instead of clicking the wrong place
- **Fast replay mode** — skip visual matching for fastest replay when the target app is unchanged
- **Procedure library** — browse, replay, and delete saved procedures (`.md` files)

## Requirements

- Windows 10/11
- Python 3.10+
- [Ollama](https://ollama.com) running locally (for AI procedure generation)
- Tesseract is **not** required

## Installation

```powershell
# 1. Install Python dependencies
py -3 -m pip install -r requirements.txt

# 2. (Optional) Install Ollama and pull a small model
ollama pull ministral-3:3b
```

Ollama must be running (`ollama serve`) before you click "Analyze with AI" or record (procedure generation runs after each recording).

## Usage

```powershell
py -3 activity_gui.py
```

The main window shows:
- **Log area** — live event stream and status messages
- **Procedure panel** (bottom) — the AI-generated description of the last recording
- **Toolbar**:
  - `Record` / `Replay Last` / `Load Log...` / `Analyze with AI` / `Show All Procedures`
  - `Fast (coords only)` — skip visual matching, use recorded coordinates only
  - `Smart click (visual)` — locate each click target via image matching
  - `Wait: 2s` — max wait per click for slow-loading pages
  - `Fail if no match` — stop with a popup if a target can't be found (UI changed)

### Recording

1. Click `Record` (or press `Ctrl+Shift+R`)
2. Perform the task normally
3. Click `Record` again (or press `Ctrl+Shift+R`) to stop
4. The app automatically generates a procedure via Ollama and saves it to `~/activity_logs/procedures/`

### Replay

- **Replay Last** — replay the most recent recording
- **Show All Procedures** — pick from the library
- Adjust the speed multiplier in the toolbar (e.g. `0.5` = half speed, `2.0` = double)
- Press `Esc` to abort mid-replay

### Replay behavior

- Each click is preceded by a one-shot visual match (~100 ms). If the target is found, that position is used; if not, the recorded coordinates are used.
- Drags are replayed with proper `press` / `move` / `release` sequences.
- The active window is reactivated if it has changed between events.
- The main window hides during replay to avoid self-matching.

## Output layout

```
~/activity_logs/
├── events_<session_id>.jsonl       # raw event log
├── screenshots_<session_id>/       # periodic screenshots
└── procedures/
    ├── procedures.jsonl            # index of all procedures
    └── <title-slug>.md             # one Markdown file per procedure
```

## Files

- `activity_gui.py` — the entire app in one self-contained Python file
- `requirements.txt` — Python dependencies

## Notes

- DPI awareness is set to per-monitor v2 at startup so clicks line up correctly on high-DPI displays.
- All four global hotkeys are active from the moment the GUI opens.
- Recorded click positions are absolute screen coordinates.
- A 10 s polling wait is available for slow-loading pages — set `Wait:` to 10.

## License

[MIT](LICENSE) — do whatever you want with it.
