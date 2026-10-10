import { useState } from "react";
import { ago, plural } from "../hooks.js";
import { Dot, Empty, Pill } from "../ui.jsx";
import { byUser, diskFreePct, fmtDuration, label, mediaState, progress, topTitles, within } from "../media.ts";

const STATE = {
  up: { dot: "up", tone: "ok", text: "up" },
  down: { dot: "down", tone: "bad", text: "not responding" },
  low: { dot: "warn", tone: "warn", text: "disk nearly full" },
  silent: { dot: "warn", tone: "warn", text: "reporter silent" },
};
const METHOD = { direct_play: "direct play", direct_stream: "direct stream", transcode: "transcoding" };
const WINDOWS = [7, 30];

const gb = (mb) => `${(mb / 1024).toFixed(mb >= 10240 ? 0 : 1)} GiB`;

// Marks sample data. It says so in words as well as colour.
export function DemoTag() {
  return (
    <span className="demo-tag" title="Sample data. Connect the Jellyfin reporter to see your own.">
      demo
    </span>
  );
}

export function DiskBar({ m }) {
  const free = diskFreePct(m);
  if (free == null) return null;
  const used = 100 - free;
  const tone = free < 10 ? "bad" : free < 20 ? "warn" : "";
  return (
    <div className={`membar ${tone}`} role="img" aria-label={`${Math.round(free)}% of the media disk is free`}>
      <i style={{ width: `${used}%` }} />
    </div>
  );
}

function ServerCard({ m, now }) {
  const state = mediaState(m);
  const s = STATE[state];
  return (
    <div className={`card md-server ${state}`}>
      <div className="site-head">
        <Dot state={s.dot} />
        <b>{m.site}</b>
        {m.version && <span className="env">Jellyfin {m.version}</span>}
      </div>
      <div className="stack-state">
        <Pill tone={s.tone}>{s.text}</Pill>
        {state === "up" && <Pill tone="run">{plural(m.sessions.length, "stream", "streams")}</Pill>}
      </div>
      {m.host_disk_total_mb != null && (
        <>
          <div className="metric">
            <span className="k">library disk free</span>
            <span className="v">
              {gb(m.host_disk_free_mb)}
              <small> / {gb(m.host_disk_total_mb)}</small>
            </span>
          </div>
          <DiskBar m={m} />
        </>
      )}
      <div className="site-foot">
        <span className={state === "silent" ? "c-warn" : ""}>
          {state === "silent" ? "last report " : "reported "}
          {ago(m.reported_at, now)}
        </span>
      </div>
    </div>
  );
}

// Who is watching right now. A silent reporter shows nothing here, on purpose:
// an old snapshot must not claim someone is still watching.
function NowStreaming({ servers }) {
  const streams = servers.flatMap((m) => m.sessions.map((s) => ({ ...s, server: m.site })));
  if (streams.length === 0) return <Empty>Nobody is watching right now.</Empty>;
  return (
    <ul className="md-now">
      {streams.map((s) => {
        const pct = progress(s);
        return (
          <li key={`${s.server}-${s.session_id}`}>
            <span className="md-who">{s.user}</span>
            <span className="md-what">
              {label(s)}
              <small>{[s.device, s.client].filter(Boolean).join(" · ")}</small>
              {pct != null && (
                <span className="md-progress" role="img" aria-label={`${Math.round(pct)}% watched`}>
                  <i style={{ width: `${pct}%` }} />
                </span>
              )}
            </span>
            {s.paused && <Pill tone="run">paused</Pill>}
            {s.play_method && <Pill tone={s.play_method === "transcode" ? "warn" : "ok"}>{METHOD[s.play_method]}</Pill>}
          </li>
        );
      })}
    </ul>
  );
}

function History({ plays, now }) {
  const [days, setDays] = useState(7);
  const shown = within(plays, days, now);
  const users = byUser(shown);
  const titles = topTitles(shown);
  return (
    <>
      <div className="sec">
        <h2>History</h2>
        <span className="count">{plural(shown.length, "viewing", "viewings")}</span>
        <span className="md-window" role="group" aria-label="History window">
          {WINDOWS.map((d) => (
            <button key={d} className={`btn ${days === d ? "pri" : ""}`} onClick={() => setDays(d)}>
              {d} days
            </button>
          ))}
        </span>
      </div>
      {shown.length === 0 ? (
        <Empty>No viewings in the last {days} days.</Empty>
      ) : (
        <>
          <div className="md-cols">
            <div className="card md-panel">
              <h3>Watched by</h3>
              <ul className="md-rank">
                {users.map((u) => (
                  <li key={u.user}>
                    <b>{u.user}</b>
                    <span className="muted">{plural(u.plays, "viewing", "viewings")}</span>
                    <span className="mono">{fmtDuration(u.watched_s)}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div className="card md-panel">
              <h3>Most watched</h3>
              <ul className="md-rank">
                {titles.map((t) => (
                  <li key={t.name}>
                    <b>{t.name}</b>
                    <span className="muted">{plural(t.plays, "viewing", "viewings")}</span>
                    <span className="mono">{fmtDuration(t.watched_s)}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
          <div className="card md-panel">
            <h3>Recent</h3>
            <ul className="md-log">
              {shown.slice(0, 40).map((p) => (
                <li key={p.id}>
                  <span className="md-who">{p.user}</span>
                  <span className="md-what">
                    {label(p)}
                    <small>{p.device || p.client || ""}</small>
                  </span>
                  <span className="mono">{fmtDuration(p.watched_s)}</span>
                  <span className="dep-age">{ago(p.started_at, now)}</span>
                </li>
              ))}
            </ul>
          </div>
        </>
      )}
    </>
  );
}

function MediaHowTo() {
  return (
    <div className="card howto">
      <h2>No media reports yet</h2>
      <p>
        The hub is on a VPS and cannot reach your home network, so a small reporter next to Jellyfin
        pushes its state in. It only watches: it never plays, stores or controls anything.
      </p>
      <pre>{`# 1. In the hub: add a site with env "media" and copy its ingest key and secret.
# 2. In Jellyfin: Dashboard > API Keys > add one. It never leaves the media machine.
# 3. On the media machine, install deploy/media/jellyfin-reporter.* (systemd timer)
#    and set NIGHTJAR_URL, NIGHTJAR_MEDIA_KEY, NIGHTJAR_MEDIA_SECRET, JELLYFIN_API_KEY.`}</pre>
      <p className="hint">
        If the reporter stops, the server is shown as <code>reporter silent</code>, never as idle.
      </p>
    </div>
  );
}

export default function Media({ f }) {
  const { media, mediaHistory, now } = f;
  const live = media.reduce((n, m) => n + (m.stale ? 0 : m.sessions.length), 0);
  return (
    <>
      <div className="page-head">
        <h1>Home media</h1>
        {media.length > 0 && <span className="count">{plural(live, "stream", "streams")} now</span>}
      </div>
      {media.length === 0 ? (
        <MediaHowTo />
      ) : (
        <>
          <div className="site-grid">
            {media.map((m) => (
              <ServerCard key={m.site} m={m} now={now} />
            ))}
          </div>
          <div className="sec">
            <h2>Now streaming</h2>
          </div>
          <div className="card md-panel">
            <NowStreaming servers={media} />
          </div>
          <History plays={mediaHistory} now={now} />
        </>
      )}
    </>
  );
}
