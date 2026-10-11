# Console TypeScript migration (planned)

> **Status: done (2026-10-11).** All seven phases are complete: tooling, generated API types, the pure logic modules,
> `hooks.ts` and `ui.tsx`, the shell, the seven views, and tightening (`allowJs` dropped, nothing in `console/src` is
> JavaScript but the `.mjs` tests). Kept as the record of how it was done and why.
>
> **TypeScript is pinned at 5.9.3, not 7.** `openapi-typescript` 7 declares a peer of `typescript ^5`, so 7.0.2 would not
> install beside it, and forcing the mismatch would risk the generator running on a compiler it was not built for.
> Revisit when `openapi-typescript` supports 7.
>
> **Found by typing:** `rateView` threw when the feed said `available` but sent no `rate` (fixed first, with a test, in
> its own commit).

## Why

The console is React written as plain `.jsx` and `.js`, about 4,000 lines across 30 files. It reads about a
dozen JSON shapes from the hub (`SiteHealthOut`, `IssueOut`, `StackOut`, `DeployOut` and so on). Nothing checks
that the console and the hub agree about them, so a renamed or removed field fails quietly at runtime.

That matters here more than usual, because this project's worst failure is a silent one: **silence must not
read as all-clear** (CLAUDE.md). Several console rules exist to prevent exactly that, and they depend on fields
being present and meaning what the code thinks:

| Rule | Field it relies on |
|---|---|
| A silent probe is never `up` | `probe_stale`, `monitored`, `last_check` |
| A silent stack reporter is never healthy | `stale` on `StackOut` |
| A stale media snapshot cannot claim someone is watching | `sessions`, `stale` |
| An unreachable hub never shows "All quiet" | the poll's error state |

With types, removing or renaming one of those on the hub is a compile error in the console, not a wrong
verdict on the wall display.

Secondary gains: safer refactors in the 300+ line views (`Overview`, `Idle`, `Staging`), and editor
completion. The browser reporter (`client/browser/nightjar.ts`) is already TypeScript and type-checks under
`--strict`, so the toolchain is known to work on this machine.

## What it is not

- **Not a rewrite.** Behaviour and appearance do not change. A migration commit that changes behaviour is a
  bad commit.
- **Not runtime validation.** No zod or similar. The API is same-origin and ours, so types are enough, and a
  validation library adds bundle weight for a console that runs on a wall display.
- **Not the hub.** The hub is Python. Only `console/` moves.

## Size

| Part | Files | Lines | Notes |
|---|---|---|---|
| Pure logic (`.js`) | `promotion`, `media`, `weather`, `home`, `push`, `monitoring` | about 580 | Already unit tested by Node, no React |
| Hooks and helpers | `hooks.js` | 177 | `usePoll`, `send`, `useReveal`: the types everything else leans on |
| Shared UI | `ui.jsx` | 227 | `SiteCard`, `Odometer`, `Pill`, `Dot`, `Sev` |
| Shell | `App`, `Root`, `Login`, `Splash`, `main` | about 450 | |
| Small components | `PushCard`, `WeatherLine`, `StatusTray` | about 270 | |
| Views | `Overview` 447, `Idle` 335, `Staging` 333, `Media` 222, `Incidents` 212, `Requests` 164, `Deploys` 58 | about 1,770 | The bulk of the effort |
| Tests | seven `.mjs` files | about 600 | Stay `.mjs`, see the Node note below |

My estimate is two to three focused sessions, with the views being most of it. That is a guess, not a
measurement; the first phases will show whether it is right.

## The plan, in order

Each phase ends with `npm test` passing and a commit. Every phase leaves the app working, so the work can stop
after any of them.

### Phase 1: tooling, nothing renamed

Cheap, and worth doing before more console code is written.

- Add `typescript`, `@types/react` and `@types/react-dom` (React 19 versions) as dev dependencies, pinned.
- Add `console/tsconfig.json`: `allowJs: true`, `checkJs: false`, `noEmit: true`, `jsx: "react-jsx"`,
  `moduleResolution: "bundler"`, `allowImportingTsExtensions: true`, `erasableSyntaxOnly: true`,
  `strict: true`. Strict applies to `.ts` and `.tsx` files only while `checkJs` is off, so the existing code
  does not suddenly fail.
- Add `tsc --noEmit` to `npm test`.
- From this point, **new console files are `.tsx` or `.ts`.** The add-site form, when it is built, is the first.

### Phase 2: generated API types

The contract is the hub's Pydantic models, so the types should come from them rather than be written twice.

1. A small script, `tools/export_openapi.py`, writes `app.openapi()` to `console/src/api/openapi.json`. It
   cannot read it from a running hub, because the hub **turns the schema off when auth is on** (`hub/main.py`).
2. `openapi-typescript` (a pinned dev dependency) turns that into `console/src/api/schema.d.ts`. Both files are
   committed.
3. `console/src/api/types.ts` gives the shapes friendly names (`export type SiteHealth = components["schemas"]["SiteHealthOut"]`).
4. **A drift test in Python**, `tests/test_openapi_snapshot.py`, fails if the committed `openapi.json` differs from
   `app.openapi()`. It needs no Node and runs in the existing suite, so a hub change without regenerating the
   types fails CI. This is the part that makes the whole thing worth doing.

Things to check when it is built: Pydantic `Literal` becomes a string union (good: `state: "up" | "warn" | "down"`
is what the console wants), `datetime` becomes `string` (the console already treats it as an ISO string), and
response models should be generated in their *output* shape, where fields with defaults are required.

### Phase 3: the pure logic modules

`monitoring`, `push`, `media`, `weather`, `home`, `promotion` become `.ts`, in that order (smallest first).
They have no React in them, so this is the safest place to learn the types.

**The Node note.** The seven `.mjs` tests import these files, and Node does not resolve `./promotion.js` to
`./promotion.ts` the way Vite does. So imports are written with the real extension (`./promotion.ts`), which
`allowImportingTsExtensions` permits, and Node 24 runs the `.ts` files directly (the browser reporter's tests
already do). That only works for files with **no JSX and no non-erasable syntax** (no enums, no parameter
properties), which is why `erasableSyntaxOnly` is on. The tests therefore stay `.mjs` and keep running without a
build step. Files with JSX are never imported by a test, so they are not affected.

### Phase 4: hooks and shared UI

`hooks.js` to `hooks.ts` and `ui.jsx` to `ui.tsx`. The important one is `usePoll`:

```ts
function usePoll<T>(url: string, ms?: number): { data: T | null; error: boolean; updatedAt: number | null };
```

It makes every `f.sites`, `f.stacks`, `f.deploys` in the views typed at once. The `send()` helper takes the
generated request bodies.

### Phase 5: shell and small components

`main`, `Root`, `App`, `Login`, `Splash`, `PushCard`, `WeatherLine`, `StatusTray`.

### Phase 6: the views

Smallest first so the patterns settle: `Deploys`, `Requests`, `Incidents`, `Media`, `Staging`, `Idle`,
`Overview`. One view per commit.

### Phase 7: tighten and finish

- With nothing left in JavaScript, drop `allowJs`.
- Consider `noUncheckedIndexedAccess` (catches `rising[0].site` on an empty list). It is noisy, so it is optional
  and judged on how many real bugs it finds in a trial run.
- Update CLAUDE.md: the stack table ("React 19 + Vite" becomes "React 19 + TypeScript + Vite"), the layout, and
  the `npm test` line.

## Rules while migrating

1. **No `any`.** Where the type is genuinely unknown (a DOM event, a third-party return), use `unknown` and
   narrow it. An `any` needs a comment saying why.
2. **No behaviour changes in a migration commit.** If a type error reveals a real bug, fix it in its own
   commit first, with a test, then migrate the file. A bug found this way is a good outcome and worth recording
   in `docs/DECISIONS.md`.
3. **Do not mirror the hub's types by hand.** If a type is wanted that the generated file lacks, change the
   Pydantic model, not the console.
4. **The "silence is not all-clear" fields stay non-optional** in the types. If the hub could omit one, the console
   should fail to compile, not guess.
5. **Keep the CSS collision guard.** `css.test.mjs` reads the stylesheet, not the components, and is unaffected.

## Done when

- `npm test` passes, including `tsc --noEmit` under `strict`.
- No `.js` or `.jsx` is left in `console/src/` apart from the `.mjs` tests.
- A hub schema change without regenerating `openapi.json` fails the Python suite.
- `vite build` output behaves the same. Check the Overview, Staging and Idle screens by eye, since no test
  covers rendering.

## Open questions

1. **Before or after the Gitea CI work?** After is better: a CI job can then run `tsc` and the drift test on every
   push. Phase 1 can happen before, since it costs little.
2. **Is `noUncheckedIndexedAccess` wanted?** It is the strictest setting and the noisiest. Decide after Phase 6.
3. **Where do shared types for the browser reporter live?** It is a separate vendored file with no dependencies,
   so it keeps its own types and does not import from the console.
