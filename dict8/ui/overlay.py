"""The overlay: the Flow bar, drawn by React (ui/src/overlay) in a WKWebView inside a panel
that NEVER takes focus.

Injection types into the frontmost app. If this panel became key, or activated Dict8, Dict8
would become the frontmost app and the next type-out would land in the overlay, or nowhere,
with no error anywhere. That is a silent failure (invariant 7b's category). So every property
that could hand this panel focus is pinned off, in the class and on the instance:

- `NSWindowStyleMaskNonactivatingPanel`: showing or clicking it never activates Dict8;
- `canBecomeKeyWindow` / `canBecomeMainWindow` return False (tests call `makeKeyWindow()`
  and check it is refused);
- `ignoresMouseEvents`: clicks pass through the panel — including the transparent margin
  around the capsule — to whatever is underneath;
- shown with `orderFrontRegardless()`, never `makeKeyAndOrderFront_`;
- `hidesOnDeactivate` off: Dict8 is never the active app, so an NSPanel's default (hide when
  the app deactivates) would keep it invisible;
- joins all Spaces and full-screen apps, and stays out of the window cycle.

The web view is only a renderer: the panel refuses key status, so nothing in the page can
take first responder, and the page sets `pointer-events: none` besides.

This module owns *what* is shown; ui/src/overlay owns how it looks:

- recording: Python maps the microphone's dBFS to 0..1 against `audio.silence_floor_dbfs`
  and pushes it `METER_HZ` times a second down a channel that does not re-render React.
  Flat bars mean "Dict8 would call this silence", which is worth seeing while you speak
  rather than after you let go;
- transcribing: the same bars, dimmed, in a travelling wave;
- the result: a glyph and a word, with an amber ring for the clipboard fallback and a red
  one for a failure;
- the advice: the model chip, its strength line, the estimate and any spoken override, in a
  tag that rises out of the capsule. Recommendation and estimate live HERE, beside the
  prompt, and are never part of the injected text (invariant 1). No recommendation means no
  chip, never a placeholder (invariant 2).

The panel is shown on key-down with no animation and dismissed `overlay.dismiss_after_s`
after the last result or advice. It never auto-dismisses while recording or transcribing: a
long dictation keeps its bars. `audio.max_hold_s` plus the dismiss delay is only a backstop
against a lost state.

Main thread only; `dict8.app` marshals every call through `AppHelper.callAfter`.
"""

from __future__ import annotations

import logging

from AppKit import (NSApplication, NSBackingStoreBuffered, NSColor, NSEvent, NSMakeRect,
                    NSPanel, NSScreen, NSStatusWindowLevel,
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorIgnoresCycle,
                    NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
                    NSWindowStyleMaskNonactivatingPanel)
from Foundation import NSPointInRect, NSRunLoop, NSRunLoopCommonModes, NSTimer

from dict8.ui import webhost

log = logging.getLogger(__name__)

# The panel is a fixed, transparent canvas; the page centres the capsule in it and draws its
# own shadow. Presentation, not thresholds.
CANVAS_W, CANVAS_H = 620, 260
SCREEN_MARGIN = 2      # the page's own padding puts the capsule ~16 pt further in
METER_HZ = 30          # level pushes per second while recording
METER_RANGE_DB = 40    # bars span silence_floor_dbfs .. floor + this

# state -> (glyph in ui/src/overlay/Glyph.tsx, words, tone). "warn" is the amber ring,
# "error" the red one.
STATES = {
    "recording": (None, "", "plain"),
    "transcribing": (None, "", "plain"),
    "typed": ("check", "Typed", "plain"),
    "sent": ("send", "Typed and sent", "plain"),
    "pasted": ("check", "Pasted", "plain"),
    "clipboard": ("clipboard", "On the clipboard — press ⌘V", "warn"),
    "cancelled": ("x", "Cancelled — nothing typed", "plain"),
    "nothing": ("mute", "Heard nothing — nothing typed", "plain"),
    "demo": ("eye", "Demo — text not typed", "plain"),
    "preview": ("eye", "Preview — nothing typed", "plain"),
    "error": ("alert", "Dictation failed", "error"),
}
LIVE = ("recording", "transcribing")


class OverlayPanel(NSPanel):
    """An NSPanel that refuses key and main status outright."""

    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class Overlay:
    def __init__(self, cfg) -> None:
        self.dismiss_after_s = float(cfg.require("overlay.dismiss_after_s"))
        self.position = str(cfg.require("overlay.position"))
        if self.position not in ("top", "bottom"):
            raise ValueError(f"overlay.position must be top or bottom, not {self.position!r}")
        self.floor_db = float(cfg.require("audio.silence_floor_dbfs"))
        self.backstop_s = float(cfg.require("audio.max_hold_s")) + self.dismiss_after_s
        # Set by the app: returns the microphone's current level in dBFS, or None.
        self.level_source = None
        self._gen = 0
        self._state: str | None = None
        self._detail = ""
        self._advice: dict = {}
        self._meter = None

        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        p = OverlayPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, CANVAS_W, CANVAS_H), style, NSBackingStoreBuffered, False)
        p.setLevel_(NSStatusWindowLevel)
        p.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | NSWindowCollectionBehaviorStationary
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary
                                 | NSWindowCollectionBehaviorIgnoresCycle)
        p.setIgnoresMouseEvents_(True)
        p.setHidesOnDeactivate_(False)
        p.setBecomesKeyOnlyIfNeeded_(True)
        p.setFloatingPanel_(True)
        p.setOpaque_(False)
        p.setBackgroundColor_(NSColor.clearColor())
        p.setHasShadow_(False)          # the capsule and tag cast their own, in CSS
        p.setReleasedWhenClosed_(False)
        self.panel = p

        self.host = webhost.WebHost("overlay", NSMakeRect(0, 0, CANVAS_W, CANVAS_H),
                                    on_action=self._action, transparent=True)
        p.setContentView_(self.host.view)
        self._push()

    def _action(self, payload: dict) -> None:
        log.debug("overlay: page sent %r (nothing in it is clickable)", payload)

    # -- state ---------------------------------------------------------------------------

    def state(self) -> dict:
        """Exactly what the page is being told to show. The tests assert on this."""
        phase = ("hidden" if self._state is None
                 else self._state if self._state in LIVE else "result")
        result = None
        if phase == "result":
            glyph, text, tone = STATES.get(self._state, (None, self._state, "plain"))
            result = {"glyph": glyph, "text": f"{text} · {self._detail}" if self._detail
                      else text, "tone": tone}
        return {"phase": phase, "position": self.position, "level": 0.0,
                "result": result, "advice": self._advice or None}

    def _push(self) -> None:
        self.host.push(self.state())

    def show_state(self, state: str, detail: str = "") -> None:
        self._state, self._detail = state, detail
        self._push()
        if state == "recording":
            self._start_meter()
        else:
            self._stop_meter()
        self._show()

    # -- the live bars -------------------------------------------------------------------

    def level_to_bar(self, db: float | None) -> float:
        """dBFS -> 0..1. Zero at and below the silence floor: flat bars mean "Dict8 would
        call this silence", which is the thing worth seeing while you speak."""
        if db is None:
            return 0.0
        return min(max((db - self.floor_db) / METER_RANGE_DB, 0.0), 1.0)

    def _start_meter(self) -> None:
        self._stop_meter()

        def tick(_timer):
            self._tick()
        self._meter = NSTimer.timerWithTimeInterval_repeats_block_(1 / METER_HZ, True, tick)
        NSRunLoop.mainRunLoop().addTimer_forMode_(self._meter, NSRunLoopCommonModes)

    def _stop_meter(self) -> None:
        if self._meter is not None:
            self._meter.invalidate()
            self._meter = None

    def _tick(self) -> None:
        try:
            level = self.level_to_bar(self.level_source() if self.level_source else None)
        except Exception:
            level = 0.0          # a failing source is flat bars, never a crash
        self.host.push_level(level)

    # -- the advice tag ------------------------------------------------------------------

    def clear_advice(self) -> None:
        self.set_advice(chip=None, strength=None, estimate=None, override=None)

    def set_advice(self, *, chip: str | None, strength: str | None, estimate: str | None,
                   override: str | None) -> None:
        """Any argument None hides its line: no recommendation means no chip (invariant 2),
        never a placeholder model."""
        self._advice = {k: v for k, v in (("chip", chip), ("strength", strength),
                                          ("estimate", estimate), ("override", override))
                        if v}
        self._push()
        if self._state is not None:
            self._show()

    # -- visibility ----------------------------------------------------------------------

    def _show(self) -> None:
        if not self.visible:
            self._position()
            self.panel.orderFrontRegardless()  # never makeKeyAndOrderFront_: no focus, ever
        self._schedule_dismiss()

    def _schedule_dismiss(self) -> None:
        self._gen += 1
        gen = self._gen
        delay = self.backstop_s if self._state in LIVE else self.dismiss_after_s

        def dismiss(_timer):
            if gen == self._gen:
                self.hide()
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(delay, False, dismiss)

    def _position(self) -> None:
        loc = NSEvent.mouseLocation()
        screen = next((s for s in (NSScreen.screens() or [])
                       if NSPointInRect(loc, s.frame())), None) or NSScreen.mainScreen()
        if screen is None:
            return
        vf = screen.visibleFrame()
        x = vf.origin.x + (vf.size.width - CANVAS_W) / 2
        y = (vf.origin.y + SCREEN_MARGIN if self.position == "bottom"
             else vf.origin.y + vf.size.height - SCREEN_MARGIN - CANVAS_H)
        self.panel.setFrameOrigin_((x, y))

    def hide(self) -> None:
        """At once: a too-short tap of the talk key vanishes the way it came."""
        self._gen += 1
        self._stop_meter()
        self._state, self._detail = None, ""
        self._advice = {}
        self._push()
        self.panel.orderOut_(None)

    @property
    def visible(self) -> bool:
        return bool(self.panel.isVisible())

    def focus_report(self) -> dict:
        """What the gate and the tests check: this panel never holds focus."""
        app = NSApplication.sharedApplication()
        key = app.keyWindow()
        return {"panel_is_key": bool(self.panel.isKeyWindow()),
                "panel_is_main": bool(self.panel.isMainWindow()),
                "app_key_window_is_panel": key is not None and key == self.panel,
                "app_active": bool(app.isActive()),
                "ignores_mouse": bool(self.panel.ignoresMouseEvents()),
                "nonactivating": bool(self.panel.styleMask()
                                      & NSWindowStyleMaskNonactivatingPanel)}

    def snapshot(self, path: str) -> bool:
        """The panel's own pixels to a PNG — WebKit renders it, so no Screen Recording
        grant is involved."""
        return self.host.snapshot(path)
