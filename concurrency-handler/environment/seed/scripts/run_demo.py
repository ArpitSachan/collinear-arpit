"""Convenience CLI to replay a job log through the reservation service.

Example:
    python scripts/run_demo.py --jobs data/jobs_seed.jsonl --workers 32
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from store.inventory import Inventory
from store.queue_processor import load_jobs
from store.worker import run_jobs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", default="data/jobs_seed.jsonl")
    parser.add_argument("--inventory", default="data/initial_inventory.json")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    with open(args.inventory) as f:
        initial_qty = json.load(f)

    jobs = list(load_jobs(args.jobs))

    store = Inventory(initial_qty)
    t0 = time.time()
    run_jobs(store, jobs, num_workers=args.workers)
    dt = time.time() - t0

    print(f"processed {len(jobs)} job deliveries with {args.workers} workers in {dt:.2f}s")
    print("final inventory:")
    for sku, qty in sorted(store.snapshot().items()):
        print(f"  {sku}: {qty}")


if __name__ == "__main__":
    main()
