"""The overlay: a small panel at the top-center of the screen with the cursor, shown on
the talk key, that NEVER takes focus.

Injection types into the frontmost app. A panel that became key or activated Dict8 would
make Dict8 the frontmost app and the next type-out would land in the overlay — or nowhere —
with no error anywhere: a silent failure (invariant 7b's category). So every property that
could hand this panel focus is pinned off, in the class and on the instance:

- `NSWindowStyleMaskNonactivatingPanel`: showing or clicking it never activates Dict8;
- `canBecomeKeyWindow` / `canBecomeMainWindow` return False (tests call `makeKeyWindow()`
  and check it is refused);
- `ignoresMouseEvents`: clicks pass through to whatever is underneath;
- shown with `orderFrontRegardless()`, never `makeKeyAndOrderFront_`;
- `hidesOnDeactivate` off — Dict8 is never the active app, so an NSPanel's default
  (hide when the app deactivates) would keep it invisible;
- joins all Spaces and full-screen apps, and stays out of the window cycle.

What it shows (the caller decides the text; this module only lays it out): the state line,
the recommended model chip with its strength line, the estimate range with n, and the
spoken override. Recommendation and estimate live HERE, next to the prompt, and are never
part of the injected text (invariant 1). Auto-dismissed after `overlay.dismiss_after_s`.

Drawn with plain views that paint their own rounded background (no NSVisualEffectView), so
`snapshot()` can render exactly what is on screen to a PNG without screen-recording access.

Main thread only; `dict8.app` marshals every call through `AppHelper.callAfter`.
"""

from __future__ import annotations

import objc
from AppKit import (NSApplication, NSBackingStoreBuffered, NSBezierPath,
                    NSBitmapImageFileTypePNG, NSColor, NSEvent, NSFont, NSMakeRect, NSPanel,
                    NSScreen, NSStatusWindowLevel, NSTextField, NSView,
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorIgnoresCycle,
                    NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
                    NSWindowStyleMaskNonactivatingPanel)
from Foundation import NSPointInRect, NSTimer

# Layout, in points. Presentation, not thresholds.
PAD = 14
GAP = 6
TOP_MARGIN = 12
MIN_W, MAX_W = 280, 560
RADIUS = 12
CHIP_PAD_X, CHIP_PAD_Y = 8, 2

STATE_TEXT = {
    "recording": "● Recording…",
    "transcribing": "◌ Transcribing…",
    "typed": "✓ Typed",
    "sent": "✓ Typed and sent",
    "pasted": "✓ Pasted",
    "clipboard": "⎘ On the clipboard — press ⌘V",
    "cancelled": "✕ Cancelled — nothing typed",
    "nothing": "Heard nothing — nothing typed",
    "demo": "Demo — text not typed (injection off)",
    "error": "⚠ Dictation failed",
}


class OverlayPanel(NSPanel):
    """An NSPanel that refuses key and main status outright."""

    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class RoundedView(NSView):
    """Paints a filled rounded rect behind its subviews."""

    def initWithFrame_fill_radius_(self, frame, fill, radius):
        self = objc.super(RoundedView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.fill = fill
        self.radius = radius
        return self

    def drawRect_(self, rect):
        self.fill.setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            self.bounds(), self.radius, self.radius).fill()


def _label(size: float, *, bold: bool = False, color=None) -> NSTextField:
    f = NSTextField.labelWithString_("")
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    f.setTextColor_(color or NSColor.whiteColor())
    f.setDrawsBackground_(False)
    f.setBezeled_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    return f


class Overlay:
    def __init__(self, cfg) -> None:
        self.dismiss_after_s = float(cfg.require("overlay.dismiss_after_s"))
        self._gen = 0
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        p = OverlayPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, MIN_W, 60), style, NSBackingStoreBuffered, False)
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
        p.setHasShadow_(True)
        p.setReleasedWhenClosed_(False)
        self.panel = p

        self.root = RoundedView.alloc().initWithFrame_fill_radius_(
            NSMakeRect(0, 0, MIN_W, 60),
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.11, 0.11, 0.13, 0.94), RADIUS)
        p.setContentView_(self.root)

        dim = NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.72)
        self.state = _label(13, bold=True)
        self.chip_bg = RoundedView.alloc().initWithFrame_fill_radius_(
            NSMakeRect(0, 0, 10, 10),
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.20, 0.45, 0.95, 1.0), 6)
        self.chip = _label(12, bold=True)
        self.chip_bg.addSubview_(self.chip)
        self.strength = _label(12, color=dim)
        self.estimate = _label(12)
        self.override = _label(12, color=NSColor.colorWithCalibratedRed_green_blue_alpha_(
            1.0, 0.8, 0.35, 1.0))
        for v in (self.state, self.chip_bg, self.strength, self.estimate, self.override):
            self.root.addSubview_(v)
        self._chip_text: str | None = None
        self._layout()

    # -- content -------------------------------------------------------------------------

    def show_state(self, state: str, detail: str = "") -> None:
        text = STATE_TEXT.get(state, state)
        self.state.setStringValue_(f"{text} · {detail}" if detail else text)
        self._layout()
        self._show()

    def clear_advice(self) -> None:
        self.set_advice(chip=None, strength=None, estimate=None, override=None)

    def set_advice(self, *, chip: str | None, strength: str | None, estimate: str | None,
                   override: str | None) -> None:
        """Any argument None hides its line: no recommendation means no chip (invariant 2),
        never a placeholder model."""
        self._chip_text = chip
        self.chip.setStringValue_(chip or "")
        self.strength.setStringValue_(strength or "")
        self.estimate.setStringValue_(estimate or "")
        self.override.setStringValue_(override or "")
        self._layout()
        self._show()

    # -- geometry ------------------------------------------------------------------------

    def _layout(self) -> None:
        def size(v):
            s = v.fittingSize()
            return s.width, s.height

        rows: list[tuple[list, float]] = []           # ([(view, w, h)], row height)
        sw, sh = size(self.state)
        rows.append(([(self.state, sw, sh)], sh))
        show_chip = bool(self._chip_text)
        self.chip_bg.setHidden_(not show_chip)
        self.strength.setHidden_(not show_chip or not self.strength.stringValue())
        if show_chip:
            cw, ch = size(self.chip)
            bw, bh = cw + 2 * CHIP_PAD_X, ch + 2 * CHIP_PAD_Y
            self.chip.setFrame_(NSMakeRect(CHIP_PAD_X, CHIP_PAD_Y, cw, ch))
            row = [(self.chip_bg, bw, bh)]
            if self.strength.stringValue():
                stw, sth = size(self.strength)
                row.append((self.strength, stw, sth))
            rows.append((row, max(h for _, _, h in row)))
        for v in (self.estimate, self.override):
            v.setHidden_(not v.stringValue())
            if v.stringValue():
                w, h = size(v)
                rows.append(([(v, w, h)], h))

        width = max(sum(w for _, w, _ in r) + GAP * (len(r) - 1) for r, _ in rows) + 2 * PAD
        width = min(max(width, MIN_W), MAX_W)
        height = sum(h for _, h in rows) + GAP * (len(rows) - 1) + 2 * PAD
        y = height - PAD
        for row, rh in rows:
            y -= rh
            x = PAD
            for v, w, h in row:
                w = min(w, width - PAD - x)
                v.setFrame_(NSMakeRect(x, y + (rh - h) / 2, w, h))
                x += w + GAP
            y -= GAP
        self.root.setFrame_(NSMakeRect(0, 0, width, height))
        self.root.setNeedsDisplay_(True)
        frame = self.panel.frame()
        frame.size.width, frame.size.height = width, height
        self.panel.setFrame_display_(frame, True)

    def _position(self) -> None:
        loc = NSEvent.mouseLocation()
        screen = next((s for s in (NSScreen.screens() or [])
                       if NSPointInRect(loc, s.frame())), None) or NSScreen.mainScreen()
        if screen is None:
            return
        vf = screen.visibleFrame()
        f = self.panel.frame()
        x = vf.origin.x + (vf.size.width - f.size.width) / 2
        y = vf.origin.y + vf.size.height - f.size.height - TOP_MARGIN
        self.panel.setFrameOrigin_((x, y))

    # -- visibility ----------------------------------------------------------------------

    def _show(self) -> None:
        self._position()
        self.panel.orderFrontRegardless()   # never makeKeyAndOrderFront_: no focus, ever
        self._gen += 1
        gen = self._gen

        def dismiss(_timer):
            if gen == self._gen:
                self.hide()
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(self.dismiss_after_s, False,
                                                              dismiss)

    def hide(self) -> None:
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
        """Render the panel's content to a PNG — the panel's own pixels, no screen capture
        (so no Screen Recording grant is involved)."""
        view = self.root
        rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        if rep is None:
            return False
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
        data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
        return bool(data is not None and data.writeToFile_atomically_(path, True))
