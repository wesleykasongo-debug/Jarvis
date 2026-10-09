"""Connexion Redis partagée (Docker en dev, Upstash / instance managée en prod).

`rediss://` active TLS automatiquement (requis par Upstash).
"""

from redis.asyncio import Redis

_redis: Redis | None = None


async def init_redis(url: str) -> Redis:
    global _redis
    _redis = Redis.from_url(
        url,
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=2,
        health_check_interval=30,
    )
    await _redis.ping()
    return _redis


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis non initialisé : appeler init_redis() au démarrage.")
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
