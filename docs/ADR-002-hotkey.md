# ADR-002: Hotkey mechanism

Status: Accepted — provisional key (2026-09-19, U5). Denis can change the key in `config.yml`
at any time; the mechanism only reopens if the key stops being a bare modifier.
Depends on: `docs/ADR-001-shell.md` (one Python process, PyObjC)

## Decision

**`CGEventTap`** (`hotkey.mechanism: cgeventtap`), an active tap at the session level on
keyDown / keyUp / flagsChanged. Talk key: **hold right Option** (`hotkey.push_to_talk:
right_option`). **Esc while held cancels** (`hotkey.cancel: escape`) and is swallowed.

## Why not `RegisterEventHotKey` (Carbon)

| | CGEventTap | RegisterEventHotKey |
|---|---|---|
| Bare modifier (right Option, fn) | yes — flagsChanged + the device-dependent flag bit tells right from left | **no** — needs a non-modifier key |
| Clean key-up for hold-to-talk | yes, flagsChanged on release | pressed/released events exist, but only for a combo |
| Swallow Esc so it doesn't also interrupt Claude Code | yes (active tap returns NULL) | no |
| Permission | Accessibility for an active tap; Input Monitoring preflighted too | none |

The first row decides it. Hold-to-talk on a key that types nothing is the feel reference
(Wispr Flow), and a combo like ⌃⌥Space is both a chord to hold and a collision risk in
terminals and IDEs. The permission cost is paid anyway: injection (`CGEventPost`) needs
Accessibility regardless, so the tap adds no *new* grant beyond Input Monitoring, which
`config.yml` requests regardless.

## Consequences

- **Not "claimed".** A tap does not register the key, so nothing can refuse it as "already
  taken" — it also cannot stop another tool (Karabiner, a launcher) from reacting to right
  Option. Such a collision shows up as that tool also firing, not as an error.
- **Chords pass through.** Any other key while right Option is held is treated as the user
  typing ⌥+key: the recording is dropped silently and the key is delivered.
- **Launch never prompts.** The tap is created only after Accessibility and Input Monitoring
  both preflight as granted; otherwise a toast names the missing one and the permission
  poll installs the tap once it is granted. macOS disables a slow tap, so the callback only
  classifies and enqueues; a disable event re-enables it, and a tap found disabled toasts.
- **Frontmost app changed between press and inject: abort typing, keep the text** — the
  transcript goes to the pasteboard with a toast, never into an app the user switched to.
- **Unverified here:** whether this macOS version needs Input Monitoring for an *active* tap
  once Accessibility is granted. Both were already granted to the host app on this machine,
  so the question could not be separated without a TCC change (Denis's click).
