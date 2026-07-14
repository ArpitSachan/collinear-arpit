# INC-4471 — Inventory Drift During Peak Load

## TL;DR

Two independent bugs, one in each direction of drift:

1. **Over-count** (reproduces at 1 worker): `Inventory.reserve()` never
   validated its `qty` argument. Malformed jobs with `qty < 0` in the
   queue log skipped the `available >= qty` guard and became
   *increments* (`store[sku] = available − (−n) = available + n`).
2. **Under-count** (only under concurrency): `reserve()` released the
   ledger lock during the simulated downstream call, in between the
   idempotency-cache check and the decrement. Two concurrent
   redeliveries of the same `job_id` could both miss the cache, both
   sleep, then both acquire the lock and decrement — the redelivered
   job got charged twice.

The "stale snapshot / manual stock correction" theory in the incident
report is a red herring: `data/initial_inventory.json` matches
`ops/system_of_record_snapshot.json` exactly for every SKU, so there
was no missed correction. The over-count is fully explained by the
negative-qty entries in the job log.

## Diagnosis

### Over-count (higher-than-expected)

`store/inventory.py` originally had:

```python
def reserve(self, job_id, sku, qty):
    ...
    with self._lock:
        available = self._store.get(sku, 0)
        approved = available >= qty
        if approved:
            self._store[sku] = available - qty
        ...
```

`data/jobs_seed.jsonl` contains six entries with `qty` values of
`-1`, `-2`, or `-3`. For any of them, `available >= qty` is trivially
true, and `available - qty` adds to stock. `README.md`'s request
contract says `qty` is a positive integer, so these were never
supposed to be accepted — but `reserve()` didn't check.

Independently-computed expected finals for the sample job log match
the "over" symptom exactly. Running the reproducer against the
original code at **1 worker** shows drift only on the SKUs that have
malformed entries in the log — BOLT (+4), DRUM (+7), CABLE (+2) —
and the delta on each equals the sum of the negative qtys for that
SKU. ANCHOR and EDGE, which have no malformed entries, match
expectation. This lines up with the incident report's observation
that the over-count reproduces at 1 worker on the same specific SKUs.

### Under-count (lower-than-expected)

The idempotency guard and the decrement were in **two different
critical sections**:

```python
with self._lock:                      # section A: idempotency check
    if job_id in self._processed_jobs:
        return self._processed_jobs[job_id]

time.sleep(DOWNSTREAM_CALL_SEC)       # <-- lock released here

with self._lock:                      # section B: decrement + record
    ...
    self._processed_jobs[job_id] = result
```

The gap between A and B is only closed by the fact that a job's own
future waits for A to finish before dispatching B. But under
at-least-once redelivery, two *different* threads run reserve() for
the same `job_id` concurrently, and the sequence

```
T1: A miss  → sleep
T2:            A miss (T1 hasn't reached B yet) → sleep
T1: B: decrement, record in _processed_jobs
T2: B: decrement AGAIN, record (overwrite) in _processed_jobs
```

double-charges the redelivered job. The idempotency cache only
suppresses a redelivery that arrives *after* the original has fully
finished — precisely what redeliveries during a scale-up spike
don't do.

## Why the existing tests missed it

- `test_redelivered_job_is_not_double_charged_when_sequential` retries
  the same `job_id` **on the same thread, back-to-back**. By the time
  the retry runs, the first call has already populated
  `_processed_jobs`, so the cache hit path is taken and no
  double-decrement is possible. The comment on that test even calls
  this out: *"Retries only ever happen back-to-back here, so this
  passes even though reserve() isn't actually safe under real
  concurrency."*
- `test_sequential_batch_via_run_jobs_single_worker` passes
  `num_workers=1`, so there is no interleaving to exercise the gap.
- None of the tests feed a malformed `qty`, so the negative-qty
  amplification bug was never exercised.

The bundled sample (`data/jobs_seed.jsonl`, 95 rows) is also too small
and too low-contention to reliably surface the concurrency bug at
default worker counts; you have to scale it up to reproduce the
under-count reliably.

## Fix

`store/inventory.py` — see the diff:

- **Input validation.** `reserve()` now rejects any `qty` that is not
  a positive `int` (excluding `bool`, since `bool` subclasses `int`
  in Python) by returning an unapproved `ReserveResult` without
  touching the ledger. This is a boundary check on the queue's
  payload and matches `README.md`'s stated contract.
- **Per-`job_id` leader election** for concurrent redeliveries. When a
  reserve() call misses the idempotency cache, it either (a) becomes
  the leader for that `job_id` (creating an `_InFlight` entry under
  `_lock`), or (b) waits on the leader's `Event`, then returns the
  leader's cached result. The leader performs the downstream call and
  the ledger update, records the result in `_processed_jobs`, clears
  its `_InFlight` slot, and signals the event. Followers never call
  the downstream and never touch the ledger.
- The ledger lock is still released around `time.sleep(DOWNSTREAM_CALL_SEC)`,
  so unrelated `job_id`s continue running the downstream call in
  parallel. The only calls that block each other are calls for the
  same `job_id` — which is exactly what idempotency requires and no
  more.
- `snapshot()` now returns a copy under the lock, and a new
  `reorder_snapshot()` is provided (per README's contract) subtracting
  the safety-stock floor. `Inventory.__init__` optionally accepts a
  `safety_stock` mapping; existing callers pass only `initial_qty`
  and are unaffected.

## Verification

All commands run from `/app`.

### Existing unit tests still pass

```
$ python -m pytest tests/ -q
.....                                                                    [100%]
5 passed in 0.61s
```

### Reproducer: `scripts/reproduce_drift.py`

The reproducer computes the independently-expected final inventory
(distinct-`job_id` sum, malformed rejected) and diffs it against the
service's actual final inventory. It replays the sample and can
`--scale` it up to incident-window concurrency.

**Before the fix**, 1 worker, sample as-is → over-count on the three
SKUs with malformed entries, matching the ops observation exactly:

```
SKU                  expected     actual      delta
SKU-ANCHOR-01               4          4         +0
SKU-BOLT-14                 5          9         +4  <-- OVER
SKU-CABLE-9                 6          8         +2  <-- OVER
SKU-DRUM-3                  6         13         +7  <-- OVER
SKU-EDGE-77                 6          6         +0
```

**Before the fix**, 96 workers, sample scaled x20 (initial stock x200
so the run isn't just insufficient-stock rejections) → both drifts:

```
SKU                  expected     actual      delta
SKU-ANCHOR-01            6560       6520        -40  <-- UNDER
SKU-BOLT-14              7120       7140        +20  <-- OVER
SKU-CABLE-9              8400       8380        -20  <-- UNDER
SKU-DRUM-3               8760       8860       +100  <-- OVER
SKU-EDGE-77              8940       8720       -220  <-- UNDER
```

**After the fix**, both configurations → drift = 0 on every SKU:

```
$ python scripts/reproduce_drift.py --workers 1 --scale 1
total |drift| = 0
$ python scripts/reproduce_drift.py --workers 96 --scale 20 --multiplier 200
total |drift| = 0
```

### Direct concurrency stress on a single `job_id`

200 threads simultaneously calling `reserve("j1", "WIDGET", 3)`:

```
all identical: True
snapshot: {'WIDGET': 997}
OK: single decrement across 200 concurrent redeliveries
```

Every caller gets the same `ReserveResult` (identity of the leader's
outcome) and the ledger is decremented exactly once.

### Throughput has not been serialized

`scripts/measure_throughput.py` replays the sample scaled to 1,900
distinct-`job_id` jobs (each with a 20ms simulated downstream call)
at 1/8/32/96 workers:

```
jobs=1900
  workers=  1  dt= 37.69s  speedup_vs_1w=  1.0x
  workers=  8  dt=  4.69s  speedup_vs_1w=  8.0x
  workers= 32  dt=  1.22s  speedup_vs_1w= 30.9x
  workers= 96  dt=  0.45s  speedup_vs_1w= 84.5x
```

Near-linear scaling up to 96 workers (the incident-window peak per
`ops/autoscaler_log.txt`). A fix that "solved" the concurrency bug by
holding the ledger lock across the downstream call would top out near
`total_jobs * 0.02s ≈ 38s` regardless of worker count; that's not
what we see. Contention is confined to same-`job_id` redeliveries,
which is inherent to per-job_id idempotency.

## Ruling out the "stale snapshot" theory

The incident report raises the possibility that a last-minute manual
stock correction was missed by newly-scaled workers. I compared:

- `data/initial_inventory.json` — the starting snapshot the service
  loaded for the incident window
- `ops/system_of_record_snapshot.json` — the warehouse system of
  record for the same window

They are identical, SKU-by-SKU. There is no missed correction to
explain the drift, and the two bugs identified above account for the
observed magnitudes exactly (over-count deltas equal the sum of
negative-qty entries per SKU; under-count only appears with
concurrent redeliveries).

## Files touched

- `store/inventory.py` — fix (validation + per-`job_id` leader
  election, plus `reorder_snapshot()`).
- `scripts/reproduce_drift.py` (new) — independent reproducer used
  above.
- `scripts/measure_throughput.py` (new) — throughput sanity check
  used above.

No changes to `tests/`, `data/`, `ops/`, `store/worker.py`, or
`store/queue_processor.py`.
