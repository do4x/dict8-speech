"""The menu-bar item: state at a glance, the last error, and the two actions that matter.

U5 scope: an `NSStatusItem` only (the overlay is U6). The title is text, not an icon file,
so there is nothing to bundle yet:

    D8 …   loading the STT model
    D8     idle, ready
    D8 ●   recording (talk key held)
    D8 ◌   transcribing / injecting
    D8 ⚠   something needs attention — the menu's first lines say what
    D8 ⎘   the last transcript is on the clipboard, not typed — press ⌘V (until next press)

U6 appends the live meter to whichever title is showing: `D8 0.8M/3.0M` is tokens spent in
the active Claude Code session since the last dictation, over that dictation's estimate high
(`—` when the estimator refused). Tokens, never USD (invariant 6). The menu adds the session
total, the last estimate range, and the quota burn rate or its labeled gap.

Every method here must run on the main thread; `dict8.app` marshals through
`PyObjCTools.AppHelper.callAfter`.
"""

from __future__ import annotations

import objc
from AppKit import NSMenu, NSMenuItem, NSStatusBar, NSVariableStatusItemLength
from Foundation import NSObject

TITLES = {
    "loading": "D8 …",
    "idle": "D8",
    "recording": "D8 ●",
    "transcribing": "D8 ◌",
    "error": "D8 ⚠",
    "fallback": "D8 ⎘",
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


class Tray:
    def __init__(self, handlers: dict, *, hotkey_label: str, mic_label: str) -> None:
        self.target = _MenuTarget.alloc().initWithHandlers_(handlers)
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        self.menu = NSMenu.alloc().init()
        self.menu.setAutoenablesItems_(False)

        self.state_line = _info("State: loading")
        self.error_line = _info("Last message: none")
        self.perm_line = _info("Permissions: not checked yet")
        self.menu.addItem_(self.state_line)
        self.menu.addItem_(self.error_line)
        self.menu.addItem_(self.perm_line)
        self.session_line = _info("Session: no Claude Code session seen yet")
        self.since_line = _info("Since last dictation: no dictation yet")
        self.burn_line = _info("Burn rate: not read yet")
        for it in (self.session_line, self.since_line, self.burn_line):
            self.menu.addItem_(it)
        self.menu.addItem_(_info(f"Talk key: {hotkey_label}"))
        self.menu.addItem_(_info(f"Mic: {mic_label}"))
        self.menu.addItem_(NSMenuItem.separatorItem())
        for title, sel in (("Open Dict8 window", "openWindow:"),
                           ("Check permissions", "checkPermissions:"),
                           ("Copy last transcript", "copyLast:"),
                           ("Quit Dict8", "quit:")):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, "")
            it.setTarget_(self.target)
            self.menu.addItem_(it)
        self.item.setMenu_(self.menu)
        self.meter_suffix = ""
        self.state = "loading"
        self.set_state("loading")

    @property
    def title(self) -> str:
        return str(self.item.button().title())

    def _retitle(self) -> None:
        title = TITLES[self.state]
        self.item.button().setTitle_(f"{title} {self.meter_suffix}" if self.meter_suffix
                                     else title)

    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self._retitle()
        self.state_line.setTitle_(f"State: {state}" + (f" — {detail}" if detail else ""))

    def set_meter(self, suffix: str, session: str, since: str, burn: str) -> None:
        self.meter_suffix = suffix
        self._retitle()
        self.session_line.setTitle_(session)
        self.since_line.setTitle_(since)
        self.burn_line.setTitle_(burn)

    def set_error(self, text: str) -> None:
        self.error_line.setTitle_(f"Last message: {text}")

    def set_permissions(self, states: dict[str, str]) -> None:
        self.perm_line.setTitle_("Permissions: " + ", ".join(
            f"{k.replace('_', ' ')} {v}" for k, v in states.items()))
