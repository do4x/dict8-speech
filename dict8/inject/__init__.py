"""Put the transcript into the frontmost app, and never lose it on the way.

Three paths, in order (config.yml `injection.*`):

1. `cgevent_unicode` — `CGEventKeyboardSetUnicodeString` + `CGEventPost`, one key-down/up
   pair per chunk of at most `injection.chunk_chars` UTF-16 units, `inter_chunk_delay_ms`
   apart. Bypasses the keymap, so curly quotes, em dashes and accents type as themselves.
2. `pasteboard_paste` — the previous pasteboard is snapshotted, the transcript written,
   a synthetic Cmd+V posted, and the snapshot put back `pasteboard_restore_delay_ms` later
   (only if nothing else has written the pasteboard in between).
3. `pasteboard_only` — the transcript is left on the pasteboard and a toast says so. Used
   when posting cannot work at all: Accessibility missing (CGEventPost no-ops silently
   without it), Secure Input on in another app (keystrokes vanish, no error), or the
   frontmost app changed between press and inject.

Invariant 1: `prepare()` strips leading/trailing whitespace and touches nothing else. There
is no other transformation anywhere on this path.

Invariant 7b: every degrade to `pasteboard_only` carries a toast naming why. The caller
shows it; this module only decides and says.

OS calls are behind small adapters (`Pasteboard`, `EventPoster`, `secure_input_enabled`,
`accessibility_trusted`) so the unit tests drive the real decision logic with fakes for the
OS and nothing else.
"""

from __future__ import annotations

import logging
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

PATH_CGEVENT = "cgevent_unicode"
PATH_PASTE = "pasteboard_paste"
PATH_PB_ONLY = "pasteboard_only"
PATH_NONE = "none"
PATH_FAILED = "failed"

# Stamped into kCGEventSourceUserData on every event Dict8 posts, so Dict8's own hotkey tap
# can tell its injected keystrokes from the user's (a talk-key press during a type-out must
# not be cancelled as a "chord" by our own key events). An identifier, not a tunable.
DICT8_EVENT_TAG = 0x44384438  # "D8D8"


def has_control_chars(text: str) -> bool:
    """CR, LF, tab and the rest of Unicode category Cc. Posted as key events these are
    *keys*: a newline typed into Claude Code submits a half-finished prompt, a tab hits
    its completion binding. Pasted (bracketed paste) they are inert text."""
    return any(unicodedata.category(ch) == "Cc" for ch in text)


def prepare(text: str) -> str:
    """The only edit the raw path makes: leading/trailing whitespace. Invariant 1."""
    return text.strip()


def _units(s: str) -> int:
    """Length in UTF-16 code units — what CGEventKeyboardSetUnicodeString counts."""
    return len(s.encode("utf-16-le")) // 2


def chunk_utf16(text: str, max_units: int) -> list[str]:
    """Split into pieces of at most `max_units` UTF-16 units, never inside a character.

    A non-BMP character (emoji) is two units and is never split; a combining mark is kept
    with its base character, so "e" + U+0301 does not arrive as two events — unless one
    grapheme is itself longer than the cap, where the cap wins. No chunk ever exceeds
    `max_units`. Joining the result gives back `text` exactly.
    """
    if max_units < 2:
        raise ValueError("chunk size must be at least 2 UTF-16 units (one surrogate pair)")
    chunks: list[str] = []
    cur = ""
    cur_units = 0
    for ch in text:
        u = _units(ch)
        if cur and cur_units + u > max_units:
            if unicodedata.combining(ch):
                # Move the whole grapheme (base + marks so far) to the next chunk...
                i = len(cur)
                while i > 0 and unicodedata.combining(cur[i - 1]):
                    i -= 1
                i -= 1
                if i > 0:
                    chunks.append(cur[:i])
                    cur = cur[i:]
                    cur_units = _units(cur)
                # ...unless the grapheme alone is over the cap: then the cap wins.
                if cur_units + u > max_units:
                    chunks.append(cur)
                    cur, cur_units = "", 0
            else:
                chunks.append(cur)
                cur, cur_units = "", 0
        cur += ch
        cur_units += u
    if cur:
        chunks.append(cur)
    return chunks


# ---- OS adapters ---------------------------------------------------------------------


def secure_input_enabled() -> bool:
    """Carbon's IsSecureEventInputEnabled — true while any app holds Secure Input
    (a password field, some terminals' "Secure Keyboard Entry"). No permission needed."""
    import ctypes
    import ctypes.util

    lib = ctypes.CDLL(ctypes.util.find_library("Carbon"))
    lib.IsSecureEventInputEnabled.restype = ctypes.c_bool
    return bool(lib.IsSecureEventInputEnabled())


def accessibility_trusted() -> bool:
    """AXIsProcessTrusted — never prompts. Re-read every call (grants are revocable)."""
    from dict8.permissions import accessibility_granted

    return accessibility_granted()


class Pasteboard:
    """NSPasteboard adapter: snapshot/restore every item and type, text in/out."""

    def __init__(self, pb=None) -> None:
        from AppKit import NSPasteboard

        self.pb = pb if pb is not None else NSPasteboard.generalPasteboard()

    def change_count(self) -> int:
        return int(self.pb.changeCount())

    def snapshot(self) -> list[dict[str, bytes]]:
        items = []
        for item in self.pb.pasteboardItems() or []:
            d: dict[str, bytes] = {}
            for t in item.types() or []:
                data = item.dataForType_(t)
                if data is not None:
                    d[str(t)] = bytes(data)
            items.append(d)
        return items

    def restore(self, snap: list[dict[str, bytes]]) -> None:
        from AppKit import NSPasteboardItem
        from Foundation import NSData

        self.pb.clearContents()
        objs = []
        for d in snap:
            it = NSPasteboardItem.alloc().init()
            for t, b in d.items():
                it.setData_forType_(NSData.dataWithBytes_length_(b, len(b)), t)
            objs.append(it)
        if objs:
            self.pb.writeObjects_(objs)

    def set_text(self, text: str) -> None:
        from AppKit import NSPasteboardTypeString

        self.pb.clearContents()
        if not self.pb.setString_forType_(text, NSPasteboardTypeString):
            raise RuntimeError("NSPasteboard refused the string")

    def get_text(self) -> str | None:
        from AppKit import NSPasteboardTypeString

        s = self.pb.stringForType_(NSPasteboardTypeString)
        return None if s is None else str(s)


class EventPoster:
    """CGEventPost adapter. Needs Accessibility; without it every post is a silent no-op,
    which is why the Injector checks the grant before choosing a posting path."""

    # kVK_ANSI_V. The one keycode this module needs: Cmd+V for the paste fallback.
    KEY_V = 9

    def __init__(self) -> None:
        import Quartz

        self.Q = Quartz

    def _post(self, ev) -> None:
        if ev is None:
            raise RuntimeError("CGEventCreateKeyboardEvent returned NULL")
        self.Q.CGEventSetIntegerValueField(ev, self.Q.kCGEventSourceUserData, DICT8_EVENT_TAG)
        self.Q.CGEventPost(self.Q.kCGHIDEventTap, ev)

    def type_chunk(self, chunk: str) -> None:
        Q = self.Q
        n = _units(chunk)
        for down in (True, False):
            ev = Q.CGEventCreateKeyboardEvent(None, 0, down)
            if ev is None:
                raise RuntimeError("CGEventCreateKeyboardEvent returned NULL")
            # No modifier may leak in from the physical keyboard (the talk key is Option).
            Q.CGEventSetFlags(ev, 0)
            Q.CGEventKeyboardSetUnicodeString(ev, n, chunk)
            self._post(ev)

    def cmd_v(self) -> None:
        Q = self.Q
        for down in (True, False):
            ev = Q.CGEventCreateKeyboardEvent(None, self.KEY_V, down)
            if ev is None:
                raise RuntimeError("CGEventCreateKeyboardEvent returned NULL")
            Q.CGEventSetFlags(ev, Q.kCGEventFlagMaskCommand)
            self._post(ev)


# ---- the decision --------------------------------------------------------------------


@dataclass
class InjectResult:
    path: str
    ms: float
    chars: int
    toast: tuple[str, str] | None = None     # (title, body) the caller must show
    notes: list[str] = field(default_factory=list)


class Injector:
    def __init__(self, cfg, *, pasteboard=None, poster=None,
                 secure_input: Callable[[], bool] | None = None,
                 ax_trusted: Callable[[], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.chunk_chars = int(cfg.require("injection.chunk_chars"))
        self.delay_s = float(cfg.require("injection.inter_chunk_delay_ms")) / 1000
        self.over_chars = int(cfg.require("injection.fallback_trigger.over_chars"))
        self.restore_delay_s = float(cfg.require("injection.pasteboard_restore_delay_ms")) / 1000
        self.restore = bool(cfg.require("injection.restore_pasteboard"))
        self.check_secure = bool(cfg.require("injection.secure_input_check"))
        self._pb = pasteboard
        self._poster = poster
        self._secure = secure_input or secure_input_enabled
        self._ax = ax_trusted or accessibility_trusted
        self._sleep = sleep

    @property
    def pb(self):
        if self._pb is None:
            self._pb = Pasteboard()
        return self._pb

    @property
    def poster(self):
        if self._poster is None:
            self._poster = EventPoster()
        return self._poster

    def warm(self) -> None:
        """Build both OS adapters now (AppKit/Quartz imports), so the first dictation does
        not pay ~40 ms of import inside release-to-text."""
        _ = self.pb, self.poster

    # -- the three paths ---------------------------------------------------------------

    def _cgevent(self, text: str) -> None:
        for i, chunk in enumerate(chunk_utf16(text, self.chunk_chars)):
            if i:
                self._sleep(self.delay_s)
            self.poster.type_chunk(chunk)

    def _paste(self, text: str) -> None:
        snap = self.pb.snapshot() if self.restore else None
        self.pb.set_text(text)
        ours = self.pb.change_count()
        self.poster.cmd_v()  # an exception here leaves the text on the pasteboard
        if snap is None:
            return
        # The target app reads the pasteboard asynchronously after Cmd+V; restoring too
        # early pastes the OLD contents. And if anything else wrote the pasteboard in the
        # meantime, that write wins — restoring over it would lose someone else's copy.
        self._sleep(self.restore_delay_s)
        if self.pb.change_count() == ours:
            self.pb.restore(snap)

    def pasteboard_only(self, text: str, why_title: str, why_body: str) -> InjectResult:
        t0 = time.perf_counter()
        try:
            self.pb.set_text(text)
        except Exception as exc:
            # The last resort failed too. The caller still holds the text in memory (menu:
            # "Copy last transcript"); it is never written to disk or to a log.
            log.error("inject: pasteboard write failed (%s)", exc)
            return InjectResult(PATH_FAILED, (time.perf_counter() - t0) * 1000, len(text),
                                toast=(why_title, f"{why_body} The clipboard write failed "
                                                  f"too — use the menu's Copy last "
                                                  f"transcript."))
        return InjectResult(PATH_PB_ONLY, (time.perf_counter() - t0) * 1000, len(text),
                            toast=(why_title, f"{why_body} The transcript is on the "
                                              f"clipboard — press ⌘V."))

    def inject(self, raw: str, *, force: str | None = None) -> InjectResult:
        """Deliver `prepare(raw)`. `force` pins one path (`dictate --inject`)."""
        text = prepare(raw)
        if not text:
            return InjectResult(PATH_NONE, 0.0, 0)
        if force == PATH_NONE:
            return InjectResult(PATH_NONE, 0.0, len(text))
        if force == PATH_PB_ONLY:
            r = self.pasteboard_only(text, "Dict8: pasteboard only",
                                     "Injection was set to pasteboard-only.")
            r.toast = None  # asked for explicitly: nothing to warn about
            return r

        # The pre-checks are OS calls too. If one raises, we cannot know whether typing
        # would land, so the text goes to the pasteboard — never up the stack (U5 fix 1).
        try:
            secure = self.check_secure and self._secure()
            trusted = self._ax()
        except Exception as exc:
            log.warning("inject: pre-check raised (%r) — pasteboard only", exc)
            return self.pasteboard_only(
                text, "Dict8: could not check before typing",
                f"A system check failed ({type(exc).__name__}), so nothing was typed.")
        if secure:
            return self.pasteboard_only(
                text, "Dict8: Secure Input is on",
                "Another app has Secure Input on (a password field or Secure Keyboard "
                "Entry), so typed keystrokes would vanish.")
        if not trusted:
            from dict8.permissions import toast_text
            title, body = toast_text("accessibility")
            return self.pasteboard_only(text, title, body)

        notes: list[str] = []
        t0 = time.perf_counter()
        path = PATH_CGEVENT
        if len(text) > self.over_chars:
            path = PATH_PASTE
            notes.append(f"{len(text)} chars > fallback_trigger.over_chars={self.over_chars}")
        elif has_control_chars(text):
            path = PATH_PASTE
            notes.append("control characters: pasted so they stay text, not keys")
        if path == PATH_CGEVENT:
            try:
                self._cgevent(text)
                return InjectResult(PATH_CGEVENT, (time.perf_counter() - t0) * 1000,
                                    len(text), notes=notes)
            except Exception as exc:
                # A partial type-out may already be on screen; pasting the whole text on
                # top of it duplicates the head, which is recoverable. Losing it is not.
                log.warning("inject: cgevent path failed (%s) — falling back to paste", exc)
                notes.append(f"cgevent failed: {type(exc).__name__}")
                path = PATH_PASTE
        try:
            self._paste(text)
            return InjectResult(PATH_PASTE, (time.perf_counter() - t0) * 1000, len(text),
                                notes=notes)
        except Exception as exc:
            log.warning("inject: paste path failed (%s) — pasteboard only", exc)
            notes.append(f"paste failed: {type(exc).__name__}")
            r = self.pasteboard_only(text, "Dict8: could not type or paste",
                                     f"Both injection paths failed ({type(exc).__name__}).")
            r.notes = notes
            return r
