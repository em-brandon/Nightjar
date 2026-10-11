# Escalating a P1 nobody acknowledged (planned)

> **Status: planned, not started.** Written down so it is not lost. It waits on a hub with a public address and a real
> P1 having been seen on a phone end to end (see `DEPLOY.md` section 7b).

## The gap

A P1 push repeats on the phone every 60 seconds for 30 minutes. If nobody acknowledges it, Pushover marks it expired, and
Nightjar then says so loudly: a red banner on every tab, the Alerts tab, the incident log and the wall display. But
**nothing escalates**. No second push, no second person, no second channel. The one person who can fix it was not woken,
and the system only tells them when they next look.

## The idea

A short ladder that starts when a P1 is sent and stops the moment it is dealt with. For example:

| After | If still unacknowledged | Channel |
|---|---|---|
| 0 min | | Pushover, priority 2, to you (built) |
| 10 min | | A second Pushover recipient or device |
| 20 min | | SMS or a voice call from a provider with Kenyan coverage, such as Africa's Talking |
| 30 min | | Email, as the record, not the alarm |

The figures are placeholders to be tuned like the sender's backoff, by breaking a staging route.

## Rules it must keep

1. **Escalation only adds.** It never replaces a channel and never suppresses the first push. If it fails, the
   first push has already gone (decision #8: nothing may suppress an alert).
2. **It stops when the incident is dealt with**, by the same mechanism that already stops a repeating push: the alerts of
   an incident are dismissed in the same transaction as the acknowledge, resolve, mute or snooze, and each later rung checks
   that before it fires. A rung that has already fired is cancelled where the channel allows it.
3. **P1 only to begin with.** A P2 that nobody answered is a morning problem.
4. **Quiet hours do not apply to a P1**, so they do not apply to its escalation.
5. **Bounded cost.** An SMS or a call costs money, so there is a cap per incident and per day, and a test mode that
   sends nothing. A flapping site must not run up a bill.
6. **Visible.** Every rung appears in the incident log and on the Alerts tab, including a rung that failed to send.

## Where it would live

- A rung is a row, like a notification, written when the P1's alert is sent and due at its offset. The sender loop
  already takes due rows with `FOR UPDATE SKIP LOCKED`, so rungs reuse that machinery and survive a restart.
- `hub/notify.py` stays the single place that knows a provider. It would gain a channel (`push`, `sms`, `email`) behind one
  `send`, so replacing a provider is still a change to one file.
- The policy (`hub/policy.py`) would decide the ladder, as a pure function with the same exhaustive tests.

## A separate, smaller idea: a fallback when Pushover itself is down

Different from escalation: if Pushover refuses or cannot be reached for a P1, a second provider should still try, rather
than waiting out the backoff. Today a failed alert is retried and shown as failed, but the phone is not reached by anything
else in the meantime.

## Open questions

1. **Who is the second recipient?** For a solo developer this is the real question: a second phone, a partner, a trusted
   colleague, or the client. Pushover can deliver to a group, or to a second user key.
2. **Which SMS or voice provider, and at what cost?** Coverage in Kenya, price per message, and whether a voice call is
   worth it at 3am.
3. **How long before each rung?** Ten minutes may be too long for a checkout that is down, or too short for someone in a
   shower.
4. **Should a P2 ever escalate?** Probably not, but a P2 spike on a client's shop during a sale might.
5. **Does an acknowledgement in the console stop the ladder?** Yes by rule 2, but acknowledging only means "I have seen
   it", so a rung that fires after an acknowledgement but before a resolve may still be wanted.
