"""In-memory stock ledger used by the workers.

Each SKU has an `available_qty`. Workers call `reserve()` to claim units of a SKU.
"""

import threading
import time
from typing import Optional

from store.dataclasses import ReserveResult
from store.constants import DOWNSTREAM_CALL_SEC


class _InFlight:
    """Coordinates concurrent redeliveries of the same job_id.

    Exactly one caller becomes the leader for a given job_id; every other
    caller for the same job_id waits on `event` and then reads `result`.
    """

    __slots__ = ("event", "result")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Optional[ReserveResult] = None


class Inventory:
    """Tracks available quantity per SKU and applies reservation jobs."""

    def __init__(self, initial_qty: dict, safety_stock: Optional[dict] = None):
        self._store = dict(initial_qty)
        self._processed_jobs: dict = {}
        self._in_flight: dict = {}
        self._lock = threading.Lock()
        self._safety_stock = dict(safety_stock or {})

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._store)

    def reorder_snapshot(self) -> dict:
        """Sellable stock per SKU — physical stock minus that SKU's safety-stock floor."""
        with self._lock:
            return {
                sku: max(0, qty - self._safety_stock.get(sku, 0))
                for sku, qty in self._store.items()
            }

    def reserve(self, job_id: str, sku: str, qty: int) -> ReserveResult:
        # README contract: qty is a positive integer. Reject malformed input
        # rather than letting a negative qty flow through as an *increment*.
        # (bool is a subclass of int in Python, so exclude it explicitly.)
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            return ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=False)

        with self._lock:
            cached = self._processed_jobs.get(job_id)
            if cached is not None:
                return cached
            in_flight = self._in_flight.get(job_id)
            if in_flight is None:
                in_flight = _InFlight()
                self._in_flight[job_id] = in_flight
                is_leader = True
            else:
                is_leader = False

        if not is_leader:
            in_flight.event.wait()
            if in_flight.result is not None:
                return in_flight.result
            # Leader failed to produce a result (e.g. exception). Retry as a
            # fresh caller; the leader's finally block cleared _in_flight.
            return self.reserve(job_id, sku, qty)

        try:
            time.sleep(DOWNSTREAM_CALL_SEC)  # simulated downstream call, off the ledger lock

            with self._lock:
                available = self._store.get(sku, 0)
                approved = available >= qty
                if approved:
                    self._store[sku] = available - qty
                result = ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=approved)
                self._processed_jobs[job_id] = result
            in_flight.result = result
            return result
        finally:
            with self._lock:
                self._in_flight.pop(job_id, None)
            in_flight.event.set()
