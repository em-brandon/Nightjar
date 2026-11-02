import { useState } from "react";
import type { Heartbeat } from "../api/types.ts";
import type { Feeds } from "../feeds.ts";
import { every, isProblem, ordered } from "../heartbeats.ts";
import { ago, plural, send } from "../hooks.ts";
import { Dot, Empty, Pill } from "../ui.tsx";

const TONE: Record<Heartbeat["state"], string> = { ok: "ok", late: "warn", recovering: "warn", missed: "bad", failed: "bad" };
const DOT: Record<Heartbeat["state"], string> = { ok: "up", late: "warn", recovering: "warn", missed: "down", failed: "down" };

// The empty state is the documentation: a heartbeat registers itself with its first ping.
function HowTo() {
  return (
    <div className="card howto">
      <h2>No heartbeats yet</h2>
      <p>
        A heartbeat is a job, worker or private app that says it is alive. When it stops saying so, Nightjar opens an incident,
        which catches a dead host or a stuck cron job that no crash report ever could. It registers itself with its first ping,
        using the site's ingest key.
      </p>
      <pre>{`# at the end of a successful run
nightjar.sh heartbeat nightly-backup --every 1d

# or without the script
curl -fsS -X POST "$NIGHTJAR_URL/ingest/$NIGHTJAR_KEY/heartbeat/nightly-backup" \\
  -H "Content-Type: application/json" \\
  -d '{"interval_s": 86400, "kind": "job"}'`}</pre>
      <p className="hint">
        <code>kind</code> is <code>job</code> (a missed one is P2) or <code>app</code> (stands in for uptime: P1 in production).
        Add <code>"status": "fail"</code> to report a run that failed. Run it once by hand after wiring it: a heartbeat that has
        never pinged does not exist yet, so it cannot be missed.
      </p>
    </div>
  );
}

function Row({ h, now, onGone }: { h: Heartbeat; now: number; onGone: () => Promise<void> }) {
  const [sure, setSure] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const remove = async () => {
    setError(null);
    try {
      await send("DELETE", `/api/heartbeats/${h.id}`);
      await onGone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  return (
    <div className="hb">
      <span className="hb-name">
        <Dot state={DOT[h.state]} />
        <b>{h.name}</b>
        <small className="muted">
          {h.site}
          {h.kind === "app" ? " · app" : ""}
        </small>
      </span>
      <span className="hb-every mono">{every(h.interval_s)}</span>
      <span className="hb-last mono">{ago(h.last_ping_at, now)}</span>
      <Pill tone={TONE[h.state]}>{h.state}</Pill>
      <span className="hb-act">
        {sure ? (
          <>
            <button className="btn" onClick={remove}>Retire</button>
            <button className="btn" onClick={() => setSure(false)}>Keep</button>
          </>
        ) : (
          <button className="btn" onClick={() => setSure(true)} aria-label={`Retire ${h.name}`}>Retire…</button>
        )}
      </span>
      {(h.last_message || error) && (
        <small className={`hb-note ${error ? "c-p1" : "muted"}`}>{error ?? `last said: ${h.last_message}`}</small>
      )}
    </div>
  );
}

export default function Heartbeats({ f }: { f: Feeds }) {
  const list = ordered(f.heartbeats);
  const bad = list.filter(isProblem).length;
  return (
    <>
      <div className="page-head">
        <h1>Heartbeats</h1>
        <span className="count">{plural(list.length, "heartbeat", "heartbeats")}</span>
        {bad > 0 && <Pill tone="bad">{bad} in trouble</Pill>}
      </div>
      {list.length === 0 ? (
        <HowTo />
      ) : (
        <>
          <div className="hbs">
            {list.map((h) => (
              <Row key={h.id} h={h} now={f.now} onGone={f.reloadHeartbeats} />
            ))}
          </div>
          {bad === 0 && <Empty>Every heartbeat is on time.</Empty>}
          <p className="hint">
            <b>late</b> is past its interval but inside its grace: nothing has alerted yet. <b>recovering</b> is pinging again; its incident resolves itself after a few good pings. <b>Retire</b> is for a job that was
            stopped on purpose; an incident it already opened stays until you resolve it.
          </p>
        </>
      )}
    </>
  );
}
