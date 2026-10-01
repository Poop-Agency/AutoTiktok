"""Instagram Reels through the Instagram API with Instagram Login.

The video is sent with a resumable upload to ``rupload.facebook.com``, so it
does not need to be reachable on a public URL (the repo can stay private).

https://developers.facebook.com/docs/instagram-platform/content-publishing
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping

import httpx

from ..tokens import expires_at, expires_soon
from .base import PermanentError, PublishResult, RetryableError, Video, poll

API_VERSION = "v23.0"
GRAPH = f"https://graph.instagram.com/{API_VERSION}"
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
EXCHANGE_URL = "https://graph.instagram.com/access_token"
RUPLOAD = f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}"

# Long-lived tokens last 60 days; refresh them well before that. Instagram
# only refreshes tokens that are at least 24 hours old.
REFRESH_MARGIN = 20 * 86400
MIN_TOKEN_AGE = 86400
LONG_LIVED_TTL = 60 * 86400

# Graph error codes worth retrying later (rate limits, temporary outages).
TRANSIENT_CODES = frozenset({1, 2, 4, 17, 32, 341, 613, 9004, 9007})
INVALID_TOKEN_CODE = 190

CONTAINER_TIMEOUT = 600.0
CONTAINER_INTERVAL = 10.0


class InstagramPublisher:
    name = "instagram"

    def __init__(
        self,
        config: dict,
        token: dict,
        client: httpx.Client,
        env: Mapping[str, str] = os.environ,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config["instagram"]
        self.token = dict(token)
        self.client = client
        self.env = env
        self.sleep = sleep

    def refresh_if_needed(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        if not expires_soon(self.token, margin=REFRESH_MARGIN, now=now):
            return False
        if now - float(self.token.get("issued_at", 0)) < MIN_TOKEN_AGE:
            return False
        resp = self.client.get(
            REFRESH_URL,
            params={"grant_type": "ig_refresh_token", "access_token": self.token["access_token"]},
        )
        body = self._check(resp, "rafraîchissement du jeton")
        self.token.update(
            access_token=body["access_token"],
            expires_at=expires_at(body.get("expires_in") or LONG_LIVED_TTL, now),
            issued_at=now,
        )
        return True

    def publish(self, video: Video) -> PublishResult:
        user_id = self.token.get("user_id") or "me"
        container = self._check(
            self.client.post(
                f"{GRAPH}/{user_id}/media",
                data={
                    "media_type": "REELS",
                    "upload_type": "resumable",
                    "caption": video.caption[:2200],
                    "share_to_feed": "true" if self.config.get("share_to_feed", True) else "false",
                    "access_token": self.token["access_token"],
                },
            ),
            "création du conteneur",
        )
        container_id = container.get("id")
        if not container_id:
            raise RetryableError(f"Instagram : réponse inattendue à la création du conteneur : {container}")

        with video.path.open("rb") as fh:
            upload = self.client.post(
                f"{RUPLOAD}/{container_id}",
                content=fh,
                headers={
                    "Authorization": f"OAuth {self.token['access_token']}",
                    "offset": "0",
                    "file_size": str(video.size),
                },
            )
        self._check(upload, "envoi de la vidéo")

        self._wait_until_ready(container_id)

        published = self._check(
            self.client.post(
                f"{GRAPH}/{user_id}/media_publish",
                data={"creation_id": container_id, "access_token": self.token["access_token"]},
            ),
            "publication",
        )
        media_id = str(published.get("id", ""))
        if not media_id:
            raise RetryableError(f"Instagram : réponse inattendue à la publication : {published}")

        details: dict = {}
        try:
            info = self.client.get(
                f"{GRAPH}/{media_id}",
                params={"fields": "permalink", "access_token": self.token["access_token"]},
            )
            if info.is_success and info.json().get("permalink"):
                details["url"] = info.json()["permalink"]
        except (httpx.HTTPError, ValueError):
            pass  # The permalink is only informative.
        return PublishResult(post_id=media_id, details=details)

    def _wait_until_ready(self, container_id: str) -> None:
        status: dict = {}

        def check() -> bool:
            resp = self.client.get(
                f"{GRAPH}/{container_id}",
                params={"fields": "status_code,status", "access_token": self.token["access_token"]},
            )
            status.update(self._check(resp, "statut du conteneur"))
            return status.get("status_code") in ("FINISHED", "ERROR", "EXPIRED", "PUBLISHED")

        ready = poll(check, timeout=CONTAINER_TIMEOUT, interval=CONTAINER_INTERVAL, sleep=self.sleep)
        code = status.get("status_code")
        if code in ("ERROR", "EXPIRED"):
            raise PermanentError(f"Instagram a rejeté la vidéo ({code}) : {status.get('status', '')}")
        if not ready:
            # Nothing was published yet, so a later run can safely start over.
            raise RetryableError("Instagram : la vidéo est toujours en traitement après 10 minutes.")

    def _check(self, resp: httpx.Response, step: str) -> dict:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if resp.is_success and "error" not in body:
            return body
        error = body.get("error") or {}
        code = error.get("code")
        message = f"Instagram ({step}) : HTTP {resp.status_code} – {error.get('message') or resp.text[:300]}"
        if code == INVALID_TOKEN_CODE:
            raise PermanentError(
                f"{message}. Le jeton est invalide ou expiré : reconnecte le compte avec "
                "`python -m autotiktok auth instagram`.",
                response=body,
            )
        if error.get("is_transient") or code in TRANSIENT_CODES or resp.status_code >= 500:
            raise RetryableError(message, response=body)
        if resp.status_code == 429:
            raise RetryableError(message, response=body)
        raise PermanentError(message, response=body)


def exchange_for_long_lived(client: httpx.Client, short_token: str, app_secret: str) -> dict | None:
    """Exchange a short-lived token (1h) for a long-lived one (60 days). None if it fails."""
    resp = client.get(
        EXCHANGE_URL,
        params={"grant_type": "ig_exchange_token", "client_secret": app_secret, "access_token": short_token},
    )
    if not resp.is_success:
        return None
    body = resp.json()
    return body if body.get("access_token") else None


def fetch_account(client: httpx.Client, access_token: str) -> dict:
    resp = client.get(f"{GRAPH}/me", params={"fields": "user_id,username", "access_token": access_token})
    if not resp.is_success:
        raise PermanentError(f"Instagram : jeton refusé ({resp.status_code}) : {resp.text[:300]}")
    return resp.json()
