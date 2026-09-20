# ADR-003: The UI is React, rendered in-process by WebKit

Status: Accepted — **LOCKED**
Date: 2026-09-20
Decider: Denis
Amends: `docs/ADR-001-shell.md` (the *shell* half of it stands; its "no web shell" clause is
narrowed to "no second process")

## Context

ADR-001 chose one Python process and, with it, AppKit views drawn by hand in PyObjC. Two
rounds of UI work (2026-09-19, 2026-09-20) showed what that costs:

- every visual change is Python laying out `NSStackView`s and painting `drawRect_`, with the
  only feedback loop being "launch the app and squint";
- Denis's verdict on the result: *"It looks nothing like Wispr."* The reference is an
  Electron app whose look comes from CSS — rounded surfaces, real type scales, a warm
  palette, motion that is one line per property. Reproducing that by hand in AppKit is a
  large amount of code for a worse result;
- Denis's standing preference: *"We usually default to css and react js for the best UI
  possible."*

The constraint that has not changed: `mlx-whisper` and `mlx-lm` are Python, Apple Silicon
only, and invariant 7 keeps inference local. There is no Node binding for MLX. So the STT
model cannot move into a JavaScript runtime, and `CGEventPost` / `CGEventTap` / TCC are
already working PyObjC code with tests around them.

## Decision

**The UI is React + TypeScript + CSS. It renders in a `WKWebView` inside the existing Python
process.** Python keeps the microphone, STT, injection, the hotkey tap, permissions, the
usage layer and the hooks. It owns *what* is shown; the page owns *how it looks*.

- source in `ui/` (Vite), built into `dict8/ui/web/` and shipped in the package;
- two pages: `overlay.html` (the Flow bar, in the non-activating panel) and `window.html`
  (the hub, in the ordinary window);
- the bridge is two calls, in `dict8/ui/webhost.py`: `window.dict8.push(state)` into the
  page, `postMessage({action})` back out. One whole state object per push, so the page is a
  pure function of state;
- assets are read off disk and served to WebKit over Dict8's own `dict8-ui://` scheme. Not
  `file://`, because WebKit gives file pages an opaque origin and then refuses to load their
  ES modules. No port is opened and nothing is fetched (invariant 7);
- `DICT8_UI_URL=http://localhost:5173` points the same views at Vite's dev server.

## Why not Electron, which is what Wispr Flow ships

It would mean a second process and a second runtime for a tool whose whole point is that
release-to-text stays inside `latency_budget_ms`, and the STT model would still have to live
in a Python sidecar — so the process boundary would land in the middle of the dictation
path, which is the one place ADR-001 was right to protect. WebKit is already on the machine,
already sandboxed per-process by the system, and costs one framework import.

## Consequences

- **A build step now exists.** `cd ui && npm install && npm run build` before `dict8 app`, or
  the app has no interface. `webhost.assets_built()` reports that as a labeled gap rather
  than showing a blank panel.
- **The overlay's guarantees are unchanged and still tested.** The panel refuses key and main
  status, ignores mouse events and is ordered in with `orderFrontRegardless()`; the web view
  is only a renderer inside it.
- **The live bars got cheaper.** Levels go down a separate channel (`window.dict8.level`)
  that does not re-render React, and the smoothing runs in the web view's own rAF loop — off
  the main thread the event tap shares.
- **`snapshot()` can lag a frame.** WebKit's `takeSnapshot` sometimes returns the previously
  rasterised frame for a window in a background app. The state pushed to the page is what
  the tests assert on; the PNGs are a development aid, and the browser is where the UI is
  reviewed.
- **Dependencies:** `pyobjc-framework-WebKit` (app extra), and Node only for the build, never
  at runtime.
- Superseded parts of ADR-001: its "no web shell" line. Everything else in it — one process,
  PyObjC for the OS surfaces, a warm in-process model, a signed `.app` with a stable bundle
  ID — still holds.
