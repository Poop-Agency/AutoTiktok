"""``publish-next``: publish the oldest queued video on every enabled platform."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

from .config import enabled_platforms
from .platforms import PUBLISHERS
from .platforms.base import HTTP_TIMEOUT, PublishError, RetryableError, Video
from .queue import FAILED, State, caption_for, finish_video, list_videos
from .tokens import TokenStore, key_from_env

STATE_FILE = Path("state/published.json")
TOKENS_FILE = Path("state/tokens.enc")

# Secrets each platform needs at publish time (Instagram only needs its token).
REQUIRED_ENV = {
    "instagram": (),
    "tiktok": ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
    "youtube": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
}


def missing_setup(platforms: list[str], store: TokenStore, env: Mapping[str, str]) -> list[str]:
    problems = []
    for platform in platforms:
        if not store.get(platform):
            problems.append(f"{platform} : compte non connecté (`python -m autotiktok auth {platform}`)")
        missing = [name for name in REQUIRED_ENV[platform] if not env.get(name)]
        if missing:
            problems.append(f"{platform} : secret(s) GitHub manquant(s) : {', '.join(missing)}")
    return problems


def _log(message: str) -> None:
    print(message, flush=True)


def publish_next(
    root: Path,
    config: dict,
    *,
    dry_run: bool = False,
    client: httpx.Client | None = None,
    env: Mapping[str, str] = os.environ,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Returns the process exit code: 0 when everything went fine, 1 when a platform failed."""
    root = Path(root)
    platforms = enabled_platforms(config)
    if not platforms:
        _log("Aucune plateforme activée dans config.yaml.")
        return 1
    state = State(root / STATE_FILE)
    videos = list_videos(root / config["queue"]["input_dir"])

    if dry_run:
        return _dry_run(videos, platforms, state, config)

    store = TokenStore(root / TOKENS_FILE, key_from_env())
    problems = missing_setup(platforms, store, env)
    if problems:
        # A setup problem is not a publishing failure: stop without touching the
        # queue, so no video is burnt before the accounts are connected.
        _log("Configuration incomplète, aucune vidéo n'a été publiée :")
        for problem in problems:
            _log(f"  - {problem}")
        _log("Connecte les comptes manquants ou désactive ces plateformes dans config.yaml.")
        return 1

    own_client = client is None
    client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False)
    failures = 0
    try:
        publishers = {}
        for platform in platforms:
            token = store.get(platform)
            if token:
                publishers[platform] = PUBLISHERS[platform](config, token, client, env=env, sleep=sleep)

        # Refresh every token on each run, even with an empty queue: Instagram
        # tokens die after 60 days and TikTok refresh tokens after a year.
        refresh_errors: dict[str, PublishError] = {}
        for platform, publisher in publishers.items():
            try:
                if publisher.refresh_if_needed():
                    store.set(platform, publisher.token)
                    store.save()
                    _log(f"[{platform}] jeton rafraîchi")
            except Exception as exc:  # noqa: BLE001 - network errors included
                if not isinstance(exc, PublishError):
                    exc = RetryableError(f"{type(exc).__name__}: {exc}")
                refresh_errors[platform] = exc
                _log(f"[{platform}] ERREUR jeton : {exc}")

        if not videos:
            _log("File d'attente vide : ajoute des vidéos dans input/.")
            return 1 if refresh_errors else 0

        video_path = videos[0]
        name = video_path.name
        caption = caption_for(video_path, config)
        max_attempts = int(config["queue"]["max_attempts"])
        _log(f"Vidéo : {name} ({video_path.stat().st_size / 1e6:.1f} Mo) – légende : {caption!r}")

        for platform in platforms:
            if state.is_final(name, platform):
                _log(f"[{platform}] déjà traité ({state.platform(name, platform)['status']})")
                continue
            publisher = publishers[platform]
            try:
                if platform in refresh_errors:
                    raise refresh_errors[platform]
                result = publisher.publish(Video(path=video_path, caption=caption))
            except PublishError as exc:
                failures += 1
                status = state.record_failure(
                    name, platform, str(exc), retryable=exc.retryable, max_attempts=max_attempts
                )
                _log(f"[{platform}] ERREUR ({status}) : {exc}")
            except Exception as exc:  # noqa: BLE001 - one platform must never break the others
                failures += 1
                status = state.record_failure(
                    name, platform, f"{type(exc).__name__}: {exc}", retryable=True, max_attempts=max_attempts
                )
                _log(f"[{platform}] ERREUR inattendue ({status}) : {type(exc).__name__}: {exc}")
            else:
                state.record_success(name, platform, result.post_id, result.details)
                _log(f"[{platform}] publié : {result.post_id} {result.details or ''}".rstrip())
            finally:
                if publisher.token != store.get(platform):
                    store.set(platform, publisher.token)
                    store.save()

        if all(state.is_final(name, p) for p in platforms):
            disposition = finish_video(video_path, config, root)
            state.mark_done(name, disposition)
            failed = [p for p in platforms if state.platform(name, p).get("status") == FAILED]
            suffix = f" (abandonné sur : {', '.join(failed)})" if failed else ""
            _log(f"{name} terminé, fichier {'déplacé' if disposition == 'moved' else 'supprimé'}{suffix}.")
        else:
            _log(f"{name} reste dans la file : nouvel essai au prochain passage.")
        return 1 if failures or refresh_errors else 0
    finally:
        store.save()
        if own_client:
            client.close()


def _dry_run(videos: list[Path], platforms: list[str], state: State, config: dict) -> int:
    if not videos:
        _log("File d'attente vide.")
        return 0
    video = videos[0]
    todo = [p for p in platforms if not state.is_final(video.name, p)]
    _log(f"Prochaine vidéo : {video.name}")
    _log(f"Légende : {caption_for(video, config)!r}")
    _log(f"Plateformes à publier : {', '.join(todo) or 'aucune'}")
    _log(f"Vidéos en attente : {len(videos)}")
    return 0


def status(root: Path, config: dict) -> None:
    state = State(Path(root) / STATE_FILE)
    videos = list_videos(Path(root) / config["queue"]["input_dir"])
    _log(f"{len(videos)} vidéo(s) en attente (≈ {len(videos)} publication(s), une par passage).")
    for video in videos[:10]:
        entries = state.video(video.name)["platforms"] if video.name in state.data["videos"] else {}
        summary = ", ".join(f"{p}={e.get('status')}" for p, e in entries.items()) or "jamais tentée"
        _log(f"  {video.name} : {summary}")
    if len(videos) > 10:
        _log(f"  … et {len(videos) - 10} autre(s)")
    recent = sorted(
        ((n, v) for n, v in state.data["videos"].items() if v.get("done_at")),
        key=lambda item: item[1]["done_at"],
    )[-5:]
    if recent:
        _log("Dernières vidéos terminées :")
        for name, entry in recent:
            summary = ", ".join(f"{p}={e.get('status')}" for p, e in entry["platforms"].items())
            _log(f"  {entry['done_at']} {name} : {summary}")
