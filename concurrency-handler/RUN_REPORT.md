# Run Report

> **Current design (v3, 4 bugs, 2 nested):** two concurrency/validation
> bugs (redelivery double-charge, malformed-qty inflation) plus a missing
> `reorder_snapshot()` whose natural first fix (one flat safety-stock
> constant) is wrong for one SKU — fixing that requires revising the
> first fix, not patching around it. **Three independent Claude Opus 4.7
> trials and two independent GPT-5.5 (`codex`) trials have now been run**
> against this exact version. Opus fails the same way every time
> (`overall = 0.875` — bugs 1+2 fixed, bug 3/4 fails verification);
> GPT-5.5 passes cleanly both times (`overall = 1.0`). Full breakdown,
> including the n=2 confirmation for both models, below.

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

## Second-round evidence: n=2 for both target models

Run 2026-07-14, via the real Harbor CLI, `--env-file` pointing at Anthropic-
and OpenAI-shaped credentials backed by a single OpenRouter key (native
Anthropic/OpenAI credits were not available for this round — see
Methodology note below). Four trials, two per model, all against the
unmodified current v3 code:

| Job | Agent / model | `overall` | `functional_correctness` | `constraint_satisfaction` | `robustness` | `artifact_quality` | Steps | Cost | Wall time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `2026-07-14__12-59-43` | claude-code / claude-opus-4-7 | 0.875 | 1.0 | 0.5 | 1.0 | 1.0 | 47 | $2.34 | 11m |
| `2026-07-14__13-12-06` | claude-code / claude-opus-4-7 | 0.875 | 1.0 | 0.5 | 1.0 | 1.0 | 47 | $2.07 | 8m39s |
| `2026-07-14__12-25-04` | codex / gpt-5.5 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 24 | $0.99 | 4m14s |
| `2026-07-14__13-20-54` | codex / gpt-5.5 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 17 | $0.68 | 3m53s |

### Opus 4.7: identical failure, twice, independently

Both new Opus trials reproduce the exact same failure shape as the
original `17-30-20` trial — bugs 1+2 fixed (in both cases with a
per-`job_id` wait/leader mechanism, not a flat lock), bug 3/4 fails on
the identical single assertion, every time:

```
FAILED test_reorder_snapshot_uses_per_sku_safety_stock_floor
assert 20 == 15
```

And the root cause is the same *class* of mistake in both trials —
`reorder_snapshot()` is implemented correctly (real per-SKU lookup, not
the flat-constant trap), but the constructor's safety-floor argument is
optional and defaults to empty, so it's never populated unless a caller
explicitly passes it in:

```python
# job 12-59-43
def __init__(self, initial_qty: dict, safety_floors: dict | None = None):
    self._safety_floors = dict(safety_floors) if safety_floors else {}

# job 13-12-06
def __init__(self, initial_qty: dict, safety_stock: Optional[dict] = None):
    self._safety_stock = dict(safety_stock or {})
```

Different variable names, same structural gap: nothing in either fix
ever reads `ops/safety_stock_floors.json` itself, so the only path that
sees real floors is one where a caller manually supplies them — which is
also how each trial verified its own fix (see the original `17-30-20`
write-up above). Three-for-three now on this exact failure mode is
strong evidence this isn't a fluke.

### GPT-5.5: two clean passes, and it's not a lucky one-shot

Both codex trials pass `1.0` outright, and both close the exact gap Opus
misses by having `Inventory` load the real floors itself when no
override is given, instead of trusting a caller to supply them:

```python
# job 12-25-04 (and the same pattern in 13-20-54)
_SAFETY_STOCK_FLOORS_PATH = Path(__file__).resolve().parents[1] / "ops" / "safety_stock_floors.json"

def __init__(self, initial_qty: dict, safety_stock_floors: dict | None = None):
    self._safety_stock_floors = (
        dict(safety_stock_floors)
        if safety_stock_floors is not None
        else self._load_safety_stock_floors()
    )
```

Neither trial one-shotted it blindly, per the full trajectories:

- **`12-25-04` (24 steps):** read the entire repo and all of `ops/`
  first, diffed `ops/system_of_record_snapshot.json` against
  `data/initial_inventory.json` itself to rule out the stale-snapshot
  theory, wrote its own reproducer, then implemented the fix plus its own
  regression tests. Verified at 1 and 96 workers, then ran a synthetic
  peak-scale throughput check. It then **refactored its own already-passing
  fix** to simplify the duplicate-wait path — that refactor introduced an
  ambiguity between "cached" and "in-flight" states, which it caught
  itself, corrected, and re-verified (full suite + 96-worker replay +
  throughput check again) before writing `REPORT.md`, then ran the test
  suite one final time "so the final status is based on the current
  code."
- **`13-20-54` (17 steps):** same shape at smaller scale — implemented
  the fix, caught a typo in its own ad hoc stress-check script mid-run
  and fixed it before trusting the result, verified at 96 workers, then
  re-ran the test suite once more after writing the report.

That re-verify-after-every-change habit — including changes made to its
own already-passing code — is precisely the discipline missing from the
Opus trials, which verified once, with a hand-fed input, and stopped.

### Methodology note: OpenRouter-routed credentials

This round used a single OpenRouter API key rather than native
Anthropic/OpenAI credentials (native credits weren't available for this
round). `claude-code` was pointed at OpenRouter's Anthropic-compatible
endpoint (`ANTHROPIC_BASE_URL=https://openrouter.ai/api`); `codex`
required a local patch to Harbor's `agents/installed/codex.py` — its
stock `openai_base_url` config write only redirects the reserved
built-in `openai` provider, not third-party routers, so a proper
`[model_providers.openrouter]` block (`wire_api = "responses"`,
`requires_openai_auth = false`) was added to actually route `codex`
through OpenRouter instead of silently falling back to a real,
unauthenticated `api.openai.com` call. Both models are confirmed to be
the real target models (`anthropic/claude-opus-4.7`, `openai/gpt-5.5`)
via OpenRouter's own response metadata, not a substitute model. This is
disclosed here in the interest of full reproducibility — a rerun against
native Anthropic/OpenAI endpoints would be the cleaner methodology if
credits become available.

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
  were both solved cleanly by the target model — v3 now has three
  independent Opus 4.7 trials, all failing the identical bug 3/4 check,
  and two independent GPT-5.5 trials, both passing cleanly — so the n=1
  fluke concern from the first round is resolved.
- The nested bug (3+4) is graded entirely by a deterministic unit test
  (`test_reorder_snapshot_uses_per_sku_safety_stock_floor`), not a
  stress/statistical check — simpler and safer to calibrate under time
  pressure, but it means this particular failure mode doesn't test
  verification-under-uncertainty the way the concurrency bugs do.
- `task.toml`'s `[verifier]`/`[environment]` `network_mode` is `"public"`,
  not `"no-network"` — not yet tightened.
- The second round of trials was run via OpenRouter rather than native
  Anthropic/OpenAI credentials, and required a local Harbor patch to get
  `codex` routing through it correctly (see Methodology note above) — not
  a task-design issue, but worth re-running against native endpoints if
  credits become available, as the cleaner methodology.
