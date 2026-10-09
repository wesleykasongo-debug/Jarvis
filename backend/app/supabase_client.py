"""Client administrateur Supabase (PostgREST) utilisé par le backend.

Authentifié avec la clé `service_role` : il contourne la RLS, il ne doit donc
servir qu'à des opérations décidées côté serveur (lecture des limites, écriture
de la consommation, messages assistant). On passe par httpx directement plutôt
que par le SDK `supabase` : appels async natifs, peu de dépendances, et un
contrôle total des timeouts.
"""

from dataclasses import dataclass
from datetime import date

import httpx


@dataclass(frozen=True)
class UserLimits:
    plan: str
    daily_message_limit: int | None  # None = illimité
    daily_token_limit: int | None


class SupabaseAdmin:
    def __init__(self, url: str, service_role_key: str, *, timeout: float = 5.0) -> None:
        headers = {"apikey": service_role_key, "Content-Type": "application/json"}
        # Clé legacy (JWT `service_role`) : aussi en Bearer. Les nouvelles clés
        # secrètes (`sb_secret_...`) ne sont pas des JWT : `apikey` seul.
        if not service_role_key.startswith("sb_"):
            headers["Authorization"] = f"Bearer {service_role_key}"
        self._http = httpx.AsyncClient(
            base_url=f"{url.rstrip('/')}/rest/v1",
            headers=headers,
            timeout=timeout,
        )

    async def get_user_limits(self, user_id: str) -> UserLimits | None:
        resp = await self._http.get(
            "/users",
            params={
                "id": f"eq.{user_id}",
                "select": "plan,daily_message_limit,daily_token_limit",
            },
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        row = rows[0]
        return UserLimits(
            plan=row["plan"],
            daily_message_limit=row["daily_message_limit"],
            daily_token_limit=row["daily_token_limit"],
        )

    async def get_daily_usage(self, user_id: str, day: date) -> tuple[int, int]:
        """Renvoie (messages_used, tokens_used) pour le jour UTC donné."""
        resp = await self._http.get(
            "/quota_usage",
            params={
                "user_id": f"eq.{user_id}",
                "usage_date": f"eq.{day.isoformat()}",
                "select": "messages_used,tokens_used",
            },
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return 0, 0
        return int(rows[0]["messages_used"]), int(rows[0]["tokens_used"])

    async def increment_quota_usage(self, user_id: str, messages: int, tokens: int) -> None:
        resp = await self._http.post(
            "/rpc/increment_quota_usage",
            json={"p_user_id": user_id, "p_messages": messages, "p_tokens": tokens},
        )
        resp.raise_for_status()

    async def ping(self) -> None:
        resp = await self._http.get("/users", params={"select": "id", "limit": "1"})
        resp.raise_for_status()

    async def set_user_plan(
        self, user_id: str, plan: str, daily_message_limit: int | None, daily_token_limit: int | None
    ) -> None:
        resp = await self._http.patch(
            "/users",
            params={"id": f"eq.{user_id}"},
            json={
                "plan": plan,
                "daily_message_limit": daily_message_limit,
                "daily_token_limit": daily_token_limit,
            },
        )
        resp.raise_for_status()

    async def find_user_id(self, email: str) -> str | None:
        resp = await self._http.get("/users", params={"email": f"eq.{email}", "select": "id"})
        resp.raise_for_status()
        rows = resp.json()
        return rows[0]["id"] if rows else None

    # Conversations & messages --------------------------------------------------

    async def get_conversation(self, conversation_id: str, user_id: str) -> dict | None:
        """La conversation, seulement si elle appartient à `user_id`."""
        resp = await self._http.get(
            "/conversations",
            params={
                "id": f"eq.{conversation_id}",
                "user_id": f"eq.{user_id}",
                "select": "id,title,persona",
            },
        )
        resp.raise_for_status()
        rows = resp.json()
        return rows[0] if rows else None

    async def create_conversation(self, user_id: str, title: str) -> dict:
        resp = await self._http.post(
            "/conversations",
            json={"user_id": user_id, "title": title},
            params={"select": "id,title,persona"},
            headers={"Prefer": "return=representation"},
        )
        resp.raise_for_status()
        return resp.json()[0]

    async def get_recent_messages(self, conversation_id: str, limit: int) -> list[dict]:
        """Les `limit` derniers messages, du plus ancien au plus récent."""
        resp = await self._http.get(
            "/messages",
            params={
                "conversation_id": f"eq.{conversation_id}",
                "select": "role,content",
                "order": "created_at.desc",
                "limit": str(limit),
            },
        )
        resp.raise_for_status()
        return list(reversed(resp.json()))

    async def insert_messages(self, rows: list[dict]) -> None:
        resp = await self._http.post(
            "/messages",
            json=rows,
            headers={"Prefer": "return=minimal"},
        )
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._http.aclose()


_supabase: SupabaseAdmin | None = None


def init_supabase(url: str, service_role_key: str) -> SupabaseAdmin:
    global _supabase
    _supabase = SupabaseAdmin(url, service_role_key)
    return _supabase


def get_supabase() -> SupabaseAdmin:
    if _supabase is None:
        raise RuntimeError("Supabase non initialisé : appeler init_supabase() au démarrage.")
    return _supabase


async def close_supabase() -> None:
    global _supabase
    if _supabase is not None:
        await _supabase.aclose()
        _supabase = None
