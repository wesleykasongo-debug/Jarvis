import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.auth import AuthUser
from app.quota import (
    QuotaReservation,
    QuotaService,
    requests_key,
    seconds_until_utc_midnight,
    tokens_key,
)
from app.supabase_client import UserLimits

USER = "11111111-1111-1111-1111-111111111111"


class FakeSupabase:
    def __init__(self, limits: UserLimits, usage: tuple[int, int] = (0, 0)):
        self.limits = limits
        self.usage = usage
        self.increments: list[tuple[str, int, int]] = []
        self.usage_reads = 0

    async def get_user_limits(self, user_id):
        return self.limits

    async def get_daily_usage(self, user_id, day):
        self.usage_reads += 1
        return self.usage

    async def increment_quota_usage(self, user_id, messages, tokens):
        self.increments.append((user_id, messages, tokens))


FREE = UserLimits(plan="free", daily_message_limit=30, daily_token_limit=50_000)
PRO_UNLIMITED = UserLimits(plan="pro", daily_message_limit=None, daily_token_limit=None)


def make_service(redis, supabase):
    return QuotaService(redis, supabase, limits_cache_ttl=300)


def today():
    return datetime.now(timezone.utc).date()


async def test_rehydrates_from_supabase_when_keys_missing(redis):
    supa = FakeSupabase(FREE, usage=(5, 1200))
    service = make_service(redis, supa)

    status = await service.reserve(USER)

    assert status.messages_used == 6
    assert status.tokens_used == 1200
    assert status.messages_remaining == 24
    assert await redis.get(requests_key(USER, today())) == "6"
    assert await redis.get(tokens_key(USER, today())) == "1200"

    # Les clés existent : plus de lecture Supabase
    await service.reserve(USER)
    assert supa.usage_reads == 1


async def test_keys_expire_at_utc_midnight(redis):
    service = make_service(redis, FakeSupabase(FREE))
    await service.reserve(USER)

    ttl = await redis.ttl(requests_key(USER, today()))
    expected = seconds_until_utc_midnight(datetime.now(timezone.utc))
    assert 0 < ttl <= expected
    assert expected - ttl <= 2
    assert await redis.ttl(tokens_key(USER, today())) > 0


async def test_message_limit_returns_429(redis):
    service = make_service(redis, FakeSupabase(FREE, usage=(29, 0)))

    await service.reserve(USER)  # 30e message : accepté
    with pytest.raises(HTTPException) as exc:
        await service.reserve(USER)

    assert exc.value.status_code == 429
    assert exc.value.detail["kind"] == "messages"
    assert exc.value.headers["X-Quota-Messages-Remaining"] == "0"
    assert int(exc.value.headers["Retry-After"]) > 0
    # Une requête refusée n'incrémente pas
    assert await redis.get(requests_key(USER, today())) == "30"


async def test_token_limit_returns_429(redis):
    service = make_service(redis, FakeSupabase(FREE, usage=(3, 50_000)))

    with pytest.raises(HTTPException) as exc:
        await service.reserve(USER)

    assert exc.value.status_code == 429
    assert exc.value.detail["kind"] == "tokens"


async def test_pro_unlimited_never_blocks(redis):
    service = make_service(redis, FakeSupabase(PRO_UNLIMITED, usage=(10_000, 99_000_000)))

    status = await service.reserve(USER)

    assert status.messages_remaining is None
    assert status.headers()["X-Quota-Messages-Limit"] == "-1"


async def test_concurrent_requests_cannot_exceed_limit(redis):
    service = make_service(redis, FakeSupabase(FREE))

    results = await asyncio.gather(
        *(service.reserve(USER) for _ in range(50)), return_exceptions=True
    )

    accepted = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, HTTPException) and r.status_code == 429]
    assert len(accepted) == 30
    assert len(refused) == 20
    assert await redis.get(requests_key(USER, today())) == "30"


async def test_record_usage_updates_redis_and_supabase(redis):
    supa = FakeSupabase(FREE)
    service = make_service(redis, supa)
    status = await service.reserve(USER)
    reservation = QuotaReservation(user=AuthUser(id=USER, email=None, role="authenticated"), status=status)

    await service.record_usage(reservation, tokens=842)

    assert await redis.get(tokens_key(USER, today())) == "842"
    assert supa.increments == [(USER, 1, 842)]


async def test_record_usage_does_not_recreate_missing_key(redis):
    supa = FakeSupabase(FREE)
    service = make_service(redis, supa)
    status = await service.reserve(USER)
    reservation = QuotaReservation(user=AuthUser(id=USER, email=None, role="authenticated"), status=status)
    await redis.delete(tokens_key(USER, today()))

    await service.record_usage(reservation, tokens=100)

    assert await redis.exists(tokens_key(USER, today())) == 0
    assert supa.increments == [(USER, 1, 100)]


async def test_release_gives_request_back(redis):
    service = make_service(redis, FakeSupabase(FREE))
    status = await service.reserve(USER)
    reservation = QuotaReservation(user=AuthUser(id=USER, email=None, role="authenticated"), status=status)

    await service.release(reservation)

    assert await redis.get(requests_key(USER, today())) == "0"


async def test_limits_are_cached(redis):
    supa = FakeSupabase(FREE)
    service = make_service(redis, supa)
    await service.get_limits(USER)

    supa.limits = PRO_UNLIMITED  # changement côté Supabase
    assert (await service.get_limits(USER)).plan == "free"  # encore en cache

    await service.invalidate_limits(USER)
    assert (await service.get_limits(USER)).plan == "pro"


async def test_degraded_mode_when_redis_down(redis, monkeypatch):
    supa = FakeSupabase(FREE, usage=(30, 0))
    service = make_service(redis, supa)
    await service.get_limits(USER)

    async def boom(*args, **kwargs):
        from redis.exceptions import ConnectionError
        raise ConnectionError("down")

    monkeypatch.setattr(service, "_reserve_script", boom)

    with pytest.raises(HTTPException) as exc:
        await service.reserve(USER)
    assert exc.value.status_code == 429

    supa.usage = (2, 0)
    status = await service.reserve(USER)
    assert status.messages_used == 3


def test_seconds_until_midnight():
    now = datetime(2026, 10, 7, 23, 59, 30, tzinfo=timezone.utc)
    assert seconds_until_utc_midnight(now) == 30
    now = datetime(2026, 10, 7, 0, 0, 0, tzinfo=timezone.utc)
    assert seconds_until_utc_midnight(now) == 86_400
