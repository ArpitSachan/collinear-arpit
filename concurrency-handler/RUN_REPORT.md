# Run Report

> **Current design (v3, 4 bugs, 2 nested):** two concurrency/validation
> bugs (redelivery double-charge, malformed-qty inflation) plus a missing
> `reorder_snapshot()` whose natural first fix (one flat safety-stock
> constant) is wrong for one SKU — fixing that requires revising the
> first fix, not patching around it. **A real Harbor CLI run against
> Claude Opus 4.7 has been completed against this exact version** (job
> `2026-07-12__17-30-20`) and produced a clean, non-infra-confounded
> failure: `overall = 0.875`, with bugs 1+2 fixed correctly (in fact more
> robustly than the oracle) but bug 3/4 failing verification. Full
> breakdown below.

## Why this version exists

Two earlier versions were each run against Claude Opus 4.7 (`claude-code`
harness) and both were solved cleanly:

| Version | Design | Result |
| --- | --- | --- |
| v1 | Single bug: fresh `threading.Lock()` created per call | Job `15-21-30` — full trajectory review: correct hypothesis on the first pass, no backtracking, 28/28 steps clean. |
| v2 | Two bugs: redelivery double-charge (missing re-check) + malformed-qty inflation | Job `16-52-17` — full trajectory review: both root causes correctly diagnosed and correctly stated by step 27/34, before a credit-exhaustion cutoff (not a capability failure — see below). |

Both trajectories showed thorough inspection, correct hypotheses formed
on the first pass, and no revised plan anywhere — consistent evidence
that a small, fully-local, cheap-to-verify repo built around
textbook-recognizable concurrency bugs (double-checked locking is one of
the most famous named antipatterns there is) plays directly to this
model's strengths. v3's bugs 3+4 were added specifically to force
genuine non-linearity rather than more subtlety within the same bug
class — see `README.md`'s Task Idea and Limitations sections.

## v2 target-model evidence (archived — code has since changed)

Job `2026-07-12__16-52-17` (`claude-code` / `claude-opus-4-7`), run
against the *v2* code (bugs 1+2 only, no `reorder_snapshot()` bug 3/4):

```json
{
  "overall": 0.9,
  "functional_correctness": 1.0,
  "constraint_satisfaction": 1.0,
  "robustness": 1.0,
  "artifact_quality": 0.0
}
```

**What happened:** the agent read every relevant file (`INCIDENT.md`,
`README.md`, all of `store/`, `ops/autoscaler_log.txt`,
`ops/system_of_record_snapshot.json`, spotted the malformed jobs itself
via `grep`), wrote its own reproducer, and by step 27 correctly stated
both root causes almost exactly as the oracle would. It fixed both,
confirmed no regression, re-verified at multiple worker counts, and had
just kicked off a more thorough incident-scale stress test in the
background when the session hit a hard `"Credit balance is too low"`
stop (step 34) — before it could check that stress run's result or
write `/app/REPORT.md`. `artifact_quality=0.0` is exactly the missing
report, not a wrong fix; every correctness/constraint/robustness metric
that *did* get graded was already 1.0.

**This is not evidence of a capability failure** — it's an infra/billing
cutoff one step from a clean pass, structurally identical to the v1
timeout-cut case (`15-14-24`, oracle-equivalent fix, same
`artifact_quality=0.0` pattern) except this time the cause is
unambiguous (a literal credit-exhaustion message vs. a config-timeout
question mark).

## Oracle result (current v3 code)

`solution/solve.sh` applied to a clean checkout, then `tests/test.sh`:

```json
{
  "overall": 1.0,
  "functional_correctness": 1.0,
  "constraint_satisfaction": 1.0,
  "robustness": 1.0,
  "artifact_quality": 1.0
}
```

**Result: PASS.** All four bugs fixed: the idempotency re-check, the
`qty<=0` rejection, and `reorder_snapshot()` implemented with a per-SKU
floor lookup (not a flat constant).

Confirmed twice: once via direct `docker build`/`docker run` (above),
and independently via the real Harbor CLI — job `2026-07-12__17-29-58`
(`harbor run -p ./concurrency-handler -a oracle`) reproduced the
identical `overall = 1.0` with no exceptions, on the actual grading
path rather than our local approximation of it.

## Calibration sweep (6 points, confirms no false positive/negative)

| Candidate code | Result |
| --- | --- |
| Untouched buggy seed (bugs 1,2,3 all present) | `reward.json`: `overall=0.25`, `functional_correctness=0.0`, `constraint_satisfaction=0.0`, `robustness=1.0`, `artifact_quality=0.0`. Regression suite: 2 failed (malformed-qty + missing `reorder_snapshot`), 5 passed. |
| Decoy-only fix (qty validated, race still present) | `overall=0.375`, `constraint_satisfaction=0.5` — regression passes, duplicate-race fails 0/30. |
| Race-only fix (idempotency re-check added, qty unvalidated) | `overall=0.375`, `constraint_satisfaction=0.5` — regression fails, duplicate-race passes 30/30. |
| "Wrap the whole method in one lock" (bugs 1+2 fixed, fully serialized) | `overall=0.65`, `functional_correctness=1.0`, `constraint_satisfaction=1.0`, `robustness=0.0` — 8 trials × 96 workers took ~197s vs. the 5.0s budget. |
| Bugs 1+2 fixed, `reorder_snapshot()` with one flat constant (bug-4 trap) | Regression suite: 6/7 pass — only `test_reorder_snapshot_uses_per_sku_safety_stock_floor` fails, isolated to the one SKU (`SKU-EDGE-77`) with a non-default floor. |
| Oracle fix (`solution/solve.sh`) | Clean `1.0` across every metric. |

All six confirm the verifier discriminates correctly — including the new
nested-bug trap, which fails exactly the test it's designed to fail and
nothing else.

## Target-model evidence (Claude Opus 4.7) — v3

Job `2026-07-12__17-30-20` (`claude-code` / `claude-opus-4-7`), run via
the real Harbor CLI against the current v3 code:

```bash
harbor run -p ./concurrency-handler -a claude-code -m claude-opus-4-7 --artifact /app/REPORT.md --artifact /app/store/inventory.py
```

```json
{
  "overall": 0.875,
  "functional_correctness": 1.0,
  "constraint_satisfaction": 0.5,
  "robustness": 1.0,
  "artifact_quality": 1.0
}
```

No exceptions, no timeout, no credit exhaustion — a complete, clean run
(46 steps, $1.84, 8 minutes wall time). This is the first non-infra-
confounded evidence of a real gap in this whole exercise.

### What the agent did

Thorough, well-organized investigation: read the entire repo
(`INCIDENT.md`, `README.md`, all of `store/`), explicitly read all three
`ops/` files on its own initiative (including `safety_stock_floors.json`),
grepped the job data for malformed entries, and wrote its own reproducer
before touching any code. It correctly diagnosed both drift directions
from `INCIDENT.md` and correctly ruled out the stale-snapshot theory by
diffing `ops/system_of_record_snapshot.json` against
`data/initial_inventory.json` itself.

**Bugs 1+2: fixed, and better than the oracle.** Instead of a simple
re-check, it implemented a per-`job_id` placeholder `threading.Event`:
the first delivery does the work; redeliveries wait on that job's own
event without touching the shared ledger lock at all. It then wrote a
*third* script, `bench_throughput.py`, unprompted, to independently
confirm no serialization regression (0.46s measured vs. a 0.42s
ideal-parallel floor). `duplicate_race`: 30/30 correctly deduped.
`functional_correctness`/`robustness` on the hidden stress fixture: 1.0/1.0.

**Bugs 3/4: functionally correct mechanism, incomplete fix.** It
implemented `reorder_snapshot()` with genuine per-SKU lookup —
`self._floors.get(sku, 0)` — correctly avoiding the flat-constant trap
we designed entirely. But it made `safety_stock_floors` an *optional
constructor parameter* defaulting to empty:

```python
def __init__(self, initial_qty: dict, safety_stock_floors: dict | None = None):
    ...
    self._floors = dict(safety_stock_floors) if safety_stock_floors else {}
```

It never wired the real values from `ops/safety_stock_floors.json` in
as an actual default anywhere in the codebase — so any caller that
constructs `Inventory(initial_qty)` the normal way (exactly what the
existing test suite, and the verifier's regression suite, both do) gets
empty floors and `reorder_snapshot()` silently returns raw physical
stock.

### The specific failure: verification that confirmed a belief instead of testing the deployed behavior

At step 39/46, the agent explicitly checked its own work:

```python
with open('ops/safety_stock_floors.json') as f:
    floors = json.load(f)
store = Inventory(inv, safety_stock_floors=floors)
print('reorder_snapshot:', store.reorder_snapshot())
# -> correct numbers, e.g. SKU-EDGE-77: 49 - 12 = 37
```

This passed — because it *manually supplied* the missing piece itself.
It never tested (or seems to have considered) the default,
no-second-argument construction path — the one every other caller in
the repo, and the verifier's regression suite, actually uses. Its final
summary and 1158-word `REPORT.md` state the `reorder_snapshot()`
requirement as done, with no caveat about the parameter being optional
or unwired. The regression suite catches it directly:

```
FAILED test_reorder_snapshot_uses_per_sku_safety_stock_floor
assert reorder["SKU-ANCHOR-01"] == 15
E   assert 20 == 15
```

**Failure mode, per the assignment's taxonomy: skipped verification**,
with a secondary element of **incomplete inspection** (never checked
whether other callers or the default constructor path needed to carry
the real floor values). It is *not* report hallucination in the literal
sense — the report's claim is true as far as it was tested, it's just
that what was tested (a hand-picked example that already assumed the
answer) wasn't representative of the actual deployed/default behavior.
That gap between "I verified it works" and "I verified it works when I
remember to supply the one piece of config nothing else in the system
provides" is exactly the kind of substantive, evidence-backed gap this
assignment asks for — not a formatting artifact, not an environment
trick, and not infra-confounded.

### Why this is credible evidence, not a lucky trap

- The task remains solvable: the oracle passes cleanly, confirmed via
  the real Harbor CLI (see Oracle result above).
- The bug the agent missed is not the one we spent the most design
  effort trying to obscure (bugs 1+2, the well-known concurrency
  patterns) — it solved those cleanly, and better than our own oracle.
  It failed on the newer, structurally different bug class (bugs 3+4),
  consistent with the hypothesis from earlier in this process that more
  subtlety within the concurrency-bug class wasn't going to move the
  needle, but a genuinely different failure shape might.
- The failure is graded by a plain, non-brittle unit test assertion
  (`20 == 15`), not a timing-sensitive or statistical check — no
  ambiguity about whether this is a false positive.

## Provenance verification performed

Checked via web search and direct inspection before finalizing this
design: no matching scenario in SWE-bench/Terminal-Bench-style public
benchmarks; no overlapping content in Harbor's own public
`examples/tasks/` (checked directly against
`harbor-framework/harbor`'s GitHub contents — all generic infra demos,
nothing resembling this domain); the one topically-adjacent public
article found (a Medium post on checkout race conditions) is a
high-level system-design piece with no code and no shared naming, not a
source this task drew from. Not independently verifiable: overlap with
any *private* Collinear task history, which this process has no access
to.

## Limitations

- Two prior versions of this task (see "Why this version exists" above)
  were both solved cleanly by the target model — v3's real trial (above)
  did produce a genuine failure, but this is n=1; a second independent
  trial would strengthen confidence that this isn't a fluke.
- The nested bug (3+4) is graded entirely by a deterministic unit test
  (`test_reorder_snapshot_uses_per_sku_safety_stock_floor`), not a
  stress/statistical check — simpler and safer to calibrate under time
  pressure, but it means this particular failure mode doesn't test
  verification-under-uncertainty the way the concurrency bugs do.
- `task.toml`'s `[verifier]`/`[environment]` `network_mode` is `"public"`,
  not `"no-network"` — not yet tightened.
- `.env` (an unused, unreferenced API key) has not yet been removed from
  the task directory.
