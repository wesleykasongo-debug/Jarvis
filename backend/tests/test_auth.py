import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import AuthUser, get_current_user
from app.config import get_settings

settings = get_settings()

app = FastAPI()


@app.get("/me")
async def me(user: AuthUser = Depends(get_current_user)):
    return {"id": user.id, "email": user.email}


client = TestClient(app)


def make_token(**overrides) -> str:
    claims = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "email": "tony@stark.dev",
        "role": "authenticated",
        "aud": "authenticated",
        "iss": settings.supabase_issuer,
        "exp": int(time.time()) + 3600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, settings.supabase_jwt_secret, algorithm="HS256")


def get_me(token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get("/me", headers=headers)


def test_valid_token_returns_user_id():
    resp = get_me(make_token())
    assert resp.status_code == 200
    assert resp.json() == {"id": "11111111-1111-1111-1111-111111111111", "email": "tony@stark.dev"}


def test_missing_token():
    resp = get_me(None)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_expired_token():
    resp = get_me(make_token(exp=int(time.time()) - 3600))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Token expiré"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "other"},
        {"iss": "https://evil.example.com/auth/v1"},
        {"role": "anon"},
        {"sub": None},
    ],
)
def test_rejected_claims(overrides):
    assert get_me(make_token(**overrides)).status_code == 401


def test_bad_signature():
    token = jwt.encode(
        {"sub": "x", "role": "authenticated", "aud": "authenticated",
         "iss": settings.supabase_issuer, "exp": int(time.time()) + 60},
        "wrong-secret-wrong-secret-wrong-secret",
        algorithm="HS256",
    )
    assert get_me(token).status_code == 401


def test_alg_none_rejected():
    token = jwt.encode(
        {"sub": "x", "role": "authenticated", "aud": "authenticated",
         "iss": settings.supabase_issuer, "exp": int(time.time()) + 60},
        None,
        algorithm="none",
    )
    assert get_me(token).status_code == 401
