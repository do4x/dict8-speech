"""U6 — voice commands, the bucket -> model lookup, the overlay's focus rules, the live
meter's dedup, and the estimate-vs-actual row.

Faked, and only this: the pasteboard and the event poster (as in test_dictation.py), the
clock the meter reads, and synthetic transcripts shaped from docs/verified-schemas.md (see
conftest). The overlay test builds the real NSPanel when PyObjC is installed. No test
posts an event, opens a microphone, or triggers a TCC prompt.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from dict8 import config as config_mod
from dict8 import dictation
from dict8.advise import recommend as rec
from dict8.advise import voice
from dict8.inject import PATH_CGEVENT, PATH_NONE, PATH_PB_ONLY
from tests.conftest import assistant_line, user_line, write_transcript
from tests.test_dictation import FakePasteboard, FakePoster, make_injector


def _cfg(tmp_path, mutate=None):
    data = yaml.safe_load(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["paths"]["db"] = str(tmp_path / "scratch.sqlite")
    data["paths"]["logs"] = str(tmp_path / "logs")
    if mutate:
        mutate(data)
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return config_mod.Config.load(p)


@pytest.fixture
def cfg(tmp_path):
    return _cfg(tmp_path)


# ---- voice commands ----------------------------------------------------------------------


@pytest.mark.parametrize("said,kept,cancel,send,override", [
    ("Cancel.", "", True, False, None),
    ("scratch that", "", True, False, None),
    ("Fix the parser, it crashes. Scratch that.", "", True, False, None),
    ("Fix the null check in the parser. Send.", "Fix the null check in the parser.",
     False, True, None),
    ("  Rename the enum!  SEND!  ", "Rename the enum!", False, True, None),
    ("Send.", "", False, True, None),
    ("Use Opus. Refactor the store module.", "Refactor the store module.", False, False,
     "claude-opus-5"),
    ("use sonnet! Rename x to y. Send.", "Rename x to y.", False, True, "claude-sonnet-5"),
    ("Use Haiku.", "", False, False, "claude-haiku-4-5-20251001"),
])
def test_voice_command_positives(cfg, said, kept, cancel, send, override):
    v = voice.parse(cfg, said)
    assert (v.text, v.cancel, v.send, v.override) == (kept, cancel, send, override)
    if kept:
        assert kept in said  # a slice of what was said, never a rewrite (invariant 1)


@pytest.mark.parametrize("said", [
    "Send the email to Ana.",
    "send the email to Ana",
    "Cancel the subscription flow.",
    "cancel the subscription flow and keep the rest",
    "Use opus-style naming for the enum.",
    "use opus style naming",
    "Send. Then fix the parser.",            # send not final
    "Fix it, send",                          # not a standalone sentence
    "Please use opus. Fix it.",              # not the exact phrase
    "Fix the parser. Use opus.",             # override only leads
    "Scratch that idea, keep the parser.",
    "Cancel. Actually keep going with the refactor.",   # cancel not final
    "The sender is fine; cancel_token stays.",
])
def test_voice_command_false_positives_inject_verbatim(cfg, said):
    v = voice.parse(cfg, said)
    assert v.commands == () and not v.cancel and not v.send and v.override is None
    assert v.text == said.strip()


def test_override_phrase_mapping_to_an_unknown_model_is_never_stripped(tmp_path):
    def bad(d):
        d["voice_commands"]["override_model"]["use opus"] = "claude-not-in-models"
    c = _cfg(tmp_path, bad)
    v = voice.parse(c, "Use opus. Fix it.")
    assert v.override is None and v.text == "Use opus. Fix it."


def test_strip_is_logged_as_the_command_name_only(cfg, caplog):
    secret = "Refactor the quota module quietly"
    with caplog.at_level("INFO", logger="dict8.advise.voice"):
        voice.parse(cfg, f"Use opus. {secret}. Send.")
    assert "override" in caplog.text and "send" in caplog.text
    assert secret not in caplog.text


class ReturnPoster(FakePoster):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.returns = 0

    def key_return(self):
        self.returns += 1


def test_send_types_the_rest_then_posts_one_return(cfg):
    poster = ReturnPoster()
    out = dictation.deliver(cfg, make_injector(cfg, poster=poster),
                            "Fix the null check. Send.", t_release=0.0, source="hotkey")
    assert out.result.path == PATH_CGEVENT and out.submitted
    assert "".join(poster.chunks) == "Fix the null check." and poster.returns == 1


def test_send_never_posts_return_when_the_text_only_reached_the_clipboard(cfg):
    poster = ReturnPoster()
    out = dictation.deliver(cfg, make_injector(cfg, poster=poster, secure=True),
                            "Fix it. Send.", t_release=0.0, source="hotkey")
    assert out.result.path == PATH_PB_ONLY and poster.returns == 0 and not out.submitted


def test_send_alone_into_a_changed_app_posts_nothing_and_keeps_the_clipboard(cfg):
    poster, pb = ReturnPoster(), FakePasteboard()
    out = dictation.deliver(cfg, make_injector(cfg, poster=poster, pb=pb), "Send.",
                            t_release=0.0, source="hotkey", frontmost_changed=lambda: True)
    assert poster.returns == 0 and pb.writes == [] and out.result.toast


def test_send_with_injection_none_posts_nothing(cfg):
    poster = ReturnPoster()
    out = dictation.deliver(cfg, make_injector(cfg, poster=poster), "Fix it. Send.",
                            t_release=0.0, source="demo", force=PATH_NONE)
    assert poster.returns == 0 and poster.chunks == [] and not out.submitted


def test_cancel_discards_everything_and_logs_no_text(cfg):
    poster, pb = ReturnPoster(), FakePasteboard()
    out = dictation.deliver(cfg, make_injector(cfg, poster=poster, pb=pb),
                            "Delete the whole repo. Scratch that.", t_release=0.0,
                            source="hotkey")
    assert poster.chunks == [] and poster.returns == 0 and pb.writes == []
    assert out.voice.cancel and out.result.path == PATH_NONE
    raw = dictation.log_path(cfg).read_text(encoding="utf-8")
    assert "Delete" not in raw and '"outcome": "cancelled"' in raw
    assert '"commands": "cancel"' in raw


def test_override_slash_command_is_off_by_default(cfg):
    poster = ReturnPoster()
    dictation.deliver(cfg, make_injector(cfg, poster=poster), "Use opus. Fix it.",
                      t_release=0.0, source="hotkey")
    assert "".join(poster.chunks) == "Fix it." and poster.returns == 0
    assert cfg.get("voice_commands.override_types_slash_command") is False


# ---- recommend: config ids only ----------------------------------------------------------


def test_recommend_only_returns_ids_from_models(cfg):
    ids = {m["id"] for m in cfg.get("models")}
    seen = set()
    for bucket in [*cfg.get("classifier.buckets"), "unknown", "nonsense", "", None,
                   "claude-opus-5", "architecture "]:
        r = rec.recommend(cfg, bucket)
        if r is not None:
            assert r.id in ids
            seen.add(bucket)
    assert seen == set(cfg.get("classifier.buckets"))   # every real bucket maps somewhere
    assert rec.recommend(cfg, "unknown") is None


def test_recommend_provisional_mapping(cfg):
    got = {b: rec.recommend(cfg, b).id for b in cfg.get("classifier.buckets")}
    assert got == {"quick-fix": "claude-sonnet-5", "debug": "claude-opus-5",
                   "feature-build": "claude-opus-5", "architecture": "claude-fable-5-1"}


def test_recommend_refuses_tbd_entries_and_ambiguous_buckets(tmp_path):
    def mutate(d):
        d["models"] = [{"id": "TBD", "label": "x", "strength_line": "x",
                        "buckets": ["quick-fix"]},
                       {"id": "a", "label": "A", "strength_line": "", "buckets": ["debug"]},
                       {"id": "b", "label": "B", "strength_line": "", "buckets": ["debug"]}]
    c = _cfg(tmp_path, mutate)
    assert rec.recommend(c, "quick-fix") is None     # the TBD entry is not an entry
    assert rec.recommend(c, "debug") is None         # two entries claim it


# ---- the meter -----------------------------------------------------------------------------


SID = "11111111-1111-4111-8111-111111111111"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _meter(store, root, cfg, clock):
    from dict8.usage.meter import Meter
    return Meter(store, root, cfg, clock=clock)


def _append(path, *lines):
    with open(path, "a", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")


def _ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_meter_counts_a_duplicated_message_id_once(cfg, store, projects_root):
    t = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
    f = write_transcript(projects_root, SID, [
        user_line("do the thing", uuid="u1", timestamp=_ts(t)),
        assistant_line(message_id="msg_1", input_tokens=100, output_tokens=0,
                       timestamp=_ts(t + timedelta(seconds=5)), uuid="a1"),
    ])
    m = _meter(store, projects_root, cfg, Clock(t))
    assert m.tick().session_tokens == 100
    # The same message.id again (Claude Code writes one line per content block) plus one
    # genuinely new message, then the same id replayed from a subagent file.
    _append(f, assistant_line(message_id="msg_1", input_tokens=100, output_tokens=0,
                              timestamp=_ts(t + timedelta(seconds=5)), uuid="a1b"),
            assistant_line(message_id="msg_2", input_tokens=40, output_tokens=10,
                           timestamp=_ts(t + timedelta(seconds=9)), uuid="a2"))
    sub = projects_root / "-tmp-dict8-test-project" / SID / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x.jsonl").write_text(assistant_line(
        message_id="msg_2", input_tokens=40, output_tokens=10, is_sidechain=True,
        timestamp=_ts(t + timedelta(seconds=9)), uuid="a2s") + "\n", encoding="utf-8")
    st = m.tick()
    assert st.session_id == SID and st.session_tokens == 150      # not 300
    assert store.session_tokens(SID) == 150
    assert sum(r["input_tokens"] + r["output_tokens"]
               for r in store.by_day(timezone.utc, session_id=SID)) == 150
    assert m.tick().new_messages == 0                               # idle tick: nothing new


def test_session_started_before_the_meter_is_picked_up_on_the_first_tick(cfg, store,
                                                                         projects_root):
    from dict8.usage.reader import scan
    t = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
    write_transcript(projects_root, SID, [
        user_line("x", uuid="u1", timestamp=_ts(t)),
        assistant_line(message_id="msg_1", input_tokens=70, output_tokens=0,
                       timestamp=_ts(t), uuid="a1")])
    scan(store, projects_root)                  # history already in the DB at launch
    assert _meter(store, projects_root, cfg, Clock(t)).tick().session_tokens == 70


def test_estimate_vs_actual_row(cfg, store, projects_root):
    from dict8.advise.estimator import Estimate
    t0 = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
    clock = Clock(t0)
    f = write_transcript(projects_root, SID, [
        user_line("earlier", uuid="u0", timestamp=_ts(t0 - timedelta(minutes=5))),
        assistant_line(message_id="msg_0", input_tokens=999, output_tokens=0,
                       timestamp=_ts(t0 - timedelta(minutes=4)), uuid="a0")])
    os.utime(f, (t0.timestamp() - 200, t0.timestamp() - 200))
    m = _meter(store, projects_root, cfg, clock)
    m.tick()
    d = m.open_dictation(at=t0, words=9, source="hotkey")
    est = Estimate(method="quantile", group="bucket debug", n=15, ood=False, low=400,
                   point=900, high=3000, cache_read_share=0.9, band_pct=50)
    m.set_estimate(d.row_id, bucket="debug", recommended="claude-opus-5",
                   override_model=None, est=est)
    _append(f, user_line("the dictated prompt", uuid="u1", timestamp=_ts(t0 + timedelta(seconds=2))),
            assistant_line(message_id="msg_1", input_tokens=1000, output_tokens=0,
                           timestamp=_ts(t0 + timedelta(seconds=6)), uuid="a1"),
            assistant_line(message_id="msg_1", input_tokens=1000, output_tokens=0,
                           timestamp=_ts(t0 + timedelta(seconds=6)), uuid="a1b"),
            assistant_line(message_id="msg_2", input_tokens=0, output_tokens=500,
                           timestamp=_ts(t0 + timedelta(seconds=30)), uuid="a2"))
    os.utime(f, (t0.timestamp() + 31, t0.timestamp() + 31))
    clock.t = t0 + timedelta(seconds=40)
    st = m.tick()
    assert st.dictation.session_id == SID and st.dictation.since_tokens == 1500
    assert st.title_suffix() == "2K/3K"
    # Next dictation starts: the first one closes with what was spent since it.
    t1 = t0 + timedelta(minutes=2)
    _append(f, assistant_line(message_id="msg_3", input_tokens=7, output_tokens=0,
                              timestamp=_ts(t1 + timedelta(seconds=1)), uuid="a3"))
    m.open_dictation(at=t1, words=3, source="hotkey")
    row = store.conn.execute("SELECT * FROM dictation_estimates WHERE id=?",
                             (d.row_id,)).fetchone()
    assert (row["low"], row["point"], row["high"], row["n"], row["actual_tokens"],
            row["close_reason"], row["session_id"], row["bucket"], row["recommended"]) == \
        (400, 900, 3000, 15, 1500, "next_dictation", SID, "debug", "claude-opus-5")
    cols = [r[1] for r in store.conn.execute("PRAGMA table_info(dictation_estimates)")]
    assert not any(b in c for c in cols for b in ("text", "usd", "cost"))


def test_idle_session_closes_the_row_and_no_session_writes_null(cfg, store, projects_root):
    t0 = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
    clock = Clock(t0)
    f = write_transcript(projects_root, SID, [
        user_line("x", uuid="u0", timestamp=_ts(t0 - timedelta(minutes=5)))])
    os.utime(f, (t0.timestamp() - 100, t0.timestamp() - 100))
    m = _meter(store, projects_root, cfg, clock)
    d = m.open_dictation(at=t0, words=4, source="hotkey")   # nothing touched afterwards
    clock.t = t0 + timedelta(seconds=float(cfg.require("meter.turn_idle_s")) + 1)
    m.tick()
    row = store.conn.execute("SELECT actual_tokens, close_reason FROM dictation_estimates "
                             "WHERE id=?", (d.row_id,)).fetchone()
    assert tuple(row) == (None, "no_session")


def test_schema_v6_migration_is_additive(tmp_path):
    from dict8.usage.store import Store
    db = tmp_path / "old.sqlite"
    with Store(db) as s:
        s.conn.execute("DROP TABLE dictation_estimates")
        s.conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
        s.add_quota_reading(12.0, "2026-09-19T10:00:00+00:00", "manual")
        s.conn.commit()
    with Store(db) as s:
        assert s.schema_version_before == 5 and s.count("quota_readings") == 1
        assert s.count("dictation_estimates") == 0


# ---- the overlay never takes focus -------------------------------------------------------


def test_overlay_never_becomes_key_or_main(cfg, tmp_path):
    pytest.importorskip("AppKit")
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    from dict8.ui.overlay import Overlay

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    ov = Overlay(cfg)
    ov.show_state("typed")
    ov.set_advice(chip="Opus 5", strength="strong", estimate="est. 1K–3K tokens · n=15",
                  override="override: Sonnet 5")
    ov.panel.makeKeyWindow()          # asked directly, it must still refuse
    import objc
    with pytest.raises(objc.error, match="canBecomeMainWindow"):
        ov.panel.makeMainWindow()     # AppKit itself refuses: the override is in force
    rep = ov.focus_report()
    assert ov.visible
    assert rep == {"panel_is_key": False, "panel_is_main": False,
                   "app_key_window_is_panel": False, "app_active": rep["app_active"],
                   "ignores_mouse": True, "nonactivating": True}
    assert not ov.panel.canBecomeKeyWindow() and not ov.panel.canBecomeMainWindow()
    assert not ov.panel.hidesOnDeactivate()
    png = tmp_path / "o.png"
    assert ov.snapshot(str(png)) and png.stat().st_size > 0
    ov.clear_advice()                 # no recommendation -> no chip, not a placeholder
    assert ov.state()["advice"] is None
    ov.hide()
    assert not ov.visible
