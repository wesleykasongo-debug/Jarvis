from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str
    supabase_service_role_key: str
    supabase_jwt_secret: str | None = None
    supabase_jwt_audience: str = "authenticated"

    redis_url: str = "redis://localhost:6379/0"

    quota_limits_cache_ttl: int = 300

    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    chat_max_output_tokens: int = 1024
    chat_history_messages: int = 20
    chat_context_ttl: int = 3600

    # Origines autorisées pour Flutter Web (liste JSON dans le .env)
    cors_origins: list[str] = ["http://localhost:3000"]

    log_level: str = "INFO"

    @property
    def supabase_issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    @property
    def supabase_jwks_url(self) -> str:
        return f"{self.supabase_issuer}/.well-known/jwks.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
