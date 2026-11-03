import { AppWindow, Building2, FlaskConical, Globe, Plus } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import type { AuditEntry, SiteCreated, SiteHealth, SiteListed, SiteOut, TestEventResult } from "../api/types.ts";
import type { Feeds } from "../feeds.ts";
import { ago, plural, send, usePoll } from "../hooks.ts";
import { groupByClient, worstState } from "../grouping.ts";
import { SECRET_NAMES, STACKS, snippet, type StackId } from "../snippets.ts";
import { Dot, Empty, Pill } from "../ui.tsx";

// Add a site, see its keys once with a ready-made setup snippet, and watch for its first report.
// Rules (docs/ONBOARDING-DASHBOARD.md): the secret is shown once and lives only in this component's
// state; a site that has never reported says so; nothing here touches alert routing.

const WAIT_MS = 3 * 60_000;
// A secret left on screen is a secret anyone walking past can read, so it clears itself.
const SECRET_SHOWN_MS = 10 * 60_000;

const why = (err: unknown) => (err instanceof Error ? err.message : String(err));
const lines = (text: string) => text.split("\n").map((l) => l.trim()).filter(Boolean);
const mmss = (ms: number) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

// What the keys panel is showing, and for which site. The secret is null unless it was just created
// or rotated: the hub never returns it again.
type Shown = {
  kind: "created" | "rotated" | "setup";
  siteId: number;
  name: string;
  ingestKey: string;
  ingestSecret: string | null;
  publicKey: string | null;
  startedAt: number;
  warning?: string;
};

function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setState("done");
    } catch {
      setState("failed"); // no clipboard (an http page, a denied permission): select and copy by hand
    }
    setTimeout(() => setState("idle"), 2000);
  };
  return (
    <button type="button" className="btn" onClick={copy}>
      {state === "done" ? "Copied" : state === "failed" ? "Select and copy" : label}
    </button>
  );
}

function CodeBlock({ text }: { text: string }) {
  return (
    <div className="sx-code">
      <pre>{text}</pre>
      <CopyButton text={text} />
    </div>
  );
}

function KeysPanel({ shown, onDone }: { shown: Shown; onDone: () => void }) {
  const [stack, setStack] = useState<StackId>("python");
  // The address apps use to reach the hub is not always the one this console is served from.
  const [hub, setHub] = useState(location.origin);
  const s = snippet({ stack, hub, ingestKey: shown.ingestKey, ingestSecret: shown.ingestSecret, publicKey: shown.publicKey });
  const title = shown.kind === "created" ? "Site created" : shown.kind === "rotated" ? "New secret" : "Setup";

  return (
    <section className="card sx-panel" aria-label={`${title}: ${shown.name}`}>
      <div className="sec sx-head">
        <h2>
          {title}: {shown.name}
        </h2>
      </div>
      {shown.warning && (
        <p className="form-error" role="alert">
          {shown.warning}
        </p>
      )}

      <div className="sx-keys">
        <div className="sx-key">
          <span className="k">Ingest key</span>
          <code>{shown.ingestKey}</code>
          <CopyButton text={shown.ingestKey} />
        </div>
        <div className="sx-key">
          <span className="k">Ingest secret</span>
          {shown.ingestSecret ? (
            <>
              <code>{shown.ingestSecret}</code>
              <CopyButton text={shown.ingestSecret} />
            </>
          ) : (
            <span className="muted">not shown again. Rotate it to get a new one.</span>
          )}
        </div>
      </div>
      {shown.ingestSecret && (
        <p className="sx-once" role="status">
          Copy the secret now. It is shown once and cannot be looked up later; losing it means rotating it.
        </p>
      )}

      <label className="sx-hub">
        <span className="k">Hub address the app will use</span>
        <input value={hub} onChange={(e) => setHub(e.target.value)} spellCheck={false} />
      </label>

      <div className="chips" role="group" aria-label="Stack">
        {STACKS.map(({ id, label }) => (
          <button key={id} type="button" className={`chip ${stack === id ? "on" : ""}`} aria-pressed={stack === id} onClick={() => setStack(id)}>
            {label}
          </button>
        ))}
      </div>

      {s.env && (
        <>
          <h3 className="sx-sub">Environment</h3>
          <CodeBlock text={s.env} />
          <p className="hint">
            In CI, store these as secrets named{" "}
            {SECRET_NAMES.map((n, i) => (
              <span key={n}>
                {i > 0 && ", "}
                <code>{n}</code>
              </span>
            ))}
            , so a rotation is one edit. The console cannot write them for you.
          </p>
        </>
      )}
      <h3 className="sx-sub">{s.filename}</h3>
      <CodeBlock text={s.code} />
      {s.notes.map((n) => (
        <p key={n} className="hint">
          {n}
        </p>
      ))}

      <div className="sx-actions">
        <button type="button" className="btn pri" onClick={onDone}>
          Done
        </button>
        {shown.ingestSecret && <span className="muted">Done clears the secret from this page.</span>}
      </div>
    </section>
  );
}

function WaitingPanel({ shown, site, health, now, onTest }: { shown: Shown; site: SiteListed | undefined; health: SiteHealth | undefined; now: number; onTest: () => Promise<TestEventResult | null> }) {
  const [tested, setTested] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const elapsed = now - (site ? new Date(site.created_at).getTime() : shown.startedAt);
  const reported = site?.last_event ?? null;

  const test = async () => {
    setBusy(true);
    try {
      const r = await onTest();
      setTested(
        r
          ? `The hub accepted a ${r.severity} test event (${r.state_change ?? "known issue"}). That proves the hub side only, not that the app is sending anything.`
          : "The hub accepted the test event.",
      );
    } catch (err) {
      setTested(`The hub refused it: ${why(err)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card sx-panel" aria-label="First report" aria-live="polite">
      <div className="sec sx-head">
        <h2>First report</h2>
        {reported ? (
          <Pill tone="ok">received {ago(reported, now)}</Pill>
        ) : elapsed < WAIT_MS ? (
          <Pill tone="run">waiting {mmss(elapsed)}</Pill>
        ) : (
          <Pill tone="warn">nothing yet · {mmss(elapsed)}</Pill>
        )}
      </div>

      {reported ? (
        <p className="hint">The app has reported to the hub, so this site is wired up.</p>
      ) : elapsed < WAIT_MS ? (
        <p className="hint">Deploy or restart the app with the settings above, then cause an error. This checks every few seconds.</p>
      ) : (
        <>
          <p className="hint">Nothing has arrived. A wired site and a silent site look the same until something fails, so check:</p>
          <ul className="sx-checks">
            <li>The app was restarted or redeployed after the settings were added.</li>
            <li>
              <code>NIGHTJAR_URL</code> is the hub itself ({location.origin} unless it is behind another address), not the full event URL.
            </li>
            <li>The key and secret are exactly as shown, with nothing trailing. A wrong secret is refused by the hub.</li>
            <li>PHP-FPM hides environment variables unless the pool allows them. Pass the values in code instead.</li>
            <li>Nothing has actually failed yet. Use the test button to prove the hub side, then trigger a real error.</li>
          </ul>
        </>
      )}

      <p className="hint">
        {health?.monitored
          ? health.last_check == null
            ? "Uptime: awaiting the first check."
            : `Uptime: ${health.probe_stale ? "probe silent, last check " : "checked "}${ago(health.last_check, now)}.`
          : "Uptime: no url set, so this site is not checked. Add one under Edit below."}
      </p>

      <div className="sx-actions">
        <button type="button" className="btn" disabled={busy} onClick={test}>
          Send a test event
        </button>
        {tested && <span className="muted">{tested}</span>}
      </div>
    </section>
  );
}

type Prefill = Partial<{ client: string; name: string; env: string }>;

function AddForm({ clients, initial, onCreated }: { clients: string[]; initial?: Prefill; onCreated: (s: Shown) => void }) {
  const [form, setForm] = useState({ client: initial?.client ?? "", name: initial?.name ?? "", env: initial?.env ?? "production", url: "", origins: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await send<SiteCreated>("POST", "/api/sites", {
        name: form.name.trim(),
        client: form.client.trim(),
        env: form.env,
        url: form.url.trim() || null,
      });
      if (!created) throw new Error("the hub sent no answer");
      let publicKey: string | null = null;
      let warning: string | undefined;
      const origins = lines(form.origins);
      if (origins.length > 0) {
        // The site exists from here on, so a bad origin must not hide its keys: show them with the reason.
        try {
          const patched = await send<SiteOut>("PATCH", `/api/sites/${created.id}`, { allowed_origins: origins });
          publicKey = patched?.public_key ?? null;
        } catch (err) {
          warning = `The site was created, but the pages were not saved: ${why(err)}. Fix them under Edit in the list.`;
        }
      }
      setForm({ client: form.client, name: "", env: form.env, url: "", origins: "" });
      onCreated({
        kind: "created",
        siteId: created.id,
        name: created.name,
        ingestKey: created.ingest_key,
        ingestSecret: created.ingest_secret,
        publicKey,
        startedAt: Date.now(),
        warning,
      });
    } catch (err) {
      setError(why(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card form" onSubmit={submit}>
      <h2>Add a site</h2>
      <p className="hint">
        Creates the site and shows its keys once. A staging site must be named <code>&lt;production&gt;-staging</code>.
      </p>
      <div className="form-grid">
        <label>
          Client
          <input value={form.client} onChange={set("client")} list="sx-clients" required maxLength={120} placeholder="Kubasu Motors" />
          <datalist id="sx-clients">
            {clients.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
        </label>
        <label>
          Site name
          <input value={form.name} onChange={set("name")} required maxLength={119} pattern="[a-z0-9][a-z0-9\-]{1,118}" title="lowercase letters, digits and hyphens" placeholder="kubasu-motors" />
        </label>
        <label>
          Environment
          <select value={form.env} onChange={set("env")}>
            <option value="production">production</option>
            <option value="staging">staging</option>
          </select>
        </label>
        <label className="wide">
          Live URL (optional)
          <input value={form.url} onChange={set("url")} maxLength={500} placeholder="https://kubasu.example/" inputMode="url" />
        </label>
        <label className="wide">
          Pages allowed to report errors from a browser (optional, one per line)
          <textarea value={form.origins} onChange={set("origins")} rows={2} spellCheck={false} placeholder="https://kubasu.example" />
        </label>
      </div>
      <p className="hint">
        {form.env === "staging"
          ? "Staging cannot be checked from the VPS: it is watched by its stack reporter, so leave the URL empty unless the hub can reach it."
          : "Without a URL the site still reports crashes, but nothing checks that it is up."}
      </p>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <button className="btn pri" disabled={busy || !form.client.trim() || !form.name.trim()}>
        Create site
      </button>
    </form>
  );
}

const AUDIT_WORDS: Record<string, string> = {
  site_created: "Site created",
  url_changed: "Live URL changed",
  origins_changed: "Browser pages changed",
  browser_key_issued: "Browser key issued",
  browser_key_revoked: "Browser key revoked",
  secret_rotated: "Secret rotated",
  new_source: "Report from a new address",
  refused_request: "Request refused",
};

// Who changed what, and when. It lists the fact of a change, never a secret: the hub does not store one here.
function Trail({ siteId }: { siteId: number }) {
  const trail = usePoll<AuditEntry[]>(`/api/sites/${siteId}/audit`);
  return (
    <div className="sx-trail">
      {trail.error && <p className="form-error" role="alert">Could not load the history: {trail.error}</p>}
      {trail.data?.length === 0 && <p className="hint">Nothing recorded yet.</p>}
      <ul>
        {trail.data?.map((e) => (
          <li key={e.id}>
            <span className="k">{new Date(e.created_at).toLocaleString()}</span> <b>{AUDIT_WORDS[e.action] ?? e.action}</b>
            {e.detail && <span className="mono"> {e.detail}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function SiteRow({ s, label, h, now, onSaved, onSetup, onRotated }: { s: SiteListed; label?: string; h: SiteHealth | undefined; now: number; onSaved: () => void; onSetup: (s: SiteListed) => void; onRotated: (shown: Shown) => void }) {
  const [editing, setEditing] = useState(false);
  const [history, setHistory] = useState(false);
  const [url, setUrl] = useState(s.url ?? "");
  const [origins, setOrigins] = useState(s.allowed_origins.join("\n"));
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setNote(null);
    try {
      await fn();
    } catch (err) {
      setNote(why(err));
    } finally {
      setBusy(false);
    }
  };

  const save = () =>
    run(async () => {
      await send("PATCH", `/api/sites/${s.id}`, { url: url.trim() || null, allowed_origins: lines(origins) });
      setEditing(false);
      onSaved();
    });

  const rotate = () => {
    if (!confirm(`Rotate the secret for "${s.name}"? The old one stops working immediately, so the app must be given the new one.`)) return;
    return run(async () => {
      const r = await send<SiteCreated>("POST", `/api/sites/${s.id}/rotate-secret`);
      if (!r) throw new Error("the hub sent no answer");
      onRotated({ kind: "rotated", siteId: r.id, name: r.name, ingestKey: r.ingest_key, ingestSecret: r.ingest_secret, publicKey: r.public_key ?? null, startedAt: Date.now() });
    });
  };

  const test = () =>
    run(async () => {
      const r = await send<TestEventResult>("POST", `/api/sites/${s.id}/test-event`);
      setNote(r ? `Test event accepted (${r.severity}, ${r.state_change ?? "known issue"}). The hub side works.` : "Test event accepted.");
    });

  const check = !h
    ? "—"
    : !h.monitored
      ? "not monitored"
      : h.last_check == null
        ? "awaiting first check"
        : `${h.probe_stale ? "probe silent · " : ""}${ago(h.last_check, now)}`;

  return (
    <div className="sx-row">
      <div className="sx-row-main">
        {s.env === "staging" ? <FlaskConical size={16} className="sx-icon sx-icon-stage" aria-label="staging" /> : <Globe size={16} className="sx-icon" aria-label="production" />}
        <b title={s.name}>{label ?? s.name}</b>
      </div>
      <div className="sx-row-facts">
        <span>
          <span className="k">reported</span>{" "}
          {s.last_event ? ago(s.last_event, now) : <Pill tone="warn">never reported</Pill>}
        </span>
        <span>
          <span className="k">checked</span>{" "}
          <span className={h && (!h.monitored || h.probe_stale) && s.env === "production" ? "c-warn" : ""}>{check}</span>
        </span>
        <span>
          <span className="k">browser</span> {s.allowed_origins.length ? `${s.allowed_origins.length} page${s.allowed_origins.length === 1 ? "" : "s"}` : "off"}
        </span>
        {s.bad_signatures_24h > 0 && (
          <span title="Signed requests the hub refused in the last 24 hours. Either someone has this site's key but not its secret, or an app is still holding a rotated secret. See History.">
            <span className="k">refused</span> {s.bad_signatures_24h} in 24 h
          </span>
        )}
        {s.new_source_at && (
          <span title="A report arrived from an address this site had never used. A leaked secret looks like this, and so does moving the app to a new host. See History.">
            <span className="k">new address</span> {ago(s.new_source_at, now)}
          </span>
        )}
        {s.url && <span className="mono sx-url">{s.url}</span>}
      </div>
      <div className="sx-row-actions">
        <button className="btn" disabled={busy} onClick={() => onSetup(s)}>
          Setup
        </button>
        <button className="btn" disabled={busy} onClick={test}>
          Test
        </button>
        <button className="btn" disabled={busy} onClick={() => setEditing((v) => !v)} aria-expanded={editing}>
          Edit
        </button>
        <button className="btn" disabled={busy} onClick={rotate}>
          Rotate secret
        </button>
        <button className="btn" disabled={busy} onClick={() => setHistory((v) => !v)} aria-expanded={history}>
          History
        </button>
      </div>
      {history && <Trail siteId={s.id} />}
      {note && <p className="hint sx-note">{note}</p>}
      {editing && (
        <div className="sx-edit form">
          <label>
            Live URL (empty stops checking)
            <input value={url} onChange={(e) => setUrl(e.target.value)} maxLength={500} placeholder="https://..." />
          </label>
          <label>
            Pages allowed to report from a browser (one per line)
            <textarea value={origins} onChange={(e) => setOrigins(e.target.value)} rows={3} spellCheck={false} />
          </label>
          <p className="hint">Removing every page turns browser reporting off and revokes its public key. Adding one again makes a new key.</p>
          <div className="sx-actions">
            <button className="btn pri" disabled={busy} onClick={save}>
              Save
            </button>
            <button className="btn" disabled={busy} onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function Sites({ f }: { f: Feeds }) {
  // Polled here and faster than the shell's 30 seconds: this is the screen you watch while wiring an app up.
  const list = usePoll<SiteListed[]>("/api/sites", 5_000);
  const health = usePoll<SiteHealth[]>("/api/sites/health", 5_000);
  const [shown, setShown] = useState<Shown | null>(null);
  const [adding, setAdding] = useState<Prefill | null>(null);
  const { now, setIdleHold } = f;

  // The idle screen must not start over a half-filled form or a secret shown once: either would be lost.
  const holding = adding !== null || shown?.ingestSecret != null;
  useEffect(() => {
    setIdleHold(holding);
    return () => setIdleHold(false);
  }, [holding, setIdleHold]);

  useEffect(() => {
    if (!shown?.ingestSecret) return;
    const id = setTimeout(
      () => setShown((s) => (s ? { ...s, ingestSecret: null, warning: "The secret was cleared after 10 minutes on screen. Rotate it to get a new one." } : s)),
      SECRET_SHOWN_MS,
    );
    return () => clearTimeout(id);
  }, [shown?.ingestSecret]);

  const sites = list.data ?? [];
  const clients = [...new Set(sites.map((s) => s.client))].sort();
  const byName = new Map((health.data ?? []).map((h) => [h.name, h]));
  const current = shown ? sites.find((s) => s.id === shown.siteId) : undefined;

  const reload = () => Promise.all([list.reload(), health.reload()]);
  const open = (s: Shown) => {
    setShown(s);
    setAdding(null);
    reload();
  };

  return (
    <>
      <div className="page-head">
        <h1>Sites</h1>
        <span className="count">{sites.length} registered</span>
        {adding === null && !shown && (
          <button className="btn pri" onClick={() => setAdding({})}>
            Add a site
          </button>
        )}
      </div>

      {list.error && (
        <p className="form-error" role="alert">
          Can't reach the hub{list.data ? ", so this list may be out of date" : ""}.
        </p>
      )}

      {adding !== null && (
        <>
          <AddForm key={JSON.stringify(adding)} clients={clients} initial={adding} onCreated={open} />
          <button className="btn sx-cancel" onClick={() => setAdding(null)}>
            Cancel
          </button>
        </>
      )}

      {shown && (
        <>
          <KeysPanel shown={shown} onDone={() => setShown(null)} />
          {shown.kind !== "setup" && (
            <WaitingPanel
              shown={shown}
              site={current}
              health={current ? byName.get(current.name) : undefined}
              now={now}
              onTest={async () => {
                const r = await send<TestEventResult>("POST", `/api/sites/${shown.siteId}/test-event`);
                reload();
                return r;
              }}
            />
          )}
        </>
      )}

      <div className="sec">
        <h2>Registered sites</h2>
        <span className="count">client apps only</span>
      </div>
      {list.data === null ? (
        <Empty>{list.error ? "No list yet." : "Loading…"}</Empty>
      ) : sites.length === 0 ? (
        <Empty>No sites yet. Add the first one above.</Empty>
      ) : (
        <div className="sx-clients">
          {groupByClient(sites).map((c) => {
            const health = c.sites.map((x) => byName.get(x.name)).filter((x): x is SiteHealth => x !== undefined);
            const rowFor = (site: (typeof sites)[number], label: string) => (
              <SiteRow
                key={site.id}
                s={site}
                label={label}
                h={byName.get(site.name)}
                now={now}
                onSaved={reload}
                onSetup={(x) =>
                  open({ kind: "setup", siteId: x.id, name: x.name, ingestKey: x.ingest_key, ingestSecret: null, publicKey: x.public_key ?? null, startedAt: Date.now() })
                }
                onRotated={open}
              />
            );
            return (
              <section key={c.client} className="card sx-client" aria-label={c.client}>
                <header className="sx-client-head">
                  <span className="sx-client-icon">
                    <Building2 size={18} aria-hidden="true" />
                  </span>
                  <h3>{c.client}</h3>
                  <span className="muted">
                    {plural(c.sites.filter((x) => x.env !== "staging").length, "site", "sites")}
                    {c.sites.some((x) => x.env === "staging") && ` · ${c.sites.filter((x) => x.env === "staging").length} staging`}
                  </span>
                  {health.length > 0 && <Dot state={worstState(health)} />}
                </header>
                {c.groups.map((g) => (
                  <div key={g.key} className="sx-group">
                    <h4 className="sx-group-title">
                      <AppWindow size={15} aria-hidden="true" />
                      {g.key}
                    </h4>
                    {g.production && rowFor(g.production, "Production")}
                    {g.staging ? (
                      <div className="sx-staging">{rowFor(g.staging, "Staging")}</div>
                    ) : (
                      g.production && (
                        <div className="sx-staging sx-nostaging">
                          <FlaskConical size={16} className="sx-icon sx-icon-stage" aria-hidden="true" />
                          <span className="muted">No staging site</span>
                          <button className="btn" onClick={() => setAdding({ client: c.client, name: `${g.production!.name}-staging`, env: "staging" })}>
                            <Plus size={14} aria-hidden="true" /> Add staging
                          </button>
                        </div>
                      )
                    )}
                  </div>
                ))}
              </section>
            );
          })}
        </div>
      )}
    </>
  );
}
