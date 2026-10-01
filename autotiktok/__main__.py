"""Command line: ``python -m autotiktok <command>``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

from . import oauth
from .config import ConfigError, load_config
from .fetcher import refresh_index
from .platforms.base import HTTP_TIMEOUT
from .publisher import TOKENS_FILE, publish_next, status
from .tokens import TokenStore, TokenStoreError, generate_key, key_from_env
from .uploader import post_next


def load_env_file(path: Path) -> None:
    """Load ``KEY=VALUE`` lines into the environment (used on a server instead of GitHub Secrets)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autotiktok", description=__doc__)
    parser.add_argument("--root", default=".", help="dossier du projet (défaut : dossier courant)")
    sub = parser.add_subparsers(dest="command", required=True)

    publish = sub.add_parser("publish-next", help="publie la prochaine vidéo de input/")
    publish.add_argument("--dry-run", action="store_true", help="affiche ce qui serait publié, sans rien envoyer")
    refresh = sub.add_parser("refresh-index", help="liste les Reels populaires des comptes du pool")
    which = refresh.add_mutually_exclusive_group()
    which.add_argument("--new", action="store_true", help="seulement les comptes que l'index n'a jamais listés")
    which.add_argument("--account", metavar="COMPTE", help="seulement ce compte du pool (nom ou URL du profil)")
    refresh.add_argument(
        "--min-views", type=int, metavar="N", help="seuil de vues pour cette fois (défaut : config.yaml)"
    )
    post = sub.add_parser("post-next", help="télécharge un Reel de l'index et le publie sur le compte d'upload")
    post.add_argument("--dry-run", action="store_true", help="affiche le Reel choisi, sans rien télécharger ni publier")
    sub.add_parser("status", help="affiche la file d'attente et les dernières publications")
    sub.add_parser("keygen", help="génère une clé TOKENS_KEY")
    auth = sub.add_parser("auth", help="connecte un compte (à lancer une fois, sur ton ordinateur)")
    auth.add_argument("platform", choices=["tiktok", "youtube", "instagram"])
    auth.add_argument("--token", default="", help="Instagram : jeton généré dans le tableau de bord Meta")

    args = parser.parse_args(argv)
    root = Path(args.root)
    load_env_file(root / ".env")

    if args.command == "keygen":
        print(generate_key())
        return 0

    try:
        config = load_config(root / "config.yaml")
        if args.command == "publish-next":
            return publish_next(root, config, dry_run=args.dry_run)
        if args.command == "refresh-index":
            if args.min_views is not None and args.min_views < 0:
                parser.error("--min-views doit être positif ou nul")
            return refresh_index(root, config, only_new=args.new, account=args.account, min_views=args.min_views)
        if args.command == "post-next":
            return post_next(root, config, dry_run=args.dry_run)
        if args.command == "status":
            status(root, config)
            return 0
        store = TokenStore(root / TOKENS_FILE, key_from_env())
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            if args.platform == "tiktok":
                message = oauth.auth_tiktok(store, os.environ, client)
            elif args.platform == "youtube":
                message = oauth.auth_youtube(store, os.environ, client)
            else:
                message = oauth.auth_instagram(store, os.environ, client, args.token)
        print(message)
        print(f"Jetons chiffrés enregistrés dans {TOKENS_FILE} : commit et push ce fichier.")
        return 0
    except (ConfigError, TokenStoreError, oauth.AuthError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
