# INC-4471 — Post-mortem and Fix

## TL;DR

Two independent bugs in `Inventory.reserve()`, one masquerading as the
other:

1. **Higher-than-expected drift** — the ledger accepted jobs with
   non-positive `qty` and applied them literally, so `qty=-3` added 3
   units of stock. This has nothing to do with concurrency; that is why
   Ops still saw it at 1 worker.
2. **Lower-than-expected drift** — `reserve()`'s idempotency check and
   its stock decrement were not in the same critical section. Under
   real concurrency, two redeliveries of the same `job_id` could both
   pass the "already processed?" check while the sibling was sleeping
   in the simulated downstream call, then both decrement the ledger.
   This is a textbook check-then-act race.

The "stale snapshot / manual correction not landed" theory was ruled
out: `data/initial_inventory.json` equals `ops/system_of_record_snapshot.json`
byte for byte, so the correction *did* land. No shim needed.

The fix keeps the pool concurrent — each `job_id` gets its own lock,
so redeliveries of *the same* job serialize on each other but
distinct jobs still run in parallel. Peak-scale stress (5,508
deliveries, 200 SKUs, 96 workers, matching the incident window) runs
in 1.17s with zero drift. A naive "one global lock across `reserve()`"
would fix the drift too but takes 123.5s on the same workload — 105×
slower, which is the SLA breach Ops called out.

---

## Root cause

### Bug 1 — malformed jobs (higher-than-expected)

`reserve()` did not validate `qty`. The README's request contract says
`qty` is a positive integer, but nothing enforced it. `data/jobs_seed.jsonl`
contains six entries with negative `qty` (`job-*-malformed-*`), and
`available - (-n) == available + n`. So a "reservation" with
`qty=-3` handed 3 units back to the ledger. Distributed over the seed
that's exactly the extra 4/2/7 units observed for BOLT/CABLE/DRUM.

Because this is deterministic and per-job, replaying at 1 worker
reproduced it identically — matching Ops's observation.

### Bug 2 — concurrent redelivery race (lower-than-expected)

Original `reserve()`:

```python
with self._lock:
    if job_id in self._processed_jobs:      # (A) check
        return self._processed_jobs[job_id]

time.sleep(DOWNSTREAM_CALL_SEC)              # (B) lock released, ~20ms outside crit-sec

with self._lock:
    ...                                      # (C) decrement + cache
```

Timeline that loses stock:

| t    | worker X (first delivery of job J)                    | worker Y (redelivery of job J)                        |
| ---- | ------------------------------------------------------ | ------------------------------------------------------ |
| t0   | passes (A): J not in cache                             |                                                        |
| t1   | in (B), sleeping                                       | passes (A): J still not in cache (X hasn't cached yet) |
| t2   | in (B), sleeping                                       | in (B), sleeping                                       |
| t3   | enters (C): available -= qty, cache J                  |                                                        |
| t4   |                                                        | enters (C): available -= qty AGAIN, overwrite cache    |

The cache is populated *after* the sleep, so it can't dedupe siblings
that entered `reserve()` while the first delivery was still in flight.
Both siblings decrement. This is what caused the negative drift, and it
only fires when at-least-once redeliveries land on parallel workers —
which is exactly what the autoscaler enabled during the 96-worker peak
(`ops/autoscaler_log.txt`).

### Why the "stale snapshot" theory was wrong

`data/initial_inventory.json` and `ops/system_of_record_snapshot.json`
are identical. The manual correction landed. No need to reconcile
against the SoR; the bug is in `reserve()`, not in how workers boot.

---

## Why the existing tests missed this

`tests/test_inventory.py` never exercised either bug:

- **No malformed input coverage.** Every test passes `qty >= 1`.
  `test_insufficient_stock_is_rejected` uses `qty=5`, not `qty=-5`.
- **No real concurrency.** The only "concurrency" test is
  `test_sequential_batch_via_run_jobs_single_worker` (`num_workers=1`),
  which cannot expose a check-then-act race. The redelivery test
  (`test_redelivered_job_is_not_double_charged_when_sequential`) even
  admits in a comment that it only exercises back-to-back retries.
- **No workload with duplicate `job_id` across parallel workers.** The
  race requires two siblings to be inside `reserve()` at the same time,
  and no test ever produces that.

Ops's single-worker replay had the same blind spot for bug 2 (it
serialized everything by construction), which is why they saw only the
"higher" drift on replay — bug 1 is order-independent and worker-count-
independent, bug 2 requires ≥2 workers plus a redelivery of the same
`job_id`.

---

## The fix

`store/inventory.py`:

1. **Reject `qty <= 0` (and non-`int`) up front.** Return an
   unapproved `ReserveResult` without touching the ledger *and without
   caching the rejection* — caching would poison a legitimate future
   retry that arrives with a corrected payload under the same `job_id`.
2. **Per-`job_id` lock.** The global lock is only held long enough to
   check the cache and to install a `threading.Lock()` for this
   `job_id` if one doesn't exist. The simulated downstream call and
   the decrement both happen under that per-job lock, so concurrent
   redeliveries of the same job serialize on each other while
   different jobs still run in parallel. A re-check of the cache
   after acquiring the per-job lock covers the case where the sibling
   finished while we were waiting.
3. **`reorder_snapshot()`** implemented to match the README contract
   (physical stock minus per-SKU safety floor). Optional
   `safety_floors=` on the constructor; defaults to empty so no
   existing test or caller has to change.

The public API relied on by the test suite (`Inventory(initial)`,
`.reserve(job_id, sku, qty)` → `.approved`, `.snapshot()`,
`run_jobs(...)`) is unchanged.

---

## Verification

### Existing test suite

```
$ python -m pytest tests/ -q
.....                                                                    [100%]
5 passed in 0.58s
```

### Bundled sample (`data/jobs_seed.jsonl`)

Independently-computed expected values (starting − sum of unique-`job_id`
qty, ignoring negative-qty malformations):

| SKU           | Expected |
| ------------- | -------- |
| SKU-ANCHOR-01 | 4        |
| SKU-BOLT-14   | 5        |
| SKU-CABLE-9   | 6        |
| SKU-DRUM-3    | 6        |
| SKU-EDGE-77   | 6        |

Before the fix (from an earlier repro): 1-worker replay drifted **up**
(BOLT 9, CABLE 8, DRUM 13) — bug 1; 32-worker replay drifted in both
directions (ANCHOR 2, BOLT 6, DRUM 11, EDGE 2) — bug 1 + bug 2.

After the fix, all three worker counts match the expected values
exactly:

```
1 worker : {ANCHOR-01: 4, BOLT-14: 5, CABLE-9: 6, DRUM-3: 6, EDGE-77: 6}
32 workers: {ANCHOR-01: 4, BOLT-14: 5, CABLE-9: 6, DRUM-3: 6, EDGE-77: 6}
96 workers: {ANCHOR-01: 4, BOLT-14: 5, CABLE-9: 6, DRUM-3: 6, EDGE-77: 6}
```

### Peak-scale stress (`scripts/stress.py`)

`data/jobs_seed.jsonl` is 95 rows; the incident peaked at 3,650 queue
depth and 96 workers. `scripts/stress.py` synthesizes a workload at
that scale (200 SKUs × 25 jobs, ~8% redelivery, ~2% malformed = 5,508
deliveries), computes the expected result independently, and runs it
against both the fixed `Inventory` and a "one global lock across
`reserve()`" baseline:

```
$ python scripts/stress.py --skus 200 --jobs-per-sku 25 --workers 96 --benchmark
Workload: 200 SKUs, 5508 deliveries (440 approx redeliveries, 110 malformed), 96 workers
per-job-lock fix: 1.17s, drifted SKUs: 0
naive global-lock  : 123.54s, drifted SKUs: 0
speedup: 105.5x
```

Two things this shows:

- **Correctness.** Zero SKUs drift at 96 workers on a workload 58× the
  size of the bundled sample. The per-job-lock design is what makes
  the parallel workers safe, not just a coincidence of the small
  sample.
- **No serialization.** The naive "hold a global lock across the whole
  `reserve()` call" fix is also correct, but takes 105× longer because
  every worker queues behind whichever one is currently sleeping in
  the simulated downstream call. The per-job-lock design fans out to
  the full pool.

Torture check — few SKUs, heavy redelivery (worst case for lock
contention on hot job_ids):

```
$ python scripts/stress.py --skus 5 --jobs-per-sku 500 --redelivery-rate 0.4 --workers 96 --seed 7
Workload: 5 SKUs, 3570 deliveries (1428 approx redeliveries, 71 malformed), 96 workers
per-job-lock fix: 0.69s, drifted SKUs: 0
```

For contrast, the *original* `reserve()` on that same workload:

```
OLD/buggy result vs expected under stress:
  SKU-0000: got=984  expected=1010 diff=-26
  SKU-0001: got=1016 expected=1022 diff=-6
  SKU-0002: got=1054 expected=1055 diff=-1
  SKU-0003: got=1000 expected=1009 diff=-9
  SKU-0004: got=970  expected=971  diff=-1
```

Same input, same worker count, same seed — the buggy code drifts, the
fix does not.

---

## Files changed

- `store/inventory.py` — validation, per-`job_id` lock, `reorder_snapshot()`.
- `scripts/stress.py` (new) — reproducible peak-scale harness so future
  regressions in this area get caught at incident-window scale, not
  95-row scale.
