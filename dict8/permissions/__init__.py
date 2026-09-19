"""TCC grants: Microphone, Accessibility, Input Monitoring (invariant 7b).

Every check here is a *preflight*: it reads the current state and never shows a system
prompt. Prompts happen only from `request()`, which only a user action calls (the menu's
"Check permissions", or the first press of the talk key opening the microphone).

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


def toast_text(grant: str, host: str | None = None,
               state: str | None = None) -> tuple[str, str]:
    """(title, body) for a missing grant. Names the permission and the Settings pane."""
    label = LABELS[grant]
    host = host or host_app()
    if state == "not_determined":
        return (f"Dict8: {label} not granted yet",
                f"Dict8 needs {label} {PURPOSE[grant]}. macOS asks {host} for it the first "
                f"time you hold the talk key, or use menu › Check permissions.")
    return (f"Dict8: {label} permission missing",
            f"Dict8 needs {label} {PURPOSE[grant]}. Grant it to {host} in System Settings › "
            f"Privacy & Security › {label} (menu › Check permissions opens the pane).")


def settings_link(cfg, grant: str) -> str:
    return str(cfg.require(f"permissions.settings_links.{grant}"))


def open_settings(cfg, grant: str) -> None:
    """Open the grant's pane in System Settings. Not a TCC prompt."""
    from AppKit import NSURL, NSWorkspace

    NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(settings_link(cfg, grant)))


def request(grant: str) -> None:
    """Ask macOS to show the grant's prompt. USER-INITIATED CALLERS ONLY.

    Each of these shows a system dialog the first time (and does nothing visible once the
    user has answered), so it is never called at launch or from a timer.
    """
    if grant == "microphone":
        import AVFoundation as AV

        AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AV.AVMediaTypeAudio, lambda ok: None)
    elif grant == "accessibility":
        import Quartz

        # Posting events is what Accessibility gates; this is the request form of the
        # same check CGPreflightPostEventAccess reads, and it shows the Accessibility prompt.
        Quartz.CGRequestPostEventAccess()
    elif grant == "input_monitoring":
        import Quartz

        Quartz.CGRequestListenEventAccess()
