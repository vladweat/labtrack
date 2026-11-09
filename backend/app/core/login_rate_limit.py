"""Process-local failed-login throttling for the single-worker demo API."""

import asyncio
import math
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from threading import Lock

from fastapi import Request


@dataclass
class _SourceAttempt:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class LoginAttemptLimiter:
    """Bound failed attempts and source buckets with a monotonic sliding window."""

    def __init__(
        self,
        *,
        limit: int = 30,
        window_seconds: int = 900,
        max_sources: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(limit, window_seconds, max_sources) <= 0:
            raise ValueError("Login limiter bounds must be positive")
        self._limit = limit
        self._window_seconds = window_seconds
        self._max_sources = max_sources
        self._clock = clock
        self._lock = Lock()
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._attempt_sources: dict[str, _SourceAttempt] = {}
        self._attempt_sources_changed = asyncio.Condition()

    @asynccontextmanager
    async def source_attempt_lock(self, source_ip: str) -> AsyncIterator[None]:
        """Serialize authentication per source, reclaiming idle lock entries."""

        # Only registry bookkeeping shares this condition. Authentication holds
        # the individual source lock, so other sources proceed independently.
        async with self._attempt_sources_changed:
            while source_ip not in self._attempt_sources and (
                len(self._attempt_sources) >= self._max_sources
            ):
                await self._attempt_sources_changed.wait()
            attempt = self._attempt_sources.get(source_ip)
            if attempt is None:
                attempt = _SourceAttempt()
                self._attempt_sources[source_ip] = attempt
            # Include waiters so an in-use source lock can never be replaced.
            attempt.users += 1
        try:
            async with attempt.lock:
                yield
        finally:
            async with self._attempt_sources_changed:
                attempt.users -= 1
                if attempt.users == 0:
                    del self._attempt_sources[source_ip]
                    self._attempt_sources_changed.notify_all()

    def _cleanup(self, now: float) -> None:
        # Sources are ordered by their most recent failure. Fully expired
        # buckets therefore form a prefix; cleanup avoids scanning every key.
        cutoff = now - self._window_seconds
        while self._failures:
            failures = next(iter(self._failures.values()))
            if failures[-1] > cutoff:
                break
            self._failures.popitem(last=False)

    def retry_after(self, source_ip: str, *, now: float | None = None) -> int | None:
        """Return the rounded-up seconds until this source can attempt login."""

        with self._lock:
            current_time = self._clock() if now is None else now
            self._cleanup(current_time)
            failures = self._failures.get(source_ip)
            if failures is None:
                return None
            cutoff = current_time - self._window_seconds
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if len(failures) < self._limit:
                return None
            return math.ceil(failures[0] + self._window_seconds - current_time)

    def record_failure(self, source_ip: str, *, now: float | None = None) -> None:
        """Record a failed authentication without allowing unbounded memory use."""

        with self._lock:
            current_time = self._clock() if now is None else now
            self._cleanup(current_time)
            failures = self._failures.get(source_ip)
            if failures is None:
                if len(self._failures) >= self._max_sources:
                    self._failures.popitem(last=False)
                failures = deque(maxlen=self._limit)
                self._failures[source_ip] = failures
            failures.append(current_time)
            self._failures.move_to_end(source_ip)

    def record_success(self, source_ip: str) -> None:
        """Clear the source's failure history after successful authentication."""

        with self._lock:
            self._failures.pop(source_ip, None)


def get_client_source_ip(
    request: Request,
    *,
    trusted_proxy_networks: Iterable[IPv4Network | IPv6Network] = (),
) -> str:
    """Use the transport peer unless it is an explicitly trusted proxy.

    A trusted proxy may provide one valid X-Real-IP value. The edge must
    overwrite or remove client-supplied forwarding headers before this header
    can be trusted. X-Forwarded-For is intentionally not used here.
    """

    transport_peer = request.client.host if request.client else None
    try:
        peer_ip = ip_address(transport_peer) if transport_peer else None
    except ValueError:
        peer_ip = None
    if peer_ip is None:
        # A forwarded value cannot replace a missing or malformed transport peer.
        return "unknown"

    if not any(peer_ip in network for network in trusted_proxy_networks):
        return str(peer_ip)

    candidates = request.headers.getlist("X-Real-IP")
    if len(candidates) != 1:
        return str(peer_ip)
    try:
        client_ip: IPv4Address | IPv6Address = ip_address(candidates[0])
    except ValueError:
        return str(peer_ip)
    return str(client_ip)
