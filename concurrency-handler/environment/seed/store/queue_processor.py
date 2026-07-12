"""Loads reservation jobs from the queue's JSONL export.

The upstream queue guarantees at-least-once delivery: if a worker doesn't
ack a job within its visibility timeout, the queue redelivers it under the
same job_id. The JSONL files in data/ already contain a realistic
mix of redelivered (duplicate job_id) entries to reproduce that.
"""

import json
from typing import Iterator


def load_jobs(path: str) -> Iterator[dict]:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)
