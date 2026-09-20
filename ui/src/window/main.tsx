import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { mockName, pushLocal, subscribe, type WindowState } from "../bridge";
import { WINDOW_MOCKS } from "../mock";
import { Hub } from "./Hub";
import "../tokens.css";

function App() {
  const [state, setState] = useState<WindowState | null>(null);

  useEffect(() => subscribe<WindowState>(setState), []);
  useEffect(() => {
    const name = mockName();
    if (name) pushLocal(WINDOW_MOCKS[name] ?? WINDOW_MOCKS.ready);
  }, []);

  if (!state) return null;   // Python pushes the first state as the window opens
  return <Hub state={state} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
