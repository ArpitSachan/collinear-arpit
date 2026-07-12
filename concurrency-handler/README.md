# collinear-arpit/concurrency-handler

---

## Task idea

Stock reservation service processes `ReserveStock` jobs off an
at-least-once delivery queue using a pool of worker threads.
`Inventory` has **three independent bugs, one of which nests a fourth**
— deliberately, so that "found a bug, tests pass, done" isn't sufficient,
and so that fixing everything correctly requires revising an earlier fix
rather than only ever adding new, independent patches:

1. **Redelivery double-charge** (needs concurrency to manifest): `reserve()`
  correctly uses a single instance-level `threading.Lock()` — not the
   classic "new `Lock()` per call" antipattern — and correctly splits its
   critical section into two `with self._lock:` blocks so the lock isn't
   held across the simulated downstream I/O call (a legitimate throughput
   optimization). But the second block never re-checks the idempotency
   cache before decrementing stock, so two truly-concurrent deliveries of
   the *same* `job_id` (a normal at-least-once redelivery racing its own
   earlier attempt) can both pass the pre-sleep check and both decrement
   the ledger. Every lock you can see is genuinely doing its job; the bug
   is a missing re-check, not a missing lock.
2. **Malformed-quantity inflation** (no concurrency required): `reserve()`
  never validates `qty`. A `qty <= 0` delivery (documented in
   `README.md`'s request contract as a possible mis-routed cancellation
   event) passes `available >= qty` trivially, and for negative `qty`
   specifically, *increases* stock via `available - qty`. This is a plain
   correctness/validation gap, unrelated to concurrency.

3. **Missing `reorder_snapshot()`, then a nested trap** (no concurrency
   required): `Inventory` never exposes `reorder_snapshot()` at all —
   `README.md`'s contract requires it to report *sellable* stock
   (physical minus a per-SKU safety-stock floor) for the automated
   reordering system, so without it the reordering system would be
   reading raw physical stock via `snapshot()` instead. The floors
   (`ops/safety_stock_floors.json`) are **not** uniform — most SKUs are
   5, `SKU-EDGE-77` is 12. The natural first fix (subtract one flat
   constant) is correct for four of five SKUs and silently wrong for
   `SKU-EDGE-77` — fixing that requires *replacing* the flat-constant
   fix with a per-SKU lookup, not adding another independent check on
   top. This is deliberately a nested bug: it's invisible until bug 3 is
   "fixed," and fixing it for real means revising that fix's own
   implementation, not patching around it.

All of these survive the existing green test suite (`tests/test_inventory.py`)
because it only exercises sequential call sequences and has no
`qty <= 0` or `reorder_snapshot()` coverage. `INCIDENT.md` reports both directions of drift (stock
ending up lower than expected on some SKUs, higher on others) and is
deliberately honest that ops' single-worker replay reproduced *one*
anomaly but not the other — a real clue that there are two separate
causes, not one fix that explains everything. `instruction.md` does not
tell the agent what scale to test at; `ops/autoscaler_log.txt` (the
incident's real peak worker count) and `ops/system_of_record_snapshot.json`
(to rule out a live "stale snapshot" red herring ops floated) are there to
be found by inspection, not handed over.

---



## Long-horizon structure

Solving this requires several linked steps, not one command:


| Step             | Action                                                                                                                                                                              |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. **Inspect**   | the service, `INCIDENT.md`, `README.md`'s request contract, and `ops/` — form hypotheses for *both* directions of drift, not just one.                                              |
| 2. **Reproduce** | each anomaly separately; notice one reproduces at 1 worker and one doesn't — that split is the signal that two root causes are in play.                                             |
| 3. **Rule out**  | the stale-snapshot theory against `ops/system_of_record_snapshot.json` rather than taking it on faith.                                                                              |
| 4. **Diagnose**  | both actual root causes — a correctly-instantiated, correctly-scoped lock that's still missing one re-check is easy to skim past as "this already looks synchronized."              |
| 5. **Fix**       | both, keeping the lock's critical section narrow enough to preserve throughput under the simulated downstream I/O delay.                                                            |
| 6. **Verify**    | at the scale the service actually runs at (`ops/autoscaler_log.txt`'s peak, not the bundled small sample), across multiple independent runs, without regressing the existing suite. |
| 7. **Implement** | `reorder_snapshot()` per `README.md`'s contract; the first reasonable attempt (one flat safety-stock constant) is wrong for one SKU. |
| 8. **Revise**    | that same implementation — not add a new check on top — once `ops/safety_stock_floors.json` reveals floors aren't uniform. |
| 9. **Report**    | all root causes and verification evidence in `REPORT.md`.                                                                                                                          |


---



## Fairness audit


| Criterion        | Assessment                                                                                                                                                                                                                                                                                                                                                                                               |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Solvable**     | Both bugs are well-defined and independently fixable; the oracle (`solution/solve.sh`) fixes both in one file and passes cleanly. Everything needed is in the provided files — see `instruction.md`.                                                                                                                                                                                                     |
| **Unambiguous**  | `instruction.md` states the exact deliverable (fixed `store/`, plus `/app/REPORT.md`), the constraints (no regression, no serialize-everything shortcut, no network), and points to `ops/` for the real-world scale signal — it just doesn't hand over the number directly.                                                                                                                              |
| **Substantive**  | Failure here reflects real capability gaps: recognizing a lock that's *almost* correct except for one missing re-check, not conflating co-occurring bugs into one fix, verifying against production scale instead of the bundled sample, and — for the nested bug — noticing a first fix is only 80% right and revising it instead of declaring victory once tests go green.                                                                                                                                                         |
| **Reproducible** | Pinned base image (`ubuntu:24.04` + `pytest==8.4.1`), no secrets, no external services required at runtime. Verified with a from-scratch `docker build` + container runs (see Reproduction below).                                                                                                                                                                                                       |
| **Non-brittle**  | The verifier never does string/format matching. It black-box imports the candidate's `store` package and checks behavior: exact-match final inventory against an independently precomputed ground truth, a deterministic barrier-synchronized duplicate-delivery probe, wall-clock throughput, and the *verifier's own pristine copy* of the regression suite (never the agent's copy in `/app/tests/`). |


---



## Verifier design

`tests/test.sh` runs four independent checks and writes
`/logs/verifier/reward.json` with `overall`, `functional_correctness`,
`constraint_satisfaction`, `robustness`, and `artifact_quality`. Exact
weighting and logic are in `tests/aggregate_reward.py`; summary:

### Metrics


| Metric                    | What it measures                                                                                                                                                                                                                                                                         | Weight |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| `functional_correctness`  | Fraction of 8 independent 96-worker stress runs (1000 unique jobs + ~10% redelivered duplicates + ~5% malformed `qty<=0` deliveries, `tests/fixtures/jobs_stress.jsonl`, hidden from the agent) whose final per-SKU inventory exactly matches an independently precomputed ground truth. | 0.40   |
| `constraint_satisfaction` | 0.5 × (verifier's own regression suite — including the `qty<=0` rejection test and the per-SKU `reorder_snapshot()` test — still passes) + 0.5 × (fraction of 30 barrier-synchronized duplicate-delivery trials correctly deduped).                                                                                                  | 0.25   |
| `robustness`              | Fraction of the 8 stress runs completing within a 5s budget.                                                                                                                                                                                                                             | 0.25   |
| `artifact_quality`        | Programmatic (non-LLM-judge) heuristic on `/app/REPORT.md`: length gate + keyword coverage across locking / idempotency / performance concepts.                                                                                                                                          | 0.10   |


`constraint_satisfaction` is deliberately split 50/50 between the
regression suite (which now covers the malformed-qty contract) and the
duplicate-race probe (which isolates the redelivery bug) — so a fix that
addresses only one of the two bugs caps at 0.5 there, and *which* half it
loses tells you which bug is still open. `functional_correctness` at full
production scale requires both bugs fixed; either one alone deterministically
breaks exact-match on every trial (see Calibration), so it doesn't carry
graded statistical nuance on its own — the multi-trial signal is really in
`constraint_satisfaction` (a single lucky small-scale run can look clean
if you only test one failure mode) and in the fact that 1 of the 2 bugs
requires real concurrency to notice at all.

### Calibration

Measured locally (see Reproduction below) to confirm the verifier isn't a
false positive/negative across six distinct code states:


| Candidate code                                                              | overall | functional_correctness | constraint_satisfaction | robustness | artifact_quality |
| --------------------------------------------------------------------------- | ------- | ---------------------- | ----------------------- | ---------- | ---------------- |
| Untouched buggy seed (both bugs present)                                    | 0.25    | 0.0                    | 0.0                     | 1.0        | 0.0              |
| Decoy-only fix (qty validated, redelivery race still present)               | 0.375   | 0.0                    | 0.5                     | 1.0        | 0.0              |
| Race-only fix (idempotency re-check added, qty still unvalidated)           | 0.375   | 0.0                    | 0.5                     | 1.0        | 0.0              |
| "Wrap the whole method in one lock" (both bugs fixed, but fully serialized) | 0.65    | 1.0                    | 1.0                     | 0.0        | 0.0              |
| Bugs 1+2 fixed, `reorder_snapshot()` added with one flat constant (bug-4 trap) | n/a\*   | —                      | —                       | —          | —                |
| Oracle fix (`solution/solve.sh`)                                            | 1.0     | 1.0                    | 1.0                     | 1.0        | 1.0              |

\* Not run through the full `reward.json` — measured directly against
the verifier's regression suite to isolate the trap: it fails exactly
one test (`test_reorder_snapshot_uses_per_sku_safety_stock_floor`, on
exactly the one SKU with a non-default floor), 6/7 tests otherwise
passing, with both concurrency bugs (already fixed in this variant)
unaffected.


The two partial-fix rows land on the same `overall` (0.375) for different
reasons — `constraint_satisfaction`'s breakdown (visible in
`reward-details.json`: `regression_score` vs. `duplicate_race.score`)
is what actually tells them apart, confirming the verifier attributes
credit to the correct sub-bug rather than just collapsing to "not done."
The naive-coarse-lock row confirms `robustness` still penalizes the
"wrap everything in one lock" shortcut once both correctness bugs are
fixed — it took ~197s across 8 trials at 96 workers, vs. the 5.0s budget.

The stress dataset (`tests/fixtures/jobs_stress.jsonl`,
`initial_inventory_stress.json`) is larger and separately seeded from the
small sample the agent sees in `data/` (`tests/fixtures/generate_jobs.py`
documents exactly how both were generated, including the malformed-job
injection), so a solution can't special-case the visible sample. Per
`tests/`, the shipped inventory always has enough headroom that every
well-formed unique job is satisfiable regardless of processing order —
partial sums of positive quantities are monotonic, so if total demand ≤
starting stock, no prefix can ever exceed it. This keeps the correctness
ground truth independent of thread-scheduling order.

---



## Provenance

Entirely original for this exercise: the service code (`store/`), all
four bugs, the incident report narrative (including the red herring, the
autoscaler-log/system-of-record/safety-stock-floor seed artifacts), the
job-generation logic, and the verifier are all written from scratch —
this is not a port of any existing benchmark, CTF, tutorial, or prior
Collinear task. Verified by web search against SWE-bench/Terminal-Bench
and Harbor's own public `examples/tasks/` — no overlapping scenario
found; see `RUN_REPORT.md` for the specific checks run.
No external libraries or datasets are used beyond the Python standard
library and `pytest` (referenced only by name/version, not vendored).
Harbor's directory/`task.toml` conventions were taken from the public
[Harbor documentation](https://www.harborframework.com/docs) (structure
only — no task content was copied).

---



## Reproduction

```bash
# Build the environment image
docker build -t concurrency-handler-task -f environment/Dockerfile environment/

# Oracle run (should score reward.json overall = 1.0)
docker run --rm -v "$PWD/tests:/tests:ro" -v "$PWD/solution:/solution:ro" \
  concurrency-handler-task \
  bash -c "mkdir -p /logs/verifier && bash /solution/solve.sh && bash /tests/test.sh && cat /logs/verifier/reward.json"

# Untouched-buggy-code run (should score reward.json overall = 0.25)
docker run --rm -v "$PWD/tests:/tests:ro" \
  concurrency-handler-task \
  bash -c "mkdir -p /logs/verifier && bash /tests/test.sh && cat /logs/verifier/reward.json"
```

Or, with the Harbor CLI:

```bash
harbor run -p ./concurrency-handler -a oracle --artifact /app/REPORT.md --artifact /app/store/inventory.py
harbor run -p ./concurrency-handler -a <agent> -m <gpt-5.5-high-or-opus-4.7> --artifact /app/REPORT.md --artifact /app/store/inventory.py
harbor view ./jobs
```

See `RUN_REPORT.md` for full run logs, the target-model evidence, and the
failure analysis.

---



## Limitations

- The `robustness` time budget (5.0s) was calibrated against one
development machine at 8 trials × 96 workers (see measurements above);
it has wide margin in both directions locally, but hasn't been
validated across materially different hardware/CI runners.
- `DOWNSTREAM_CALL_SEC` (0.02s) is a fixed constant chosen to make the
race reliably reproducible and the throughput gap unmistakable; it's a
simulated I/O delay, not a realistic network call.
- At the stress fixture's current scale (~100 duplicate pairs across
~1150 deliveries), either bug alone breaks *every* stress trial, not
just some — the redelivery race turned out to be far more reliably
triggered at this scale than originally assumed. The multi-trial
design still adds value (a single small-scale check can miss it), but
the primary difficulty driver is testing at the right scale at all and
correctly separating two co-occurring bugs, not statistical luck across
repeated runs of the same scale.
- The `artifact_quality` check is a keyword/length heuristic, not a
semantic judge, by design (the assignment asks to avoid LLM-as-judge
over trajectories); it can be gamed by stuffing the right keywords into
a low-quality report. It's weighted low (0.10) for exactly this reason.
- `task.toml`'s `[verifier]`/`[environment]` `network_mode` is `"public"`,
not `"no-network"` — flagged in an earlier pass as worth tightening for
determinism, not yet changed.
- **Honest self-critique from earlier iterations of this task:** two prior
versions (a single fresh-`Lock()`-per-call bug, then bugs 1+2 above on
their own) were both solved cleanly by Claude Opus 4.7 in full trajectory
review — correct hypothesis on the first pass, no backtracking, no
revised plan, in both cases. That's real evidence those versions leaned
on well-known, textbook-recognizable bug patterns (double-checked
locking is one of the most famous named concurrency antipatterns there
is) inside an environment shape (small, fully-local, cheap-to-verify
repo) that plays directly to a strong coding agent's strengths. Bugs 3+4
(this version) were added specifically to force genuine non-linearity —
a first fix that looks complete until a later discovery requires
revising it, not just patching around it — rather than adding more
subtlety to the same bug class. Whether that's enough is still an open,
empirical question; see `RUN_REPORT.md` for what's actually been
observed against the target model.

