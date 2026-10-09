import os

import fakeredis
import pytest_asyncio
from redis.asyncio import Redis

os.environ.setdefault("SUPABASE_URL", "https://test-project.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-role-test")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-with-at-least-32-characters!!")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

# REDIS_TEST_URL=redis://localhost:6379/15 -> tests sur un vrai Redis
# (base dédiée, vidée à chaque test). Sinon : Redis simulé (fakeredis).
REDIS_TEST_URL = os.environ.get("REDIS_TEST_URL")


@pytest_asyncio.fixture
async def redis():
    if REDIS_TEST_URL:
        r = Redis.from_url(REDIS_TEST_URL, decode_responses=True)
        await r.flushdb()
    else:
        r = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield r
    if REDIS_TEST_URL:
        await r.flushdb()
    await r.aclose()
