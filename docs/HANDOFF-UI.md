# Handoff — the React UI (ADR-003), 2026-09-20

Written for the next agent picking this up in the terminal. Read `docs/ADR-003-ui.md` first;
this file is the state, the one open bug, and the traps.

## Where things stand

The UI was rewritten from hand-drawn AppKit to React + TypeScript + CSS in a `WKWebView`
(ADR-003, LOCKED, amends ADR-001). Denis asked for this explicitly: *"We usually default to
css and react js for the best UI possible."* CLAUDE.md's Stack section now says so, so the
old AppKit approach is not re-derived by accident.

- **pytest: 183 passing** (`.venv/bin/python -m pytest -q`).
- **Nothing is committed.** Branch `claude/modest-hamilton-lh72fw`, last commit `f7632f9`
  (pre-refactor). 24 paths modified/untracked — see `git status`.
- **Nothing is pushed.** Denis asked for a push to `https://github.com/do4x/dict8`
  (remote name `dict8`, already configured). It was deliberately held back until the drag
  bug below is fixed.
- The built UI lives in `dict8/ui/web/` and **is currently committed-by-intent** (it ships in
  the package so `dict8 app` works without Node). `ui/node_modules/` is gitignored.

## Fixed 2026-09-20: the window drags

Denis, after testing: *"clicks work, however clicking outside the box to drag the app panel
isnt functional."* The diagnosis in the previous handoff was right — `-webkit-app-region` is
a Chromium/Electron extension that WebKit silently ignores, and the WKWebView is the entire
content view, so it swallowed every mouse-down that should have dragged the window.

**The fix**: `_DragStrip` in `dict8/ui/window.py` — a native `NSView` across the top
`TITLE_STRIP` (28 pt) of the window, added to a container content view *after* the web view
so it hit-tests first. Its `mouseDown_` hands the real event to
`performWindowDragWithEvent:`. The dead CSS rules are gone and the wrong comment on the
`titlebarAppearsTransparent_` line is corrected.

**Two things the previous handoff got wrong** — both measured on this machine, not inferred:

- **`mouseDownCanMoveWindow` (the recommended option A) does not work here**, alone or with
  `setMovableByWindowBackground_(True)`. With the web view in the content view AppKit never
  even *calls* it: the mouse-down is delivered straight to the view's `mouseDown_`, and the
  window does not move. Do not reach for it again.
- **The strip does not need to avoid the traffic lights.** `NSTitlebarContainerView` is a
  sibling *above* the content view, so it hit-tests first and the buttons keep their clicks
  under a full-width strip. The whole top band drags.

Option B works, but natively, not over the bridge: passing the event we were already handed
means there is no round trip through the page and no `NSApp.currentEvent()` to go stale.

**Verified by actually dragging**, per the last handoff's own instruction: a synthetic
mouse-down on the strip plus a 12-step drag moved the window exactly the gesture's delta
(`dx=+160, dy=-120`), and the middle of the page still hit-tests to the web view. The moves
have to be posted from a second thread — `performWindowDragWithEvent:` blocks the main
thread in the window server's drag loop until the button comes up. Synthetic events reach a
native view fine; it is only WKWebView *content* they cannot reach (see the traps below).

`tests/test_ui.py` pins the structure (strip above the web view, full width, pinned to the
top, page keeps the rest, traffic lights still win) and the cross-file coupling: `.hub`'s
`padding-top` must equal `TITLE_STRIP`, and `app-region` must not come back.

## Traps — do not repeat these

- **Synthetic clicks (`CGEventPost`) do not reach `WKWebView` content on this machine.**
  A minimal reproduction with zero Dict8 code confirmed it: a native `NSButton` receives
  synthetic clicks fine, a `WKWebView` button never does (a JS `mousedown` listener counted
  exactly 0), regardless of window style, activation policy, first-responder state, or a
  preceding cursor warp/move. **Real physical clicks work** — Denis confirmed. A previous
  session burned a lot of budget concluding "clicks are broken" from this. They are not.
  Do not use CGEventPost to test the web UI. Use the browser (below).
- **`screencapture -l <windowid>` lies about WKWebView.** It pulls a window's own content
  buffer and often returns a stale frame or omits the web layer entirely (blank/dark image),
  even while the DOM is correct. Use `screencapture -R <x,y,w,h>` (a real screen region) or
  just look at the browser.
- **`WKWebView.takeSnapshot` can also return the previous frame** for a background app's
  window, even with `afterScreenUpdates = True`. `Overlay.snapshot()` / `StatusWindow.
  snapshot()` are development aids, not ground truth. Tests assert on the *pushed state*
  (`overlay.state()`, `window.data`) instead — keep it that way.
- **Ad-hoc test scripts need `if __name__ == "__main__":`.** Without it, the classifier's
  `multiprocessing` spawn re-executes the script and you silently get **two** full app
  instances racing for key-window status, which looks like a focus bug. The real entry point
  (`dict8/cli.py:833`) is correctly guarded; only throwaway scripts hit this.
- Do not run a second `dict8 app` while Denis has one running — two `CGEventTap`s both grab
  the talk key. If you must, monkeypatch `ctl._install_tap = lambda: None` before `start()`.

## How to work on the UI

```bash
cd ui && npm install && npm run build      # writes dict8/ui/web/
uv run --extra app dict8 app               # the real app

# live reload:
cd ui && npm run dev
DICT8_UI_URL=http://localhost:5173 uv run --extra app dict8 app

# the actual feedback loop — every state, in a browser, no app launch:
open http://localhost:5173/window.html?mock=ready      # also: fresh, attention, recording
open http://localhost:5173/overlay.html?mock=advice    # also: recording, quiet, transcribing,
                                                       # typed, override, clipboard, error, top
```

Mock states are in `ui/src/mock.ts` and are shaped exactly like what Python pushes, including
the labeled gaps. **A Playwright MCP is now configured for this project** — use it against
those URLs rather than relaunching the app and squinting.

## Architecture in one paragraph

Python owns *what* is shown, the page owns *how it looks*. `dict8/ui/webhost.py` is the whole
bridge: `window.dict8.push(state)` sends one whole state object in (the page is a pure
function of it), `postMessage({action})` sends user actions back, and
`window.dict8.level(v)` is a separate 30 Hz channel for the overlay's bars that deliberately
does **not** re-render React. Assets are served over a custom `dict8-ui://` scheme — not
`file://`, because WebKit gives file pages an opaque origin and then refuses to load their ES
modules (React would never mount). The overlay panel keeps every focus guarantee it had:
non-activating, refuses key/main, `ignoresMouseEvents`, `orderFrontRegardless`. Tests cover
that in `tests/test_ui.py` and `tests/test_u6.py`.

## Next steps

1. ~~Fix the drag bug~~ — done, see above. Denis should confirm it feels right by
   dragging the real window.
2. Ask Denis to confirm `overlay.position` (`bottom`, above the Dock) or switch it to `top`.
3. Commit and push to `dict8` remote (`https://github.com/do4x/dict8`) — Denis asked for this
   and it is still outstanding. Note `.agents/`, `.claude/skills/`, `.hermes/` and
   `skills-lock.json` are untracked plugin/skill scaffolding; decide with Denis whether those
   belong in the repo before staging everything.
4. Unrelated pre-existing gap: `gate_phase1` is 7/8 because Claude Code 2.1.278 now writes
   transcripts and is not yet pinned in `docs/verified-schemas.md` (invariant 4).
