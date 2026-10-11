// The Node reporter's rules, with no network where possible. Run with: node client/node/nightjar.test.mjs
// (Node 22.18 or newer runs the .ts file directly.)

import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import { captureException, createReporter, expressErrorHandler, flush, hubRefusal, init, parseStack, prepare } from "./nightjar.ts";

const ok = (name) => console.log(`  ok   ${name}`);
const OPTS = { hub: "https://nightjar.example/", key: "KEY1", secret: "s3cret", release: "v2" };

function harness(options = {}, over = {}) {
  const sent = [];
  let t = 1_000_000;
  const env = {
    send: async (url, headers, body) => void sent.push({ url, headers, body: JSON.parse(body), raw: body }),
    now: () => t,
    ...over,
  };
  return { reporter: createReporter({ ...OPTS, ...options }, env), sent, tick: (ms) => (t += ms) };
}
const settle = () => new Promise((r) => setTimeout(r, 5));

// ---- a hub that is not https ----------------------------------------------------------------------------------

{
  for (const ok_ of ["https://nightjar.example", "https://nightjar.example/", "http://localhost:8000", "http://127.0.0.1:9", "http://[::1]:8000", "http://app.localhost"]) {
    assert.equal(hubRefusal(ok_), null, `${ok_} is fine`);
  }
  for (const bad of ["http://nightjar.example", "http://10.0.0.5", "http://192.168.1.20:8000", "http://hub.test", "ftp://nightjar.example", "nightjar.example", "//nightjar.example"]) {
    assert.ok(hubRefusal(bad), `${bad} is refused`);
  }
  // the reason names the host but never the whole address, which could carry a credential
  assert.doesNotMatch(hubRefusal("http://user:pa55word@nightjar.example/path?token=abc"), /pa55word|token=abc|user:/);
  ok("only https, or http to this machine, is accepted as a hub");
}

{
  const warned = [];
  const sent = [];
  const r = createReporter(
    { hub: "http://nightjar.example", key: "K", secret: "s" },
    { send: async (...a) => void sent.push(a), now: () => 1, warn: (m) => warned.push(m) },
  );
  assert.equal(warned.length, 1, "it says so once, when created, not silently");
  assert.match(warned[0], /refusing to report errors to http:\/\/nightjar\.example/);
  assert.doesNotThrow(() => {
    r.capture(new Error("boom"));
    r.capture(new Error("boom again"));
  }, "refusing must never throw");
  assert.equal(sent.length, 0, "and nothing is sent");
  assert.equal(warned.length, 1, "it does not repeat itself on every error");
  ok("an http:// hub sends nothing, throws nothing, and logs once");
}

{
  const warned = [];
  const sent = [];
  for (const hub of ["https://nightjar.example", "http://127.0.0.1:8000", "http://localhost:8000"]) {
    const r = createReporter({ hub, key: "K", secret: "s" }, { send: async (...a) => void sent.push(a), now: () => 1, warn: (m) => warned.push(m) });
    r.capture(new Error("boom"));
  }
  await settle();
  assert.deepEqual(warned, [], "a good hub is not warned about");
  assert.equal(sent.length, 3, "https and loopback http still report");
  ok("https and loopback http still work");
}

{
  // not configured at all stays quiet, as before: that is a different state from a hub that is wrong
  const warned = [];
  createReporter({ key: "K", secret: "s" }, { send: async () => {}, now: () => 1, warn: (m) => warned.push(m) });
  assert.deepEqual(warned, []);
  // init with a refused hub does nothing, throws nothing, and returns null
  const saved = console.error;
  const lines = [];
  console.error = (m) => lines.push(m);
  try {
    assert.equal(init({ hub: "http://nightjar.example", key: "K", secret: "s" }), null);
  } finally {
    console.error = saved;
  }
  assert.equal(lines.length, 1, "init says why on standard error by default");
  ok("init refuses an http:// hub out loud, and an unset hub stays quiet");
}

// ---- parsing -------------------------------------------------------------------------------

{
  const frames = parseStack(
    [
      "TypeError: x",
      "    at handler (/srv/app/routes/cart.js:42:11)",
      "    at async Layer.handle [as handle_request] (/srv/app/node_modules/express/lib/router/layer.js:95:5)",
      "    at new Cart (file:///srv/app/cart.mjs:7:3)",
      "    at /srv/app/anon.js:3:1",
      "    at C:\\app\\win.js:9:2",
      "    at fn (node:internal/process/task_queues:105:5)",
    ].join("\n"),
  );
  assert.equal(frames.length, 6);
  assert.deepEqual(frames.map((f) => f.module), ["task_queues", "win.js", "anon.js", "cart.mjs", "layer.js", "cart.js"]);
  assert.equal(frames.at(-1).function, "handler", "innermost is LAST, the order the hub expects");
  assert.equal(frames.at(-1).lineno, 42);
  assert.equal(frames[3].function, "Cart", "'new' removed");
  assert.equal(frames[3].file, "/srv/app/cart.mjs", "file:// removed");
  assert.equal(frames[2].function, undefined, "an anonymous frame has no function");
  assert.equal(frames[1].file, "C:\\app\\win.js", "a Windows path keeps its drive letter");
  assert.equal(frames[1].module, "win.js");
  ok("V8 stacks: innermost last, async/new removed, file URLs and Windows paths handled");
  assert.deepEqual(parseStack(undefined), []);
  assert.deepEqual(parseStack("garbage"), []);
  assert.equal(parseStack(Array.from({ length: 300 }, (_, i) => `    at f${i} (/a.js:${i + 1}:1)`).join("\n")).length, 100);
  ok("no stack, a nonsense stack and a very deep stack");
}

// ---- the signed request --------------------------------------------------------------------

{
  const err = new RangeError("out of range");
  const p = prepare(OPTS, err, "error", { method: "POST", path: "/cart?token=SECRET" });
  assert.equal(p.url, "https://nightjar.example/ingest/KEY1/event", "trailing slash on the hub is tolerated");
  const expected = "sha256=" + createHmac("sha256", "s3cret").update(p.body).digest("hex");
  assert.equal(p.headers["X-Hub-Signature"], expected, "the HMAC is over exactly the bytes sent");
  const b = JSON.parse(p.body);
  assert.deepEqual([b.exception_type, b.message, b.level, b.release, b.environment], ["RangeError", "out of range", "error", "v2", "production"]);
  assert.deepEqual(b.request, { method: "POST", path: "/cart" }, "the query string is never sent");
  assert.ok(b.frames.length > 0);
  ok("a request is signed over its exact bytes, and carries no query string");

  const viaEnv = (() => {
    const old = { ...process.env };
    Object.assign(process.env, { NIGHTJAR_URL: "https://h.example", NIGHTJAR_KEY: "K9", NIGHTJAR_SECRET: "e", NIGHTJAR_ENV: "staging", NIGHTJAR_RELEASE: "r1" });
    try {
      return prepare({}, new Error("x"));
    } finally {
      process.env = old;
    }
  })();
  assert.equal(viaEnv.url, "https://h.example/ingest/K9/event");
  assert.equal(JSON.parse(viaEnv.body).environment, "staging");
  ok("the three NIGHTJAR_* variables are the defaults");
}

{
  const { reporter, sent } = harness();
  reporter.capture("a string was thrown");
  reporter.capture({ code: 7 });
  await settle();
  assert.deepEqual(sent.map((s) => [s.body.exception_type, s.body.message]), [["NonErrorThrown", "a string was thrown"], ["NonErrorThrown", '{"code":7}']]);
  ok("a thrown string or object is still reported");
}

// ---- never breaking the app ----------------------------------------------------------------

{
  assert.equal(init({}), null, "no configuration: nothing happens");
  assert.doesNotThrow(() => captureException(new Error("x")), "capturing before init is harmless");
  const { reporter } = harness({ hub: "", key: "", secret: "" });
  assert.doesNotThrow(() => reporter.capture(new Error("x")));
  ok("an unconfigured reporter does nothing and throws nothing");
}

{
  const { reporter } = harness({}, { send: () => Promise.reject(new Error("network down")) });
  assert.doesNotThrow(() => reporter.capture(new Error("x")));
  await settle(); // an unhandled rejection here would crash this test run
  const sync = harness({}, { send: () => { throw new Error("sync boom"); } });
  assert.doesNotThrow(() => sync.reporter.capture(new Error("x")));
  const hostile = { get message() { throw new Error("getter"); }, get stack() { throw new Error("getter"); }, get name() { throw new Error("getter"); } };
  assert.doesNotThrow(() => harness().reporter.capture(hostile));
  assert.doesNotThrow(() => harness().reporter.capture(null));
  assert.doesNotThrow(() => harness().reporter.capture(undefined));
  ok("a broken network or a hostile error object never throws into the app");
}

// ---- the brakes ----------------------------------------------------------------------------

{
  const { reporter, sent, tick } = harness({ maxPerMinute: 3 });
  for (let i = 0; i < 10; i++) reporter.capture(new Error(`e${i}`));
  await settle();
  assert.equal(sent.length, 3, "a runaway loop cannot send more than the cap");
  tick(61_000);
  reporter.capture(new Error("later"));
  await settle();
  assert.equal(sent.length, 4, "the minute rolls over");
  ok("a cap on reports per minute");
}

{
  let release;
  const gate = new Promise((r) => (release = r));
  let started = 0;
  const { reporter } = harness({ maxInFlight: 2, maxPerMinute: 100 }, { send: async () => { started++; await gate; } });
  for (let i = 0; i < 10; i++) reporter.capture(new Error(`e${i}`));
  assert.equal(started, 0);
  await settle();
  assert.equal(started, 2, "with the hub hung, only two sends pile up and the rest are dropped, not queued");
  release();
  await reporter.flush(500);
  reporter.capture(new Error("after"));
  await settle();
  assert.equal(started, 3, "and it recovers once they finish");
  ok("a hung hub cannot pile up work: sends in flight are capped and nothing is queued");
}

{
  const { reporter } = harness({}, { send: () => new Promise(() => {}) });
  reporter.capture(new Error("never finishes"));
  const t0 = Date.now();
  await reporter.flush(100);
  assert.ok(Date.now() - t0 < 1000, "flush gives up after its timeout");
  ok("flush never waits forever");
}

// ---- Express -------------------------------------------------------------------------------

{
  const calls = [];
  const handler = expressErrorHandler();
  const err = new Error("route failed");
  let nextArg = "unset";
  handler(err, { method: "GET", originalUrl: "/orders/7?token=SECRET" }, {}, (e) => (nextArg = e));
  assert.equal(nextArg, err, "the error is passed on untouched, so the app's own handler still runs");
  handler(err, undefined, undefined, () => calls.push("ok"));
  handler(null, {}, {}, () => calls.push("ok2"));
  assert.deepEqual(calls, ["ok", "ok2"], "even odd arguments never stop next()");
  ok("the Express middleware reports and always calls next(err)");
}

// ---- real processes: a crash is reported, and still behaves like a crash -------------------

async function crash(script, hubUrl) {
  const here = fileURLToPath(new URL("./nightjar.ts", import.meta.url));
  const child = spawn(process.execPath, ["--input-type=module", "-e", script.replaceAll("__LIB__", here.replaceAll("\\", "/"))], {
    env: { ...process.env, NIGHTJAR_URL: hubUrl, NIGHTJAR_KEY: "KEY1", NIGHTJAR_SECRET: "s3cret" },
  });
  let stderr = "";
  child.stderr.on("data", (d) => (stderr += d));
  const started = Date.now();
  const code = await new Promise((r) => child.on("close", r));
  return { code, stderr, ms: Date.now() - started };
}

{
  const got = [];
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      got.push({ url: req.url, sig: req.headers["x-hub-signature"], body });
      res.writeHead(200).end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const hub = `http://127.0.0.1:${server.address().port}`;

  const thrown = await crash(`import { init, installProcessHandlers } from "file:///__LIB__"; init(); installProcessHandlers(); setTimeout(() => { throw new TypeError("kaboom"); }, 10);`, hub);
  assert.equal(thrown.code, 1, "still exits with code 1, as Node would have");
  assert.match(thrown.stderr, /kaboom/, "and still prints the error");
  assert.equal(got.length, 1, "and the crash reached the hub before the process died");
  const b = JSON.parse(got[0].body);
  assert.deepEqual([b.exception_type, b.message, b.level], ["TypeError", "kaboom", "fatal"]);
  assert.equal(got[0].url, "/ingest/KEY1/event");
  assert.equal(got[0].sig, "sha256=" + createHmac("sha256", "s3cret").update(got[0].body).digest("hex"));
  ok("an uncaught exception is reported as fatal, signed, and the process still exits 1");

  got.length = 0;
  const rejected = await crash(`import { init, installProcessHandlers } from "file:///__LIB__"; init(); installProcessHandlers(); Promise.reject(new Error("lost promise"));`, hub);
  assert.equal(rejected.code, 1);
  assert.equal(JSON.parse(got[0].body).message, "lost promise");
  ok("an unhandled rejection is reported the same way");

  got.length = 0;
  const handled = await crash(`import { init, installProcessHandlers } from "file:///__LIB__"; process.on("uncaughtException", () => { console.log("app handled it"); }); init(); installProcessHandlers(); setTimeout(() => { throw new Error("app has its own handler"); }, 10); setTimeout(() => process.exit(0), 400);`, hub);
  assert.equal(handled.code, 0, "when the app handles crashes itself, the reporter does not turn them into exits");
  assert.equal(got.length, 1, "but it still reports");
  ok("if the app has its own crash handler, the reporter reports and steps aside");

  server.close();
}

{
  // the hub is unreachable: the crash must still happen, promptly
  const dead = await crash(`import { init, installProcessHandlers } from "file:///__LIB__"; init(); installProcessHandlers(); setTimeout(() => { throw new Error("hub is down"); }, 10);`, "http://127.0.0.1:9");
  assert.equal(dead.code, 1);
  assert.ok(dead.ms < 6000, `took ${dead.ms} ms`);
  ok("with the hub unreachable the process still crashes, and promptly");
}

{
  // not configured at all: installing the handler must not change what a crash does
  const bare = await crash(`import { installProcessHandlers } from "file:///__LIB__"; installProcessHandlers(); setTimeout(() => { throw new Error("no config"); }, 10);`, "");
  assert.equal(bare.code, 1);
  assert.match(bare.stderr, /no config/);
  ok("without configuration a crash looks exactly as it does without the reporter");
}

console.log("\nnode reporter passed");
