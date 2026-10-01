"""Reels of the pool accounts: ``refresh-index`` lists them, ``post-next`` downloads one and publishes it.

Only meant for accounts you own or have explicit permission to reuse.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import random
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import enabled_platforms
from .publisher import TOKENS_FILE, missing_setup, publish_next
from .queue import list_videos, now_iso
from .tokens import TokenStore, key_from_env

FETCHED_FILE = Path("state/fetched.jsonl")

IG_APP_ID = "936619743392459"  # public app id of Instagram's own web client
IG_API = "https://www.instagram.com/api/v1"
DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
PAGE_SIZE = 12


class FetchError(Exception):
    pass


class RateLimited(FetchError):
    """Instagram answered 429: stop everything and try again later."""


def _log(message: str) -> None:
    print(message, flush=True)


# --- the pool and the cookies of the browsing account --------------------------------------------


def read_pool(path: Path) -> list[str]:
    """Profile URLs from the pool file, one per line (blank lines and ``#`` comments ignored)."""
    if not Path(path).is_file():
        return []
    lines = (line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def username_of(profile_url: str) -> str:
    """``https://www.instagram.com/<user>/`` (with or without trailing slash) -> ``<user>``."""
    return urlparse(profile_url).path.strip("/").split("/")[0]


def load_cookies(path: Path) -> http.cookiejar.MozillaCookieJar:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise FetchError(f"{path.name} illisible (format Netscape attendu) : {exc}") from exc
    return jar


# --- Instagram's web API ----------------------------------------------------------------------------


def _api(client: httpx.Client, method: str, path: str, user_agent: str, **kwargs) -> dict:
    headers = {
        "X-IG-App-ID": IG_APP_ID,
        "X-CSRFToken": client.cookies.get("csrftoken") or "",
        "Referer": "https://www.instagram.com/",
        "User-Agent": user_agent,
    }
    response = client.request(method, f"{IG_API}{path}", headers=headers, **kwargs)
    if response.status_code in (401, 403):
        raise FetchError("Instagram refuse la session : cookies absents ou expirés (ré-exporte le fichier de cookies).")
    if response.status_code == 429:
        raise RateLimited("Instagram limite les requêtes (429) : réessaie plus tard.")
    if response.status_code != 200:
        raise FetchError(f"Instagram a répondu {response.status_code} sur {path}.")
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(f"Réponse Instagram illisible sur {path}.") from exc


def _reel_of(item: dict) -> dict | None:
    media = item.get("media") or {}
    code = media.get("code")
    if not code:
        return None
    return {
        "id": code,
        "url": f"https://www.instagram.com/reel/{code}/",
        "views": media.get("play_count") or media.get("view_count") or 0,
        "caption": ((media.get("caption") or {}).get("text") or "").strip(),
    }


def iter_reels(
    client: httpx.Client,
    username: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    limit: int = 0,
    delay: tuple[float, float] = (0, 0),
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> Iterator[list[dict]]:
    """The account's Reels, one page at a time (newest first): ``{id, url, views, caption}``.

    ``limit`` caps the number of Reels read (0 = all of them). A random pause of ``delay`` seconds
    separates two requests, to stay under Instagram's rate limit.
    """
    rng = rng or random.SystemRandom()
    profile = _api(client, "GET", "/users/web_profile_info/", user_agent, params={"username": username})
    user = (profile.get("data") or {}).get("user")
    if not user or not user.get("id"):
        raise FetchError(f"Compte introuvable ou privé : {username}.")

    seen = 0
    max_id = ""
    while True:
        sleep(rng.uniform(*delay))
        form = {"target_user_id": user["id"], "page_size": str(PAGE_SIZE)}
        if max_id:
            form["max_id"] = max_id
        data = _api(client, "POST", "/clips/user/", user_agent, data=form)
        page = [reel for item in data.get("items") or [] if (reel := _reel_of(item))]
        seen += len(page)
        yield page
        paging = data.get("paging_info") or {}
        max_id = paging.get("max_id") or ""
        if not page or not paging.get("more_available") or not max_id or (limit and seen >= limit):
            return


# --- the index of Reels above the view threshold ---------------------------------------------------


def load_index(path: Path) -> dict:
    if Path(path).exists():
        index = json.loads(Path(path).read_text(encoding="utf-8"))
        index.setdefault("reels", {})
        return index
    return {"updated_at": None, "reels": {}}


def save_index(path: Path, index: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    index["updated_at"] = now_iso()
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_fetched(path: Path) -> set[str]:
    """Ids of the Reels already downloaded, so the same one is never drawn twice."""
    if not Path(path).exists():
        return set()
    entries = (json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip())
    return {entry["id"] for entry in entries}


def record_fetched(path: Path, entry: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _open_client(root: Path, settings: dict) -> httpx.Client | None:
    cookies_path = root / settings["cookies_file"]
    if not cookies_path.is_file():
        _log(f"{settings['cookies_file']} introuvable : exporte les cookies d'une session Instagram (voir le README).")
        return None
    try:
        jar = load_cookies(cookies_path)
    except FetchError as exc:
        _log(str(exc))
        return None
    return httpx.Client(cookies=jar, timeout=30, follow_redirects=True)


def refresh_index(
    root: Path,
    config: dict,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> int:
    """List every Reel above ``min_views`` of every pool account into the index file.

    Returns 0 when every account was read, 1 when something went wrong (what was found is kept).
    """
    root = Path(root)
    settings = config["fetch"]
    pool = read_pool(root / settings["pool_file"])
    if not pool:
        _log(f"Aucun compte dans {settings['pool_file']}.")
        return 1
    own_client = client is None
    client = client or _open_client(root, settings)
    if client is None:
        return 1

    index_path = root / settings["index_file"]
    index = load_index(index_path)
    delay = tuple(settings["request_delay"])
    user_agent = settings["user_agent"] or DEFAULT_USER_AGENT
    failures = 0
    try:
        for profile in pool:
            username = username_of(profile)
            _log(f"Compte : {username}")
            kept = read = 0
            try:
                for page in iter_reels(
                    client,
                    username,
                    user_agent=user_agent,
                    limit=settings["max_reels_per_account"],
                    delay=delay,
                    sleep=sleep,
                    rng=rng,
                ):
                    read += len(page)
                    for reel in page:
                        if reel["views"] < settings["min_views"]:
                            continue
                        kept += 1
                        entry = index["reels"].setdefault(reel["id"], {"first_seen": now_iso()})
                        entry.update(
                            url=reel["url"],
                            account=username,
                            views=reel["views"],
                            caption=reel["caption"],
                            updated_at=now_iso(),
                        )
                        entry.pop("gone", None)
            except RateLimited as exc:
                save_index(index_path, index)
                _log(f"  {exc} Arrêt : {len(index['reels'])} Reel(s) dans l'index, relance plus tard.")
                return 1
            except (FetchError, httpx.HTTPError) as exc:
                failures += 1
                _log(f"  ignoré : {exc}")
            else:
                _log(f"  {read} Reel(s) lus, {kept} à plus de {settings['min_views']} vues.")
            save_index(index_path, index)
    finally:
        if own_client:
            client.close()
    _log(f"Index : {len(index['reels'])} Reel(s) dans {settings['index_file']}.")
    return 1 if failures else 0


# --- download one Reel from the index -----------------------------------------------------------------


def _default_ydl(opts: dict):
    import yt_dlp

    return yt_dlp.YoutubeDL(opts)


def fetch_next(
    root: Path,
    config: dict,
    *,
    dry_run: bool = False,
    ydl_factory: Callable[[dict], object] = _default_ydl,
    rng: random.Random | None = None,
) -> int:
    """Pick a random Reel of the index never downloaded before and put it in ``input/``."""
    from yt_dlp.utils import DownloadError  # noqa: PLC0415

    root = Path(root)
    rng = rng or random.SystemRandom()
    settings = config["fetch"]
    index_path = root / settings["index_file"]
    index = load_index(index_path)
    fetched = load_fetched(root / FETCHED_FILE)
    candidates = [
        (reel_id, entry)
        for reel_id, entry in index["reels"].items()
        if reel_id not in fetched and not entry.get("gone")
    ]
    if not candidates:
        _log("Aucun Reel disponible dans l'index : lance `python -m autotiktok refresh-index`.")
        return 1
    rng.shuffle(candidates)
    _log(f"{len(candidates)} Reel(s) disponible(s) dans l'index.")

    cookies_path = root / settings["cookies_file"]
    input_dir = root / config["queue"]["input_dir"]
    for reel_id, entry in candidates:
        _log(f"Reel {reel_id} : {entry['views']} vues, compte {entry['account']} ({entry['url']})")
        if dry_run:
            _log("Simulation : rien n'est téléchargé.")
            return 0

        input_dir.mkdir(parents=True, exist_ok=True)
        target = input_dir / f"ig_{reel_id}.mp4"
        options = {
            "quiet": True,
            "no_warnings": True,
            "outtmpl": str(target.with_suffix(".%(ext)s")),
            "format": "mp4/best",
            "merge_output_format": "mp4",
        }
        if cookies_path.is_file():
            options["cookiefile"] = str(cookies_path)
        try:
            with ydl_factory(options) as ydl:
                ydl.download([entry["url"]])
        except DownloadError as exc:
            _log(f"  téléchargement impossible, Reel écarté de l'index : {str(exc)[:300]}")
            entry["gone"] = True
            save_index(index_path, index)
            continue

        if entry.get("caption"):
            target.with_suffix(".txt").write_text(entry["caption"] + "\n", encoding="utf-8")
        record_fetched(
            root / FETCHED_FILE,
            {
                "id": reel_id,
                "url": entry["url"],
                "account": entry["account"],
                "views": entry["views"],
                "file": target.name,
                "at": now_iso(),
            },
        )
        _log(f"Téléchargé : {target.name}")
        return 0

    _log("Aucun Reel de l'index n'a pu être téléchargé.")
    return 1


def post_next(
    root: Path,
    config: dict,
    *,
    dry_run: bool = False,
    env: Mapping[str, str] = os.environ,
    **fetch_kwargs,
) -> int:
    """Download a Reel from the index (unless a video is already waiting) and publish it."""
    root = Path(root)
    waiting = list_videos(root / config["queue"]["input_dir"])
    if waiting:
        _log(f"{len(waiting)} vidéo(s) déjà dans input/ : pas de nouveau téléchargement.")
    elif dry_run:
        return fetch_next(root, config, dry_run=True, **fetch_kwargs)
    else:
        # A setup problem must not burn a Reel: check the accounts before downloading anything.
        platforms = enabled_platforms(config)
        problems = missing_setup(platforms, TokenStore(root / TOKENS_FILE, key_from_env()), env) if platforms else []
        if problems:
            _log("Configuration incomplète, aucun Reel téléchargé :")
            for problem in problems:
                _log(f"  - {problem}")
            return 1
        code = fetch_next(root, config, **fetch_kwargs)
        if code != 0:
            return code
    return publish_next(root, config, dry_run=dry_run, env=env)
