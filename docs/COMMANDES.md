# Commandes et paramètres d'AutoTiktok

Toutes les commandes se lancent depuis le dossier du projet :

```bash
.venv/bin/python -m autotiktok [--root DOSSIER] <commande> [options]
```

`--root DOSSIER` indique le dossier du projet (par défaut : le dossier courant). Les exemples ci-dessous
écrivent `python -m autotiktok` pour aller plus vite ; ajoute `.venv/bin/` devant si tu n'as pas activé l'environnement.

| Commande | À quoi elle sert |
|---|---|
| [`refresh-index`](#refresh-index) | liste les Reels populaires des comptes de `account_pools.json` |
| [`post-next`](#post-next) | télécharge un Reel de la liste et le publie sur le compte d'upload |
| [`publish-next`](#publish-next) | publie la prochaine vidéo déjà présente dans `input/` |
| [`status`](#status) | affiche la file d'attente et les dernières publications |
| [`auth`](#auth) | connecte un compte (TikTok, YouTube, Instagram) |
| [`keygen`](#keygen) | génère la clé de chiffrement des jetons |

Code de retour : `0` quand tout s'est bien passé, `1` quand une étape a échoué (la sortie dit laquelle),
`2` pour une erreur de configuration (`config.yaml`, clé, connexion d'un compte).

---

## refresh-index

Ouvre la page Reels de chaque compte du pool dans un navigateur invisible (avec les cookies du compte Instagram
jetable), lit tous les Reels et enregistre dans `state/index.json` ceux qui dépassent le seuil de vues.
Rien n'est téléchargé. L'index est écrit après chaque page : si tu interromps la commande (Ctrl+C) ou si
Instagram répond 429, ce qui est déjà trouvé est conservé.

```
python -m autotiktok refresh-index [--new | --account COMPTE] [--min-views N]
```

| Option | Effet |
|---|---|
| *(aucune)* | liste **tous** les comptes de `account_pools.json` |
| `--new` | seulement les comptes que l'index n'a **jamais** listés |
| `--account COMPTE` | seulement ce compte. Nom (`granny___1`, `@granny___1`) ou URL du profil. Il doit être dans le pool |
| `--min-views N` | seuil de vues pour cette fois, à la place de `fetch.min_views` de `config.yaml`. **Un seuil écrit dans `account_pools.json` pour un compte passe avant** (voir [le seuil de vues](#le-seuil-de-vues)) |

`--new` et `--account` s'excluent l'un l'autre. `--min-views` se combine avec les deux.

**Exemples**

```bash
# Première fois, ou mise à jour complète : tous les comptes, seuil de config.yaml (1 M par défaut)
python -m autotiktok refresh-index

# J'ai ajouté 2 comptes à account_pools.json : ne traiter que ceux-là
python -m autotiktok refresh-index --new

# Un seul compte, par son nom ou par son URL
python -m autotiktok refresh-index --account kujo__o
python -m autotiktok refresh-index --account https://www.instagram.com/kujo__o/

# Seuil à 100 000 vues pour les nouveaux comptes seulement
python -m autotiktok refresh-index --new --min-views 100000

# Refaire un compte déjà listé avec un seuil plus bas
python -m autotiktok refresh-index --account granny___1 --min-views 100000
```

À savoir :
- **Baisser le seuil ne retrouve pas les Reels des comptes déjà listés.** Les Reels sous l'ancien seuil n'ont pas
  été gardés : il faut relancer ces comptes avec `--account NOM --min-views N`.
- **Un compte listé sans aucun Reel au-dessus du seuil** est quand même mémorisé : `--new` ne le relira pas.
- **Durée :** compte environ 1 minute pour 50 à 100 Reels (pause de 3 à 6 s entre deux défilements). Un compte
  de plusieurs centaines de Reels (`granny___1`, `kujo__o`) prend de 30 à 45 minutes. Pour accélérer les passages
  suivants, mets `fetch.max_reels_per_account` à 150 : seuls les Reels les plus récents sont relus.
- **Relancer la commande** met à jour les vues des Reels déjà dans l'index et ajoute ceux qui ont passé le seuil.
  Les entrées existantes ne sont jamais supprimées.

---

## account_pools.json

La liste des comptes à lister. C'est un tableau JSON ; chaque compte peut avoir son propre seuil de vues.

```json
[
  { "account": "https://www.instagram.com/vidconsumer/", "min_views": 1000000 },
  { "account": "https://www.instagram.com/granny___1/",  "min_views": 250000 },
  { "account": "https://www.instagram.com/alcr/",        "min_views": null },
  { "account": "kujo__o" }
]
```

| Champ | Rôle |
|---|---|
| `account` | URL du profil, `@nom` ou `nom` |
| `min_views` | seuil de ce compte (entier). `null` ou absent = pas de seuil propre |

Écrire juste une chaîne (`"kujo__o"`) revient à un compte sans seuil propre. Un fichier `.txt` (un profil par ligne)
est encore lu si tu le mets dans `fetch.pool_file`, mais sans seuil par compte. Une erreur dans le fichier
(JSON invalide, seuil qui n'est pas un nombre) arrête `refresh-index` avec un message, avant tout accès à Instagram.

### Le seuil de vues

Pour chaque compte, `refresh-index` prend **le premier** de ces trois :

1. le `min_views` écrit pour ce compte dans `account_pools.json` (toujours prioritaire) ;
2. sinon `--min-views N` de la commande ;
3. sinon `fetch.min_views` de `config.yaml` (1 000 000 par défaut).

`0` est une vraie valeur (tous les Reels), pas « vide ».

```bash
# vidconsumer : 1 000 000 (son seuil) ; alcr et kujo__o : 100 000 (la commande) ; granny___1 : 250 000 (son seuil)
python -m autotiktok refresh-index --min-views 100000
```

Le seuil utilisé est affiché pour chaque compte (`Compte : alcr (seuil : 100000 vues)`) et enregistré dans
`state/index.json`. Changer le seuil d'un compte déjà listé ne retrouve pas les anciens Reels sous l'ancien seuil :
relance-le avec `refresh-index --account NOM`.

---

## post-next

Prend un Reel de l'index que tu n'as jamais pris, le télécharge dans `input/` avec sa légende, le **publie sur le
compte d'upload** (le compte « bestof »), supprime le fichier et l'ajoute à l'archive.

La publication passe par le site instagram.com, dans un navigateur invisible connecté avec les cookies du compte
d'upload (`cookies_upload.txt`). Elle n'utilise ni l'API officielle ni TikTok / YouTube : pour ceux-là,
utilise `publish-next`.

```
python -m autotiktok post-next [--dry-run]
```

| Option | Effet |
|---|---|
| *(aucune)* | télécharge et publie |
| `--dry-run` | affiche le Reel qui serait choisi, sans rien télécharger ni publier |

**Exemples**

```bash
# Voir quel Reel serait pris
python -m autotiktok post-next --dry-run

# Un passage complet : téléchargement + publication
python -m autotiktok post-next
```

**Ce qui se passe, dans l'ordre**
1. Vérifie `cookies_upload.txt` (présent, avec un cookie `sessionid`) et, s'ils sont configurés, les fichiers de légende et de miniature. Sinon la commande s'arrête **avant** de
   télécharger : aucun Reel n'est « brûlé ».
2. Tire un Reel de l'index au hasard, jamais pris (`state/fetched.jsonl`), et le télécharge dans `input/`.
3. Ouvre instagram.com et publie le Reel avec la légende de `upload.caption_file` (ou, s'il est vide, la légende d'origine suivie des `caption.hashtags`), et la miniature `upload.cover_file` si elle est définie.
4. Attend que le Reel apparaisse sur le profil du compte d'upload (jusqu'à `upload.share_timeout` secondes).
5. Supprime la vidéo de `input/` et écrit une ligne dans `state/archive.jsonl`.

**Où est enregistrée l'URL utilisée**
- `state/fetched.jsonl` : une ligne par Reel téléchargé, avec l'URL source, le compte d'origine, les vues, le
  fichier et la date. C'est ce qui empêche de reprendre deux fois le même Reel.
- `state/archive.jsonl` : une ligne par Reel publié, avec `source_url`, `source_account` et l'URL du Reel
  publié sur le compte d'upload (`platforms.instagram.post_id`).

À savoir :
- Si la publication échoue (interface d'Instagram qui change, cookies expirés), la vidéo **reste dans `input/`**
  et le prochain `post-next` la republie sans retélécharger.
- Si le Reel n'apparaît pas sur le profil dans le délai, la commande s'arrête avec un message : il est peut-être
  publié. **Vérifie le profil avant de relancer**, sinon tu risques un doublon.
- Un Reel qui n'est plus disponible sur Instagram est marqué `gone` dans l'index et un autre est tiré.
- Pour l'automatiser, mets `post-next` à la place de `publish-next` dans `scripts/run.sh`.

---

## publish-next

Publie la prochaine vidéo qui se trouve déjà dans `input/` (tirée au hasard ou par ordre alphabétique selon
`queue.order`). C'est la commande à utiliser pour tes propres vidéos.

```
python -m autotiktok publish-next [--dry-run]
```

| Option | Effet |
|---|---|
| `--dry-run` | affiche la vidéo, la légende et les plateformes visées, sans rien envoyer |

```bash
python -m autotiktok publish-next --dry-run
python -m autotiktok publish-next
```

---

## status

Affiche le nombre de vidéos en attente, l'état de chacune par plateforme, et les 5 dernières vidéos terminées.

```bash
python -m autotiktok status
```

---

## auth

Connecte un compte. À lancer **une seule fois, sur ton ordinateur** (un navigateur s'ouvre pour TikTok et YouTube).
Les jetons sont enregistrés chiffrés dans `state/tokens.enc`. La mise en place complète est dans le
[README](../README.md#mise-en-place-une-seule-fois-1-h).

```
python -m autotiktok auth {tiktok,youtube,instagram} [--token JETON]
```

| Option | Effet |
|---|---|
| `tiktok` / `youtube` | ouvre la connexion dans le navigateur |
| `instagram` | enregistre le jeton généré dans le tableau de bord Meta |
| `--token JETON` | Instagram uniquement : le jeton à enregistrer |

```bash
python -m autotiktok auth tiktok
python -m autotiktok auth youtube
python -m autotiktok auth instagram --token "LE_JETON"
```

---

## keygen

Génère une clé de chiffrement. Mets-la dans `TOKENS_KEY=` du fichier `.env`, sur l'ordinateur et sur le serveur.

```bash
python -m autotiktok keygen
```

---

## Réglages de `config.yaml`

Tout est optionnel : une valeur absente prend sa valeur par défaut (indiquée ci-dessous).

### `platforms`
Plateformes sur lesquelles publier.

| Clé | Défaut | Valeurs |
|---|---|---|
| `instagram`, `tiktok`, `youtube` | `true` | `true` / `false` |

```yaml
platforms:        # publier uniquement sur Instagram
  instagram: true
  tiktok: false
  youtube: false
```

### `caption`
Légende utilisée quand une vidéo n'a pas de fichier `.txt` du même nom. Avec `post-next`, la légende d'origine
du Reel est écrite dans ce fichier `.txt`.

| Clé | Défaut | Rôle |
|---|---|---|
| `default` | `😂` | légende par défaut |
| `hashtags` | `#funny #humour #fyp` | ajoutés à la fin de **chaque** légende. Vide (`""`) pour n'en mettre aucun |

### `instagram`
| Clé | Défaut | Rôle |
|---|---|---|
| `share_to_feed` | `true` | le Reel apparaît aussi dans la grille du profil |

### `tiktok`
| Clé | Défaut | Valeurs / rôle |
|---|---|---|
| `mode` | `draft` | `draft` : la vidéo arrive dans tes brouillons, tu publies d'un tap. `direct` : publication directe |
| `privacy_level` | `PUBLIC_TO_EVERYONE` | `PUBLIC_TO_EVERYONE`, `MUTUAL_FOLLOW_FRIENDS`, `FOLLOWER_OF_CREATOR`, `SELF_ONLY`. Avant l'audit TikTok, seul `SELF_ONLY` est accepté en mode `direct` |
| `disable_comment`, `disable_duet`, `disable_stitch` | `false` | désactive les commentaires, les duos, les collages |
| `is_aigc` | `false` | déclare un contenu généré par IA |

### `youtube`
| Clé | Défaut | Rôle |
|---|---|---|
| `title` | `"{caption}"` | `{caption}` est remplacé par la légende ; `#Shorts` est ajouté tout seul. 100 caractères maximum |
| `privacy` | `public` | `public`, `unlisted`, `private`. YouTube force `private` tant que l'audit n'est pas passé |
| `category_id` | `"23"` | catégorie (23 = Humour) |
| `made_for_kids` | `false` | vidéo « conçue pour les enfants » |
| `tags` | `[funny, humour, shorts]` | liste de tags |

### `queue`
| Clé | Défaut | Valeurs / rôle |
|---|---|---|
| `input_dir` | `input` | dossier des vidéos en attente |
| `done_dir` | `done` | dossier d'archivage quand `after_publish: move` |
| `order` | `alphabetical` (`random` dans le `config.yaml` fourni) | `random` : tirée au hasard. `alphabetical` : par nom |
| `after_publish` | `move` (`delete` dans le `config.yaml` fourni) | `delete` : supprime la vidéo une fois publiée. `move` : la déplace dans `done_dir` |
| `max_attempts` | `3` | au bout de ce nombre d'échecs sur une plateforme, on abandonne cette plateforme pour cette vidéo |

### `upload`
Compte sur lequel `post-next` publie (le compte « bestof »).

| Clé | Défaut | Rôle |
|---|---|---|
| `cookies_file` | `cookies_upload.txt` | cookies (format Netscape) **de ce compte**, avec au minimum `sessionid`. Jamais dans git |
| `caption_file` | `""` | fichier texte (`legende.txt`) dont le contenu est la légende de **chaque** publication, tel quel (retours à la ligne et hashtags compris). Vide = légende d'origine du Reel + `caption.hashtags` |
| `cover_file` | `""` | image (`assets/miniature_spiderman.png`) envoyée comme miniature de chaque Reel. Vide = Instagram prend la première image de la vidéo. Format conseillé : 1080×1920 (9:16) |
| `browser_path` | `""` | chemin d'un Chromium. Vide = celui installé par `playwright install chromium` |
| `headless` | `true` | `false` affiche le navigateur : utile pour voir une publication bloquée |
| `share_timeout` | `240` | secondes d'attente maximum pour que le Reel apparaisse sur le profil après « Share » |

### `fetch`
Réglages de `refresh-index` (et du téléchargement de `post-next`).

| Clé | Défaut | Rôle |
|---|---|---|
| `pool_file` | `account_pools.json` | liste des comptes, avec un seuil de vues optionnel par compte ([format](#account_poolsjson)) |
| `cookies_file` | `cookies_browse.txt` | cookies (format Netscape) du compte Instagram **jetable** qui sert à lister. Jamais dans git |
| `index_file` | `state/index.json` | fichier de la liste des Reels |
| `min_views` | `1000000` | seuil de vues **par défaut**. Passe après le seuil d'un compte dans `account_pools.json` et après `refresh-index --min-views` |
| `max_reels_per_account` | `0` | nombre maximum de Reels lus par compte, des plus récents aux plus anciens. `0` = tous |
| `request_delay` | `[3, 6]` | pause aléatoire, en secondes, entre deux défilements de la page. Plus c'est long, moins Instagram bloque |
| `browser_path` | `""` | chemin d'un Chromium. Vide = celui installé par `playwright install chromium` |
| `headless` | `true` | `false` affiche le navigateur : utile pour voir pourquoi Instagram bloque |

```yaml
fetch:            # exemple : mises à jour rapides, 150 Reels récents par compte, seuil 500 000
  min_views: 500000
  max_reels_per_account: 150
```

---

## Fichiers utilisés

| Fichier | Contenu | Dans git ? |
|---|---|---|
| `account_pools.json` | comptes Instagram à lister, avec leur seuil de vues | oui |
| `cookies_browse.txt` | cookies du compte jetable, qui sert à lister et à télécharger | **non** |
| `cookies_upload.txt` | cookies du compte « bestof », sur lequel `post-next` publie | **non** |
| `legende.txt` | légende reprise à chaque publication de `post-next` | oui |
| `assets/miniature_spiderman.png` | miniature de chaque Reel publié par `post-next` (`upload.cover_file`) | oui |
| `.env` | `TOKENS_KEY` et clés des API TikTok / YouTube / Instagram | **non** |
| `state/index.json` | liste des Reels au-dessus du seuil, et comptes déjà listés | non |
| `state/fetched.jsonl` | URL de chaque Reel téléchargé (jamais repris) | non |
| `state/archive.jsonl` | vidéos déjà publiées : empreinte, URL source, URL publiée | non |
| `state/published.json` | état des publications en cours | non |
| `state/tokens.enc` | jetons des comptes de publication, chiffrés | non |
| `input/` | vidéos en attente | non |

## Problèmes courants

| Message | Cause et solution |
|---|---|
| `cookies_browse.txt introuvable` | exporte les cookies d'`instagram.com` du compte jetable (extension « cookies.txt ») |
| `Instagram demande de se connecter : cookies expirés` | la session jetable a expiré : reconnecte-toi sur instagram.com et ré-exporte les cookies |
| `Instagram limite les requêtes (429)` | trop de requêtes : attends quelques heures, augmente `request_delay`. L'index garde ce qui est déjà trouvé |
| `X n'est pas dans le pool` | ajoute le compte à `account_pools.json` avant `--account` |
| `account_pools.json : ... min_views doit être un entier...` | le seuil d'un compte doit être un nombre entier positif, `null`, ou absent. Écris `1000000`, pas `"1 million"` |
| `Aucun nouveau compte à lister` | normal : `--new` n'a rien à faire, tous les comptes sont déjà dans l'index |
| `Aucun Reel disponible dans l'index` | lance d'abord `refresh-index`, ou tous les Reels de l'index ont déjà été pris |
| `Configuration incomplète, aucun Reel téléchargé` | `cookies_upload.txt` est absent ou sans `sessionid` (colle-y l'export des cookies du compte d'upload), ou `legende.txt` / la miniature est configuré mais absent |
| `Instagram demande de se connecter : cookies du compte d'upload expirés` | ré-exporte les cookies du compte d'upload |
| `Interface d'Instagram inattendue, rien n'a été publié` | Instagram a changé son site : lance avec `upload.headless: false` pour voir où ça bloque |
