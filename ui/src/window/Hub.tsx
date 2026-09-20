import React, { useState } from "react";
import { send, type Advice, type Metric, type WindowState } from "../bridge";
import { Glyph } from "../overlay/Glyph";
import "./hub.css";

/* The Dict8 window: a sidebar and one inset panel, after Wispr Flow's hub.
 *
 * It is not a settings GUI — config.yml stays the settings surface (CLAUDE.md), and nothing
 * here writes config. Three pages, because Dict8 has three things to say: what just
 * happened (Dictation), what it is costing (Usage), and what it needs from macOS (System).
 *
 * Every gap keeps its words: a TBD, an unset threshold or a refused estimate is shown as
 * text, never hidden and never replaced by a plausible-looking zero (invariant 3).
 */

type Page = "dictation" | "usage" | "system";

const NAV: { id: Page; label: string; icon: React.ReactElement }[] = [
  { id: "dictation", label: "Dictation", icon: <IconWave /> },
  { id: "usage", label: "Usage", icon: <IconMeter /> },
  { id: "system", label: "System", icon: <IconShield /> },
];

export function Hub({ state }: { state: WindowState }) {
  const [page, setPage] = useState<Page>("dictation");
  const needs = state.permissions.filter((p) => p.button);

  return (
    <div className="hub">
      <aside className="sidebar">
        <div className="brand">
          <Mark />
          <span>Dict8</span>
        </div>
        <div className="status-pill" data-state={state.status.state}>
          <span className="dot" />
          {state.status.headline}
        </div>
        <nav className="nav">
          {NAV.map((item) => (
            <button
              key={item.id}
              className="nav__item"
              data-active={page === item.id}
              onClick={() => setPage(item.id)}
            >
              {item.icon}
              <span>{item.label}</span>
              {item.id === "system" && needs.length > 0 && (
                <span className="nav__badge">{needs.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar__foot">
          <button className="ghost" onClick={() => send({ action: "check_permissions" })}>
            Check permissions
          </button>
          <button className="ghost" onClick={() => send({ action: "quit" })}>
            Quit Dict8
          </button>
        </div>
      </aside>

      <main className="panel">
        {page === "dictation" && <Dictation state={state} />}
        {page === "usage" && <Usage state={state} />}
        {page === "system" && <System state={state} />}
      </main>
    </div>
  );
}

/* -- Dictation ------------------------------------------------------------------------ */

function Dictation({ state }: { state: WindowState }) {
  return (
    <div className="page">
      <div className="page__main">
        <header className="hero">
          <h1>{state.status.headline}</h1>
          {state.status.detail && <p className="hero__detail">{state.status.detail}</p>}
          <p className="hero__how">
            Hold <Key>{state.keys.talk}</Key> to talk, let go to type.{" "}
            <Key>{state.keys.cancel}</Key> cancels.
          </p>
        </header>

        {state.notice && (
          <div className="notice">
            <Glyph name="alert" />
            <span>{state.notice}</span>
            <button
              className="notice__x"
              aria-label="Dismiss"
              onClick={() => send({ action: "dismiss_notice" })}
            >
              <Glyph name="x" size={12} />
            </button>
          </div>
        )}

        <Section title="Last dictation">
          <div className="card">
            {state.last ? (
              <>
                {state.last.heard && <p className="quote">“{state.last.heard}”</p>}
                <p className="meta">{state.last.summary}</p>
                {state.last.advice && <AdviceGrid advice={state.last.advice} />}
              </>
            ) : (
              <p className="empty">
                Nothing yet. Hold <Key>{state.keys.talk}</Key> and speak; what you said shows
                up here.
              </p>
            )}
          </div>
        </Section>

        <Section title="Try it here">
          <div className="card">
            <textarea
              className="practice"
              placeholder={`Click here, hold ${state.keys.talk} and speak.`}
              spellCheck={false}
            />
            <div className="row row--between">
              <div className="row">
                <button className="btn" onClick={() => send({ action: "preview" })}>
                  Preview overlay
                </button>
                <button className="btn" onClick={() => send({ action: "copy_last" })}>
                  Copy last transcript
                </button>
              </div>
              <button
                className="ghost"
                onClick={(e) => {
                  const box = e.currentTarget
                    .closest(".card")
                    ?.querySelector("textarea");
                  if (box) box.value = "";
                }}
              >
                Clear
              </button>
            </div>
            <p className="foot">
              Dictation types into whichever app is in front. Click back into Claude Code to
              dictate there.
            </p>
          </div>
        </Section>
      </div>

      <aside className="rail">
        <Section title="This session">
          <div className="card card--tight">
            <Big metric={state.usage.session} />
            <hr />
            <Big metric={state.usage.since} />
          </div>
        </Section>
        <Section title="On this Mac">
          <div className="card card--tight">
            {state.models.map((m) => (
              <div className="model" key={m.id}>
                <div className="row row--between">
                  <span className="model__title">{m.title}</span>
                  <span className="model__status" data-ok={String(m.ok)}>
                    <span className="dot" />
                    {m.status}
                  </span>
                </div>
                <span className="model__id">{m.model}</span>
              </div>
            ))}
          </div>
        </Section>
      </aside>
    </div>
  );
}

/* -- Usage ---------------------------------------------------------------------------- */

function Usage({ state }: { state: WindowState }) {
  return (
    <div className="page page--single">
      <div className="page__main">
        <header className="hero">
          <h1>Usage</h1>
          <p className="hero__detail">
            Tokens, not dollars — Dict8 never shows a price (invariant 6).
          </p>
        </header>
        <div className="card">
          <div className="metrics">
            <Big metric={state.usage.session} large />
            <Big metric={state.usage.since} large />
          </div>
          {state.usage.burn && <p className="foot foot--block">{state.usage.burn}</p>}
        </div>
        <Section title="Calibration">
          <div className="card">
            <p className="empty">
              The unit is tokens until a quota check-in exists to calibrate against. Run{" "}
              <code>dict8 quota &lt;pct&gt;</code> with the weekly percent from{" "}
              <code>/usage</code> to turn these into quota percentages.
            </p>
          </div>
        </Section>
      </div>
    </div>
  );
}

/* -- System --------------------------------------------------------------------------- */

function System({ state }: { state: WindowState }) {
  const needs = state.permissions.filter((p) => p.button);
  return (
    <div className="page page--single">
      <div className="page__main">
        <header className="hero">
          <h1>System</h1>
          <p className="hero__detail">
            What Dict8 needs from macOS, and what it runs locally.
          </p>
        </header>

        <Section title="Permissions">
          <div className="card card--tight">
            {state.permissions.map((p) => (
              <div className="perm" key={p.id}>
                <div className="row row--between">
                  <span className="perm__name">
                    <span className="perm__mark" data-state={p.state}>
                      <Glyph
                        name={p.state === "granted" ? "check" : p.button ? "alert" : "x"}
                        size={12}
                      />
                    </span>
                    {p.label}
                  </span>
                  {p.button ? (
                    <button
                      className="btn btn--accent"
                      onClick={() => send({ action: "grant", grant: p.id })}
                    >
                      {p.button}
                    </button>
                  ) : (
                    <span className="perm__ok">{p.words}</span>
                  )}
                </div>
                {p.button && (
                  <p className="perm__why">
                    {p.words}. Needed {p.purpose}.
                  </p>
                )}
              </div>
            ))}
          </div>
          {needs.length > 0 && (
            <p className="foot foot--block">
              While Dict8 runs from a terminal, macOS lists these under {state.hostName}, not
              Dict8.
            </p>
          )}
        </Section>

        <Section title="Models · nothing leaves this Mac">
          <div className="card card--tight">
            {state.models.map((m) => (
              <div className="model" key={m.id}>
                <div className="row row--between">
                  <span className="model__title">{m.title}</span>
                  <span className="model__status" data-ok={String(m.ok)}>
                    <span className="dot" />
                    {m.status}
                  </span>
                </div>
                <span className="model__id">{m.model}</span>
              </div>
            ))}
            <div className="model">
              <span className="model__title">Microphone</span>
              <span className="model__id">{state.mic}</span>
            </div>
          </div>
        </Section>
      </div>
    </div>
  );
}

/* -- pieces --------------------------------------------------------------------------- */

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="section">
      <h2 className="section__label">{title}</h2>
      {children}
    </section>
  );
}

function Key({ children }: { children: React.ReactNode }) {
  return <kbd className="key">{children}</kbd>;
}

function Big({ metric, large }: { metric: Metric; large?: boolean }) {
  return (
    <div className={`big${large ? " big--large" : ""}`}>
      <span className="big__value" data-numeric={String(metric.numeric)}>
        {metric.value}
      </span>
      <span className="big__caption">{metric.caption}</span>
    </div>
  );
}

function AdviceGrid({ advice }: { advice: Advice }) {
  const rows: [string, React.ReactNode][] = [];
  if (advice.chip)
    rows.push([
      "Recommended",
      <>
        <span className="chip chip--light">{advice.chip}</span>
        {advice.strength && <span className="dim">{advice.strength}</span>}
      </>,
    ]);
  if (advice.estimate)
    rows.push(["Estimate", <span className="nums">{advice.estimate.replace(/^est\. /, "")}</span>]);
  if (advice.override)
    rows.push([
      "Override",
      <span className="warn">{advice.override.replace(/^override: /, "")}</span>,
    ]);
  if (!rows.length) return null;
  return (
    <div className="advice">
      {rows.map(([label, value]) => (
        <div className="advice__row" key={label}>
          <span className="advice__label">{label}</span>
          <span className="advice__value">{value}</span>
        </div>
      ))}
    </div>
  );
}

/* -- marks ---------------------------------------------------------------------------- */

function Mark() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true" className="mark">
      <g fill="currentColor">
        <rect x="1" y="7" width="2" height="4" rx="1" />
        <rect x="5" y="4" width="2" height="10" rx="1" />
        <rect x="9" y="1.5" width="2" height="15" rx="1" />
        <rect x="13" y="5.5" width="2" height="7" rx="1" />
      </g>
    </svg>
  );
}

function IconWave() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
      <path d="M2 7v2M5.3 4.5v7M8.6 2.5v11M11.9 5.5v5M15 7v2" />
    </svg>
  );
}

function IconMeter() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2.5 13.5a6 6 0 1 1 11 0" />
      <path d="M8 13.5 11 7" />
    </svg>
  );
}

function IconShield() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M8 1.8 13.2 4v4c0 3.2-2.2 5.3-5.2 6.2C5 13.3 2.8 11.2 2.8 8V4z" />
      <polyline points="5.8 8 7.4 9.6 10.4 6.6" />
    </svg>
  );
}
