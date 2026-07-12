"""A copy of the shipped tests/test_inventory.py
This is run by the verifier against the candidate's /app/store package directly.

So that if agent somehow chnages the tests/test_inventory.py we still have this to run against and verify.
"""

import sys

sys.path.insert(0, "/app")

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
    store = Inventory({"WIDGET": 10})
    first = store.reserve(job_id="j1", sku="WIDGET", qty=4)
    second = store.reserve(job_id="j1", sku="WIDGET", qty=4)
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


def test_malformed_qty_is_rejected_without_side_effects():
    # README.md's request contract: qty must be a positive integer;
    # qty<=0 deliveries (e.g. mis-routed cancellation events) must be
    # rejected, not silently applied.
    store = Inventory({"WIDGET": 10})

    negative = store.reserve(job_id="j-neg", sku="WIDGET", qty=-3)
    assert not negative.approved
    assert store.snapshot()["WIDGET"] == 10

    zero = store.reserve(job_id="j-zero", sku="WIDGET", qty=0)
    assert not zero.approved
    assert store.snapshot()["WIDGET"] == 10


def test_reorder_snapshot_uses_per_sku_safety_stock_floor():
    # README.md's request contract: reorder_snapshot() must report
    # sellable stock (physical minus that SKU's safety-stock floor).
    # Per ops/safety_stock_floors.json floors are NOT uniform - most
    # SKUs are 5, but SKU-EDGE-77 is 12 (longer supplier lead time).
    store = Inventory({"SKU-ANCHOR-01": 20, "SKU-EDGE-77": 20})
    reorder = store.reorder_snapshot()
    assert reorder["SKU-ANCHOR-01"] == 15  # 20 - 5
    assert reorder["SKU-EDGE-77"] == 8     # 20 - 12, NOT 20 - 5

    # SKUs with no documented floor default to 0, so this is purely
    # additive - snapshot()'s existing raw-physical-count contract (and
    # every test above that relies on it) is unaffected.
    other = Inventory({"WIDGET": 10})
    assert other.reorder_snapshot()["WIDGET"] == 10
    assert other.snapshot()["WIDGET"] == 10
