# Jarvis

Assistant personnel cross-platform (Flutter : Android, iOS, Web, Windows) avec mode
vocal et écrit. Ce dépôt contient la couche de données et le backend.

```
jarvis/
├── GUIDE_DEV.md          # Guide complet pour installer et contribuer au projet
├── lancer_jarvis.bat     # démarre tout (Docker : Redis + API)
├── arreter_jarvis.bat    # arrête tout
├── tester_jarvis.bat     # test de bout en bout (connexion, quota, chat)
├── supabase/
│   └── schema.sql        # tables, RLS, quotas, bucket Storage
├── backend/              # API FastAPI : auth JWT Supabase, quotas Redis, chat SSE
└── flutter/lib/          # fichiers à intégrer dans l'app Flutter
```

## Architecture

- **Supabase** (PostgreSQL + Auth + Storage) : utilisateurs, paliers et limites,
  conversations, messages, documents, trace de consommation (`quota_usage`).
- **Redis** : compteurs de quota temps réel (vérification atomique en Lua),
  contexte court des conversations.
- **FastAPI** : valide le JWT Supabase, applique les quotas, diffuse les réponses
  `gpt-4o-mini` en SSE et les enregistre dans Supabase.

Quotas : **Free** = 30 messages et 50 000 tokens par jour, **Pro** = sur-mesure ou
illimité. Remise à zéro à minuit UTC.

## Installation

Prérequis : [Docker Desktop](https://www.docker.com/products/docker-desktop/),
un projet [Supabase](https://supabase.com), une clé [OpenAI](https://platform.openai.com).
Python 3.12+ seulement pour les tests et scripts.

1. **Supabase** : SQL Editor → New query → coller tout `supabase/schema.sql` → Run.
2. **Configuration** : lancer `lancer_jarvis.bat` une première fois ; il crée
   `backend/.env` et l'ouvre. Renseigner :
   - `SUPABASE_URL` : `https://<ref>.supabase.co`
   - `SUPABASE_SERVICE_ROLE_KEY` : clé secrète (`sb_secret_...`)
   - `SUPABASE_PUBLISHABLE_KEY` : clé publique (`sb_publishable_...`)
   - `OPENAI_API_KEY`
3. **Démarrer** : double-cliquer sur `lancer_jarvis.bat`.
   L'API répond sur http://localhost:8000 (documentation : `/docs`).
4. **Tester** : créer un utilisateur dans Supabase (Authentication → Users → Add user,
   « Auto Confirm User »), puis lancer `tester_jarvis.bat`.

Le fichier `backend/.env` contient des secrets : il est exclu du dépôt par `.gitignore`.

Mise en ligne gratuite 24h/24 (Oracle Cloud) ou Render : [DEPLOIEMENT.md](DEPLOIEMENT.md).

Détails du backend (routes, scripts d'administration, tests, production) :
[backend/README.md](backend/README.md).

## Flutter

Les fichiers de `flutter/lib/` s'intègrent dans le projet Flutter existant :

| Fichier | Rôle |
|---|---|
| `models/user_quota.dart` | modèle du quota journalier |
| `services/supabase_service.dart` | authentification, jeton JWT, données, Storage |
| `services/moteur_conversation_ia.dart` | envoi des messages et réception SSE |
| `widgets/jauge_quota_widget.dart` | jauge discrète de consommation |

Dépendances : voir `flutter/pubspec_dependencies.yaml`.
