"""Combines the individual grader outputs into /logs/verifier/reward.json.

Weighting (documented in README.md / RUN_REPORT.md):
  functional_correctness  0.40  - stress-trial correctness vs ground truth
  constraint_satisfaction 0.25  - regression suite (0.5) + duplicate-race
                                   idempotency invariant (0.5)
  robustness               0.25  - stress trials finishing within the
                                   throughput budget (penalizes a
                                   correct-but-fully-serialized fix)
  artifact_quality         0.10  - REPORT.md heuristic
"""

import argparse
import json

WEIGHTS = {
    "functional_correctness": 0.40,
    "constraint_satisfaction": 0.25,
    "robustness": 0.25,
    "artifact_quality": 0.10,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--regression-pass", type=int, required=True, help="1 or 0")
    parser.add_argument("--duplicate-race", required=True)
    parser.add_argument("--stress", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    duplicate_race = json.load(open(args.duplicate_race))
    stress = json.load(open(args.stress))
    artifact = json.load(open(args.artifact))

    regression_score = 1.0 if args.regression_pass == 1 else 0.0
    duplicate_score = duplicate_race["score"]
    constraint_satisfaction = 0.5 * regression_score + 0.5 * duplicate_score

    functional_correctness = stress["functional_correctness"]
    robustness = stress["robustness"]
    artifact_quality = artifact["score"]

    overall = (
        WEIGHTS["functional_correctness"] * functional_correctness
        + WEIGHTS["constraint_satisfaction"] * constraint_satisfaction
        + WEIGHTS["robustness"] * robustness
        + WEIGHTS["artifact_quality"] * artifact_quality
    )

    # Harbor's VerifierResult schema requires every top-level value in
    # reward.json to be a plain number - no nested objects. The full
    # breakdown goes in a sibling reward-details.json instead (same
    # convention Harbor's own reward-kit examples use).
    reward = {
        "overall": round(overall, 4),
        "functional_correctness": round(functional_correctness, 4),
        "constraint_satisfaction": round(constraint_satisfaction, 4),
        "robustness": round(robustness, 4),
        "artifact_quality": round(artifact_quality, 4),
    }
    details = {
        "regression_score": regression_score,
        "duplicate_race": duplicate_race,
        "stress": stress,
        "artifact": artifact,
        "weights": WEIGHTS,
    }

    with open(args.out, "w") as f:
        json.dump(reward, f, indent=2)

    details_path = args.out.replace("reward.json", "reward-details.json")
    with open(details_path, "w") as f:
        json.dump(details, f, indent=2)

    print(json.dumps({**reward, "details": details}, indent=2))


if __name__ == "__main__":
    main()
