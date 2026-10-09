"""Validation des JWT Supabase (`Authorization: Bearer <access_token>`).

Deux modes, choisis selon l'en-tête `alg` du token :
  * HS256        -> secret JWT legacy du projet (SUPABASE_JWT_SECRET) ;
  * ES256/RS256  -> clés asymétriques publiées sur le JWKS du projet
                    ({SUPABASE_URL}/auth/v1/.well-known/jwks.json), mises en cache.
Chaque mode n'accepte que son propre algorithme : pas de confusion d'algorithme.
"""

import asyncio
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from .config import Settings, get_settings

_ASYMMETRIC_ALGS = {"ES256", "RS256"}

_bearer = HTTPBearer(auto_error=False)
_jwks_client: PyJWKClient | None = None


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str | None
    role: str


class AuthServiceUnavailable(Exception):
    pass


def _get_jwks_client(settings: Settings) -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(settings.supabase_jwks_url, cache_keys=True, lifespan=600)
    return _jwks_client


def decode_supabase_token(token: str, settings: Settings) -> dict:
    alg = jwt.get_unverified_header(token).get("alg")

    if alg == "HS256":
        if not settings.supabase_jwt_secret:
            raise jwt.InvalidAlgorithmError("HS256 reçu mais SUPABASE_JWT_SECRET non configuré")
        key = settings.supabase_jwt_secret
    elif alg in _ASYMMETRIC_ALGS:
        try:
            key = _get_jwks_client(settings).get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError as exc:
            raise AuthServiceUnavailable(str(exc)) from exc
    else:
        raise jwt.InvalidAlgorithmError(f"Algorithme non supporté : {alg}")

    return jwt.decode(
        token,
        key,
        algorithms=[alg],
        audience=settings.supabase_jwt_audience,
        issuer=settings.supabase_issuer,
        options={"require": ["exp", "sub", "aud", "iss"]},
        leeway=10,
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> AuthUser:
    """Dépendance FastAPI : renvoie l'utilisateur authentifié ou lève 401."""
    if credentials is None:
        raise _unauthorized("Token d'authentification manquant")

    try:
        # Thread séparé : la récupération du JWKS est un appel réseau bloquant.
        claims = await asyncio.to_thread(decode_supabase_token, credentials.credentials, settings)
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Token expiré")
    except AuthServiceUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service d'authentification indisponible",
        )
    except jwt.PyJWTError:
        raise _unauthorized("Token invalide")

    # Le token `anon` (clé publique) n'a ni sub ni rôle authenticated.
    if claims.get("role") != "authenticated":
        raise _unauthorized("Session utilisateur requise")

    return AuthUser(id=claims["sub"], email=claims.get("email"), role=claims["role"])
