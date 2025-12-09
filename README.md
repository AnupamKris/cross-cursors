## Cross Cursors

Small PySide6 utility that turns your screen into a mouse-capture overlay. It
registers a global shortcut so you can toggle an always-on-top layer that
blocks clicks to underlying apps while showing live mouse coordinates and the
events you trigger. Handy for forwarding cursor data to another machine or just
inspecting input.

### Features
- Global hotkeys: `Ctrl+Alt+O` to toggle the overlay, `Ctrl+Alt+Q` to quit.
- Overlay blocks app interaction while it is visible.
- Live display of mouse moves, presses/releases, wheel deltas, and global
  coordinates.
- Adjustable overlay resolution to match a remote screen or custom capture
  area.
- Optional auto-activate when the cursor hits a configurable hot corner
  (position + size), with an on-screen marker on every monitor.
- TCP server broadcast of mouse events for remote control clients.
- Escape closes the overlay in case the hotkey is unavailable.

### Run it
```bash
uv run python main.py
```

Use the controller window to set the overlay width/height, then use the global
hotkey to enable the overlay. Mouse events are also printed to stdout if you
need to pipe them elsewhere.

### Remote mouse server/client
- In the controller, enable "Serve mouse events over TCP" and pick a port
  (defaults to 8765). The app persists your settings to `config.json`.
- On another machine (or the same), run the client:
  ```bash
  uv run python client.py --host <server-ip> --port 8765
  ```
  The client uses `pynput` and works on Linux/Windows to move/click/scroll the
  local mouse based on server events.

