/**
 * Nightjar browser reporter. One file, no dependencies: copy it into the site.
 *
 *   import { init } from "./nightjar";
 *   init({ hub: "https://nightjar.example.co.ke", key: "<the site's public key>" });
 *
 * The key is PUBLIC. It sits in the page for anyone to read, so all it can do is report errors for
 * this one site, and only from the origins you listed in Nightjar. It is not the site's ingest
 * secret, and the secret must never be put in a web page.
 *
 * The rule it shares with client/reporter.py: it must never break the page it reports on. Every
 * path is wrapped and swallowed, there are no retries and no queue, and a Nightjar outage costs
 * the visitor nothing.
 *
 * Plain TypeScript with no enums or parameter properties, so it also runs under Node's type
 * stripping (that is how it is tested) and compiles with any bundler or `tsc`.
 */

export type Level = "warning" | "error";

export interface Options {
  /** Where the hub is, for example https://nightjar.example.co.ke (no trailing path). */
  hub: string;
  /** The site's PUBLIC browser key. */
  key: string;
  /** A version or commit, so an error can be tied to a deploy. */
  release?: string;
  environment?: string;
  /**
   * Level for uncaught errors. Default "warning", which reaches the morning digest and never wakes
   * you. A page error is usually noise; escalate the ones that matter on purpose with
   * captureException(err, "error").
   */
  level?: Level;
  /** Extra things to drop, matched against the message and the file names. */
  ignore?: Array<string | RegExp>;
  /** Drop errors that happened only in other people's scripts (analytics, extensions). Default true. */
  firstPartyOnly?: boolean;
  /** Most reports sent in a minute. Default 6. */
  maxPerMinute?: number;
  /** The same error is sent at most once in this many minutes. Default 10. */
  dedupeMinutes?: number;
}

export interface Frame {
  module?: string;
  file?: string;
  function?: string;
  lineno?: number;
}

/** What the reporter needs from the browser, so it can be tested without one. */
export interface Env {
  send(url: string, body: string): void;
  now(): number;
  /** The page's address, and its origin (https://host). */
  page(): string;
  origin(): string;
}

const MAX_BODY = 15_000; // the hub refuses 16 KB
const MAX_FRAMES = 30;
const MAX_MESSAGE = 1500;
const MAX_REMEMBERED = 50;

// Noise that is never worth a row: browser quirks and code that is not yours.
const DEFAULT_IGNORE: Array<string | RegExp> = [
  /^Script error\.?$/i, // a cross-origin script error, which carries no detail at all
  /ResizeObserver loop/i,
  /Non-Error promise rejection captured/i,
  /(chrome|moz|safari|safari-web)-extension:/i,
];

/** Frames from a stack string, innermost LAST, which is the order the hub expects. */
export function parseStack(stack: string | undefined | null): Frame[] {
  if (!stack) return [];
  const frames: Frame[] = [];
  for (const raw of stack.split("\n")) {
    const line = raw.trim();
    // Chrome:   at fn (https://x/a.js:1:2)   or   at https://x/a.js:1:2
    // Firefox:  fn@https://x/a.js:1:2        Safari: same, or  @https://x/a.js:1:2
    let fn: string | undefined;
    let loc: string | undefined;
    let m = /^at\s+(?:(.*?)\s+\()?(.+?)\)?$/.exec(line);
    if (m && line.startsWith("at ")) {
      fn = m[1];
      loc = m[2];
    } else if ((m = /^(.*?)@(.+)$/.exec(line))) {
      fn = m[1];
      loc = m[2];
    } else {
      continue;
    }
    const at = /^(.*?):(\d+)(?::(\d+))?$/.exec(loc ?? "");
    if (!at) continue;
    const url = at[1].split("?")[0].split("#")[0];
    const frame: Frame = { file: url.slice(0, 200), module: url.split("/").pop() || url, lineno: Number(at[2]) };
    const name = (fn ?? "").replace(/^(async|new)\s+/, "").trim();
    if (name && name !== "<anonymous>") frame.function = name.slice(0, 200);
    frames.push(frame);
  }
  return frames.reverse().slice(-MAX_FRAMES);
}

function toError(thing: unknown): { type: string; message: string; stack?: string } {
  if (thing instanceof Error) {
    return { type: thing.name || "Error", message: String(thing.message ?? ""), stack: thing.stack };
  }
  let message: string;
  try {
    message = typeof thing === "string" ? thing : JSON.stringify(thing);
  } catch {
    message = String(thing);
  }
  return { type: "UnhandledRejection", message: message ?? "" };
}

function matches(rule: string | RegExp, text: string): boolean {
  return typeof rule === "string" ? text.includes(rule) : rule.test(text);
}

export interface Reporter {
  capture(error: unknown, level?: Level): void;
}

/** The reporter itself, with the browser passed in. `init` wires it to the real one. */
export function createReporter(options: Options, env: Env): Reporter {
  const url = `${options.hub.replace(/\/+$/, "")}/ingest/browser/${options.key}`;
  const rules = [...DEFAULT_IGNORE, ...(options.ignore ?? [])];
  const maxPerMinute = options.maxPerMinute ?? 6;
  const dedupeMs = (options.dedupeMinutes ?? 10) * 60_000;
  const firstPartyOnly = options.firstPartyOnly ?? true;
  const recent = new Map<string, number>(); // error signature -> when it was last sent
  let sent: number[] = []; // when each report in the last minute was sent

  function capture(error: unknown, level?: Level): void {
    try {
      const { type, message, stack } = toError(error);
      const frames = parseStack(stack);
      const text = `${type}: ${message}\n${frames.map((f) => f.file).join("\n")}`;

      if (rules.some((r) => matches(r, text))) return;
      // Everything in the stack came from someone else's script: not this site's bug.
      if (firstPartyOnly && frames.length > 0 && !frames.some((f) => (f.file ?? "").startsWith(env.origin()))) return;

      const now = env.now();
      const top = frames[frames.length - 1];
      // digits and quoted bits vary per occurrence, so they are left out of the signature
      const signature = [type, message.replace(/\d+|'[^']*'|"[^"]*"/g, "#"), top?.module, top?.function, top?.lineno].join("|");
      const last = recent.get(signature);
      if (last !== undefined && now - last < dedupeMs) return;

      sent = sent.filter((t) => now - t < 60_000);
      if (sent.length >= maxPerMinute) return;

      if (recent.size >= MAX_REMEMBERED) recent.delete(recent.keys().next().value as string);
      recent.set(signature, now);
      sent.push(now);

      let body = JSON.stringify({
        exception_type: type,
        message: message.slice(0, MAX_MESSAGE),
        frames,
        level: level ?? options.level ?? "warning",
        release: options.release,
        environment: options.environment ?? "production",
        page: env.page(),
      });
      // keep the innermost frames if the report is too big, since those identify the bug
      let keep = frames.length;
      while (body.length > MAX_BODY && keep > 1) {
        keep = Math.floor(keep / 2);
        body = JSON.stringify({ ...JSON.parse(body), frames: frames.slice(-keep) });
      }
      if (body.length > MAX_BODY) return;
      env.send(url, body);
    } catch {
      // reporting must never break the page
    }
  }

  return { capture };
}

let active: Reporter | null = null;

/** Start reporting uncaught errors and unhandled promise rejections. Returns a function that stops it. */
export function init(options: Options): () => void {
  try {
    if (typeof window === "undefined" || !options?.hub || !options?.key) return () => {};
    const env: Env = {
      now: () => Date.now(),
      page: () => window.location.href,
      origin: () => window.location.origin,
      send(url, body) {
        try {
          // A text/plain Blob is a "simple" request: no preflight, so one round trip and nothing
          // for the visitor to wait on. sendBeacon also survives the page closing.
          if (navigator.sendBeacon?.(url, new Blob([body], { type: "text/plain" }))) return;
        } catch {
          // fall through to fetch
        }
        try {
          void fetch(url, {
            method: "POST",
            body,
            mode: "no-cors",
            keepalive: true,
            headers: { "Content-Type": "text/plain" },
          }).catch(() => {});
        } catch {
          // nothing else to try, and nothing to tell the visitor
        }
      },
    };
    const reporter = createReporter(options, env);
    active = reporter;

    const onError = (e: ErrorEvent) => {
      // a failed <img> or <script> load fires "error" on the element with no .error: not a code bug
      if (!e.error && !e.message) return;
      reporter.capture(e.error ?? new Error(e.message));
    };
    const onRejection = (e: PromiseRejectionEvent) => reporter.capture(e.reason);
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
      if (active === reporter) active = null;
    };
  } catch {
    return () => {};
  }
}

/** Report an error you caught yourself. Use "error" for the ones that matter (a failed checkout). */
export function captureException(error: unknown, level: Level = "error"): void {
  try {
    active?.capture(error, level);
  } catch {
    // never throw into the caller
  }
}
