"""Quotas journaliers par utilisateur.

Redis tient le compteur temps réel ; Supabase (`quota_usage`) garde la trace
persistante et sert à réhydrater Redis si une clé est absente (reboot, éviction).

Schéma des clés Redis (jour = date UTC) :
  quota:{user_id}:{YYYY-MM-DD}:requests  int   requêtes acceptées aujourd'hui
  quota:{user_id}:{YYYY-MM-DD}:tokens    int   tokens consommés aujourd'hui
  quota:{user_id}:limits                 hash  {plan, messages, tokens}, -1 = illimité
                                               (cache des limites, TTL court)
Les compteurs expirent exactement à minuit UTC (TTL posé à leur création).

Cycle d'une requête de chat :
  1. reserve()       vérifie les deux limites et incrémente `requests` en un
                     seul script Lua (atomique : pas de race condition) ;
  2. record_usage()  après la réponse : ajoute les tokens dans Redis et
                     persiste (+1 message, +N tokens) dans Supabase ;
  3. release()       si la génération échoue avant tout token : rend la requête.

La limite de tokens est vérifiée avant la requête (le coût exact n'est connu
qu'après) : une dernière requête peut donc dépasser légèrement la limite.
"""

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import httpx
from fastapi import Depends, HTTPException, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .auth import AuthUser, get_current_user
from .supabase_client import SupabaseAdmin, UserLimits

logger = logging.getLogger(__name__)

UNLIMITED = -1

# Codes de retour du script de réservation
_MISS = -1             # compteurs absents : réhydrater depuis Supabase
_DENIED_MESSAGES = 0
_OK = 1
_DENIED_TOKENS = 2

# KEYS[1] = clé requests, KEYS[2] = clé tokens
# ARGV    = limite messages, limite tokens, ttl, seed? (0/1), seed requests, seed tokens
_RESERVE_LUA = """
local req_key, tok_key = KEYS[1], KEYS[2]
local msg_limit = tonumber(ARGV[1])
local tok_limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

if ARGV[4] == '1' then
  -- NX : si une requête concurrente a déjà réhydraté, on ne l'écrase pas.
  redis.call('SET', req_key, ARGV[5], 'NX', 'EX', ttl)
  redis.call('SET', tok_key, ARGV[6], 'NX', 'EX', ttl)
end

local req = redis.call('GET', req_key)
local tok = redis.call('GET', tok_key)
if not req or not tok then
  return {-1, 0, 0}
end
req = tonumber(req)
tok = tonumber(tok)

if msg_limit >= 0 and req >= msg_limit then
  return {0, req, tok}
end
if tok_limit >= 0 and tok >= tok_limit then
  return {2, req, tok}
end

req = redis.call('INCR', req_key)
return {1, req, tok}
"""

# N'incrémente que si la clé existe : une clé recréée sans réhydratation
# fausserait le compteur. Si elle a disparu, le prochain reserve() la
# réhydratera depuis Supabase, déjà à jour.
_INCRBY_IF_EXISTS_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return redis.call('INCRBY', KEYS[1], ARGV[1])
end
return -1
"""

_DECR_IF_POSITIVE_LUA = """
local v = tonumber(redis.call('GET', KEYS[1]) or '0')
if v > 0 then
  return redis.call('DECR', KEYS[1])
end
return v
"""


# --- Clés & temps ---------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def requests_key(user_id: str, day: date) -> str:
    return f"quota:{user_id}:{day.isoformat()}:requests"


def tokens_key(user_id: str, day: date) -> str:
    return f"quota:{user_id}:{day.isoformat()}:tokens"


def limits_key(user_id: str) -> str:
    return f"quota:{user_id}:limits"


def seconds_until_utc_midnight(now: datetime) -> int:
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return max(1, math.ceil((midnight - now).total_seconds()))


def _encode_limit(value: int | None) -> int:
    return UNLIMITED if value is None else value


def _decode_limit(value: str | int) -> int | None:
    value = int(value)
    return None if value == UNLIMITED else value


# --- Modèles --------------------------------------------------------------------

@dataclass(frozen=True)
class QuotaStatus:
    plan: str
    day: date
    messages_used: int
    messages_limit: int | None
    tokens_used: int
    tokens_limit: int | None
    resets_in: int  # secondes avant minuit UTC

    @property
    def messages_remaining(self) -> int | None:
        if self.messages_limit is None:
            return None
        return max(0, self.messages_limit - self.messages_used)

    @property
    def tokens_remaining(self) -> int | None:
        if self.tokens_limit is None:
            return None
        return max(0, self.tokens_limit - self.tokens_used)

    def headers(self) -> dict[str, str]:
        """En-têtes exposés à Flutter pour la jauge (-1 = illimité)."""
        def fmt(v: int | None) -> str:
            return str(UNLIMITED if v is None else v)

        return {
            "X-Quota-Plan": self.plan,
            "X-Quota-Messages-Limit": fmt(self.messages_limit),
            "X-Quota-Messages-Remaining": fmt(self.messages_remaining),
            "X-Quota-Tokens-Limit": fmt(self.tokens_limit),
            "X-Quota-Tokens-Remaining": fmt(self.tokens_remaining),
            "X-Quota-Reset": str(self.resets_in),
        }

    def to_dict(self) -> dict:
        return {
            "plan": self.plan,
            "date": self.day.isoformat(),
            "messages_used": self.messages_used,
            "messages_limit": self.messages_limit,
            "messages_remaining": self.messages_remaining,
            "tokens_used": self.tokens_used,
            "tokens_limit": self.tokens_limit,
            "tokens_remaining": self.tokens_remaining,
            "resets_in": self.resets_in,
        }


@dataclass(frozen=True)
class QuotaReservation:
    user: AuthUser
    status: QuotaStatus


def _quota_exceeded(kind: str, quota: QuotaStatus) -> HTTPException:
    message = (
        "Limite quotidienne de messages atteinte"
        if kind == "messages"
        else "Limite quotidienne de tokens atteinte"
    )
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"error": "quota_exceeded", "kind": kind, "message": message, "quota": quota.to_dict()},
        headers={**quota.headers(), "Retry-After": str(quota.resets_in)},
    )


# --- Service --------------------------------------------------------------------

class QuotaService:
    def __init__(self, redis: Redis, supabase: SupabaseAdmin, *, limits_cache_ttl: int = 300) -> None:
        self._redis = redis
        self._supabase = supabase
        self._limits_cache_ttl = limits_cache_ttl
        self._reserve_script = redis.register_script(_RESERVE_LUA)
        self._incrby_script = redis.register_script(_INCRBY_IF_EXISTS_LUA)
        self._decr_script = redis.register_script(_DECR_IF_POSITIVE_LUA)

    # Limites (palier) ------------------------------------------------------------

    async def get_limits(self, user_id: str) -> UserLimits:
        key = limits_key(user_id)
        try:
            cached = await self._redis.hgetall(key)
        except RedisError:
            logger.warning("Redis indisponible pour le cache des limites", exc_info=True)
            cached = None
        if cached:
            return UserLimits(
                plan=cached["plan"],
                daily_message_limit=_decode_limit(cached["messages"]),
                daily_token_limit=_decode_limit(cached["tokens"]),
            )

        limits = await self._supabase.get_user_limits(user_id)
        if limits is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Profil utilisateur introuvable")

        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.hset(key, mapping={
                    "plan": limits.plan,
                    "messages": _encode_limit(limits.daily_message_limit),
                    "tokens": _encode_limit(limits.daily_token_limit),
                })
                pipe.expire(key, self._limits_cache_ttl)
                await pipe.execute()
        except RedisError:
            logger.warning("Impossible de mettre en cache les limites", exc_info=True)
        return limits

    async def invalidate_limits(self, user_id: str) -> None:
        """À appeler après un changement de palier (passage Pro, etc.)."""
        await self._redis.delete(limits_key(user_id))

    # Réservation -----------------------------------------------------------------

    async def reserve(self, user_id: str) -> QuotaStatus:
        """Vérifie et consomme une requête. Lève 429 si une limite est atteinte."""
        limits = await self.get_limits(user_id)
        now = utc_now()
        day = now.date()
        ttl = seconds_until_utc_midnight(now)
        keys = [requests_key(user_id, day), tokens_key(user_id, day)]
        base_args = [_encode_limit(limits.daily_message_limit), _encode_limit(limits.daily_token_limit), ttl]

        try:
            code, req, tok = await self._reserve_script(keys=keys, args=[*base_args, 0, 0, 0])
            if int(code) == _MISS:
                used_messages, used_tokens = await self._supabase.get_daily_usage(user_id, day)
                code, req, tok = await self._reserve_script(
                    keys=keys, args=[*base_args, 1, used_messages, used_tokens]
                )
        except RedisError:
            logger.error("Redis indisponible : contrôle de quota en mode dégradé", exc_info=True)
            return await self._reserve_degraded(user_id, limits, day, ttl)

        quota = QuotaStatus(
            plan=limits.plan,
            day=day,
            messages_used=int(req),
            messages_limit=limits.daily_message_limit,
            tokens_used=int(tok),
            tokens_limit=limits.daily_token_limit,
            resets_in=ttl,
        )
        code = int(code)
        if code == _DENIED_MESSAGES:
            raise _quota_exceeded("messages", quota)
        if code == _DENIED_TOKENS:
            raise _quota_exceeded("tokens", quota)
        if code != _OK:
            raise RuntimeError(f"Code de retour inattendu du script de quota : {code}")
        return quota

    async def _reserve_degraded(self, user_id: str, limits: UserLimits, day: date, ttl: int) -> QuotaStatus:
        """Sans Redis : contrôle (non atomique) sur la trace Supabase.

        La consommation sera persistée par record_usage() en fin de requête.
        """
        used_messages, used_tokens = await self._supabase.get_daily_usage(user_id, day)
        quota = QuotaStatus(
            plan=limits.plan,
            day=day,
            messages_used=used_messages,
            messages_limit=limits.daily_message_limit,
            tokens_used=used_tokens,
            tokens_limit=limits.daily_token_limit,
            resets_in=ttl,
        )
        if limits.daily_message_limit is not None and used_messages >= limits.daily_message_limit:
            raise _quota_exceeded("messages", quota)
        if limits.daily_token_limit is not None and used_tokens >= limits.daily_token_limit:
            raise _quota_exceeded("tokens", quota)
        return QuotaStatus(**{**quota.__dict__, "messages_used": used_messages + 1})

    # Fin de requête --------------------------------------------------------------

    async def record_usage(self, reservation: QuotaReservation, tokens: int) -> None:
        """Comptabilise une requête terminée : tokens dans Redis + trace Supabase."""
        user_id = reservation.user.id
        tokens = max(0, tokens)

        # Supabase d'abord : si la clé Redis disparaît entre-temps, la
        # réhydratation lira une valeur à jour.
        try:
            await self._supabase.increment_quota_usage(user_id, 1, tokens)
        except httpx.HTTPError:
            logger.error("Échec de persistance de la consommation (user=%s)", user_id, exc_info=True)

        if tokens:
            try:
                await self._incrby_script(keys=[tokens_key(user_id, reservation.status.day)], args=[tokens])
            except RedisError:
                logger.warning("Échec d'incrément des tokens dans Redis (user=%s)", user_id, exc_info=True)

    async def release(self, reservation: QuotaReservation) -> None:
        """Rend une requête réservée qui n'a produit aucune réponse."""
        try:
            await self._decr_script(keys=[requests_key(reservation.user.id, reservation.status.day)])
        except RedisError:
            logger.warning("Échec de libération du quota (user=%s)", reservation.user.id, exc_info=True)

    # Lecture seule ---------------------------------------------------------------

    async def get_status(self, user_id: str) -> QuotaStatus:
        """État du quota sans rien consommer (jauge Flutter)."""
        limits = await self.get_limits(user_id)
        now = utc_now()
        day = now.date()
        try:
            req, tok = await self._redis.mget(requests_key(user_id, day), tokens_key(user_id, day))
        except RedisError:
            req = tok = None
        if req is None or tok is None:
            used_messages, used_tokens = await self._supabase.get_daily_usage(user_id, day)
        else:
            used_messages, used_tokens = int(req), int(tok)
        return QuotaStatus(
            plan=limits.plan,
            day=day,
            messages_used=used_messages,
            messages_limit=limits.daily_message_limit,
            tokens_used=used_tokens,
            tokens_limit=limits.daily_token_limit,
            resets_in=seconds_until_utc_midnight(now),
        )


# --- Dépendances FastAPI ------------------------------------------------------

_service: QuotaService | None = None


def init_quota_service(redis: Redis, supabase: SupabaseAdmin, *, limits_cache_ttl: int = 300) -> QuotaService:
    global _service
    _service = QuotaService(redis, supabase, limits_cache_ttl=limits_cache_ttl)
    return _service


def get_quota_service() -> QuotaService:
    if _service is None:
        raise RuntimeError("QuotaService non initialisé : appeler init_quota_service() au démarrage.")
    return _service


async def enforce_quota(
    user: AuthUser = Depends(get_current_user),
    service: QuotaService = Depends(get_quota_service),
) -> QuotaReservation:
    """Dépendance à poser sur les routes coûteuses (ex. POST /chat).

    Authentifie, puis réserve une requête ; lève 401 / 429 sinon.
    La route doit ensuite appeler record_usage() ou release().
    """
    quota = await service.reserve(user.id)
    return QuotaReservation(user=user, status=quota)
