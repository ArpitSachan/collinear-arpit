# Stock Reservation Service

This service consumes reserve stocks jobs from the order intake queue and decrements `available_qty` in the in-memory `Inventory` for each SKU. A pool of worker threads (`store.worker.run_jobs`) drains the queue concurrently for throughput.

---

## Delivery Semantic

The upstream queue is **at-least-once**: if a worker doesn't ack a job within its visibility timeout, the queue redelivers the same job (same `job_id`) later. `Inventory.reserve()` is documented as idempotent per `job_id`.

## Request Contract

`reserve(job_id, sku, qty)` expects `qty` to be a positive integer.

`Inventory` also exposes `reorder_snapshot()`, reporting sellable stock
per SKU — physical stock minus that SKU's safety-stock floor
(`ops/safety_stock_floors.json`) — for the automated reordering system.
`snapshot()` continues to report raw physical stock, as used by
`tests/test_inventory.py`.

---



## Layout


| Path                       | Description                                                                                                                        |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `store/inventory.py`       | The ledger and `reserve()` logic                                                                                                   |
| `store/worker.py`          | Thread-pool fan-out over a job list                                                                                                |
| `store/queue_processor.py` | Loads a JSONL job log (`data/jobs_seed.jsonl`)                                                                                     |
| `data/`                    | A starting inventory snapshot and a sample job log                                                                                 |
| `ops/`                     | What the on-call team pulled together during the incident (autoscaler log, system-of-record snapshot, per-SKU safety-stock floors) |
| `tests/`                   | The existing unit test suite.                                                                                                      |
| `scripts/run_demo.py`      | CLI to replay a job log through the service with a configurable worker count                                                       |


---



## Running the Existing Tests

```
python -m pytest tests/ -q
```



## Running the Demo

```
python scripts/run_demo.py --jobs data/jobs_seed.jsonl --workers 32
```

