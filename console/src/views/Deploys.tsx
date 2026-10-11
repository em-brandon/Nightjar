import type { Deploy } from "../api/types.ts";
import type { Feeds } from "../feeds.ts";
import { ago } from "../hooks.ts";
import { Pill } from "../ui.tsx";

const TONE: Record<Deploy["status"], string> = { success: "ok", failed: "bad", running: "run" };

export function DeployList({ deploys, now }: { deploys: Deploy[]; now: number }) {
  return (
    <div className="deploys">
      {deploys.map((d) => (
        <div className="dep" key={d.id}>
          <span className="dep-site">{d.site}</span>
          <span className="dep-ref mono">{d.ref || "—"}</span>
          <Pill tone={TONE[d.status]}>{d.status}</Pill>
          <span className="dep-age">{ago(d.created_at, now)}</span>
        </div>
      ))}
    </div>
  );
}

// Shown while nothing has reported. The steps are the real interface, so the
// empty state doubles as the documentation for hooking CI up.
export function DeployHowTo({ env }: { env: string }) {
  return (
    <div className="card howto">
      <h2>Nothing reported yet</h2>
      <p>
        Add one step to the end of the pipeline for the {env} site. It authenticates with that
        site's ingest key, the same one crash reports use.
      </p>
      <pre>{`curl -s -X POST "$NIGHTJAR_URL/ingest/$NIGHTJAR_KEY/deploy" \\
  -H "Content-Type: application/json" \\
  -d '{"ref": "'"$GITHUB_REF_NAME@$GITHUB_SHA"'", "status": "success"}'`}</pre>
      <p className="hint">
        Send <code>running</code> when a deploy starts, then <code>success</code> or{" "}
        <code>failed</code> when it ends. For a staging deploy you can add <code>"progress": 0-100</code> to
        the running reports and the wall shows how far the upload has got. If signatures are on, sign the body like a crash report.
      </p>
    </div>
  );
}

export default function Deploys({ f }: { f: Feeds }) {
  const { deploys, now } = f;
  const prod = deploys.filter((d) => d.env === "production");
  const failed = prod.filter((d) => d.status === "failed").length;

  return (
    <>
      <div className="page-head">
        <h1>Production deploys</h1>
        <span className="count">{prod.length} recorded</span>
        {failed > 0 && <Pill tone="bad">{failed} failed</Pill>}
      </div>
      {prod.length === 0 ? <DeployHowTo env="production" /> : <DeployList deploys={prod} now={now} />}
    </>
  );
}
