"""`dict8 app` — the menu-bar process (ADR-001: one Python process, PyObjC, model warm).

Threads:
- main: the Cocoa run loop — status item, event tap callback, timers. Nothing slow runs
  here; the tap callback only classifies the key and enqueues.
- worker: one thread, one queue. Mic start/stop, STT, injection, the latency row, in the
  order the keys happened. One dictation at a time by construction.
- loader: loads the STT model once at launch, then exits.
- classifier-loader: warms the resident classifier (`classify` extra) in parallel, then exits.
- advisor (U6): classify + recommend + estimate, AFTER injection returned. It never holds
  the text: a slow or dead classifier means no chip, never a delayed or lost prompt
  (invariant 2).
- meter (U6): every `cli.tail_interval_s`, one incremental scan (dict8.usage.meter) and the
  menu-bar meter; owns the estimate-vs-actual rows. Its own SQLite connection.

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

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dict8 import dictation, permissions
from dict8.advise import recommend as rec_mod
from dict8.audio import MicUnavailable, Recorder
from dict8.hotkey import CANCEL, CHORD, PRESS, RELEASE, HotkeyTap
from dict8.inject import PATH_CGEVENT, PATH_FAILED, PATH_NONE, PATH_PASTE, PATH_PB_ONLY, Injector
from dict8.stt import STT
from dict8.ui import toast as toast_mod
from dict8.usage.meter import fmt_tokens

log = logging.getLogger(__name__)

DEMO = "demo"                   # a queued `--demo` text, run through dictation.deliver()
# `--demo` pacing only: how long the demo thread lets the run loop draw before it checks the
# panel, and the gap between demo texts so each overlay can be seen. Never on a real
# dictation's path.
DEMO_SETTLE_S = 0.3
DEMO_PAUSE_S = 0.8


@dataclass
class AdviceJob:
    seq: int                    # which dictation; a newer press makes this one stale
    text: str                   # the injected text — memory only, never logged
    words: int
    override: str | None
    t_release: float            # perf_counter at key-up (demo: text handed over)
    t_injected: float           # perf_counter when deliver() returned
    source: str


def estimate_line(est) -> str:
    if est is None:
        return "est. — estimator unavailable"
    if est.low is None or est.high is None:
        return f"est. — not enough similar history (n={est.n})"
    tail = " · extrapolated" if est.ood else ""
    return (f"est. {fmt_tokens(est.low)}–{fmt_tokens(est.high)} tokens · n={est.n} · "
            f"{est.group}{tail}")


def frontmost_app() -> str:
    from AppKit import NSWorkspace

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return str(app.bundleIdentifier()) if app is not None else "none"


def release_wall_time(t_release: float) -> datetime:
    """The wall-clock instant of a perf_counter stamp taken moments ago."""
    return datetime.now(timezone.utc) - timedelta(seconds=time.perf_counter() - t_release)


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
        # U6: overlay, advisor, meter
        self.overlay = None
        self.classifier = None
        self.classifier_ready = threading.Event()
        self.advice_q: queue.Queue = queue.Queue()
        self.meter_q: queue.Queue = queue.Queue()
        self.meter_stopped = threading.Event()
        self._seq = 0
        self._chip_events: dict[int, threading.Event] = {}
        self.max_scan_ms = 0.0
        self.max_tx_ms = 0.0

    # -- UI helpers (thread-safe) -------------------------------------------------------

    def ui(self, state: str, detail: str = "") -> None:
        if self.tray is not None:
            self.call_main(self.tray.set_state, state, detail)

    def toast(self, title: str, body: str) -> None:
        toast_mod.toast(title, body)

    # -- launch ------------------------------------------------------------------------

    def start(self, exit_after: float | None = None, demo: list[str] | None = None,
              snapshot: str | None = None) -> None:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        from dict8.ui.overlay import Overlay
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

        self.overlay = Overlay(self.cfg)
        self.refresh_permissions(announce=True)
        self._install_tap()
        threading.Thread(target=self._worker, name="dict8-worker", daemon=True).start()
        threading.Thread(target=self._load, name="dict8-stt-load", daemon=True).start()
        threading.Thread(target=self._load_classifier, name="dict8-classifier-load",
                         daemon=True).start()
        threading.Thread(target=self._advisor, name="dict8-advisor", daemon=True).start()
        threading.Thread(target=self._meter_loop, name="dict8-meter", daemon=True).start()
        self._schedule(self._poll_s, self._poll, repeat=True)
        if demo:
            threading.Thread(target=self._run_demo, args=(demo, snapshot),
                             name="dict8-demo", daemon=True).start()
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

    def _load_classifier(self) -> None:
        """Warm the resident classifier. Missing extra or a failed load: one line, no
        chip for the session — never an error in the dictation path (invariant 2)."""
        try:
            from dict8.advise.classifier import (MISSING_BACKEND_HINT, Classifier,
                                                 backend_available)
            if not backend_available():
                say(f"classifier unavailable — no model chip this session: "
                    f"{MISSING_BACKEND_HINT}")
                return
            t0 = time.perf_counter()
            clf = Classifier(self.cfg)
            clf.warm()
            self.classifier = clf
            say(f"classifier ready: {clf.model_id} warmed in "
                f"{(time.perf_counter() - t0) * 1000:.0f} ms")
        except Exception as exc:
            say(f"classifier failed to load — no model chip this session ({exc!r})")
        finally:
            self.classifier_ready.set()

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
                if action == DEMO:
                    self._demo_one(t, front)
                elif self.dry_run:
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
                self._overlay_state("error")
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
        # U6: a new dictation — any advice still in flight for the last one is stale, and
        # the last one's estimate-vs-actual window ends here.
        self._seq += 1
        self.meter_q.put(("close", datetime.now(timezone.utc), "next_dictation"))
        self._overlay_state("recording", clear=True)
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
            if self.overlay is not None:
                self.call_main(self.overlay.hide)
            return
        self.ui("transcribing")
        self._overlay_state("transcribing")
        front = self._press_front
        out = dictation.process(self.cfg, self.stt, self.injector, audio,
                                audio_ms=audio_ms, t_release=t_release, source="hotkey",
                                frontmost_changed=lambda: frontmost_pid() != front,
                                on_text=lambda t: setattr(self, "last_text", t))
        self._after_delivery(out, t_release, "hotkey")
        if out.result.toast:
            self.toast(*out.result.toast)
        say(f"dictation: audio {audio_ms:.0f} ms, stt {out.stt_ms:.0f} ms, inject "
            f"{out.result.ms:.0f} ms via {out.result.path}, release->text "
            f"{out.release_to_text_ms:.0f} ms, {out.result.chars} chars"
            + (f", commands {','.join(out.voice.commands)}" if out.voice and out.voice.commands
               else ""))
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
            self._overlay_state("cancelled", clear=True)
        self._recording = False
        self.ui("idle", "cancelled" if action == CANCEL else "")

    # -- U6: overlay, advice, meter ----------------------------------------------------

    def _overlay_state(self, state: str, *, clear: bool = False, override: str | None = None,
                       pending: bool = False) -> None:
        def show():
            if self.overlay is None:
                return
            if clear:
                self.overlay.clear_advice()
            if pending or override:
                self.overlay.set_advice(chip=None, strength=None,
                                        estimate="est. …" if pending else None,
                                        override=override)
            self.overlay.show_state(state)
        self.call_main(show)

    def _after_delivery(self, out, t_release: float, source: str) -> None:
        """Worker thread, after deliver() returned: the text has landed (or not) and nothing
        below can change that. Updates the overlay and queues the advisory work."""
        t_injected = time.perf_counter()
        v = out.voice
        path = out.result.path
        if v is not None and v.cancel:
            state = "cancelled"
        elif path in (PATH_CGEVENT, PATH_PASTE):
            state = "sent" if out.submitted else ("typed" if path == PATH_CGEVENT else "pasted")
        elif path == PATH_PB_ONLY:
            state = "clipboard"
        elif path == PATH_FAILED:
            state = "error"
        elif path == PATH_NONE and out.submitted:
            state = "sent"
        elif path == PATH_NONE and v is not None and v.text:
            state = DEMO if source == DEMO else "typed"
        else:
            state = "nothing"
        override = None
        if v is not None and v.override:
            entry = rec_mod.by_id(self.cfg, v.override)
            override = f"override: {entry.label if entry else v.override}"
        text = v.text if (v is not None and not v.cancel) else ""
        advise = bool(text) and path != PATH_FAILED
        self._overlay_state(state, clear=True, override=override, pending=advise)
        if not advise:
            return
        seq = self._seq
        self.meter_q.put(("open", seq, release_wall_time(t_release), len(text.split()), source))
        self.advice_q.put(AdviceJob(seq=seq, text=text, words=len(text.split()),
                                    override=v.override, t_release=t_release,
                                    t_injected=t_injected, source=source))

    def _advisor(self) -> None:
        from dict8.advise import estimator as est_mod
        from dict8.usage.store import Store

        store = None
        while True:
            job: AdviceJob = self.advice_q.get()
            t0 = time.perf_counter()
            bucket, cls_ms = None, None
            if self.classifier is not None:
                try:
                    r = self.classifier.classify(job.text)   # enforces classifier.timeout_ms
                    if r is not None:
                        bucket, cls_ms = r.bucket, r.latency_ms
                except Exception as exc:
                    log.warning("advisor: classify raised (%r) — no chip", exc)
            t_cls = time.perf_counter()
            rec = rec_mod.recommend(self.cfg, bucket)
            est = None
            try:
                if store is None:
                    store = Store(self.cfg.path("paths.db"))
                est = est_mod.estimate(store, self.cfg, est_mod.Features(
                    words=job.words, files=0, bucket=bucket, model=None))
            except Exception as exc:
                log.warning("advisor: estimate failed (%r) — no range", exc)
            t_est = time.perf_counter()
            self.meter_q.put(("estimate", job.seq, bucket, rec.id if rec else None,
                              job.override, est))
            timings = {"classify_ms": (t_cls - t0) * 1000, "model_ms": cls_ms,
                       "estimate_ms": (t_est - t_cls) * 1000}
            override = None
            if job.override:
                entry = rec_mod.by_id(self.cfg, job.override)
                override = f"override: {entry.label if entry else job.override}"
            self.call_main(self._show_advice, job, bucket, rec, estimate_line(est), override,
                           timings)

    def _show_advice(self, job: AdviceJob, bucket, rec, est_text: str, override,
                     timings: dict) -> None:
        """Main thread: the chip is on screen when this returns."""
        t_shown = time.perf_counter()
        stale = job.seq != self._seq
        if not stale and self.overlay is not None:
            self.overlay.set_advice(chip=rec.label if rec else None,
                                    strength=rec.strength_line if rec else None,
                                    estimate=est_text, override=override)
        model = timings["model_ms"]
        say(f"advice #{job.seq} ({job.source}): release->injected "
            f"{(job.t_injected - job.t_release) * 1000:.1f} ms, release->chip "
            f"{(t_shown - job.t_release) * 1000:.1f} ms; classify "
            f"{timings['classify_ms']:.0f} ms (model "
            f"{'none' if model is None else f'{model:.0f} ms'}), estimate "
            f"{timings['estimate_ms']:.1f} ms; bucket={bucket or 'none'} "
            f"chip={rec.id if rec else 'none'}; injected-before-chip="
            f"{job.t_injected < t_shown}{'; STALE, not shown' if stale else ''}")
        ev = self._chip_events.get(job.seq)
        if ev is not None:
            ev.set()

    def _meter_loop(self) -> None:
        """Owns the meter's Store and every dictation_estimates write."""
        from dict8.hooks.user_prompt_submit import quota_lines
        from dict8.usage.meter import Meter
        from dict8.usage.store import Store
        from dict8.cli import _projects_root

        try:
            store = Store(self.cfg.path("paths.db"))
            meter = Meter(store, _projects_root(self.cfg), self.cfg)
        except Exception as exc:
            say(f"meter unavailable ({exc!r}) — no live meter this session")
            self.meter_stopped.set()
            return
        interval = float(self.cfg.require("cli.tail_interval_s"))
        rows: dict[int, int] = {}          # dictation seq -> dictation_estimates.id
        next_tick = 0.0
        while True:
            try:
                op = self.meter_q.get(timeout=max(0.0, next_tick - time.monotonic()))
            except queue.Empty:
                op = None
            try:
                if op is None:
                    st = meter.tick()
                    next_tick = time.monotonic() + interval
                    self.max_scan_ms = max(self.max_scan_ms, st.scan_ms)
                    self.max_tx_ms = st.max_tx_ms
                    self._push_meter(st, store, quota_lines)
                elif op[0] == "close":
                    meter.close(op[1], reason=op[2])
                elif op[0] == "open":
                    _, seq, at, words, source = op
                    rows[seq] = meter.open_dictation(at=at, words=words, source=source).row_id
                elif op[0] == "estimate":
                    _, seq, bucket, rec_id, override, est = op
                    if seq in rows:
                        meter.set_estimate(rows[seq], bucket=bucket, recommended=rec_id,
                                           override_model=override, est=est)
                    next_tick = 0.0            # refresh the title with the new high
                elif op[0] == "shutdown":
                    meter.close(op[1], reason="shutdown")
                    say(f"meter: longest scan {self.max_scan_ms:.1f} ms, longest write "
                        f"transaction {store.max_tx_ms:.2f} ms")
                    self.meter_stopped.set()
                    return
            except Exception as exc:
                log.warning("meter: %r", exc)

    def _push_meter(self, st, store, quota_lines) -> None:
        if self.tray is None:
            return
        sid = st.session_id
        session = (f"Session {sid[:8]}: {fmt_tokens(st.session_tokens)} tokens"
                   if sid else "Session: no Claude Code transcript found")
        d = st.dictation
        if d is None:
            since = "Since last dictation: no dictation yet"
        else:
            spent = d.actual if d.closed and d.actual is not None else d.since_tokens
            rng = (f"est. {fmt_tokens(d.low)}–{fmt_tokens(d.high)} (n={d.n})"
                   if d.has_estimate and d.low is not None else "no estimate range")
            since = (f"Since last dictation: {fmt_tokens(spent)} tokens"
                     f"{' (closed)' if d.closed else ''} · {rng}")
        try:
            lines = quota_lines(store, self.cfg, datetime.now(timezone.utc))
            burn = next((ln for ln in lines if ln.startswith("burn rate")), "burn rate : —")
            burn = "Burn rate: " + burn.split(":", 1)[1].strip()
        except Exception as exc:
            burn = f"Burn rate: unavailable ({type(exc).__name__})"
        self.call_main(self.tray.set_meter, st.title_suffix(), session, since, burn)

    # -- demo (`dict8 app --demo TEXT`) -------------------------------------------------

    def _demo_one(self, t_release: float, text: str) -> None:
        out = dictation.deliver(self.cfg, self.injector, text, t_release=t_release,
                                source=DEMO, force=PATH_NONE,
                                on_text=lambda t: setattr(self, "last_text", t))
        self._after_delivery(out, t_release, DEMO)

    def _run_demo(self, texts: list[str], snapshot: str | None) -> None:
        """Runs each text through the post-STT path with injection off. Waits for the
        classifier to be warm first, so the latency printed is the warm path."""
        load_s = float(self.cfg.require("classifier.load_timeout_ms")) / 1000
        self.classifier_ready.wait(timeout=load_s)          # the loader gives up by then
        # A timed-out call kills and reloads the worker, so a chip can take one call
        # budget plus one reload before it is certainly not coming.
        chip_wait = float(self.cfg.require("classifier.timeout_ms")) / 1000 + load_s
        for i, text in enumerate(texts, 1):
            before = frontmost_app()
            self._seq += 1
            ev = threading.Event()
            self._chip_events[self._seq] = ev
            self.q.put((DEMO, time.perf_counter(), text))
            shown = ev.wait(timeout=chip_wait)
            time.sleep(DEMO_SETTLE_S)           # let the run loop draw it
            if snapshot:
                path = f"{snapshot}-{i}.png"
                self.call_main(lambda p=path: say(
                    f"demo {i}: overlay snapshot {'written' if self.overlay.snapshot(p) else 'FAILED'}: {p}"))
            self.call_main(lambda: say(f"demo {i}: overlay visible="
                                       f"{self.overlay.visible}, focus {self.overlay.focus_report()}"))
            time.sleep(DEMO_SETTLE_S)
            after = frontmost_app()
            say(f"demo {i}: chip shown={shown}; frontmost app before={before} after={after} "
                f"changed={before != after}")
            time.sleep(DEMO_PAUSE_S)

    def copy_last(self) -> None:
        if self.last_text:
            self.injector.pb.set_text(self.last_text)

    def quit(self) -> None:
        from AppKit import NSApplication

        if self.recorder is not None:
            self.recorder.cancel()
        self.meter_q.put(("shutdown", datetime.now(timezone.utc), "shutdown"))
        # The meter thread takes ops between ticks, so one tick period bounds the wait.
        self.meter_stopped.wait(timeout=float(self.cfg.require("cli.tail_interval_s")))
        if self.classifier is not None:
            try:
                self.classifier.close()
            except Exception:
                pass
        if self.tray is not None:
            say("menu: " + " | ".join(str(i.title()) for i in (
                self.tray.session_line, self.tray.since_line, self.tray.burn_line)))
        say(f"quitting (status item title {self.tray.title!r})" if self.tray else "quitting")
        NSApplication.sharedApplication().terminate_(None)


def run(cfg, *, exit_after: float | None = None, dry_run: bool = False,
        notify: bool = True, demo: list[str] | None = None,
        snapshot: str | None = None) -> int:
    from PyObjCTools import AppHelper

    toast_mod.notify = notify
    ctl = Controller(cfg, dry_run=dry_run)
    ctl.start(exit_after=exit_after, demo=demo, snapshot=snapshot)
    AppHelper.runEventLoop(installInterrupt=True)
    return 0
