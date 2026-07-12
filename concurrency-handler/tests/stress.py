"""Concurrency stress harness.

Replays a production scale job stream through the candidate's Inventory
using a 96-worker pool (matching the incident's peak autoscaler size),
NUM_TRIALS independent times (fresh store each time). For each trial
records:

  - correct: If the expected data (calculated manually in the code by
             deduplicating job_ids, dropping malformed qty<=0 jobs, and
             decrementing qty) matches the final snapshot of the
             inventory.
  - elapsed_sec: The time the trial took to finish up.

Emits JSON:
{
  "trials": [{"correct": bool, "elapsed_sec": float}, ...],
  "functional_correctness": fraction of trials that were correct,
  "robustness": fraction of trials that finished within TIME_BUDGET_SEC
}

Trial count is deliberately > 1: the redelivery race this task is built
around only manifests when two deliveries of the same job_id happen to
land on different worker threads close enough together in the queue -
a single stress run can pass by chance even on buggy code. Repeated
independent trials are what actually give a trustworthy signal, which is
also what the reward computed from these trials is checking for.

TIME_BUDGET_SEC is calibrated so that a fine-grained, correct fix
(lock scope limited to the in-memory state, downstream I/O outside the
lock) comfortably finishes well under budget, while a "wrap the whole
method in one lock" fix - correct, but serializing ~1000 jobs x 20ms
(DOWNSTREAM_CALL_SEC) of simulated downstream latency - blows it.
"""

import argparse
import json
import sys
import time


TIME_BUDGET_SEC = 5.0
NUM_TRIALS = 8
NUM_WORKERS = 96


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default="/app")
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    sys.path.insert(0, args.app)

    from store.inventory import Inventory
    from store.worker import run_jobs
    from store.queue_processor import load_jobs

    with open(args.inventory) as f:
        initial_qty = json.load(f)

    jobs = list(load_jobs(args.jobs))

    unique = {j["job_id"]: j for j in jobs}
    expected = dict(initial_qty)
    for j in unique.values():
        # Malformed (qty<=0) deliveries must be rejected without side
        # effects - they don't factor into the expected final inventory.
        if j["qty"] > 0:
            expected[j["sku"]] -= j["qty"]

    trials = []
    for _ in range(NUM_TRIALS):
        store = Inventory(initial_qty)
        t0 = time.time()
        try:
            run_jobs(store, jobs, num_workers=NUM_WORKERS)
        except Exception as exc:
            trials.append({"correct": False, "elapsed_sec": time.time() - t0, "error": str(exc)})
            continue
        elapsed = time.time() - t0
        actual = store.snapshot()
        trials.append({"correct": actual == expected, "elapsed_sec": elapsed})

    functional_correctness = sum(1 for t in trials if t["correct"]) / len(trials)
    robustness = sum(1 for t in trials if t["elapsed_sec"] <= TIME_BUDGET_SEC) / len(trials)

    out = {
        "trials": trials,
        "expected_final_inventory": expected,
        "time_budget_sec": TIME_BUDGET_SEC,
        "num_workers": NUM_WORKERS,
        "functional_correctness": functional_correctness,
        "robustness": robustness,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)

    for i, t in enumerate(trials):
        print(f"stress trial {i}: correct={t['correct']} elapsed={t['elapsed_sec']:.2f}s")
    print(f"functional_correctness={functional_correctness} robustness={robustness}")


if __name__ == "__main__":
    main()
