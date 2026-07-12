# Inventory Drift During Peak Load

You are the on-call engineer for the stock reservation service. Read `/app/INCIDENT.md` first, it's the ops report that describes what was observed and what's already been ruled out.

---

## Repository layout


| Path                                                  | Description                                                                                                                                                   |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `store/inventory.py`                                  | the in-memory stock ledger and its `reserve()` method.                                                                                                        |
| `store/worker.py`                                     | fans a list of jobs out across a pool of worker threads and calls `reserve()` for each.                                                                       |
| `store/queue_processor.py`                            | loads a job log (JSONL) from disk.                                                                                                                            |
| `data/initial_inventory.json`, `data/jobs_seed.jsonl` | a starting inventory snapshot and a sample job log you can use to reproduce and experiment locally.                                                           |
| `ops/`                                                | what the on-call team pulled together during the incident — autoscaler log and a system-of-record inventory snapshot for the incident window.                |
| `tests/test_inventory.py`                             | the existing (currently green) unit test suite.                                                                                                               |
| `scripts/run_demo.py`                                 | CLI to replay a job log through the service with a configurable worker count, e.g.: `python scripts/run_demo.py --jobs data/jobs_seed.jsonl --workers 32`     |
| `README.md`                                           | describes the service, its delivery semantics (at-least-once, so `reserve()` is meant to be idempotent per `job_id`), and its full request contract. |


---



## What you need to deliver

1. **A fix to the reservation service** (in `store/`) that resolves the drift described in `INCIDENT.md` under realistic concurrent load, fully satisfies `README.md`'s request contract, and does not regress `tests/test_inventory.py` or materially hurt throughput of the worker pool (see the incident report's SLA note, a fix that "solves" this by serializing everything is not acceptable).
2. `/app/REPORT.md`, a written explanation of:
  - what the root cause actually was,
  - why it was invisible under the existing test suite and under low-concurrency replay,
  - what you changed, and
  - how you verified the fix (what you ran, and what it showed).

You may add, edit, or run any files/scripts you find useful for investigation. You do not need to modify `tests/test_inventory.py`, `data/`, or `scripts/`, the deliverable is the fix in `store/` plus `REPORT.md`.

---



## Constraints

- Do not change the public behavior relied on by `tests/test_inventory.py` (e.g. `Inventory(...)`, `.reserve(job_id, sku, qty)` returning an object with `.approved`, `.snapshot()`, and `run_jobs(store, jobs, num_workers=...)` must keep working as documented/tested).
- The fix must hold up at the scale the service actually runs at during peak, not just at the scale of the bundled sample in `data/`. `ops/` has what's known about the incident window.
- No network access is available or required for this task.

---



## Environment

- Python 3, `pytest` are already installed. No internet access.
- Your working directory is `/app`.

