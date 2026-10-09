"""Test de bout en bout du backend lancé en local (remplace Flutter).

Prérequis : l'API tourne (uvicorn ou docker compose) et un utilisateur existe
dans Supabase (Authentication -> Users -> Add user, « Auto Confirm User »).

Usage (depuis jarvis/backend) :
  python -m scripts.smoke_test
  python -m scripts.smoke_test --api http://localhost:8000 --email test@exemple.com

Le mot de passe est demandé au clavier (jamais stocké).
"""

import argparse
import asyncio
import getpass
import json
import os
import sys

import httpx

from app.config import get_settings
from app.supabase_client import SupabaseAdmin


def step(title: str) -> None:
    print(f"\n=== {title}")


async def sign_in(settings, publishable_key: str, email: str, password: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{settings.supabase_issuer}/token",
            params={"grant_type": "password"},
            headers={"apikey": publishable_key},
            json={"email": email, "password": password},
        )
    if resp.status_code != 200:
        raise SystemExit(f"Connexion Supabase refusée ({resp.status_code}) : {resp.text}")
    return resp.json()["access_token"]


async def stream_chat(client: httpx.AsyncClient, message: str, conversation_id: str | None) -> str | None:
    body = {"message": message, "ton": "amical"}
    if conversation_id:
        body["conversation_id"] = conversation_id
    print(f"> {message}")
    async with client.stream("POST", "/api/v1/chat/stream", json=body, timeout=60) as resp:
        if resp.status_code != 200:
            print(f"ÉCHEC {resp.status_code} : {(await resp.aread()).decode()}")
            return None
        event = None
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:])
                if event == "meta":
                    conversation_id = data["conversation_id"]
                elif event == "delta":
                    print(data["text"], end="", flush=True)
                elif event == "done":
                    q = data["quota"]
                    print(f"\n  [tokens : {data['usage']['total_tokens']} · "
                          f"messages restants : {q['messages_remaining']} · "
                          f"tokens restants : {q['tokens_remaining']}]")
                elif event == "error":
                    print(f"\nERREUR SSE : {data['message']}")
                    return None
    return conversation_id


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--email")
    args = parser.parse_args()

    settings = get_settings()
    publishable = os.environ.get("SUPABASE_PUBLISHABLE_KEY") or _from_dotenv("SUPABASE_PUBLISHABLE_KEY")
    if not publishable:
        raise SystemExit("SUPABASE_PUBLISHABLE_KEY manquante dans .env")

    email = args.email or input("E-mail de l'utilisateur de test : ").strip()
    password = getpass.getpass("Mot de passe : ")

    step("Connexion Supabase")
    token = await sign_in(settings, publishable, email, password)
    print("Token JWT obtenu.")

    async with httpx.AsyncClient(base_url=args.api, headers={"Authorization": f"Bearer {token}"}) as api:
        step("GET /health/ready")
        r = await api.get("/health/ready")
        print(r.status_code, r.json())

        step("GET /me")
        r = await api.get("/me")
        print(r.status_code, r.json())
        if r.status_code != 200:
            return 1
        user_id = r.json()["id"]

        step("GET /me/quota")
        r = await api.get("/me/quota")
        print(r.status_code, r.json())

        step("Chat : nouvelle conversation")
        conversation_id = await stream_chat(api, "Bonjour JARVIS, présente-toi en une phrase.", None)
        if not conversation_id:
            return 1

        step("Chat : suite de la conversation (contexte)")
        await stream_chat(api, "Qu'est-ce que je viens de te demander ?", conversation_id)

    step("Vérification dans Supabase")
    await asyncio.sleep(1)  # laisse la sauvegarde de fond se terminer
    admin = SupabaseAdmin(settings.supabase_url, settings.supabase_service_role_key)
    try:
        messages = await admin.get_recent_messages(conversation_id, 10)
        usage = await admin._http.get(
            "/quota_usage", params={"user_id": f"eq.{user_id}", "select": "*"}
        )
    finally:
        await admin.aclose()
    print(f"{len(messages)} messages enregistrés dans la conversation {conversation_id}")
    print("quota_usage :", usage.json())
    ok = len(messages) == 4
    print("\nOK : tout fonctionne." if ok else "\nATTENTION : 4 messages attendus.")
    return 0 if ok else 1


def _from_dotenv(name: str) -> str | None:
    try:
        with open(".env", encoding="utf-8") as f:
            for line in f:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip() or None
    except FileNotFoundError:
        pass
    return None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
