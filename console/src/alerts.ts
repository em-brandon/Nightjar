import type { Alert } from "./api/types.ts";

// What the console says about alerts that did not reach the phone. An alert that could not be sent must
// never look like an alert that was not needed, so anything failed, or failed once and still being
// retried, is said out loud on every tab until it is delivered. Pure, so the wording is tested.

export type AlertNotice = { tone: "bad" | "warn"; text: string; detail: string };

const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

// The newest by when it was queued, which is the one most likely to be about right now.
const newest = (alerts: Alert[]): Alert => alerts.reduce((a, b) => (new Date(b.created_at) > new Date(a.created_at) ? b : a));

// `alerts` is /api/notifications?status=undelivered: failed rows, and rows that have failed once and are being retried.
export function alertNotice(alerts: Alert[]): AlertNotice | null {
  const failed = alerts.filter((a) => a.status === "failed");
  const retrying = alerts.filter((a) => a.status !== "failed");
  if (failed.length === 0 && retrying.length === 0) return null;

  const latest = newest(failed.length > 0 ? failed : retrying);
  const detail = `${latest.site}: ${latest.last_error ?? "no reason recorded"}`;

  if (failed.length > 0) {
    const more = retrying.length > 0 ? ` · ${retrying.length} more being retried` : "";
    return { tone: "bad", text: `${count(failed.length, "alert", "alerts")} could not be sent to your phone${more}`, detail };
  }
  return { tone: "warn", text: `${count(retrying.length, "alert is", "alerts are")} being retried after a failed send`, detail };
}
