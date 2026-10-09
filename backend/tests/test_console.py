from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

# Sans `with`, le lifespan (Redis, Supabase) n'est pas lancé : inutile ici.
client = TestClient(app)


def test_console_page_served():
    resp = client.get("/console")
    assert resp.status_code == 200
    assert "JARVIS" in resp.text


def test_console_config_exposes_no_secret():
    data = client.get("/console/config").json()
    settings = get_settings()
    assert set(data) == {"supabase_url", "publishable_key", "openai_configured", "model"}
    assert settings.supabase_service_role_key not in str(data)
    assert settings.openai_api_key not in str(data)


def test_console_can_be_disabled():
    settings = get_settings()
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update={"enable_console": False})
    try:
        assert client.get("/console").status_code == 404
        assert client.get("/console/config").status_code == 404
    finally:
        app.dependency_overrides.clear()
