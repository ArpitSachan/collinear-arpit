# INC-4471 — Post-mortem and Fix

## TL;DR

The drift was **two independent bugs** in `Inventory.reserve()`, not one:

1. **Over-count (higher-than-expected available_qty)** — malformed jobs
   with `qty <= 0` were silently *applied* instead of rejected. The
   guard `available >= qty` is trivially true for negative `qty`, and
   `available - qty` then *adds* stock. Reproduces at any worker count,
   including 1, exactly matching Ops's 1-worker replay.
2. **Under-count (lower-than-expected available_qty)** — the idempotency
   check was not atomic with the decrement. `reserve()` released the
   lock across the simulated downstream call, so two concurrent
   redeliveries of the same `job_id` both saw "not yet processed" and
   both applied the decrement. Only reproduces under real concurrency,
   which is why the 1-worker replay didn't show it.

The "stale-snapshot" theory is ruled out: `data/initial_inventory.json`
matches `ops/system_of_record_snapshot.json` byte-for-byte, so no
correction was missed by newly-started workers.

---

## Root causes in detail

### Bug 1: malformed `qty <= 0` decrements *add* stock

`Inventory.reserve()` did:

```python
approved = available >= qty
if approved:
    self._store[sku] = available - qty
```

For `qty = -3` and `available = 48`, `48 >= -3` is `True`, so
`self._store[sku] = 48 - (-3) = 51`. The README documents that
malformed requests (`qty <= 0`) **must** be rejected with
`approved=False` and no inventory change — this code did the opposite.

The seed log has six such entries (all with `-malformed-` in the
`job_id`, so they're presumably a known upstream misroute):

| job_id                                | sku            | qty |
| ------------------------------------- | -------------- | --- |
| job-SKU-BOLT-14-malformed-00005       | SKU-BOLT-14    | -1  |
| job-SKU-BOLT-14-malformed-00003       | SKU-BOLT-14    | -3  |
| job-SKU-CABLE-9-malformed-00004       | SKU-CABLE-9    | -2  |
| job-SKU-DRUM-3-malformed-00000        | SKU-DRUM-3     | -3  |
| job-SKU-DRUM-3-malformed-00001        | SKU-DRUM-3     | -2  |
| job-SKU-DRUM-3-malformed-00002        | SKU-DRUM-3     | -2  |

At 1 worker, running the seed log gave BOLT-14=9, CABLE-9=8, DRUM-3=13
against independently-computed expected values 5, 6, 6 — exactly
`+4`, `+2`, `+7`, precisely the sum of the negated malformed
quantities per SKU. So this bug alone explains every unit of the
over-count.

### Bug 2: TOCTOU on `_processed_jobs` under concurrency

The previous implementation:

```python
with self._lock:
    cached = self._processed_jobs.get(job_id)   # (A) read
    if cached is not None:
        return cached

time.sleep(DOWNSTREAM_CALL_SEC)                 # lock released here

with self._lock:
    ...
    self._store[sku] = available - qty          # (B) write
    self._processed_jobs[job_id] = result
```

The comment on the `time.sleep` was honest — the lock is intentionally
released across the downstream call to preserve throughput — but the
consequence was that between (A) and (B) another thread could enter
with the same `job_id`, also see "not cached", also sleep, and also
apply the decrement. Under the incident window's autoscaler behavior
(8 → 96 workers over ~4 minutes, per `ops/autoscaler_log.txt`) that
race is common: every duplicate delivery landing on a different worker
within the ~20ms downstream window would double-charge stock.

### Why the test suite missed it

`tests/test_inventory.py` covers exactly the two shapes that don't
trigger either bug:

- Every test uses positive `qty` — the malformed-quantity path is never
  exercised at all.
- The only redelivery test (`test_redelivered_job_is_not_double_charged_when_sequential`)
  runs the two `reserve()` calls **sequentially on the same thread**.
  The test's own docstring flags this: "Retries only ever happen
  back-to-back here, so this passes even though reserve() isn't
  actually safe under real concurrency." That's exactly the shape the
  race needs to hide behind.
- `test_sequential_batch_via_run_jobs_single_worker` uses
  `num_workers=1`, so nothing is ever concurrent.

Ops's 1-worker post-hoc replay reproduces bug 1 (which is
concurrency-independent) but hides bug 2 (which needs contention), so
their "one issue with two symptoms vs two unrelated issues" question
resolves as **two unrelated issues** that happened to co-occur in the
incident window.

---

## The fix

Two changes, both in `store/inventory.py`:

1. **Reject malformed `qty` at the top of `reserve()`.** Cache the
   rejection under `job_id` so redeliveries also short-circuit
   (respecting the "idempotent per `job_id`" contract).
2. **Add per-`job_id` in-flight tracking.** Under `_lock`, if this
   `job_id` isn't cached and isn't in flight, install a
   `threading.Event` and mark it in flight; then release the lock and
   perform the downstream call. Concurrent duplicates that arrive
   while the first delivery is in flight see the Event, `wait()` on
   it, and after it fires read the cached result — they never run the
   downstream call themselves and never touch `_store`.

The main `_lock` is still only held for O(1) dict work — never
across `time.sleep(DOWNSTREAM_CALL_SEC)` — so distinct `job_id`s run
their downstream calls in parallel exactly as before. Only same-`job_id`
duplicates serialize, which is what idempotency requires anyway.

---

## Verification

### Existing suite still green

```
$ python -m pytest tests/ -q
.....                                                                    [100%]
5 passed in 0.60s
```

### Seed log replay — before vs after, independently-computed oracle

Oracle: apply each distinct `job_id` once, reject `qty <= 0`, otherwise
apply the decrement if stock covers it.

|                        | ANCHOR-01 | BOLT-14 | CABLE-9 | DRUM-3 | EDGE-77 |
| ---------------------- | --------- | ------- | ------- | ------ | ------- |
| **Oracle (expected)**  |     4     |    5    |    6    |    6   |    6    |
| Before, 1 worker       |     4     |    9    |    8    |   13   |    6    |
| Before, 32 workers     |     2     |    6    |    7    |   11   |    2    |
| **After, 1 worker**    |     4     |    5    |    6    |    6   |    6    |
| **After, 32 workers**  |     4     |    5    |    6    |    6   |    6    |
| **After, 96 workers**  |     4     |    5    |    6    |    6   |    6    |

The before/1-worker column is exactly bug 1 (over-count matches the
sum of malformed quantities per SKU). The before/32-worker column adds
bug 2 (further drift, this time downward, from concurrent double
decrements).

### Peak-scale stress

`scripts/repro_incident.py` synthesises a workload sized like the
incident window (per `ops/autoscaler_log.txt`: worker pool peaked at
96, queue depth ~3.6k), with realistic redelivery and malformed rates,
and compares final inventory to an independent oracle:

```
$ python scripts/repro_incident.py --workers 96 --seeds 8
summary: 8 run(s), 42080 total job deliveries, 7.50s wall time,
5607 jobs/s aggregate, ok=True

$ python scripts/repro_incident.py --workers 96 --seeds 4 \
        --unique 2000 --redelivery-rate 0.35 --malformed-rate 0.10
summary: 4 run(s), 68073 total job deliveries, 9.76s wall time,
6974 jobs/s aggregate, ok=True
```

Zero drift across ~110k job deliveries at peak worker count and heavy
redelivery + malformed rates. Multiple random seeds are important
here — a single "green" concurrent run could just be lucky
interleaving.

### Throughput is not regressed

The SLA concern is that a fix which serializes everything would trade
one incident for another. Simple scaling test (2000 unique jobs, no
duplicates, so each hits the 20 ms simulated downstream call):

```
workers=  1  2000 jobs in 46.68s  (43 jobs/s)
workers=  8  2000 jobs in  5.58s  (358 jobs/s)
workers= 32  2000 jobs in  1.39s  (1441 jobs/s)
workers= 96  2000 jobs in  0.47s  (4214 jobs/s)
```

96 workers is ~98× faster than 1 worker — close to linear scaling
against `DOWNSTREAM_CALL_SEC`. The pool is parallelizing as intended;
the lock is only held across dict work.

---

## Files touched / added

- `store/inventory.py` — the fix (per-`job_id` in-flight Event +
  malformed-qty rejection).
- `scripts/repro_incident.py` — new. Peak-scale reproducer with
  independent oracle, used to validate the fix.
- `tests/test_inventory.py` — untouched, still passes.
- `data/`, `ops/` — untouched.
