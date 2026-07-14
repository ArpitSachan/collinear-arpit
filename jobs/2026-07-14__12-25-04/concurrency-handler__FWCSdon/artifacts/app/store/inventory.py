"""In-memory stock ledger used by the workers.

Each SKU has an `available_qty`. Workers call `reserve()` to claim units of a SKU.
"""

import json
from pathlib import Path
import threading
import time
from store.dataclasses import ReserveResult
from store.constants import DOWNSTREAM_CALL_SEC


_SAFETY_STOCK_FLOORS_PATH = (
    Path(__file__).resolve().parents[1] / "ops" / "safety_stock_floors.json"
)


class Inventory:
    """Tracks available quantity per SKU and applies reservation jobs."""

    def __init__(self, initial_qty: dict, safety_stock_floors: dict | None = None):
        self._store = dict(initial_qty)
        self._processed_jobs = {}
        self._inflight_jobs = {}
        self._jobs_lock = threading.Lock()
        self._store_lock = threading.Lock()
        self._safety_stock_floors = (
            dict(safety_stock_floors)
            if safety_stock_floors is not None
            else self._load_safety_stock_floors()
        )

    def snapshot(self) -> dict:
        with self._store_lock:
            return dict(self._store)

    def reorder_snapshot(self) -> dict:
        with self._store_lock:
            return {
                sku: qty - self._safety_stock_floors.get(sku, 0)
                for sku, qty in self._store.items()
            }

    def reserve(self, job_id: str, sku: str, qty: int) -> ReserveResult:
        while True:
            with self._jobs_lock:
                cached = self._processed_jobs.get(job_id)
                if cached is not None:
                    return cached

                event = self._inflight_jobs.get(job_id)
                if event is None:
                    self._inflight_jobs[job_id] = threading.Event()
                    break

            event.wait()

        try:
            result = self._apply_reservation(job_id=job_id, sku=sku, qty=qty)
        except Exception:
            with self._jobs_lock:
                event = self._inflight_jobs.pop(job_id)
                event.set()
            raise

        with self._jobs_lock:
            self._processed_jobs[job_id] = result
            event = self._inflight_jobs.pop(job_id)
            event.set()
        return result

    def _apply_reservation(self, job_id: str, sku: str, qty: int) -> ReserveResult:
        if type(qty) is not int or qty <= 0:
            return ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=False)

        time.sleep(DOWNSTREAM_CALL_SEC)  # simulated downstream call, kept outside stock locks for throughput

        with self._store_lock:
            available = self._store.get(sku, 0)
            approved = available >= qty
            if approved:
                self._store[sku] = available - qty

        return ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=approved)

    @staticmethod
    def _load_safety_stock_floors() -> dict:
        try:
            with open(_SAFETY_STOCK_FLOORS_PATH) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
