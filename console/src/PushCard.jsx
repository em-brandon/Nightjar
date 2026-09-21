import { useRef } from "react";
import { CircleCheck, CircleX, FlaskConical } from "lucide-react";

// The staging push, as a card under the clock. It slides in when there is something to say and
// slides away when it has said it. The states, in the order a healthy push goes through them:
// loading (files going up, with a bar), done (delivered), complete (the stack came up healthy).
const SAY = {
  loading: "Uploading files",
  done: "Files uploaded",
  complete: "Live on staging",
  failed: "Push failed",
};
export default function PushCard({ push, sample = false, revealed = false }) {
  // Kept while the card slides away, so its words do not vanish before it has left.
  const last = useRef(null);
  if (push) last.current = push;
  const p = push ?? last.current;
  if (!p) return <aside className="push-card" aria-hidden="true" />;

  const indeterminate = p.state === "loading" && p.progress == null;
  return (
    <aside className={`push-card pc-${p.state} ${push ? "on" : ""}`} aria-hidden={!push} aria-live="polite">
      <div className="pc-head">
        <FlaskConical size={16} strokeWidth={2} aria-hidden="true" />
        Staging push
        {sample && <span className="demo-tag" title="Sample push, for judging the card">sample</span>}
        <span className="pc-pill">
          {p.state === "complete" && <CircleCheck size={13} strokeWidth={2.4} aria-hidden="true" />}
          {p.state === "failed" && <CircleX size={13} strokeWidth={2.4} aria-hidden="true" />}
          {p.state === "loading" ? "loading" : p.state}
        </span>
      </div>
      <div className="pc-row">
        <span
          className={`pc-bar ${indeterminate ? "pc-indeterminate" : ""}`}
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={p.progress ?? undefined}
        >
          <i style={indeterminate ? undefined : { width: `${p.state === "failed" ? 100 : p.progress ?? 0}%` }} />
        </span>
        {p.state === "loading" && p.progress != null && <span className="pc-pct">{p.progress}%</span>}
      </div>
      <div className="pc-text">
        <span className="pc-say">{SAY[p.state]}</span>
        <span className="pc-sub">
          {p.site}
          {/* commit refs are detail: blurred until the fingerprint reveal, like the deploys panels */}
          {p.ref && <span className={`veil ${revealed ? "" : "veiled"}`} aria-hidden={!revealed}> {p.ref}</span>}
        </span>
      </div>
    </aside>
  );
}
