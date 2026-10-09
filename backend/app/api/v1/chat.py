"""Chat en streaming SSE vers gpt-4o-mini.

POST /api/v1/chat/stream   (Authorization: Bearer <jwt Supabase>)
  corps : {"message": "...", "conversation_id": "<uuid>" | null, "ton": "naturel"}

L'authentification et le quota (401 / 403 / 404 / 429) sont contrôlés AVANT
l'ouverture du flux : ces erreurs arrivent en réponse HTTP JSON classique.
Ensuite, le flux `text/event-stream` émet :
  event: meta   {"conversation_id", "message_id", "quota"}   une fois, au début
  event: delta  {"text"}                                      fragments de réponse
  event: done   {"message_id", "usage", "quota"}              fin normale
  event: error  {"message"}                                   échec de génération

Fin de flux (normale, erreur ou déconnexion du client) : une tâche de fond
enregistre le message utilisateur et la réponse dans `messages`, comptabilise
les tokens et met à jour le contexte court Redis. Si aucun texte n'a été
généré, la requête est rendue au quota et rien n'est enregistré.

Contexte court (Redis) :
  ctx:{conversation_id}   liste JSON {role, content}, N derniers messages, TTL 1 h
  Absent -> rechargé depuis Supabase (`messages`).
"""

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
import openai as openai_errors
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from ...config import Settings, get_settings
from ...quota import QuotaReservation, QuotaService, enforce_quota, get_quota_service
from ...redis_client import get_redis
from ...supabase_client import SupabaseAdmin, get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# --- Ton & prompt system ----------------------------------------------------------

class Ton(str, Enum):
    naturel = "naturel"
    amical = "amical"
    professionnel = "professionnel"
    concis = "concis"
    pedagogue = "pedagogue"
    enthousiaste = "enthousiaste"


INSTRUCTIONS_TON: dict[Ton, str] = {
    Ton.naturel: "Exprime-toi de façon naturelle et fluide, comme dans une conversation ordinaire.",
    Ton.amical: "Adopte un ton chaleureux et amical, tutoie l'utilisateur, reste bienveillant.",
    Ton.professionnel: "Adopte un ton professionnel et posé, vouvoie l'utilisateur, structure tes réponses.",
    Ton.concis: "Sois bref : va droit au but, quelques phrases au maximum, sans préambule.",
    Ton.pedagogue: "Explique pas à pas, avec des exemples simples, en vérifiant les notions clés.",
    Ton.enthousiaste: "Montre de l'énergie et de l'enthousiasme, tout en restant précis.",
}

SYSTEM_PROMPT = """Tu es JARVIS, l'assistant personnel de l'utilisateur.
Tu réponds en français, sauf si l'utilisateur écrit dans une autre langue.
Tes réponses peuvent être lues à voix haute : évite les tableaux et le Markdown
lourd, privilégie des phrases claires.
Date du jour (UTC) : {date}.

Ton demandé : {ton}"""


def build_system_prompt(ton: Ton, persona: str | None, now: datetime) -> str:
    prompt = SYSTEM_PROMPT.format(date=now.date().isoformat(), ton=INSTRUCTIONS_TON[ton])
    if persona:
        prompt += f"\nPersona actif : {persona}"
    return prompt


# --- Modèles ----------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: UUID | None = None
    ton: Ton = Ton.naturel


# --- Utilitaires --------------------------------------------------------------------

def sse(event: str, data: dict[str, Any]) -> bytes:
    # json.dumps échappe les retours à la ligne : `data` tient sur une ligne.
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def describe_llm_error(exc: Exception) -> dict[str, str]:
    """Événement `error` : message pour l'utilisateur + code pour l'app.

    Codes : llm_credit (crédit OpenAI épuisé), llm_auth (clé invalide),
    llm_rate_limit (trop de requêtes), llm_unavailable (autre).
    """
    if isinstance(exc, openai_errors.RateLimitError):
        body = getattr(exc, "body", None) or {}
        code = body.get("code") if isinstance(body, dict) else None
        if code in ("insufficient_quota", "credit_balance_exhausted") or "insufficient_quota" in str(exc):
            return {"code": "llm_credit", "message": "Le service IA est temporairement indisponible (crédit du fournisseur épuisé)."}
        return {"code": "llm_rate_limit", "message": "Le service IA est très sollicité. Réessaie dans quelques secondes."}
    if isinstance(exc, openai_errors.AuthenticationError):
        return {"code": "llm_auth", "message": "Le service IA est mal configuré (clé refusée)."}
    return {"code": "llm_unavailable", "message": "Le moteur IA est momentanément indisponible. Réessaie dans un instant."}


def estimate_tokens(text: str) -> int:
    """Estimation grossière (~4 caractères par token) quand OpenAI ne renvoie
    pas l'usage (client déconnecté avant la fin)."""
    return max(1, len(text) // 4)


def conversation_title(message: str) -> str:
    title = " ".join(message.split())
    return title if len(title) <= 60 else title[:57].rstrip() + "…"


def context_key(conversation_id: str) -> str:
    return f"ctx:{conversation_id}"


_openai: AsyncOpenAI | None = None


def get_openai() -> AsyncOpenAI:
    global _openai
    if _openai is None:
        _openai = AsyncOpenAI(api_key=get_settings().openai_api_key)
    return _openai


# Les tâches de fond sont référencées ici pour ne pas être collectées en cours
# de route, et pour pouvoir les attendre à l'arrêt du serveur.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def wait_background_tasks() -> None:
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)


# --- Contexte court -------------------------------------------------------------

async def load_context(
    redis: Redis, supabase: SupabaseAdmin, conversation_id: str, settings: Settings
) -> list[dict]:
    key = context_key(conversation_id)
    try:
        cached = await redis.lrange(key, 0, -1)
    except RedisError:
        logger.warning("Redis indisponible pour le contexte", exc_info=True)
        cached = None
    if cached:
        return [json.loads(item) for item in cached]

    rows = await supabase.get_recent_messages(conversation_id, settings.chat_history_messages)
    history = [
        {"role": row["role"], "content": row["content"]}
        for row in rows
        if row["role"] in ("user", "assistant")
    ]
    if history:
        try:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.rpush(key, *(json.dumps(m, ensure_ascii=False) for m in history))
                pipe.expire(key, settings.chat_context_ttl)
                await pipe.execute()
        except RedisError:
            logger.warning("Impossible de mettre en cache le contexte", exc_info=True)
    return history


async def append_context(
    redis: Redis, conversation_id: str, new_conversation: bool, messages: list[dict], settings: Settings
) -> None:
    key = context_key(conversation_id)
    # Si le cache a expiré, on ne le recrée pas avec 2 messages seulement :
    # le prochain appel le rechargera complet depuis Supabase.
    if not new_conversation and not await redis.exists(key):
        return
    async with redis.pipeline(transaction=True) as pipe:
        pipe.rpush(key, *(json.dumps(m, ensure_ascii=False) for m in messages))
        pipe.ltrim(key, -settings.chat_history_messages, -1)
        pipe.expire(key, settings.chat_context_ttl)
        await pipe.execute()


# --- Finalisation (tâche de fond) -------------------------------------------------

async def finalize_exchange(
    *,
    quota_service: QuotaService,
    supabase: SupabaseAdmin,
    redis: Redis,
    settings: Settings,
    reservation: QuotaReservation,
    conversation_id: str,
    new_conversation: bool,
    user_row: dict,
    assistant_id: str,
    text: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    if not text:
        await quota_service.release(reservation)
        return

    assistant_row = {
        "id": assistant_id,
        "conversation_id": conversation_id,
        "user_id": reservation.user.id,
        "role": "assistant",
        "content": text,
        "model": settings.openai_model,
        "tokens_input": prompt_tokens,
        "tokens_output": completion_tokens,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await supabase.insert_messages([user_row, assistant_row])
    except httpx.HTTPError:
        logger.error("Échec d'enregistrement des messages (conversation=%s)", conversation_id, exc_info=True)

    await quota_service.record_usage(reservation, prompt_tokens + completion_tokens)

    try:
        await append_context(
            redis,
            conversation_id,
            new_conversation,
            [
                {"role": "user", "content": user_row["content"]},
                {"role": "assistant", "content": text},
            ],
            settings,
        )
    except RedisError:
        logger.warning("Échec de mise à jour du contexte Redis", exc_info=True)


async def _finalize_safely(**kwargs) -> None:
    try:
        await finalize_exchange(**kwargs)
    except Exception:
        logger.exception("Erreur lors de la finalisation d'un échange")


# --- Route --------------------------------------------------------------------------

@router.post("/stream")
async def chat_stream(
    body: ChatRequest,
    reservation: QuotaReservation = Depends(enforce_quota),
    quota_service: QuotaService = Depends(get_quota_service),
    supabase: SupabaseAdmin = Depends(get_supabase),
    redis: Redis = Depends(get_redis),
    openai: AsyncOpenAI = Depends(get_openai),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    user_id = reservation.user.id
    now = datetime.now(timezone.utc)

    # Préparation : tout échec ici rend la requête au quota.
    try:
        if body.conversation_id is not None:
            conversation = await supabase.get_conversation(str(body.conversation_id), user_id)
            if conversation is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation introuvable")
            new_conversation = False
            history = await load_context(redis, supabase, conversation["id"], settings)
        else:
            conversation = await supabase.create_conversation(user_id, conversation_title(body.message))
            new_conversation = True
            history = []
    except HTTPException:
        await quota_service.release(reservation)
        raise
    except httpx.HTTPError:
        await quota_service.release(reservation)
        logger.error("Supabase indisponible pendant la préparation du chat", exc_info=True)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Base de données indisponible")

    conversation_id = conversation["id"]
    user_row = {
        "id": str(uuid4()),
        "conversation_id": conversation_id,
        "user_id": user_id,
        "role": "user",
        "content": body.message,
        "created_at": now.isoformat(),
    }
    assistant_id = str(uuid4())
    prompt = [
        {"role": "system", "content": build_system_prompt(body.ton, conversation.get("persona"), now)},
        *history,
        {"role": "user", "content": body.message},
    ]

    async def event_stream():
        parts: list[str] = []
        usage = None
        try:
            yield sse("meta", {
                "conversation_id": conversation_id,
                "message_id": assistant_id,
                "quota": reservation.status.to_dict(),
            })

            stream = await openai.chat.completions.create(
                model=settings.openai_model,
                messages=prompt,
                max_tokens=settings.chat_max_output_tokens,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if chunk.usage is not None:
                    usage = chunk.usage
                if chunk.choices:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        parts.append(delta)
                        yield sse("delta", {"text": delta})

            text = "".join(parts)
            prompt_tokens, completion_tokens = _token_counts(usage, prompt, text)
            final_quota = replace(
                reservation.status,
                tokens_used=reservation.status.tokens_used + prompt_tokens + completion_tokens,
            )
            yield sse("done", {
                "message_id": assistant_id,
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
                "quota": final_quota.to_dict(),
            })
        except Exception as exc:
            logger.exception("Échec de génération (conversation=%s)", conversation_id)
            yield sse("error", describe_llm_error(exc))
        finally:
            # Exécuté aussi si le client se déconnecte en plein flux :
            # la réponse partielle est enregistrée et facturée.
            text = "".join(parts)
            prompt_tokens, completion_tokens = _token_counts(usage, prompt, text)
            _spawn(_finalize_safely(
                quota_service=quota_service,
                supabase=supabase,
                redis=redis,
                settings=settings,
                reservation=reservation,
                conversation_id=conversation_id,
                new_conversation=new_conversation,
                user_row=user_row,
                assistant_id=assistant_id,
                text=text,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            **reservation.status.headers(),
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # désactive le buffering nginx / proxies
        },
    )


def _token_counts(usage, prompt: list[dict], text: str) -> tuple[int, int]:
    if usage is not None:
        return usage.prompt_tokens, usage.completion_tokens
    prompt_chars = "".join(m["content"] for m in prompt)
    return estimate_tokens(prompt_chars), (estimate_tokens(text) if text else 0)
