# Stock Reservation Service

This service consumes reserve stocks jobs from the order intake queue and decrements `available_qty` in the in-memory `Inventory` for each SKU. A pool of worker threads (`store.worker.run_jobs`) drains the queue concurrently for throughput.

---

## Delivery Semantics

The upstream queue is **at-least-once**: if a worker doesn't ack a job within its visibility timeout, the queue redelivers the same job (same `job_id`) later. `Inventory.reserve()` is documented as idempotent per `job_id` for exactly this reason replaying a `job_id` must not change inventory twice.

## Request Contract

`reserve(job_id, sku, qty)` expects `qty` to be a **positive integer**.
Malformed requests (`qty <= 0`) can appear on the queue — e.g. a
mis-routed cancellation event replayed onto the reservation topic — and
must be rejected (`approved=False`, no inventory change), not silently
applied.

`Inventory` must also expose `reorder_snapshot()`, reporting **sellable**
stock per SKU (physical stock minus that SKU's safety-stock floor) —
the automated reordering system reads this directly to decide what to
reorder, so it must never include stock that's actually off-limits.
Floors are tracked per SKU (they are **not** uniform — ops adjusts
individual SKUs based on that SKU's supplier lead time) in
`ops/safety_stock_floors.json`. `snapshot()` is unaffected and continues
to report raw physical stock, as tested today.

---



## Layout


| Path                       | Description                                                                  |
| -------------------------- | ---------------------------------------------------------------------------- |
| `store/inventory.py`       | The ledger and `reserve()` logic                                             |
| `store/worker.py`          | Thread-pool fan-out over a job list                                          |
| `store/queue_processor.py` | Loads a JSONL job log (`data/jobs_seed.jsonl`)                               |
| `data/`                    | A starting inventory snapshot and a sample job log                           |
| `ops/`                     | What the on-call team pulled together during the incident (autoscaler log, system-of-record snapshot, per-SKU safety-stock floors) |
| `tests/`                   | The existing unit test suite.                                                |
| `scripts/run_demo.py`      | CLI to replay a job log through the service with a configurable worker count |


---



## Running the Existing Tests

```
python -m pytest tests/ -q
```



## Running the Demo

```
python scripts/run_demo.py --jobs data/jobs_seed.jsonl --workers 32
```

