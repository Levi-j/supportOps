from collections.abc import Callable

import httpx
import pytest

from supportops.http_checks import create_client
from supportops.latency import MAX_REQUESTS, measure_latency, percentile
from supportops.settings import Settings
from tests.unit.fake_api import FakeApi, respond


def stepping_clock(durations_ms: list[float]) -> Callable[[], float]:
    ticks: list[float] = []
    now = 0.0
    for duration in durations_ms:
        ticks += [now, now + duration / 1000]
        now += 1.0
    iterator = iter(ticks)
    return lambda: next(iterator)


def statuses(*codes: int) -> FakeApi:
    remaining = list(codes)

    def responder(request: httpx.Request) -> httpx.Response:
        return respond(remaining.pop(0), {"data": []})(request)

    return FakeApi().on("GET", "/v1/invoices", responder)


def client(api: FakeApi, url: str = "http://127.0.0.1:8001") -> httpx.Client:
    return create_client(Settings(api_url=url), transport=api.transport)


def test_nearest_rank_percentiles() -> None:
    values = [float(value) for value in range(1, 21)]

    assert percentile(values, 50) == 10
    assert percentile(values, 95) == 19
    assert percentile(values, 100) == 20
    assert percentile([7.0], 95) == 7


def test_statistics_are_deterministic_with_an_injected_clock() -> None:
    durations = [10.0, 30.0, 20.0, 40.0, 50.0]
    api = statuses(200, 200, 200, 200, 200)

    report = measure_latency(
        client(api), "/v1/invoices", 5, threshold_ms=1000, clock=stepping_clock(durations)
    )

    assert report.successful == 5
    assert report.failed == 0
    assert report.p50_ms == 30
    assert report.p95_ms == 50
    assert report.max_ms == 50
    assert [sample.duration_ms for sample in report.slowest] == [50, 40, 30]
    assert report.status_counts == {"200": 5}
    assert report.ok
    assert api.methods == ["GET"] * 5
    assert len({request.headers["X-Request-Id"] for request in api.requests}) == 5


def test_failed_responses_are_counted_but_excluded_from_statistics() -> None:
    api = statuses(200, 503, 200, 500)

    report = measure_latency(
        client(api), "/v1/invoices", 4, threshold_ms=1000, clock=stepping_clock([10, 1, 30, 2])
    )

    assert report.successful == 2
    assert report.failed == 2
    assert report.status_counts == {"200": 2, "503": 1, "500": 1}
    assert report.p50_ms == 10
    assert report.max_ms == 30
    assert not report.ok


def test_no_successful_responses_means_no_statistics() -> None:
    api = FakeApi().on("GET", "/v1/invoices", httpx.ConnectTimeout("timed out"))

    report = measure_latency(client(api), "/v1/invoices", 3, threshold_ms=1000)

    assert report.successful == 0
    assert report.p50_ms is None
    assert report.p95_ms is None
    assert report.status_counts == {"connect_timeout": 3}
    assert not report.ok


def test_p95_above_the_threshold_is_flagged() -> None:
    api = statuses(*[200] * 20)

    report = measure_latency(
        client(api),
        "/v1/invoices",
        20,
        threshold_ms=100,
        clock=stepping_clock([50.0] * 18 + [150.0, 200.0]),
    )

    assert report.p95_ms == 150
    assert report.threshold_exceeded
    assert not report.ok


@pytest.mark.parametrize("count", [0, MAX_REQUESTS + 1])
def test_count_is_limited(count: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        measure_latency(client(FakeApi()), "/v1/invoices", count, threshold_ms=1000)


def test_a_slow_first_request_is_explained_as_connection_setup() -> None:
    api = statuses(200, 200, 200, 200)

    report = measure_latency(
        client(api, "http://localhost:8001"),
        "/v1/invoices",
        4,
        threshold_ms=5000,
        clock=stepping_clock([2050.0, 9.0, 10.0, 11.0]),
    )

    assert len(report.notes) == 2
    assert "Only the first request had to open the connection" in report.notes[0]
    assert "127.0.0.1" in report.notes[1]


def test_no_connection_note_when_requests_are_even() -> None:
    api = statuses(200, 200, 200)

    report = measure_latency(
        client(api), "/v1/invoices", 3, threshold_ms=5000, clock=stepping_clock([12, 10, 11])
    )

    assert report.notes == []
