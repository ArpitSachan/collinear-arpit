"""Duplicate delivery race check.

Fires two threads at (as close to) the same instant as possible, both
delivering the *same* job_id, and checks the SKU was only decremented
once. Using a threading.Barrier to force a simultaneous start makes this
race trigger reliably (on the unfixed code, every trial fails) instead of
depending on incidental timing, so the result isn't flaky in either
direction.

Emits JSON: {"score": fraction_of_trials_correctly_idempotent, "trials": N,
"failures": n_failed}.
"""

import argparse
import json
import sys
import threading


def run_trial(Inventory, qty: int) -> bool:
    """Returns True if the duplicate delivery was correctly deduped."""
    from store.inventory import Inventory  # inline import so that onlyn gets imported when the system path is set.
    store = Inventory({"WIDGET": 1_000_000})
    barrier = threading.Barrier(2)

    def deliver():
        barrier.wait()
        store.reserve(job_id="dup-race-job", sku="WIDGET", qty=qty)

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    remaining = store.snapshot()["WIDGET"]
    return remaining == 1_000_000 - qty


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default="/app")
    parser.add_argument("--trials", type=int, default=30)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    sys.path.insert(0, args.app)
    from store.inventory import Inventory  # inline import so that onlyn gets imported when the system path is set.

    failures = 0
    for _ in range(args.trials):
        ok = run_trial(Inventory, qty=5)
        if not ok:
            failures += 1

    score = (args.trials - failures) / args.trials
    with open(args.out, "w") as f:
        json.dump({"score": score, "trials": args.trials, "failures": failures}, f)
    print(f"duplicate_race: {args.trials - failures}/{args.trials} trials correctly deduped")


if __name__ == "__main__":
    main()
