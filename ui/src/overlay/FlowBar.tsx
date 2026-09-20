import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { subscribeLevel, type Advice, type OverlayState, type Tone } from "../bridge";
import { Glyph } from "./Glyph";
import "./flowbar.css";

/* The Flow bar: a black capsule that is the whole overlay.
 *
 * It has three jobs, and changes size rather than stacking text (Wispr Flow's Flow bar is
 * the reference for feel — CLAUDE.md):
 *  - recording: nine bars follow the microphone. Python maps dBFS to 0..1 against
 *    audio.silence_floor_dbfs, so flat bars mean "Dict8 would call this silence";
 *  - transcribing: the same bars, dimmed, in a travelling wave (pure CSS, compositor-only);
 *  - result: a glyph and a word, with an amber ring for the clipboard fallback and a red
 *    one for a failure.
 *
 * The level is written straight to each bar's transform in a rAF loop instead of through
 * React state: at 60 fps a re-render per frame would be wasteful, and CSS variables on the
 * parent would recalculate styles for every child. Interpolation happens here, in the web
 * view, so Python's main thread — which the hotkey's event tap shares — only pushes a
 * number 30 times a second.
 */

const BARS = 9;
// Centre bars reach highest, so the shape reads as a voice rather than a graph.
const ENVELOPE = [0.42, 0.6, 0.8, 0.94, 1, 0.94, 0.8, 0.6, 0.42];
const MIN_SCALE = 0.2; // a bar never disappears: nothing in the real world scales to 0
const BARS_W = 72; // the recording capsule: nine bars plus its side padding
const PAD_X = 14;

export function FlowBar({ state }: { state: OverlayState }) {
  const bars = useRef<(HTMLSpanElement | null)[]>([]);
  const level = useRef(state.level);
  const smoothed = useRef(0);
  const recording = state.phase === "recording";

  // Python pushes the level on its own channel, so a moving voice never re-renders React.
  useEffect(() => subscribeLevel((value) => (level.current = value)), []);

  useEffect(() => {
    if (!recording) return;
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const target = level.current;
      const s = smoothed.current;
      // Fast attack, slower release: speech reads as a pulse, not a flicker.
      smoothed.current = s + (target - s) * (target > s ? 0.35 : 0.12);
      const t = (now - start) / 1000;
      for (let i = 0; i < BARS; i++) {
        const el = bars.current[i];
        if (!el) continue;
        const wobble = 0.72 + 0.28 * Math.sin(t * (7 + 1.3 * i) + 1.7 * i);
        const scale = MIN_SCALE + (1 - MIN_SCALE) * smoothed.current * ENVELOPE[i] * wobble;
        el.style.transform = `scaleY(${Math.max(MIN_SCALE, scale).toFixed(3)})`;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [recording]);

  useEffect(() => {
    if (recording) return;
    smoothed.current = 0;
    for (const el of bars.current) if (el) el.style.transform = "";
  }, [recording]);

  // The capsule sizes itself to whichever word it is showing, and the CSS spring animates
  // between the two widths. Measured rather than `width: fit-content`, because a length is
  // what a transition can interpolate everywhere.
  const resultRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(BARS_W);
  const label = state.result?.text ?? "";
  useLayoutEffect(() => {
    if (state.phase !== "result" || !resultRef.current) {
      setWidth(BARS_W);
      return;
    }
    setWidth(Math.max(BARS_W, Math.ceil(resultRef.current.offsetWidth) + 2 * PAD_X));
  }, [state.phase, label]);

  const tone: Tone = state.result?.tone ?? "plain";
  const showBars = state.phase === "recording" || state.phase === "transcribing";

  return (
    <div className={`flow flow--${state.position}`} data-phase={state.phase}>
      {state.position === "bottom" && <Tag advice={state.advice} />}
      <div
        className="capsule"
        data-tone={tone}
        data-phase={state.phase}
        style={{ width }}
      >
        <div className="capsule__bars" aria-hidden={!showBars}>
          {Array.from({ length: BARS }, (_, i) => (
            <span
              key={i}
              className="bar"
              ref={(el) => {
                bars.current[i] = el;
              }}
              style={{ "--i": i } as React.CSSProperties}
            />
          ))}
        </div>
        <div className="capsule__result" ref={resultRef}>
          {state.result?.glyph && <Glyph name={state.result.glyph} />}
          <span className="capsule__text">{state.result?.text}</span>
        </div>
      </div>
      {state.position === "top" && <Tag advice={state.advice} />}
    </div>
  );
}

/* The advice tag: the model chip, its strength line, the estimate and any spoken override.
 * It rises out of the capsule (transform-origin at the capsule's edge) when the
 * recommendation lands, which is after the text has already been typed. Recommendation and
 * estimate live here, beside the prompt, and are never part of the injected text
 * (invariant 1). No recommendation means no chip, never a placeholder (invariant 2). */
function Tag({ advice }: { advice?: Advice | null }) {
  const has = advice && (advice.chip || advice.estimate || advice.override);
  if (!has) return null;
  return (
    <div className="tag" role="status">
      {advice.chip && (
        <div className="tag__row">
          <span className="chip">{advice.chip}</span>
          {advice.strength && <span className="tag__dim">{advice.strength}</span>}
        </div>
      )}
      {advice.estimate && <div className="tag__row tag__est">{advice.estimate}</div>}
      {advice.override && (
        <div className="tag__row tag__override">{advice.override}</div>
      )}
    </div>
  );
}
