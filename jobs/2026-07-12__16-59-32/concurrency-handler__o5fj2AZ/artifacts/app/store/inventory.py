"""In-memory stock ledger used by the workers.

Each SKU has an `available_qty`. Workers call `reserve()` to claim units of a SKU.
"""

import threading
import time
from store.dataclasses import ReserveResult
from store.constants import DOWNSTREAM_CALL_SEC


class Inventory:
    """Tracks available quantity per SKU and applies reservation jobs.

    Concurrency model:
      - A single lock guards `_store`, `_processed_jobs`, and `_in_flight`,
        but is only held for O(1) dict work, never across the simulated
        downstream call. Distinct job_ids still run their downstream call
        in parallel.
      - `_in_flight` maps job_id -> Event. When two workers race the same
        job_id (at-least-once redelivery landing on two workers at once),
        the second one waits on the Event instead of also running the
        downstream call and applying the decrement — that's what makes
        `reserve()` genuinely idempotent per job_id under concurrency, not
        just under back-to-back sequential retries.
    """

    def __init__(self, initial_qty: dict):
        self._store = dict(initial_qty)
        self._processed_jobs = {}
        self._in_flight = {}
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        return self._store

    def reserve(self, job_id: str, sku: str, qty: int) -> ReserveResult:
        with self._lock:
            cached = self._processed_jobs.get(job_id)
            if cached is not None:
                return cached

            # Reject malformed requests per the contract in README.md
            # (qty must be a positive integer). Cache the rejection so
            # redeliveries return the same result and don't hit the
            # downstream call.
            if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
                result = ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=False)
                self._processed_jobs[job_id] = result
                return result

            waiter = self._in_flight.get(job_id)
            if waiter is None:
                waiter = threading.Event()
                self._in_flight[job_id] = waiter
                owns_job = True
            else:
                owns_job = False

        if not owns_job:
            waiter.wait()
            with self._lock:
                return self._processed_jobs[job_id]

        try:
            time.sleep(DOWNSTREAM_CALL_SEC)  # simulated downstream call, kept outside the lock for throughput

            with self._lock:
                available = self._store.get(sku, 0)
                approved = available >= qty
                if approved:
                    self._store[sku] = available - qty

                result = ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=approved)
                self._processed_jobs[job_id] = result
                return result
        finally:
            with self._lock:
                self._in_flight.pop(job_id, None)
            waiter.set()
