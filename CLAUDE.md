# Telegram Desktop — GTK4/WebKitGTK Native GNOME Wrapper

> Guardrails: [umami.md](https://github.com/goodcol-dennis/umami/blob/main/umami.md) — Tier 1 (Foundation)
>
> Shared techniques live in the [WebKitGTK SPA Wrapper Playbook](../webkit-wrapper-playbook.md)
> — consult it before changing navigation policy, notifications, downloads, or
> the paste bridge. slack.py is the family reference implementation.

## Versions & Environment

| Component | Version |
|-----------|---------|
| OS | Ubuntu 26.04 |
| Desktop | GNOME (Wayland) |
| Python | 3 (system) |
| GTK | 4.0 (`gi.require_version("Gtk", "4.0")`) |
| libadwaita | 1 (`gi.require_version("Adw", "1")`) |
| WebKitGTK | 6.0 / 2.52.3 (`gi.require_version("WebKit", "6.0")`) |
| GPU | Intel Arc (Arrow Lake-P) |

System packages required: `gir1.2-webkit-6.0`, GTK4, libadwaita, Python 3.

## Common Commands

```bash
./telegram.py          # Run the app
./telegram.py --dev    # Run with WebKit inspector + [paste] logging to stdout
./telegram.py --test   # Test mode: F11/F12 hooks, separate profile + app id
./install.sh           # Kill running instance, install icon + desktop entry, relaunch
cd tests/narkina-e2e && cargo test   # Headless E2E through narkina
```

**Agents:** neither `./telegram.py` nor `./install.sh` may be run on the user's
live session — see Critical Rule 8. Test through the e2e crate above, or drive
[narkina](../narkina/) directly: `Session::builder(".../telegram.py").arg("--test")`,
`.env("TELEGRAM_TEST_DIR", ...)`, `.stderr_to_file()`; then F11 executes a
command file, F12 dumps `key=value` state. `cage`, `wtype`, `wlrctl` are
installed.

## Project Structure

```
telegram/
├── CLAUDE.md                  # This file — project instructions
├── telegram.py                # Single-file app (all logic here)
├── telegram.svg               # App icon (blue rounded square, white paper plane)
├── com.local.Telegram.desktop # GNOME desktop entry (name MUST match app id)
├── install.sh                 # Installs icon + .desktop, restarts the app
├── tests/narkina-e2e/         # Headless E2E (Rust, drives the app via narkina)
└── .gitignore
```

Untracked and intentionally ignored: `.claude/`, `.mcp.json`,
`tests/narkina-e2e/target/`, `SWEEP_NOTES.md` (local research notes —
the telegram-tt source-verified corpus sweep backing these rules).

## Architecture

- Python 3 + GTK4 + libadwaita + WebKitGTK 6.0 (no Electron)
- Single-file app: `telegram.py` (family convention — slack.py is ~1000 lines
  single-file too; rule 2's 400-line clause permits splitting but parity wins)
- `Adw.ApplicationWindow` with `Adw.ToolbarView` + `Adw.HeaderBar`
- WebKitGTK `NetworkSession.new(data_directory=..., cache_directory=...)`,
  SQLite cookie jar, cookie policy `ALWAYS`, ITP disabled
- Target URL: `https://web.telegram.org/a/` (Telegram Web A)
- App ID: `com.local.Telegram` (`com.local.Telegram.Test` in `--test` mode)
- Data directory: `~/.local/share/telegram-web/` (`telegram-web-test/` in test mode)
- Config: `~/.local/share/telegram-web/config.json` — `zoom` (persisted by the
  zoom shortcuts), `user_agent` (escape-hatch override; unset by default), and
  `enable_service_workers` (default false — see Media formats; set true only
  to re-test WebKit bug 239925 after an engine upgrade)
- User agent: **stock WebKitGTK UA — never spoof Chrome.** telegram-tt's
  `IS_SAFARI` branch is its first-class tested path on WebKit; a Chrome UA
  enables Chromium-only code (round-video recording, wave transform, snap
  effect) that is broken on this engine and hides the quick-upload drop
  target. No server-side UA wall exists — only client-side feature detection
  (`compatTest.js`), whose escape hatch we pre-seed
  (`localStorage['tt-ignore-compat']='1'`).

## Critical Rules

1. **No Electron** — GTK4/WebKitGTK only. No extra menus or chrome beyond the GNOME header bar.
2. **Single-file app** — All logic lives in `telegram.py`. Splitting is permitted past 400 lines but the family convention is single-file; keep parity with slack.py.
3. **Persistent login** — Cookies + IndexedDB in `~/.local/share/telegram-web/`. Login must survive restarts. Cookie policy `ALWAYS`, ITP off (`set_itp_enabled(False)`) or auth tokens get purged.
4. **External links in browser** — but only deliberate user clicks leave the app (gesture-gated policy, playbook §1). Redirects/form-submissions/JS navigations stay in-app. Dot-boundary domain matching only. t.me/tg:// links become in-app deep links, never external.
5. **No unconditional debug logging** — `console.log`, `print()`, `set_enable_developer_extras()` and console-to-stdout are permitted only behind `DEV_LOGGING` (`--dev`/`--test`). Nothing may log on a default launch.
6. **Never monkey-patch browser APIs** — Overriding `Notification`, `AudioContext`, `URL.createObjectURL`, or similar in user scripts breaks the app in opaque ways. Native WebKit APIs exist for every case (e.g. `initialize_notification_permissions`). Defining a *missing* API is sanctioned polyfilling — the `navigator.setAppBadge` badge shim exists precisely because WebKitGTK lacks it.
7. **No `WEBKIT_DISABLE_DMABUF_RENDERER` / `GSK_RENDERER`** — Causes severe keyboard/rendering lag on Intel Arc. Shader warnings are cosmetic; ignore them. The one sanctioned knob is `WEBKIT_DMABUF_RENDERER_DISABLE_GBM=1` (set before `import gi`, playbook #29) — it fixes the Arrow Lake-P diagonal-shearing bug while keeping the zero-copy dmabuf path; `TELEGRAM_FORCE_DMABUF=1` bypasses it for re-testing.
8. **Headless testing only — never launch on the live session** — Any run of the app for testing goes through [narkina](../narkina/) (the `tests/narkina-e2e` crate is the ready-made path). Do **not** run `./telegram.py` or `./install.sh` on the user's real compositor — `install.sh` relaunches the app by design. This applies to subagents too: state the constraint explicitly in their prompts.
9. **App id ↔ desktop filename coupling** — The desktop entry must be named `com.local.Telegram.desktop` or GNOME Shell rejects every `Gio.Notification` (verified hard failure, playbook §2). The badge's `application://com.local.Telegram.desktop` string and install.sh must change together with it.
10. **Filter WebKitGTK context menu, don't blanket-suppress** — `_on_context_menu` strips the menu to spelling entries only (playbook #32): no spelling entries → suppress (`True`) so Telegram's own right-click menu works; over a misspelling → show just the suggestions, which are the only correction UI spell checking has.

## Change Propagation Map

| Change type | Files touched (in order) |
|-------------|--------------------------|
| App behavior / features | `telegram.py` → `tests/narkina-e2e` (extend if testable) → `CLAUDE.md` Implementation Notes (always, if the mechanism changed) → playbook (if family-wide) |
| Icon change | `telegram.svg` → `./install.sh` (re-install) |
| Desktop entry metadata | `com.local.Telegram.desktop` → `./install.sh` (re-install) — never rename without Rule 9 |
| New system dependency | Verify installed → `CLAUDE.md` versions table |

## Implementation Notes

### Clipboard File Paste (implemented)
WebKitGTK does NOT expose clipboard images to the web Clipboard API. The bridge:
1. JS intercepts `paste` events (capture phase, **TOP_FRAME only** — Python's
   `evaluate_javascript` replies land in the main frame, so an ALL_FRAMES
   copy inside a Mini-App iframe would intercept pastes it can never
   complete); intercepts only when every non-empty line of the clipboard text
   looks like a file path (no extension whitelist — Python validates with
   `isfile()`). Stashes the text so a failed round-trip can restore it via
   `execCommand('insertText')`. An `_injecting` latch hard-guards against
   synthetic-paste re-entry independent of clipboardData support.
2. Python reads the GTK clipboard — `image/*` textures first; `text/uri-list`
   via `read_async` (the GNOME portal **silently blocks** `read_text_async`
   for file clipboards); `text/plain` last.
3. Python base64-encodes (50 MB cap → notification), maps extension →
   MIME (`MIME_TYPES`), calls `window._injectClipboardFile(b64, mime, name)` —
   mime/filename pass through `json.dumps` (script-injection guard).
4. JS builds a `File` and dispatches a **synthetic `ClipboardEvent('paste')`
   built with a constructor-init `DataTransfer`** at the active element —
   Telegram Web A's own paste handler consumes it (`defaultPrevented`).
   Constructor init is the one way to attach files: *assigning*
   `clipboardData` after construction is read-only in WebKit.
5. Fallbacks in order: synthetic drop on the composer
   (`#editable-message-text` → `[contenteditable]` → `#MiddleColumn`), gated
   on `dropEvent.defaultPrevented`; then fill the last `<input type="file">`
   via `DataTransfer` + synthetic `change`.

### Notifications (implemented)
1. `show-notification` fires because WebKitGTK lacks `PushManager` →
   telegram-tt's `checkIfPushSupported()` is false → it uses page-created
   `new Notification()` (source-verified). If a future WebKitGTK adds
   PushManager, the app silently switches to service-worker notifications
   and `show-notification` stops firing — that's the first thing to check if
   notifications ever die after a WebKit upgrade.
2. Native permission: `initialize-notification-permissions` signal →
   `initialize_notification_permissions([web.telegram.org origin], [])`. No JS
   shim (rule 6). `permission-request` auto-grant kept as belt-and-braces
   (telegram-tt requests lazily after the first click/keypress).
3. `query-permission-state` → GRANTED for `"notifications"` (family parity;
   telegram-tt itself only reads static `Notification.permission`).
4. `show-notification` → `Gio.Notification` with
   `set_default_action_and_target("app.notification-clicked", id)`; the action
   calls the stored `WebKitNotification.clicked()` (Telegram's own onclick
   focuses the message) and presents the window. `closed` signal → withdraw.
   Tags are per-message (`String(message.id)`; calls `call_<id>`), so
   notifications stack rather than replace; map bounded at 100.
5. Focus-withdraw: `notify::is-active` → `close()` every retained
   notification — Web A never closes its own on focus (it only resets
   title/favicon), and GNOME 50 no longer auto-clears either.
6. Notification sound is page-side (`new Audio('./notification.mp3')` in
   `onshow`) — it depends on the audio/autoplay settings, not on us.
7. **Never** `set_default_action("app.activate")` — the action doesn't exist.

### Downloads (implemented)
1. `download-started` on **NetworkSession** (not WebContext).
2. RESPONSE policy: `decision.download()` when main-frame main-resource and
   (unsupported MIME or `Content-Disposition: attachment`) — cross-origin
   `<a download>` is ignored by WebCore so attachments arrive as navigations.
   Telegram media saves are same-origin `blob:` + `<a download>`, which fire
   `download-started` directly.
3. `decide-destination`: XDG download dir (**None fallback** → `~/Downloads`),
   `makedirs`, dedupe to `name (2).ext`; `set_destination` takes a plain path,
   not `file://`.
4. `finished` fires after `failed` too — the failed handler marks the download
   to prevent double notification.

### Audio / Media (implemented)
1. `WebKit.WebsitePolicies(autoplay=ALLOW)` **passed to the WebView constructor**
2. `enable_webaudio`, `enable_encrypted_media`, `enable_mediasource`, `enable_media_stream`
3. `enable_webrtc` — RTCPeerConnection is off by default; calls need it
4. Non-navigation policy decisions → `decision.use()`; `blob:`/`data:` allowed

### Navigation Policy (implemented — playbook §1)
1. RESPONSE → download detection (above) else `use()`.
2. Other non-NAVIGATION_ACTION → `use()`.
3. Allowed domains (`telegram.org`, dot-boundary) → `use()`.
4. `blob:`/`data:`/`about:` → `use()`.
5. t.me/telegram.me/telegram.dog/tg:// → `ignore()` + in-app deep link (below).
6. http(s) out of scope: only `LINK_CLICKED` + `is_user_gesture()` +
   `not is_redirect()` leaves for the system browser; everything else stays.
7. Non-web schemes (`mailto:`, `tel:`) → `ignore()` + `launch_default_for_uri`
   wrapped in try/except (no handler registered is not a crash).
8. `create` (target=_blank): t.me/tg:// → deep link; out-of-scope → browser;
   in-scope → **related-view popup** sharing the web process/session
   (`window.opener` stays alive).

### Deep Links (implemented)
1. `Gio.ApplicationFlags.HANDLES_OPEN` + `do_open()` — GApplication forwards
   `tg://` URIs from second instances over DBus.
2. Desktop entry: `Exec=… %u` + `MimeType=x-scheme-handler/tg;`; install.sh
   runs `xdg-mime default com.local.Telegram.desktop x-scheme-handler/tg`.
3. `_telegram_uri_to_url()` wraps tg:// and t.me URLs (incl. the
   `username.t.me` subdomain form) as
   `https://web.telegram.org/a/#?tgaddr=<urlencoded>` — Telegram Web A routes
   tgaddr through its own deep-link processor, which accepts both forms.
4. `open_deeplink()` forces a real page load (tgaddr is processed at client
   boot): fragment-only `load_uri` doesn't reload, so it follows up with
   `reload()` when already in-app.

### Zoom (implemented)
Ctrl+=/− /0 and Ctrl+scroll. Persisted to config.json. Default 1.0, range 0.5–3.0.

### Drag-and-Drop / File Upload (native — do not touch)
- Drops into the page are standard HTML5 DnD delivered by WebKitGTK's own
  GTK integration. **Never install a `Gtk.DropTarget` on the WebView** — it
  would starve Web A's overlay logic (`MiddleColumn` dragenter →
  code-split `DropArea` portal; body-level guard preventDefaults everything
  outside `data-dropzone`). Under the stock Safari-shaped UA both drop
  targets (quick/photo + document) render.
- File upload is a dynamically created hidden `<input type="file">`
  (`openSystemFilesDialog`) → WebKitGTK's native GTK file chooser. No
  `showOpenFilePicker`/`showSaveFilePicker` anywhere in telegram-tt.

### Media formats (system-side, verified present)
- Animated stickers: Lottie via rlottie-WASM — no codecs needed.
- Video stickers: WebM VP9 (`vp9dec`/`vavp9dec`); voice: OGG Opus
  (`opusdec`; JS WASM fallback exists); video/GIFs: H.264
  (`avdec_h264`/`vah264dec`). All probed "probably" via canPlayType on this
  engine (headless probe, 2026-07-31).

### Service workers OFF (implemented — the GIF/video fix)
- WebKit bug 239925: WebKitGTK fails `FetchEvent.respondWith` streaming on
  Web A's service-worker `/progressive/` URLs — every *received* inline
  video/GIF (and music file) hits MEDIA_ERR and telegram-tt toasts
  "Video.Unsupported.Desktop". Own just-sent media plays (local blob), which
  is the telltale split confirming codecs are fine.
- Fix: `_set_webkit_feature(settings, "ServiceWorkers", False)` (default).
  telegram-tt then sees `IS_PROGRESSIVE_SUPPORTED == false` and serves all
  media as blob URLs by design. Verified headlessly: with the feature off,
  `'serviceWorker' in navigator` is false.
- Trade-offs: no offline asset cache (slower cold start); no >2GB downloads
  (the SW `/download/` streamer was the only path — OPFS is absent in this
  WebKit too). Notifications unaffected (page-created, no PushManager).
- `config.json {"enable_service_workers": true}` restores SWs to re-test
  after a WebKit upgrade; if 239925 is ever fixed, flip the default back.

### Robustness (implemented)
- bfcache OFF (`set_enable_page_cache(False)`, playbook #28): cross-document
  navigation + open IndexedDB deadlocks the new page's `open()` (verified on
  Slack; preventative here — `open_deeplink()` navigates cross-document).
  Smoking gun if it ever regresses: console line "WebSocket is closed due to
  suspension."
- `WEBKIT_DMABUF_RENDERER_DISABLE_GBM=1` before `import gi` (playbook #29):
  fixes Arrow Lake-P dmabuf shearing with no latency cost (rule 7).
- `web-process-terminated` → `reload()`, rate-limited to one per 10 s.
- `Gio.bus_get_sync` wrapped in try/except — headless sessions have no bus;
  badge degrades instead of crashing at startup.
- `hasattr(WebKit, "ClipboardPermissionRequest")` guard before isinstance.
- Spell checking on, languages from `GLib.get_language_names()` filtered
  (entries containing `.` and `C` match no hunspell dictionary). Corrections
  come from the context-menu spelling filter (rule 10) — squiggles without it
  are a half-feature.
- Fullscreen (video player/media viewer) hides the header bar via
  `set_reveal_top_bars`.
- Compat-gate insurance: `localStorage['tt-ignore-compat']='1'` pre-seeded
  (document-start user script) so a future `compatTest.js` probe addition
  degrades to a console warning instead of a dead app.
- Known engine bug, not ours (playbook §4, open in WebKitGTK 2.52.3): the
  network process can segfault under media load. Fingerprint is a *cluster*:
  console "Network process crashed", Cache API / IndexedDB internal errors,
  truncated downloads, **spontaneous logout** (auth lives in IndexedDB, which
  that process hosts). If a logout report matches the cluster, restart the
  app before suspecting ITP/cookies or re-authenticating — on-disk state
  survives. Fixed only by the pending webkit2gtk security update.

### Dock Badge (implemented)
1. **`navigator.setAppBadge` shim** (document-start user script) →
   `badgeBridge` script message → `update_badge()`. This is telegram-tt's
   intended app-badge channel: `<UnreadCounter isForAppBadge />` calls it
   with the exact All-folder unmuted-unread count on every change, and
   `updateAppBadge(0)` on sign-out. WebKitGTK lacks the API, so the shim
   collides with nothing.
2. **Do NOT parse the title.** Web A's title is never "Telegram (N)" — while
   focused it defaults to the *current chat's name* (`canDisplayChatInTitle`),
   and while blurred it *blinks* "N notifications" counting only
   new-since-blur. Any title regex is folklore from stale wrappers.
3. `com.canonical.Unity.LauncherEntry` `Update` signal on `/com/local/Telegram`
4. `application://com.local.Telegram.desktop`, `count` as int64 variant `"x"`
5. Guarded on count-changed; disabled when no session bus

### Test hooks (implemented — `--test` mode only)
- App id `com.local.Telegram.Test`, profile `telegram-web-test/` (concurrent
  WebKit sessions on one data dir corrupt cookies.sqlite/IndexedDB)
- F11 executes `$TELEGRAM_TEST_DIR/telegram-test-input.txt` (`navigate`,
  `navigate-js`, `js`, `paste`, `deeplink`); F12 dumps state to
  `telegram-test-state.txt`
- `tests/narkina-e2e/` runs the full scenario headlessly (launch, UA, deep
  links, zoom, paste failure branch, popup) — `cargo test` in that dir

## Pre-Commit Checklist

- [ ] `python3 -m py_compile telegram.py` clean
- [ ] `cd tests/narkina-e2e && cargo test` passes (headless — safe anywhere)
- [ ] No unrelated files modified (scope discipline)
- [ ] Changes match what was requested — nothing more, nothing less
- [ ] No unconditional debug logging (everything behind `DEV_LOGGING`)
- [ ] CLAUDE.md Implementation Notes match the code if a mechanism changed
- [ ] Git status reviewed — no unintended files staged
