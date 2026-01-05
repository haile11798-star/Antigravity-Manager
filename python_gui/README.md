## Antigravity Python GUI (Windows-friendly)

This lightweight Tkinter app re-implements the repository's account management and proxy setting flows in pure Python. It focuses on Windows users but works cross-platform.

### Features
- Account list with add/delete, current-account marker, and proxy toggle per account.
- Simulated quota refresh (updates timestamps and demo model percentages).
- Export selected or all accounts as `{ email, refresh_token }` JSON.
- Proxy settings editor: port, request timeout, auth mode, LAN toggle, upstream proxy, scheduling mode, API key regeneration, and start/stop status tracking.
- State persistence to `~/.antigravity_gui_state.json` so the UI restores between launches.

### Run
```bash
python python_gui/antigravity_gui.py
```

> Note: The built-in proxy toggle is intentionally state-only. It does not bind to a real port; you can extend `ProxyService` to launch your own proxy implementation if needed.
