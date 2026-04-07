#!/bin/bash
# Install the Telegram desktop entry and make the app launchable from GNOME

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Make the app executable
chmod +x "$SCRIPT_DIR/telegram.py"

# Install icon into hicolor theme
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICON_DIR"
cp "$SCRIPT_DIR/telegram.svg" "$ICON_DIR/telegram.svg"
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null || true

# Install desktop entry
cp "$SCRIPT_DIR/telegram.desktop" ~/.local/share/applications/telegram.desktop

# Update desktop database
update-desktop-database ~/.local/share/applications 2>/dev/null || true

echo "Done! Telegram should now appear in your GNOME app launcher."
echo "You can also run it directly: $SCRIPT_DIR/telegram.py"
