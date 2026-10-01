"""YouTube Shorts through the YouTube Data API v3 (resumable upload).

A vertical video of 3 minutes or less is classified as a Short automatically.
Until the Google Cloud project passes the YouTube API audit, YouTube locks
videos uploaded through the API as private.

https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping

import httpx

from ..tokens import expires_at, expires_soon
from .base import PermanentError, PublishResult, RetryableError, Video

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload",)

TITLE_MAX = 100
SHORTS_TAG = "#Shorts"
RETRYABLE_REASONS = frozenset(
    {"quotaExceeded", "rateLimitExceeded", "userRateLimitExceeded", "uploadLimitExceeded", "backendError"}
)


def build_title(template: str, caption: str) -> str:
    title = template.replace("{caption}", caption).replace("<", "").replace(">", "").strip()
    title = " ".join(title.split()) or "😂"
    if SHORTS_TAG.lower() not in title.lower() and len(title) + len(SHORTS_TAG) + 1 <= TITLE_MAX:
        title = f"{title} {SHORTS_TAG}"
    return title[:TITLE_MAX].strip()


class YouTubePublisher:
    name = "youtube"

    def __init__(
        self,
        config: dict,
        token: dict,
        client: httpx.Client,
        env: Mapping[str, str] = os.environ,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config["youtube"]
        self.token = dict(token)
        self.client = client
        self.env = env
        self.sleep = sleep

    def refresh_if_needed(self, now: float | None = None) -> bool:
        if self.token.get("access_token") and not expires_soon(self.token, margin=600, now=now):
            return False
        client_id = self.env.get("GOOGLE_CLIENT_ID", "")
        client_secret = self.env.get("GOOGLE_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise PermanentError("YouTube : secrets GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET manquants.")
        resp = self.client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": self.token.get("refresh_token", ""),
                "grant_type": "refresh_token",
            },
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not resp.is_success or "access_token" not in body:
            message = f"YouTube : échec du rafraîchissement du jeton ({body.get('error', resp.status_code)})."
            if resp.status_code >= 500 or resp.status_code == 429:
                raise RetryableError(message)
            raise PermanentError(
                message + " Reconnecte le compte avec `python -m autotiktok auth youtube` et vérifie que "
                "l'écran de consentement OAuth est « En production » (sinon le jeton expire après 7 jours)."
            )
        self.token.update(access_token=body["access_token"], expires_at=expires_at(body.get("expires_in"), now))
        if body.get("refresh_token"):
            self.token["refresh_token"] = body["refresh_token"]
        return True

    def publish(self, video: Video) -> PublishResult:
        metadata = {
            "snippet": {
                "title": build_title(self.config.get("title", "{caption}"), video.caption),
                "description": f"{video.caption}\n\n{SHORTS_TAG}".strip(),
                "categoryId": str(self.config.get("category_id", "23")),
                "tags": list(self.config.get("tags") or []),
            },
            "status": {
                "privacyStatus": self.config.get("privacy", "public"),
                "selfDeclaredMadeForKids": bool(self.config.get("made_for_kids", False)),
            },
        }
        auth = {"Authorization": f"Bearer {self.token['access_token']}"}
        init = self.client.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            json=metadata,
            headers={
                **auth,
                "X-Upload-Content-Type": video.content_type,
                "X-Upload-Content-Length": str(video.size),
            },
        )
        _raise_for_google(init, "initialisation de l'envoi")
        session_url = init.headers.get("location")
        if not session_url:
            raise RetryableError("YouTube : pas d'URL de session dans la réponse d'initialisation.")

        with video.path.open("rb") as fh:
            upload = self.client.put(
                session_url,
                content=fh,
                headers={**auth, "Content-Type": video.content_type, "Content-Length": str(video.size)},
            )
        _raise_for_google(upload, "envoi de la vidéo")
        body = upload.json()
        video_id = body.get("id")
        if not video_id:
            raise RetryableError(f"YouTube : réponse inattendue après l'envoi : {str(body)[:300]}")
        privacy = (body.get("status") or {}).get("privacyStatus")
        return PublishResult(
            post_id=video_id,
            details={"url": f"https://youtube.com/shorts/{video_id}", "privacy": privacy},
        )


def _raise_for_google(resp: httpx.Response, step: str) -> None:
    if resp.is_success:
        return
    try:
        error = resp.json().get("error") or {}
    except ValueError:
        error = {}
    reasons = {e.get("reason") for e in error.get("errors") or [] if isinstance(e, dict)}
    message = f"YouTube ({step}) : HTTP {resp.status_code} – {error.get('message') or resp.text[:300]}"
    if reasons & RETRYABLE_REASONS or resp.status_code in (401, 408, 429) or resp.status_code >= 500:
        raise RetryableError(message, response=error)
    raise PermanentError(message, response=error)
