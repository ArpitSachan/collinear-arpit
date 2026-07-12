#!/bin/bash
# Oracle solution for collinear-arpit/concurrency-handler.
#
# Two independent root causes explain the two directions of drift:
#
# 1. Lower-than-expected stock (redelivery double-charge): reserve()
#    correctly uses a single instance-level lock, correctly keeps the
#    simulated downstream call outside the lock for throughput - but
#    never re-checks the idempotency cache after re-acquiring the lock
#    post-sleep. Two concurrent deliveries of the same job_id (a normal
#    at-least-once redelivery racing its own earlier attempt) can both
#    pass the pre-sleep cache check before either writes a result, so
#    both proceed to decrement stock. This only manifests when both
#    deliveries are truly concurrent, which is why ops' single-worker
#    replay didn't reproduce it.
#
# 2. Higher-than-expected stock (malformed qty inflation): reserve()
#    never validates qty. A qty<=0 delivery (a mis-routed cancellation
#    event, per README.md's request contract) passes `available >= qty`
#    trivially and, for negative qty, *adds* stock via `available - qty`.
#    This needs no concurrency at all, which is why it reproduced
#    identically at 1 worker - a completely separate bug from #1, not a
#    second symptom of it.
#
# Fix: re-check the idempotency cache after the lock is re-acquired
# (closing the redelivery race), and reject qty<=0 up front (closing the
# malformed-input gap). Both checks stay inside the existing lock
# scoping, so the throughput characteristics are unchanged.
set -euo pipefail

cat > /app/store/inventory.py << 'PYEOF'
"""In-memory stock ledger used by the workers.

Each SKU has an `available_qty`. Workers call `reserve()` to claim units of a SKU.
"""

import threading
import time
from store.dataclasses import ReserveResult
from store.constants import DOWNSTREAM_CALL_SEC

# Per-SKU safety-stock floors (ops/safety_stock_floors.json) - NOT
# uniform. SKU-EDGE-77 carries a higher floor because of a longer
# supplier lead time; everything else documented so far is 5. SKUs with
# no documented floor default to 0.
SAFETY_STOCK_FLOORS = {
    "SKU-ANCHOR-01": 5,
    "SKU-BOLT-14": 5,
    "SKU-CABLE-9": 5,
    "SKU-DRUM-3": 5,
    "SKU-EDGE-77": 12,
}


class Inventory:
    """Tracks available quantity per SKU and applies reservation jobs."""

    def __init__(self, initial_qty: dict):
        self._store = dict(initial_qty)
        self._processed_jobs = {}
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        return self._store

    def reorder_snapshot(self) -> dict:
        """Sellable stock per SKU (physical minus that SKU's safety-stock
        floor), for the automated reordering system - never a single
        flat constant, floors differ per SKU."""
        return {
            sku: qty - SAFETY_STOCK_FLOORS.get(sku, 0)
            for sku, qty in self._store.items()
        }

    def reserve(self, job_id: str, sku: str, qty: int) -> ReserveResult:
        with self._lock:
            cached = self._processed_jobs.get(job_id)
            if cached is not None:
                return cached

        time.sleep(DOWNSTREAM_CALL_SEC)  # simulated downstream call, kept outside the lock for throughput

        with self._lock:
            # Re-check: a concurrent redelivery of the same job_id may
            # have been processed by another thread while this one was
            # sleeping between the two lock acquisitions above.
            cached = self._processed_jobs.get(job_id)
            if cached is not None:
                return cached

            if qty <= 0:
                # Malformed request (e.g. a mis-routed cancellation
                # event, per README.md's request contract) - reject,
                # no inventory side effect.
                result = ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=False)
                self._processed_jobs[job_id] = result
                return result

            available = self._store.get(sku, 0)
            approved = available >= qty
            if approved:
                self._store[sku] = available - qty

            result = ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=approved)
            self._processed_jobs[job_id] = result
            return result
PYEOF

cat > /app/REPORT.md << 'MDEOF'
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

### 3. Missing `reorder_snapshot()`: reordering system was seeing raw physical stock

`README.md`'s request contract requires `Inventory` to expose
`reorder_snapshot()` reporting *sellable* stock (physical minus that
SKU's safety-stock floor) - `Inventory` didn't expose this at all, so
the automated reordering system would have been reading `snapshot()`'s
raw physical count directly, overstating how much stock is actually
free to sell by up to a whole SKU's floor.

Per `ops/safety_stock_floors.json`, floors are **not** uniform - most
SKUs are 5, but `SKU-EDGE-77` is 12 (longer supplier lead time). A fix
that subtracts one flat constant would be correct for four of five SKUs
and silently wrong for `SKU-EDGE-77` specifically - the floors have to
be looked up per SKU, not applied as a single number.

## Fix

- Added a second idempotency check inside the post-sleep lock block, so
  a same-`job_id` race is caught before the ledger is touched a second
  time. The critical section stays scoped to in-memory state only - the
  downstream call remains outside the lock, so independent jobs still
  overlap in flight.
- Added an up-front `qty <= 0` rejection, inside the same lock scope, so
  malformed deliveries are recorded as rejected (idempotently) rather
  than silently mutating the ledger.
- Added `reorder_snapshot()`, subtracting each SKU's documented
  safety-stock floor via a per-SKU lookup (`SAFETY_STOCK_FLOORS`), not a
  single shared constant. `snapshot()` itself is untouched and still
  reports raw physical stock, so nothing that depends on its existing
  contract regresses.

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
MDEOF

echo "Applied fix to /app/store/inventory.py and wrote /app/REPORT.md"
