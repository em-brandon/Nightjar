// The browser reporter's rules, with no browser. Run with: node client/browser/nightjar.test.mjs
// (Node 22.18 or newer runs the .ts file directly.)

import assert from "node:assert/strict";
import { createReporter, hubRefusal, parseStack, scrub } from "./nightjar.ts";

const ok = (name) => console.log(`  ok   ${name}`);
const ORIGIN = "https://birdbox.example";

function harness(options = {}, over = {}) {
  const sent = [];
  let t = 1_000_000;
  const env = {
    send: (url, body) => sent.push({ url, body: JSON.parse(body), raw: body }),
    now: () => t,
    page: () => `${ORIGIN}/cart?token=SECRET`,
    origin: () => ORIGIN,
    ...over,
  };
  const reporter = createReporter({ hub: "https://nightjar.example/", key: "PUBKEY", ...options }, env);
  return { reporter, sent, tick: (ms) => (t += ms) };
}

const errorAt = (message, file = `${ORIGIN}/assets/index-3fa9c1B2.js`, line = 10, name = "render") => {
  const e = new TypeError(message);
  e.stack = `TypeError: ${message}\n    at ${name} (${file}:${line}:5)\n    at run (${file}:${line + 20}:9)`;
  return e;
};

// ---- parsing -------------------------------------------------------------------------------

{
  const chrome = parseStack("TypeError: x\n    at render (https://a.example/assets/index-3fa9c1B2.js?v=2:10:5)\n    at async run (https://a.example/main.js:4:1)\n    at https://a.example/anon.js:7:2");
  assert.equal(chrome.length, 3);
  assert.deepEqual(chrome.map((f) => f.module), ["anon.js", "main.js", "index-3fa9c1B2.js"], "innermost last, the order the hub expects");
  assert.equal(chrome[2].function, "render");
  assert.equal(chrome[1].function, "run", "async prefix removed");
  assert.equal(chrome[0].function, undefined, "an anonymous frame has no function");
  assert.equal(chrome[2].file, "https://a.example/assets/index-3fa9c1B2.js", "query string removed");
  assert.equal(chrome[2].lineno, 10);
  ok("Chrome stacks, innermost last, query strings removed");

  const firefox = parseStack("render@https://a.example/app.js:10:5\nrun@https://a.example/app.js:30:9\n@https://a.example/boot.js:1:1");
  assert.deepEqual(firefox.map((f) => f.function), [undefined, "run", "render"]);
  assert.equal(firefox[2].lineno, 10);
  ok("Firefox and Safari stacks");

  assert.deepEqual(parseStack(undefined), []);
  assert.deepEqual(parseStack("garbage\nmore garbage"), []);
  assert.equal(parseStack(Array.from({ length: 100 }, (_, i) => `    at f${i} (https://a.example/a.js:${i + 1}:1)`).join("\n")).length, 30);
  ok("no stack, a nonsense stack and a very deep stack");
}

// ---- what is sent --------------------------------------------------------------------------

{
  const { reporter, sent } = harness({ release: "v1.4.2" });
  reporter.capture(errorAt("x is undefined"));
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, "https://nightjar.example/ingest/browser/PUBKEY", "the trailing slash on hub is tolerated");
  const b = sent[0].body;
  assert.deepEqual([b.exception_type, b.message, b.level, b.release, b.environment], ["TypeError", "x is undefined", "warning", "v1.4.2", "production"]);
  assert.equal(b.frames.at(-1).function, "render");
  ok("a report carries the type, message, frames, release and a default level of warning");

  reporter.capture(errorAt("a different one", undefined, 50), "error");
  assert.equal(sent[1].body.level, "error", "escalating on purpose works");
  ok("a deliberate capture can be an error");
}

{
  const { reporter, sent } = harness();
  reporter.capture("a string was thrown");
  reporter.capture({ code: 7 });
  assert.deepEqual(sent.map((s) => [s.body.exception_type, s.body.message]), [["UnhandledRejection", "a string was thrown"], ["UnhandledRejection", '{"code":7}']]);
  ok("a thrown string or object is still reported");
}

// ---- noise ---------------------------------------------------------------------------------

{
  const { reporter, sent } = harness();
  reporter.capture(new Error("Script error."));
  reporter.capture(new Error("ResizeObserver loop completed with undelivered notifications."));
  const ext = new Error("boom");
  ext.stack = "Error: boom\n    at f (chrome-extension://abc/content.js:1:1)";
  reporter.capture(ext);
  assert.equal(sent.length, 0);
  ok("browser quirks and extension errors are dropped");

  const third = new Error("boom");
  third.stack = "Error: boom\n    at track (https://analytics.example/t.js:1:1)";
  reporter.capture(third);
  assert.equal(sent.length, 0, "an error only in someone else's script is not this site's bug");
  const mixed = new Error("boom");
  mixed.stack = `Error: boom\n    at track (https://analytics.example/t.js:1:1)\n    at ours (${ORIGIN}/a.js:1:1)`;
  reporter.capture(mixed);
  assert.equal(sent.length, 1, "but a stack that passes through our code is reported");
  ok("only first-party errors are reported");
}

{
  const { reporter, sent } = harness({ ignore: [/flaky widget/] });
  reporter.capture(errorAt("the flaky widget broke"));
  assert.equal(sent.length, 0);
  const off = harness({ firstPartyOnly: false });
  const third = new Error("boom");
  third.stack = "Error: boom\n    at track (https://analytics.example/t.js:1:1)";
  off.reporter.capture(third);
  assert.equal(off.sent.length, 1);
  ok("the ignore list and first-party filter are configurable");
}

// ---- the brakes ----------------------------------------------------------------------------

{
  const { reporter, sent, tick } = harness();
  reporter.capture(errorAt("Cannot read 'abc'"));
  reporter.capture(errorAt("Cannot read 'xyz'"));
  reporter.capture(errorAt("item 7 failed"));
  reporter.capture(errorAt("item 8 failed"));
  assert.equal(sent.length, 2, "the same error with different values is sent once");
  tick(11 * 60_000);
  reporter.capture(errorAt("Cannot read 'abc'"));
  assert.equal(sent.length, 3, "and again after the dedupe window");
  ok("the same error is sent once per window");
}

{
  const { reporter, sent, tick } = harness({ maxPerMinute: 3 });
  for (let i = 0; i < 10; i++) reporter.capture(errorAt(`unique failure ${String.fromCharCode(97 + i)}`, undefined, 100 + i));
  assert.equal(sent.length, 3, "a runaway page cannot send more than the cap");
  tick(61_000);
  reporter.capture(errorAt("later failure", undefined, 999));
  assert.equal(sent.length, 4, "the minute rolls over");
  ok("a cap on reports per minute");
}

{
  const { reporter, sent } = harness();
  for (let i = 0; i < 200; i++) reporter.capture(errorAt(`kind${"x".repeat(i % 7)}`, `${ORIGIN}/f${i}.js`, i));
  assert.ok(sent.length <= 6);
  ok("memory stays bounded (the dedupe map is capped)");
}

// ---- size, and never breaking the page -----------------------------------------------------

{
  const { reporter, sent } = harness();
  const big = errorAt("y".repeat(50_000));
  big.stack = "TypeError: y\n" + Array.from({ length: 200 }, (_, i) => `    at fn${i} (${ORIGIN}/assets/${"d".repeat(150)}${i}.js:${i + 1}:1)`).join("\n");
  reporter.capture(big);
  assert.equal(sent.length, 1);
  assert.ok(sent[0].raw.length <= 15_000, `body is ${sent[0].raw.length} bytes`);
  assert.ok(sent[0].body.message.length <= 1500);
  assert.ok(sent[0].body.frames.length >= 1, "the innermost frames are kept");
  ok("an oversized report is trimmed under the hub's limit");
}

{
  const boom = () => {
    throw new Error("the transport is broken");
  };
  const { reporter } = harness({}, { send: boom });
  assert.doesNotThrow(() => reporter.capture(errorAt("anything")));
  const hostile = { get message() { throw new Error("getter"); }, get stack() { throw new Error("getter"); } };
  assert.doesNotThrow(() => harness().reporter.capture(hostile));
  assert.doesNotThrow(() => harness().reporter.capture(null));
  assert.doesNotThrow(() => harness().reporter.capture(undefined));
  ok("a broken transport or a hostile error object never throws into the page");
}

// ---- a hub that is not https ----------------------------------------------------------------------------------

{
  for (const good of ["https://nightjar.example", "https://nightjar.example/", "http://localhost:8000", "http://127.0.0.1:9", "http://[::1]:8000", "http://app.localhost"]) {
    assert.equal(hubRefusal(good), null, `${good} is fine`);
  }
  for (const bad of ["http://nightjar.example", "http://10.0.0.5", "http://192.168.1.20:8000", "http://hub.test", "ftp://nightjar.example", "nightjar.example", "//nightjar.example"]) {
    assert.ok(hubRefusal(bad), `${bad} is refused`);
  }
  assert.doesNotMatch(hubRefusal("http://user:pa55word@nightjar.example/path?token=abc"), /pa55word|token=abc|user:/);
  ok("only https, or http to this machine, is accepted as a hub");
}

{
  const warned = [];
  const { reporter, sent } = harness({ hub: "http://nightjar.example" }, { warn: (m) => warned.push(m) });
  assert.equal(warned.length, 1, "it says so once, when created, not silently");
  assert.match(warned[0], /refusing to report errors to http:\/\/nightjar\.example/);
  assert.doesNotThrow(() => {
    reporter.capture(errorAt("boom"));
    reporter.capture(errorAt("a different boom", undefined, 99));
  }, "refusing must never throw into the page");
  assert.equal(sent.length, 0, "and nothing is sent");
  assert.equal(warned.length, 1, "it does not repeat itself on every error");
  ok("an http:// hub sends nothing, throws nothing, and logs once");
}

{
  for (const hub of ["https://nightjar.example", "http://127.0.0.1:8000", "http://localhost:8000"]) {
    const warned = [];
    const { reporter, sent } = harness({ hub }, { warn: (m) => warned.push(m) });
    reporter.capture(errorAt("boom"));
    assert.equal(sent.length, 1, `${hub} still reports`);
    assert.deepEqual(warned, [], `${hub} is not warned about`);
  }
  ok("https and loopback http still work");
}

{
  const out = scrub("login failed for bob@example.com with password=hunter2 via https://u:pw@db.internal/x, Authorization: Bearer abcdef123456789");
  for (const leaked of ["hunter2", "u:pw", "abcdef123456789", "bob@"]) assert.ok(!out.includes(leaked), `${leaked} leaked: ${out}`);
  assert.equal(scrub("cart is empty for order 42"), "cart is empty for order 42");
  ok("scrub removes passwords, URL credentials, bearer tokens and emails, and leaves ordinary text");
  const { reporter, sent } = harness();
  reporter.capture(errorAt("invalid email bob@example.com, token=abc123"));
  assert.equal(sent.length, 1);
  assert.ok(!sent[0].raw.includes("bob@") && !sent[0].raw.includes("abc123"), sent[0].raw);
  assert.ok(!sent[0].raw.includes("SECRET") && sent[0].body.page === `${ORIGIN}/cart`, sent[0].body.page);
  ok("a report carries a scrubbed message and a page address with no query string");
}

console.log("\nbrowser reporter passed");
