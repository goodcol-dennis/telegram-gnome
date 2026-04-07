# Image Paste Bridge for WebKitGTK Apps

## Problem
WebKitGTK (even 2.52.0) doesn't translate GTK clipboard image data into the web Clipboard API's `image/png` format. When users copy an image (e.g., screenshot, file manager), the clipboard contains file URIs or `application/vnd.portal.files` — not raw image data the web page can access.

## What doesn't work
- **`ClipboardEvent` with `clipboardData`** — `clipboardData` is read-only in WebKit's `ClipboardEvent` constructor. You cannot set files on it.
- **Synthetic `DragEvent`** — `dataTransfer.files` is also read-only when constructed from JavaScript. The drop target never sees the files.
- **`read_texture_async`** — On GNOME/portal, copied files appear as `application/vnd.portal.files` + `text/uri-list` + `text/plain`, NOT as `image/*`. Texture read fails.

## What works: `<input type="file">` + `DataTransfer`
`DataTransfer.items.add(file)` works, and you can assign `dataTransfer.files` to an `<input type="file">.files`. Then fire a `change` event. This is how web apps (WhatsApp, Telegram) actually receive files.

## Implementation

### Python side

#### 1. Detect clipboard content
```python
def _on_paste_requested(self, _content_manager, message):
    clipboard = self.get_clipboard()
    formats = clipboard.get_formats()
    mime_types = formats.get_mime_types() or []

    if any(m.startswith("image/") for m in mime_types):
        clipboard.read_texture_async(None, self._on_clipboard_texture, None)
    elif "text/plain" in mime_types or "text/uri-list" in mime_types:
        clipboard.read_text_async(None, self._on_clipboard_text, None)
    else:
        self._inject_failed()
```

#### 2. Handle file path from clipboard text
The clipboard typically contains a plain text file path like `/home/user/Pictures/image.png` or a `file://` URI. Read the actual file bytes and base64-encode them:
```python
def _on_clipboard_text(self, clipboard, result, _user_data):
    text = clipboard.read_text_finish(result)
    # Parse file:// URIs or plain paths
    # Read the file, base64-encode, detect mime from extension
    # Call: self.webview.evaluate_javascript(f"window._injectClipboardImage('{b64}', '{mime}');", ...)
```

#### 3. Texture fallback (rare, but handles raw image clipboard)
```python
def _on_clipboard_texture(self, clipboard, result, _user_data):
    texture = clipboard.read_texture_finish(result)
    png_bytes = texture.save_to_png_bytes()
    b64 = base64.b64encode(png_bytes.get_data()).decode("ascii")
    # inject via JS
```

### JavaScript side

#### Key insight
You CANNOT construct paste or drop events with files from JS in WebKit. Instead, set `.files` on an `<input type="file">` element using `DataTransfer`, then dispatch a `change` event.

```javascript
// Intercept paste, ask Python for image data
document.addEventListener('paste', function(e) {
    if (e.clipboardData && e.clipboardData.files.length > 0) return;
    // Suppress file-path text from being pasted
    const text = (e.clipboardData && e.clipboardData.getData('text/plain')) || '';
    if (/\.(png|jpe?g|gif|webp|bmp)$/im.test(text.trim())) e.preventDefault();
    window.webkit.messageHandlers.clipboardBridge.postMessage('paste');
}, true);

// Called from Python with base64 image data
window._injectClipboardImage = function(b64, mime) {
    const arr = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    const file = new File([arr], 'clipboard.png', {type: mime});
    const dt = new DataTransfer();
    dt.items.add(file);

    // Find existing file input and set its files
    const inputs = document.querySelectorAll('input[type="file"]');
    if (inputs.length > 0) {
        const input = inputs[inputs.length - 1];
        input.files = dt.files;
        input.dispatchEvent(new Event('change', {bubbles: true}));
        return;
    }

    // If no file input exists, use MutationObserver + click attach button
    // to trigger one, then fill it when it appears
};
```

### WebKit settings required
```python
settings.set_javascript_can_access_clipboard(True)
```

### Clipboard MIME types you'll see on GNOME (portal)
When a user copies a file from Nautilus or takes a screenshot:
```
['application/vnd.portal.files', 'application/vnd.portal.filetransfer',
 'text/uri-list', 'text/plain;charset=utf-8']
```
There is NO `image/*` type — you must read the text, parse the path, and read the file yourself.

## App-specific notes

### Telegram Web
Telegram Web's attach button and file input selectors will differ from WhatsApp. Inspect the DOM to find:
- The attach/paperclip button selector
- The `<input type="file">` that appears when attaching
- Use the same `DataTransfer` → `input.files` → `change` event pattern
