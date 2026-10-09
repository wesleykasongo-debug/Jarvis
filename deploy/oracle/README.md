# JARVIS 24h/24 sur Oracle Cloud (gratuit)

Une machine virtuelle **Always Free** d'Oracle fait tourner en continu :
**Caddy** (HTTPS automatique) → **API FastAPI** → **Redis**, avec Supabase
comme base de données. Coût : 0 €.

Durée : ~30 minutes la première fois.

---

## 1. Créer le compte Oracle Cloud

1. https://signup.cloud.oracle.com → remplissez le formulaire.
2. **Home Region** : choisissez une région proche, par ex. *Germany Central
   (Frankfurt)* ou *France Central (Paris)*. **Elle ne pourra plus être changée.**
3. Une carte bancaire est demandée pour vérifier l'identité : rien n'est débité
   tant que vous restez dans les ressources « Always Free ».

> **Conseil important** : une fois le compte créé, passez-le en
> **Pay As You Go** (Billing → Upgrade). Les ressources Always Free restent
> gratuites, mais :
> - Oracle **ne récupère plus** les VM jugées inactives (sur un compte gratuit,
>   une VM peu utilisée pendant 7 jours peut être arrêtée) ;
> - les erreurs « Out of capacity » à la création deviennent rares.
>
> Créez alors une **alerte budget à 1 €** (Billing → Budgets) pour être prévenu
> au moindre centime.

## 2. Créer la machine virtuelle

**Menu ☰ → Compute → Instances → Create instance**

| Champ | Valeur |
|---|---|
| Name | `jarvis` |
| Image | **Canonical Ubuntu 24.04** |
| Shape | **Ampere → VM.Standard.A1.Flex**, 2 OCPU, 12 Go RAM (gratuit jusqu'à 4 OCPU / 24 Go) |
| Networking | *Create new virtual cloud network*, sous-réseau **public**, **Assign a public IPv4 address** coché |
| SSH keys | **Generate a key pair for me** → **Save private key** (fichier `.key`, à garder précieusement) |

**Create**. Après 1 à 2 minutes, l'instance est *Running* : notez son
**Public IP address**.

> « Out of capacity for shape VM.Standard.A1.Flex » : réessayez plus tard, ou
> changez d'*Availability Domain*, ou choisissez provisoirement la shape
> *VM.Standard.E2.1.Micro* (AMD, 1 Go, gratuite aussi : le script ajoute du swap).

## 3. Ouvrir les ports 80 et 443

Page de l'instance → **Subnet** (lien) → **Security Lists** → *Default Security List*
→ **Add Ingress Rules**, deux fois :

| Source CIDR | IP Protocol | Destination Port Range |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

(Le pare-feu interne de la VM est ouvert automatiquement par le script.)

## 4. Se connecter à la VM depuis Windows

Dans PowerShell, en adaptant le chemin de la clé et l'IP :

```powershell
# Une seule fois : la clé doit n'être lisible que par vous, sinon SSH la refuse
icacls "C:\Users\VOUS\Downloads\ssh-key.key" /inheritance:r /grant:r "$($env:USERNAME):(R)"

ssh -i "C:\Users\VOUS\Downloads\ssh-key.key" ubuntu@IP_PUBLIQUE
```

Répondez `yes` à la première connexion.

## 5. Installer JARVIS (une commande)

Sur la VM :

```bash
curl -fsSLO https://raw.githubusercontent.com/wesleykasongo-debug/Jarvis/main/deploy/oracle/install.sh
sudo bash install.sh
```

Le script :
1. installe Docker et ouvre le pare-feu de la VM ;
2. télécharge le code depuis GitHub dans `/opt/jarvis` ;
3. vous demande le **domaine** — appuyez sur Entrée pour l'adresse gratuite
   proposée, du type `141-147-12-34.sslip.io` (elle pointe vers votre IP) ;
4. vous demande vos **clés** (celles de `backend/.env` sur votre PC) — les
   secrets ne s'affichent pas pendant la saisie ;
5. démarre tout et obtient le certificat HTTPS.

À la fin : `JARVIS est en ligne 24h/24.` avec vos adresses :
- `https://<domaine>/health/ready` → `{"status":"ok","redis":"ok","supabase":"ok"}`
- `https://<domaine>/console` → la console de test, depuis n'importe quel appareil.

## 6. Dernier réglage Supabase

**Authentication → URL Configuration → Site URL** : `https://<domaine>/console`
(les liens des e-mails de confirmation pointeront vers votre serveur).

---

## Au quotidien

| Action | Commande (sur la VM) |
|---|---|
| Mettre à jour après un `git push` | `sudo bash /opt/jarvis/deploy/oracle/update.sh` |
| Voir les logs en direct | `sudo docker compose -f /opt/jarvis/deploy/oracle/docker-compose.yml logs -f api` |
| État des services | `sudo docker compose -f /opt/jarvis/deploy/oracle/docker-compose.yml ps` |
| Modifier une clé | `sudo nano /opt/jarvis/backend/.env` puis `update.sh` |
| Redémarrer | `sudo docker compose -f /opt/jarvis/deploy/oracle/docker-compose.yml restart` |

Les services redémarrent seuls après une panne ou un redémarrage de la VM
(`restart: unless-stopped`), et le certificat HTTPS se renouvelle seul.

## Votre propre nom de domaine (optionnel)

Créez un enregistrement DNS **A** `api.mondomaine.com → IP_PUBLIQUE`, puis sur la VM :

```bash
sudo nano /opt/jarvis/deploy/oracle/.env              # DOMAIN=api.mondomaine.com
sudo nano /opt/jarvis/backend/.env                    # CORS_ORIGINS avec le nouveau domaine
sudo bash /opt/jarvis/deploy/oracle/update.sh
```

Domaine gratuit possible avec https://www.duckdns.org (`monjarvis.duckdns.org`).
