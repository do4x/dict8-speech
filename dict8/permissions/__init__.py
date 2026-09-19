"""TCC grants: Microphone, Accessibility, Input Monitoring (invariant 7b).

Every check here is a *preflight*: it reads the current state and never shows a system
prompt. Prompts happen only from `request()`. Denis, 2026-09-19: the app must ask for the
microphone itself — waiting for the stream to trigger it never showed a dialog — so
`dict8 app` calls `request()` at launch when `permissions.ask_at_launch` is on, on the
first press of the talk key while the microphone is still unasked, and from the window's
and the menu's buttons.

States are re-read on every call — never cached as "granted once". A grant can be revoked
from System Settings at any time, reset by an OS update, or (for the signed `.app`, later)
lost by re-signing.

While Dict8 runs from a terminal (LOOP.md "Provisional defaults"), macOS attributes the
grants to the app that launched it — Terminal, iTerm, VS Code — not to Python. The toast
names that host so Denis knows which row to tick in System Settings.

The Settings deep links live in config.yml `permissions.settings_links` (invariant 3).
"""

from __future__ import annotations

import os
from typing import Callable

GRANTS = ("microphone", "accessibility", "input_monitoring")

LABELS = {
    "microphone": "Microphone",
    "accessibility": "Accessibility",
    "input_monitoring": "Input Monitoring",
}

PURPOSE = {
    "microphone": "to hear you while the talk key is held",
    "accessibility": "to type the transcript into the frontmost app",
    "input_monitoring": "to see the talk key from any app",
}

GRANTED = "granted"
# AVAuthorizationStatus: 0 notDetermined, 1 restricted, 2 denied, 3 authorized.
_AV_STATES = {0: "not_determined", 1: "restricted", 2: "denied", 3: GRANTED}


def microphone_state() -> str:
    try:
        import AVFoundation as AV

        return _AV_STATES.get(
            int(AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio)),
            "unknown")
    except Exception:
        return "unknown"


def accessibility_granted() -> bool:
    """AXIsProcessTrusted (no options, so never prompts)."""
    import ctypes
    import ctypes.util

    lib = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
    lib.AXIsProcessTrusted.restype = ctypes.c_bool
    return bool(lib.AXIsProcessTrusted())


def input_monitoring_granted() -> bool:
    """CGPreflightListenEventAccess — the preflight, not the request, so never prompts."""
    import Quartz

    return bool(Quartz.CGPreflightListenEventAccess())


def check() -> dict[str, str]:
    """Current state of all three grants. Each is `granted` or the reason it is not."""
    out: dict[str, str] = {"microphone": microphone_state()}
    for name, fn in (("accessibility", accessibility_granted),
                     ("input_monitoring", input_monitoring_granted)):
        try:
            out[name] = GRANTED if fn() else "denied"
        except Exception:
            out[name] = "unknown"
    return out


def missing(states: dict[str, str]) -> list[str]:
    return [g for g in GRANTS if states.get(g) != GRANTED]


def host_app() -> str:
    """Who macOS will ask about, while Dict8 runs from a terminal."""
    return (os.environ.get("__CFBundleIdentifier") or os.environ.get("TERM_PROGRAM")
            or "the app that launched Dict8")


def host_name() -> str:
    """`host_app()` as System Settings lists it ("Visual Studio Code", not the bundle id)."""
    host = host_app()
    try:
        from AppKit import NSFileManager, NSWorkspace

        url = NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(host)
        if url is not None:
            name = str(NSFileManager.defaultManager().displayNameAtPath_(url.path()))
            return name.removesuffix(".app") or host
    except Exception:
        pass
    return host


def toast_text(grant: str, host: str | None = None,
               state: str | None = None) -> tuple[str, str]:
    """(title, body) for a missing grant. Names the permission and the Settings pane."""
    label = LABELS[grant]
    host = host or host_name()
    if state == "not_determined":
        return (f"Dict8: {label} not granted yet",
                f"Dict8 needs {label} {PURPOSE[grant]}. Click Allow in the macOS dialog, "
                f"or press Allow… in the Dict8 window.")
    return (f"Dict8: {label} permission missing",
            f"Dict8 needs {label} {PURPOSE[grant]}. Grant it to {host} in System Settings › "
            f"Privacy & Security › {label} (menu › Check permissions opens the pane).")


def settings_link(cfg, grant: str) -> str:
    return str(cfg.require(f"permissions.settings_links.{grant}"))


def open_settings(cfg, grant: str) -> None:
    """Open the grant's pane in System Settings. Not a TCC prompt."""
    from AppKit import NSURL, NSWorkspace

    NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(settings_link(cfg, grant)))


def asking_text(grant: str, host: str | None = None) -> tuple[str, str]:
    """(title, body) while macOS's own dialog for `grant` is on screen."""
    label = LABELS[grant]
    host = host or host_name()
    return (f"Dict8 is asking for {label}",
            f"Click Allow in the macOS dialog. It will name {host}: while Dict8 runs from "
            f"its terminal, macOS gives the grant to {host}.")


def answer_text(grant: str, ok: bool, state: str,
                host: str | None = None) -> tuple[str, str]:
    """(title, body) once the dialog `request()` showed has been answered — or never
    appeared, which leaves the grant `not_determined` and must not read as a denial."""
    label = LABELS[grant]
    host = host or host_name()
    if ok or state == GRANTED:
        return (f"Dict8: {label} allowed", "Hold the talk key and speak.")
    if state == "not_determined":
        return (f"Dict8: macOS showed no {label} dialog",
                f"macOS did not ask {host} for {label}. Quit Dict8 and start it again from "
                f"Terminal (Applications › Utilities), which macOS can ask for the "
                f"microphone.")
    return (f"Dict8: {label} was denied",
            f"Turn on {host} in System Settings › Privacy & Security › {label} "
            f"(Open Settings in the Dict8 window), then hold the talk key again.")


def request(grant: str, on_done: Callable[[bool], None] | None = None) -> None:
    """Ask macOS to show the grant's prompt.

    macOS shows each dialog once; after an answer it does nothing visible, and the fix is
    the Settings pane. `on_done(granted)` is called for the microphone only, on an arbitrary
    thread, when the dialog is answered (or at once, when there is nothing to ask).
    """
    if grant == "microphone":
        import AVFoundation as AV

        def done(ok):
            if on_done is not None:
                on_done(bool(ok))
        AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AV.AVMediaTypeAudio, done)
    elif grant == "accessibility":
        import Quartz

        # Posting events is what Accessibility gates; this is the request form of the
        # same check CGPreflightPostEventAccess reads, and it shows the Accessibility prompt.
        Quartz.CGRequestPostEventAccess()
    elif grant == "input_monitoring":
        import Quartz

        Quartz.CGRequestListenEventAccess()
