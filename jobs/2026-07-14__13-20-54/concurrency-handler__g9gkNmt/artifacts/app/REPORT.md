# Inventory Drift Incident Report

## Root Cause

There were two independent defects that produced opposite-looking drift.

First, `Inventory.reserve()` checked `_processed_jobs`, released the lock for the simulated downstream call, and only recorded the `job_id` after applying the reservation. Under concurrent redelivery, multiple workers could observe the same `job_id` as unprocessed, all run the downstream call, and then each apply the same reservation. That caused available stock to end lower than the distinct fulfilled-order total.

Second, `reserve()` did not enforce the README request contract that `qty` must be a positive integer. The incident job log contains malformed deliveries with negative quantities. The old code treated those as approved because `available >= negative_qty` is true, then subtracted the negative value, increasing stock. That caused the SKUs that reproduced at one worker to end higher than expected.

The stale-starting-snapshot theory was not supported by the repository evidence: `data/initial_inventory.json` and `ops/system_of_record_snapshot.json` contain the same quantities for the incident SKUs.

## Why Existing Tests Missed It

The existing idempotency test redelivered the same `job_id` sequentially. In that path, the first call writes `_processed_jobs` before the second call checks it, so the race is invisible.

The low-concurrency replay reproduced only the malformed-quantity issue because a single worker cannot overlap two deliveries of the same `job_id` during the downstream delay. It still accepted negative quantities, so stock was increased deterministically.

The original tests also did not cover invalid `qty` values or the documented `reorder_snapshot()` method.

## What Changed

`store/inventory.py` now tracks in-flight reservations by `job_id`. The first worker to claim a `job_id` performs the downstream work; concurrent redeliveries wait for that same result and return it. The downstream call remains outside the lock, so unrelated jobs still run concurrently and the worker pool is not serialized.

`reserve()` now rejects non-positive and non-integer quantities by returning `ReserveResult(..., approved=False)` without changing stock. This makes malformed deliveries idempotently rejected instead of turning into stock increments.

`snapshot()` now returns a locked copy of the raw physical stock, and `reorder_snapshot()` was added to report physical stock minus the configured per-SKU safety-stock floor from `ops/safety_stock_floors.json`.

I also added regression tests in `tests/test_inventory_incident_regression.py` for concurrent redelivery, invalid quantities, and `reorder_snapshot()`.

## Verification

Existing and new tests:

```text
$ python -m pytest tests/ -q
........                                                                 [100%]
8 passed in 0.67s
```

Code compilation:

```text
$ python -m compileall store tests -q
```

Before the fix, replaying the bundled job log showed both symptoms:

```text
$ python scripts/run_demo.py --jobs data/jobs_seed.jsonl --workers 1
SKU-ANCHOR-01: 4
SKU-BOLT-14: 9
SKU-CABLE-9: 8
SKU-DRUM-3: 13
SKU-EDGE-77: 6

$ python scripts/run_demo.py --jobs data/jobs_seed.jsonl --workers 96
SKU-ANCHOR-01: 2
SKU-BOLT-14: 6
SKU-CABLE-9: 5
SKU-DRUM-3: 11
SKU-EDGE-77: 2
```

Auditing the log by distinct valid positive `job_id` gives the expected final inventory:

```text
SKU-ANCHOR-01: 4
SKU-BOLT-14: 5
SKU-CABLE-9: 6
SKU-DRUM-3: 6
SKU-EDGE-77: 6
```

After the fix, the replay matches that expected inventory across low and peak worker counts:

```text
$ python scripts/run_demo.py --jobs data/jobs_seed.jsonl --workers 1
SKU-ANCHOR-01: 4
SKU-BOLT-14: 5
SKU-CABLE-9: 6
SKU-DRUM-3: 6
SKU-EDGE-77: 6

$ python scripts/run_demo.py --jobs data/jobs_seed.jsonl --workers 96
SKU-ANCHOR-01: 4
SKU-BOLT-14: 5
SKU-CABLE-9: 6
SKU-DRUM-3: 6
SKU-EDGE-77: 6
```

I also ran a synthetic peak-scale replay based on the autoscaler log's observed 96-worker peak. It used 3,650 distinct valid jobs, duplicate redeliveries, and malformed negative deliveries:

```text
deliveries 4241 distinct_valid 3650 workers 96 elapsed_sec 0.974
matches_expected True
```

That confirms the fix preserves concurrent throughput while making the final ledger match the distinct valid reservation log.
