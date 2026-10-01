"""Encrypted storage for OAuth tokens.

Tokens rotate (TikTok refresh tokens, Instagram long-lived tokens), so they
cannot live in GitHub Secrets, which a workflow cannot update. They are kept in
``state/tokens.enc``, encrypted with a Fernet key stored in the ``TOKENS_KEY``
secret, and committed back by the workflow after each refresh.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

KEY_ENV = "TOKENS_KEY"


class TokenStoreError(Exception):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode()


def key_from_env() -> str:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise TokenStoreError(
            f"La variable d'environnement {KEY_ENV} est vide. Génère une clé avec "
            "`python -m autotiktok keygen` et ajoute-la aux secrets GitHub."
        )
    return key


class TokenStore:
    def __init__(self, path: Path, key: str):
        self.path = Path(path)
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise TokenStoreError(f"{KEY_ENV} n'est pas une clé Fernet valide.") from exc
        self._data: dict[str, dict] = {}
        self.dirty = False
        if self.path.exists():
            try:
                raw = self._fernet.decrypt(self.path.read_bytes())
            except InvalidToken as exc:
                raise TokenStoreError(
                    f"Impossible de déchiffrer {self.path} : {KEY_ENV} ne correspond pas à la clé utilisée "
                    "lors de la connexion des comptes."
                ) from exc
            self._data = json.loads(raw)

    def get(self, platform: str) -> dict | None:
        token = self._data.get(platform)
        return dict(token) if token else None

    def set(self, platform: str, token: dict) -> None:
        self._data[platform] = dict(token)
        self.dirty = True

    def platforms(self) -> list[str]:
        return sorted(self._data)

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, sort_keys=True).encode()
        self.path.write_bytes(self._fernet.encrypt(payload))
        self.dirty = False


def expires_at(expires_in: int | float | None, now: float | None = None) -> float | None:
    if not expires_in:
        return None
    return (now if now is not None else time.time()) + float(expires_in)


def expires_soon(token: dict, margin: float, now: float | None = None) -> bool:
    """True when the access token expires within ``margin`` seconds (or has no known expiry)."""
    exp = token.get("expires_at")
    if exp is None:
        return True
    return (now if now is not None else time.time()) + margin >= float(exp)
