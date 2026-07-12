"""In-memory stock ledger used by the workers.

Each SKU has an `available_qty`. Workers call `reserve()` to claim units of a SKU.
"""

import threading
import time
from store.dataclasses import ReserveResult
from store.constants import DOWNSTREAM_CALL_SEC


class Inventory:
    """Tracks available quantity per SKU and applies reservation jobs."""

    def __init__(self, initial_qty: dict, safety_stock_floors: dict | None = None):
        self._store = dict(initial_qty)
        # _processed_jobs[job_id] is either a ReserveResult (final) or a
        # threading.Event (a job with that id is currently being processed).
        self._processed_jobs: dict = {}
        self._lock = threading.Lock()
        self._floors = dict(safety_stock_floors) if safety_stock_floors else {}

    def snapshot(self) -> dict:
        return self._store

    def reorder_snapshot(self) -> dict:
        """Sellable stock per SKU: physical stock minus that SKU's safety-stock floor.

        The automated reordering system reads this directly, so stock that
        is off-limits (the per-SKU safety floor) must never be included.
        """
        with self._lock:
            return {
                sku: qty - self._floors.get(sku, 0)
                for sku, qty in self._store.items()
            }

    def reserve(self, job_id: str, sku: str, qty: int) -> ReserveResult:
        # Reject malformed requests up front. Per the request contract,
        # qty must be a positive integer; a mis-routed cancellation event
        # (qty <= 0) must be rejected, not silently applied. Rejection is
        # deterministic per (job_id, sku, qty), so re-rejecting on redelivery
        # is safe and we don't need to cache it.
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            return ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=False)

        # Idempotency: concurrent duplicates of the same job_id must apply
        # at most once. The first arrival installs a placeholder Event and
        # does the work; later arrivals wait on it and return the cached
        # result. Distinct job_ids never wait on each other, so throughput
        # is not serialized.
        with self._lock:
            cached = self._processed_jobs.get(job_id)
            if isinstance(cached, ReserveResult):
                return cached
            if cached is None:
                event = threading.Event()
                self._processed_jobs[job_id] = event
                we_own = True
            else:
                event = cached
                we_own = False

        if not we_own:
            event.wait()
            with self._lock:
                # By contract, the owner replaces the Event with a
                # ReserveResult before setting the event.
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
        finally:
            event.set()

        return result
