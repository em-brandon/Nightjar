/**
 * Nightjar reporter for Node servers (Express and anything else). One file, no dependencies: copy it in.
 *
 *   import { init, expressErrorHandler, installProcessHandlers } from "./nightjar";
 *   init();                              // reads NIGHTJAR_URL, NIGHTJAR_KEY and NIGHTJAR_SECRET
 *   installProcessHandlers();            // report a crash that kills the process
 *   app.use(expressErrorHandler());      // after your routes, before your own error handler
 *
 * This holds the site's ingest SECRET, so it belongs on a server and never in a web page. For pages use
 * client/browser/nightjar.ts, which has a public key and no secret. See docs/KEYS.md.
 *
 * The rule it shares with client/reporter.py: it must never break the app it reports on. Every path is
 * wrapped and swallowed, a send has a short timeout and no retry, there is no queue (a report that arrives
 * while too many are in flight is dropped), and a Nightjar outage is invisible to the app's users.
 *
 * Needs Node 18 or newer (global fetch). Runs directly on Node 22.18+ and 24 (type stripping); on older
 * Node, compile it first with `tsc` or `esbuild`. Plain TypeScript, no enums or parameter properties.
 */

import { createHmac } from "node:crypto";

export type Level = "warning" | "error" | "fatal";

export interface Options {
  /** The hub, for example https://nightjar.example.co.ke. Default: NIGHTJAR_URL. */
  hub?: string;
  /** The site's ingest key (it is in the URL). Default: NIGHTJAR_KEY. */
  key?: string;
  /** The site's ingest secret, which signs each report. Default: NIGHTJAR_SECRET. */
  secret?: string;
  environment?: string; // default NIGHTJAR_ENV, then "production"
  release?: string; // default NIGHTJAR_RELEASE
  /** How long one send may take. Default 3000 ms. */
  timeoutMs?: number;
  /** Most reports sent in a minute; the rest are dropped. Default 60. */
  maxPerMinute?: number;
  /** Most sends in flight at once; the rest are dropped. Default 5. */
  maxInFlight?: number;
  /** Level for a crash that kills the process. Default "fatal", which is P1. */
  crashLevel?: Level;
}

export interface Frame {
  module?: string;
  file?: string;
  function?: string;
  lineno?: number;
}

export interface RequestInfo {
  method?: string;
  path?: string;
}

/** What the reporter needs from the outside world, so it can be tested without a network. */
export interface Env {
  send(url: string, headers: Record<string, string>, body: string, timeoutMs: number): Promise<void>;
  now(): number;
  /** Where the one-line warning about an unusable hub goes. Default: standard error. */
  warn?(message: string): void;
}

export interface Prepared {
  url: string;
  headers: Record<string, string>;
  body: string;
}

const MAX_MESSAGE = 3900; // the hub allows 4000
const MAX_FRAMES = 100; // the hub allows 200; the innermost matter most
const MAX_FIELD = 200;

/** Frames from a V8 stack, innermost LAST, which is the order the hub expects. */
export function parseStack(stack: string | undefined | null): Frame[] {
  if (!stack) return [];
  const frames: Frame[] = [];
  for (const raw of stack.split("\n")) {
    const line = raw.trim();
    if (!line.startsWith("at ")) continue;
    const body = line.slice(3);
    // "fn (location)", "async fn (location)", "new Foo (location)" or a bare "location"
    const paren = /^(.*?)\s+\((.*)\)$/.exec(body);
    const fn = paren ? paren[1] : "";
    const loc = paren ? paren[2] : body;
    // the location ends in :line:col (a Windows drive letter's colon is earlier, so anchor on the end)
    const at = /^(.*?):(\d+)(?::(\d+))?$/.exec(loc);
    if (!at) continue;
    const file = at[1].replace(/^file:\/\//, "").replace(/^\/([A-Za-z]:)/, "$1").split("?")[0];
    const frame: Frame = {
      file: file.slice(0, MAX_FIELD),
      module: (file.split(/[\\/]/).pop() || file).slice(0, MAX_FIELD),
      lineno: Number(at[2]),
    };
    const name = fn.replace(/^(async|new)\s+/, "").trim();
    if (name && name !== "<anonymous>") frame.function = name.slice(0, MAX_FIELD);
    frames.push(frame);
  }
  return frames.reverse().slice(-MAX_FRAMES);
}

function describe(thing: unknown): { type: string; message: string; stack?: string } {
  if (thing instanceof Error) {
    return { type: thing.name || "Error", message: String(thing.message ?? ""), stack: thing.stack };
  }
  let message: string;
  try {
    message = typeof thing === "string" ? thing : JSON.stringify(thing);
  } catch {
    message = String(thing);
  }
  return { type: "NonErrorThrown", message: message ?? "" };
}

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

/**
 * Whether this hub address may be used, and if not, why. https is always fine. Plain http is fine only for a machine
 * talking to itself, which development needs: anywhere else it would send error text, readable, across a network.
 * Returns null when the address is fine, or one line saying what is wrong. It never includes the whole address, which
 * could carry a credential.
 */
export function hubRefusal(hub: string): string | null {
  let url: URL;
  try {
    url = new URL(hub);
  } catch {
    return "Nightjar: the hub address is not a valid URL, so no errors will be reported. Use https://your-hub.";
  }
  if (url.protocol === "https:") return null;
  if (url.protocol === "http:" && (LOOPBACK.has(url.hostname) || url.hostname.endsWith(".localhost"))) return null;
  return (
    `Nightjar: refusing to report errors to ${url.protocol}//${url.hostname}: plain http would send them readable ` +
    "across the network. Use an https:// hub address (http:// is only accepted for localhost). No errors will be reported."
  );
}

function resolve(options: Options): Required<Pick<Options, "hub" | "key" | "secret">> & Options {
  const env = typeof process !== "undefined" ? process.env : {};
  return {
    ...options,
    hub: (options.hub ?? env.NIGHTJAR_URL ?? "").replace(/\/+$/, ""),
    key: options.key ?? env.NIGHTJAR_KEY ?? "",
    secret: options.secret ?? env.NIGHTJAR_SECRET ?? "",
    environment: options.environment ?? env.NIGHTJAR_ENV ?? "production",
    release: options.release ?? env.NIGHTJAR_RELEASE,
  };
}

/** The signed request for one error. Exposed so a test can check the signature the hub will see. */
export function prepare(options: Options, error: unknown, level: Level = "error", request?: RequestInfo): Prepared {
  const o = resolve(options);
  const { type, message, stack } = describe(error);
  const body = JSON.stringify({
    exception_type: type.slice(0, MAX_FIELD),
    message: message.slice(0, MAX_MESSAGE),
    frames: parseStack(stack),
    level,
    environment: o.environment,
    release: o.release,
    // the path only: a query string can carry a token or a person's details
    request: request ? { method: request.method, path: request.path?.split("?")[0] } : undefined,
  });
  // The HMAC is over exactly these bytes, which is what the hub verifies.
  const signature = "sha256=" + createHmac("sha256", o.secret).update(body).digest("hex");
  return {
    url: `${o.hub}/ingest/${o.key}/event`,
    headers: { "Content-Type": "application/json", "X-Hub-Signature": signature },
    body,
  };
}

export interface Reporter {
  capture(error: unknown, level?: Level, request?: RequestInfo): void;
  /** Wait (up to timeoutMs) for reports already on their way. */
  flush(timeoutMs?: number): Promise<void>;
  readonly options: Options;
}

/** The reporter itself, with the network passed in. `init` wires it to the real one. */
export function createReporter(options: Options, env: Env): Reporter {
  const o = resolve(options);
  const timeoutMs = o.timeoutMs ?? 3000;
  const maxPerMinute = o.maxPerMinute ?? 60;
  const maxInFlight = o.maxInFlight ?? 5;
  const inflight = new Set<Promise<void>>();
  let sentAt: number[] = [];

  // An unusable hub is never silent: a mistyped address would otherwise look exactly like an app with no errors. It says
  // so once, here, and then sends nothing. It never throws, because a reporter must not break the app it reports on.
  const refusal = o.hub ? hubRefusal(o.hub) : null;
  if (refusal) {
    try {
      (env.warn ?? ((m: string) => console.error(m)))(refusal);
    } catch {
      // even the warning must not break the app
    }
  }

  function capture(error: unknown, level: Level = "error", request?: RequestInfo): void {
    try {
      if (!o.hub || !o.key || !o.secret) return; // not configured: report nothing, break nothing
      if (refusal) return; // already said so, once, when it was created
      const now = env.now();
      sentAt = sentAt.filter((t) => now - t < 60_000);
      if (sentAt.length >= maxPerMinute || inflight.size >= maxInFlight) return;
      sentAt.push(now);

      const { url, headers, body } = prepare(o, error, level, request);
      const p: Promise<void> = Promise.resolve()
        .then(() => env.send(url, headers, body, timeoutMs))
        .catch(() => {}) // a failed report is not the app's problem
        .finally(() => inflight.delete(p));
      inflight.add(p);
    } catch {
      // reporting must never break the app
    }
  }

  async function flush(wait = 2000): Promise<void> {
    try {
      if (inflight.size === 0) return;
      let timer: ReturnType<typeof setTimeout> | undefined;
      const timeout = new Promise<void>((res) => {
        timer = setTimeout(res, wait);
      });
      await Promise.race([Promise.allSettled([...inflight]).then(() => {}), timeout]);
      if (timer) clearTimeout(timer);
    } catch {
      // nothing to do
    }
  }

  return { capture, flush, options: o };
}

const realEnv: Env = {
  now: () => Date.now(),
  async send(url, headers, body, timeoutMs) {
    const res = await fetch(url, { method: "POST", headers, body, signal: AbortSignal.timeout(timeoutMs) });
    await res.body?.cancel(); // free the connection; the answer is not needed
  },
};

let active: Reporter | null = null;

/** Configure the reporter. Returns null (and does nothing) if the hub, key or secret is missing. */
export function init(options: Options = {}): Reporter | null {
  try {
    const reporter = createReporter(options, realEnv);
    const o = reporter.options;
    if (!o.hub || !o.key || !o.secret) return null;
    if (hubRefusal(o.hub)) return null; // createReporter has already said why
    active = reporter;
    return reporter;
  } catch {
    return null;
  }
}

/** Report an error you caught yourself. */
export function captureException(error: unknown, level: Level = "error", request?: RequestInfo): void {
  try {
    active?.capture(error, level, request);
  } catch {
    // never throw into the caller
  }
}

/** Wait for reports already on their way, for example before a deliberate shutdown. */
export async function flush(timeoutMs = 2000): Promise<void> {
  try {
    await active?.flush(timeoutMs);
  } catch {
    // nothing to do
  }
}

// The few Express fields used, written out so this file needs no Express types.
interface ReqLike {
  method?: string;
  path?: string;
  originalUrl?: string;
  url?: string;
}

/**
 * Express error middleware. Reports, then hands the error on with next(err), so the app's own error
 * handling and response are exactly what they were. Register it after the routes.
 */
export function expressErrorHandler(): (err: unknown, req: ReqLike, res: unknown, next: (e?: unknown) => void) => void {
  return (err, req, _res, next) => {
    try {
      const path = req?.path ?? (req?.originalUrl ?? req?.url ?? "").split("?")[0];
      captureException(err, "error", { method: req?.method, path });
    } catch {
      // never block the app's own handler
    }
    next(err);
  };
}

/**
 * Report a crash. An uncaught exception or unhandled rejection kills the process; this reports it as
 * `fatal` (P1 by default), waits up to two seconds for the report to leave, prints the error as Node
 * would have, and exits with code 1 as Node would have. It only exits itself if nothing else is
 * listening for the crash: if the app has its own handler, this reports and gets out of the way.
 * Returns a function that removes it.
 */
export function installProcessHandlers(): () => void {
  try {
    const others = process.listenerCount("uncaughtException");
    const handler = (err: unknown) => {
      try {
        console.error(err);
      } catch {
        // stderr may be gone
      }
      const level = active?.options.crashLevel ?? "fatal";
      captureException(err, level);
      if (others === 0) {
        void flush(2000).finally(() => process.exit(1));
      }
    };
    process.on("uncaughtException", handler);
    return () => {
      process.off("uncaughtException", handler);
    };
  } catch {
    return () => {};
  }
}
