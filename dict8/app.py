"""`dict8 app` — the menu-bar process (ADR-001: one Python process, PyObjC, model warm).

Threads:
- main: the Cocoa run loop — status item, event tap callback, timers. Nothing slow runs
  here; the tap callback only classifies the key and enqueues.
- worker: one thread, one queue. Mic start/stop, STT, injection, the latency row, in the
  order the keys happened. One dictation at a time by construction.
- loader: loads the STT model once at launch, then exits.

Permissions (invariant 7b) are preflighted at launch, on every press, and on a
`permissions.poll_interval_s` timer, so a grant revoked mid-session produces a toast rather
than a tap that silently stops seeing keys. Launch never prompts: the tap is only created
once both Accessibility and Input Monitoring preflight as granted, and the microphone is
only opened by a press of the talk key.

The last transcript is kept in memory only (menu › Copy last transcript) so that even the
case where the pasteboard write itself fails does not lose a prompt. It is never written
anywhere.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time

from dict8 import dictation, permissions
from dict8.audio import MicUnavailable, Recorder
from dict8.hotkey import CANCEL, CHORD, PRESS, RELEASE, HotkeyTap
from dict8.inject import PATH_PB_ONLY, Injector
from dict8.stt import STT
from dict8.ui import toast as toast_mod

log = logging.getLogger(__name__)


def say(msg: str) -> None:
    """Launch/status lines on stdout, for the terminal Dict8 runs in. Never transcript text."""
    print(f"dict8 app: {msg}", flush=True)


def frontmost_pid() -> int | None:
    from AppKit import NSWorkspace

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(app.processIdentifier()) if app is not None else None


class Controller:
    def __init__(self, cfg, *, dry_run: bool = False) -> None:
        from PyObjCTools import AppHelper

        self.cfg = cfg
        self.dry_run = dry_run
        self.call_main = AppHelper.callAfter
        self.stt = STT(cfg)
        self.injector = Injector(cfg)
        self.recorder: Recorder | None = None
        self.ready = False
        self.last_text: str | None = None
        self.q: queue.Queue = queue.Queue()
        self.tray = None
        self.tap: HotkeyTap | None = None
        self.perm_states: dict[str, str] = {}
        self._press_front: int | None = None
        self._recording = False
        self._poll_s = float(cfg.require("permissions.poll_interval_s"))
        self._max_hold_s = float(cfg.require("audio.max_hold_s"))
        self._hold_id = 0
        self._last_tap_note = ""
        self._tap_dead_toasted = False

    # -- UI helpers (thread-safe) -------------------------------------------------------

    def ui(self, state: str, detail: str = "") -> None:
        if self.tray is not None:
            self.call_main(self.tray.set_state, state, detail)

    def toast(self, title: str, body: str) -> None:
        toast_mod.toast(title, body)

    # -- launch ------------------------------------------------------------------------

    def start(self, exit_after: float | None = None) -> None:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        from dict8.ui.tray import Tray

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon

        mic_label = (str(self.cfg.get("hardware.mic_device"))
                     if self.cfg.get("hardware.mic_device")
                     else "system default input (hardware.mic_device is TBD)")
        hotkey_label = (f"hold {self.cfg.require('hotkey.push_to_talk')}, "
                        f"{self.cfg.require('hotkey.cancel')} cancels")
        self.tray = Tray({"check_permissions": self.check_permissions,
                          "copy_last": self.copy_last, "quit": self.quit},
                         hotkey_label=hotkey_label, mic_label=mic_label)
        toast_mod.add_listener(lambda t, b: self.call_main(self.tray.set_error, t))
        toast_mod.add_listener(lambda t, b: say(f"toast: {t} — {b}"))
        say(f"status item created, title {self.tray.title!r} (pid {os.getpid()}, "
            f"host {permissions.host_app()}{', DRY RUN: mic never opened' if self.dry_run else ''})")

        self.refresh_permissions(announce=True)
        self._install_tap()
        threading.Thread(target=self._worker, name="dict8-worker", daemon=True).start()
        threading.Thread(target=self._load, name="dict8-stt-load", daemon=True).start()
        self._schedule(self._poll_s, self._poll, repeat=True)
        if exit_after:
            self._schedule(exit_after, self.quit, repeat=False)

    def _schedule(self, seconds: float, fn, *, repeat: bool) -> None:
        from Foundation import NSTimer

        def tick(_timer):
            fn()
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(seconds, repeat, tick)

    def _load(self) -> None:
        try:
            ms = self.stt.load()
            self.injector.warm()
            self.recorder = Recorder(self.cfg, self.stt.sample_rate)
            self.ready = True
            say(f"stt ready: {self.stt.model_id} loaded + warmed in {ms:.0f} ms")
            self.ui("idle")
        except Exception as exc:
            say(f"stt FAILED to load: {exc}")
            self.toast("Dict8: speech model failed to load", f"{type(exc).__name__}: {exc}")
            self.ui("error", "STT not loaded")

    def _note_tap(self, msg: str) -> None:
        if msg != self._last_tap_note:  # the poll re-tries; say it once, not every tick
            self._last_tap_note = msg
            say(msg)

    def _install_tap(self) -> None:
        if self.tap is not None:
            return
        need = [g for g in ("accessibility", "input_monitoring")
                if self.perm_states.get(g) != permissions.GRANTED]
        if need:
            self._note_tap(f"hotkey NOT installed — missing: {', '.join(need)} (no prompt "
                           f"shown; menu › Check permissions asks)")
            return
        tap = HotkeyTap(self.cfg, self.on_action)
        if tap.install():
            self.tap = tap
            self._note_tap(f"hotkey installed: CGEventTap, hold "
                           f"{self.cfg.require('hotkey.push_to_talk')}")
        elif self._last_tap_note != "tap-null":
            self._note_tap("hotkey NOT installed — CGEventTapCreate returned NULL")
            self._last_tap_note = "tap-null"
            self.toast("Dict8: talk key unavailable",
                       "macOS refused the keyboard tap. Check Accessibility and Input "
                       "Monitoring (menu › Check permissions).")

    # -- permissions -------------------------------------------------------------------

    def refresh_permissions(self, *, announce: bool = False) -> list[str]:
        prev = self.perm_states
        states = permissions.check()
        self.perm_states = states
        if self.tray is not None:
            self.call_main(self.tray.set_permissions, states)
        gone = permissions.missing(states)
        if announce:
            say("permissions: " + ", ".join(f"{k}={v}" for k, v in states.items()))
        for g in gone:
            if announce or prev.get(g) == permissions.GRANTED:
                self.toast(*permissions.toast_text(g, state=states.get(g)))
        return gone

    def _poll(self) -> None:
        self.refresh_permissions()
        if self.tap is None:
            self._install_tap()
        elif not self.tap.enabled() and not self._tap_dead_toasted:
            self._tap_dead_toasted = True
            self.toast("Dict8: talk key stopped working",
                       "macOS disabled the keyboard tap — usually a revoked Accessibility or "
                       "Input Monitoring grant.")

    def check_permissions(self) -> None:
        """Menu action — user-initiated, so this is where prompts and Settings panes open."""
        gone = self.refresh_permissions()
        for g in gone:
            try:
                permissions.request(g)
                permissions.open_settings(self.cfg, g)
            except Exception as exc:
                log.warning("permissions: could not request/open %s (%s)", g, exc)
            self.toast(*permissions.toast_text(g, state=self.perm_states.get(g)))
        if not gone:
            self.toast("Dict8: permissions OK", "Microphone, Accessibility and Input "
                                                "Monitoring are all granted.")
            self._install_tap()

    # -- actions -----------------------------------------------------------------------

    def on_action(self, action: str) -> None:
        """Main thread, from the tap callback: stamp and enqueue, nothing else."""
        t = time.perf_counter()
        front = frontmost_pid() if action == PRESS else None
        self.q.put((action, t, front))

    def _worker(self) -> None:
        while True:
            action, t, front = self.q.get()
            try:
                if self.dry_run:
                    say(f"hotkey {action} (dry run)")
                elif action == PRESS:
                    self._press(front)
                elif action == RELEASE:
                    self._release(t)
                elif action in (CANCEL, CHORD):
                    self._abort(action)
            except Exception as exc:
                log.error("dictation: %r", exc)
                self.toast("Dict8: dictation failed", f"{type(exc).__name__}: {exc}")
                self._recording = False
                self.ui("error", type(exc).__name__)

    def _press(self, front: int | None) -> None:
        if not self.ready or self.recorder is None:
            self.toast("Dict8: not ready yet", "The speech model is still loading.")
            return
        self.refresh_permissions()  # every dictation re-checks (7b)
        if self.perm_states.get("microphone") in ("denied", "restricted"):
            self.toast(*permissions.toast_text("microphone"))
            self.ui("error", "microphone")
            return
        self._press_front = front
        try:
            self.recorder.start()  # first-ever start shows the Microphone prompt
        except MicUnavailable as exc:
            self.toast("Dict8: microphone unavailable", str(exc))
            self.ui("error", "mic")
            return
        self._recording = True
        self._hold_id += 1
        hold = self._hold_id
        timer = threading.Timer(self._max_hold_s,
                                lambda: self.call_main(self._hold_expired, hold))
        timer.daemon = True
        timer.start()
        self.ui("recording")

    def _hold_expired(self, hold: int) -> None:
        """Main thread. `audio.max_hold_s` passed with no key-up: a lost key-up must not
        leave the mic open. The hold is ended and what was recorded is processed."""
        if hold != self._hold_id or not self._recording:
            return
        say(f"max hold ({self._max_hold_s:.0f} s) reached — ending the recording")
        if self.tap is not None:
            self.tap.state.force_release()
        self.q.put((RELEASE, time.perf_counter(), None))

    def _release(self, t_release: float) -> None:
        if not self._recording or self.recorder is None:
            return
        self._recording = False
        audio, audio_ms = self.recorder.stop()
        if audio is None:
            dictation.record(self.cfg, {"source": "hotkey", "outcome": "discarded_short",
                                        "audio_ms": audio_ms})
            self.ui("idle")
            return
        self.ui("transcribing")
        front = self._press_front
        out = dictation.process(self.cfg, self.stt, self.injector, audio,
                                audio_ms=audio_ms, t_release=t_release, source="hotkey",
                                frontmost_changed=lambda: frontmost_pid() != front,
                                on_text=lambda t: setattr(self, "last_text", t))
        if out.result.toast:
            self.toast(*out.result.toast)
        say(f"dictation: audio {audio_ms:.0f} ms, stt {out.stt_ms:.0f} ms, inject "
            f"{out.result.ms:.0f} ms via {out.result.path}, release->text "
            f"{out.release_to_text_ms:.0f} ms, {out.result.chars} chars")
        # A fallback is never only a banner (Notification Center may hide it): the title
        # keeps saying so until the next press.
        if out.result.path == PATH_PB_ONLY:
            self.ui("fallback", "transcript on the clipboard — press ⌘V")
        elif out.result.toast:
            self.ui("error", out.result.toast[0].removeprefix("Dict8: "))
        else:
            self.ui("idle")

    def _abort(self, action: str) -> None:
        if self.recorder is not None:
            self.recorder.cancel()
        if self._recording:
            dictation.record(self.cfg, {"source": "hotkey", "outcome": "cancelled"})
        self._recording = False
        self.ui("idle", "cancelled" if action == CANCEL else "")

    def copy_last(self) -> None:
        if self.last_text:
            self.injector.pb.set_text(self.last_text)

    def quit(self) -> None:
        from AppKit import NSApplication

        if self.recorder is not None:
            self.recorder.cancel()
        say(f"quitting (status item title {self.tray.title!r})" if self.tray else "quitting")
        NSApplication.sharedApplication().terminate_(None)


def run(cfg, *, exit_after: float | None = None, dry_run: bool = False,
        notify: bool = True) -> int:
    from PyObjCTools import AppHelper

    toast_mod.notify = notify
    ctl = Controller(cfg, dry_run=dry_run)
    ctl.start(exit_after=exit_after)
    AppHelper.runEventLoop(installInterrupt=True)
    return 0
