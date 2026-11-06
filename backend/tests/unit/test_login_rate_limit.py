"""Failed-login windows and source-address handling."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from ipaddress import ip_network

import pytest
from starlette.requests import Request

from app.core.config import Settings
from app.core.login_rate_limit import LoginAttemptLimiter, get_client_source_ip


def test_threshold_limits_only_after_the_configured_failures() -> None:
    limiter = LoginAttemptLimiter(limit=3, window_seconds=10)
    for _ in range(2):
        limiter.record_failure("192.0.2.1", now=0)
        assert limiter.retry_after("192.0.2.1", now=0) is None
    limiter.record_failure("192.0.2.1", now=0)
    assert limiter.retry_after("192.0.2.1", now=0) == 10
    assert limiter.retry_after("192.0.2.2", now=0) is None


def test_sliding_window_expires_at_the_boundary_and_rounds_retry_up() -> None:
    limiter = LoginAttemptLimiter(limit=2, window_seconds=10)
    limiter.record_failure("192.0.2.1", now=0)
    limiter.record_failure("192.0.2.1", now=3)
    assert limiter.retry_after("192.0.2.1", now=9.25) == 1
    assert limiter.retry_after("192.0.2.1", now=10) is None
    limiter.record_failure("192.0.2.1", now=10)
    assert limiter.retry_after("192.0.2.1", now=10) == 3
    assert limiter.retry_after("192.0.2.1", now=20) is None


def test_source_keys_are_bounded_and_expired_buckets_are_reclaimed() -> None:
    limiter = LoginAttemptLimiter(limit=1, window_seconds=10, max_sources=2)
    limiter.record_failure("192.0.2.1", now=0)
    limiter.record_failure("192.0.2.2", now=2)
    limiter.record_failure("192.0.2.3", now=10)
    assert limiter.retry_after("192.0.2.1", now=10) is None
    assert limiter.retry_after("192.0.2.2", now=10) == 2
    limiter.record_failure("192.0.2.4", now=10)
    assert limiter.retry_after("192.0.2.2", now=10) is None
    assert limiter.retry_after("192.0.2.3", now=10) == 10
    assert limiter.retry_after("192.0.2.4", now=10) == 10


def test_record_success_clears_only_that_source() -> None:
    limiter = LoginAttemptLimiter(limit=1, window_seconds=10)
    limiter.record_failure("192.0.2.1", now=0)
    limiter.record_failure("192.0.2.2", now=0)
    limiter.record_success("192.0.2.1")
    assert limiter.retry_after("192.0.2.1", now=0) is None
    assert limiter.retry_after("192.0.2.2", now=0) == 10
    limiter.record_success("192.0.2.3")


def test_injected_monotonic_clock_drives_default_times() -> None:
    current_time = [50.0]
    limiter = LoginAttemptLimiter(limit=1, window_seconds=10, clock=lambda: current_time[0])
    limiter.record_failure("192.0.2.1")
    current_time[0] = 59.5
    assert limiter.retry_after("192.0.2.1") == 1
    current_time[0] = 60
    assert limiter.retry_after("192.0.2.1") is None


def test_concurrent_failures_do_not_lose_attempts() -> None:
    limiter = LoginAttemptLimiter(limit=200, window_seconds=30)
    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(lambda _: limiter.record_failure("192.0.2.1", now=0), range(200)))
    assert limiter.retry_after("192.0.2.1", now=0) == 30
    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(lambda _: limiter.retry_after("192.0.2.1", now=30), range(200)))
    assert limiter.retry_after("192.0.2.1", now=30) is None


@pytest.mark.parametrize(
    "header",
    [
        "198.51.100.7",
        "2001:0db8::1",
        "spoofed",
        "198.51.100.7, 198.51.100.8",
        "",
    ],
)
def test_untrusted_peer_uses_transport_ip_regardless_of_real_ip_header(header: str) -> None:
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-real-ip", header.encode()), (b"x-forwarded-for", b"203.0.113.1")],
            "client": ("192.0.2.9", 1234),
        }
    )
    assert get_client_source_ip(request) == "192.0.2.9"


@pytest.mark.parametrize(
    ("peer", "network", "header", "expected"),
    [
        ("192.0.2.9", "192.0.2.0/24", "198.51.100.7", "198.51.100.7"),
        ("2001:db8::2", "2001:db8::/32", "2001:0db8:0:0::7", "2001:db8::7"),
    ],
)
def test_trusted_proxy_uses_one_valid_real_ip(
    peer: str, network: str, header: str, expected: str
) -> None:
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-real-ip", header.encode())],
            "client": (peer, 1234),
        }
    )

    assert (
        get_client_source_ip(
            request,
            trusted_proxy_networks=(ip_network(network),),
        )
        == expected
    )


def test_x_forwarded_for_is_ignored_even_for_a_trusted_peer() -> None:
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"198.51.100.7")],
            "client": ("192.0.2.9", 1234),
        }
    )

    assert (
        get_client_source_ip(
            request,
            trusted_proxy_networks=(ip_network("192.0.2.9/32"),),
        )
        == "192.0.2.9"
    )


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"x-real-ip", b"invalid")],
        [(b"x-real-ip", b"198.51.100.7, 198.51.100.8")],
        [(b"x-real-ip", b"2001:db8::not-an-ip")],
        [(b"x-real-ip", b"198.51.100.7"), (b"x-real-ip", b"198.51.100.8")],
    ],
)
def test_trusted_proxy_falls_back_to_peer_for_missing_or_invalid_real_ip(headers) -> None:
    request = Request(
        {
            "type": "http",
            "headers": headers,
            "client": ("2001:db8::2", 1234),
        }
    )

    assert (
        get_client_source_ip(
            request,
            trusted_proxy_networks=(ip_network("2001:db8::/32"),),
        )
        == "2001:db8::2"
    )


def test_missing_transport_peer_cannot_be_replaced_by_real_ip() -> None:
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-real-ip", b"198.51.100.7")],
            "client": None,
        }
    )

    assert (
        get_client_source_ip(
            request,
            trusted_proxy_networks=(ip_network("192.0.2.0/24"),),
        )
        == "unknown"
    )


def test_trusted_proxy_configuration_is_empty_by_default_and_validates_networks() -> None:
    assert Settings().trusted_login_proxy_networks == ()
    settings = Settings(login_trusted_proxy_networks="192.0.2.9,2001:db8::/32")
    assert tuple(str(network) for network in settings.trusted_login_proxy_networks) == (
        "192.0.2.9/32",
        "2001:db8::/32",
    )
    with pytest.raises(ValueError, match="comma-separated IPs or CIDRs"):
        Settings(login_trusted_proxy_networks="not-an-ip")


@pytest.mark.asyncio
async def test_attempt_locks_serialize_one_source_and_allow_another_source() -> None:
    limiter = LoginAttemptLimiter(max_sources=2)
    same_source_entered = asyncio.Event()
    other_source_entered = asyncio.Event()

    async def enter(source: str, entered: asyncio.Event) -> None:
        async with limiter.source_attempt_lock(source):
            entered.set()

    async with limiter.source_attempt_lock("192.0.2.1"):
        same_source = asyncio.create_task(enter("192.0.2.1", same_source_entered))
        other_source = asyncio.create_task(enter("192.0.2.2", other_source_entered))
        await asyncio.wait_for(other_source, timeout=1)
        assert other_source_entered.is_set()
        assert not same_source_entered.is_set()
    await asyncio.wait_for(same_source, timeout=1)
    assert same_source_entered.is_set()


@pytest.mark.asyncio
async def test_attempt_lock_capacity_is_reclaimed_after_waiter_cancel_and_holder_error() -> None:
    limiter = LoginAttemptLimiter(max_sources=1)
    same_source_entered = asyncio.Event()
    other_source_entered = asyncio.Event()

    async def enter(source: str, entered: asyncio.Event) -> None:
        async with limiter.source_attempt_lock(source):
            entered.set()

    with pytest.raises(RuntimeError, match="authentication failed unexpectedly"):
        async with limiter.source_attempt_lock("192.0.2.1"):
            same_source = asyncio.create_task(enter("192.0.2.1", same_source_entered))
            other_source = asyncio.create_task(enter("192.0.2.2", other_source_entered))
            await asyncio.sleep(0)
            assert not same_source_entered.is_set()
            assert not other_source_entered.is_set()
            same_source.cancel()
            with pytest.raises(asyncio.CancelledError):
                await same_source
            raise RuntimeError("authentication failed unexpectedly")

    await asyncio.wait_for(other_source, timeout=1)
    assert other_source_entered.is_set()
    # Successful release must also reclaim the sole registry slot.
    async with limiter.source_attempt_lock("192.0.2.3"):
        pass
