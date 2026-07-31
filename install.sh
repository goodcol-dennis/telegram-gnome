#!/bin/bash
# Install the Telegram desktop entry and make the app launchable from GNOME

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Kill running instance if any. Match the interpreter invocation rather than a
# bare path, so a moved checkout still stops without matching unrelated shells.
pkill -f "python3? .*/telegram\.py" 2>/dev/null && echo "Stopped running instance." || true

# Make the app executable
chmod +x "$SCRIPT_DIR/telegram.py"

# Install icon into hicolor theme
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICON_DIR"
cp "$SCRIPT_DIR/telegram.svg" "$ICON_DIR/telegram.svg"
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null || true

# Install desktop entry, rewriting Exec/Icon to this checkout's location.
# Filename MUST match the app id (com.local.Telegram) or GNOME rejects every
# Gio.Notification sent by the app.
mkdir -p ~/.local/share/applications
sed -e "s|^Exec=.*|Exec=$SCRIPT_DIR/telegram.py %u|" \
    -e "s|^Icon=.*|Icon=$SCRIPT_DIR/telegram.svg|" \
    "$SCRIPT_DIR/com.local.Telegram.desktop" > ~/.local/share/applications/com.local.Telegram.desktop
# Purge the stale pre-rename entry
rm -f ~/.local/share/applications/telegram.desktop

# Update desktop database and register tg:// URI handler
update-desktop-database ~/.local/share/applications 2>/dev/null || true
xdg-mime default com.local.Telegram.desktop x-scheme-handler/tg

# Relaunch the app
echo "Relaunching Telegram..."
setsid nohup "$SCRIPT_DIR/telegram.py" > /dev/null 2>&1 < /dev/null &
disown

echo "Done! Telegram is running and available in your GNOME app launcher."
