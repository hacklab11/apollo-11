"""Timeouts, retries with full-jitter backoff and per-host circuit breakers for outbound HTTP."""

import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from agent_runtime.errors import CircuitOpenError
from agent_runtime.tracing import inject_trace_context

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempts: int = Field(default=3, ge=1)
    initial_backoff_s: float = Field(default=0.2, ge=0)
    max_backoff_s: float = Field(default=5.0, ge=0)
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})
    idempotent_methods: frozenset[str] = frozenset(
        {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
    )

    def allows_retry(self, request: httpx.Request) -> bool:
        """Only retry what is safe to send twice and whose body can be sent again."""
        replayable = isinstance(request.stream, httpx.ByteStream)
        idempotent = (
            request.method in self.idempotent_methods
            or IDEMPOTENCY_KEY_HEADER in request.headers
        )
        return replayable and idempotent


class CircuitBreaker:
    """Opens after `failure_threshold` consecutive failures; lets one probe through per `reset_after_s`."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_after_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_after_s = reset_after_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.reset_after_s:
            return "half_open"
        return "open"

    def before_call(self) -> None:
        state = self.state
        if state == "open":
            raise CircuitOpenError("circuit open")
        if state == "half_open":
            now = self._clock()
            # A probe that never reports back (e.g. cancelled) stops blocking after one interval.
            if self._probe_at is not None and now - self._probe_at < self.reset_after_s:
                raise CircuitOpenError("circuit half-open: probe in flight")
            self._probe_at = now

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_at = None

    def record_failure(self) -> None:
        self._probe_at = None
        self._failures += 1
        if self.state == "half_open" or self._failures >= self.failure_threshold:
            self._opened_at = self._clock()


class _RetryableStatus(Exception):
    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


class ResilientTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        wrapped: httpx.AsyncBaseTransport,
        *,
        policy: RetryPolicy,
        breaker_factory: Callable[[], CircuitBreaker] = CircuitBreaker,
    ) -> None:
        self._wrapped = wrapped
        self._policy = policy
        self._breaker_factory = breaker_factory
        self._breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, host: str) -> CircuitBreaker:
        if host not in self._breakers:
            self._breakers[host] = self._breaker_factory()
        return self._breakers[host]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        breaker = self.breaker(request.url.host)
        retrying = AsyncRetrying(
            stop=stop_after_attempt(
                self._policy.attempts if self._policy.allows_retry(request) else 1
            ),
            wait=wait_random_exponential(
                multiplier=self._policy.initial_backoff_s,
                max=self._policy.max_backoff_s,
            ),
            retry=retry_if_exception_type((httpx.TransportError, _RetryableStatus)),
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await self._send_once(request, breaker)
        except _RetryableStatus as exc:
            return exc.response
        raise AssertionError("unreachable")

    async def _send_once(
        self, request: httpx.Request, breaker: CircuitBreaker
    ) -> httpx.Response:
        breaker.before_call()
        try:
            response = await self._wrapped.handle_async_request(request)
        except httpx.TransportError:
            breaker.record_failure()
            raise
        if response.status_code in self._policy.retry_statuses:
            breaker.record_failure()
            await response.aread()
            raise _RetryableStatus(response)
        breaker.record_success()
        return response

    async def aclose(self) -> None:
        await self._wrapped.aclose()


async def _inject_trace_headers(request: httpx.Request) -> None:
    inject_trace_context(request.headers)


def resilient_client(
    *,
    timeout: httpx.Timeout | None = None,
    policy: RetryPolicy | None = None,
    breaker_factory: Callable[[], CircuitBreaker] = CircuitBreaker,
    transport: httpx.AsyncBaseTransport | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """The one HTTP client factory for A2A, MCP and data calls: timeouts, retries, breakers, traceparent."""
    return httpx.AsyncClient(
        timeout=timeout or httpx.Timeout(10.0, connect=3.0),
        transport=ResilientTransport(
            transport or httpx.AsyncHTTPTransport(),
            policy=policy or RetryPolicy(),
            breaker_factory=breaker_factory,
        ),
        event_hooks={"request": [_inject_trace_headers]},
        **kwargs,
    )
