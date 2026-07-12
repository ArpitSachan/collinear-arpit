"""Programmatic heuristic check of /app/REPORT.md.

Keyword/structure based:
we only need a coarse, reproducible signal that the agent (a) produced a
written explanation at all and (b) that explanation actually names the
concurrency correctness and idempotency mechanisms involved, rather than
being a generic non-answer. This is graded on the final artifact's text,
not on the agent's trajectory.
"""

from __future__ import annotations

import argparse
import json
import os

MIN_WORDS = 40

# Each group is an "idea" the report should demonstrate awareness of.
# The report gets credit for a group if it contains any one term from it.
CONCEPT_GROUPS = [
    ["lock", "mutex", "synchroniz", "thread-safe", "atomic", "race"],
    ["idempoten", "duplicate", "redeliver", "dedup", "job_id"],
    ["throughput", "latency", "performance", "serializ", "concurren", "parallel"],
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default="/app")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    report_path = os.path.join(args.app, "REPORT.md")

    if not os.path.exists(report_path):
        result = {"score": 0.0, "reason": "REPORT.md not found"}
    else:
        text = open(report_path, encoding="utf-8", errors="replace").read()
        lower = text.lower()
        word_count = len(text.split())

        length_ok = word_count >= MIN_WORDS
        concept_hits = [
            any(term in lower for term in group) for group in CONCEPT_GROUPS
        ]
        concept_score = sum(concept_hits) / len(CONCEPT_GROUPS)

        score = concept_score if length_ok else concept_score * 0.5  # normalize the score if the length is not satisfied.

        result = {
            "score": round(score, 3),
            "word_count": word_count,
            "length_ok": length_ok,
            "concept_hits": dict(zip(["locking", "idempotency", "performance"], concept_hits)),
        }

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"grade_report: {result}")


if __name__ == "__main__":
    main()
