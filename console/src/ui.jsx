import { useEffect, useState } from "react";
import { ago, fmtPct, plural } from "./hooks.js";

// ---------------------------------------------------------------- odometer

const DIGITS = "0123456789".split("");

function Digit({ d }) {
  // Mount showing 0 and roll to the real value, so the first paint animates
  // in like a real counter instead of just appearing.
  const [shown, setShown] = useState(0);
  useEffect(() => {
    const id = requestAnimationFrame(() => setShown(d));
    return () => cancelAnimationFrame(id);
  }, [d]);
  return (
    <span className="odo-d" aria-hidden="true">
      <span className="odo-strip" style={{ transform: `translateY(calc(${-shown} * var(--odo-h)))` }}>
        {DIGITS.map((n) => (
          <span key={n}>{n}</span>
        ))}
      </span>
    </span>
  );
}

// Rolling-digit counter. Anything that is not a digit ("," "." "%" "—") is
// drawn as-is. Keys count from the right so that 99 -> 100 keeps the existing
// digits rolling instead of remounting all of them.
export function Odometer({ value, className = "" }) {
  const text = String(value);
  return (
    <span className={`odo ${className}`} role="img" aria-label={text}>
      {[...text].map((ch, i) =>
        /\d/.test(ch) ? (
          <Digit key={text.length - i} d={Number(ch)} />
        ) : (
          <span key={text.length - i} className="odo-sep" aria-hidden="true">
            {ch}
          </span>
        ),
      )}
    </span>
  );
}

// ---------------------------------------------------------------- gauge

export function Gauge({ percent, state = "up", size = 92, children }) {
  const stroke = 7;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const filled = percent == null ? 0 : Math.max(0, Math.min(100, percent));
  return (
    <div className={`gauge ${state}`} style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle className="g-track" cx={size / 2} cy={size / 2} r={r} strokeWidth={stroke} />
        <circle
          className="g-fill"
          cx={size / 2}
          cy={size / 2}
          r={r}
          strokeWidth={stroke}
          strokeDasharray={c}
          strokeDashoffset={c * (1 - filled / 100)}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </svg>
      <div className="g-center">{children}</div>
    </div>
  );
}

// ---------------------------------------------------------------- sparkline

// `points` is numbers, with null marking a failed check. Failures are drawn as
// red ticks on the floor rather than silently bridged over.
export function Spark({ points, width = 132, height = 30, tone = "var(--green)" }) {
  const nums = points.filter((p) => p != null);
  if (nums.length < 2) {
    return <svg className="spark" width={width} height={height} aria-hidden="true">
      <line x1="0" x2={width} y1={height - 2} y2={height - 2} className="spark-idle" />
    </svg>;
  }
  const max = Math.max(...nums, 1);
  const step = width / (points.length - 1);
  const y = (v) => height - 4 - (v / max) * (height - 8);
  let d = "";
  let pen = false;
  const ticks = [];
  points.forEach((p, i) => {
    if (p == null) {
      pen = false;
      ticks.push(i * step);
      return;
    }
    d += `${pen ? "L" : "M"}${(i * step).toFixed(1)} ${y(p).toFixed(1)} `;
    pen = true;
  });
  return (
    <svg className="spark" width={width} height={height} aria-hidden="true">
      <path d={d} fill="none" stroke={tone} strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" />
      {ticks.map((x) => (
        <line key={x} x1={x} x2={x} y1={height - 8} y2={height - 1} className="spark-fail" />
      ))}
    </svg>
  );
}

// ---------------------------------------------------------------- ribbon

const RIBBON_LABEL = { ok: "no failures", deg: "some failed checks", out: "outage", gap: "no data" };

export function Ribbon({ days }) {
  return (
    <div>
      <div className="ribbon" role="img" aria-label="90 day check history">
        {days.map((d, i) => (
          <i key={i} className={d} title={`${days.length - 1 - i}d ago: ${RIBBON_LABEL[d]}`} />
        ))}
      </div>
      <div className="rscale">
        <span>90d</span>
        <span>today</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- small parts

export function Dot({ state }) {
  return <span className={`dot ${state}`} aria-hidden="true" />;
}

export function Sev({ level }) {
  return <span className={`sev ${level.toLowerCase()}`}>{level}</span>;
}

export function Pill({ tone, children }) {
  return <span className={`pill ${tone}`}>{children}</span>;
}

export function Empty({ children }) {
  return <p className="empty">{children}</p>;
}

// ---------------------------------------------------------------- site card

// One monitored site: uptime ring, live latency, latency trend, 90-day ribbon.
export function SiteCard({ site: s, now }) {
  const total = s.open_p1 + s.open_p2 + s.open_p3;
  const lat = s.latency_ms;
  // The ring and sparkline describe the probe alone. The dot and border carry
  // the overall state, which also counts open incidents: a site can be serving
  // 100% of checks and still be "down" because of an open P1, and a red ring
  // reading 100% would just be confusing.
  const probeTone = s.uptime_24h == null ? "up" : s.uptime_24h >= 99.5 ? "up" : s.uptime_24h >= 95 ? "warn" : "down";
  const sparkTone = lat == null ? "var(--red)" : "var(--green)";
  return (
    <div className={`card site ${s.state}`}>
      <div className="site-head">
        <Dot state={s.state} />
        <b>{s.name}</b>
        <span className="env">{s.env}</span>
      </div>

      {s.monitored ? (
        <>
          <div className="site-body">
            <Gauge percent={s.uptime_24h} state={probeTone}>
              <span className="g-num">
                <Odometer value={fmtPct(s.uptime_24h)} />
                <small>%</small>
              </span>
              <span className="g-cap">24h</span>
            </Gauge>
            <div className="site-metrics">
              <div className="metric">
                <span className="k">latency</span>
                <span className="v">
                  {lat == null ? <span className="bad">failed</span> : <Odometer value={lat} />}
                  {lat != null && <small>ms</small>}
                </span>
              </div>
              <div className="metric">
                <span className="k">avg 24h</span>
                <span className="v dim">
                  {s.latency_avg_ms == null ? "—" : <Odometer value={s.latency_avg_ms} />}
                  {s.latency_avg_ms != null && <small>ms</small>}
                </span>
              </div>
              <Spark points={s.latency_series} tone={sparkTone} />
            </div>
          </div>
          <Ribbon days={s.ribbon} />
        </>
      ) : (
        <p className="unmonitored">
          No health check. Give this site a <code>url</code> and it appears here with uptime
          and latency.
        </p>
      )}

      <div className="site-foot">
        {s.monitored && (
          <span className={s.probe_stale ? "c-warn" : ""}>
            {s.probe_stale ? "probe silent · last check " : "checked "}
            {ago(s.last_check, now)}
          </span>
        )}
        <span>
          {total === 0 ? (
            "no open issues"
          ) : (
            <>
              {plural(total, "open issue", "open issues")}
              {s.open_p1 > 0 && <span className="c-p1"> · {s.open_p1} P1</span>}
              {s.open_p2 > 0 && <span className="c-p2"> · {s.open_p2} P2</span>}
            </>
          )}
        </span>
      </div>
    </div>
  );
}
