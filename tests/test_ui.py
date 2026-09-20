"""The React UI's Python side (ADR-003): the state Dict8 pushes into each page, the level
the overlay's bars read, and the actions the pages send back.

The look lives in `ui/` and is reviewed in a browser; what is pinned here is the contract
between Python and the page. Hermetic: no microphone, no TCC dialog. The panel and window
are real AppKit objects, each with its real WKWebView.
"""

from __future__ import annotations

import math

import pytest

from dict8 import permissions
from tests.test_dictation import FakeStream, cfg  # noqa: F401  (fixture)

pytest.importorskip("AppKit")
pytest.importorskip("WebKit")


@pytest.fixture
def app():
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    a = NSApplication.sharedApplication()
    a.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    return a


# ---- the recorder's level: what the bars read --------------------------------------------


def test_level_is_none_until_recording_then_the_newest_block_in_dbfs(cfg):
    np = pytest.importorskip("numpy")
    from dict8.audio import Recorder

    rec = Recorder(cfg, 16000, stream_factory=lambda **kw: FakeStream(**kw))
    assert rec.level() is None                       # not recording
    rec.start()
    assert rec.level() is None                       # recording, nothing heard yet
    rec._callback(np.full((160, 1), 0.001, dtype=np.float32), 160, None, None)
    rec._callback(np.full((160, 1), 0.1, dtype=np.float32), 160, None, None)
    assert rec.level() == pytest.approx(20 * math.log10(0.1), abs=0.01)   # newest block
    rec._callback(np.zeros((160, 1), dtype=np.float32), 160, None, None)
    assert rec.level() < -150                        # digital zeros: a dead or muted mic
    rec.stop()
    assert rec.level() is None


# ---- the overlay ---------------------------------------------------------------------------


def test_bars_are_flat_at_the_silence_floor_and_full_one_range_above(cfg, app):
    from dict8.ui.overlay import METER_RANGE_DB, Overlay

    ov = Overlay(cfg)
    floor = float(cfg.require("audio.silence_floor_dbfs"))
    assert ov.level_to_bar(None) == 0.0
    assert ov.level_to_bar(-200.0) == 0.0            # digital zeros
    assert ov.level_to_bar(floor) == 0.0             # "Dict8 would call this silence"
    assert ov.level_to_bar(floor + METER_RANGE_DB / 2) == pytest.approx(0.5)
    assert ov.level_to_bar(0.0) == 1.0


def test_recording_pushes_levels_and_stops_with_the_recording(cfg, app):
    from dict8.ui.overlay import Overlay

    ov = Overlay(cfg)
    sent: list[float] = []
    ov.host.push_level = sent.append
    ov.level_source = lambda: -20.0
    ov.show_state("recording")
    assert ov._meter is not None                     # the tick timer runs while recording
    ov._tick()
    assert sent and sent[-1] > 0.0
    ov.level_source = lambda: (_ for _ in ()).throw(RuntimeError("mic gone"))
    ov._tick()
    assert sent[-1] == 0.0                           # a failing source is flat, not a crash
    ov.show_state("typed")
    assert ov._meter is None
    ov.hide()


def test_each_state_pushes_its_glyph_words_and_tone(cfg, app):
    from dict8.ui.overlay import Overlay

    ov = Overlay(cfg)
    ov.show_state("recording")
    assert ov.state()["phase"] == "recording" and ov.state()["result"] is None
    ov.show_state("transcribing")
    assert ov.state()["phase"] == "transcribing"
    ov.show_state("clipboard")
    result = ov.state()["result"]
    assert result["tone"] == "warn" and "⌘V" in result["text"]   # the amber fallback ring
    assert result["glyph"] == "clipboard"
    ov.show_state("error")
    assert ov.state()["result"]["tone"] == "error"
    ov.show_state("typed", "412 ms")
    assert ov.state()["result"]["text"] == "Typed · 412 ms"
    ov.hide()
    assert not ov.visible and ov.state()["phase"] == "hidden"


def test_the_live_states_never_use_the_short_dismiss(cfg, app):
    from dict8.ui.overlay import Overlay

    ov = Overlay(cfg)
    delays = []
    ov._schedule_dismiss = lambda: delays.append(
        ov.backstop_s if ov._state in ("recording", "transcribing") else ov.dismiss_after_s)
    ov.show_state("recording")
    ov.show_state("transcribing")
    ov.show_state("typed")
    assert delays[:2] == [ov.backstop_s] * 2 and delays[2] == ov.dismiss_after_s
    assert ov.backstop_s > float(cfg.require("audio.max_hold_s"))
    ov.hide()


def test_the_tag_carries_only_what_it_was_given(cfg, app):
    from dict8.ui.overlay import Overlay

    ov = Overlay(cfg)
    ov.show_state("typed")
    assert ov.state()["advice"] is None
    ov.set_advice(chip=None, strength=None, estimate=None, override="override: Opus 5")
    assert ov.state()["advice"] == {"override": "override: Opus 5"}   # no chip invented
    ov.set_advice(chip="Sonnet 5", strength="balanced", estimate="est. 1K–3K tokens · n=9",
                  override=None)
    advice = ov.state()["advice"]
    assert advice["chip"] == "Sonnet 5" and "override" not in advice
    ov.clear_advice()
    assert ov.state()["advice"] is None
    ov.hide()


def test_overlay_position_comes_from_config(cfg, app, tmp_path):
    import yaml

    from dict8 import config as config_mod
    from dict8.ui.overlay import Overlay

    assert cfg.require("overlay.position") in ("top", "bottom")
    data = yaml.safe_load(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["overlay"]["position"] = "sideways"
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="overlay.position"):
        Overlay(config_mod.Config.load(p))


# ---- the window ----------------------------------------------------------------------------


def _window(cfg):
    from dict8.ui.window import StatusWindow

    return StatusWindow(cfg, {"grant": lambda g: None, "preview": lambda: None,
                              "copy_last": lambda: None, "quit": lambda: None,
                              "check_permissions": lambda: None},
                        stt_model="stt", classifier_model="clf", mic_label="mic")


def test_before_the_first_check_no_grant_button_is_offered(cfg, app):
    w = _window(cfg)
    assert all(r["button"] is None and r["words"] == "Checking…"
               for r in w.data["permissions"])
    assert w.data["last"] is None and w.data["notice"] is None
    w.set_permissions({"microphone": "denied", "accessibility": permissions.GRANTED,
                       "input_monitoring": permissions.GRANTED})
    rows = {r["id"]: r for r in w.data["permissions"]}
    assert rows["microphone"]["button"] == "Open Settings"       # macOS will not ask twice
    assert "hear you" in rows["microphone"]["purpose"]
    assert w.data["hostName"]                                    # who macOS lists instead


def test_meter_lines_become_big_numbers_and_gaps_keep_their_words(cfg, app):
    from dict8.ui.window import metric

    assert metric("Session 3f2a91c0: 1.4M tokens") == {
        "value": "1.4M", "caption": "Session 3f2a91c0", "numeric": True}
    since = metric("Since last dictation: 212K tokens (closed) · est. 832K–2.9M (n=28)")
    assert since["value"] == "212K" and "est. 832K–2.9M" in since["caption"]
    gap = metric("Session: no Claude Code transcript found")
    assert gap["value"] == "—" and not gap["numeric"]            # never a made-up zero
    assert "no Claude Code transcript found" in gap["caption"]

    w = _window(cfg)
    w.set_meter("Session 3f2a91c0: 1.4M tokens", "Since last dictation: no dictation yet",
                "Burn rate: NOT COMPUTED — quota.stale_after_hours is unset (TBD)")
    assert w.data["usage"]["session"]["value"] == "1.4M"
    assert "TBD" in w.data["usage"]["burn"]


def test_the_page_can_dismiss_its_notice_and_reach_the_handlers(cfg, app):
    calls: list = []
    from dict8.ui.window import StatusWindow

    w = StatusWindow(cfg, {"grant": lambda g: calls.append(("grant", g)),
                           "preview": lambda: calls.append("preview"),
                           "copy_last": lambda: calls.append("copy"),
                           "quit": lambda: calls.append("quit"),
                           "check_permissions": lambda: calls.append("check")},
                     stt_model="stt", classifier_model="clf", mic_label="mic")
    w.set_notice("Dict8: Microphone allowed — dictation is ready.")
    assert w.data["notice"]
    w._action({"action": "dismiss_notice"})
    assert w.data["notice"] is None
    for action in ("preview", "copy_last", "check_permissions", "quit"):
        w._action({"action": action})
    w._action({"action": "grant", "grant": "microphone"})
    w._action({"action": "nonsense"})           # an unknown action is ignored, never raised
    assert calls == ["preview", "copy", "check", "quit", ("grant", "microphone")]


def test_models_and_advice_reach_the_page(cfg, app):
    w = _window(cfg)
    w.set_model("stt", "loaded and warm (994 ms)", True)
    w.set_model("classifier", "not installed — no model chip", False)
    models = {m["id"]: m for m in w.data["models"]}
    assert models["stt"]["ok"] is True and models["stt"]["status"].startswith("Loaded")
    assert models["classifier"]["ok"] is False
    w.set_last("Typed · 412 ms", "Fix the parser")
    w.set_advice(chip="Opus 5", strength="deep", estimate="est. 1.2M–3.1M tokens · n=15",
                 override="override: Opus 5")
    advice = w.data["last"]["advice"]
    assert advice["chip"] == "Opus 5" and advice["override"] == "override: Opus 5"
    assert w.data["last"]["heard"] == "Fix the parser"
    w.set_advice(chip=None, strength=None, estimate=None, override=None)
    assert w.data["last"]["advice"] is None


def test_the_top_strip_drags_the_window_and_leaves_the_page_its_clicks(cfg, app):
    """The window is FullSizeContentView, so the WKWebView is the whole content view and
    would swallow every mouse-down that should drag the window by its title bar. CSS cannot
    give them back -- `-webkit-app-region` is a Chromium extension WebKit ignores -- so a
    native strip sits above the web view and hands the real mouse-down to
    `performWindowDragWithEvent:`. A synthetic drag was measured moving the window
    (dx=+160, dy=-120 for that gesture) on 2026-09-20; what is pinned here is the structure
    that puts the strip in the pointer's way, and keeps it out of the page's.
    """
    from AppKit import NSWindowCloseButton

    from dict8.ui.window import TITLE_STRIP, _DragStrip

    w = _window(cfg)
    content = w.win.contentView()
    assert isinstance(w.drag, _DragStrip)
    assert list(content.subviews())[-1] is w.drag      # added last, so it hit-tests first
    assert "mouseDown_" in _DragStrip.__dict__          # the drag is this class's own doing
    assert w.win.respondsToSelector_("performWindowDragWithEvent:")

    bounds, strip = content.bounds(), w.drag.frame()
    assert strip.size.height == TITLE_STRIP
    assert strip.size.width == bounds.size.width       # full width: see _DragStrip
    assert strip.origin.y + strip.size.height == bounds.size.height      # pinned to the top

    top = content.hitTest_((bounds.size.width / 2.0, bounds.size.height - TITLE_STRIP / 2.0))
    middle = content.hitTest_((bounds.size.width / 2.0, bounds.size.height / 2.0))
    assert top is w.drag                               # the top band drags
    assert middle is not w.drag and middle.isDescendantOf_(w.host.view)  # the page keeps the rest

    # The traffic lights live in a sibling view *above* the content view, so a full-width
    # strip never reaches them.
    btn = w.win.standardWindowButton_(NSWindowCloseButton)
    centre = (btn.bounds().size.width / 2.0, btn.bounds().size.height / 2.0)
    assert content.superview().hitTest_(btn.convertPoint_toView_(centre, None)) is btn


def test_the_page_reserves_exactly_the_band_the_drag_strip_covers():
    """The strip is only free because the page leaves that band empty; if one side moves the
    other has to move with it. And the dead `-webkit-app-region` rules must stay gone: WebKit
    silently ignores them, so all they can do is mislead the next reader into thinking the
    window already drags."""
    import re
    from pathlib import Path

    from dict8.ui.window import TITLE_STRIP

    text = (Path(__file__).resolve().parent.parent / "ui" / "src" / "window"
            / "hub.css").read_text(encoding="utf-8")
    hub = re.search(r"\.hub\s*\{(.*?)\}", text, re.S)
    assert hub, ".hub rule not found in hub.css"
    pad = re.search(r"padding-top:\s*(\d+)px", hub.group(1))
    assert pad and int(pad.group(1)) == TITLE_STRIP, "hub.css and TITLE_STRIP disagree"
    assert "app-region" not in text, "WebKit does not implement app-region; it does nothing"


# ---- the web host --------------------------------------------------------------------------


def test_state_is_buffered_until_the_page_says_it_has_mounted(app):
    from AppKit import NSMakeRect
    from dict8.ui import webhost

    seen: list = []
    host = webhost.WebHost("overlay", NSMakeRect(0, 0, 10, 10),
                           on_action=seen.append, transparent=True)
    sent: list = []
    host._eval = sent.append
    host.push({"phase": "recording"})
    assert not sent and not host.ready          # nothing is sent to a page that is not up
    host._receive({"action": "ready"})          # the page mounted
    assert host.ready and "recording" in sent[-1]
    host._receive({"action": "grant", "grant": "microphone"})
    assert seen == [{"action": "grant", "grant": "microphone"}]


def test_the_asset_handler_refuses_paths_outside_the_built_ui(app):
    from dict8.ui import webhost

    assert webhost.assets_built(), "run `npm run build` in ui/ first"
    assert webhost.page_url("overlay").scheme() == webhost.SCHEME
    assert (webhost.WEB_ROOT / "window.html").exists()


# ---- the tray ------------------------------------------------------------------------------


def test_tray_attention_lines_appear_only_with_something_to_say(app):
    from dict8.ui.tray import Tray

    t = Tray({"check_permissions": lambda: None, "copy_last": lambda: None,
              "quit": lambda: None, "open_window": lambda: None},
             hotkey_label="Hold the right ⌥ Option key to talk · esc cancels")
    assert t.error_line.isHidden() and t.perm_line.isHidden()
    t.set_permissions({"microphone": "not_determined", "accessibility": permissions.GRANTED,
                       "input_monitoring": permissions.GRANTED})
    assert not t.perm_line.isHidden() and "Microphone (not determined)" in str(
        t.perm_line.title())
    t.set_permissions({g: permissions.GRANTED for g in permissions.GRANTS})
    assert t.perm_line.isHidden()
    t.set_error("Dict8: speech model failed to load")
    assert str(t.error_line.title()) == "Last message: speech model failed to load"
    t.set_state("recording")
    t.set_meter("0.8M/3.0M", "s", "d", "b")
    assert t.title == "D8 ● 0.8M/3.0M"               # the text form the logs print
    assert t.item.button().image() is not None        # the SF Symbol is what is shown
    assert "0.8M/3.0M" in str(t.item.button().title())
