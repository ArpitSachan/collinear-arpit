# Incident Report: INC-4471 — Inventory Drift During Peak Load

| Field           | Value                 |
| --------------- | --------------------- |
| **Reported by** | Fulfillment Ops       |
| **Severity**    | SEV-2                 |
| **Status**      | Open, assigned to you |

---

## Summary

During Friday's peak traffic window, the stock reservation service's worker
pool autoscaled up to handle a surge in queue depth. Ops noticed the
numbers didn't add up afterwards, and the pattern doesn't look like a
single clean issue:

- For **several SKUs**, the final `available_qty` came in **lower** than
  `starting_qty - sum(qty of every distinct order that was actually
  fulfilled)`, computed independently from the order log — as if some
  decrements happened more than once.
- For **a few other SKUs**, `available_qty` came in **higher** than that
  same independently-computed figure — as if stock appeared from
  nowhere.
- The queue's delivery metrics confirm a normal rate of redeliveries
  during the window (worker acks timing out under load and the job
  being resent) — this is expected/handled behavior per the queue's
  at-least-once contract.

## What Ops Already Tried

Ops replayed the same job log through the service with a **single
worker thread**, after the fact:

- The **higher-than-expected** anomaly reproduced identically at 1
  worker — same SKUs, same magnitude.
- The **lower-than-expected** anomaly did **not** reproduce at 1 worker
  — those SKUs matched the independently-computed figure exactly.

Ops isn't sure whether that means these are two unrelated issues, or one
issue with two different symptoms depending on timing. Worth keeping an
open mind rather than assuming a single fix explains both.

## Live Theories (Unconfirmed)

- **Stale snapshot theory:** one engineer suspects the autoscaler's rapid
  scale-up may have caused newly-started workers to boot from an
  inventory snapshot taken *before* a last-minute manual stock
  correction landed, which would explain the over-count. This hasn't
  been confirmed. `/app/ops/` has what the on-call team pulled together
  during the incident, including the warehouse system-of-record's own
  snapshot from that window — worth checking directly rather than taking
  the theory on faith.
- Ops has **not** done a full audit of the queue's job records for the
  incident window — a couple dozen entries were spot-checked and nothing
  obviously wrong stood out, but that was not exhaustive.

## What We Need

1. Determine why the service's final inventory numbers diverge from the
   independently computed expected numbers — for both directions of
   drift — and produce a fix.
2. The fix must not regress the existing test suite.
3. The service's whole reason for using a worker pool instead of one
   thread is to keep up with queue throughput under load. Ops has an SLA
   that reservation processing must not fall meaningfully behind queue
   arrival rate — a fix that "solves" this by serializing everything
   trades one incident for another (queue backlog / SLA breach), so it
   isn't acceptable either.
4. Show your work: back your diagnosis and your fix with reproducible
   evidence, not just a plausible-sounding explanation. Ops needs
   confidence this won't recur at the scale the service actually runs
   under — see `/app/ops/` for what's known about the incident window.

## What Ops Has Already Ruled Out

| Ruled out  | Evidence                                                                |
| ---------- | ------------------------------------------------------------------------ |
| Queue bug  | Redelivery behavior matches the queue's documented at-least-once contract |

Everything else — including both live theories above — is fair game to
investigate and confirm or rule out yourself.
