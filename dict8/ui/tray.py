"""The menu-bar item: state at a glance, the last error, and the two actions that matter.

U5 scope: an `NSStatusItem` only (the overlay is U6). The title is text, not an icon file,
so there is nothing to bundle yet:

    D8 …   loading the STT model
    D8     idle, ready
    D8 ●   recording (talk key held)
    D8 ◌   transcribing / injecting
    D8 ⚠   something needs attention — the menu's first lines say what
    D8 ⎘   the last transcript is on the clipboard, not typed — press ⌘V (until next press)

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
        self.error_line = _info("Last error: none")
        self.perm_line = _info("Permissions: not checked yet")
        self.menu.addItem_(self.state_line)
        self.menu.addItem_(self.error_line)
        self.menu.addItem_(self.perm_line)
        self.menu.addItem_(_info(f"Talk key: {hotkey_label}"))
        self.menu.addItem_(_info(f"Mic: {mic_label}"))
        self.menu.addItem_(NSMenuItem.separatorItem())
        for title, sel in (("Check permissions", "checkPermissions:"),
                           ("Copy last transcript", "copyLast:"),
                           ("Quit Dict8", "quit:")):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, "")
            it.setTarget_(self.target)
            self.menu.addItem_(it)
        self.item.setMenu_(self.menu)
        self.set_state("loading")

    @property
    def title(self) -> str:
        return str(self.item.button().title())

    def set_state(self, state: str, detail: str = "") -> None:
        self.item.button().setTitle_(TITLES[state])
        self.state_line.setTitle_(f"State: {state}" + (f" — {detail}" if detail else ""))

    def set_error(self, text: str) -> None:
        self.error_line.setTitle_(f"Last error: {text}")

    def set_permissions(self, states: dict[str, str]) -> None:
        self.perm_line.setTitle_("Permissions: " + ", ".join(
            f"{k.replace('_', ' ')} {v}" for k, v in states.items()))
