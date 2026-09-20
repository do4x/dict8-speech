"""The menu-bar item: state at a glance, the last message, and the actions that matter.

The item is an SF Symbol template image, which the menu bar tints for Light and Dark mode.
If this macOS lacks the symbol, the text title below is shown instead, so the item is never
blank:

    waveform                        D8        idle, ready
    ellipsis                        D8 …      loading the STT model
    waveform.circle.fill            D8 ●      recording (talk key held)
    waveform.circle                 D8 ◌      transcribing / injecting
    exclamationmark.triangle        D8 ⚠      needs attention; the menu's top lines say what
    doc.on.clipboard                D8 ⎘      the last transcript is on the clipboard, not
                                              typed — press ⌘V (until the next press)

U6 puts the live meter beside the icon: `0.8M/3.0M` is the tokens spent in the active Claude
Code session since the last dictation, over that dictation's estimate high (`—` when the
estimator refused). It is set in tabular digits so the item does not jitter as it counts.
Tokens, never USD (invariant 6). The menu adds the session total, the last estimate range,
and the quota burn rate or its labeled gap.

The menu opens with the state and the talk key. Next come attention lines, shown only when
they have something to say: the last message, and any missing grant. Then the meter, then
the actions.

Every method here must run on the main thread; `dict8.app` marshals through
`PyObjCTools.AppHelper.callAfter`.
"""

from __future__ import annotations

import objc
from AppKit import (NSAttributedString, NSColor, NSFontAttributeName,
                    NSForegroundColorAttributeName, NSImageLeft, NSMenu, NSMenuItem,
                    NSStatusBar, NSVariableStatusItemLength)
from Foundation import NSObject

from dict8 import permissions
from dict8.ui import theme

TITLES = {   # the text form: fallback title, and what the logs print
    "loading": "D8 …",
    "idle": "D8",
    "recording": "D8 ●",
    "transcribing": "D8 ◌",
    "error": "D8 ⚠",
    "fallback": "D8 ⎘",
}

SYMBOLS = {
    "loading": "ellipsis",
    "idle": "waveform",
    "recording": "waveform.circle.fill",
    "transcribing": "waveform.circle",
    "error": "exclamationmark.triangle",
    "fallback": "doc.on.clipboard",
}

HEADLINES = {
    "loading": "Loading the speech model…",
    "idle": "Ready",
    "recording": "Recording…",
    "transcribing": "Transcribing…",
    "error": "Needs attention",
    "fallback": "On the clipboard — press ⌘V",
}


class _MenuTarget(NSObject):
    """ObjC target for the menu items; forwards to plain Python callables."""

    def initWithHandlers_(self, handlers):
        self = objc.super(_MenuTarget, self).init()
        if self is None:
            return None
        self.handlers = handlers
        return self

    def openWindow_(self, sender):
        self.handlers["open_window"]()

    def checkPermissions_(self, sender):
        self.handlers["check_permissions"]()

    def copyLast_(self, sender):
        self.handlers["copy_last"]()

    def quit_(self, sender):
        self.handlers["quit"]()


def _info(title: str) -> NSMenuItem:
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
    item.setEnabled_(False)
    return item


def _styled(text: str, *, size: float = 13, weight=theme.REGULAR, color=None):
    return NSAttributedString.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: theme.font(size, weight),
               NSForegroundColorAttributeName: color or NSColor.labelColor()})


class Tray:
    def __init__(self, handlers: dict, *, hotkey_label: str) -> None:
        self.target = _MenuTarget.alloc().initWithHandlers_(handlers)
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        self.item.button().setImagePosition_(NSImageLeft)
        self.menu = NSMenu.alloc().init()
        self.menu.setAutoenablesItems_(False)

        self.state_line = _info("Loading…")
        self.menu.addItem_(self.state_line)
        self.menu.addItem_(_info(hotkey_label))
        self.error_line = _info("")
        self.perm_line = _info("")
        self.error_line.setHidden_(True)
        self.perm_line.setHidden_(True)
        self.menu.addItem_(self.error_line)
        self.menu.addItem_(self.perm_line)
        self.menu.addItem_(NSMenuItem.separatorItem())
        self.session_line = _info("Session: no Claude Code session seen yet")
        self.since_line = _info("Since last dictation: no dictation yet")
        self.burn_line = _info("Burn rate: not read yet")
        for it in (self.session_line, self.since_line, self.burn_line):
            self.menu.addItem_(it)
        self.menu.addItem_(NSMenuItem.separatorItem())
        for title, sel, key in (("Open Dict8…", "openWindow:", ""),
                                ("Copy Last Transcript", "copyLast:", ""),
                                ("Check Permissions", "checkPermissions:", ""),
                                (None, None, None),
                                ("Quit Dict8", "quit:", "q")):
            if title is None:
                self.menu.addItem_(NSMenuItem.separatorItem())
                continue
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, key)
            it.setTarget_(self.target)
            self.menu.addItem_(it)
        self.item.setMenu_(self.menu)
        self.meter_suffix = ""
        self.state = "loading"
        self.set_state("loading")

    @property
    def title(self) -> str:
        """The item as text (for logs): the state's title plus the meter."""
        title = TITLES[self.state]
        return f"{title} {self.meter_suffix}" if self.meter_suffix else title

    def _retitle(self) -> None:
        button = self.item.button()
        image = theme.symbol(SYMBOLS[self.state], 14, theme.MEDIUM)
        button.setImage_(image)
        text = self.meter_suffix if image is not None else self.title
        if image is not None and text:
            text = " " + text
        button.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: theme.digits(12, theme.MEDIUM)}))
        button.setToolTip_(f"Dict8 — {HEADLINES[self.state]}")

    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self._retitle()
        line = _styled(HEADLINES.get(state, state), weight=theme.SEMIBOLD).mutableCopy()
        if detail:
            line.appendAttributedString_(_styled(f" — {detail}",
                                                 color=NSColor.secondaryLabelColor()))
        self.state_line.setAttributedTitle_(line)

    def set_meter(self, suffix: str, session: str, since: str, burn: str) -> None:
        self.meter_suffix = suffix
        self._retitle()
        self.session_line.setTitle_(session)
        self.since_line.setTitle_(since)
        self.burn_line.setTitle_(burn)

    def set_error(self, text: str) -> None:
        self.error_line.setTitle_(f"Last message: {text.removeprefix('Dict8: ')}")
        self.error_line.setHidden_(not text)

    def set_permissions(self, states: dict[str, str]) -> None:
        missing = permissions.missing(states)
        self.perm_line.setTitle_("Needs " + ", ".join(
            f"{permissions.LABELS[g]} ({states.get(g, 'unknown').replace('_', ' ')})"
            for g in missing))
        self.perm_line.setHidden_(not missing)
