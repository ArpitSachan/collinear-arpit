"""Worker pool that drains jobs from a queue and applies them to the store."""

from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

from store.inventory import Inventory
from store.dataclasses import ReserveResult


def process_job(store: Inventory, job: dict) -> ReserveResult:
    return store.reserve(job_id=job["job_id"], sku=job["sku"], qty=job["qty"])


def run_jobs(store: Inventory, jobs: Iterable[dict], num_workers: int) -> list:
    """Process jobs using a pool of num_workers concurrent workers.

    simulates a real production qyueue processing.
    """
    jobs = list(jobs)
    results = []
    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = [pool.submit(process_job, store, job) for job in jobs]
        for future in futures:
            results.append(future.result())
    return results
