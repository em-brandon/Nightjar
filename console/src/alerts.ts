import type { Alert } from "./api/types.ts";

// What the console says about alerts that did not do their job. An alert that could not be sent must never
// look like an alert that was not needed, and a P1 nobody acknowledged means the phone did not wake anyone, so
// both are said out loud on every tab until they are dealt with. Pure, so the wording is tested.

export type AlertNotice = { tone: "bad" | "warn"; text: string; detail: string };

const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

// The newest by when it was queued, which is the one most likely to be about right now.
const newest = (alerts: Alert[]): Alert => alerts.reduce((a, b) => (new Date(b.created_at) > new Date(a.created_at) ? b : a));

// A P1 whose receipt expired with nobody acknowledging it. Sent, so it is not a failure, but no human answered.
export const isUnacknowledged = (a: Alert) => a.status === "sent" && a.expired_at != null && a.acked_at == null;

// `alerts` is /api/notifications?status=attention: failed rows, rows that failed once and are being retried, and
// P1s that expired unacknowledged.
export function alertNotice(alerts: Alert[]): AlertNotice | null {
  const failed = alerts.filter((a) => a.status === "failed");
  const unacked = alerts.filter(isUnacknowledged);
  const retrying = alerts.filter((a) => a.status === "pending" || a.status === "sending");
  if (failed.length === 0 && unacked.length === 0 && retrying.length === 0) return null;

  if (failed.length === 0 && unacked.length === 0) {
    const latest = newest(retrying);
    return {
      tone: "warn",
      text: `${count(retrying.length, "alert is", "alerts are")} being retried after a failed send`,
      detail: `${latest.site}: ${latest.last_error ?? "no reason recorded"}`,
    };
  }

  const parts: string[] = [];
  if (failed.length > 0) parts.push(`${count(failed.length, "alert", "alerts")} could not be sent to your phone`);
  if (unacked.length > 0) parts.push(`${count(unacked.length, "alert was", "alerts were")} not acknowledged, so your phone may not have woken you`);
  if (retrying.length > 0) parts.push(`${retrying.length} more being retried`);

  const latest = newest(failed.length > 0 ? failed : unacked);
  const detail = failed.length > 0 ? `${latest.site}: ${latest.last_error ?? "no reason recorded"}` : `${latest.site}: ${latest.title}`;
  return { tone: "bad", text: parts.join(" · "), detail };
}

// ---------------------------------------------------------------- one alert, and the sender

export type AlertState = { label: string; tone: "ok" | "warn" | "bad" | "run" };

// What to call one alert in the list. "Sent" alone would hide the two cases that matter: a P1 still waiting
// for someone to acknowledge it, and one that expired with nobody having done so.
export function alertState(a: Alert): AlertState {
  if (a.status === "failed") return { label: "failed", tone: "bad" };
  if (a.status === "sent") {
    if (a.acked_at != null) return { label: "acknowledged", tone: "ok" };
    if (a.expired_at != null) return { label: "not acknowledged", tone: "bad" };
    if (a.priority === 2) return { label: "sent · awaiting acknowledgement", tone: "run" };
    return { label: "sent", tone: "ok" };
  }
  if (a.status === "sending") return { label: "sending", tone: "run" };
  return a.attempts > 0 ? { label: `retrying (attempt ${a.attempts})`, tone: "warn" } : { label: "queued", tone: "run" };
}

export type SenderHealth = { state: "ok" | "starting" | "stalled" | "off"; tone: "ok" | "warn" | "bad"; text: string };

type SenderFacts = { sender_enabled: boolean; sender_poll_s: number; sender_last_pass_at: string | null; sender_last_error: string | null };

const MIN = 60_000;
const age = (ms: number) => (ms < MIN ? `${Math.max(0, Math.round(ms / 1000))}s ago` : ms < 60 * MIN ? `${Math.round(ms / MIN)}m ago` : `${Math.round(ms / (60 * MIN))}h ago`);

// Whether the loop that delivers alerts is alive. A sender that has stopped looks exactly like a quiet night, so
// a pass that is overdue is said plainly. The same hub process serves this answer, so a hub that is down is the
// external dead-man's switch's job to notice, not this panel's.
export function senderHealth(s: SenderFacts, now: number): SenderHealth {
  if (!s.sender_enabled) return { state: "off", tone: "bad", text: "switched off (OUTBOX_ENABLED): alerts are queued but never sent" };
  if (s.sender_last_pass_at == null) return { state: "starting", tone: "warn", text: s.sender_last_error ? `no pass has completed yet (${s.sender_last_error})` : "no pass has completed yet" };
  const since = now - new Date(s.sender_last_pass_at).getTime();
  const overdue = since > Math.max(30_000, s.sender_poll_s * 6 * 1000);
  if (overdue) return { state: "stalled", tone: "bad", text: `stalled: last pass ${age(since)}` };
  if (s.sender_last_error) return { state: "ok", tone: "warn", text: `running, but the last attempt failed (${s.sender_last_error})` };
  return { state: "ok", tone: "ok", text: `running, last pass ${age(since)}` };
}
