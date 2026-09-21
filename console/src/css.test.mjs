// Guards against a CSS class-name collision. Run with: npm test
//
// The bug this exists for: a status dot's "stopped" state was the class `idle`, and
// the idle SCREEN already owned a bare `.idle { min-height: 100vh; padding: ... }`.
// So every stopped dot inherited a full-height page style and became 72px by
// 1000px, making a stack card 3,231px tall. Nothing failed and no test noticed: it
// only showed on screen.
//
// A "modifier" class (`.dot.idle`, `h1.ok`, `.tile.bad`) only makes sense attached to
// its component. If the same name also appears as a bare, global rule (`.idle { }`),
// that rule hits every element carrying the class, in every component.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

// every selector in the file, with at-rule headers (@media, @keyframes) and their
// keyframe steps (`from`, `50%`) skipped
const selectors = [];
for (const [, head] of css.matchAll(/([^{}]+)\{/g)) {
  const text = head.trim();
  if (!text || text.startsWith("@")) continue;
  for (const part of text.split(",")) {
    const sel = part.trim();
    if (/^(from|to|\d+%)$/.test(sel)) continue;
    selectors.push(sel);
  }
}

// names used as an attached modifier: `.dot.idle`, `.tile.bad`, `h1.ok`, `.ribbon i.deg`
const modifiers = new Set();
for (const sel of selectors) {
  for (const m of sel.matchAll(/(?:\.[\w-]+|\b[a-z][a-z0-9]*)\.([a-z][\w-]*)/g)) modifiers.add(m[1]);
}

// a selector that is nothing but one bare class, ignoring pseudo-classes: `.idle`, `.idle:hover`
const bare = (sel) => {
  const m = sel.replace(/::?[\w-]+(\([^)]*\))?/g, "").match(/^\.([a-z][\w-]*)$/);
  return m ? m[1] : null;
};

// Global utility classes on purpose. `.muted` is defined once as a utility and reinforced by a
// compound selector that wants the same colour, which is not a collision.
const UTILITIES = new Set(["muted"]);

let n = 0;
const t = (name, fn) => {
  fn();
  n++;
  console.log("  ok  ", name);
};

t("the stylesheet was actually parsed", () => {
  assert.ok(selectors.length > 200, `only found ${selectors.length} selectors`);
  assert.ok(modifiers.size >= 8, `only found modifiers: ${[...modifiers]}`);
  for (const known of ["idle", "bad", "warn", "ok", "down", "up"]) assert.ok(modifiers.has(known), `expected modifier .${known}`);
});

t("no modifier class is also defined as a bare global rule", () => {
  const collisions = selectors
    .map((sel) => ({ sel, name: bare(sel) }))
    .filter(({ name }) => name && modifiers.has(name) && !UTILITIES.has(name))
    .map(({ sel }) => sel);
  assert.deepEqual(
    collisions,
    [],
    `bare rules that also match a modifier of another component: ${collisions.join(", ")}. ` +
      "Rename one of them (component modifiers keep their short name; the odd one out gets a specific one).",
  );
});

console.log(`\n${n} groups passed`);
