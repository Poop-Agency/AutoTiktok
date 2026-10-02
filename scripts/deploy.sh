#!/usr/bin/env bash
# Met le serveur à jour depuis ce dossier : copie le code et les réglages, puis installe les dépendances.
#   scripts/deploy.sh                 copie et met à jour
#   scripts/deploy.sh --dry-run       montre ce qui serait copié, sans rien envoyer
#   scripts/deploy.sh --with-cookies  copie aussi cookies_browse.txt et cookies_upload.txt
#
# Jamais copiés : state/ (l'état du serveur : liste des Reels, Reels déjà pris, archive), input/, logs/, .env,
# .venv et .git. Les cookies ne sont copiés qu'avec --with-cookies.
# Réglable par variables d'environnement : AUTOTIKTOK_SSH_KEY, AUTOTIKTOK_SERVER, AUTOTIKTOK_REMOTE_DIR.
set -euo pipefail

cd "$(dirname "$0")/.."
key="${AUTOTIKTOK_SSH_KEY:-$HOME/.ssh/id_ed25519}"
server="${AUTOTIKTOK_SERVER:-ubuntu@141.253.101.226}"
remote_dir="${AUTOTIKTOK_REMOTE_DIR:-AutoTiktok}"

dry_run=0
cookies=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    --with-cookies) cookies=1 ;;
    *) echo "Option inconnue : $arg" >&2; exit 2 ;;
  esac
done

excludes=(--exclude '.venv/' --exclude '.git/' --exclude '__pycache__/' --exclude '.pytest_cache/'
          --exclude '.ruff_cache/' --exclude 'state/' --exclude 'input/' --exclude 'logs/' --exclude 'done/'
          --exclude '.env')
[ "$cookies" = 1 ] || excludes+=(--exclude 'cookies_*.txt')

ssh_cmd="ssh -i $key"
if [ "$dry_run" = 1 ]; then
  echo "Simulation : rien n'est envoyé."
  rsync -azn --itemize-changes -e "$ssh_cmd" "${excludes[@]}" ./ "$server:$remote_dir/"
  exit 0
fi

rsync -az --itemize-changes -e "$ssh_cmd" "${excludes[@]}" ./ "$server:$remote_dir/"
# shellcheck disable=SC2029  # $remote_dir est volontairement développé ici
ssh -i "$key" "$server" "cd $remote_dir && chmod +x scripts/*.sh && chmod 600 cookies_*.txt 2>/dev/null; .venv/bin/pip install -q -r requirements.txt && echo 'Dépendances à jour.'"
echo "Serveur à jour : $server:$remote_dir"
