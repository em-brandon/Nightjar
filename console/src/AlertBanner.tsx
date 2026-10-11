import type { Alert } from "./api/types.ts";
import type { Feeds } from "./feeds.ts";
import { alertNotice } from "./alerts.ts";

// Shown on every tab, above the page, for as long as an alert has not reached the phone. It is not
// dismissible: the only way to clear it is for the alert to be delivered (or for someone to look).
export default function AlertBanner({ alerts, go }: { alerts: Alert[]; go: Feeds["go"] }) {
  const notice = alertNotice(alerts);
  if (!notice) return null;
  return (
    <div className={`banner banner-row ${notice.tone === "warn" ? "warn" : ""}`} role="alert">
      <span>
        <b>{notice.text}.</b> {notice.detail}
      </span>
      <button className="btn" onClick={() => go("alerts")}>
        Open alerts
      </button>
    </div>
  );
}
