/* Mock states for designing in a browser: `?mock=<name>` on either page.
 *
 * Every string here is shaped exactly like what Python pushes, including the labeled gaps
 * ("not enough similar history", "unset (TBD)"), so the layout is designed against the real
 * worst cases rather than tidy placeholders.
 */

import type { OverlayState, WindowState } from "./bridge";

const advice = {
  chip: "Sonnet 5",
  strength: "balanced — debugging and feature work",
  estimate: "est. 832K–2.9M tokens · n=28 · bucket debug",
  override: null,
};

export const OVERLAY_MOCKS: Record<string, OverlayState> = {
  recording: { phase: "recording", position: "bottom", level: 0.7, advice: null },
  quiet: { phase: "recording", position: "bottom", level: 0, advice: null },
  transcribing: { phase: "transcribing", position: "bottom", level: 0, advice: null },
  typed: {
    phase: "result",
    position: "bottom",
    level: 0,
    result: { glyph: "check", text: "Typed", tone: "plain" },
    advice: null,
  },
  advice: {
    phase: "result",
    position: "bottom",
    level: 0,
    result: { glyph: "send", text: "Typed and sent", tone: "plain" },
    advice,
  },
  override: {
    phase: "result",
    position: "bottom",
    level: 0,
    result: { glyph: "check", text: "Typed", tone: "plain" },
    advice: {
      chip: "Opus 5",
      strength: "deep — architecture and hard bugs",
      estimate: "est. — not enough similar history (n=0)",
      override: "override: Opus 5",
    },
  },
  clipboard: {
    phase: "result",
    position: "bottom",
    level: 0,
    result: { glyph: "clipboard", text: "On the clipboard — press ⌘V", tone: "warn" },
    advice: null,
  },
  error: {
    phase: "result",
    position: "bottom",
    level: 0,
    result: { glyph: "alert", text: "Dictation failed", tone: "error" },
    advice: null,
  },
  top: { phase: "recording", position: "top", level: 0.6, advice: null },
};

const PERMS_OK = [
  { id: "microphone", label: "Microphone", state: "granted", words: "Granted",
    purpose: "to hear you while the talk key is held", button: null },
  { id: "accessibility", label: "Accessibility", state: "granted", words: "Granted",
    purpose: "to type the transcript into the frontmost app", button: null },
  { id: "input_monitoring", label: "Input Monitoring", state: "granted", words: "Granted",
    purpose: "to see the talk key from any app", button: null },
];

const BASE: WindowState = {
  status: { state: "idle", headline: "Ready", detail: "" },
  keys: { talk: "right ⌥", cancel: "esc" },
  notice: null,
  last: {
    summary: "Typed · 412 ms from release to text",
    heard: "Fix the failing test in the transcript parser and explain what caused it.",
    advice,
  },
  permissions: PERMS_OK,
  hostName: "Visual Studio Code",
  models: [
    { id: "stt", title: "Speech to text", model: "mlx-community/whisper-small.en-mlx",
      status: "Loaded and warm (994 ms)", ok: true },
    { id: "classifier", title: "Model picker", model: "mlx-community/Qwen2.5-3B-Instruct-4bit",
      status: "Ready (2251 ms)", ok: true },
  ],
  mic: "system default input (hardware.mic_device is TBD)",
  usage: {
    session: { value: "1.4M", caption: "Session 3f2a91c0", numeric: true },
    since: { value: "212K", caption: "Since last dictation · est. 832K–2.9M (n=28)", numeric: true },
    burn: "Burn rate: NOT COMPUTED — quota.stale_after_hours is unset (TBD) in config.yml, so there is no threshold to judge against.",
  },
};

export const WINDOW_MOCKS: Record<string, WindowState> = {
  ready: BASE,
  fresh: {
    ...BASE,
    status: { state: "loading", headline: "Loading the speech model…", detail: "" },
    last: null,
    permissions: [
      { id: "microphone", label: "Microphone", state: "not_determined",
        words: "Not asked yet", purpose: "to hear you while the talk key is held",
        button: "Allow…" },
      { id: "accessibility", label: "Accessibility", state: "denied", words: "Not granted",
        purpose: "to type the transcript into the frontmost app", button: "Open Settings" },
      { id: "input_monitoring", label: "Input Monitoring", state: "granted", words: "Granted",
        purpose: "to see the talk key from any app", button: null },
    ],
    models: [
      { ...BASE.models[0], status: "Loading…", ok: null },
      { ...BASE.models[1], status: "Loading…", ok: null },
    ],
    usage: {
      session: { value: "—", caption: "Session · no Claude Code transcript found", numeric: false },
      since: { value: "—", caption: "Since last dictation · no dictation yet", numeric: false },
      burn: "",
    },
  },
  attention: {
    ...BASE,
    status: {
      state: "error", headline: "Almost ready",
      detail: "Allow Microphone and Input Monitoring to dictate.",
    },
    notice: "Dict8: Input Monitoring not granted — macOS lists it under Visual Studio Code.",
    permissions: [
      { id: "microphone", label: "Microphone", state: "not_determined",
        words: "Not asked yet", purpose: "to hear you while the talk key is held",
        button: "Allow…" },
      PERMS_OK[1],
      { id: "input_monitoring", label: "Input Monitoring", state: "denied",
        words: "Not granted", purpose: "to see the talk key from any app",
        button: "Open Settings" },
    ],
  },
  recording: {
    ...BASE,
    status: { state: "recording", headline: "Recording…", detail: "" },
  },
};
