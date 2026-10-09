"""Console de test : mini-interface de chat servie par l'API (remplace Flutter
pour les essais). Désactivable avec ENABLE_CONSOLE=false.

Elle ne contient aucun secret : la connexion se fait avec la clé publique
Supabase, puis la console appelle l'API comme le ferait l'app Flutter.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from .config import Settings, get_settings

_PAGE = Path(__file__).parent / "static" / "console.html"

router = APIRouter(prefix="/console", include_in_schema=False)


def _require_enabled(settings: Settings = Depends(get_settings)) -> Settings:
    if not settings.enable_console:
        raise HTTPException(404)
    return settings


@router.get("")
async def console_page(_: Settings = Depends(_require_enabled)) -> FileResponse:
    return FileResponse(_PAGE, headers={"Cache-Control": "no-store"})


@router.get("/config")
async def console_config(settings: Settings = Depends(_require_enabled)) -> dict:
    return {
        "supabase_url": settings.supabase_url.rstrip("/"),
        "publishable_key": settings.supabase_publishable_key,
        "openai_configured": bool(settings.openai_api_key.strip()),
        "model": settings.openai_model,
    }
