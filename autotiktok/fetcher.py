"""Reels of the pool accounts: ``refresh-index`` lists them, ``post-next`` downloads one and publishes it.

Only meant for accounts you own or have explicit permission to reuse.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import random
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from urllib.parse import urlparse

from .config import enabled_platforms
from .publisher import TOKENS_FILE, missing_setup, publish_next
from .queue import list_videos, now_iso
from .tokens import TokenStore, key_from_env

FETCHED_FILE = Path("state/fetched.jsonl")


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


# --- listing the Reels with a real browser -------------------------------------------------------------

# Instagram's own API answers 429 to anything that does not look like its web app, so the Reels tab
# of a profile is opened in headless Chromium (with the cookies of the browsing account) and the
# GraphQL answers the page receives while scrolling are read.
MAX_IDLE_SCROLLS = 4


def _find_key(obj, key: str):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def parse_reels(payload: dict) -> tuple[list[dict], bool | None]:
    """Reels of one GraphQL answer of the profile Reels tab, and whether more pages follow (None if unknown)."""
    connection = _find_key(payload, "clips_connection")
    if not isinstance(connection, dict):
        return [], None
    reels = []
    for edge in connection.get("edges") or []:
        media = (edge.get("node") or {}).get("media") or {}
        code = media.get("code")
        if not code:
            continue
        reels.append(
            {
                "id": code,
                "url": f"https://www.instagram.com/reel/{code}/",
                "views": media.get("play_count") or media.get("view_count") or 0,
                "caption": ((media.get("caption") or {}).get("text") or "").strip(),
            }
        )
    return reels, (connection.get("page_info") or {}).get("has_next_page")


class BrowserLister:
    """``with BrowserLister(...) as lister: lister.reels("user")`` yields the Reels page by page."""

    def __init__(
        self,
        cookies: http.cookiejar.CookieJar,
        *,
        delay: tuple[float, float] = (3, 6),
        limit: int = 0,
        executable_path: str = "",
        headless: bool = True,
        rng: random.Random | None = None,
    ):
        self.cookies = cookies
        self.delay = delay
        self.limit = limit
        self.executable_path = executable_path
        self.headless = headless
        self.rng = rng or random.SystemRandom()
        self._pending: list[tuple[list[dict], bool | None]] = []
        self._rate_limited = False

    def __enter__(self) -> BrowserLister:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        self._playwright = sync_playwright().start()
        try:
            launch = {"headless": self.headless}
            if self.executable_path:
                launch["executable_path"] = self.executable_path
            self._browser = self._playwright.chromium.launch(**launch)
            context = self._browser.new_context(viewport={"width": 1280, "height": 900})
            context.add_cookies(
                [
                    {
                        "name": c.name,
                        "value": c.value or "",
                        "domain": c.domain,
                        "path": c.path,
                        "secure": bool(c.secure),
                        "expires": c.expires or -1,
                    }
                    for c in self.cookies
                ]
            )
            self._page = context.new_page()
            self._page.on("response", self._on_response)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc) -> None:
        browser = getattr(self, "_browser", None)
        if browser is not None:
            browser.close()
        self._playwright.stop()

    def _on_response(self, response) -> None:
        if "graphql" not in response.url:
            return
        if response.status == 429:
            self._rate_limited = True
            return
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - not every GraphQL answer is JSON we care about
            return
        reels, has_next = parse_reels(payload) if isinstance(payload, dict) else ([], None)
        if reels or has_next is not None:
            self._pending.append((reels, has_next))

    def reels(self, username: str) -> Iterator[list[dict]]:
        """The account's Reels (newest first), one page at a time: ``{id, url, views, caption}``."""
        page = self._page
        self._pending.clear()
        self._rate_limited = False
        response = page.goto(f"https://www.instagram.com/{username}/reels/", wait_until="domcontentloaded")
        if "/accounts/login" in page.url:
            raise FetchError("Instagram demande de se connecter : cookies expirés (ré-exporte le fichier de cookies).")
        if response is not None and response.status == 429:
            raise RateLimited("Instagram limite les requêtes (429) : réessaie plus tard.")
        if response is not None and response.status == 404:
            raise FetchError(f"Compte introuvable : {username}.")

        seen = idle = 0
        has_next: bool | None = True
        while True:
            page.wait_for_timeout(self.rng.uniform(*self.delay) * 1000)
            if self._rate_limited:
                raise RateLimited("Instagram limite les requêtes (429) : réessaie plus tard.")
            pending, self._pending = self._pending, []
            batch = [reel for reels, _ in pending for reel in reels]
            for _, flag in pending:
                if flag is not None:
                    has_next = flag
            if batch:
                seen += len(batch)
                idle = 0
                yield batch
            else:
                idle += 1
            if has_next is False or idle >= MAX_IDLE_SCROLLS or (self.limit and seen >= self.limit):
                return
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")


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


def _default_lister(root: Path, settings: dict) -> BrowserLister | None:
    cookies_path = root / settings["cookies_file"]
    if not cookies_path.is_file():
        _log(f"{settings['cookies_file']} introuvable : exporte les cookies d'une session Instagram (voir le README).")
        return None
    try:
        jar = load_cookies(cookies_path)
    except FetchError as exc:
        _log(str(exc))
        return None
    return BrowserLister(
        jar,
        delay=tuple(settings["request_delay"]),
        limit=settings["max_reels_per_account"],
        executable_path=settings["browser_path"],
        headless=settings["headless"],
    )


def refresh_index(
    root: Path,
    config: dict,
    *,
    lister: BrowserLister | None = None,
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
    lister = lister or _default_lister(root, settings)
    if lister is None:
        return 1

    index_path = root / settings["index_file"]
    index = load_index(index_path)
    failures = 0
    with lister:
        for profile in pool:
            username = username_of(profile)
            _log(f"Compte : {username}")
            kept = read = 0
            try:
                for batch in lister.reels(username):
                    read += len(batch)
                    for reel in batch:
                        if reel["views"] < settings["min_views"]:
                            continue
                        kept += 1
                        entry = index["reels"].setdefault(reel["id"], {"first_seen": now_iso()})
                        entry.update(
                            url=reel["url"],
                            account=username,
                            views=reel["views"],
                            caption=reel["caption"] or entry.get("caption", ""),
                            updated_at=now_iso(),
                        )
                        entry.pop("gone", None)
                    save_index(index_path, index)
            except RateLimited as exc:
                save_index(index_path, index)
                _log(f"  {exc} Arrêt : {len(index['reels'])} Reel(s) dans l'index, relance plus tard.")
                return 1
            except FetchError as exc:
                failures += 1
                _log(f"  ignoré : {exc}")
            else:
                _log(f"  {read} Reel(s) lus, {kept} à plus de {settings['min_views']} vues.")
            save_index(index_path, index)
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
