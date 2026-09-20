"""The Dict8 window: what Dict8 is doing, what it still needs, and a place to try it.

Added 2026-09-19 at Denis's request ("I'd like to see the UI"). The interface itself is
React (ui/src/window), rendered by a WKWebView that fills the window (ADR-003); this module
owns the window, the state pushed into the page, and the actions the page sends back.

It is not a settings GUI: `config.yml` stays the settings surface (CLAUDE.md), and nothing
here writes config.

Unlike the overlay, this is an ordinary window that takes focus when clicked. Clicking it
makes Dict8 the frontmost app, which is what the practice box needs: dictating into the box
tries the whole path (mic, STT, typing, chip, estimate) without touching Claude Code.
Dictation types into whatever app is frontmost at press time, so clicking back into Claude
Code sends the next dictation there.

Python owns every string the page shows — `fmt_tokens`, the estimator's wording, the
permission words — so a labeled gap (a TBD, an unset threshold, a refused estimate) reaches
the screen exactly as the layer that knows about it wrote it (invariant 3). The page decides
only where things sit.

The last transcript shown here is the in-memory copy (`Controller.last_text`), and it is
never written anywhere.

Main thread only. `dict8.app` marshals every call through `AppHelper.callAfter`.
"""

from __future__ import annotations

import logging
import re

from AppKit import (NSApplication, NSBackingStoreBuffered, NSMakeRect, NSMenu, NSMenuItem,
                    NSView, NSViewMinYMargin, NSViewWidthSizable, NSWindow,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskFullSizeContentView,
                    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled, NSWindowTitleHidden)

from dict8 import permissions
from dict8.ui import theme, webhost

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 980, 720          # the page is responsive; this is where it opens
MIN_W, MIN_H = 720, 520
TITLE_STRIP = 28                  # keep in step with `.hub { padding-top }` in hub.css

STATUS = {   # tray state -> headline
    "loading": "Loading the speech model…",
    "idle": "Ready",
    "recording": "Recording…",
    "transcribing": "Transcribing…",
    "error": "Needs attention",
    "fallback": "On the clipboard: press ⌘V",
}

PERM_WORDS = {   # grant state -> (words, button title or None)
    permissions.GRANTED: ("Granted", None),
    "not_determined": ("Not asked yet", "Allow…"),
    "denied": ("Not granted", "Open Settings"),
    "restricted": ("Blocked by a profile", "Open Settings"),
    "unknown": ("Could not be read", "Open Settings"),
}

_NUMBER = re.compile(r"^(?P<num>[\d.,]+[KMB]?)\s+tokens?\b\s*(?P<rest>.*)$")


def metric(line: str) -> dict:
    """A meter line as a big number and its caption. A line with no token count in it keeps
    its words and shows a dash — never a made-up zero (invariant 3)."""
    label, _, value = line.partition(": ")
    if not value:
        label, value = "", line
    m = _NUMBER.match(value)
    if m:
        rest = m.group("rest")
        return {"value": m.group("num"),
                "caption": f"{label} {rest}".strip(), "numeric": True}
    caption = f"{label} · {value}" if label else value
    return {"value": "—", "caption": caption, "numeric": False}


class _DragStrip(NSView):
    """The band across the top of the window, and the only thing that drags it.

    The window is `FullSizeContentView` with a transparent title bar, so the WKWebView is
    the entire content view and swallows the mouse events that would otherwise drag the
    window by its title bar. CSS cannot give them back: `-webkit-app-region` is a
    Chromium/Electron extension that WebKit does not implement, prefix or no prefix, so it
    is silently ignored.

    So the drag is done here, natively, by handing the real mouse-down straight to
    `performWindowDragWithEvent:` -- the window server then runs the drag, with snapping
    and Spaces handling, until the button comes up. Measured on this machine, 2026-09-20:
    `mouseDownCanMoveWindow` is *not* an alternative. With the web view in the content
    view AppKit never even calls it (the mouse-down is delivered straight to this view
    instead), and the window does not move, with or without
    `setMovableByWindowBackground_`. Only this path moved the window.

    Passing the event we were given is also what keeps this honest: there is no round trip
    through the page and no `NSApp.currentEvent()` to go stale underneath it.

    It draws nothing, and it costs the page nothing: `.hub` reserves exactly this strip
    with `padding-top`, so there is no page content underneath it. Full width is safe --
    `NSTitlebarContainerView` is a sibling *above* the content view, so the traffic lights
    hit-test first and keep their clicks (verified the same day).
    """

    def mouseDown_(self, event):
        self.window().performWindowDragWithEvent_(event)


class StatusWindow:
    def __init__(self, cfg, handlers: dict, *, stt_model: str, classifier_model: str,
                 mic_label: str) -> None:
        self.handlers = handlers
        talk = str(cfg.require("hotkey.push_to_talk"))
        cancel = str(cfg.require("hotkey.cancel"))
        self.talk_key = theme.key_name(talk)
        self._placed = False
        self._state, self._detail = "loading", ""
        self._missing: list[str] = []

        self.data: dict = {
            "status": {"state": "loading", "headline": STATUS["loading"], "detail": ""},
            "keys": {"talk": theme.key_cap(talk), "cancel": theme.key_cap(cancel)},
            "notice": None,
            "last": None,
            "permissions": [
                {"id": g, "label": permissions.LABELS[g], "state": "checking",
                 "words": "Checking…", "purpose": permissions.PURPOSE[g], "button": None}
                for g in permissions.GRANTS],
            "hostName": permissions.host_name(),
            "models": [
                {"id": "stt", "title": "Speech to text", "model": stt_model,
                 "status": "Loading…", "ok": None},
                {"id": "classifier", "title": "Model picker", "model": classifier_model,
                 "status": "Loading…", "ok": None}],
            "mic": mic_label,
            "usage": {"session": metric("Session: waiting for the first scan…"),
                      "since": metric("Since last dictation: no dictation yet"),
                      "burn": ""},
        }

        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
                 | NSWindowStyleMaskFullSizeContentView)
        w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), style, NSBackingStoreBuffered, False)
        w.setTitle_("Dict8")
        w.setTitleVisibility_(NSWindowTitleHidden)
        w.setTitlebarAppearsTransparent_(True)   # the page draws under it; _DragStrip drags it
        w.setMinSize_((MIN_W, MIN_H))
        w.setReleasedWhenClosed_(False)          # closing hides it; the menu reopens it
        self.win = w

        self.host = webhost.WebHost("window", NSMakeRect(0, 0, WIDTH, HEIGHT),
                                    on_action=self._action)
        # The web view fills the window; the drag strip is added after it, so it sits above
        # it and hit-tests first.
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        content.addSubview_(self.host.view)
        self.drag = _DragStrip.alloc().initWithFrame_(
            NSMakeRect(0, HEIGHT - TITLE_STRIP, WIDTH, TITLE_STRIP))
        self.drag.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        content.addSubview_(self.drag)
        w.setContentView_(content)
        self._push()

    # -- the page's actions ----------------------------------------------------------------

    def _action(self, payload: dict) -> None:
        action = payload.get("action")
        if action == "grant":
            self.handlers["grant"](str(payload.get("grant")))
        elif action == "dismiss_notice":
            self.set_notice(None)
        elif action in ("preview", "copy_last", "check_permissions", "quit"):
            self.handlers[action]()
        else:
            log.debug("window: ignored action %r", payload)

    def _push(self) -> None:
        self.host.push(self.data)

    # -- geometry / visibility -------------------------------------------------------------

    def show(self) -> None:
        if not self._placed:
            self.win.center()
            self._placed = True
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.win.makeKeyAndOrderFront_(None)

    @property
    def visible(self) -> bool:
        return bool(self.win.isVisible())

    def snapshot(self, path: str) -> bool:
        """The window's own pixels to a PNG — WebKit renders it, so no Screen Recording
        grant is involved."""
        return self.host.snapshot(path)

    # -- content ---------------------------------------------------------------------------

    def set_state(self, state: str, detail: str = "") -> None:
        self._state, self._detail = state, detail
        headline = STATUS.get(state, state)
        if state == "idle" and self._missing:   # loaded, but a grant still blocks dictation
            headline = "Almost ready"
            detail = detail or ("Allow " + " and ".join(permissions.LABELS[g]
                                                        for g in self._missing) + " to dictate.")
        self.data["status"] = {"state": state, "headline": headline,
                               "detail": detail[:1].upper() + detail[1:] if detail else ""}
        self._push()

    def set_notice(self, text: str | None) -> None:
        self.data["notice"] = text or None
        self._push()

    def dismiss_notice(self) -> None:
        self.set_notice(None)

    def set_permissions(self, states: dict[str, str]) -> None:
        self._missing = permissions.missing(states)
        rows = []
        for g in permissions.GRANTS:
            state = states.get(g, "unknown")
            words, button = PERM_WORDS.get(state, PERM_WORDS["unknown"])
            rows.append({"id": g, "label": permissions.LABELS[g], "state": state,
                         "words": words, "purpose": permissions.PURPOSE[g],
                         "button": button})
        self.data["permissions"] = rows
        self.set_state(self._state, self._detail)   # pushes

    def set_model(self, kind: str, text: str, ok: bool | None) -> None:
        """ok: True ready, False failed/unavailable, None still loading."""
        for row in self.data["models"]:
            if row["id"] == kind:
                row["status"] = text[:1].upper() + text[1:] if text else text
                row["ok"] = ok
        self._push()

    def set_last(self, summary: str, heard: str | None) -> None:
        advice = (self.data["last"] or {}).get("advice")
        self.data["last"] = {"summary": summary, "heard": heard, "advice": advice}
        self._push()

    def set_advice(self, *, chip: str | None, strength: str | None, estimate: str | None,
                   override: str | None) -> None:
        """Mirrors the overlay: a None hides its line, never a placeholder model."""
        advice = {k: v for k, v in (("chip", chip), ("strength", strength),
                                    ("estimate", estimate), ("override", override)) if v}
        last = self.data["last"] or {"summary": "", "heard": None}
        last["advice"] = advice or None
        self.data["last"] = last if (last["summary"] or last["heard"] or advice) else None
        self._push()

    def set_meter(self, session: str, since: str, burn: str) -> None:
        self.data["usage"] = {"session": metric(session), "since": metric(since),
                              "burn": burn}
        self._push()

    def clear_practice(self) -> None:
        """The page owns the practice box's text; clearing it is a page-side action."""
        self.host._eval("document.querySelector('.practice') && "
                        "(document.querySelector('.practice').value = '')")


def install_edit_menu() -> None:
    """A main menu with Edit, so ⌘V / ⌘C / ⌘A / ⌘Z work in the practice box. Dict8 has no
    Dock icon, so the menu bar never shows it, but AppKit still routes key equivalents
    through it — including into the web view. Without it, the paste fallback could not land
    in the practice box."""
    app = NSApplication.sharedApplication()
    main = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    app_menu = NSMenu.alloc().initWithTitle_("Dict8")
    app_menu.addItemWithTitle_action_keyEquivalent_("Close Window", "performClose:", "w")
    app_item.setSubmenu_(app_menu)
    main.addItem_(app_item)
    edit_item = NSMenuItem.alloc().init()
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, sel, key in (("Undo", "undo:", "z"), ("Cut", "cut:", "x"),
                            ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
                            ("Select All", "selectAll:", "a")):
        edit.addItemWithTitle_action_keyEquivalent_(title, sel, key)
    edit_item.setSubmenu_(edit)
    main.addItem_(edit_item)
    app.setMainMenu_(main)
