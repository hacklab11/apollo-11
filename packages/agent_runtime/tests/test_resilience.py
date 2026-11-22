from collections.abc import AsyncIterator

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider

from agent_runtime import (
    IDEMPOTENCY_KEY_HEADER,
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
    resilient_client,
)

FAST = RetryPolicy(attempts=3, initial_backoff_s=0, max_backoff_s=0)


def flaky(statuses: list[int], seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(statuses.pop(0) if len(statuses) > 1 else statuses[0])

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_retries_retryable_status_then_succeeds() -> None:
    seen: list[httpx.Request] = []
    async with resilient_client(
        policy=FAST, transport=flaky([503, 502, 200], seen)
    ) as client:
        response = await client.get("http://data.local/metrics")
    assert response.status_code == 200
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_returns_last_response_when_retries_run_out() -> None:
    seen: list[httpx.Request] = []
    async with resilient_client(policy=FAST, transport=flaky([503], seen)) as client:
        response = await client.get("http://data.local/metrics")
    assert response.status_code == 503
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_does_not_retry_client_errors() -> None:
    seen: list[httpx.Request] = []
    async with resilient_client(policy=FAST, transport=flaky([404], seen)) as client:
        response = await client.get("http://data.local/metrics")
    assert response.status_code == 404
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_retries_transport_errors() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200)

    async with resilient_client(
        policy=FAST, transport=httpx.MockTransport(handler)
    ) as client:
        assert (await client.get("http://agent.local/")).status_code == 200
    assert attempts == 2


@pytest.mark.asyncio
async def test_open_breaker_fails_fast_without_calling_host() -> None:
    seen: list[httpx.Request] = []
    single = RetryPolicy(attempts=1)
    client = resilient_client(
        policy=single,
        breaker_factory=lambda: CircuitBreaker(failure_threshold=2),
        transport=flaky([503], seen),
    )
    async with client:
        await client.get("http://down.local/")
        await client.get("http://down.local/")
        with pytest.raises(CircuitOpenError):
            await client.get("http://down.local/")
        assert (await client.get("http://up.local/")).status_code == 503
    assert [r.url.host for r in seen] == ["down.local", "down.local", "up.local"]


def test_breaker_half_opens_after_reset_and_closes_on_success() -> None:
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1, reset_after_s=10, clock=lambda: now[0]
    )
    breaker.record_failure()
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    now[0] = 10
    assert breaker.state == "half_open"
    breaker.before_call()
    breaker.record_success()
    assert breaker.state == "closed"


def test_failed_probe_reopens_breaker() -> None:
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=3, reset_after_s=10, clock=lambda: now[0]
    )
    for _ in range(3):
        breaker.record_failure()
    now[0] = 10
    breaker.record_failure()
    assert breaker.state == "open"


@pytest.mark.asyncio
async def test_injects_traceparent_on_every_request() -> None:
    seen: list[httpx.Request] = []
    tracer = TracerProvider().get_tracer("test")
    async with resilient_client(policy=FAST, transport=flaky([200], seen)) as client:
        with tracer.start_as_current_span("call") as span:
            await client.get("http://agent.local/")
    trace_id = format(span.get_span_context().trace_id, "032x")
    assert seen[0].headers["traceparent"].split("-")[1] == trace_id


@pytest.mark.asyncio
async def test_does_not_retry_non_idempotent_post() -> None:
    seen: list[httpx.Request] = []
    async with resilient_client(
        policy=FAST, transport=flaky([502, 200], seen)
    ) as client:
        response = await client.post("http://selleros.local/refunds", json={"order": 1})
    assert response.status_code == 502
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_retries_post_with_idempotency_key() -> None:
    seen: list[httpx.Request] = []
    async with resilient_client(
        policy=FAST, transport=flaky([502, 200], seen)
    ) as client:
        response = await client.post(
            "http://selleros.local/refunds",
            json={"order": 1},
            headers={IDEMPOTENCY_KEY_HEADER: "run-1:abc"},
        )
    assert response.status_code == 200
    assert [r.content for r in seen] == [b'{"order":1}', b'{"order":1}']


@pytest.mark.asyncio
async def test_does_not_retry_streamed_upload() -> None:
    seen: list[httpx.Request] = []

    async def body() -> AsyncIterator[bytes]:
        yield b"chunk"

    async with resilient_client(
        policy=FAST, transport=flaky([503, 200], seen)
    ) as client:
        response = await client.put("http://data.local/blob", content=body())
    assert response.status_code == 503
    assert len(seen) == 1


def test_half_open_admits_a_single_probe() -> None:
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1, reset_after_s=10, clock=lambda: now[0]
    )
    breaker.record_failure()
    now[0] = 10
    breaker.before_call()
    with pytest.raises(CircuitOpenError, match="probe in flight"):
        breaker.before_call()
    now[0] = 20
    breaker.before_call()
