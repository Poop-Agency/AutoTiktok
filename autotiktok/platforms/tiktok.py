"""TikTok Content Posting API.

Two modes:
- ``draft`` (default): the video lands in the creator's TikTok inbox/drafts and
  they publish it from the app with one tap. Works without TikTok's audit.
- ``direct``: Direct Post. Until the app passes TikTok's Content Posting audit,
  TikTok only allows ``SELF_ONLY`` (private) posts.

https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
https://developers.tiktok.com/doc/content-posting-api-reference-upload-video
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping

import httpx

from ..tokens import expires_at, expires_soon
from .base import PermanentError, PublishResult, RetryableError, Video, poll

API_BASE = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = f"{API_BASE}/oauth/token/"
SCOPES = ("user.info.basic", "video.upload", "video.publish")

# Videos up to this size are sent as a single chunk; bigger ones in CHUNK_SIZE
# pieces, the last chunk absorbing the remainder (TikTok allows up to 128 MB
# for it). TikTok's limits: 5 MB <= chunk <= 64 MB.
SINGLE_CHUNK_MAX = 64_000_000
CHUNK_SIZE = 10_000_000

# Error codes that retrying cannot fix.
PERMANENT_CODES = frozenset(
    {
        "unaudited_client_can_only_post_to_private_accounts",
        "privacy_level_option_mismatch",
        "invalid_param",
        "invalid_file_upload",
        "scope_not_authorized",
        "scope_permission_missed",
        "spam_risk_user_banned_from_posting",
        "file_format_check_failed",
        "duration_check_failed",
        "frame_rate_check_failed",
        "picture_size_check_failed",
    }
)

AUDIT_HINT = (
    "L'app TikTok n'a pas encore passé l'audit Content Posting API : seules les publications privées "
    "(SELF_ONLY) sont autorisées en mode direct. Repasse `tiktok.mode` à `draft` dans config.yaml, "
    "ou mets `tiktok.privacy_level: SELF_ONLY`, en attendant l'audit."
)

STATUS_TIMEOUT = 300.0
STATUS_INTERVAL = 10.0


def chunk_plan(size: int) -> tuple[int, int]:
    """Return ``(chunk_size, total_chunk_count)`` following TikTok's chunking rules."""
    if size <= 0:
        raise PermanentError("TikTok : le fichier vidéo est vide.")
    if size <= SINGLE_CHUNK_MAX:
        return size, 1
    return CHUNK_SIZE, size // CHUNK_SIZE


class TikTokPublisher:
    name = "tiktok"

    def __init__(
        self,
        config: dict,
        token: dict,
        client: httpx.Client,
        env: Mapping[str, str] = os.environ,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config["tiktok"]
        self.token = dict(token)
        self.client = client
        self.env = env
        self.sleep = sleep

    # ------------------------------------------------------------------ auth

    def _credentials(self) -> tuple[str, str]:
        key = self.env.get("TIKTOK_CLIENT_KEY", "")
        secret = self.env.get("TIKTOK_CLIENT_SECRET", "")
        if not key or not secret:
            raise PermanentError("TikTok : secrets TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET manquants.")
        return key, secret

    def refresh_if_needed(self, now: float | None = None) -> bool:
        """Refresh the 24h access token. TikTok rotates the refresh token too, so it must be persisted."""
        if not expires_soon(self.token, margin=3600, now=now):
            return False
        key, secret = self._credentials()
        resp = self.client.post(
            TOKEN_URL,
            data={
                "client_key": key,
                "client_secret": secret,
                "grant_type": "refresh_token",
                "refresh_token": self.token.get("refresh_token", ""),
            },
        )
        body = _json(resp)
        if "access_token" not in body:
            error = body.get("error") or body
            message = f"TikTok : échec du rafraîchissement du jeton ({error})."
            if resp.status_code >= 500 or resp.status_code == 429:
                raise RetryableError(message)
            raise PermanentError(message + " Reconnecte le compte avec `python -m autotiktok auth tiktok`.")
        self.token.update(token_from_response(body, now=now))
        return True

    # --------------------------------------------------------------- publish

    def publish(self, video: Video) -> PublishResult:
        size = video.size
        chunk_size, chunk_count = chunk_plan(size)
        source_info = {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": chunk_count,
        }

        if self.config["mode"] == "direct":
            privacy = self._check_privacy()
            data = self._api(
                "/post/publish/video/init/",
                {"post_info": self._post_info(video, privacy), "source_info": source_info},
            )
        else:
            # Inbox uploads take no caption: the creator writes it when publishing the draft.
            data = self._api("/post/publish/inbox/video/init/", {"source_info": source_info})

        publish_id = data.get("publish_id")
        upload_url = data.get("upload_url")
        if not publish_id or not upload_url:
            raise RetryableError(f"TikTok : réponse d'initialisation inattendue : {data}")

        self._upload_chunks(video, upload_url, chunk_size, chunk_count)
        # From here on TikTok holds the video: never raise a retryable error,
        # or the next run would upload it a second time.
        return self._wait_for_status(publish_id)

    def _check_privacy(self) -> str:
        privacy = self.config["privacy_level"]
        info = self._api("/post/publish/creator_info/query/", {})
        options = info.get("privacy_level_options") or []
        if options and privacy not in options:
            message = (
                f"TikTok refuse le niveau de confidentialité '{privacy}' pour ce compte "
                f"(autorisés : {', '.join(options)})."
            )
            if options == ["SELF_ONLY"]:
                message = f"{message} {AUDIT_HINT}"
            raise PermanentError(message, response=info)
        return privacy

    def _post_info(self, video: Video, privacy: str) -> dict:
        info: dict = {"title": video.caption[:2200], "privacy_level": privacy}
        for field in ("disable_comment", "disable_duet", "disable_stitch", "is_aigc"):
            info[field] = bool(self.config.get(field, False))
        return info

    def _upload_chunks(self, video: Video, upload_url: str, chunk_size: int, chunk_count: int) -> None:
        size = video.size
        with video.path.open("rb") as fh:
            for index in range(chunk_count):
                start = index * chunk_size
                end = size - 1 if index == chunk_count - 1 else start + chunk_size - 1
                fh.seek(start)
                payload = fh.read(end - start + 1)
                resp = self.client.put(
                    upload_url,
                    content=payload,
                    headers={
                        "Content-Type": video.content_type,
                        "Content-Length": str(len(payload)),
                        "Content-Range": f"bytes {start}-{end}/{size}",
                    },
                )
                if resp.status_code not in (200, 201, 206):
                    raise RetryableError(
                        f"TikTok : échec de l'envoi du morceau {index + 1}/{chunk_count} "
                        f"(HTTP {resp.status_code}) : {resp.text[:300]}"
                    )

    def _wait_for_status(self, publish_id: str) -> PublishResult:
        result: dict = {}

        def check() -> bool:
            try:
                result.update(self._api("/post/publish/status/fetch/", {"publish_id": publish_id}))
            except RetryableError:
                return False
            return result.get("status") in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED")

        poll(check, timeout=STATUS_TIMEOUT, interval=STATUS_INTERVAL, sleep=self.sleep)
        status = result.get("status", "UNKNOWN")
        if status == "FAILED":
            raise PermanentError(f"TikTok a rejeté la vidéo : {result.get('fail_reason', 'raison inconnue')}")
        details = {"publish_id": publish_id, "tiktok_status": status, "mode": self.config["mode"]}
        post_ids = result.get("publicaly_available_post_id") or []
        if post_ids:
            details["post_ids"] = [str(p) for p in post_ids]
        return PublishResult(post_id=str(post_ids[0]) if post_ids else publish_id, details=details)

    # ------------------------------------------------------------------ http

    def _api(self, path: str, payload: dict) -> dict:
        resp = self.client.post(
            f"{API_BASE}{path}",
            json=payload,
            headers={
                "Authorization": f"Bearer {self.token.get('access_token', '')}",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
        body = _json(resp)
        error = body.get("error") or {}
        code = error.get("code", "")
        if resp.is_success and code in ("", "ok"):
            return body.get("data") or {}
        message = f"TikTok {path} : {code or 'HTTP ' + str(resp.status_code)} – {error.get('message', resp.text[:300])}"
        if code == "unaudited_client_can_only_post_to_private_accounts":
            message = f"{message}. {AUDIT_HINT}"
        if code in PERMANENT_CODES:
            raise PermanentError(message, response=body)
        if resp.status_code in (401, 429) or resp.status_code >= 500 or code:
            raise RetryableError(message, response=body)
        raise PermanentError(message, response=body)


def token_from_response(body: dict, now: float | None = None) -> dict:
    token = {
        "access_token": body["access_token"],
        "expires_at": expires_at(body.get("expires_in"), now),
    }
    if body.get("refresh_token"):
        token["refresh_token"] = body["refresh_token"]
    if body.get("refresh_expires_in"):
        token["refresh_expires_at"] = expires_at(body["refresh_expires_in"], now)
    if body.get("open_id"):
        token["open_id"] = body["open_id"]
    return token


def _json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
