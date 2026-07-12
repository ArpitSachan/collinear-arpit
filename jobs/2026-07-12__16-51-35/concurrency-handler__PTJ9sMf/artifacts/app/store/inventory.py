"""In-memory stock ledger used by the workers.

Each SKU has an `available_qty`. Workers call `reserve()` to claim units of a SKU.
"""

import threading
import time
from store.dataclasses import ReserveResult
from store.constants import DOWNSTREAM_CALL_SEC


class Inventory:
    """Tracks available quantity per SKU and applies reservation jobs."""

    def __init__(self, initial_qty: dict):
        self._store = dict(initial_qty)
        self._processed_jobs = {}
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        return self._store

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
