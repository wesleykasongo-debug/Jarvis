import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from redis.exceptions import RedisError

from . import console
from .api.v1 import chat
from .auth import AuthUser, get_current_user
from .config import get_settings
from .quota import QuotaService, get_quota_service, init_quota_service
from .redis_client import close_redis, get_redis, init_redis
from .supabase_client import close_supabase, get_supabase, init_supabase

logging.basicConfig(
    level=get_settings().log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    redis = await init_redis(settings.redis_url)
    supabase = init_supabase(settings.supabase_url, settings.supabase_service_role_key)
    init_quota_service(redis, supabase, limits_cache_ttl=settings.quota_limits_cache_ttl)
    yield
    # Laisse les sauvegardes de fin de flux se terminer avant de fermer les clients.
    await chat.wait_background_tasks()
    await close_supabase()
    await close_redis()


app = FastAPI(title="JARVIS API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    # Sans ça, Flutter Web ne peut pas lire les en-têtes de quota.
    expose_headers=[
        "X-Quota-Plan",
        "X-Quota-Messages-Limit",
        "X-Quota-Messages-Remaining",
        "X-Quota-Tokens-Limit",
        "X-Quota-Tokens-Remaining",
        "X-Quota-Reset",
        "Retry-After",
    ],
)

app.include_router(chat.router, prefix="/api/v1")
app.include_router(console.router)


@app.get("/health")
async def health() -> dict:
    """Vivacité : le processus répond."""
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(response: Response) -> dict:
    """Disponibilité : Redis et Supabase joignables."""
    checks = {}
    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except RedisError as exc:
        checks["redis"] = f"erreur : {exc}"
    try:
        await get_supabase().ping()
        checks["supabase"] = "ok"
    except httpx.HTTPError as exc:
        checks["supabase"] = f"erreur : {exc}"

    ok = all(v == "ok" for v in checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "degraded", **checks}


@app.get("/me")
async def me(user: AuthUser = Depends(get_current_user)) -> dict:
    return {"id": user.id, "email": user.email}


@app.get("/me/quota")
async def my_quota(
    response: Response,
    user: AuthUser = Depends(get_current_user),
    service: QuotaService = Depends(get_quota_service),
) -> dict:
    quota = await service.get_status(user.id)
    response.headers.update(quota.headers())
    return quota.to_dict()
