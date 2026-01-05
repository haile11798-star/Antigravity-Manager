## Antigravity Python GUI (Windows-friendly)

This Tkinter app now implements the real Google OAuth flow plus quota retrieval (mirroring the Rust backend logic) in pure Python. It focuses on Windows users but works cross-platform.

### Features
- OAuth login (opens browser, captures the callback, exchanges code for access/refresh token).
- Auto-fetch user info to bind accounts by email.
- Real quota refresh via `cloudcode-pa` APIs, including subscription tier detection.
- Token refresh with expiry tracking and manual add/edit options.
- Account list with add/delete, current-account marker, and proxy toggle per account.
- Export selected or all accounts as `{ email, refresh_token, access_token, expires_at }` JSON.
- Proxy settings editor: port, request timeout, auth mode, LAN toggle, upstream proxy, scheduling mode, API key regeneration, and start/stop status tracking.
- State persistence to `~/.antigravity_gui_state.json` so the UI restores between launches.

### Run
```bash
python python_gui/antigravity_gui.py
```

> Notes
> - OAuth uses the same client as the Tauri backend and requires an interactive browser to finish the consent screen.
> - Quota calls rely on the `cloudcode-pa` endpoints the Rust app uses; make sure your network can reach Google.
> - The built-in proxy toggle is intentionally state-only. Extend `ProxyService` to wire up a real proxy if desired.
