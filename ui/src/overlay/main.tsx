import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { mockName, pushLocal, pushLocalLevel, subscribe, type OverlayState } from "../bridge";
import { OVERLAY_MOCKS } from "../mock";
import { FlowBar } from "./FlowBar";
import "../tokens.css";
import "./overlay.css";

const HIDDEN: OverlayState = { phase: "hidden", position: "bottom", level: 0, advice: null };

function Overlay() {
  const [state, setState] = useState<OverlayState>(HIDDEN);

  useEffect(() => subscribe<OverlayState>(setState), []);

  useEffect(() => {
    const name = mockName();
    if (!name) return;
    const mock = OVERLAY_MOCKS[name] ?? OVERLAY_MOCKS.recording;
    pushLocal(mock);
    if (mock.phase !== "recording" || mock.level === 0) return;
    // A believable voice, so the bars can be judged in the browser.
    const id = setInterval(() => {
      pushLocalLevel(0.35 + 0.45 * Math.abs(Math.sin(performance.now() / 420)));
    }, 33);
    return () => clearInterval(id);
  }, []);

  if (state.phase === "hidden") return null;
  return <FlowBar state={state} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Overlay />
  </StrictMode>,
);
