# Telegram Desktop — GTK4/WebKitGTK Native Wrapper

## Goal
Build a native-feeling Telegram Web wrapper for GNOME on Ubuntu 26.04, identical in approach to the WhatsApp wrapper in `../whatsapp/`.

## Architecture
- Python 3 + GTK4 + libadwaita + WebKitGTK 6.0 (no Electron)
- Single-file app: `telegram.py`
- Uses `Adw.ApplicationWindow` with `Adw.ToolbarView` + `Adw.HeaderBar` for proper GNOME window controls (draggable title bar, close/minimize/maximize)
- WebKitGTK `NetworkSession.new(data_directory=..., cache_directory=...)` for persistent session/cookies
- Target URL: `https://web.telegram.org/a/` (Telegram Web A — the more modern client)
- App ID: `com.local.Telegram`

## Clipboard image paste
See `CLIPBOARD_HOWTO.md` for the full implementation guide. WebKitGTK does NOT expose clipboard images to the web Clipboard API. You must:
1. Intercept paste events in JS via user script
2. Read the file path from GTK clipboard in Python
3. Base64-encode the image file
4. Inject it into an `<input type="file">` via `DataTransfer` and fire a `change` event

## Notifications
WebKitGTK does NOT display desktop notifications automatically. You must:
1. Grant `NotificationPermissionRequest` via `permission-request` signal
2. Handle the `show-notification` signal on the WebView
3. Forward as `Gio.Notification` via `app.send_notification()`

## Key requirements
- Persistent login (cookies stored in `~/.local/share/telegram-web/`)
- Auto-grant notification and microphone/camera permissions
- External links open in default system browser
- No Electron, no extra menus — just the Telegram interface with a standard GNOME header bar
- SVG icon (Telegram style — blue rounded square, white paper plane)
- `.desktop` file for GNOME app launcher
- `install.sh` to install icon + desktop entry

## Reference implementation
See `../whatsapp/whatsapp.py` for the working pattern. Copy the structure closely — the only differences should be:
- App name/title: "Telegram"
- URL: `https://web.telegram.org/a/`
- Data directory: `~/.local/share/telegram-web/`
- App ID: `com.local.Telegram`
- Icon: Telegram-style (blue, paper plane)
- User agent: keep the same Safari/WebKit UA string

## System dependencies
Already installed: `gir1.2-webkit-6.0`, GTK4, libadwaita, Python 3.

## Files to create
1. `telegram.py` — the app (~100 lines)
2. `telegram.svg` — icon
3. `telegram.desktop` — launcher entry
4. `install.sh` — installs icon + desktop entry
