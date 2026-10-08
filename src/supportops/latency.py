import math
import secrets
import time
from collections import Counter
from collections.abc import Mapping, Sequence

import httpx
from pydantic import BaseModel

from supportops.http_checks import LOCALHOST_NOTE, Clock, send

MAX_REQUESTS = 100
SLOW_FIRST_REQUEST_MS = 500


class LatencySample(BaseModel):
    request_id: str
    status: int | None
    duration_ms: float
    failure: str | None = None


class LatencyReport(BaseModel):
    method: str
    url: str
    requested: int
    threshold_ms: float
    successful: int
    failed: int
    status_counts: dict[str, int]
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None
    threshold_exceeded: bool
    slowest: list[LatencySample]
    notes: list[str]
    samples: list[LatencySample]

    @property
    def ok(self) -> bool:
        return self.failed == 0 and not self.threshold_exceeded


def percentile(values: Sequence[float], percent: float) -> float:
    if not values:
        raise ValueError("percentile of an empty sequence")
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return ordered[rank - 1]


def measure_latency(
    client: httpx.Client,
    path: str,
    count: int,
    *,
    threshold_ms: float,
    headers: Mapping[str, str] | None = None,
    clock: Clock = time.perf_counter,
) -> LatencyReport:
    if not 1 <= count <= MAX_REQUESTS:
        raise ValueError(f"count must be between 1 and {MAX_REQUESTS}")
    run = secrets.token_hex(3)
    samples = []
    url = path
    for number in range(1, count + 1):
        result = send(
            client,
            "GET",
            path,
            headers=headers,
            request_id=f"supportops-latency-{run}-{number:03d}",
            clock=clock,
        )
        url = result.url
        samples.append(
            LatencySample(
                request_id=result.request_id,
                status=result.status,
                duration_ms=result.duration_ms,
                failure=result.failure.category if result.failure else None,
            )
        )
    successful = [sample for sample in samples if _is_success(sample)]
    durations = [sample.duration_ms for sample in successful]
    p95 = percentile(durations, 95) if durations else None
    return LatencyReport(
        method="GET",
        url=url,
        requested=count,
        threshold_ms=threshold_ms,
        successful=len(successful),
        failed=count - len(successful),
        status_counts=dict(Counter(_outcome(sample) for sample in samples)),
        p50_ms=percentile(durations, 50) if durations else None,
        p95_ms=p95,
        max_ms=max(durations) if durations else None,
        threshold_exceeded=p95 is not None and p95 > threshold_ms,
        slowest=sorted(successful, key=lambda sample: sample.duration_ms, reverse=True)[:3],
        notes=_connection_setup_notes(samples, url),
        samples=samples,
    )


def _connection_setup_notes(samples: list[LatencySample], url: str) -> list[str]:
    first, rest = samples[0], [sample for sample in samples[1:] if _is_success(sample)]
    if not _is_success(first) or len(rest) < 2:
        return []
    typical = percentile([sample.duration_ms for sample in rest], 50)
    if first.duration_ms < max(SLOW_FIRST_REQUEST_MS, 5 * typical):
        return []
    notes = [
        f"The first request took {first.duration_ms:.0f} ms, while the others took about "
        f"{typical:.0f} ms. Only the first request had to open the connection, so the extra "
        "time is probably spent connecting (name resolution or TCP setup), not inside the API."
    ]
    if httpx.URL(url).host == "localhost":
        notes.append(f"The URL uses 'localhost'. {LOCALHOST_NOTE}")
    return notes


def _is_success(sample: LatencySample) -> bool:
    return sample.status is not None and 200 <= sample.status < 300


def _outcome(sample: LatencySample) -> str:
    return str(sample.status) if sample.status is not None else str(sample.failure)
