"""Change le palier d'un utilisateur et applique le changement immédiatement.

Usage (depuis jarvis/backend) :
  python -m scripts.set_plan tony@stark.dev pro                  # Pro illimité
  python -m scripts.set_plan tony@stark.dev pro --messages 500 --tokens 1000000
  python -m scripts.set_plan tony@stark.dev free                 # retour aux limites Free

Met à jour `public.users` dans Supabase puis supprime le cache Redis des
limites : pas besoin d'attendre les 5 minutes du cache.
"""

import argparse
import asyncio
import sys

from redis.asyncio import Redis

from app.config import get_settings
from app.quota import limits_key
from app.supabase_client import SupabaseAdmin

FREE_LIMITS = (30, 50_000)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Changer le palier d'un utilisateur JARVIS")
    parser.add_argument("email")
    parser.add_argument("plan", choices=["free", "pro"])
    parser.add_argument("--messages", type=int, help="limite de messages/jour (Pro sur-mesure)")
    parser.add_argument("--tokens", type=int, help="limite de tokens/jour (Pro sur-mesure)")
    args = parser.parse_args()

    if args.plan == "free":
        messages, tokens = FREE_LIMITS
    else:
        messages, tokens = args.messages, args.tokens  # None = illimité

    settings = get_settings()
    supabase = SupabaseAdmin(settings.supabase_url, settings.supabase_service_role_key)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        user_id = await supabase.find_user_id(args.email)
        if user_id is None:
            print(f"Aucun utilisateur avec l'e-mail {args.email}", file=sys.stderr)
            return 1
        await supabase.set_user_plan(user_id, args.plan, messages, tokens)
        await redis.delete(limits_key(user_id))
    finally:
        await supabase.aclose()
        await redis.aclose()

    def fmt(v):
        return "illimité" if v is None else v

    print(f"{args.email} ({user_id}) -> {args.plan} : messages {fmt(messages)}, tokens {fmt(tokens)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
