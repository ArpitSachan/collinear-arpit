# Inventory Drift During Peak Load

## Root Cause

There were two independent defects.

1. `reserve()` was not idempotent while a redelivered job was already in
   flight. The old code checked `_processed_jobs`, released the lock for the
   simulated downstream call, then decremented stock without checking whether
   another worker had completed the same `job_id` in the meantime. At high
   worker counts, duplicate deliveries of the same `job_id` could therefore
   decrement inventory more than once.

2. `reserve()` did not enforce the documented positive-integer `qty` contract.
   The seed job log contains six malformed deliveries with negative
   quantities. The old approval check treated those as valid because
   `available >= negative_qty` is true, then subtracted the negative number and
   increased stock. That reproduced with one worker, which matches Ops'
   observation that some SKUs were higher than expected even without
   concurrency.

The stale-snapshot theory did not hold for the bundled incident evidence:
`ops/system_of_record_snapshot.json` matches `data/initial_inventory.json`.

## Why Existing Tests Missed It

The existing redelivery test is sequential: the first delivery fully finishes
and caches its result before the duplicate arrives. It never exercises the
window where two workers see the same unseen `job_id` concurrently.

The existing tests also only use positive quantities, so they never hit the
negative-quantity path that was increasing inventory. Low-concurrency replay
could reproduce only that input-validation bug; it could not reproduce the
in-flight duplicate race.

## What Changed

`store/inventory.py` now tracks in-flight jobs separately from completed jobs.
The first worker for a new `job_id` owns the reservation. Concurrent
redeliveries of that same `job_id` wait for the owner and return the cached
`ReserveResult`. Other `job_id`s continue processing concurrently; the
simulated downstream call remains outside stock locks.

`reserve()` now rejects malformed quantities (`qty` must be an actual positive
`int`) by returning an unapproved `ReserveResult` without mutating stock. The
result is cached by `job_id`, so malformed redeliveries are idempotent too.

I also added thread-safe snapshot copies and implemented the README-documented
`reorder_snapshot()`, subtracting each SKU's safety-stock floor from
`ops/safety_stock_floors.json`.

## Verification

Commands run:

```bash
python -m pytest tests/ -q
python -m py_compile store/*.py tests/*.py scripts/run_demo.py
python scripts/run_demo.py --jobs data/jobs_seed.jsonl --workers 1
python scripts/run_demo.py --jobs data/jobs_seed.jsonl --workers 96
```

Results:

- The full test suite passes: `9 passed`.
- One-worker replay now ends at the independently expected inventory:
  `SKU-ANCHOR-01=4`, `SKU-BOLT-14=5`, `SKU-CABLE-9=6`, `SKU-DRUM-3=6`,
  `SKU-EDGE-77=6`.
- Ninety-six-worker replay now ends at the same inventory, so the peak-load
  duplicate redelivery race no longer changes totals.
- A synthetic peak-scale check using 3,852 deliveries, 3,650 unique valid
  reservations, duplicate redeliveries, malformed negative quantities, and 96
  workers completed in `0.907s`; the sold count was exactly `3650`.

I added regression coverage in `tests/test_inventory_incident_regression.py`
for concurrent duplicate idempotency, malformed quantities, `reorder_snapshot()`,
and a 96-worker replay of the seed incident log against an independently
computed expected inventory.
