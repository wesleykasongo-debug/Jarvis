# 🚀 Guide Développeur — Prise en main rapide de JARVIS

Bienvenue sur le projet **JARVIS** ! Les clés d'API et la base de données étant déjà partagées et configurées dans le projet, tu peux lancer le projet et commencer à coder en **moins de 2 minutes**.

---

## ⚡ En résumé : comment démarrer tout de suite

1. Installe **[Docker Desktop](https://www.docker.com/products/docker-desktop/)** et lance-le.
2. Assure-toi que le fichier `backend/.env` est bien présent à côté de ce guide avec toutes nos clés.
3. Double-clique sur **`lancer_jarvis.bat`** (ou lance `docker compose up -d --build` dans `backend/`).
4. Ouvre [http://localhost:8000/console](http://localhost:8000/console) pour tester le chat !

---

## 📋 1. Prérequis sur ton PC

| Outil | Utilité | Installation |
|---|---|---|
| **Docker Desktop** *(Indispensable)* | Démarre l'API FastAPI et Redis sans rien avoir à installer manuellement | [Télécharger Docker Desktop](https://www.docker.com/products/docker-desktop/) *(Activer WSL 2 sous Windows)* |
| **Python 3.12+** *(Recommandé)* | Pour lancer les tests automatisés ou modifier le backend localement | [Télécharger Python](https://python.org/downloads/) |
| **Flutter SDK** *(Pour le client)* | Pour compiler l'application mobile (Android/iOS) ou PC (Windows/Web) | [Documentation Flutter](https://docs.flutter.dev/get-started/install) |

---

## 🔑 2. Clés & Services (Déjà configurés)

Le fichier `backend/.env` contient déjà l'ensemble de nos accès partagés :
* **Supabase** (Projet distant : `aacguimngsksfmmwonby.supabase.co`) : Base de données, authentification des comptes, quotas et stockage.
* **Groq API** : Fournisseur IA ultra-rapide pour le modèle `qwen/qwen3.8-27b`.
* **Redis** : Cache temps réel (tourne automatiquement dans Docker).

> [!NOTE]
> Tu n'as pas besoin de recréer de compte Supabase ou Groq : tout est déjà connecté au même environnement de travail.

---

## 🏃 3. Démarrer et arrêter le projet

### Démarrage
* **Sur Windows :** Double-clique sur **`lancer_jarvis.bat`** à la racine.
* **En ligne de commande (Windows / macOS / Linux) :**
  ```bash
  cd backend
  docker compose up -d --build
  ```

### Arrêt
* **Sur Windows :** Double-clique sur **`arreter_jarvis.bat`**.
* **En ligne de commande :**
  ```bash
  cd backend
  docker compose down
  ```

---

## 🧪 4. Comment tester et vérifier le fonctionnement

Dès que Docker tourne :

1. **Vérifier la santé du backend :**
   Ouvre [http://localhost:8000/health/ready](http://localhost:8000/health/ready). Tu dois obtenir :
   ```json
   {"status": "ok", "redis": "ok", "supabase": "ok"}
   ```

2. **Tester le chat en direct (Console Web) :**
   Ouvre **[http://localhost:8000/console](http://localhost:8000/console)**.
   * Connecte-toi avec un compte utilisateur Supabase (ou crée-en un dans la console/Supabase).
   * Envoie un message pour voir la réponse de l'IA en streaming et la jauge de quotas se décompter.

3. **Documentation interactive de l'API :**
   Ouvre **[http://localhost:8000/docs](http://localhost:8000/docs)** (Swagger UI) pour voir toutes les routes HTTP, les requêtes acceptées et les réponses.

4. **Script de test complet :**
   Double-clique sur **`tester_jarvis.bat`** pour tester la chaîne complète (Connexion → Quota → Chat) dans un terminal.

---

## 🏗️ 5. Structure du projet

```
jarvis/
├── GUIDE_DEV.md            # Ce guide
├── lancer_jarvis.bat       # Démarrage automatique (Docker)
├── arreter_jarvis.bat      # Arrêt automatique (Docker)
├── tester_jarvis.bat       # Test de bout en bout
├── supabase/
│   └── schema.sql          # Schéma PostgreSQL, tables, RLS et quotas
├── backend/                # Serveur FastAPI
│   ├── .env                # Variables d'environnement et clés d'API partagées
│   ├── docker-compose.yml  # Orchestration de l'API et de Redis
│   ├── app/
│   │   ├── api/v1/chat.py  # Moteur de chat, prompt Jarvis, streaming SSE
│   │   ├── quota.py        # Gestion des quotas par utilisateur (Redis Lua)
│   │   ├── auth.py         # Validation des tokens JWT Supabase
│   │   ├── config.py       # Configuration du backend
│   │   └── console.py      # Console HTML de test (/console)
│   └── scripts/            # Scripts de test automatisés
└── flutter/lib/            # Code Dart prêt à intégrer dans l'app Flutter
    ├── models/             # Modèles de données (UserQuota...)
    ├── services/           # SupabaseService, MoteurConversationIA (SSE)
    └── widgets/            # JaugeQuotaWidget...
```

---

## 🎯 6. Ta mission : les axes de perfectionnement du projet

Voici les principales fonctionnalités à développer et améliorer ensemble :

### 1. Interface Graphique Flutter (`flutter/lib/`)
* Finaliser l'interface mobile/desktop du chat (bulles de message, saisie fluide, auto-scroll).
* Afficher la jauge de quota en temps réel dans l'interface (`widgets/jauge_quota_widget.dart`).
* Proposer un sélecteur de ton (Naturel, Amical, Professionnel, Concis, Pédagogue).

### 2. Mode Vocal (Voice Assistant)
* Intégrer la capture microphone dans Flutter et l'envoyer au backend.
* Transcription vocale (STT avec Whisper).
* Synthèse vocale de la réponse de Jarvis (TTS).

### 3. Gestion de Documents & RAG (Supabase Storage)
* Permettre à l'utilisateur d'uploader des fichiers ou PDF dans le bucket `documents`.
* Faire en sorte que Jarvis puisse lire ces documents pour répondre à des questions précises dessus.

### 4. Outils & Actions (Function Calling)
* Donner à Jarvis la capacité d'effectuer des actions : météo, recherche web, rappels, agenda.
