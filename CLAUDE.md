# Telegram Desktop — GTK4/WebKitGTK Native GNOME Wrapper

> Guardrails: [umami.md](https://github.com/goodcol-dennis/umami/blob/main/umami.md) — Tier 1 (Foundation)

## Versions & Environment

| Component | Version |
|-----------|---------|
| OS | Ubuntu 26.04 |
| Desktop | GNOME (Wayland) |
| Python | 3 (system) |
| GTK | 4.0 (`gi.require_version("Gtk", "4.0")`) |
| libadwaita | 1 (`gi.require_version("Adw", "1")`) |
| WebKitGTK | 6.0 (`gi.require_version("WebKit", "6.0")`) |
| GPU | Intel Arc (Arrow Lake-P) |

System packages already installed: `gir1.2-webkit-6.0`, GTK4, libadwaita, Python 3.

## Common Commands

```bash
./telegram.py          # Run the app
./install.sh           # Install icon + desktop entry to GNOME launcher
```

## Project Structure

```
telegram/
├── CLAUDE.md           # This file — project instructions
├── telegram.py         # Single-file app (all logic here)
├── telegram.svg        # App icon (blue rounded square, white paper plane)
├── telegram.desktop    # GNOME desktop entry
├── install.sh          # Installs icon + .desktop to ~/.local/share/
└── .gitignore
```

## Architecture

- Python 3 + GTK4 + libadwaita + WebKitGTK 6.0 (no Electron)
- Single-file app: `telegram.py`
- `Adw.ApplicationWindow` with `Adw.ToolbarView` + `Adw.HeaderBar` for native GNOME window controls
- WebKitGTK `NetworkSession.new(data_directory=..., cache_directory=...)` for persistent session/cookies
- Target URL: `https://web.telegram.org/a/` (Telegram Web A)
- App ID: `com.local.Telegram`
- Data directory: `~/.local/share/telegram-web/`

## Critical Rules

1. **No Electron** — GTK4/WebKitGTK only. No extra menus or chrome beyond GNOME header bar.
2. **Single-file app** — All logic lives in `telegram.py`. No splitting into modules unless it exceeds 400 lines.
3. **Persistent login** — Cookies + IndexedDB stored in `~/.local/share/telegram-web/`. ITP must be disabled (`set_itp_enabled(False)`) or auth tokens get purged.
4. **External links in browser** — Handle both `decide-policy` (NAVIGATION + NEW_WINDOW_ACTION) and the `create` signal for `target="_blank"` / `window.open()`.
5. **No `WEBKIT_DISABLE_DMABUF_RENDERER`** — Causes severe keyboard/rendering lag on Intel Arc. Shader warnings are cosmetic; ignore them.
6. **Suppress WebKitGTK context menu** — Return `True` from `context-menu` signal so Telegram's own right-click menu works.

## Change Propagation Map

| Change type | Files touched (in order) |
|-------------|--------------------------|
| App behavior / features | `telegram.py` → test manually → `CLAUDE.md` (if new critical rule) |
| Icon change | `telegram.svg` → `./install.sh` (re-install) |
| Desktop entry metadata | `telegram.desktop` → `./install.sh` (re-install) |
| New system dependency | Verify installed → `CLAUDE.md` versions table |

## Implementation Notes

### Clipboard Image Paste (implemented)
WebKitGTK does NOT expose clipboard images to the web Clipboard API. The bridge works as:
1. JS intercepts `paste` events via user script, sends message to Python via `clipboardBridge` handler
2. Python reads GTK clipboard using `clipboard.read_async(["text/uri-list"], ...)` — **not** `read_text_async()` which the GNOME portal silently blocks
3. Python base64-encodes the image file bytes
4. Python calls JS `_injectClipboardImage()` which dispatches a synthetic `ClipboardEvent('paste')` with a `DataTransfer` containing the file
5. Telegram Web's paste handler consumes this natively (`defaultPrevented: true`)

### Notifications (implemented)
1. Auto-grant `NotificationPermissionRequest` via `permission-request` signal
2. Handle `show-notification` signal on the WebView
3. Forward as `Gio.Notification` via `app.send_notification()`

### Dock Badge (implemented)
1. Watch `notify::title` signal — Telegram Web sets title to `"Telegram (N)"` for unread count
2. Parse count with regex, send via `com.canonical.Unity.LauncherEntry` DBus signal

### External Links (implemented)
1. `decide-policy` handles `NAVIGATION_ACTION` + `NEW_WINDOW_ACTION`
2. `create` signal catches `window.open()` / `target="_blank"` before policy decision
3. Both use `Gio.AppInfo.launch_default_for_uri()` to open in default browser

### Audio Playback (not yet implemented)
WebKitGTK defaults break audio. Required:
1. `WebKit.WebsitePolicies(autoplay=WebKit.AutoplayPolicy.ALLOW)` passed to WebView constructor
2. `settings.set_enable_webaudio(True)` and `settings.set_enable_encrypted_media(True)`
3. Allow `blob:` and `data:` URIs in navigation policy
4. Allow all `*.telegram.org` subdomains

### Permissions (implemented)
Auto-grant: `NotificationPermissionRequest`, `MediaKeySystemPermissionRequest`, `UserMediaPermissionRequest`, `ClipboardPermissionRequest` (if available in WebKitGTK version).

## Reference Implementation
See `../whatsapp/whatsapp.py` for the sibling WhatsApp wrapper — same architecture, different URL/branding.

## Pre-Commit Checklist

- [ ] App launches and loads Telegram Web without errors
- [ ] No unrelated files modified (scope discipline)
- [ ] Changes match what was requested — nothing more, nothing less
- [ ] No debug logging left in (`console.log`, `print()`, `set_enable_developer_extras(True)`)
- [ ] No `WEBKIT_DISABLE_DMABUF_RENDERER` or `GSK_RENDERER` env vars (causes perf issues)
- [ ] Git status reviewed — no unintended files staged
- [ ] `.gitignore` excludes local data (`.sqlite`, `__pycache__/`, `.claude/settings.local.json`)
