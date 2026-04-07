#!/usr/bin/env python3
"""Telegram Web — native GTK4/WebKitGTK wrapper for GNOME."""

import base64
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")

from gi.repository import Gtk, Adw, WebKit, GLib, Gio


APP_ID = "com.local.Telegram"
DATA_DIR = GLib.get_user_data_dir() + "/telegram-web"
TELEGRAM_URL = "https://web.telegram.org/a/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


CLIPBOARD_BRIDGE_JS = """
(function() {
    // Intercept paste events — if no image in clipboardData, ask Python
    document.addEventListener('paste', function(e) {
        if (e.clipboardData && e.clipboardData.files.length > 0) return;
        var text = (e.clipboardData && e.clipboardData.getData('text/plain')) || '';
        if (/\\.(png|jpe?g|gif|webp|bmp)$/im.test(text.trim())) {
            e.preventDefault();
        }
        window.webkit.messageHandlers.clipboardBridge.postMessage('paste');
    }, true);

    // Called from Python with base64 image data
    window._injectClipboardImage = function(b64, mime, filename) {
        var arr = Uint8Array.from(atob(b64), function(c) { return c.charCodeAt(0); });
        var file = new File([arr], filename || 'clipboard.png', {type: mime});
        var dt = new DataTransfer();
        dt.items.add(file);

        // Try to find an existing file input
        var inputs = document.querySelectorAll('input[type="file"]');
        if (inputs.length > 0) {
            var input = inputs[inputs.length - 1];
            input.files = dt.files;
            input.dispatchEvent(new Event('change', {bubbles: true}));
            return;
        }

        // Click the attach button to create a file input, then fill it
        var attachBtn = document.querySelector('.AttachMenu button, .attach-file, button[aria-label="Attach"]');
        if (!attachBtn) return;
        var observer = new MutationObserver(function(mutations, obs) {
            var newInput = document.querySelector('input[type="file"]');
            if (newInput) {
                obs.disconnect();
                newInput.files = dt.files;
                newInput.dispatchEvent(new Event('change', {bubbles: true}));
            }
        });
        observer.observe(document.body, {childList: true, subtree: true});
        attachBtn.click();
        setTimeout(function() { observer.disconnect(); }, 3000);
    };
})();
"""


class TelegramWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(
            title="Telegram",
            default_width=1100,
            default_height=750,
            **kwargs,
        )

        # Ensure data directory exists
        os.makedirs(DATA_DIR + "/cache", exist_ok=True)

        # -- WebKit setup --
        network_session = WebKit.NetworkSession.new(
            data_directory=DATA_DIR,
            cache_directory=DATA_DIR + "/cache",
        )

        # Persistent cookie jar so login survives restarts
        cookie_manager = network_session.get_cookie_manager()
        cookie_manager.set_persistent_storage(
            DATA_DIR + "/cookies.sqlite",
            WebKit.CookiePersistentStorage.SQLITE,
        )

        # Disable ITP so auth tokens in IndexedDB/localStorage are not purged
        network_session.set_itp_enabled(False)

        self.webview = WebKit.WebView(network_session=network_session)

        settings = self.webview.get_settings()
        settings.set_user_agent(USER_AGENT)
        settings.set_enable_media_stream(True)           # mic/camera access
        settings.set_enable_mediasource(True)
        settings.set_javascript_can_access_clipboard(True)
        settings.set_enable_developer_extras(False)
        settings.set_enable_smooth_scrolling(True)

        # -- Clipboard image paste bridge --
        content_manager = self.webview.get_user_content_manager()
        content_manager.register_script_message_handler("clipboardBridge")
        content_manager.connect(
            "script-message-received::clipboardBridge", self._on_paste_requested
        )
        # Inject JS that intercepts paste and asks Python for image data
        clipboard_js = WebKit.UserScript.new(
            source=CLIPBOARD_BRIDGE_JS,
            injected_frames=WebKit.UserContentInjectedFrames.ALL_FRAMES,
            injection_time=WebKit.UserScriptInjectionTime.START,
        )
        content_manager.add_script(clipboard_js)

        # Suppress WebKitGTK's default context menu so Telegram's own menu works
        self.webview.connect("context-menu", lambda *_: True)

        # Allow notification and media permission requests
        self.webview.connect("permission-request", self._on_permission_request)
        # Show web notifications as native GNOME notifications
        self.webview.connect("show-notification", self._on_show_notification)
        # Open external links in default browser
        self.webview.connect("decide-policy", self._on_decide_policy)

        # Track title changes for dock badge (unread count)
        self.webview.connect("notify::title", self._on_title_changed)

        self.webview.set_vexpand(True)
        self.webview.set_hexpand(True)
        self.webview.load_uri(TELEGRAM_URL)

        # -- Layout: proper GNOME/libadwaita structure --
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Telegram", subtitle=""))

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self.webview)

        self.set_content(toolbar_view)

    @staticmethod
    def _on_permission_request(_webview, request):
        """Auto-grant notification, media, and clipboard permissions."""
        if isinstance(
            request,
            (
                WebKit.NotificationPermissionRequest,
                WebKit.MediaKeySystemPermissionRequest,
                WebKit.UserMediaPermissionRequest,
            ),
        ):
            request.allow()
            return True
        # Clipboard read/write — needed for pasting images
        if hasattr(WebKit, "ClipboardPermissionRequest") and isinstance(
            request, WebKit.ClipboardPermissionRequest
        ):
            request.allow()
            return True
        return False

    def _on_show_notification(self, _webview, notification):
        """Forward web notifications to GNOME desktop notifications."""
        app = self.get_application()
        if not app:
            return False
        gnome_notif = Gio.Notification.new(notification.get_title() or "Telegram")
        body = notification.get_body()
        if body:
            gnome_notif.set_body(body)
        gnome_notif.set_default_action("app.activate")
        app.send_notification(None, gnome_notif)
        return True

    def _on_paste_requested(self, _content_manager, message):
        """Handle paste request from JS — read GTK clipboard for image data."""
        clipboard = self.get_clipboard()
        formats = clipboard.get_formats()
        mime_types = formats.get_mime_types() or []

        if any(m.startswith("image/") for m in mime_types):
            clipboard.read_texture_async(None, self._on_clipboard_texture, None)
        elif "text/uri-list" in mime_types or "text/plain" in mime_types or "text/plain;charset=utf-8" in mime_types:
            clipboard.read_text_async(None, self._on_clipboard_text, None)

    def _on_clipboard_texture(self, clipboard, result, _user_data):
        """Handle raw image texture from clipboard."""
        try:
            texture = clipboard.read_texture_finish(result)
            png_bytes = texture.save_to_png_bytes()
            b64 = base64.b64encode(png_bytes.get_data()).decode("ascii")
            self._inject_image(b64, "image/png", "clipboard.png")
        except Exception:
            pass

    def _on_clipboard_text(self, clipboard, result, _user_data):
        """Handle text/URI clipboard — check if it's a file path to an image."""
        try:
            text = clipboard.read_text_finish(result)
            if not text:
                return
            # Parse file:// URIs or plain paths
            text = text.strip()
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("file://"):
                    path = unquote(urlparse(line).path)
                elif line.startswith("/"):
                    path = line
                else:
                    continue
                p = Path(path)
                if p.is_file() and p.suffix.lower() in (
                    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                ):
                    data = p.read_bytes()
                    b64 = base64.b64encode(data).decode("ascii")
                    mime_map = {
                        ".png": "image/png", ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg", ".gif": "image/gif",
                        ".webp": "image/webp", ".bmp": "image/bmp",
                    }
                    mime = mime_map.get(p.suffix.lower(), "image/png")
                    self._inject_image(b64, mime, p.name)
                    return
        except Exception:
            pass

    def _inject_image(self, b64, mime, filename):
        """Send base64 image data to JS for injection."""
        js = f"window._injectClipboardImage('{b64}', '{mime}', '{filename}');"
        self.webview.evaluate_javascript(js, -1, None, None, None, None, None)

    def _on_title_changed(self, webview, _pspec):
        """Extract unread count from page title and update dock badge."""
        title = webview.get_title() or ""
        # Telegram Web sets title like "Telegram (3)" when there are unread messages
        match = re.search(r"\((\d+)\)", title)
        count = int(match.group(1)) if match else 0
        app = self.get_application()
        if app:
            app.update_badge(count)

    def _on_decide_policy(self, _webview, decision, decision_type):
        """Open non-Telegram links in the system browser."""
        if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            nav = decision.get_navigation_action()
            req = nav.get_request()
            uri = req.get_uri()
            if uri and not uri.startswith(TELEGRAM_URL) and not uri.startswith("https://web.telegram.org"):
                decision.ignore()
                Gio.AppInfo.launch_default_for_uri(uri, None)
                return True
        return False


class TelegramApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._dbus_connection = None
        self._badge_count = 0

    def do_startup(self):
        Adw.Application.do_startup(self)
        self._dbus_connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = TelegramWindow(application=self)
        win.present()

    def update_badge(self, count):
        """Update the dock badge via Unity Launcher API (works with Ubuntu Dock)."""
        if count == self._badge_count or not self._dbus_connection:
            return
        self._badge_count = count
        self._dbus_connection.emit_signal(
            None,
            "/com/local/Telegram",
            "com.canonical.Unity.LauncherEntry",
            "Update",
            GLib.Variant("(sa{sv})", (
                "application://telegram.desktop",
                {
                    "count": GLib.Variant("x", count),
                    "count-visible": GLib.Variant("b", count > 0),
                },
            )),
        )


def main():
    app = TelegramApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
