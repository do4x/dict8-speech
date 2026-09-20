"""Design tokens shared by the overlay, the Dict8 window and the menu-bar item.

Presentation only: sizes, colors, curves, key spellings. Nothing here is a threshold, a model
name or a path. Invariant 3 is about those, and config.yml still owns every decision.

Where the values come from (Denis, 2026-09-19: "a great, minimalist, neat-looking UI"):
- Wispr Flow's Flow bar sets the *feel* (CLAUDE.md). It is a small black capsule with white
  level bars, 30 pt tall, and it changes size rather than stacking text. Extra information
  hangs off it as a tag. Transitions take 100-200 ms on a strong ease-out, and an error gets
  a red ring. Read from the installed app (1.6.897), not guessed.
- Emil Kowalski's and Apple's motion rules set the *motion*. The talk key is pressed tens of
  times a day, so the overlay responds on key-down with no entrance animation. Enter and exit
  use a strong ease-out, and exit is faster than enter. On-screen morphs are critically
  damped springs (damping 1.0, response 0.3 s). Every animation starts from the on-screen
  (presentation) value, so a new state can interrupt the last one mid-flight. Reduce Motion
  keeps opacity changes and drops movement.
"""

from __future__ import annotations

import math

from AppKit import (NSAppearanceNameAqua, NSAppearanceNameDarkAqua, NSBitmapImageRep,
                    NSColor, NSDeviceRGBColorSpace, NSFont, NSFontWeightMedium,
                    NSFontWeightRegular, NSFontWeightSemibold, NSGraphicsContext, NSImage,
                    NSImageSymbolConfiguration, NSMakeRect, NSWorkspace)

# -- motion ------------------------------------------------------------------------------

EASE_OUT = (0.23, 1.0, 0.32, 1.0)        # enter / exit / feedback
EASE_IN_OUT = (0.77, 0.0, 0.175, 1.0)    # continuous on-screen loops
SPRING_RESPONSE = 0.3                    # s; Apple's "response", not a duration
SPRING_DAMPING = 1.0                     # critically damped: no overshoot on UI chrome

T_FAST = 0.10       # s: bars fading out under a result, a tag leaving
T_ENTER = 0.18      # s: a tag or a label arriving
T_EXIT = 0.16       # s: the overlay leaving; faster than anything arrives


def reduce_motion() -> bool:
    try:
        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:
        return False


def timing(points):
    from Quartz import CAMediaTimingFunction

    return CAMediaTimingFunction.functionWithControlPoints____(*points)


def named_timing(name):
    from Quartz import CAMediaTimingFunction

    return CAMediaTimingFunction.functionWithName_(name)


def spring_constants(response: float = SPRING_RESPONSE,
                     damping_ratio: float = SPRING_DAMPING) -> tuple[float, float]:
    """Apple's (response, damping ratio) as Core Animation's (stiffness, damping), mass 1."""
    stiffness = (2 * math.pi / response) ** 2
    return stiffness, 4 * math.pi * damping_ratio / response


# -- color -------------------------------------------------------------------------------

def rgba(r: float, g: float, b: float, a: float = 1.0):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


def dynamic(light: tuple, dark: tuple):
    """One NSColor that resolves per appearance, so the window follows Light/Dark mode."""
    lc, dc = rgba(*light), rgba(*dark)

    def provider(appearance):
        best = appearance.bestMatchFromAppearancesWithNames_(
            [NSAppearanceNameAqua, NSAppearanceNameDarkAqua])
        return dc if best == NSAppearanceNameDarkAqua else lc
    return NSColor.colorWithName_dynamicProvider_(None, provider)


# The window: a warm off-white canvas with white cards in Light mode, near-black in Dark mode.
CANVAS = dynamic((0.957, 0.953, 0.941), (0.110, 0.110, 0.118))
CARD = dynamic((1.0, 1.0, 1.0), (0.153, 0.153, 0.161))
HAIRLINE = dynamic((0.0, 0.0, 0.0, 0.08), (1.0, 1.0, 1.0, 0.08))
WELL = dynamic((0.0, 0.0, 0.0, 0.03), (1.0, 1.0, 1.0, 0.04))        # the practice box
KEYCAP_FILL = dynamic((1.0, 1.0, 1.0), (1.0, 1.0, 1.0, 0.07))
KEYCAP_EDGE = dynamic((0.0, 0.0, 0.0, 0.16), (1.0, 1.0, 1.0, 0.18))
NOTICE_FILL = dynamic((0.0, 0.0, 0.0, 0.04), (1.0, 1.0, 1.0, 0.05))

# The overlay is always dark, like the Flow bar: it has to read over any app.
CAPSULE = (0.0, 0.0, 0.0, 0.96)
CAPSULE_EDGE = (1.0, 1.0, 1.0, 0.16)
TAG = (0.075, 0.075, 0.08, 0.97)
INK = (1.0, 1.0, 1.0, 1.0)
INK_DIM = (1.0, 1.0, 1.0, 0.62)
AMBER = (1.0, 0.73, 0.32, 1.0)
RED = (0.94, 0.42, 0.42, 1.0)


def cgcolor(c: tuple):
    return rgba(*c).CGColor()


# -- type --------------------------------------------------------------------------------

def font(size: float, weight=NSFontWeightRegular):
    return NSFont.systemFontOfSize_weight_(size, weight)


def digits(size: float, weight=NSFontWeightRegular):
    """Tabular digits, so numbers that update in place do not jitter sideways."""
    return NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight)


REGULAR, MEDIUM, SEMIBOLD = NSFontWeightRegular, NSFontWeightMedium, NSFontWeightSemibold

# -- symbols -----------------------------------------------------------------------------


def symbol(name: str, size: float, weight=NSFontWeightMedium, color=None):
    """An SF Symbol as an NSImage. With a color it is tinted (a list tints its layers in
    order: a white check on a green disc is [white, green]); without one it is a template
    (for the menu bar, which tints it). None if this macOS lacks the symbol."""
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is None:
        return None
    conf = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, weight)
    if color is not None:
        conf = conf.configurationByApplyingConfiguration_(
            NSImageSymbolConfiguration.configurationWithPaletteColors_(
                color if isinstance(color, list) else [color]))
    img = img.imageWithSymbolConfiguration_(conf)
    img.setTemplate_(color is None)
    return img


def symbol_cgimage(name: str, size: float, color: tuple, weight=NSFontWeightSemibold,
                   scale: float = 2.0):
    """A tinted symbol rendered to a CGImage, for a CALayer's contents. Returns
    (cgimage, width_pt, height_pt), or (None, 0, 0) if the symbol is missing."""
    img = symbol(name, size, weight, rgba(*color))
    if img is None:
        return None, 0.0, 0.0
    sz = img.size()
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
        None, int(math.ceil(sz.width * scale)), int(math.ceil(sz.height * scale)), 8, 4, True,
        False, NSDeviceRGBColorSpace, 0, 0)
    rep.setSize_(sz)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    img.drawInRect_(NSMakeRect(0, 0, sz.width, sz.height))
    NSGraphicsContext.restoreGraphicsState()
    return rep.CGImage(), float(sz.width), float(sz.height)


# -- keys --------------------------------------------------------------------------------
# How config's key names read on screen. Config names the key; this only spells it.

KEY_NAMES = {
    "right_option": "the right ⌥ Option key",
    "left_option": "the left ⌥ Option key",
    "right_command": "the right ⌘ Command key",
    "right_control": "the right ⌃ Control key",
    "right_shift": "the right ⇧ Shift key",
    "escape": "Esc",
}

KEY_CAPS = {   # the same keys, short enough to sit on a keycap
    "right_option": "right ⌥",
    "left_option": "left ⌥",
    "right_command": "right ⌘",
    "right_control": "right ⌃",
    "right_shift": "right ⇧",
    "escape": "esc",
}


def key_name(key: str) -> str:
    return KEY_NAMES.get(key, key)


def key_cap(key: str) -> str:
    return KEY_CAPS.get(key, key)
