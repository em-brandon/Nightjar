import { ArrowDownUp, Banknote, CloudDownload, CloudUpload, Cpu, DatabaseBackup, House, Plane, ThermometerSnowflake, ThermometerSun, Wifi, WifiOff } from "lucide-react";
import { activityView, backupView, hostView, netView, pairAt, presenceView, rateView, tempsView } from "./home.ts";
import { Odometer } from "./ui.jsx";

// A few small status icons, like a phone's notification area: an icon with a live number beside it.
// Dim while all is well, amber for "look soon", red for "look now". A silent reporter is an amber
// icon with a dash, never a healthy number. Hover (or focus) an icon for what it means.
function Item({ icon: Icon, tone = "", motion = "", tip, children }) {
  return (
    <span className={`tip bar-item ${tone ? `bar-${tone}` : ""}`} data-tip={tip} tabIndex={0}>
      <Icon size={19} strokeWidth={1.9} className={motion ? `bar-motion bar-${motion}` : undefined} aria-hidden="true" />
      {children}
    </span>
  );
}

const Num = ({ value, unit }) => (
  <span className="bar-num">
    {value == null ? "—" : <Odometer value={String(value)} />}
    {unit && <small>{unit}</small>}
  </span>
);

const TONE = { silent: "warn", old: "warn", none: "", hot: "bad", low: "bad", warm: "warn", stale: "warn", missing: "warn", offline: "bad" };
// Temperatures colour by their own range: cool, operating, high.
const TEMP_TONE = { cool: "cool", op: "op", high: "high" };

// The internet icon changes with what the line is doing.
const NET_ICON = { offline: WifiOff, silent: WifiOff, idle: Wifi, down: CloudDownload, up: CloudUpload, both: ArrowDownUp };
const NET_WORDS = { offline: "Offline", silent: "Reporter silent", idle: "Connected, idle", down: "Downloading", up: "Uploading", both: "Downloading and uploading" };

export default function StatusTray({ home, homeDemo, presence, rates, offline = false, now }) {
  const isDemo = home.length === 0 && !!homeDemo;
  const h = home[0] ?? homeDemo?.[0] ?? null;

  const net = netView(h, now);
  const host = hostView(h);
  const temps = tempsView(h);
  const backup = backupView(h);
  const who = presenceView(presence);
  const rate = rateView(rates);
  const netLive = net && (net.state === "ok" || net.state === "old") && !offline;
  const act = activityView(h, offline);
  const rateNow = pairAt(rate, now);
  const sample = isDemo ? "\n(sample data until the home reporter is connected)" : "";

  return (
    <div className="idle-tray" role="group" aria-label="Status">
      {act && (
        <Item
          icon={NET_ICON[act.state]}
          tone={act.state === "offline" ? "bad" : act.state === "silent" || net?.state === "old" ? "warn" : ""}
          motion={["down", "up", "both"].includes(act.state) ? act.state : ""}
          tip={
            `Internet: ${NET_WORDS[act.state]}\n` +
            (act.state === "offline"
              ? "The wall cannot reach the hub, so the home connection may be down"
              : act.state === "silent"
                ? "The home reporter has stopped, so nothing is known about the line"
                : `Traffic in the last minute: ${act.text}`) +
            (netLive ? `\nLast speed test: ${net.down} Mbps down${net.up ? `, ${net.up} up` : ""}${net.latency ? `, ${net.latency}` : ""}, ${net.age} ago${net.state === "old" ? " (the test has stopped)" : ""}` : "") +
            sample
          }
        >
          <Num value={netLive ? net.down : null} unit={netLive ? "Mbps" : null} />
        </Item>
      )}

      {backup && (
        <Item icon={DatabaseBackup} tone={TONE[backup.state]} tip={`Last good backup\n${backup.text}${sample}`}>
          <span className="bar-num">{backup.state === "ok" || backup.state === "stale" ? backup.age.replace(" ", "") : "—"}</span>
        </Item>
      )}

      {who && (
        <Item icon={who.state === "home" ? House : Plane} tone={who.state === "home" ? "home" : ""} tip={`Presence\n${who.text}${who.sub ? `, ${who.sub}` : ""}`}>
          <span className="bar-num bar-word">{who.text}</span>
        </Item>
      )}

      {temps && !temps.silent && temps.intake && (
        <Item icon={ThermometerSnowflake} tone={TEMP_TONE[temps.intake.state]} tip={`Cabinet intake: ${temps.intake.value}°C\nAir going into the cabinet\n${temps.intake.range}${sample}`}>
          <Num value={temps.intake.value} unit="°" />
        </Item>
      )}
      {temps && !temps.silent && temps.exhaust && (
        <Item icon={ThermometerSun} tone={TEMP_TONE[temps.exhaust.state]} tip={`Cabinet exhaust: ${temps.exhaust.value}°C\nAir leaving the cabinet\n${temps.exhaust.range}${sample}`}>
          <Num value={temps.exhaust.value} unit="°" />
        </Item>
      )}
      {host && (
        <Item
          icon={Cpu}
          tone={host.state === "silent" ? "warn" : host.state === "low" ? "bad" : TEMP_TONE[temps?.cpu?.state] ?? ""}
          tip={
            host.state === "silent"
              ? "ThinkCentre\nreporter silent"
              : `ThinkCentre CPU: ${temps?.cpu ? `${temps.cpu.value}°C` : "no reading"}\n${temps?.cpu?.range ?? ""}${host.diskFree != null ? `\nDisk ${host.diskFree}% free${host.state === "low" ? " (low)" : ""}` : ""}${host.memUsed != null ? `, memory ${host.memUsed}% used` : ""}${sample}`
          }
        >
          <Num value={host.state === "silent" ? null : temps?.cpu?.value ?? null} unit={host.state === "silent" ? null : "°"} />
        </Item>
      )}

      {rate && (
        <Item
          icon={Banknote}
          tone={rate.state === "off" || rate.state === "old" ? "warn" : rateNow ? `cur-${rateNow.code.toLowerCase()}` : ""}
          tip={
            rate.value == null
              ? "Exchange rate\nunavailable"
              : `Kenyan shillings you get for one unit${rate.state === "old" ? " (old)" : ""}\n${rate.pairs.map((p) => `${p.symbol} 1 ${p.name} = ${p.value} KES`).join("\n")}\nCycles every few seconds`
          }
        >
          {rateNow ? (
            <span className={`bar-num bar-rate rate-${rateNow.code.toLowerCase()}`} key={rateNow.code}>
              <span className="rate-sym">{rateNow.symbol}1</span>
              <span className="rate-eq">=</span>
              <span className="rate-num">{rateNow.value}</span>
              <small>KES</small>
            </span>
          ) : (
            <Num value={null} />
          )}
        </Item>
      )}
    </div>
  );
}
