"""U5 dictation MVP — the decision logic under the OS calls.

What is faked, and only this: the pasteboard, the event poster, the Secure Input and
Accessibility probes, the audio stream factory, and the STT model. Every rule under test —
chunking, the <300 ms discard, verbatim passthrough, pasteboard restore, which path a
missing grant forces, what the toast says, the talk-key state machine, the latency row
schema — is the shipped code. One test (`test_real_nspasteboard_roundtrip`) runs against a
real, privately named NSPasteboard when PyObjC is installed, never the general one.

No test here opens a microphone, posts an event, or triggers a TCC prompt.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml

from dict8 import config as config_mod
from dict8 import dictation, permissions
from dict8.hotkey import CANCEL, CHORD, FLAGS_CHANGED, KEY_DOWN, KEY_UP, PRESS, RELEASE, TalkKey
from dict8.inject import (PATH_CGEVENT, PATH_FAILED, PATH_PASTE, PATH_PB_ONLY, Injector,
                          _units, chunk_utf16, prepare)

# Voice-free: text the injector must pass through untouched. Curly quotes, em dash, braces,
# snake_case, accents, a combining sequence, an emoji (a surrogate pair), a tab inside.
HOSTILE_TEXT = ("  Rename snake_case_id → {braces}, “curly” and \"straight\" quotes — "
                "café résumé naïve e\u0301 😀 tab\there; don't stop.\n ")


@pytest.fixture
def cfg(tmp_path):
    data = yaml.safe_load(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["paths"]["db"] = str(tmp_path / "scratch.sqlite")
    data["paths"]["logs"] = str(tmp_path / "logs")
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return config_mod.Config.load(p)


# ---- fakes for the OS ------------------------------------------------------------------


class FakePasteboard:
    def __init__(self, items=None):
        self.items = items if items is not None else [{"public.utf8-plain-text": b"before"}]
        self.cc = 1
        self.writes: list[str] = []

    def change_count(self):
        return self.cc

    def snapshot(self):
        return [dict(i) for i in self.items]

    def restore(self, snap):
        self.items = [dict(i) for i in snap]
        self.cc += 1

    def set_text(self, text):
        self.items = [{"public.utf8-plain-text": text.encode("utf-8")}]
        self.writes.append(text)
        self.cc += 1

    def get_text(self):
        for i in self.items:
            if "public.utf8-plain-text" in i:
                return i["public.utf8-plain-text"].decode("utf-8")
        return None


class FakePoster:
    def __init__(self, fail_type=False, fail_paste=False):
        self.chunks: list[str] = []
        self.pastes = 0
        self.fail_type, self.fail_paste = fail_type, fail_paste

    def type_chunk(self, chunk):
        if self.fail_type:
            raise RuntimeError("CGEventCreateKeyboardEvent returned NULL")
        self.chunks.append(chunk)

    def cmd_v(self):
        if self.fail_paste:
            raise RuntimeError("post failed")
        self.pastes += 1


def make_injector(cfg, *, pb=None, poster=None, secure=False, ax=True, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return Injector(cfg, pasteboard=pb or FakePasteboard(), poster=poster or FakePoster(),
                    secure_input=lambda: secure, ax_trusted=lambda: ax,
                    sleep=sleeps.append)


# ---- chunker ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [2, 3, 7, 20])
def test_chunker_rejoins_exactly_and_respects_the_unit_limit(n):
    chunks = chunk_utf16(HOSTILE_TEXT, n)
    assert "".join(chunks) == HOSTILE_TEXT
    for c in chunks:
        assert _units(c) <= n
        assert not (0xDC00 <= ord(c[0]) <= 0xDFFF)  # never starts on a low surrogate


def test_chunker_hard_caps_a_combining_run_longer_than_the_chunk():
    text = "ab e" + "\u0301" * 50 + " end"
    chunks = chunk_utf16(text, 20)
    assert "".join(chunks) == text and all(_units(c) <= 20 for c in chunks)


def test_chunker_never_splits_an_emoji_or_detaches_a_combining_mark():
    chunks = chunk_utf16("ab😀" + "e\u0301" * 3, 3)
    assert "😀" in "".join(chunks) and all("\ud83d" not in c for c in chunks)
    assert all(not c.startswith("\u0301") for c in chunks)


def test_chunker_rejects_a_size_that_cannot_hold_a_surrogate_pair():
    with pytest.raises(ValueError):
        chunk_utf16("x", 1)


# ---- verbatim passthrough (invariant 1) ------------------------------------------------


def test_prepare_strips_only_the_ends():
    assert prepare(HOSTILE_TEXT) == HOSTILE_TEXT.strip()
    assert prepare(HOSTILE_TEXT)[0] == "R" and prepare(HOSTILE_TEXT).endswith("stop.")


def test_cgevent_path_types_the_transcript_verbatim_in_config_sized_chunks(cfg):
    poster, sleeps = FakePoster(), []
    typed = HOSTILE_TEXT.replace("\t", " ")  # control characters take the paste path
    r = make_injector(cfg, poster=poster, sleeps=sleeps).inject(typed)
    assert r.path == PATH_CGEVENT and r.toast is None
    assert "".join(poster.chunks).encode("utf-8") == typed.strip().encode("utf-8")
    limit = int(cfg.require("injection.chunk_chars"))
    assert len(poster.chunks) > 1 and all(_units(c) <= limit for c in poster.chunks)
    delay = float(cfg.require("injection.inter_chunk_delay_ms")) / 1000
    assert sleeps == [delay] * (len(poster.chunks) - 1)


def test_long_transcript_pastes_verbatim_and_restores_the_pasteboard(cfg):
    pb, poster, sleeps = FakePasteboard(), FakePoster(), []
    over = int(cfg.require("injection.fallback_trigger.over_chars"))
    text = (HOSTILE_TEXT.strip() + " ") * (over // len(HOSTILE_TEXT) + 2)
    r = make_injector(cfg, pb=pb, poster=poster, sleeps=sleeps).inject(text)
    assert r.path == PATH_PASTE and poster.chunks == [] and poster.pastes == 1
    assert pb.writes == [text.strip()]
    assert pb.items == [{"public.utf8-plain-text": b"before"}]  # restored
    assert sleeps == [float(cfg.require("injection.pasteboard_restore_delay_ms")) / 1000]


def test_cgevent_failure_falls_back_to_paste_not_to_nothing(cfg):
    pb = FakePasteboard()
    r = make_injector(cfg, pb=pb, poster=FakePoster(fail_type=True)).inject("hello — “x”")
    assert r.path == PATH_PASTE and pb.writes == ["hello — “x”"]


def test_both_posting_paths_failing_leaves_the_text_on_the_pasteboard(cfg):
    pb = FakePasteboard()
    r = make_injector(cfg, pb=pb,
                      poster=FakePoster(fail_type=True, fail_paste=True)).inject("keep me")
    assert r.path == PATH_PB_ONLY and pb.get_text() == "keep me" and r.toast


def test_pasteboard_write_failing_too_is_reported_not_raised(cfg):
    class Broken(FakePasteboard):
        def set_text(self, text):
            raise RuntimeError("refused")
    r = make_injector(cfg, pb=Broken(), ax=False).inject("keep me")
    assert r.path == PATH_FAILED and "Copy last transcript" in r.toast[1]


# ---- pasteboard save + restore ---------------------------------------------------------


def test_restore_is_skipped_when_someone_else_wrote_the_pasteboard(cfg):
    class Racy(FakePasteboard):
        def __init__(self):
            super().__init__()
            self.after_paste = None

    pb = Racy()
    poster = FakePoster()

    def sleep(_s):  # another app copies something while we wait to restore
        pb.set_text("the user's new copy")
    inj = Injector(cfg, pasteboard=pb, poster=poster, secure_input=lambda: False,
                   ax_trusted=lambda: True, sleep=sleep)
    inj._paste("dictated")
    assert pb.get_text() == "the user's new copy"


def test_real_nspasteboard_roundtrip():
    """A real NSPasteboard, privately named — never the general one."""
    pytest.importorskip("AppKit")
    from AppKit import NSPasteboard

    from dict8.inject import Pasteboard

    raw = NSPasteboard.pasteboardWithUniqueName()
    try:
        pb = Pasteboard(raw)
        pb.set_text("original “one”")
        snap = pb.snapshot()
        pb.set_text(HOSTILE_TEXT)
        assert pb.get_text() == HOSTILE_TEXT
        pb.restore(snap)
        assert pb.get_text() == "original “one”" and pb.snapshot() == snap
    finally:
        raw.releaseGlobally()


# ---- permissions -> toast, never a silent no-op (invariant 7b) -------------------------


@pytest.mark.parametrize("grant,label", [("microphone", "Microphone"),
                                         ("accessibility", "Accessibility"),
                                         ("input_monitoring", "Input Monitoring")])
def test_toast_names_the_missing_grant_and_its_settings_pane(grant, label):
    title, body = permissions.toast_text(grant, host="com.example.Host")
    assert label in title
    assert f"Privacy & Security › {label}" in body and "com.example.Host" in body


def test_every_grant_has_a_settings_link_in_config(cfg):
    for g in permissions.GRANTS:
        assert permissions.settings_link(cfg, g).startswith("x-apple.systempreferences:")


def test_missing_lists_everything_not_granted():
    assert permissions.missing({"microphone": "granted", "accessibility": "denied",
                                "input_monitoring": "unknown"}) == ["accessibility",
                                                                    "input_monitoring"]


def test_accessibility_missing_forces_pasteboard_only_with_a_toast_naming_it(cfg):
    pb, poster = FakePasteboard(), FakePoster()
    r = make_injector(cfg, pb=pb, poster=poster, ax=False).inject("hello")
    assert r.path == PATH_PB_ONLY and poster.chunks == [] and poster.pastes == 0
    assert pb.get_text() == "hello" and "Accessibility" in r.toast[0]


def test_secure_input_forces_pasteboard_only_with_a_toast(cfg):
    pb, poster = FakePasteboard(), FakePoster()
    r = make_injector(cfg, pb=pb, poster=poster, secure=True).inject("hello")
    assert r.path == PATH_PB_ONLY and poster.chunks == [] and "Secure Input" in r.toast[0]


# ---- audio: < min_hold_ms is discarded silently ----------------------------------------


class FakeStream:
    def __init__(self, **kw):
        self.kw = kw
        self.started = self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        self.closed = True


def _record(cfg, ms: float):
    np = pytest.importorskip("numpy")
    from dict8.audio import Recorder

    made = []
    rec = Recorder(cfg, 16000, stream_factory=lambda **kw: made.append(FakeStream(**kw))
                   or made[-1])
    rec.start()
    n = int(16000 * ms / 1000)
    block = 160  # 10 ms blocks, like a real callback
    for i in range(0, n, block):
        rec._callback(np.full((min(block, n - i), 1), 0.1, dtype=np.float32), block, None, None)
    audio, dur = rec.stop()
    return audio, dur, made[0]


def test_short_release_is_discarded(cfg):
    audio, dur, stream = _record(cfg, float(cfg.require("audio.min_hold_ms")) - 10)
    assert audio is None and 0 < dur < float(cfg.require("audio.min_hold_ms"))
    assert stream.closed


def test_long_enough_release_returns_mono_float32_at_the_model_rate(cfg):
    audio, dur, stream = _record(cfg, 1000)
    assert audio is not None and audio.ndim == 1 and audio.dtype.name == "float32"
    assert abs(dur - 1000) < 1 and stream.kw["samplerate"] == 16000 and stream.kw["channels"] == 1


def test_mic_that_will_not_open_raises_a_named_error(cfg):
    from dict8.audio import MicUnavailable, Recorder

    def boom(**kw):
        raise OSError("device busy")
    with pytest.raises(MicUnavailable, match="device busy"):
        Recorder(cfg, 16000, stream_factory=boom).start()


# ---- talk key state machine ------------------------------------------------------------

ROPT, LOPT, ESC, KEY_A = 61, 58, 53, 0
ROPT_DOWN, LOPT_DOWN = 0x80000 | 0x40, 0x80000 | 0x20


def test_hold_and_release_right_option():
    k = TalkKey("right_option", "escape")
    assert k.handle(FLAGS_CHANGED, ROPT, ROPT_DOWN) == (PRESS, False)
    assert k.handle(FLAGS_CHANGED, ROPT, 0) == (RELEASE, False)


def test_left_option_is_not_the_talk_key():
    k = TalkKey("right_option", "escape")
    assert k.handle(FLAGS_CHANGED, LOPT, LOPT_DOWN) == (None, False)


def test_escape_while_held_cancels_and_is_swallowed_down_and_up():
    k = TalkKey("right_option", "escape")
    k.handle(FLAGS_CHANGED, ROPT, ROPT_DOWN)
    assert k.handle(KEY_DOWN, ESC, ROPT_DOWN) == (CANCEL, True)
    assert k.handle(KEY_DOWN, ESC, ROPT_DOWN) == (None, True)  # auto-repeat
    assert k.handle(KEY_UP, ESC, ROPT_DOWN) == (None, True)
    assert k.handle(FLAGS_CHANGED, ROPT, 0) == (None, False)  # no dictation after cancel
    assert k.handle(KEY_DOWN, ESC, 0) == (None, False)  # Esc when not held passes through


def test_other_key_while_held_is_a_chord_and_passes_through():
    k = TalkKey("right_option", "escape")
    k.handle(FLAGS_CHANGED, ROPT, ROPT_DOWN)
    assert k.handle(KEY_DOWN, KEY_A, ROPT_DOWN) == (CHORD, False)
    assert k.handle(KEY_DOWN, ESC, ROPT_DOWN) == (None, False)  # Esc in a chord is not ours
    assert k.handle(FLAGS_CHANGED, ROPT, 0) == (None, False)


def test_config_hotkey_names_resolve():
    c = config_mod.load()
    TalkKey(str(c.require("hotkey.push_to_talk")), str(c.require("hotkey.cancel")))


# ---- the pipeline and its latency row --------------------------------------------------


class FakeSTT:
    sample_rate = 16000

    def __init__(self, text):
        self.text = text
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return self.text, 12.5


def test_silence_never_reaches_stt_so_nothing_invented_is_typed(cfg):
    """stt.vad: Whisper answers pure silence with words nobody said (invariant 1)."""
    np = pytest.importorskip("numpy")
    stt, poster = FakeSTT("Thank you."), FakePoster()
    out = dictation.process(cfg, stt, make_injector(cfg, poster=poster),
                            np.zeros(16000, dtype=np.float32), audio_ms=1000, t_release=0.0,
                            source="hotkey")
    assert stt.calls == 0 and poster.chunks == [] and out.text == ""
    assert "heard nothing" in out.result.toast[0]
    assert json.loads(dictation.log_path(cfg).read_text().splitlines()[-1])["outcome"] == "silent"


def test_speech_level_audio_passes_the_silence_floor(cfg):
    np = pytest.importorskip("numpy")
    from dict8.audio import is_silent, peak_frame_dbfs

    t = np.arange(16000, dtype=np.float32) / 16000
    tone = (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)  # about -23 dBFS RMS
    floor = float(cfg.require("audio.silence_floor_dbfs"))
    frame = float(cfg.require("audio.frame_ms"))
    assert -24 < peak_frame_dbfs(tone, 16000, frame) < -22
    assert not is_silent(tone, 16000, floor, frame)
    assert is_silent(tone * 1e-3, 16000, floor, frame)  # the same tone 60 dB down


def test_frontmost_change_puts_the_text_on_the_pasteboard_instead_of_typing(cfg):
    pb, poster = FakePasteboard(), FakePoster()
    out = dictation.process(cfg, FakeSTT(" “hi” — {x} "), make_injector(cfg, pb=pb, poster=poster),
                            None, audio_ms=900, t_release=0.0, source="hotkey",
                            frontmost_changed=lambda: True)
    assert out.result.path == PATH_PB_ONLY and poster.chunks == []
    assert pb.get_text() == "“hi” — {x}" and out.result.toast


def test_latency_row_is_numbers_and_labels_only(cfg):
    secret = "a sentence Denis said"
    dictation.process(cfg, FakeSTT(secret), make_injector(cfg), None, audio_ms=900,
                      t_release=0.0, source="file")
    raw = dictation.log_path(cfg).read_text(encoding="utf-8")
    assert secret not in raw
    row = json.loads(raw.splitlines()[-1])
    assert set(row) <= set(dictation.ROW_KEYS)
    assert row["path"] == PATH_CGEVENT and row["chars"] == len(secret)


def test_latency_row_refuses_a_text_field(cfg):
    dictation.record(cfg, {"source": "file", "outcome": "injected", "text": "leak"})
    assert not dictation.log_path(cfg).exists()


# ---- the hook's base env stays light ---------------------------------------------------


def test_cli_and_hook_import_nothing_from_the_app_extra():
    heavy = ["AppKit", "Quartz", "AVFoundation", "objc", "sounddevice", "numpy", "mlx",
             "mlx_whisper", "mlx_lm"]
    code = ("import sys, dict8.cli, dict8.hooks.user_prompt_submit; "
            f"print([m for m in {heavy!r} if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert out == "[]"


def test_real_cgevents_carry_the_exact_chunks_no_modifiers_and_the_dict8_tag(monkeypatch):
    """Real Quartz events, built by the shipped poster; only CGEventPost is replaced, so
    nothing reaches the frontmost app. Reads back what each event would type."""
    Q = pytest.importorskip("Quartz")
    from dict8.inject import DICT8_EVENT_TAG, EventPoster

    posted = []
    poster = EventPoster()
    monkeypatch.setattr(poster, "Q", type("Q", (), {
        **{k: getattr(Q, k) for k in dir(Q) if k.startswith(("CGEvent", "kCG"))},
        "CGEventPost": staticmethod(lambda tap, ev: posted.append(ev)),
    }))
    chunks = chunk_utf16(HOSTILE_TEXT.strip().replace("\t", " "), 20)
    for c in chunks:
        poster.type_chunk(c)
    downs = posted[0::2]
    typed = [Q.CGEventKeyboardGetUnicodeString(ev, 64, None, None)[1] for ev in downs]
    assert "".join(typed) == HOSTILE_TEXT.strip().replace("\t", " ")
    assert all(Q.CGEventGetFlags(ev) == 0 for ev in posted)
    assert all(Q.CGEventGetIntegerValueField(ev, Q.kCGEventSourceUserData) == DICT8_EVENT_TAG
               for ev in posted)


# ---- U5 fix round ----------------------------------------------------------------------


@pytest.mark.parametrize("text", ["line one\nline two", "a\rb", "col\tcol", "x\x1by"])
def test_control_characters_are_pasted_never_posted_as_keys(cfg, text):
    """A newline typed as a key submits a half-finished prompt in Claude Code; a tab hits
    its completion binding. Pasted, they stay text — verbatim (invariant 1)."""
    pb, poster = FakePasteboard(), FakePoster()
    r = make_injector(cfg, pb=pb, poster=poster).inject(text)
    assert r.path == PATH_PASTE and poster.chunks == [] and poster.pastes == 1
    assert pb.writes == [text]


def _boom():
    raise OSError("probe unavailable")


@pytest.mark.parametrize("raiser", ["secure_input", "ax_trusted", "frontmost"])
def test_a_raising_pre_check_lands_the_text_on_the_pasteboard(cfg, raiser):
    """Verifier's lose-probe: once STT has returned, no OS check may lose the words."""
    pb, poster, kept = FakePasteboard(), FakePoster(), []
    inj = Injector(cfg, pasteboard=pb, poster=poster,
                   secure_input=_boom if raiser == "secure_input" else (lambda: False),
                   ax_trusted=_boom if raiser == "ax_trusted" else (lambda: True),
                   sleep=lambda s: None)
    out = dictation.process(cfg, FakeSTT("the words the user said"), inj, None,
                            audio_ms=1000, t_release=0.0, source="file",
                            frontmost_changed=_boom if raiser == "frontmost" else None,
                            on_text=kept.append)
    assert kept == ["the words the user said"]  # saved before any OS call ran
    assert out.result.path == PATH_PB_ONLY and pb.get_text() == "the words the user said"
    assert poster.chunks == [] and out.result.toast


def test_injector_raising_anything_still_lands_the_text(cfg):
    class Exploding(Injector):
        def inject(self, raw, *, force=None):
            raise KeyError("unexpected")
    pb = FakePasteboard()
    inj = Exploding(cfg, pasteboard=pb, poster=FakePoster(), secure_input=lambda: False,
                    ax_trusted=lambda: True)
    out = dictation.process(cfg, FakeSTT("keep"), inj, None, audio_ms=1000, t_release=0.0,
                            source="file")
    assert out.result.path == PATH_PB_ONLY and pb.get_text() == "keep"


def test_max_hold_or_a_disabled_tap_ends_a_live_hold_as_a_release():
    k = TalkKey("right_option", "escape")
    assert k.force_release() is None                      # nothing held
    k.handle(FLAGS_CHANGED, ROPT, ROPT_DOWN)
    assert k.force_release() == RELEASE                   # audio so far gets processed
    assert k.handle(FLAGS_CHANGED, ROPT, 0) == (None, False)  # the late key-up is ignored
    assert k.handle(FLAGS_CHANGED, ROPT, ROPT_DOWN) == (PRESS, False)  # next hold works


def test_force_release_after_a_cancel_is_not_a_dictation():
    k = TalkKey("right_option", "escape")
    k.handle(FLAGS_CHANGED, ROPT, ROPT_DOWN)
    k.handle(KEY_DOWN, ESC, ROPT_DOWN)
    assert k.force_release() is None and not k.held


def test_max_hold_is_configured():
    assert float(config_mod.load().require("audio.max_hold_s")) > 0
