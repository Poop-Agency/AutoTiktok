#!/usr/bin/env bash
# Lance un passage de publication. Prévu pour la crontab du serveur :
#   47 10,17 * * * /chemin/vers/AutoTiktok/scripts/run.sh
# flock empêche deux passages en même temps ; la sortie va dans logs/AAAA-MM.log.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs
log="logs/$(date -u +%Y-%m).log"
python="${AUTOTIKTOK_PYTHON:-.venv/bin/python}"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  flock -n 9 || { echo "Un passage est déjà en cours, abandon."; exit 0; }
  "$python" -m autotiktok publish-next
} 9>logs/.lock >>"$log" 2>&1
