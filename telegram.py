#!/usr/bin/env python3
"""Telegram Web — native GTK4/WebKitGTK wrapper for GNOME."""

import base64
import json
import os
import sys
import time
from urllib.parse import quote, unquote, urlparse

# Intel Arrow Lake-P on the i915 driver mis-negotiates dmabuf tiling modifiers:
# frames arrive sheared into diagonal bands. Reproduced in stock GNOME Web, so
# it is a driver/engine bug, not ours. GBM is what negotiates those modifiers,
# so disabling only GBM sidesteps the bug while keeping the zero-copy dmabuf
# path — unlike WEBKIT_DISABLE_DMABUF_RENDERER, which also clears it but falls
# back to shared memory and costs noticeable input latency (rule 7, playbook #29).
# Remove once the GPU is on the `xe` driver or Mesa/WebKit fix the negotiation.
# TELEGRAM_FORCE_DMABUF=1 disables this guard entirely (to re-test the hardware).
if os.environ.get("TELEGRAM_FORCE_DMABUF") != "1":
    os.environ.setdefault("WEBKIT_DMABUF_RENDERER_DISABLE_GBM", "1")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")

from gi.repository import Gtk, Gdk, Adw, WebKit, GLib, Gio


DEV_LOGGING = "--dev" in sys.argv
if DEV_LOGGING:
    sys.argv.remove("--dev")
TEST_MODE = "--test" in sys.argv
if TEST_MODE:
    sys.argv.remove("--test")
    DEV_LOGGING = True
APP_ID = "com.local.Telegram.Test" if TEST_MODE else "com.local.Telegram"
# Separate profile in test mode — concurrent WebKit sessions on one
# data_directory corrupt cookies.sqlite/IndexedDB
DATA_DIR = GLib.get_user_data_dir() + (
    "/telegram-web-test" if TEST_MODE else "/telegram-web"
)
CONFIG_FILE = DATA_DIR + "/config.json"
# Test harness IO — TELEGRAM_TEST_DIR lets each headless session use its own pair
_TEST_DIR = os.environ.get("TELEGRAM_TEST_DIR", "/tmp")
TEST_INPUT_FILE = _TEST_DIR + "/telegram-test-input.txt"
TEST_STATE_FILE = _TEST_DIR + "/telegram-test-state.txt"
TELEGRAM_URL = "https://web.telegram.org/a/"
TELEGRAM_DOMAINS = ("telegram.org",)
# t.me short-link hosts — converted to in-app deep links, never navigated to
TME_HOSTS = ("t.me", "telegram.me", "telegram.dog")
DEFAULT_ZOOM = 1.0
ZOOM_STEP = 0.1
MIN_ZOOM = 0.5
MAX_ZOOM = 3.0
# Stock WebKitGTK UA, deliberately NOT overridden: telegram-tt's IS_SAFARI
# branch is its first-class tested path on WebKit — a Chrome-shaped UA
# enables Chromium-only code (round-video recording, wave transform, snap
# effect) that telegram-tt disables on WebKit because it is broken there,
# and hides the quick-upload drop target. There is no server-side UA wall;
# the only gate is client-side feature detection (compatTest.js).


def _load_zoom():
    try:
        with open(CONFIG_FILE) as f:
            zoom = float(json.load(f).get("zoom", DEFAULT_ZOOM))
    except (OSError, ValueError, TypeError, AttributeError):
        # AttributeError: top-level non-dict config; ValueError covers JSON errors
        return DEFAULT_ZOOM
    return max(MIN_ZOOM, min(MAX_ZOOM, zoom))


def _save_zoom(level):
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError):
        config = {}
    config["zoom"] = round(level, 2)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f)


def _load_user_agent():
    """config.json {"user_agent": ...} escape hatch — None means keep the
    stock WebKitGTK UA (the correct default for Telegram Web A)."""
    try:
        with open(CONFIG_FILE) as f:
            ua = json.load(f).get("user_agent")
        if isinstance(ua, str) and ua.strip():
            return ua.strip()
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _download_dir():
    """XDG download dir, falling back to ~/Downloads when user-dirs are unset."""
    d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
    return d or os.path.expanduser("~/Downloads")


def _write_state_file(content):
    """Atomic write — the e2e harness polls for the file and reads on first
    sighting, so it must never observe a partial write."""
    tmp = TEST_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, TEST_STATE_FILE)


def _telegram_uri_to_url(uri):
    """Convert a tg:// or t.me deep link to a web.telegram.org/a/ URL.

    Telegram Web A processes the tgaddr hash parameter through its own
    deep-link handler, which accepts both tg:// URIs and t.me URLs:
      tg://resolve?domain=NAME     https://t.me/NAME
      tg://join?invite=HASH        https://t.me/+HASH
      tg://privatepost?channel=X   https://t.me/c/X/N
    plus the subdomain form https://NAME.t.me (telegram-tt RE_TME_LINK).
    """
    parsed = urlparse(uri)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "tg":
        pass
    elif parsed.scheme in ("http", "https") and (
            (host in TME_HOSTS and parsed.path.strip("/"))
            or host.endswith(".t.me")):
        pass
    else:
        return None
    return TELEGRAM_URL + "#?tgaddr=" + quote(uri, safe="")


# Badge bridge: telegram-tt's intended app-badge channel is
# navigator.setAppBadge(count), fired on every change of the All-folder
# unread-notifications count. WebKitGTK has no setAppBadge, so this shim is a
# pure polyfill (defines a missing API — not a monkey-patch of an existing
# one). It supersedes title parsing: Web A's title never contains "(N)" — by
# default it is the *current chat's name*, so a title regex misfires.
BADGE_BRIDGE_JS = """
(function() {
    if (!('setAppBadge' in navigator)) {
        navigator.setAppBadge = function(n) {
            try { window.webkit.messageHandlers.badgeBridge.postMessage(Number(n) || 0); } catch (e) {}
            return Promise.resolve();
        };
        navigator.clearAppBadge = function() {
            try { window.webkit.messageHandlers.badgeBridge.postMessage(0); } catch (e) {}
            return Promise.resolve();
        };
    }
})();
"""

# telegram-tt's compatTest.js replaces the page with "browser not supported"
# on any missing feature probe. All probed features exist in WebKitGTK 2.52,
# but pre-seeding its own escape hatch makes a future probe addition degrade
# to a console warning instead of a dead app.
COMPAT_SEED_JS = """
try { localStorage.setItem('tt-ignore-compat', '1'); } catch (e) {}
"""

# JavaScript injected to bridge GTK clipboard into Telegram Web.
# Strategy: intercept the paste event, then inject a synthetic ClipboardEvent
# built with a constructor-init DataTransfer — Telegram Web A's own paste
# handler consumes it (defaultPrevented), proven in this app. Fall back to a
# synthetic drop on the composer, then to filling a file input directly.
PASTE_BRIDGE_JS = """
(function() {
    let _waitingForNative = false;
    let _pendingText = null;
    let _injecting = false;
    const _dev = %DEV%;
    function _log() { if (_dev) console.log.apply(console, ['[paste]'].concat(Array.from(arguments))); }

    document.addEventListener('paste', function(e) {
        // Hard re-entry guard: our own synthetic paste is dispatching right
        // now. The files.length check below also catches it, but only while
        // WebKit honors constructor-init clipboardData — this one always does.
        if (_injecting) return;
        _log('paste event, files=' + (e.clipboardData ? e.clipboardData.files.length : 'N/A'));
        if (e.clipboardData && e.clipboardData.files.length > 0) return;
        if (_waitingForNative) { _log('already waiting'); return; }

        // Intercept only when every non-empty line looks like a file path.
        // No extension whitelist — the Python side validates with isfile()
        // and falls back to application/octet-stream for unknown types.
        var text = (e.clipboardData && e.clipboardData.getData('text/plain')) || '';
        var t = text.trim();
        var looksLikePath = t !== '' && t.split('\\n').every(function(l) {
            l = l.trim();
            return l === '' || /^(file:\\/\\/\\/|\\/)/.test(l);
        });
        if (!looksLikePath && text) {
            _log('not a file path, letting paste through: ' + text.substring(0, 100));
            return;
        }

        _waitingForNative = true;
        if (looksLikePath) {
            // preventDefault only works synchronously — stash the text so a
            // failed native round-trip can restore it
            _pendingText = text;
            e.preventDefault();
        }
        window.webkit.messageHandlers.clipboardBridge.postMessage('paste');
    }, true);

    // Synthetic paste: Telegram Web A's document-level paste handler accepts
    // a ClipboardEvent carrying files. The re-entry into our own capture
    // listener above is harmless — it early-returns on files.length > 0.
    function injectViaPaste(file) {
        var dt = new DataTransfer();
        dt.items.add(file);
        try {
            var pasteEvent = new ClipboardEvent('paste', {
                bubbles: true,
                cancelable: true,
                clipboardData: dt
            });
            var target = document.activeElement || document;
            _injecting = true;
            try {
                target.dispatchEvent(pasteEvent);
            } finally {
                _injecting = false;
            }
            _log('paste defaultPrevented=' + pasteEvent.defaultPrevented);
            return pasteEvent.defaultPrevented;
        } catch (e) {
            _injecting = false;
            _log('synthetic paste failed: ' + e);
            return false;
        }
    }

    // Find the message composer for drop events
    function findDropTarget() {
        return document.querySelector('#editable-message-text')
            || document.querySelector('[contenteditable="true"]')
            || document.querySelector('#MiddleColumn')
            || document.body;
    }

    function injectViaDrop(file) {
        var dt = new DataTransfer();
        dt.items.add(file);
        var target = findDropTarget();
        _log('drop target: ' + target.tagName + (target.id ? '#' + target.id : ''));

        target.dispatchEvent(new DragEvent('dragenter', {dataTransfer: dt, bubbles: true, cancelable: true}));
        target.dispatchEvent(new DragEvent('dragover', {dataTransfer: dt, bubbles: true, cancelable: true}));
        var dropEvent = new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true});
        target.dispatchEvent(dropEvent);
        _log('drop defaultPrevented=' + dropEvent.defaultPrevented);
        return dropEvent.defaultPrevented;
    }

    function findFileInput() {
        var inputs = document.querySelectorAll('input[type="file"]');
        if (inputs.length > 0) return inputs[inputs.length - 1];
        return null;
    }

    function fillInput(input, file) {
        _log('filling input, accept=' + input.accept);
        var dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
        input.dispatchEvent(new Event('change', {bubbles: true}));
    }

    window._injectClipboardFile = function(b64, mime, filename) {
        _waitingForNative = false;
        _pendingText = null;
        mime = mime || 'application/octet-stream';
        filename = filename || ('clipboard.' + (mime.split('/')[1] || 'bin'));
        _log('injecting file: ' + filename + ', mime=' + mime + ', b64len=' + b64.length);

        var byteStr = atob(b64);
        var arr = new Uint8Array(byteStr.length);
        for (var i = 0; i < byteStr.length; i++) arr[i] = byteStr.charCodeAt(i);
        var file = new File([arr], filename, {type: mime});

        // Synthetic paste first (proven consumed by Telegram Web A), then
        // drop, then file input.
        if (injectViaPaste(file)) return;
        _log('paste not handled, trying drop injection');
        if (injectViaDrop(file)) return;
        var input = findFileInput();
        if (input) {
            _log('drop not handled, filling file input as fallback');
            fillInput(input, file);
        }
    };

    // Keep old name as alias
    window._injectClipboardImage = window._injectClipboardFile;

    window._injectClipboardImageFailed = function() {
        _log('clipboard injection failed');
        _waitingForNative = false;
        if (_pendingText) {
            // We swallowed the original paste — restore the text
            document.execCommand('insertText', false, _pendingText);
            _pendingText = null;
        }
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
        cookie_manager.set_accept_policy(WebKit.CookieAcceptPolicy.ALWAYS)

        # Disable ITP so auth tokens in IndexedDB/localStorage are not purged
        network_session.set_itp_enabled(False)

        # Track downloads for notifications
        self._network_session = network_session
        network_session.connect("download-started", self._on_download_started)

        policies = WebKit.WebsitePolicies(
            autoplay=WebKit.AutoplayPolicy.ALLOW,
        )
        self.webview = WebKit.WebView(
            network_session=network_session,
            website_policies=policies,
        )

        settings = self.webview.get_settings()
        ua_override = _load_user_agent()
        if ua_override:
            settings.set_user_agent(ua_override)
        settings.set_enable_media_stream(True)
        settings.set_enable_mediasource(True)
        settings.set_enable_webaudio(True)
        settings.set_enable_encrypted_media(True)
        # RTCPeerConnection is off by default; without it calls cannot connect
        settings.set_enable_webrtc(True)
        settings.set_javascript_can_access_clipboard(True)
        # Popups opened from async callbacks carry no user gesture
        settings.set_javascript_can_open_windows_automatically(True)
        settings.set_enable_developer_extras(DEV_LOGGING)
        settings.set_enable_smooth_scrolling(True)
        # bfcache off (playbook #28, preventative): a cross-document navigation
        # parks the old document in the back/forward cache with its IndexedDB
        # connection still open, deadlocking the new page's open() — verified
        # on Slack. Web A holds IndexedDB and open_deeplink() navigates
        # cross-document; a single-window wrapper has no back/forward UI, so
        # bfcache buys nothing here.
        settings.set_enable_page_cache(False)
        if DEV_LOGGING:
            settings.set_enable_write_console_messages_to_stdout(True)

        context = self.webview.get_context()
        # Native per-origin notification grant — Notification.permission reads
        # "granted" from web-process start, including in workers, without
        # monkey-patching the API
        context.connect(
            "initialize-notification-permissions",
            lambda ctx: ctx.initialize_notification_permissions(
                [WebKit.SecurityOrigin.new_for_uri(TELEGRAM_URL)], []
            ),
        )
        context.set_spell_checking_enabled(True)
        context.set_spell_checking_languages(
            [l for l in GLib.get_language_names() if "." not in l and l != "C"]
        )

        # -- User scripts --
        content_manager = self.webview.get_user_content_manager()
        paste_js = PASTE_BRIDGE_JS.replace('%DEV%', 'true' if DEV_LOGGING else 'false')
        # TOP_FRAME only: the composer lives in the top frame, and Python's
        # evaluate_javascript replies land in the main frame — an ALL_FRAMES
        # copy inside a Mini-App iframe would intercept pastes it can never
        # complete (wedged latch, wrong-frame injection)
        content_manager.add_script(WebKit.UserScript(
            paste_js,
            WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.START,
            None, None,
        ))
        content_manager.register_script_message_handler("clipboardBridge")
        content_manager.connect(
            "script-message-received::clipboardBridge",
            self._on_paste_requested,
        )
        for source in (BADGE_BRIDGE_JS, COMPAT_SEED_JS):
            content_manager.add_script(WebKit.UserScript(
                source,
                WebKit.UserContentInjectedFrames.TOP_FRAME,
                WebKit.UserScriptInjectionTime.START,
                None, None,
            ))
        content_manager.register_script_message_handler("badgeBridge")
        content_manager.connect(
            "script-message-received::badgeBridge",
            self._on_badge_message,
        )

        # Suppress WebKitGTK's default context menu so Telegram's own menu works
        self.webview.connect("context-menu", lambda *_: True)
        # Allow notification and media permission requests
        self.webview.connect("permission-request", self._on_permission_request)
        # Permissions API must agree with Notification.permission
        self.webview.connect("query-permission-state", self._on_query_permission_state)
        # Forward web notifications to GNOME
        self.webview.connect("show-notification", self._on_show_notification)
        # Open external links in default browser
        self.webview.connect("decide-policy", self._on_decide_policy)
        # Intercept target="_blank" links
        self.webview.connect("create", self._on_create_new_window)
        # Recover from web-process crashes instead of showing a dead view
        self._last_crash_reload = 0.0
        self.webview.connect("web-process-terminated", self._on_web_process_terminated)
        # GNOME 50 no longer auto-clears notifications when the app gains
        # focus — withdraw ours when the user comes back to the window
        self.connect("notify::is-active", self._on_active_changed)

        self.webview.set_vexpand(True)
        self.webview.set_hexpand(True)
        self.webview.set_zoom_level(_load_zoom())
        self.webview.load_uri(TELEGRAM_URL)

        # Zoom keyboard shortcuts (Ctrl+=/+, Ctrl+-, Ctrl+0)
        zoom_controller = Gtk.EventControllerKey()
        zoom_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(zoom_controller)
        # Ctrl+scroll-wheel zoom (GNOME convention)
        scroll_controller = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
        )
        scroll_controller.connect("scroll", self._on_scroll)
        self.add_controller(scroll_controller)

        # -- Test hooks (F11/F12) — only in --test mode --
        if TEST_MODE:
            self._register_test_actions()

        # -- Layout: proper GNOME/libadwaita structure --
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Telegram", subtitle=""))

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self.webview)

        # Hide the header bar during HTML5 fullscreen (video player, media
        # viewer) — otherwise it draws over the fullscreen content
        self.webview.connect(
            "enter-fullscreen",
            lambda *_: (toolbar_view.set_reveal_top_bars(False), False)[1],
        )
        self.webview.connect(
            "leave-fullscreen",
            lambda *_: (toolbar_view.set_reveal_top_bars(True), False)[1],
        )

        self.set_content(toolbar_view)

    # -- Zoom controls --

    def _on_key_pressed(self, _controller, keyval, _keycode, state):
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        if keyval in (Gdk.KEY_equal, Gdk.KEY_plus, Gdk.KEY_KP_Add):
            self._adjust_zoom(ZOOM_STEP)
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self._adjust_zoom(-ZOOM_STEP)
            return True
        if keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
            self.webview.set_zoom_level(DEFAULT_ZOOM)
            _save_zoom(DEFAULT_ZOOM)
            return True
        return False

    def _adjust_zoom(self, delta):
        level = self.webview.get_zoom_level() + delta
        level = max(MIN_ZOOM, min(MAX_ZOOM, level))
        self.webview.set_zoom_level(level)
        _save_zoom(level)

    def _on_scroll(self, controller, _dx, dy):
        state = controller.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            self._adjust_zoom(-ZOOM_STEP if dy > 0 else ZOOM_STEP)
            return True
        return False

    # -- Deep links --

    def open_deeplink(self, url):
        """Navigate to a converted tg:///t.me deep link.

        Telegram Web A processes the tgaddr parameter during client boot, so
        the link needs a real page load: when already inside the app a
        fragment-only load_uri wouldn't reload — force it with reload().
        """
        current = self.webview.get_uri() or ""
        same_page = current.split("#", 1)[0] == url.split("#", 1)[0]
        self.webview.load_uri(url)
        if same_page:
            self.webview.reload()
        self.present()

    # -- Test hooks (F11 = command, F12 = dump state) --

    def _register_test_actions(self):
        sc = Gtk.ShortcutController()
        sc.set_scope(Gtk.ShortcutScope.GLOBAL)

        # F11 — read command from file and execute
        sc.add_shortcut(Gtk.Shortcut(
            trigger=Gtk.ShortcutTrigger.parse_string("F11"),
            action=Gtk.CallbackAction.new(self._on_test_command),
        ))
        # F12 — dump current state to file
        sc.add_shortcut(Gtk.Shortcut(
            trigger=Gtk.ShortcutTrigger.parse_string("F12"),
            action=Gtk.CallbackAction.new(self._on_dump_state),
        ))
        self.add_controller(sc)
        self._devlog("test hooks registered (F11/F12)")

    def _on_test_command(self, _widget=None, _args=None):
        try:
            with open(TEST_INPUT_FILE) as f:
                lines = f.read().strip().splitlines()
        except OSError:
            self._devlog("test-command: no input file")
            return True
        if not lines:
            return True
        cmd = lines[0].strip()
        data = lines[1] if len(lines) > 1 else ""
        self._devlog(f"test-command: {cmd} {data[:80]}")

        if cmd == "navigate":
            self.webview.load_uri(data)
        elif cmd == "navigate-js":
            js = f"window.location.assign({json.dumps(data)});"
            self.webview.evaluate_javascript(js, -1, None, None, None, None, None)
        elif cmd == "deeplink":
            # Simulate receiving a tg:// or t.me deep link
            url = _telegram_uri_to_url(data)
            # Write converted URL to state file so tests can verify
            _write_state_file(f"deeplink_url={url or ''}\n")
            if url:
                self.open_deeplink(url)
        elif cmd == "js":
            self.webview.evaluate_javascript(data, -1, None, None, None, None, None)
        elif cmd == "paste":
            self._on_paste_requested(None, None)
        return True

    def _on_dump_state(self, _widget=None, _args=None):
        title = self.webview.get_title() or ""
        uri = self.webview.get_uri() or ""
        loading = self.webview.is_loading()
        zoom = self.webview.get_zoom_level()
        ua = self.webview.get_settings().get_user_agent()
        dl_dir = _download_dir()
        dl_files = sorted(os.listdir(dl_dir)) if os.path.isdir(dl_dir) else []

        state = (
            f"title={title}\n"
            f"uri={uri}\n"
            f"loading={loading}\n"
            f"zoom={zoom}\n"
            f"ua={ua}\n"
            f"dl_dir={dl_dir}\n"
            f"dl_files={','.join(dl_files)}\n"
        )
        _write_state_file(state)
        self._devlog(f"state dumped to {TEST_STATE_FILE}")
        return True

    # -- Clipboard file bridge --

    @staticmethod
    def _devlog(msg):
        if DEV_LOGGING:
            print(f"[paste] {msg}")

    def _on_paste_requested(self, _content_manager, _message):
        clipboard = self.get_clipboard()
        formats = clipboard.get_formats()
        mime_types = formats.get_mime_types() or []
        self._devlog(f"clipboard mimes: {mime_types}")

        if any(m.startswith("image/") for m in mime_types):
            self._devlog("trying texture read")
            clipboard.read_texture_async(None, self._on_clipboard_texture, None)
        elif "text/uri-list" in mime_types:
            # The GNOME portal silently blocks read_text_async for file
            # clipboards — read the uri-list stream directly
            self._devlog("trying URI list read")
            clipboard.read_async(
                ["text/uri-list"], GLib.PRIORITY_DEFAULT, None,
                self._on_clipboard_uri_stream, None,
            )
        elif "text/plain" in mime_types or "text/plain;charset=utf-8" in mime_types:
            self._devlog("trying text read")
            clipboard.read_text_async(None, self._on_clipboard_text, None)
        else:
            self._devlog("no usable format")
            self._inject_failed()

    def _on_clipboard_uri_stream(self, clipboard, result, _user_data):
        try:
            stream, mime = clipboard.read_finish(result)
            data = stream.read_bytes(65536, None)
            text = data.get_data().decode("utf-8", errors="replace").strip()
            stream.close(None)
            self._process_uri_text(text)
        except Exception:
            self._inject_failed()

    def _process_uri_text(self, text):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            if line.startswith("file://"):
                path = unquote(urlparse(line).path)
            elif line.startswith("/"):
                path = line
            else:
                continue
            if os.path.isfile(path):
                self._inject_file(path)
                return
        self._inject_failed()

    def _on_clipboard_texture(self, clipboard, result, _user_data):
        try:
            texture = clipboard.read_texture_finish(result)
        except GLib.Error as e:
            self._devlog(f"texture failed: {e}, falling back to text")
            clipboard.read_text_async(None, self._on_clipboard_text, None)
            return
        if texture is None:
            self._devlog("texture is None")
            self._inject_failed()
            return
        self._devlog(f"got texture {texture.get_width()}x{texture.get_height()}")
        self._send_texture(texture)

    def _on_clipboard_text(self, clipboard, result, _user_data):
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error as e:
            self._devlog(f"text read failed: {e}")
            self._inject_failed()
            return
        if not text:
            self._devlog("text is empty")
            self._inject_failed()
            return
        self._devlog(f"clipboard text: {text[:200]!r}")
        self._process_uri_text(text)

    # Mime type map for supported file extensions
    MIME_TYPES = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska", ".3gp": "video/3gpp",
        ".pdf": "application/pdf",
        ".doc": "application/msword", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".txt": "text/plain", ".zip": "application/zip", ".rar": "application/x-rar-compressed",
        ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".opus": "audio/opus",
    }

    MAX_PASTE_BYTES = 50 * 1024 * 1024

    def _inject_file(self, path):
        try:
            if os.path.getsize(path) > self.MAX_PASTE_BYTES:
                # The JS side already swallowed the paste — tell the user why
                # nothing appeared
                app = self.get_application()
                if app:
                    notif = Gio.Notification.new("File too large to paste")
                    notif.set_body(
                        f"{os.path.basename(path)} exceeds 50 MB — drag and drop it instead"
                    )
                    app.send_notification(None, notif)
                self._inject_failed()
                return
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self._inject_failed()
            return
        b64 = base64.b64encode(data).decode("ascii")
        ext = os.path.splitext(path)[1].lower()
        mime = self.MIME_TYPES.get(ext, "application/octet-stream")
        filename = os.path.basename(path)
        self._devlog(f"injecting file ({len(data)} bytes, {mime}, {filename})")
        # json.dumps guards against quotes/backslashes in filenames
        js = f"window._injectClipboardFile('{b64}', {json.dumps(mime)}, {json.dumps(filename)});"
        self.webview.evaluate_javascript(js, -1, None, None, None, None, None)

    def _send_texture(self, texture):
        png_bytes = texture.save_to_png_bytes()
        b64 = base64.b64encode(png_bytes.get_data()).decode("ascii")
        self._devlog(f"injecting texture ({len(png_bytes.get_data())} bytes)")
        js = f"window._injectClipboardFile('{b64}', 'image/png', 'clipboard.png');"
        self.webview.evaluate_javascript(js, -1, None, None, None, None, None)

    def _inject_failed(self):
        self.webview.evaluate_javascript(
            "window._injectClipboardImageFailed();",
            -1, None, None, None, None, None,
        )

    # -- Badge --

    def _on_badge_message(self, _content_manager, value):
        """Exact unread count from the page's setAppBadge call (badge shim)."""
        try:
            count = max(0, int(value.to_double()))
        except Exception:
            return
        app = self.get_application()
        if app:
            app.update_badge(count)

    # -- Permissions --

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
        if hasattr(WebKit, "ClipboardPermissionRequest") and isinstance(
            request, WebKit.ClipboardPermissionRequest
        ):
            request.allow()
            return True
        return False

    @staticmethod
    def _on_query_permission_state(_webview, query):
        if query.get_name() == "notifications":
            query.finish(WebKit.PermissionState.GRANTED)
            return True
        # Unhandled queries finish as PROMPT — correct for camera/mic/etc.
        return False

    # -- Notifications --

    def _on_show_notification(self, _webview, notification):
        """Forward web notifications to GNOME desktop notifications.

        Clicking the GNOME notification relays back to WebKit's clicked()
        so Telegram's own onclick handler jumps to the right chat.
        Same-tag notifications replace each other instead of stacking.
        """
        app = self.get_application()
        if not app:
            return False
        gnome_notif = Gio.Notification.new(notification.get_title() or "Telegram")
        body = notification.get_body()
        if body:
            gnome_notif.set_body(body)
        nid = notification.get_id()
        gnome_notif.set_default_action_and_target(
            "app.notification-clicked", GLib.Variant("t", nid)
        )
        app.track_web_notification(notification)
        app.send_notification(app.web_notification_tag(notification), gnome_notif)
        return True

    def _on_active_changed(self, _window, _pspec):
        if self.is_active():
            app = self.get_application()
            if app:
                app.clear_web_notifications()

    def _on_web_process_terminated(self, webview, reason):
        self._devlog(f"web process terminated: {reason}")
        now = time.monotonic()
        if now - self._last_crash_reload > 10:
            self._last_crash_reload = now
            webview.reload()

    # -- Downloads --

    def _on_download_started(self, _session, download):
        self._devlog(f"download started: {download.get_request().get_uri()[:100]}")
        download.connect("decide-destination", self._on_download_decide_dest, _download_dir())
        download.connect("finished", self._on_download_finished)
        download.connect("failed", self._on_download_failed)

    @staticmethod
    def _on_download_decide_dest(download, suggested_filename, dl_dir):
        os.makedirs(dl_dir, exist_ok=True)
        base, ext = os.path.splitext(suggested_filename or "file")
        dest = os.path.join(dl_dir, base + ext)
        n = 2
        while os.path.exists(dest):
            dest = os.path.join(dl_dir, f"{base} ({n}){ext}")
            n += 1
        download.set_destination(dest)
        return True

    def _on_download_finished(self, download):
        # WebKitDownload emits finished after failed too — don't double-notify
        if getattr(download, "_failed", False):
            return
        dest = download.get_destination() or ""
        filename = os.path.basename(dest) if dest else "File"
        self._devlog(f"download finished: {filename}")
        app = self.get_application()
        if app:
            notif = Gio.Notification.new("Download complete")
            notif.set_body(filename)
            # No explicit default action — GNotification falls back to
            # activating the app, which presents the window
            app.send_notification(None, notif)

    def _on_download_failed(self, download, error):
        download._failed = True
        self._devlog(f"download failed: {error}")
        app = self.get_application()
        if app:
            notif = Gio.Notification.new("Download failed")
            notif.set_body(str(error))
            app.send_notification(None, notif)

    # -- New window requests (target="_blank") --

    def _on_create_new_window(self, _webview, nav_action):
        """Handle window.open / target="_blank".

        t.me links route back into the app as deep links. External links go
        to the system browser. In-scope URIs get a real related-view popup:
        it shares our web process and session and keeps window.opener/
        postMessage alive. WebKit loads the URI into the returned view
        itself; calling load_uri here would sever the opener.
        """
        req = nav_action.get_request()
        uri = req.get_uri() if req else None
        if uri:
            deeplink = _telegram_uri_to_url(uri)
            if deeplink:
                self.open_deeplink(deeplink)
                return None
        if uri and uri.startswith(("http://", "https://")):
            host = urlparse(uri).hostname or ""
            if not self._is_allowed_domain(host):
                Gio.AppInfo.launch_default_for_uri(uri, None)
                return None

        popup = WebKit.WebView(related_view=self.webview)
        popup.set_settings(self.webview.get_settings())
        popup.connect("decide-policy", self._on_decide_policy)
        popup.connect("permission-request", self._on_permission_request)
        popup.connect("create", self._on_create_new_window)

        win = Adw.Window(
            transient_for=self,
            default_width=500,
            default_height=650,
            title="Telegram",
        )
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())
        toolbar_view.set_content(popup)
        win.set_content(toolbar_view)

        popup.connect("ready-to-show", lambda *_: win.present())
        popup.connect("close", lambda *_: win.close())
        return popup

    # -- Navigation policy --

    @staticmethod
    def _is_allowed_domain(host):
        """Dot-boundary matching: "telegram.org" matches telegram.org and
        web.telegram.org, never eviltelegram.org."""
        return any(
            host == d or host.endswith("." + d)
            for d in TELEGRAM_DOMAINS
        )

    def _on_decide_policy(self, _webview, decision, decision_type):
        """Keep app flows in-app; hand explicit external link-clicks to the browser.

        Redirect chains and form submissions always stay in-app — only a
        deliberate user click on an out-of-scope link leaves for the system
        browser. t.me and tg:// links become in-app deep links.
        """
        if decision_type == WebKit.PolicyDecisionType.RESPONSE:
            # Cross-origin <a download> is ignored by WebCore, so file links
            # arrive as plain navigations — catch Content-Disposition: attachment
            headers = decision.get_response().get_http_headers()
            is_attachment = False
            if headers:
                ok, disp, _params = headers.get_content_disposition()
                is_attachment = ok and (disp or "").lower() == "attachment"
            if decision.is_main_frame_main_resource() and (
                    not decision.is_mime_type_supported() or is_attachment):
                self._devlog(f"download detected: {decision.get_response().get_uri()[:100]}")
                decision.download()
                return True
            decision.use()
            return True
        if decision_type != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            decision.use()
            return True

        nav = decision.get_navigation_action()
        uri = nav.get_request().get_uri() or ""
        host = urlparse(uri).hostname or ""

        if self._is_allowed_domain(host):
            decision.use()
            return True
        # blob:/data:/about: are internal (media playback, popup bootstrap)
        if uri.startswith(("blob:", "data:", "about:")):
            decision.use()
            return True

        # A deliberate click on a t.me link → in-app deep link, never the
        # t.me landing page. Redirect hops and subframe/JS navigations fall
        # through to the branches below (use() keeps subframe t.me widgets
        # working; a rare redirect onto t.me lands on its harmless preview).
        if (nav.get_navigation_type() == WebKit.NavigationType.LINK_CLICKED
                and nav.is_user_gesture()
                and not nav.is_redirect()):
            deeplink = _telegram_uri_to_url(uri)
            if deeplink:
                decision.ignore()
                self.open_deeplink(deeplink)
                return True

        if uri.startswith(("https://", "http://")):
            # Only a genuine link click leaves the app. Redirect hops keep the
            # original navigation type, so is_redirect() must gate too — else
            # the first hop of a chain started by a click escapes to the
            # browser. FORM_SUBMITTED and OTHER (JS) always stay.
            if (nav.get_navigation_type() == WebKit.NavigationType.LINK_CLICKED
                    and nav.is_user_gesture()
                    and not nav.is_redirect()):
                decision.ignore()
                Gio.AppInfo.launch_default_for_uri(uri, None)
                return True
            decision.use()
            return True

        # Non-web scheme (mailto:, tel:, ...) → system handler. WebKit's
        # default use() would commit an error page over the app.
        decision.ignore()
        scheme = urlparse(uri).scheme
        if scheme and scheme != "file":
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except GLib.Error:
                pass  # no handler registered for this scheme
        return True


class TelegramApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self._dbus_connection = None
        self._badge_count = 0
        self._web_notifications = {}

    def do_startup(self):
        Adw.Application.do_startup(self)
        try:
            self._dbus_connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            # No session bus (e.g. headless test harness) — badge disabled
            self._dbus_connection = None
        action = Gio.SimpleAction.new("notification-clicked", GLib.VariantType.new("t"))
        action.connect("activate", self._on_notification_clicked)
        self.add_action(action)

    # -- Web notification click-through --

    def track_web_notification(self, notification):
        nid = notification.get_id()
        if nid not in self._web_notifications:
            notification.connect("closed", self._on_web_notification_closed)
        self._web_notifications[nid] = notification
        # Bound the map in case a notification never closes
        while len(self._web_notifications) > 100:
            old_id = next(iter(self._web_notifications))
            del self._web_notifications[old_id]

    @staticmethod
    def web_notification_tag(notification):
        return "web-" + (notification.get_tag() or str(notification.get_id()))

    def _on_web_notification_closed(self, notification):
        self.withdraw_notification(self.web_notification_tag(notification))
        self._web_notifications.pop(notification.get_id(), None)

    def clear_web_notifications(self):
        """Withdraw every outstanding web notification (window regained focus).

        close() fires each notification's closed signal, which withdraws the
        GNOME notification and drops it from the map.
        """
        for notification in list(self._web_notifications.values()):
            notification.close()

    def _on_notification_clicked(self, _action, param):
        notification = self._web_notifications.pop(param.get_uint64(), None)
        if notification:
            # Fires Telegram's own onclick → navigates to the chat
            notification.clicked()
        win = self.props.active_window
        if win:
            win.present()

    def update_badge(self, count):
        if count == self._badge_count or not self._dbus_connection:
            return
        self._badge_count = count
        self._dbus_connection.emit_signal(
            None,
            "/com/local/Telegram",
            "com.canonical.Unity.LauncherEntry",
            "Update",
            GLib.Variant("(sa{sv})", (
                "application://com.local.Telegram.desktop",
                {
                    "count": GLib.Variant("x", count),
                    "count-visible": GLib.Variant("b", count > 0),
                },
            )),
        )

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = TelegramWindow(application=self)
        win.present()

    def do_open(self, files, _n_files, _hint):
        """tg:// URIs — GApplication forwards these from second instances."""
        self.activate()
        for f in files:
            url = _telegram_uri_to_url(f.get_uri())
            if url:
                win = self.props.active_window
                if win:
                    win.open_deeplink(url)
                break


def main():
    app = TelegramApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
