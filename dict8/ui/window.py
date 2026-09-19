"""The Dict8 window: what Dict8 is doing, what it still needs, and a place to try it.

Added 2026-09-19 at Denis's request ("I'd like to see the UI"): one plain window beside the
tray item, the overlay and the toasts. It is not a settings GUI. `config.yml` stays the
settings surface (CLAUDE.md), and nothing here writes config.

Unlike the overlay, this is an ordinary window that takes focus when clicked. Clicking it
makes Dict8 the frontmost app, which is what the practice box needs: dictating into the box
tries the whole path (mic, STT, typing, chip, estimate) without touching Claude Code.
Dictation types into whatever app is frontmost at press time, so clicking back into Claude
Code sends the next dictation there.

Sections, top to bottom:
- status and the talk key;
- permissions, one row per grant, with the button that fixes it;
- the two local models;
- the last dictation and the overlay's advice;
- the usage meter;
- the practice box.

The last transcript shown here is the in-memory copy (`Controller.last_text`), and it is
never written anywhere.

Main thread only. `dict8.app` marshals every call through `AppHelper.callAfter`.
"""

from __future__ import annotations

import objc
from AppKit import (NSApplication, NSBackingStoreBuffered, NSBezelBorder,
                    NSBitmapImageFileTypePNG, NSBox, NSBoxSeparator, NSButton, NSColor, NSFont,
                    NSLayoutAttributeCenterY, NSLayoutAttributeLeading, NSMakeRect, NSMenu,
                    NSMenuItem, NSScrollView, NSStackView, NSTextField, NSTextView,
                    NSUserInterfaceLayoutOrientationHorizontal,
                    NSUserInterfaceLayoutOrientationVertical, NSViewWidthSizable, NSWindow,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskTitled)
from Foundation import NSObject

from dict8 import permissions

# Layout, in points. Presentation, not thresholds.
WIDTH = 520
INSET = 20
PRACTICE_H = 72

# How config's key names read on screen. Config names the key; this only spells it.
KEY_NAMES = {
    "right_option": "the right ⌥ Option key",
    "left_option": "the left ⌥ Option key",
    "right_command": "the right ⌘ Command key",
    "right_control": "the right ⌃ Control key",
    "right_shift": "the right ⇧ Shift key",
    "escape": "Esc",
}

STATUS = {   # tray state -> (headline, color name)
    "loading": ("Loading the speech model…", "systemOrangeColor"),
    "idle": ("Ready", "systemGreenColor"),
    "recording": ("Recording…", "systemRedColor"),
    "transcribing": ("Transcribing…", "systemBlueColor"),
    "error": ("Needs attention", "systemOrangeColor"),
    "fallback": ("On the clipboard: press ⌘V", "systemOrangeColor"),
}

PERM_STATES = {  # grant state -> (mark, words, color name, button title or None)
    permissions.GRANTED: ("✓", "granted", "systemGreenColor", None),
    "not_determined": ("!", "not asked yet", "systemOrangeColor", "Allow…"),
    "denied": ("✕", "not granted", "systemRedColor", "Open Settings"),
    "restricted": ("✕", "blocked by a profile", "systemRedColor", "Open Settings"),
    "unknown": ("?", "could not be read", "systemOrangeColor", "Open Settings"),
}


def _color(name: str):
    return getattr(NSColor, name)()


def _label(text: str = "", *, size: float = 13, bold: bool = False, dim: bool = False,
           wrap: bool = False) -> NSTextField:
    f = (NSTextField.wrappingLabelWithString_(text) if wrap
         else NSTextField.labelWithString_(text))
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    f.setTextColor_(NSColor.secondaryLabelColor() if dim else NSColor.labelColor())
    f.setSelectable_(wrap)
    if wrap:
        f.setPreferredMaxLayoutWidth_(WIDTH)
    return f


def _row(*views, spacing: float = 8) -> NSStackView:
    r = NSStackView.stackViewWithViews_(list(views))
    r.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal)
    r.setAlignment_(NSLayoutAttributeCenterY)
    r.setSpacing_(spacing)
    return r


def _separator() -> NSBox:
    b = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 1))
    b.setBoxType_(NSBoxSeparator)
    b.widthAnchor().constraintEqualToConstant_(WIDTH).setActive_(True)
    return b


class _Target(NSObject):
    """ObjC target for the window's buttons; forwards to plain Python callables."""

    def initWithHandlers_(self, handlers):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self.handlers = handlers
        return self

    def grant_(self, sender):
        self.handlers["grant"](permissions.GRANTS[int(sender.tag())])

    def preview_(self, sender):
        self.handlers["preview"]()

    def copyLast_(self, sender):
        self.handlers["copy_last"]()

    def clearPractice_(self, sender):
        self.handlers["clear_practice"]()

    def quit_(self, sender):
        self.handlers["quit"]()


class StatusWindow:
    def __init__(self, cfg, handlers: dict, *, stt_model: str, classifier_model: str,
                 mic_label: str) -> None:
        handlers = {"clear_practice": self.clear_practice, **handlers}
        self.target = _Target.alloc().initWithHandlers_(handlers)
        talk = str(cfg.require("hotkey.push_to_talk"))
        cancel = str(cfg.require("hotkey.cancel"))
        self.talk_key = KEY_NAMES.get(talk, talk)
        cancel_key = KEY_NAMES.get(cancel, cancel)

        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable)
        w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH + 2 * INSET, 600), style, NSBackingStoreBuffered, False)
        w.setTitle_("Dict8")
        w.setReleasedWhenClosed_(False)   # closing hides it; the menu reopens it
        self.win = w
        self._placed = False
        self._state, self._detail = "loading", ""
        self._missing: list[str] = []

        views: list = []

        def add(*vs):
            views.extend(vs)

        # -- status ------------------------------------------------------------------
        self.status_dot = _label("●", size=15)
        self.status = _label("", size=15, bold=True)
        self.status_detail = _label("", dim=True)
        add(_row(self.status_dot, self.status, self.status_detail, spacing=6))
        add(_label(f"Hold {self.talk_key}, speak, then let go: the text is typed where your "
                   f"cursor is. {cancel_key} while holding cancels.", wrap=True))
        self.notice = _label("Last message: none", dim=True, wrap=True)
        add(self.notice, _separator())

        # -- permissions -------------------------------------------------------------
        add(_label("Permissions", bold=True),
            _label(f"While Dict8 runs from a terminal, macOS lists these under "
                   f"{permissions.host_name()}, not Dict8.", dim=True, wrap=True))
        self.perm_rows: dict[str, tuple] = {}
        for i, g in enumerate(permissions.GRANTS):
            mark = _label("?", bold=True)
            name = _label(permissions.LABELS[g], bold=True)
            state = _label("", dim=True)
            btn = NSButton.buttonWithTitle_target_action_("Allow…", self.target, "grant:")
            btn.setTag_(i)
            add(_row(mark, name, state, btn))
            add(_label(f"    Needed {permissions.PURPOSE[g]}.", dim=True, size=12))
            self.perm_rows[g] = (mark, state, btn)
        add(_separator())

        # -- models ------------------------------------------------------------------
        add(_label("Models (run on this Mac, nothing leaves it)", bold=True))
        self.models = {
            "stt": (_label(f"Speech to text: {stt_model}", size=12), _label("loading…", dim=True, size=12)),
            "classifier": (_label(f"Model picker: {classifier_model}", size=12),
                           _label("loading…", dim=True, size=12)),
        }
        for a, b in self.models.values():
            add(_row(a, b, spacing=6))
        add(_label(f"Microphone: {mic_label}", dim=True, size=12), _separator())

        # -- last dictation ----------------------------------------------------------
        add(_label("Last dictation", bold=True))
        self.last = _label("None yet.", dim=True, wrap=True)
        self.heard = _label("", wrap=True)
        self.chip = _label("", bold=True)
        self.chip.setTextColor_(NSColor.systemBlueColor())
        self.strength = _label("", dim=True, size=12)
        self.estimate = _label("", size=12)
        self.override = _label("", size=12)
        self.override.setTextColor_(NSColor.systemOrangeColor())
        self.advice_row = _row(self.chip, self.strength)
        add(self.last, self.heard, self.advice_row, self.estimate, self.override, _separator())

        # -- usage -------------------------------------------------------------------
        add(_label("Usage (tokens, not dollars)", bold=True))
        self.session = _label("Session: waiting for the first scan…", size=12)
        self.since = _label("", size=12)
        self.burn = _label("", dim=True, size=12, wrap=True)
        add(self.session, self.since, self.burn, _separator())

        # -- practice ----------------------------------------------------------------
        add(_label("Try it here", bold=True),
            _label(f"Click in the box, hold {self.talk_key}, speak, let go. Click back into "
                   f"Claude Code to dictate there.", dim=True, wrap=True))
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, PRACTICE_H))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, PRACTICE_H))
        tv.setRichText_(False)
        tv.setFont_(NSFont.systemFontOfSize_(13))
        tv.setAutoresizingMask_(NSViewWidthSizable)
        scroll.setDocumentView_(tv)
        scroll.widthAnchor().constraintEqualToConstant_(WIDTH).setActive_(True)
        scroll.heightAnchor().constraintEqualToConstant_(PRACTICE_H).setActive_(True)
        self.practice = tv
        add(scroll)

        add(_row(
            NSButton.buttonWithTitle_target_action_("Preview overlay", self.target, "preview:"),
            NSButton.buttonWithTitle_target_action_("Copy last transcript", self.target,
                                                    "copyLast:"),
            NSButton.buttonWithTitle_target_action_("Clear box", self.target,
                                                    "clearPractice:"),
            NSButton.buttonWithTitle_target_action_("Quit Dict8", self.target, "quit:")))

        stack = NSStackView.stackViewWithViews_(views)
        stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        stack.setAlignment_(NSLayoutAttributeLeading)
        stack.setSpacing_(6)
        stack.setEdgeInsets_((INSET, INSET, INSET, INSET))
        w.setContentView_(stack)
        self.stack = stack
        self.set_state("loading")
        self.set_advice(chip=None, strength=None, estimate=None, override=None)
        self._fit()

    # -- geometry / visibility -----------------------------------------------------------

    def _fit(self) -> None:
        self.stack.layoutSubtreeIfNeeded()
        size = self.stack.fittingSize()
        top = self.win.frame().origin.y + self.win.frame().size.height
        self.win.setContentSize_((WIDTH + 2 * INSET, size.height))
        if self._placed:   # grow downwards, keep the title bar where the user put it
            f = self.win.frame()
            self.win.setFrameTopLeftPoint_((f.origin.x, top))

    def show(self) -> None:
        if not self._placed:
            self.win.center()
            self._placed = True
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.win.makeKeyAndOrderFront_(None)
        self.win.makeFirstResponder_(self.practice)

    @property
    def visible(self) -> bool:
        return bool(self.win.isVisible())

    def snapshot(self, path: str) -> bool:
        """The window's own pixels, title bar included, to a PNG — no Screen Recording grant."""
        view = self.win.contentView().superview() or self.win.contentView()
        view.layoutSubtreeIfNeeded()
        rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        if rep is None:
            return False
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
        data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
        return bool(data is not None and data.writeToFile_atomically_(path, True))

    # -- content -------------------------------------------------------------------------

    def set_state(self, state: str, detail: str = "") -> None:
        self._state, self._detail = state, detail
        headline, color = STATUS.get(state, (state, "systemGrayColor"))
        if state == "idle" and self._missing:   # loaded, but a grant still blocks dictation
            headline, color = "Almost ready", "systemOrangeColor"
            detail = detail or ("allow " + " and ".join(permissions.LABELS[g]
                                                        for g in self._missing) + " below")
        self.status.setStringValue_(headline)
        self.status_dot.setTextColor_(_color(color))
        self.status_detail.setStringValue_(detail)
        self._fit()

    def set_notice(self, text: str) -> None:
        self.notice.setStringValue_(f"Last message: {text}")
        self._fit()

    def set_permissions(self, states: dict[str, str]) -> None:
        self._missing = permissions.missing(states)
        for g, (mark, state, btn) in self.perm_rows.items():
            m, words, color, button = PERM_STATES.get(states.get(g, "unknown"),
                                                      PERM_STATES["unknown"])
            mark.setStringValue_(m)
            mark.setTextColor_(_color(color))
            state.setStringValue_(words)
            btn.setHidden_(button is None)
            if button:
                btn.setTitle_(button)
        self.set_state(self._state, self._detail)

    def set_model(self, kind: str, text: str, ok: bool | None) -> None:
        """ok: True ready, False failed/unavailable, None still loading."""
        label = self.models[kind][1]
        label.setStringValue_(text)
        label.setTextColor_(NSColor.secondaryLabelColor() if ok is None
                            else _color("systemGreenColor") if ok
                            else _color("systemOrangeColor"))
        self._fit()

    def set_last(self, summary: str, heard: str | None) -> None:
        self.last.setStringValue_(summary)
        self.heard.setStringValue_(f"“{heard}”" if heard else "")
        self.heard.setHidden_(not heard)
        self._fit()

    def set_advice(self, *, chip: str | None, strength: str | None, estimate: str | None,
                   override: str | None) -> None:
        """Mirrors the overlay: a None hides its line, never a placeholder model."""
        self.chip.setStringValue_(f"Recommended: {chip}" if chip else "")
        self.strength.setStringValue_(strength or "")
        self.advice_row.setHidden_(not chip)
        self.estimate.setStringValue_(estimate or "")
        self.estimate.setHidden_(not estimate)
        self.override.setStringValue_(override or "")
        self.override.setHidden_(not override)
        self._fit()

    def set_meter(self, session: str, since: str, burn: str) -> None:
        self.session.setStringValue_(session)
        self.since.setStringValue_(since)
        self.burn.setStringValue_(burn)
        self._fit()

    def clear_practice(self) -> None:
        self.practice.setString_("")


def install_edit_menu() -> None:
    """A main menu with Edit, so ⌘V / ⌘C / ⌘A / ⌘Z work in the practice box. Dict8 has no
    Dock icon, so the menu bar never shows it, but AppKit still routes key equivalents
    through it. Without it, the paste fallback could not land in the practice box."""
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
