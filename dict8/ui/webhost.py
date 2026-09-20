"""The web host: a WKWebView that renders the React UI, and the bridge to Python.

ADR-003: the UI is React + CSS (built from `ui/` into `dict8/ui/web/`), rendered by WebKit
inside this process. Everything that must not grow a process boundary — the CGEventTap, the
CGEvent injection, the TCC checks, the warm STT model — stays in Python exactly where it was.

The bridge is deliberately small, and one-way-per-direction:

    Python -> page    webview.evaluateJavaScript("window.dict8.push({...})")
    page   -> Python  window.webkit.messageHandlers.dict8.postMessage({action: ...})

`push` sends one whole state object, so the page is a pure function of state and Python owns
every string it shows (fmt_tokens, the estimator's wording, the labeled gaps). `level` is a
separate, cheaper call for the overlay's bars, 30 times a second: it never re-renders React,
it only writes a number the bar loop reads.

A page that has not mounted yet cannot be pushed to, so the page posts `{"action": "ready"}`
when React mounts, and the last state is re-sent then. Every push before that is buffered,
never lost.

Assets are read straight off disk and handed to WebKit over Dict8's own URL scheme (see
_AssetHandler): no port is opened and nothing is fetched (invariant 7). `DICT8_UI_URL` points
the same views at Vite's dev server instead, which is how the UI is iterated on.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

import objc
from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep, NSColor
from Foundation import NSURL, NSDate, NSObject, NSRunLoop
from WebKit import (WKUserContentController, WKWebView, WKWebViewConfiguration,
                    WKSnapshotConfiguration)

log = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).resolve().parent / "web"
BRIDGE_NAME = "dict8"          # window.webkit.messageHandlers.dict8
SNAPSHOT_TIMEOUT_S = 2.0


SCHEME = "dict8-ui"            # see _AssetHandler
MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".json": "application/json", ".svg": "image/svg+xml", ".woff2": "font/woff2",
        ".png": "image/png", ".map": "application/json"}


def page_url(page: str) -> NSURL:
    """`overlay` / `window` as a URL: the built assets over Dict8's own scheme, or the dev
    server when DICT8_UI_URL is set
    (`DICT8_UI_URL=http://localhost:5173 uv run --extra app dict8 app`)."""
    dev = os.environ.get("DICT8_UI_URL")
    if dev:
        return NSURL.URLWithString_(f"{dev.rstrip('/')}/{page}.html")
    return NSURL.URLWithString_(f"{SCHEME}://local/{page}.html")


class _AssetHandler(NSObject):
    """Serves the built UI from disk over `dict8-ui://local/…`.

    Not file://, because WebKit gives file:// pages an opaque origin and then refuses to
    load their ES modules — React would never mount. A custom scheme is a real origin, so
    the modules load, while every byte still comes from `dict8/ui/web` on this machine: no
    port is opened and nothing is fetched (invariant 7).
    """

    def webView_startURLSchemeTask_(self, webview, task):
        from Foundation import NSData, NSHTTPURLResponse

        url = task.request().URL()
        rel = (url.path() or "/").lstrip("/") or "overlay.html"
        target = (WEB_ROOT / rel).resolve()
        try:                                  # never serve outside the built UI
            target.relative_to(WEB_ROOT.resolve())
            body = target.read_bytes()
        except Exception:
            log.warning("ui: refused asset request %s", rel)
            response = NSHTTPURLResponse.alloc().initWithURL_statusCode_HTTPVersion_headerFields_(
                url, 404, "HTTP/1.1", {})
            task.didReceiveResponse_(response)
            task.didFinish()
            return
        headers = {"Content-Type": MIME.get(target.suffix, "application/octet-stream"),
                   "Content-Length": str(len(body)),
                   "Cache-Control": "no-store"}
        response = NSHTTPURLResponse.alloc().initWithURL_statusCode_HTTPVersion_headerFields_(
            url, 200, "HTTP/1.1", headers)
        task.didReceiveResponse_(response)
        task.didReceiveData_(NSData.dataWithBytes_length_(body, len(body)))
        task.didFinish()

    def webView_stopURLSchemeTask_(self, webview, task):
        pass


class _Bridge(NSObject):
    """Receives postMessage from the page. One handler per web view."""

    def initWithHandler_(self, handler):
        self = objc.super(_Bridge, self).init()
        if self is None:
            return None
        self.handler = handler
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        body = message.body()
        try:
            payload = json.loads(body) if isinstance(body, str) else dict(body)
        except Exception:
            log.warning("ui: unreadable message from the page: %r", body)
            return
        self.handler(payload)


class WebHost:
    """A WKWebView plus the bridge. Main thread only."""

    def __init__(self, page: str, frame, *, on_action: Callable[[dict], None],
                 transparent: bool = False) -> None:
        self.page = page
        self.on_action = on_action
        self.ready = False
        self._pending: dict | None = None
        self._level: float | None = None

        controller = WKUserContentController.alloc().init()
        self.bridge = _Bridge.alloc().initWithHandler_(self._receive)
        controller.addScriptMessageHandler_name_(self.bridge, BRIDGE_NAME)
        config = WKWebViewConfiguration.alloc().init()
        config.setUserContentController_(controller)
        self.assets = _AssetHandler.alloc().init()
        config.setURLSchemeHandler_forURLScheme_(self.assets, SCHEME)

        self.view = WKWebView.alloc().initWithFrame_configuration_(frame, config)
        self.view.setAutoresizingMask_(1 << 1 | 1 << 4)   # width | height sizable
        if transparent:
            # The overlay draws its own capsule; everything around it must let the desktop
            # through. `drawsBackground` is the only way to turn WebKit's white page off.
            try:
                self.view.setValue_forKey_(False, "drawsBackground")
            except Exception:
                log.warning("ui: could not clear the web view's background")
            try:
                self.view.setUnderPageBackgroundColor_(NSColor.clearColor())
            except Exception:
                pass
        self.view.setValue_forKey_(False, "allowsMagnification")
        from Foundation import NSURLRequest

        self.view.loadRequest_(NSURLRequest.requestWithURL_(page_url(page)))

    # -- bridge ---------------------------------------------------------------------------

    def _receive(self, payload: dict) -> None:
        if payload.get("action") == "ready":
            self.ready = True
            if self._pending is not None:
                self.push(self._pending)
            return
        try:
            self.on_action(payload)
        except Exception as exc:                 # a UI click must never take the app down
            log.error("ui: action %r failed: %r", payload.get("action"), exc)

    def _eval(self, js: str) -> None:
        """Every call into the page goes through here — one seam to watch in tests."""
        self.view.evaluateJavaScript_completionHandler_(js, None)

    def push(self, state: dict) -> None:
        """Send one whole state object. Buffered until the page says it has mounted."""
        self._pending = state
        if not self.ready:
            return
        self._eval(f"window.dict8 && window.dict8.push("
                   f"{json.dumps(state, ensure_ascii=False)})")

    def push_level(self, level: float) -> None:
        """The overlay's bars only: no React render, just a number for the animation loop."""
        if not self.ready:
            return
        self._eval(f"window.dict8 && window.dict8.level({level:.4f})")

    # -- snapshot -------------------------------------------------------------------------

    def snapshot(self, path: str, rect=None) -> bool:
        """Render the view to a PNG. WebKit's snapshot is asynchronous, so the run loop is
        spun until it lands — the tests and `--snapshot` call this synchronously. No screen
        capture is involved, so no Screen Recording grant."""
        done: list = []
        config = WKSnapshotConfiguration.alloc().init()
        if rect is not None:
            config.setRect_(rect)
        try:
            # Wait for the pending frame: without this WebKit can hand back the last
            # rasterised one, which shows the previous state.
            config.setAfterScreenUpdates_(True)
        except AttributeError:
            pass

        def handler(image, error):
            done.append((image, error))

        self.view.takeSnapshotWithConfiguration_completionHandler_(config, handler)
        deadline = NSDate.dateWithTimeIntervalSinceNow_(SNAPSHOT_TIMEOUT_S)
        while not done and NSDate.date().compare_(deadline) < 0:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.01))
        if not done:
            log.warning("ui: snapshot of %s timed out", self.page)
            return False
        image, error = done[0]
        if image is None:
            log.warning("ui: snapshot of %s failed: %r", self.page, error)
            return False
        tiff = image.TIFFRepresentation()
        if tiff is None:
            return False
        rep = NSBitmapImageRep.imageRepWithData_(tiff)
        data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
        return bool(data is not None and data.writeToFile_atomically_(path, True))


def assets_built() -> bool:
    """False when `npm run build` has not run: the app says so instead of showing a blank
    panel (invariant 7b's category — never a silent no-op)."""
    return (WEB_ROOT / "overlay.html").exists() and (WEB_ROOT / "window.html").exists()
