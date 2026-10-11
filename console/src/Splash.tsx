import { useEffect, useState, type ReactElement } from "react";

// Cosmetic for now: the lines tick over on a timer and do not check anything.
// When chunk 2 lands, replace STEP_MS scheduling with real checks (hub /healthz,
// database, Pushover) and stop on the first failure, so a failing dependency
// can never hide behind a splash that always ends in "ready".
const STEPS = [
  ["caddy", "reverse proxy"],
  ["postgres", "issue store"],
  ["fastapi", "hub"],
  ["react", "console"],
  ["sentry", "exception capture"],
  ["grafana", "metrics"],
  ["pushover", "push channel"],
];

const STEP_MS = 155;
const FIRST_MS = 120;
const HOLD_MS = 850;
const FADE_MS = 450;
const SEEN_KEY = "nightjar.splash.seen";

// Original geometric marks (from the mockup), not the projects' official logos.
const GLYPHS: Record<string, ReactElement> = {
  caddy: (
    <>
      <circle cx="10" cy="10" r="7" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="M6 10h8" stroke="currentColor" strokeWidth="1.6" />
    </>
  ),
  postgres: (
    <>
      <ellipse cx="10" cy="6" rx="6.5" ry="2.6" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M3.5 6v8c0 1.4 2.9 2.6 6.5 2.6s6.5-1.2 6.5-2.6V6" fill="none" stroke="currentColor" strokeWidth="1.5" />
    </>
  ),
  fastapi: (
    <path d="M11 2 L4 11h5l-1 7 8-9h-5z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
  ),
  react: (
    <>
      <circle cx="10" cy="10" r="2" fill="currentColor" />
      {[0, 60, 120].map((deg) => (
        <ellipse
          key={deg}
          cx="10" cy="10" rx="8" ry="3.2"
          fill="none" stroke="currentColor" strokeWidth="1.3"
          transform={`rotate(${deg} 10 10)`}
        />
      ))}
    </>
  ),
  sentry: (
    <path d="M3 15c3.6-7 7-10.5 7-10.5S13.4 8 17 15h-3.5c-2-4-3.5-6-3.5-6s-1.5 2-3.5 6z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
  ),
  grafana: (
    <path d="M3 15l4-5 3 3 4-7 3 9" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" />
  ),
  pushover: (
    <>
      <path d="M10 3a5 5 0 0 0-5 5v3l-1.5 2.5h13L15 11V8a5 5 0 0 0-5-5z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M8.5 16a1.6 1.6 0 0 0 3 0" fill="none" stroke="currentColor" strokeWidth="1.4" />
    </>
  ),
};

function seenThisSession() {
  try {
    return sessionStorage.getItem(SEEN_KEY) === "1";
  } catch {
    return false; // storage blocked: just show it
  }
}

function markSeen() {
  try {
    sessionStorage.setItem(SEEN_KEY, "1");
  } catch {
    /* fine: it will play again next load */
  }
}

// Plays once per browser tab session, so the 30-second polling and page
// refreshes while working do not keep replaying it. Click or Escape skips.
export default function Splash() {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const [gone, setGone] = useState(seenThisSession);
  const [shown, setShown] = useState(0); // how many lines are visible
  const [leaving, setLeaving] = useState(false);

  useEffect(() => {
    if (gone) return;
    const timers: ReturnType<typeof setTimeout>[] = [];
    const at = (ms: number, fn: () => void) => timers.push(setTimeout(fn, reduce ? 0 : ms));

    STEPS.forEach((_, i) => at(FIRST_MS + i * STEP_MS, () => setShown(i + 1)));
    const readyAt = FIRST_MS + STEPS.length * STEP_MS;
    at(readyAt, () => setShown(STEPS.length + 1)); // the "ready" line
    at(readyAt + HOLD_MS, () => setLeaving(true));
    at(readyAt + HOLD_MS + FADE_MS, () => {
      markSeen();
      setGone(true);
    });

    return () => timers.forEach(clearTimeout);
  }, [gone, reduce]);

  useEffect(() => {
    if (gone) return;
    const skip = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        markSeen();
        setGone(true);
      }
    };
    addEventListener("keydown", skip);
    return () => removeEventListener("keydown", skip);
  }, [gone]);

  if (gone) return null;

  const skip = () => {
    markSeen();
    setGone(true);
  };

  return (
    <div className={`splash${leaving ? " leaving" : ""}`} onClick={skip} role="presentation">
      <div className="stage">
        <div className="mark">
          <h1>night<span>jar</span></h1>
          <p>incident hub</p>
        </div>

        <div className="boot" aria-hidden="true">
          {STEPS.map(([name, role], i) => (
            <div key={name} className={`ln${shown > i ? " show" : ""}${shown > i + 1 || shown > STEPS.length ? " done" : ""}`}>
              <svg className="glyph" viewBox="0 0 20 20">{GLYPHS[name]}</svg>
              <span className="nm">{name}</span>
              <span className="role">{role}</span>
              <span className="st">✓</span>
            </div>
          ))}
          <div className={`ln${shown > STEPS.length ? " show" : ""}`}>
            <span className="glyph" />
            <span className="nm">ready<span className="cursor" /></span>
          </div>
        </div>
      </div>
    </div>
  );
}
