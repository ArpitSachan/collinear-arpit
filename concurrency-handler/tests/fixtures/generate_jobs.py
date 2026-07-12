import json
import random


def gen_job_set(
    skus,
    jobs_per_sku,
    dup_rate,
    seed,
    qty_choices=(1, 2, 3, 4),
    malformed_rate=0.0,
    malformed_qty_choices=(-3, -2, -1),
):
    rng = random.Random(seed)
    unique_jobs = []
    counter = 0
    for sku in skus:
        for _ in range(jobs_per_sku):
            counter += 1
            job_id = f"job-{sku}-{counter:05d}"
            qty = rng.choice(qty_choices)
            unique_jobs.append({"job_id": job_id, "sku": sku, "qty": qty})

    # Build delivery stream: start from unique jobs in a shuffled order,
    # then splice in duplicate (redelivered) entries at random positions.
    stream = list(unique_jobs)
    rng.shuffle(stream)

    num_dups = int(len(unique_jobs) * dup_rate)
    for _ in range(num_dups):
        src = rng.choice(unique_jobs)
        insert_at = rng.randrange(len(stream) + 1)
        stream.insert(insert_at, dict(src))

    # Malformed deliveries: qty <= 0 requests mixed onto the same queue
    # (e.g. a mis-routed cancellation event). These get distinct job_ids
    # (never redelivered, never colliding with a real job_id) and are
    # NOT part of `unique_jobs` - a well-behaved service must reject them
    # (approved=False, no inventory change), so they don't factor into
    # ground truth at all.
    num_malformed = int(len(unique_jobs) * malformed_rate)
    malformed_jobs = []
    for i in range(num_malformed):
        sku = rng.choice(skus)
        qty = rng.choice(malformed_qty_choices)
        job_id = f"job-{sku}-malformed-{i:05d}"
        job = {"job_id": job_id, "sku": sku, "qty": qty}
        malformed_jobs.append(job)
        insert_at = rng.randrange(len(stream) + 1)
        stream.insert(insert_at, job)

    # Ground-truth headroom: enough stock that every well-formed unique
    # job is satisfiable regardless of processing order (keeps grading
    # order-independent). Malformed (qty<=0) jobs never consume headroom.
    totals = {}
    for j in unique_jobs:
        totals[j["sku"]] = totals.get(j["sku"], 0) + j["qty"]

    # Headroom keeps every ordering of unique jobs satisfiable (partial sums
    # of positive quantities are monotonic, so if total <= initial stock,
    # no prefix can ever exceed it) -> approval outcome is order-independent,
    # which keeps stress-test grading deterministic regardless of thread
    # scheduling.
    headroom_factor = 1.15
    initial_inventory = {
        sku: int(totals[sku] * headroom_factor) for sku in skus
    }

    return initial_inventory, unique_jobs, malformed_jobs, stream


def write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    skus = ["SKU-ANCHOR-01", "SKU-BOLT-14", "SKU-CABLE-9", "SKU-DRUM-3", "SKU-EDGE-77"]

    # Small sample the agent gets locally, for their own investigation.
    # malformed_rate=0.08 gives the decoy bug (missing qty<=0 validation)
    # visible, reproducible impact even in the small local sample.
    demo_inv, demo_unique, demo_malformed, demo_stream = gen_job_set(
        skus, jobs_per_sku=16, dup_rate=0.12, malformed_rate=0.08, seed=7
    )
    write_jsonl("jobs_seed.jsonl", demo_stream)
    with open("initial_inventory_demo.json", "w") as f:
        json.dump(demo_inv, f, indent=2)
    print(
        "demo: unique=%d malformed=%d stream=%d inv=%s"
        % (len(demo_unique), len(demo_malformed), len(demo_stream), demo_inv)
    )

    # Larger, higher-concurrency set used only by the verifier's stress
    # test. Scaled to match the peak worker count in ops/autoscaler_log.txt
    # (96 workers at the incident's peak).
    stress_inv, stress_unique, stress_malformed, stress_stream = gen_job_set(
        skus, jobs_per_sku=200, dup_rate=0.10, malformed_rate=0.05, seed=1234
    )
    write_jsonl("jobs_stress.jsonl", stress_stream)
    with open("initial_inventory_stress.json", "w") as f:
        json.dump(stress_inv, f, indent=2)
    print(
        "stress: unique=%d malformed=%d stream=%d inv=%s"
        % (len(stress_unique), len(stress_malformed), len(stress_stream), stress_inv)
    )
