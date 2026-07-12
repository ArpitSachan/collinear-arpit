from dataclasses import dataclass


@dataclass(frozen=True)
class ReserveResult:
    job_id: str
    sku: str
    qty: int
    approved: bool
