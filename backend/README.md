# JARVIS — Backend FastAPI

API de chat (SSE, `gpt-4o-mini`) avec authentification Supabase et quotas
journaliers Redis. Le schéma de base est dans `../supabase/schema.sql`.

## Démarrage

```
copy .env.example .env        # puis remplir les clés
docker compose up -d          # Redis + API sur http://localhost:8000
```

Développement avec rechargement automatique :

```
docker compose up -d redis
python -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\uvicorn app.main:app --reload
```

Après une modification du `.env` avec Docker : `docker compose up -d --force-recreate api`.

## Routes

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/health` | le processus répond |
| GET | `/health/ready` | Redis et Supabase joignables (503 sinon) |
| GET | `/me` | utilisateur du JWT |
| GET | `/me/quota` | quota du jour (temps réel) |
| POST | `/api/v1/chat/stream` | chat SSE : `{message, conversation_id?, ton}` |

Documentation interactive : http://localhost:8000/docs (bouton **Authorize** pour coller un JWT).

## Scripts

```
.\.venv\Scripts\python -m scripts.smoke_test                       # test de bout en bout
.\.venv\Scripts\python -m scripts.set_plan email@x.com pro          # Pro illimité
.\.venv\Scripts\python -m scripts.set_plan email@x.com pro --messages 500 --tokens 1000000
.\.venv\Scripts\python -m scripts.set_plan email@x.com free
```

## Tests

```
.\.venv\Scripts\python -m pytest                                    # Redis simulé
$env:REDIS_TEST_URL="redis://localhost:6379/15"; .\.venv\Scripts\python -m pytest   # vrai Redis
```

## Production

- `REDIS_URL` -> Upstash (`rediss://...`)
- `CORS_ORIGINS` -> domaine(s) réel(s) de l'app web
- Image : `docker build -t jarvis-api .` (écoute sur `$PORT`, 8000 par défaut)
- La clé secrète Supabase ne quitte jamais le serveur.
