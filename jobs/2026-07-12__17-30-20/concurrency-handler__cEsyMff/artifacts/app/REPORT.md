# INC-4471 — Postmortem & Fix

## TL;DR

Two independent bugs, one per drift direction:

1. **Phantom stock (over-count)** — `reserve()` accepted malformed
   `qty <= 0` jobs. `available >= qty` is true for any negative `qty`,
   and `available - qty` with `qty < 0` *adds* stock. Deterministic;
   reproduces at 1 worker.
2. **Double-decrement (under-count)** — the idempotency check and the
   ledger write in `reserve()` were separated by an ~20 ms simulated
   downstream call held outside the lock. Two concurrent redeliveries
   of the same `job_id` could both find `cached is None`, both sleep,
   and both apply the decrement. Only appears under concurrency.

The two live theories from ops were red herrings: the queue's
at-least-once redelivery was correctly documented, and the
system-of-record snapshot for the incident window is byte-identical
to `data/initial_inventory.json` (`diff` shows no delta), so no
last-minute manual stock correction went missing.

## Root cause

### Bug 1: phantom stock

`store/inventory.py`:

```python
available = self._store.get(sku, 0)
approved = available >= qty          # True for any qty <= available, including negatives
if approved:
    self._store[sku] = available - qty   # available - (-3) = available + 3
```

Six malformed jobs in `data/jobs_seed.jsonl` (`qty=-1`, `-2`, `-2`,
`-2`, `-3`, `-3`) inflated three SKUs. The README explicitly requires
these to be rejected ("`Malformed requests (qty <= 0)` ... must be
rejected, not silently applied"), but there was no check.

Expected drift from just this bug (single worker):

| SKU              | phantom units added |
|------------------|---------------------|
| SKU-BOLT-14      | +4  (`-1 + -3`)     |
| SKU-CABLE-9      | +2  (`-2`)          |
| SKU-DRUM-3       | +7  (`-2 + -3 + -2`)|

Reproduced exactly by `scripts/repro_drift.py --workers 1`.

### Bug 2: double-decrement under concurrency

The critical section is split across two `with self._lock:` blocks
with a `time.sleep(DOWNSTREAM_CALL_SEC)` in the middle:

```python
with self._lock:
    if job_id in self._processed_jobs: return cached   # <-- check
time.sleep(DOWNSTREAM_CALL_SEC)                        # <-- lock released
with self._lock:
    ... apply decrement ...
    self._processed_jobs[job_id] = result              # <-- record
```

At-least-once delivery + a wide window between check and record means
two workers can both pass the check for the same `job_id`, both sleep,
and both apply the decrement. The idempotency guarantee is only
enforced when the second delivery arrives *after* the first has
finished writing to the cache — which in practice means only when
retries are far enough apart. Under the peak-window fan-out
(8 → 24 → 64 → 96 workers per `ops/autoscaler_log.txt`), that window
was routinely crossed.

## Why the existing signals missed it

- `tests/test_inventory.py` covers redelivery only in
  `test_redelivered_job_is_not_double_charged_when_sequential`, whose
  own docstring flags that "retries only ever happen back-to-back
  here." The test is sequential; the bug is a race, so the test can't
  see it.
- No test exercises `qty <= 0`, so the phantom-stock bug never fired
  on CI. It would have shown up on any 1-worker replay of real
  traffic — but ops only ran the 1-worker replay looking to reproduce
  the *concurrency* symptom, and stopped once the over-count showed
  up, without noticing that the specific SKUs and magnitudes lined up
  perfectly with the malformed job payloads.
- The bundled sample (95 jobs) is tiny; even at 96 workers, most of
  the wall-clock is dominated by the sleep, so the race window is
  narrow enough to *sometimes* not fire. Peak load did ~3.6k queued
  jobs across 96 workers, per `autoscaler_log.txt` — a much larger
  fan-out and a lot more redeliveries in flight simultaneously.
- The "stale snapshot from before a manual correction" theory
  predicted the SoR would differ from the initial snapshot. It
  doesn't: `diff ops/system_of_record_snapshot.json
  data/initial_inventory.json` is empty. Rules that theory out.

## The fix (`store/inventory.py`)

1. **Reject `qty <= 0` at the entrance.** No inventory mutation, no
   cache write; returns `ReserveResult(..., approved=False)`.
   Redeliveries of malformed jobs re-reject deterministically, so no
   cache entry is needed.
2. **Concurrency-safe idempotency via per-`job_id` placeholder
   events.** The cache slot `_processed_jobs[job_id]` now holds one
   of:
   - a `ReserveResult` (job is fully applied), or
   - a `threading.Event` (some other worker is currently applying
     this `job_id`; wait on it, then read the cached
     `ReserveResult`).
   Only the first arrival for a `job_id` does the work; later
   arrivals block on that job_id's Event *without* touching the
   ledger lock. Distinct `job_id`s do not wait on each other.
3. **`reorder_snapshot()`** — new method per README.md's request
   contract. Returns `physical - safety_stock_floor` per SKU using
   floors passed into the constructor (`ops/safety_stock_floors.json`
   contains the per-SKU values). `snapshot()` behavior is unchanged
   and continues to report raw physical stock, as the existing tests
   require.

Public surface preserved: `Inventory(initial_qty)`,
`reserve(job_id, sku, qty)`, `.approved`, `.snapshot()`, and
`run_jobs(store, jobs, num_workers=...)` all keep their existing
signatures and semantics. `safety_stock_floors` is an optional
kwarg.

### Why this doesn't serialize

The simulated downstream call still happens *outside* the ledger
lock. Threads only touch `self._lock` for O(dict-lookup) time
during the cache check, then again for the ledger write. Two
workers on unrelated `job_id`s never block each other. Only
concurrent duplicates of the *same* `job_id` wait, and they wait on
that job_id's private `Event`, not on the ledger.

Measured: 2000 unique jobs / 96 workers → **0.46s wall-clock** vs
the fully-serial bound of 40s, essentially at the ideal-parallel
floor of 0.42s (`scripts/bench_throughput.py`). Throughput
~4,300 jobs/s.

## How I verified

Everything below is reproducible in this repo. No network required.

### 1. Existing test suite — still green

```
$ python -m pytest tests/ -q
.....                                                    [100%]
5 passed in 0.60s
```

### 2. Deterministic phantom-stock reproduction

`scripts/repro_drift.py` computes the expected final inventory
independently (rejects `qty<=0`, applies each distinct `job_id`
once) and diffs it against the service's snapshot.

Before the fix, at 1 worker:
```
run 1/1 workers=1 jobs=95 elapsed=2.04s
  drift={'SKU-BOLT-14': 4, 'SKU-CABLE-9': 2, 'SKU-DRUM-3': 7}
```
Matches the malformed-job payloads exactly.

After the fix, at 1 worker:
```
run 1/1 workers=1 jobs=95 elapsed=1.95s  drift={}
```

### 3. Race reproduction at peak-window concurrency

Same script, replaying the seed log 20× (~1900 job deliveries) at
96 workers — the peak fan-out from `ops/autoscaler_log.txt`.

Before the fix, three runs:
```
run 1/3 workers=96 jobs=1900 elapsed=0.06s
  drift={'SKU-ANCHOR-01': -4, 'SKU-BOLT-14': -5, 'SKU-CABLE-9': -6,
         'SKU-DRUM-3': -6, 'SKU-EDGE-77': -6}
run 2/3 ... drift={...: -4, ...: -5, ...: -4, ...: -6, ...: -6}
run 3/3 ... drift={...: -4, ...: -5, ...: -1, ...: -6, ...: -6}
```
Different every run — as expected for a race — and always in the
under-count direction, as ops observed.

After the fix, five runs at the same load:
```
run 1/5 workers=96 jobs=1900 elapsed=0.06s drift={}
run 2/5 workers=96 jobs=1900 elapsed=0.05s drift={}
run 3/5 workers=96 jobs=1900 elapsed=0.05s drift={}
run 4/5 workers=96 jobs=1900 elapsed=0.07s drift={}
run 5/5 workers=96 jobs=1900 elapsed=0.05s drift={}
no drift
```

### 4. Throughput sanity check

`scripts/bench_throughput.py` — 2000 distinct `job_id`s (no
redelivery), plenty of stock, 96 workers, so every job should be
approved and every downstream call should be able to run in
parallel:

```
jobs=2000 workers=96 approved=2000
elapsed=0.46s  ideal_parallel=0.42s  fully_serial=40.00s
throughput=4324 jobs/s
```

Wall-clock is at the ideal-parallel floor, confirming the fix does
not serialize the worker pool. The SLA-breaking "wrap-everything-in-
one-lock" alternative would have shown ~40s here (~87× slower).
