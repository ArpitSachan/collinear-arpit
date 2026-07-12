# INC-4471 Root Cause & Fix

## Root cause

Two independent bugs explain the two directions of drift ops reported.
Neither is the "obvious" fresh-`Lock()`-per-call antipattern - the
instance-level lock is correctly created once in `__init__` and every
`with self._lock:` block genuinely excludes concurrent callers.

### 1. Lower-than-expected stock: missing re-check after reacquiring the lock

`reserve()` deliberately splits its critical section into two
`with self._lock:` blocks around the simulated downstream call, so the
lock isn't held during I/O - a legitimate throughput optimization.
But the second block never re-checks `_processed_jobs` before decrementing
stock. Two concurrent deliveries of the *same* `job_id` (a redelivery
racing its own earlier attempt, which is normal under the queue's
at-least-once contract) can both pass the first (pre-sleep) idempotency
check before either has written a result, both sleep concurrently, then
both proceed through the second block and both decrement the ledger -
double-charging that job.

This only shows up when two deliveries of the same `job_id` are truly
concurrent, which needs enough worker threads and enough redeliveries in
flight at once. At 1 worker (ops' replay) or in a small low-concurrency
sample, deliveries of the same `job_id` essentially never overlap, so it
never reproduced there - consistent with what ops observed.

### 2. Higher-than-expected stock: no validation on `qty`

`reserve()` never checked that `qty` is positive. `README.md`'s request
contract says malformed deliveries (`qty <= 0`) can appear on the queue
(e.g. a mis-routed cancellation event) and must be rejected. Without
that check, `available >= qty` is trivially true for `qty <= 0`, and for
negative `qty` specifically, `available - qty` *increases* stock. This
needs no concurrency at all - it reproduces identically on a single
worker, which is exactly why ops' single-worker replay showed this
anomaly but not the other one. It is a separate bug from #1, not a
second symptom of it; the "stale snapshot" theory ops floated was
checked against `ops/system_of_record_snapshot.json` (matches
`data/initial_inventory.json` exactly) and ruled out.

## Fix

- Added a second idempotency check inside the post-sleep lock block, so
  a same-`job_id` race is caught before the ledger is touched a second
  time. The critical section stays scoped to in-memory state only - the
  downstream call remains outside the lock, so independent jobs still
  overlap in flight.
- Added an up-front `qty <= 0` rejection, inside the same lock scope, so
  malformed deliveries are recorded as rejected (idempotently) rather
  than silently mutating the ledger.

## Verification

- `python -m pytest tests/ -q` still passes (no regression) - the
  existing suite only exercises sequential/single-threaded call
  sequences and has no `qty<=0` coverage, which is exactly why neither
  bug was caught by it originally.
- A barrier-synchronized duplicate-delivery probe (forcing two same-
  `job_id` deliveries to start at the same instant, many independent
  trials) now shows 0 double-charges, vs. reliably failing on the
  unfixed code.
- Replaying the production-scale job log (96-worker pool, matching the
  incident's peak autoscaler size, ~10% redelivered duplicates, ~5%
  malformed `qty<=0` deliveries mixed in) across many independent runs
  now yields a final inventory that exactly matches the independently-
  computed expected totals on every run, not just one - a single run
  isn't sufficient evidence here since the redelivery race is
  probabilistic depending on thread scheduling.
- The same replay completes in well under a second per run, in the same
  range as the (incorrect) pre-fix behavior, confirming the fix didn't
  trade correctness for a serialized-everything throughput regression.
