# AutoTiktok

Publie automatiquement tes vidéos, déjà prêtes, sur **TikTok**, **Instagram Reels** et **YouTube Shorts**,
une ou deux fois par jour, depuis ton serveur (cron). Tout passe par les **API officielles** des plateformes :
pas d'abonnement, et pas de risque de bannissement lié à un faux navigateur.

```
input/  ──►  12h47 : une vidéo tirée au hasard → TikTok + Instagram + YouTube → supprimée + notée dans l'archive
        ──►  19h47 : une autre vidéo au hasard…
```

- **Tirage au hasard** parmi les vidéos de `input/`. Une vidéo dont une publication a échoué est retentée en priorité.
- **Suppression** de la vidéo une fois traitée (`after_publish: delete`).
- **Archive anti-doublon** (`state/archive.jsonl`) : chaque vidéo publiée y est notée avec l'empreinte de son contenu (SHA-256).
  Si le même fichier revient dans `input/`, même sous un autre nom, il est ignoré.

Les vidéos de `input/` doivent être les tiennes, ou des vidéos que tu as le droit de publier.

## Ce qui est automatique

| Plateforme | Au départ | Après validation de la plateforme |
|---|---|---|
| **Instagram Reels** | ✅ Publication publique automatique | — |
| **YouTube Shorts** | ⚠️ Vidéos envoyées en **privé** (règle YouTube pour les apps non auditées) | ✅ Public automatique après l'[audit YouTube](#audit-youtube) |
| **TikTok** | ⚠️ Vidéo envoyée dans tes **brouillons TikTok** : tu la publies d'un tap dans l'appli | ✅ Public automatique après l'[audit TikTok](#audit-tiktok) |

Ces audits ne concernent que la publication **par API**, c'est-à-dire par un programme. Ils sont gratuits.

---

## Mise en place (une seule fois, ~1 h)

La connexion des comptes ouvre un navigateur, donc elle se fait **sur ton ordinateur**.
Ensuite, tu copies le fichier de jetons chiffrés sur le serveur.

### 1. Installer (ordinateur et serveur)

Il faut Python 3.11 ou plus récent.

```bash
git clone https://github.com/Poop-Agency/AutoTiktok.git
cd AutoTiktok
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
```

Les valeurs du fichier `.env` sont lues automatiquement par toutes les commandes. Remplis-le sur l'ordinateur comme sur le serveur.

### 2. Clé de chiffrement

Les jetons d'accès à tes comptes sont stockés **chiffrés** dans `state/tokens.enc`. Génère une clé :

```bash
.venv/bin/python -m autotiktok keygen
```

Mets-la dans `TOKENS_KEY=` des deux fichiers `.env`. Garde-la aussi dans un gestionnaire de mots de passe.

### 3. Instagram

1. Dans l'appli Instagram, passe ton compte en **compte professionnel** (Créateur ou Entreprise) :
   Paramètres → Type de compte et outils.
2. Sur [developers.facebook.com](https://developers.facebook.com/), clique sur **Créer une app**.
   Choisis le cas d'usage **« Gérer les messages et le contenu sur Instagram »** (API Instagram avec connexion Instagram).
3. Dans **Rôles de l'app → Rôles**, ajoute ton compte Instagram comme **testeur Instagram**.
   Accepte ensuite l'invitation dans Instagram (Paramètres → Apps et sites web → Invitations de testeur).
4. Dans **Configuration de l'API avec la connexion Instagram**, ajoute ton compte, puis clique sur **Générer un jeton**.
   Les permissions `instagram_business_basic` et `instagram_business_content_publish` doivent être cochées.
5. Lance cette commande avec le jeton :
   ```bash
   .venv/bin/python -m autotiktok auth instagram --token "LE_JETON"
   ```

L'app peut rester en **mode développement** : c'est suffisant pour publier sur ton propre compte.
Le jeton est valable 60 jours et il est renouvelé automatiquement à chaque passage.

### 4. YouTube

1. Sur [console.cloud.google.com](https://console.cloud.google.com/), crée un projet.
   Dans **API et services → Bibliothèque**, active **YouTube Data API v3**.
2. Ouvre **Écran de consentement OAuth** :
   - choisis le type **Externe** ;
   - ajoute le scope `.../auth/youtube.upload` ;
   - ajoute ton adresse Gmail comme utilisateur test ;
   - clique sur **Publier l'application** pour passer « En production ». Une validation Google n'est pas nécessaire pour ton usage perso.
     ⚠️ Si tu restes en mode « Test », la connexion expire au bout de 7 jours.
3. Dans **Identifiants → Créer des identifiants → ID client OAuth**, choisis le type **Application de bureau**.
4. Mets les deux valeurs dans `GOOGLE_CLIENT_ID` et `GOOGLE_CLIENT_SECRET` des deux fichiers `.env`.
5. Lance :
   ```bash
   .venv/bin/python -m autotiktok auth youtube
   ```
   Le navigateur s'ouvre et Google affiche « Application non validée » : clique sur **Paramètres avancés → Accéder à … (non sécurisé)**.
   C'est normal, c'est ta propre app.

### 5. TikTok

1. Sur [developers.tiktok.com](https://developers.tiktok.com/), va dans **Manage apps → Connect an app**.
2. Ajoute les produits **Login Kit** et **Content Posting API**.
3. Dans Login Kit, choisis la plateforme **Desktop** et ajoute l'URL de redirection `http://localhost:8765/callback/`.
4. Passe en **Sandbox** et ajoute ton compte TikTok comme **Target user**.
   Ça permet d'utiliser l'API sans attendre la validation de l'app.
5. Mets le *Client key* et le *Client secret* dans `TIKTOK_CLIENT_KEY` et `TIKTOK_CLIENT_SECRET` des deux fichiers `.env`.
6. Lance :
   ```bash
   .venv/bin/python -m autotiktok auth tiktok
   ```

Si le mode Sandbox ne suffit pas (par exemple si la connexion échoue), soumets l'app à la validation de TikTok
(*Submit for review*) avec la page décrite dans [Audit TikTok](#audit-tiktok).

### 6. Copier les jetons sur le serveur et lancer le cron

```bash
scp state/tokens.enc utilisateur@serveur:AutoTiktok/state/tokens.enc
```

Sur le serveur, fais d'abord un essai sans rien envoyer :

```bash
.venv/bin/python -m autotiktok publish-next --dry-run
```

Puis ajoute le passage automatique avec `crontab -e` :

```
47 12,19 * * * /home/utilisateur/AutoTiktok/scripts/run.sh
```

- **Fuseau horaire :** les heures suivent celui du serveur. Pour être à l'heure de Paris :
  `sudo timedatectl set-timezone Europe/Paris`.
- **Fréquence :** pour une seule vidéo par jour, garde une seule heure (`47 12 * * *`).
- **Le script `scripts/run.sh` :**
  - empêche deux passages en même temps ;
  - écrit tout dans `logs/AAAA-MM.log` ;
  - utilise `.venv/bin/python`, ou le Python indiqué par la variable `AUTOTIKTOK_PYTHON`.
- **Plateforme non connectée :** une plateforme que tu ne veux pas utiliser se désactive dans `config.yaml` (`platforms: youtube: false`).
  Tant qu'une plateforme activée n'est pas connectée, **aucune vidéo n'est publiée**. Comme ça, ta file n'est pas gâchée.

Le workflow GitHub `publish.yml` ne se lance plus qu'à la main. Ne réactive pas son planning en même temps que le cron,
sinon chaque vidéo partirait deux fois.

---

## Utilisation au quotidien

### Ajouter des vidéos

- **Où les mettre :** dans `input/` sur le serveur, en `.mp4` ou `.mov`, par `scp`, `rsync` ou ton propre script.
  Ces fichiers ne sont pas suivis par git.
- **Légende (optionnel) :** crée un fichier texte du même nom (`video.txt` pour `video.mp4`).
  Sinon, c'est la légende par défaut de `config.yaml`. Les hashtags de `config.yaml` sont toujours ajoutés.
- **Fichier encore en cours d'écriture :** pour que le cron ne prenne pas une vidéo à moitié copiée, écris-la d'abord sous un autre nom,
  par exemple `video.mp4.part`, puis renomme-la en `.mp4`.

En mode brouillon, TikTok ignore la légende : tu l'écris au moment de publier le brouillon dans l'appli.

### Récupérer des vidéos d'un pool de comptes

Deux commandes, qui se partagent un index (`state/index.json`) :

1. **`python -m autotiktok refresh-index`** lit tous les Reels des profils de `account_pools.txt` (un profil Instagram par ligne)
   et note dans l'index ceux qui dépassent `fetch.min_views` vues (1 million par défaut). Il n'y a pas de requête par vidéo,
   mais des pauses de quelques secondes entre les requêtes (`fetch.request_delay`) : compte une dizaine de minutes pour quelques comptes.
   À relancer à la main de temps en temps, pour rattraper les Reels qui passent la barre du million. Un 429 d'Instagram
   arrête proprement la commande : ce qui est déjà trouvé est gardé.
2. **`python -m autotiktok post-next`** tire dans l'index un Reel jamais pris, le télécharge dans `input/` avec sa légende,
   le publie sur les comptes connectés (comme `publish-next`), supprime le fichier et l'ajoute à l'archive.
   Les URLs déjà prises sont dans `state/fetched.jsonl` : un Reel n'est jamais repris. `--dry-run` affiche le choix sans rien faire.
   Si les comptes de publication ne sont pas connectés, rien n'est téléchargé.

⚠️ N'utilise que des comptes **à toi ou dont les propriétaires t'ont donné leur accord** : republier la vidéo de quelqu'un d'autre
sans autorisation viole le droit d'auteur et les règles des plateformes.

Instagram n'a pas d'API pour lire les vidéos d'un autre compte. La liste des Reels (avec leurs vues) vient de l'API web d'Instagram,
non officielle, avec les cookies d'une session ; le téléchargement passe par [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Cette session est celle d'un compte Instagram **jetable, uniquement pour lister** : ce n'est pas le compte sur lequel tu publies.
Exporte ses cookies au format Netscape (extension « cookies.txt ») dans `cookies_browse.txt` à la racine du projet (ignoré par git),
en ne gardant que ceux d'`instagram.com`. Si Instagram répond 429, copie le User-Agent de ton navigateur dans `fetch.user_agent`.
Si le téléchargement échoue, mets yt-dlp à jour (`pip install -U yt-dlp`).

### Format conseillé

- MP4 (H.264 + AAC), **vertical 9:16** (1080×1920), 4 Go maximum.
- Entre 3 secondes et **3 minutes**. Au-delà de 3 minutes, YouTube ne la classe plus en Short.
- Pas de filigrane TikTok sur les vidéos envoyées à Instagram et YouTube : ça réduit leur visibilité.

### Suivi

- **Journal :** `logs/AAAA-MM.log` contient chaque passage, ce qui a été publié et les erreurs, avec ce qu'il faut faire.
- **État :** lance `.venv/bin/python -m autotiktok status` pour voir les vidéos en attente et les dernières publiées.
- **Fichiers d'état :**
  - `state/published.json` : les vidéos en cours (nouvel essai prévu) ;
  - `state/archive.jsonl` : toutes les vidéos terminées, avec les identifiants des posts.

### Ce qui se passe en cas d'échec

- **Chaque plateforme est indépendante :** si YouTube échoue, Instagram et TikTok sont quand même publiés.
- **Nouvel essai :** au passage suivant, seule la plateforme en échec est retentée, donc pas de doublon.
- **Abandon :** après 3 échecs, ou une erreur définitive (vidéo refusée…), la vidéo est supprimée, notée dans l'archive, et une autre prend le relais.

---

## Audits

### Audit YouTube

Tant que l'audit n'est pas passé, YouTube garde les vidéos envoyées par l'API en **privé**.
Remplis le [formulaire d'audit YouTube API Services](https://support.google.com/youtube/contact/yt_api_form)
en expliquant qu'il s'agit d'un outil personnel qui publie tes propres vidéos sur ta propre chaîne.
C'est gratuit, et le délai est en général d'une à quelques semaines. Pendant ce temps :
- les vidéos s'accumulent en privé dans YouTube Studio ;
- tu peux les passer en public à la main, ou les programmer en lot.

### Audit TikTok

Sans audit, TikTok n'autorise que le **mode brouillon** (`tiktok.mode: draft`, par défaut) ou des posts privés.
Pour demander l'audit, va dans le portail développeur → ton app → **Content Posting API → Direct Post → Apply for audit**.
Tu devras fournir :
- **une URL de site** avec une politique de confidentialité et des conditions d'utilisation.
  Le fichier [`docs/index.html`](docs/index.html) est prêt :
  - remplace `CONTACT_EMAIL` par ton adresse ;
  - publie-le avec GitHub Pages (Settings → Pages → dossier `/docs`), ou sur ton serveur.
- une description de l'usage et une vidéo de démonstration.

⚠️ Les règles de TikTok demandent que l'utilisateur confirme chaque publication. Une app qui publie
**sans intervention** peut donc être refusée. Si c'est le cas, garde le mode brouillon (un tap par jour).

Une fois l'audit accepté, mets ceci dans `config.yaml` :

```yaml
tiktok:
  mode: direct
  privacy_level: PUBLIC_TO_EVERYONE
```

---

## Limites

- **Limites des plateformes :**
  - TikTok : environ 5 brouillons en attente maximum sur 24 h ;
  - Instagram : 50 posts par jour ;
  - YouTube : environ 6 envois par jour avec le quota gratuit.
- **Droits d'auteur :** une vidéo reprise d'un autre créateur sans son accord s'expose à des réclamations
  (Content ID sur YouTube, avertissements pour atteinte aux droits d'auteur, suppression sur Instagram et TikTok)
  et peut faire fermer le compte. Instagram et YouTube réduisent aussi la portée du contenu non original.

## Développement

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/pytest
```

Structure :
- `autotiktok/platforms/` : un module par plateforme ;
- `autotiktok/publisher.py` : le passage quotidien ;
- `autotiktok/queue.py` : la file d'attente, l'état et l'archive ;
- `autotiktok/tokens.py` : les jetons chiffrés ;
- `autotiktok/oauth.py` : la connexion des comptes ;
- `scripts/run.sh` : le script lancé par cron.
