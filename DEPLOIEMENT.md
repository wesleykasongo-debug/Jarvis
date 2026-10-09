# Déploiement gratuit de JARVIS

## Deux options

| | **Oracle Cloud Always Free** (recommandé) | **Render + Upstash** |
|---|---|---|
| Disponibilité | **24h/24**, jamais en veille | veille après 15 min (réveil ~50 s) |
| Ressources | jusqu'à 4 cœurs ARM, 24 Go RAM | 0,1 cœur, 512 Mo |
| Redis | sur la VM (illimité) | Upstash, 500 000 commandes/mois |
| Mise en place | ~30 min, carte bancaire de vérification | ~15 min, sans carte |
| Guide | **[deploy/oracle/README.md](deploy/oracle/README.md)** | ci-dessous |

---

# Option Render + Upstash

| Brique | Service gratuit | Rôle |
|---|---|---|
| Base de données, Auth, Storage | **Supabase** (déjà en place) | profils, conversations, messages |
| Cache et quotas | **Upstash Redis** | compteurs de quota, contexte court |
| API FastAPI | **Render** (Docker) | auth JWT, quotas, chat SSE, console |
| App Flutter Web (plus tard) | **Vercel** ou Cloudflare Pages | site statique `flutter build web` |

## Pourquoi pas Vercel pour l'API ?

Vercel exécute le code en fonctions « serverless » qui s'arrêtent dès la réponse
envoyée. Or l'API JARVIS :
- diffuse des réponses longues en SSE ;
- enregistre la réponse dans Supabase **après** la fin du flux (tâche de fond) ;
- garde une connexion Redis ouverte.

Sur Vercel, la sauvegarde de fin de flux serait coupée. Render fait tourner le
conteneur Docker tel quel, comme sur votre PC. Vercel reste parfait pour
l'app Flutter Web.

---

## Étape 1 — Redis sur Upstash (5 min)

1. Créez un compte sur https://upstash.com (connexion GitHub possible).
2. **Create Database** :
   - Name : `jarvis`
   - Type : **Regional**, région **eu-central-1 (Frankfurt)** (même région que l'API)
   - TLS : activé
3. Dans la page de la base, section **Connect**, copiez l'URL qui commence par
   `rediss://default:...@....upstash.io:6379` → c'est votre `REDIS_URL`.

## Étape 2 — API sur Render (10 min)

1. Créez un compte sur https://render.com avec **Sign in with GitHub**.
2. **New → Blueprint**, autorisez l'accès au dépôt `wesleykasongo-debug/Jarvis`.
3. Render lit `render.yaml` et demande les secrets (valeurs de `backend/.env`) :

   | Variable | Valeur |
   |---|---|
   | `SUPABASE_URL` | `https://<ref>.supabase.co` |
   | `SUPABASE_SERVICE_ROLE_KEY` | clé `sb_secret_...` |
   | `SUPABASE_PUBLISHABLE_KEY` | clé `sb_publishable_...` |
   | `OPENAI_API_KEY` | clé `sk-...` |
   | `REDIS_URL` | URL Upstash de l'étape 1 |

4. **Apply**. Le premier build prend 3 à 5 minutes.
5. Votre API est en ligne sur `https://jarvis-api-xxxx.onrender.com` :
   - `/health/ready` doit répondre `{"status":"ok","redis":"ok","supabase":"ok"}`
   - `/console` : la console de test, utilisable depuis n'importe quel appareil.

Chaque `git push` sur `main` qui modifie `backend/` redéploie automatiquement.

## Étape 3 — Supabase : adresse du site

Les e-mails de confirmation renvoient vers le « Site URL » de Supabase.
**Authentication → URL Configuration** :
- Site URL : `https://jarvis-api-xxxx.onrender.com/console`
- Redirect URLs : ajoutez aussi `http://localhost:8000/console`

## Étape 4 — Protéger le crédit OpenAI

Les inscriptions sont ouvertes : chaque compte a 30 messages par jour.
Sur https://platform.openai.com/settings/organization/limits, fixez un
**budget mensuel** (ex. 5 $) pour ne jamais dépasser ce que vous avez prévu.

---

## Limites des offres gratuites

| Service | Limite | Conséquence |
|---|---|---|
| Render Free | mise en veille après 15 min sans requête | la 1re requête suivante attend ~50 s |
| Render Free | 750 h/mois | suffit pour un service allumé en continu |
| Upstash Free | 500 000 commandes/mois | ~ 40 000 messages de chat |
| Supabase Free | pause après 7 jours sans activité | réactivation en 1 clic dans le dashboard |

**Éviter la mise en veille (optionnel)** : créez un moniteur gratuit sur
https://uptimerobot.com qui appelle `https://jarvis-api-xxxx.onrender.com/health`
toutes les 10 minutes.

## Avant d'ouvrir au public

- Régénérer les clés `sb_secret_...` et `sk-...` (elles ont circulé hors du
  serveur) et les remplacer dans Render → Environment.
- Une fois l'app Flutter prête : `ENABLE_CONSOLE=false` et mettre le domaine
  de l'app web dans `CORS_ORIGINS` (ex. `["https://jarvis.vercel.app"]`).
- Réactiver « Confirm email » dans Supabase si vous l'aviez désactivé.

## Plus tard — Flutter Web sur Vercel

```
flutter build web --release
```
Puis déposer le dossier `build/web` sur Vercel (`vercel deploy build/web --prod`)
ou Cloudflare Pages, avec l'URL Render comme `baseUrl` du `MoteurConversationIA`.
