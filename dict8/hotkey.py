"""Hold-to-talk on a CGEventTap (docs/ADR-002-hotkey.md).

`TalkKey` is the whole state machine and has no OS dependency: it takes (event type,
keycode, flags) and says what happened and whether to swallow the event. `HotkeyTap` is
the thin Quartz shell around it.

Rules, from the provisional default in docs/LOOP.md:
- `hotkey.push_to_talk` (a bare modifier, right Option) down -> "press", up -> "release".
- `hotkey.cancel` (Esc) while held -> "cancel", and the Esc is SWALLOWED: in Claude Code a
  stray Esc interrupts the running turn, which is not what "cancel my dictation" means.
- Any other key while held -> "chord": the user is typing Option+key (e.g. ⌥-e for an
  accent), not dictating. The recording is dropped silently and the key passes through.
- A release after cancel/chord is not a dictation.

Bare modifiers are why this is a tap and not `RegisterEventHotKey` (ADR-002): Carbon hotkeys
need a non-modifier key and have no clean key-up for a held modifier.
"""

from __future__ import annotations

import logging
from typing import Callable

from dict8.inject import DICT8_EVENT_TAG

log = logging.getLogger(__name__)

# name -> (virtual keycode, NX device-dependent flag bit or None). Hardware constants from
# <Carbon/HIToolbox/Events.h> and <IOKit/hidsystem/IOLLEvent.h>; config names the key,
# this table only spells it. The device bit tells right Option from left Option, which the
# device-independent kCGEventFlagMaskAlternate cannot.
KEYS: dict[str, tuple[int, int | None]] = {
    "right_option": (61, 0x00000040),
    "left_option": (58, 0x00000020),
    "right_command": (54, 0x00000010),
    "right_control": (62, 0x00002000),
    "right_shift": (60, 0x00000004),
    "escape": (53, None),
}

# CGEventType values (CGEventTypes.h).
KEY_DOWN, KEY_UP, FLAGS_CHANGED = 10, 11, 12
TAP_DISABLED_BY_TIMEOUT, TAP_DISABLED_BY_USER_INPUT = 0xFFFFFFFE, 0xFFFFFFFF

PRESS, RELEASE, CANCEL, CHORD = "press", "release", "cancel", "chord"


def resolve(name: str) -> tuple[int, int | None]:
    try:
        return KEYS[name]
    except KeyError:
        raise ValueError(f"unknown key {name!r}; known: {', '.join(sorted(KEYS))}") from None


class TalkKey:
    def __init__(self, talk: str, cancel: str) -> None:
        self.talk_code, self.talk_bit = resolve(talk)
        if self.talk_bit is None:
            raise ValueError(f"hotkey.push_to_talk {talk!r} must be a modifier key")
        self.cancel_code, _ = resolve(cancel)
        self.held = False
        self.aborted = False
        self._esc_cancelled = False
        self._swallow_up: set[int] = set()

    def force_release(self) -> str | None:
        """End a hold whose key-up may never arrive: `audio.max_hold_s` expired, or macOS
        disabled the tap mid-hold (the key-up is then lost). A live hold becomes RELEASE —
        the audio so far is processed, not dropped; the late real key-up is then ignored."""
        if not self.held:
            return None
        self.held = False
        return None if self.aborted else RELEASE

    def handle(self, etype: int, keycode: int, flags: int) -> tuple[str | None, bool]:
        """-> (action or None, swallow the event?)"""
        if etype == FLAGS_CHANGED and keycode == self.talk_code:
            down = bool(flags & self.talk_bit)
            if down and not self.held:
                self.held, self.aborted, self._esc_cancelled = True, False, False
                return PRESS, False
            if not down and self.held:
                self.held = False
                return (None if self.aborted else RELEASE), False
            return None, False
        if etype == KEY_DOWN and self.held:
            if keycode == self.cancel_code and (not self.aborted or self._esc_cancelled):
                first = not self.aborted
                self.aborted = self._esc_cancelled = True
                self._swallow_up.add(keycode)
                return (CANCEL if first else None), True  # later ones: Esc auto-repeat
            if not self.aborted:
                self.aborted = True
                return CHORD, False
            return None, False
        if etype == KEY_UP and keycode in self._swallow_up:
            self._swallow_up.discard(keycode)
            return None, True
        return None, False


class HotkeyTap:
    """Installs the tap on the current (main) run loop. The callback must stay tiny: macOS
    disables a tap whose callback is slow, so it only classifies and hands off."""

    def __init__(self, cfg, on_action: Callable[[str], None]) -> None:
        if str(cfg.require("hotkey.mechanism")) != "cgeventtap":
            raise ValueError("hotkey.mechanism must be cgeventtap (ADR-002)")
        self.state = TalkKey(str(cfg.require("hotkey.push_to_talk")),
                             str(cfg.require("hotkey.cancel")))
        self.on_action = on_action
        self.tap = None
        self._source = None

    def install(self) -> bool:
        import Quartz as Q

        mask = (Q.CGEventMaskBit(Q.kCGEventKeyDown) | Q.CGEventMaskBit(Q.kCGEventKeyUp)
                | Q.CGEventMaskBit(Q.kCGEventFlagsChanged))
        # An active (not listen-only) tap: it has to be able to swallow the cancelling Esc.
        self.tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap,
                                      Q.kCGEventTapOptionDefault, mask, self._callback, None)
        if self.tap is None:
            return False
        self._source = Q.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        Q.CFRunLoopAddSource(Q.CFRunLoopGetCurrent(), self._source, Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(self.tap, True)
        return True

    def enabled(self) -> bool:
        import Quartz as Q

        return bool(self.tap is not None and Q.CGEventTapIsEnabled(self.tap))

    def _callback(self, proxy, etype, event, refcon):
        import Quartz as Q

        try:
            if etype in (TAP_DISABLED_BY_TIMEOUT, TAP_DISABLED_BY_USER_INPUT):
                log.warning("hotkey: tap disabled by the system (%#x) — re-enabling", etype)
                Q.CGEventTapEnable(self.tap, True)
                if self.state.force_release() == RELEASE:  # the key-up may be gone
                    self.on_action(RELEASE)
                return event
            if Q.CGEventGetIntegerValueField(event, Q.kCGEventSourceUserData) == DICT8_EVENT_TAG:
                return event  # our own injected keystroke, not the user's
            code = Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventKeycode)
            action, swallow = self.state.handle(int(etype), int(code),
                                                int(Q.CGEventGetFlags(event)))
            if action:
                self.on_action(action)
            return None if swallow else event
        except Exception as exc:  # never let the tap die with an exception in it
            log.error("hotkey: callback error %r", exc)
            return event
