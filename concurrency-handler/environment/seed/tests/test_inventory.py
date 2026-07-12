"""Existing unit tests for the reservation service.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from store.inventory import Inventory
from store.worker import run_jobs


def test_single_reservation_deducts_stock():
    store = Inventory({"WIDGET": 10})
    result = store.reserve(job_id="j1", sku="WIDGET", qty=3)
    assert result.approved
    assert store.snapshot()["WIDGET"] == 7


def test_insufficient_stock_is_rejected():
    store = Inventory({"WIDGET": 2})
    result = store.reserve(job_id="j1", sku="WIDGET", qty=5)
    assert not result.approved
    assert store.snapshot()["WIDGET"] == 2


def test_redelivered_job_is_not_double_charged_when_sequential():
    # Retries only ever happen back-to-back here, so this passes even
    # though reserve() isn't actually safe under real concurrency.
    store = Inventory({"WIDGET": 10})
    first = store.reserve(job_id="j1", sku="WIDGET", qty=4)
    second = store.reserve(job_id="j1", sku="WIDGET", qty=4)  # redelivery
    assert first == second
    assert store.snapshot()["WIDGET"] == 6


def test_multiple_skus_independent():
    store = Inventory({"WIDGET": 10, "GADGET": 5})
    store.reserve(job_id="j1", sku="WIDGET", qty=3)
    store.reserve(job_id="j2", sku="GADGET", qty=2)
    snap = store.snapshot()
    assert snap["WIDGET"] == 7
    assert snap["GADGET"] == 3


def test_sequential_batch_via_run_jobs_single_worker():
    store = Inventory({"WIDGET": 100})
    jobs = [{"job_id": f"j{i}", "sku": "WIDGET", "qty": 1} for i in range(20)]
    results = run_jobs(store, jobs, num_workers=1)
    assert all(r.approved for r in results)
    assert store.snapshot()["WIDGET"] == 80
