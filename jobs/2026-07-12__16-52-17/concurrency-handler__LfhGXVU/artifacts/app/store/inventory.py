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
        # job_id -> Event held while a worker owns the first delivery of that
        # job_id. Concurrent redeliveries wait on the event, then read from
        # _processed_jobs. Keeps the downstream sleep out of the store lock so
        # unrelated jobs run in parallel.
        self._inflight = {}
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        return self._store

    def reserve(self, job_id: str, sku: str, qty: int) -> ReserveResult:
        # Reject malformed requests per the request contract (qty must be a
        # positive integer). A negative qty would otherwise be "approved" by
        # `available >= qty` and then applied as `available - qty`, silently
        # inflating stock.
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            return ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=False)

        while True:
            with self._lock:
                cached = self._processed_jobs.get(job_id)
                if cached is not None:
                    return cached
                event = self._inflight.get(job_id)
                if event is None:
                    event = threading.Event()
                    self._inflight[job_id] = event
                    break
            # Another worker owns the first delivery of this job_id; wait for
            # it to publish the result, then re-check the cache.
            event.wait()

        try:
            time.sleep(DOWNSTREAM_CALL_SEC)  # simulated downstream call, kept outside the lock for throughput
            with self._lock:
                available = self._store.get(sku, 0)
                approved = available >= qty
                if approved:
                    self._store[sku] = available - qty
                result = ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=approved)
                self._processed_jobs[job_id] = result
                del self._inflight[job_id]
            return result
        finally:
            event.set()
