"""The Dict8 window and the microphone ask (Denis, 2026-09-19).

Hermetic: `permissions.check` / `permissions.request` are replaced, so no test here shows a
TCC dialog or opens the microphone. The real dialog is Denis's check (docs/LOOP.md U7).
"""

from __future__ import annotations

import pytest

from dict8 import permissions
from tests.test_dictation import cfg  # noqa: F401  (fixture)

pytest.importorskip("AppKit")


class FakeRecorder:
    def __init__(self):
        self.started = 0

    def start(self):
        self.started += 1


def make_controller(cfg, monkeypatch, mic_state, *, ask=True):
    from dict8 import app as app_mod

    ctl = app_mod.Controller(cfg)
    ctl._ask_auto = ask
    ctl.ready = True
    ctl.recorder = FakeRecorder()
    ctl.call_main = lambda fn, *a, **k: None          # no run loop in tests
    toasts: list[tuple[str, str]] = []
    ctl.toast = lambda t, b: toasts.append((t, b))
    asked: list[str] = []
    states = {"microphone": mic_state, "accessibility": permissions.GRANTED,
              "input_monitoring": permissions.GRANTED}
    monkeypatch.setattr(permissions, "check", lambda: dict(states))
    monkeypatch.setattr(permissions, "request",
                        lambda g, on_done=None: asked.append(g))
    return ctl, toasts, asked


def test_press_while_the_mic_is_unasked_asks_macos_and_records_nothing(cfg, monkeypatch):
    ctl, toasts, asked = make_controller(cfg, monkeypatch, "not_determined")
    ctl._press(None)
    assert asked == ["microphone"]
    assert ctl.recorder.started == 0 and not ctl._recording
    assert toasts and toasts[0][0] == "Dict8 is asking for Microphone"


def test_press_with_asking_off_keeps_opening_the_stream(cfg, monkeypatch):
    ctl, toasts, asked = make_controller(cfg, monkeypatch, "not_determined", ask=False)
    monkeypatch.setattr(ctl, "_overlay_state", lambda *a, **k: None)
    ctl._press(None)
    assert asked == [] and ctl.recorder.started == 1
    ctl._recording = False


def test_press_with_the_mic_denied_toasts_the_grant_and_asks_nothing(cfg, monkeypatch):
    ctl, toasts, asked = make_controller(cfg, monkeypatch, "denied")
    ctl._press(None)
    assert asked == [] and ctl.recorder.started == 0
    assert "Microphone" in toasts[0][0]


def test_an_already_answered_grant_opens_its_settings_pane_from_the_window(cfg, monkeypatch):
    ctl, toasts, asked = make_controller(cfg, monkeypatch, "denied")
    opened: list[str] = []
    monkeypatch.setattr(permissions, "open_settings", lambda c, g: opened.append(g))
    ctl.ask_from_window("microphone")
    assert opened == ["microphone"] and asked == []     # macOS will not ask twice


@pytest.mark.parametrize("ok,state,title", [
    (True, permissions.GRANTED, "Dict8: Microphone allowed"),
    (False, "denied", "Dict8: Microphone was denied"),
    (False, "not_determined", "Dict8: macOS showed no Microphone dialog"),
])
def test_the_answer_toast_tells_a_denial_from_a_dialog_that_never_showed(ok, state, title):
    got, body = permissions.answer_text("microphone", ok, state, host="Visual Studio Code")
    assert got == title and body


def test_window_rows_buttons_and_advice(cfg, tmp_path):
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    from dict8.ui.window import StatusWindow

    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory)
    calls: list = []
    w = StatusWindow(cfg, {"grant": calls.append, "preview": lambda: calls.append("p"),
                           "copy_last": lambda: None, "quit": lambda: None},
                     stt_model="stt-model", classifier_model="clf-model", mic_label="mic")
    w.set_permissions({"microphone": "not_determined", "accessibility": "denied",
                       "input_monitoring": permissions.GRANTED})
    mic_btn = w.perm_rows["microphone"][2]
    assert str(mic_btn.title()) == "Allow…" and not mic_btn.isHidden()
    assert str(w.perm_rows["accessibility"][2].title()) == "Open Settings"
    assert w.perm_rows["input_monitoring"][2].isHidden()
    mic_btn.performClick_(None)                  # the button reaches the handler
    assert calls == ["microphone"]
    w.set_state("idle")                          # loaded, but not usable yet
    assert str(w.status.stringValue()) == "Almost ready"
    assert "Microphone and Accessibility" in str(w.status_detail.stringValue())
    w.set_permissions({g: permissions.GRANTED for g in permissions.GRANTS})
    assert str(w.status.stringValue()) == "Ready"

    w.set_advice(chip=None, strength=None, estimate=None, override=None)
    assert w.advice_row.isHidden() and w.estimate.isHidden()   # no placeholder model
    w.set_advice(chip="Sonnet 5", strength="s", estimate="est. 1K–3K tokens",
                 override=None)
    assert not w.advice_row.isHidden() and "Sonnet 5" in str(w.chip.stringValue())
    w.set_last("Typed · 200 ms from release to text", None)
    assert w.heard.isHidden()
    png = tmp_path / "w.png"
    assert w.snapshot(str(png)) and png.stat().st_size > 0
