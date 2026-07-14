"""In-memory stock ledger used by the workers.

Each SKU has an `available_qty`. Workers call `reserve()` to claim units of a SKU.
"""

import threading
import time
from store.dataclasses import ReserveResult
from store.constants import DOWNSTREAM_CALL_SEC


class Inventory:
    """Tracks available quantity per SKU and applies reservation jobs."""

    def __init__(self, initial_qty: dict, safety_floors: dict | None = None):
        self._store = dict(initial_qty)
        self._safety_floors = dict(safety_floors) if safety_floors else {}
        self._processed_jobs: dict = {}
        self._job_locks: dict = {}
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        return self._store

    def reorder_snapshot(self) -> dict:
        return {
            sku: qty - self._safety_floors.get(sku, 0)
            for sku, qty in self._store.items()
        }

    def reserve(self, job_id: str, sku: str, qty: int) -> ReserveResult:
        # Contract: qty must be a positive integer. Malformed jobs are
        # rejected without touching stock or the idempotency cache — caching
        # a rejection would poison a legitimate future retry of the same id.
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            return ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=False)

        # Acquire (or create) a per-job_id lock so concurrent redeliveries of
        # the same job serialize on each other, while distinct jobs stay
        # concurrent. The global lock is only held long enough to look up /
        # install the per-job lock and to touch the store dicts.
        with self._lock:
            cached = self._processed_jobs.get(job_id)
            if cached is not None:
                return cached
            job_lock = self._job_locks.get(job_id)
            if job_lock is None:
                job_lock = threading.Lock()
                self._job_locks[job_id] = job_lock

        with job_lock:
            # Recheck: a sibling redelivery may have finished while we waited.
            with self._lock:
                cached = self._processed_jobs.get(job_id)
                if cached is not None:
                    return cached

            time.sleep(DOWNSTREAM_CALL_SEC)  # simulated downstream call

            with self._lock:
                available = self._store.get(sku, 0)
                approved = available >= qty
                if approved:
                    self._store[sku] = available - qty
                result = ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=approved)
                self._processed_jobs[job_id] = result
                return result
