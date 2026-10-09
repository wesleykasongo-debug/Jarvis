import json
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest_asyncio
from fastapi import FastAPI

from app.api.v1 import chat
from app.config import get_settings
from app.quota import QuotaService, get_quota_service, requests_key, utc_now
from app.redis_client import get_redis
from app.supabase_client import UserLimits, get_supabase

USER = "11111111-1111-1111-1111-111111111111"
OTHER_CONV = "22222222-2222-2222-2222-222222222222"
OWN_CONV = "33333333-3333-3333-3333-333333333333"
settings = get_settings()


# --- Doublures ----------------------------------------------------------------

class FakeSupabase:
    def __init__(self, limits=UserLimits("free", 30, 50_000), usage=(0, 0)):
        self.limits = limits
        self.usage = usage
        self.increments = []
        self.inserted = []
        self.history = {OWN_CONV: [
            {"role": "user", "content": "Salut JARVIS"},
            {"role": "assistant", "content": "Bonjour !"},
        ]}

    async def get_user_limits(self, user_id):
        return self.limits

    async def get_daily_usage(self, user_id, day):
        return self.usage

    async def increment_quota_usage(self, user_id, messages, tokens):
        self.increments.append((messages, tokens))

    async def get_conversation(self, conversation_id, user_id):
        if conversation_id == OWN_CONV and user_id == USER:
            return {"id": OWN_CONV, "title": "Ancienne", "persona": None}
        return None

    async def create_conversation(self, user_id, title):
        return {"id": "44444444-4444-4444-4444-444444444444", "title": title, "persona": None}

    async def get_recent_messages(self, conversation_id, limit):
        return self.history.get(conversation_id, [])[-limit:]

    async def insert_messages(self, rows):
        self.inserted.extend(rows)


def chunk(text=None, usage=None):
    choices = [SimpleNamespace(delta=SimpleNamespace(content=text))] if text is not None else []
    return SimpleNamespace(choices=choices, usage=usage)


class FakeOpenAI:
    def __init__(self, chunks=None, fail=False):
        self.calls = []
        self._chunks = chunks if chunks is not None else [
            chunk("Bonjour"), chunk(", Tony."),
            chunk(usage=SimpleNamespace(prompt_tokens=40, completion_tokens=6)),
        ]
        self._fail = fail
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("OpenAI down")

        async def gen():
            for c in self._chunks:
                yield c
        return gen()


# --- Mise en place --------------------------------------------------------------

@pytest_asyncio.fixture
async def env(redis):
    supa = FakeSupabase()
    llm = FakeOpenAI()
    service = QuotaService(redis, supa)

    app = FastAPI()
    app.include_router(chat.router, prefix="/api/v1")
    app.dependency_overrides[get_quota_service] = lambda: service
    app.dependency_overrides[get_supabase] = lambda: supa
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[chat.get_openai] = lambda: llm

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    yield SimpleNamespace(client=client, redis=redis, supa=supa, llm=llm)
    await client.aclose()


def auth_headers():
    token = jwt.encode(
        {"sub": USER, "role": "authenticated", "aud": "authenticated",
         "iss": settings.supabase_issuer, "exp": int(time.time()) + 3600},
        settings.supabase_jwt_secret,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


async def post_chat(env, **body):
    resp = await env.client.post("/api/v1/chat/stream", json=body, headers=auth_headers())
    await chat.wait_background_tasks()
    return resp


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    events = []
    for block in raw.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def requests_count(env):
    return env.redis.get(requests_key(USER, utc_now().date()))


# --- Tests --------------------------------------------------------------------------

async def test_stream_new_conversation(env):
    resp = await post_chat(env, message="Bonjour JARVIS", ton="amical")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["X-Quota-Messages-Remaining"] == "29"

    events = parse_sse(resp.text)
    assert [e for e, _ in events] == ["meta", "delta", "delta", "done"]
    meta, done = events[0][1], events[-1][1]
    assert meta["conversation_id"] == "44444444-4444-4444-4444-444444444444"
    assert "".join(d["text"] for e, d in events if e == "delta") == "Bonjour, Tony."
    assert done["message_id"] == meta["message_id"]
    assert done["usage"]["total_tokens"] == 46
    assert done["quota"]["tokens_used"] == 46

    # Persistance asynchrone
    user_row, assistant_row = env.supa.inserted
    assert user_row["role"] == "user" and user_row["content"] == "Bonjour JARVIS"
    assert assistant_row["role"] == "assistant"
    assert assistant_row["id"] == meta["message_id"]
    assert assistant_row["content"] == "Bonjour, Tony."
    assert (assistant_row["tokens_input"], assistant_row["tokens_output"]) == (40, 6)
    assert env.supa.increments == [(1, 46)]

    # Contexte court Redis créé pour la nouvelle conversation
    ctx = await env.redis.lrange(chat.context_key(meta["conversation_id"]), 0, -1)
    assert [json.loads(m)["role"] for m in ctx] == ["user", "assistant"]


async def test_tone_is_injected_in_system_prompt(env):
    await post_chat(env, message="Résume ma journée", ton="professionnel")

    system = env.llm.calls[0]["messages"][0]
    assert system["role"] == "system"
    assert chat.INSTRUCTIONS_TON[chat.Ton.professionnel] in system["content"]
    assert env.llm.calls[0]["model"] == "gpt-4o-mini"


async def test_existing_conversation_uses_history(env):
    await post_chat(env, message="Et demain ?", conversation_id=OWN_CONV)

    sent = env.llm.calls[0]["messages"]
    assert [m["content"] for m in sent[1:]] == ["Salut JARVIS", "Bonjour !", "Et demain ?"]
    # Le contexte est désormais en cache Redis (historique + nouvel échange)
    assert await env.redis.llen(chat.context_key(OWN_CONV)) == 4


async def test_foreign_conversation_404_and_quota_released(env):
    resp = await post_chat(env, message="Hack", conversation_id=OTHER_CONV)

    assert resp.status_code == 404
    assert await requests_count(env) == "0"
    assert env.llm.calls == []


async def test_llm_failure_sends_error_and_releases_quota(env):
    env.llm._fail = True
    resp = await post_chat(env, message="Bonjour")

    events = parse_sse(resp.text)
    assert [e for e, _ in events] == ["meta", "error"]
    assert await requests_count(env) == "0"
    assert env.supa.inserted == []


def test_llm_error_codes():
    import httpx as _httpx
    import openai

    req = _httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

    def err(cls, status, body):
        return cls("x", response=_httpx.Response(status, request=req), body=body)

    credit = err(openai.RateLimitError, 429, {"code": "insufficient_quota", "type": "insufficient_quota"})
    assert chat.describe_llm_error(credit)["code"] == "llm_credit"
    rate = err(openai.RateLimitError, 429, {"code": "rate_limit_exceeded"})
    assert chat.describe_llm_error(rate)["code"] == "llm_rate_limit"
    auth = err(openai.AuthenticationError, 401, {"code": "invalid_api_key"})
    assert chat.describe_llm_error(auth)["code"] == "llm_auth"
    assert chat.describe_llm_error(RuntimeError())["code"] == "llm_unavailable"


async def test_quota_exceeded_returns_429_before_stream(env):
    env.supa.usage = (30, 0)
    resp = await post_chat(env, message="Bonjour")

    assert resp.status_code == 429
    assert resp.json()["detail"]["kind"] == "messages"
    assert env.llm.calls == []


async def test_missing_token_401(env):
    resp = await env.client.post("/api/v1/chat/stream", json={"message": "x"})
    assert resp.status_code == 401


async def test_partial_response_estimated_when_no_usage(env):
    env.llm._chunks = [chunk("Réponse sans usage")]
    await post_chat(env, message="Bonjour")

    assistant_row = env.supa.inserted[1]
    assert assistant_row["tokens_output"] == chat.estimate_tokens("Réponse sans usage")
    assert env.supa.increments[0][1] > 0
