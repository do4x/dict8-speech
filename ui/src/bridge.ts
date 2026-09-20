/* The bridge between Python (dict8.ui.webhost) and React.
 *
 * Python pushes one whole state object:      window.dict8.push({...})
 * React sends user actions back:             window.webkit.messageHandlers.dict8.postMessage({...})
 *
 * In a browser (npm run dev, or Playwright) neither exists, so `?mock=<name>` drives the
 * same states from mock.ts and actions log to the console. That is how the UI is designed
 * and reviewed without launching the app.
 */

export type Tone = "plain" | "warn" | "error";

export type Advice = {
  chip?: string | null;
  strength?: string | null;
  estimate?: string | null;
  override?: string | null;
};

export type OverlayState = {
  phase: "hidden" | "recording" | "transcribing" | "result";
  position: "top" | "bottom";
  /** 0..1, already mapped from dBFS against audio.silence_floor_dbfs by Python. */
  level: number;
  result?: { glyph: string; text: string; tone: Tone } | null;
  advice?: Advice | null;
};

export type PermissionRow = {
  id: string;
  label: string;
  state: string;
  words: string;
  purpose: string;
  button: string | null;
};

export type ModelRow = {
  id: string;
  title: string;
  model: string;
  status: string;
  ok: boolean | null;
};

export type Metric = { value: string; caption: string; numeric: boolean };

export type WindowState = {
  status: { state: string; headline: string; detail: string };
  keys: { talk: string; cancel: string };
  notice: string | null;
  last: { summary: string; heard: string | null; advice: Advice | null } | null;
  permissions: PermissionRow[];
  hostName: string;
  models: ModelRow[];
  mic: string;
  usage: { session: Metric; since: Metric; burn: string };
};

export type Action =
  | { action: "grant"; grant: string }
  | { action: "preview" }
  | { action: "copy_last" }
  | { action: "check_permissions" }
  | { action: "quit" }
  | { action: "dismiss_notice" }
  | { action: "practice"; text: string };

declare global {
  interface Window {
    dict8?: { push: (state: unknown) => void; level: (value: number) => void };
    webkit?: { messageHandlers?: { dict8?: { postMessage: (msg: unknown) => void } } };
  }
}

/** Send a user action to Python. A no-op in the browser, where it just logs. */
export function send(action: Action): void {
  const handler = window.webkit?.messageHandlers?.dict8;
  if (handler) handler.postMessage(action);
  else console.info("[dict8] action", action);
}

/** Subscribe to pushed state, and tell Python the page is mounted so it can send the
 * first one. Returns an unsubscribe function. */
export function subscribe<T>(onState: (state: T) => void): () => void {
  const listener = (event: Event) => onState((event as CustomEvent<T>).detail);
  window.addEventListener("dict8:state", listener as EventListener);
  window.dict8 = {
    push: (state: unknown) =>
      window.dispatchEvent(new CustomEvent("dict8:state", { detail: state })),
    // The overlay's bars: a number, 30 times a second, with no React render behind it.
    level: (value: number) =>
      window.dispatchEvent(new CustomEvent("dict8:level", { detail: value })),
  };
  window.webkit?.messageHandlers?.dict8?.postMessage({ action: "ready" });
  return () => window.removeEventListener("dict8:state", listener as EventListener);
}

/** Subscribe to the level fast path (overlay only). */
export function subscribeLevel(onLevel: (value: number) => void): () => void {
  const listener = (event: Event) => onLevel((event as CustomEvent<number>).detail);
  window.addEventListener("dict8:level", listener as EventListener);
  return () => window.removeEventListener("dict8:level", listener as EventListener);
}

/** Push a state from inside the page (the mock driver uses this). */
export function pushLocal(state: unknown): void {
  window.dispatchEvent(new CustomEvent("dict8:state", { detail: state }));
}

/** Push a level from inside the page (the mock driver uses this). */
export function pushLocalLevel(value: number): void {
  window.dispatchEvent(new CustomEvent("dict8:level", { detail: value }));
}

export function mockName(): string | null {
  return new URLSearchParams(window.location.search).get("mock");
}
