import type React from "react";
/* Inline SVG glyphs. Drawn here rather than pulled from an icon package so the app ships
 * with no font or icon request at runtime (invariant 7: nothing leaves the machine). */

const PATHS: Record<string, React.ReactElement> = {
  check: <polyline points="3.5 8.5 6.5 11.5 12.5 4.5" />,
  send: (
    <>
      <path d="M13.5 2.5 7 9" />
      <path d="M13.5 2.5 9.5 13.5 7.4 8.6 2.5 6.5z" />
    </>
  ),
  clipboard: (
    <>
      <rect x="3" y="3.5" width="7" height="9" rx="1.5" />
      <path d="M6 3.5V2.8A1.3 1.3 0 0 1 7.3 1.5h1.4A1.3 1.3 0 0 1 10 2.8v.7" />
      <path d="M10 5.5h1.5A1.5 1.5 0 0 1 13 7v6a1.5 1.5 0 0 1-1.5 1.5H7" />
    </>
  ),
  x: (
    <>
      <path d="M4 4l8 8" />
      <path d="M12 4l-8 8" />
    </>
  ),
  mute: (
    <>
      <path d="M2.5 6.5v3M5.5 4.5v7M8.5 7v2" />
      <path d="M2 14 14 2" />
      <path d="M11.5 5.5v5" />
    </>
  ),
  eye: (
    <>
      <path d="M1.5 8S3.9 3.5 8 3.5 14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z" />
      <circle cx="8" cy="8" r="1.8" />
    </>
  ),
  alert: (
    <>
      <path d="M8 2.5 14.5 13.5h-13z" />
      <path d="M8 6.5v3.2" />
      <path d="M8 11.8v.2" />
    </>
  ),
};

export function Glyph({ name, size = 14 }: { name: string; size?: number }) {
  const path = PATHS[name];
  if (!path) return null;
  return (
    <svg
      className="glyph"
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {path}
    </svg>
  );
}
