# AutoTiktok

Publie automatiquement tes vidéos, déjà prêtes, sur **TikTok**, **Instagram Reels** et **YouTube Shorts**,
une ou deux fois par jour. Pas de serveur ni d'abonnement : tout tourne gratuitement sur GitHub Actions et
passe par les **API officielles** des plateformes. Tes comptes ne risquent donc pas d'être bannis.

```
input/001.mp4  ──►  12h47 : TikTok + Instagram + YouTube  ──►  done/001.mp4
input/002.mp4  ──►  19h47 : TikTok + Instagram + YouTube  ──►  done/002.mp4
input/003.mp4  ──►  le lendemain 12h47…
```

## Ce qui est automatique

| Plateforme | Au départ | Après validation de la plateforme |
|---|---|---|
| **Instagram Reels** | ✅ Publication publique automatique | — |
| **YouTube Shorts** | ⚠️ Vidéos envoyées en **privé** (règle YouTube pour les apps non auditées) | ✅ Public automatique après l'[audit YouTube](#audit-youtube) |
| **TikTok** | ⚠️ Vidéo envoyée dans tes **brouillons TikTok** : tu la publies d'un tap dans l'appli | ✅ Public automatique après l'[audit TikTok](#audit-tiktok) |

Ces audits ne concernent que la publication **par API**, c'est-à-dire par un programme. Ils sont gratuits.

---

## Mise en place (une seule fois, ~1 h)

Il te faut Python 3.11 ou plus récent sur ton ordinateur, et le repo cloné.
[GitHub Desktop](https://desktop.github.com/) est le plus simple pour cloner.

```bash
cd AutoTiktok
python -m pip install -r requirements.txt
```

### 1. Clé de chiffrement

Les jetons d'accès à tes comptes sont stockés **chiffrés** dans `state/tokens.enc`. Génère une clé :

```bash
python -m autotiktok keygen
```

- Copie la clé dans GitHub : repo → **Settings → Secrets and variables → Actions → New repository secret**,
  nom `TOKENS_KEY`.
- Garde-la aussi dans un endroit sûr (gestionnaire de mots de passe). Elle sert pour les commandes `auth` ci-dessous.

Dans le terminal où tu lanceras les commandes `auth` :

```bash
export TOKENS_KEY="la-clé"            # macOS / Linux
$env:TOKENS_KEY="la-clé"              # Windows PowerShell
```

### 2. Instagram

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
   python -m autotiktok auth instagram --token "LE_JETON"
   ```

L'app peut rester en **mode développement** : c'est suffisant pour publier sur ton propre compte.
Le jeton est valable 60 jours et il est renouvelé automatiquement à chaque passage.

### 3. YouTube

1. Sur [console.cloud.google.com](https://console.cloud.google.com/), crée un projet.
   Dans **API et services → Bibliothèque**, active **YouTube Data API v3**.
2. Ouvre **Écran de consentement OAuth** :
   - choisis le type **Externe** ;
   - ajoute le scope `.../auth/youtube.upload` ;
   - ajoute ton adresse Gmail comme utilisateur test ;
   - clique sur **Publier l'application** pour passer « En production ». Une validation Google n'est pas nécessaire pour ton usage perso.
     ⚠️ Si tu restes en mode « Test », la connexion expire au bout de 7 jours.
3. Dans **Identifiants → Créer des identifiants → ID client OAuth**, choisis le type **Application de bureau**.
4. Ajoute ces deux valeurs aux secrets GitHub, sous les noms `GOOGLE_CLIENT_ID` et `GOOGLE_CLIENT_SECRET`.
   Exporte-les aussi dans ton terminal, comme `TOKENS_KEY`.
5. Lance :
   ```bash
   python -m autotiktok auth youtube
   ```
   Le navigateur s'ouvre et Google affiche « Application non validée » : clique sur **Paramètres avancés → Accéder à … (non sécurisé)**.
   C'est normal, c'est ta propre app.

### 4. TikTok

1. Sur [developers.tiktok.com](https://developers.tiktok.com/), va dans **Manage apps → Connect an app**.
2. Ajoute les produits **Login Kit** et **Content Posting API**.
3. Dans Login Kit, choisis la plateforme **Desktop** et ajoute l'URL de redirection `http://localhost:8765/callback/`.
4. Passe en **Sandbox** et ajoute ton compte TikTok comme **Target user**.
   Ça permet d'utiliser l'API sans attendre la validation de l'app.
5. Ajoute le *Client key* et le *Client secret* aux secrets GitHub, sous les noms `TIKTOK_CLIENT_KEY` et `TIKTOK_CLIENT_SECRET`.
   Exporte-les aussi dans ton terminal.
6. Lance :
   ```bash
   python -m autotiktok auth tiktok
   ```

Si le mode Sandbox ne suffit pas (par exemple si la connexion échoue), soumets l'app à la validation de TikTok
(*Submit for review*) avec la page décrite dans [Audit TikTok](#audit-tiktok).

### 5. Activer

```bash
git add state/tokens.enc
git commit -m "Connexion des comptes"
git push
```

- Le planning ne tourne que sur la **branche principale** (`main`) du repo.
- Pour tester, va dans l'onglet **Actions → Publier une vidéo → Run workflow** :
  - coche « Simulation » pour voir ce qui serait publié, sans rien envoyer ;
  - décoche-la pour publier réellement.
- Une plateforme que tu ne veux pas utiliser se désactive dans `config.yaml` (`platforms: youtube: false`).
  Tant qu'une plateforme activée n'est pas connectée, **aucune vidéo n'est publiée**. Comme ça, ta file n'est pas gâchée.

---

## Utilisation au quotidien

### Ajouter des vidéos

- **Où les mettre :** dans `input/`. Elles partent par ordre alphabétique, donc nomme-les `001.mp4`, `002.mp4`, etc.
- **Légende (optionnel) :** crée un fichier texte du même nom (`001.txt`) avec la légende de la vidéo.
  Sinon, c'est la légende par défaut de `config.yaml`. Les hashtags de `config.yaml` sont toujours ajoutés.
- **Envoi :** commit et push avec GitHub Desktop.
  Le site GitHub limite les fichiers à 25 Mo, git ou GitHub Desktop à **100 Mo**.
- **Avant d'ajouter des vidéos :** fais **Fetch / Pull**, car le robot commit après chaque publication.

En mode brouillon, TikTok ignore la légende : tu l'écris au moment de publier le brouillon dans l'appli.

### Format conseillé

- MP4 (H.264 + AAC), **vertical 9:16** (1080×1920).
- Entre 3 secondes et **3 minutes**. Au-delà de 3 minutes, YouTube ne la classe plus en Short.
- Pas de filigrane TikTok sur les vidéos envoyées à Instagram et YouTube : ça réduit leur visibilité.

### Horaires et fréquence

Modifie les lignes `cron` dans `.github/workflows/publish.yml`. Les heures sont en UTC :
ajoute 2 h l'été et 1 h l'hiver pour l'heure de Paris. Pour une seule vidéo par jour, supprime une des deux lignes.
GitHub peut retarder un passage de quelques minutes, parfois plus aux heures de pointe.

### Suivi

- **Si une publication échoue**, GitHub t'envoie un e-mail : le passage apparaît en rouge dans l'onglet Actions.
  Ouvre-le pour lire le message, qui dit quoi faire.
- **Historique :** `state/published.json` contient chaque vidéo, l'identifiant du post et l'erreur éventuelle.
- **File d'attente :** lance `python -m autotiktok status` pour voir les vidéos en attente.

### Ce qui se passe en cas d'échec

- **Chaque plateforme est indépendante :** si YouTube échoue, Instagram et TikTok sont quand même publiés.
- **Nouvel essai :** au passage suivant, seule la plateforme en échec est retentée, donc pas de doublon.
- **Abandon :** après 3 échecs, ou une erreur définitive (vidéo refusée…), la vidéo passe dans `done/` et la suivante prend le relais.

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
  - publie-le avec GitHub Pages (Settings → Pages → dossier `/docs`).
  GitHub Pages est gratuit sur un repo public. Pour un repo privé, mets ce fichier dans un petit repo public séparé ;
- une description de l'usage et une vidéo de démonstration.

⚠️ Les règles de TikTok demandent que l'utilisateur confirme chaque publication. Une app qui publie
**sans intervention** peut donc être refusée. Si c'est le cas, garde le mode brouillon (un tap par jour).
Sinon, le site TikTok permet de programmer jusqu'à 10 jours de vidéos d'un coup.

Une fois l'audit accepté, mets ceci dans `config.yaml` :

```yaml
tiktok:
  mode: direct
  privacy_level: PUBLIC_TO_EVERYONE
```

---

## Coût et limites

- **Gratuit** : un passage dure environ 2 à 5 minutes. Avec 2 passages par jour, ça fait environ 300 minutes par mois,
  alors que GitHub offre 2000 minutes par mois sur un repo privé (illimité sur un repo public).
- **Limites des plateformes :**
  - TikTok : environ 5 brouillons en attente maximum sur 24 h ;
  - Instagram : 50 posts par jour ;
  - YouTube : environ 6 envois par jour avec le quota gratuit.
- **Taille du repo** : les vidéos restent dans l'historique git. Si le repo dépasse quelques Go, le plus simple est de
  recréer un repo neuf. Pour garder le repo plus léger, utilise `queue.after_publish: delete`.
- **Contenu non original** : Instagram et YouTube réduisent la portée des vidéos reprises d'autres créateurs, et YouTube peut
  refuser la monétisation pour du « contenu réutilisé ».

## Développement

```bash
python -m pip install -r requirements-dev.txt
ruff check . && pytest
```

Structure :
- `autotiktok/platforms/` : un module par plateforme ;
- `autotiktok/publisher.py` : le passage quotidien ;
- `autotiktok/queue.py` : la file d'attente et l'état ;
- `autotiktok/tokens.py` : les jetons chiffrés ;
- `autotiktok/oauth.py` : la connexion des comptes.
