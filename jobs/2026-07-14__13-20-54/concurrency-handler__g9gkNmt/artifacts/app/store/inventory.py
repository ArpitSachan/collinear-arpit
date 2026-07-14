"""In-memory stock ledger used by the workers.

Each SKU has an `available_qty`. Workers call `reserve()` to claim units of a SKU.
"""

import json
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from store.dataclasses import ReserveResult
from store.constants import DOWNSTREAM_CALL_SEC

_DEFAULT_SAFETY_STOCK_PATH = Path(__file__).resolve().parents[1] / "ops" / "safety_stock_floors.json"


def _load_default_safety_stock_floors() -> dict:
    try:
        with open(_DEFAULT_SAFETY_STOCK_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


class Inventory:
    """Tracks available quantity per SKU and applies reservation jobs."""

    def __init__(self, initial_qty: dict, safety_stock_floors: dict | None = None):
        self._store = dict(initial_qty)
        self._processed_jobs = {}
        self._inflight_jobs = {}
        self._safety_stock_floors = (
            _load_default_safety_stock_floors()
            if safety_stock_floors is None
            else dict(safety_stock_floors)
        )
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._store)

    def reorder_snapshot(self) -> dict:
        with self._lock:
            return {
                sku: qty - self._safety_stock_floors.get(sku, 0)
                for sku, qty in self._store.items()
            }

    def reserve(self, job_id: str, sku: str, qty: int) -> ReserveResult:
        should_process, inflight = self._claim_job(job_id)
        if not should_process:
            return inflight.result()

        try:
            result = self._process_reservation(job_id, sku, qty)
        except BaseException as exc:
            self._fail_job(job_id, inflight, exc)
            raise

        self._complete_job(job_id, inflight, result)
        return result

    def _claim_job(self, job_id: str) -> tuple[bool, Future]:
        with self._lock:
            cached = self._processed_jobs.get(job_id)
            if cached is not None:
                completed = Future()
                completed.set_result(cached)
                return False, completed

            inflight = self._inflight_jobs.get(job_id)
            if inflight is not None:
                return False, inflight

            inflight = Future()
            self._inflight_jobs[job_id] = inflight
            return True, inflight

    def _process_reservation(self, job_id: str, sku: str, qty: int) -> ReserveResult:
        if not self._valid_qty(qty):
            return ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=False)

        time.sleep(DOWNSTREAM_CALL_SEC)  # simulated downstream call, kept outside the lock for throughput

        with self._lock:
            available = self._store.get(sku, 0)
            approved = available >= qty
            if approved:
                self._store[sku] = available - qty

            result = ReserveResult(job_id=job_id, sku=sku, qty=qty, approved=approved)
            return result

    def _complete_job(self, job_id: str, inflight: Future, result: ReserveResult) -> None:
        with self._lock:
            self._processed_jobs[job_id] = result
            self._inflight_jobs.pop(job_id, None)

        inflight.set_result(result)

    def _fail_job(self, job_id: str, inflight: Future, exc: BaseException) -> None:
        with self._lock:
            self._inflight_jobs.pop(job_id, None)

        inflight.set_exception(exc)

    @staticmethod
    def _valid_qty(qty: int) -> bool:
        return type(qty) is int and qty > 0
