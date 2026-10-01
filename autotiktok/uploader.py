"""``post-next``: take a Reel from the index, download it, and publish it on the upload account.

The upload goes through instagram.com in headless Chromium, logged in with the cookies of the upload
account (``upload.cookies_file``), the same way the browsing account is read by ``refresh-index``.
"""

from __future__ import annotations

import json
from pathlib import Path

from .fetcher import FETCHED_FILE, FetchError, fetch_next, load_cookies, playwright_cookies
from .publisher import ARCHIVE_FILE
from .queue import Archive, caption_for, file_sha256, finish_video, list_videos, now_iso

PLATFORM = "instagram"
PROFILE_SETTLE_MS = 6000
POLL_INTERVAL_S = 30


class UploadError(Exception):
    pass


class UploadUncertain(UploadError):
    """Share was clicked but the Reel was not seen on the profile: it may or may not be published."""


def _log(message: str) -> None:
    print(message, flush=True)


def cookies_problem(path: Path) -> str | None:
    """Why the upload cookies cannot be used yet (None when they look fine)."""
    if not path.is_file():
        return f"{path.name} introuvable : exporte les cookies d'instagram.com du compte sur lequel publier."
    try:
        names = {c.name for c in load_cookies(path)}
    except FetchError as exc:
        return str(exc)
    if "sessionid" not in names:
        return f"{path.name} ne contient pas de cookie « sessionid » : colle l'export des cookies du compte d'upload."
    return None


class BrowserUploader:
    """``with BrowserUploader(cookies) as up: up.upload(video, caption)`` publishes a Reel from the web UI."""

    def __init__(
        self,
        cookies,
        *,
        executable_path: str = "",
        headless: bool = True,
        share_timeout: float = 240,
    ):
        self.cookies = cookies
        self.executable_path = executable_path
        self.headless = headless
        self.share_timeout = share_timeout

    def __enter__(self) -> BrowserUploader:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        self._playwright = sync_playwright().start()
        try:
            launch = {"headless": self.headless}
            if self.executable_path:
                launch["executable_path"] = self.executable_path
            self._browser = self._playwright.chromium.launch(**launch)
            context = self._browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
            context.add_cookies(playwright_cookies(self.cookies))
            self._page = context.new_page()
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc) -> None:
        browser = getattr(self, "_browser", None)
        if browser is not None:
            browser.close()
        self._playwright.stop()

    def _dismiss_popups(self) -> None:
        for label in ("Not Now", "Not now"):
            button = self._page.get_by_role("button", name=label)
            if button.count():
                button.first.click()
                self._page.wait_for_timeout(1500)

    def _profile_path(self) -> str:
        """``/username/`` of the logged-in account, read from the sidebar's Profile link."""
        link = self._page.get_by_role("link", name="Profile").first
        href = link.get_attribute("href", timeout=15000) if link.count() else None
        if not href:
            raise UploadError("Profil du compte d'upload introuvable : la session est-elle connectée ?")
        return href

    def _profile_reels(self, profile_path: str) -> set[str]:
        page = self._page
        page.goto(f"https://www.instagram.com{profile_path}reels/", wait_until="domcontentloaded")
        page.wait_for_timeout(PROFILE_SETTLE_MS)
        links = page.locator('a[href*="/reel/"]').all()
        return {href for link in links if (href := link.get_attribute("href"))}

    def upload(self, video: Path, caption: str) -> dict:
        """Publish ``video`` as a Reel. Returns ``{"url": ..., "username": ...}``."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: PLC0415

        page = self._page
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        if "/accounts/login" in page.url:
            raise UploadError("Instagram demande de se connecter : cookies du compte d'upload expirés.")
        self._dismiss_popups()
        profile_path = self._profile_path()
        before = self._profile_reels(profile_path)

        page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        self._dismiss_popups()
        try:
            page.locator('svg[aria-label="New post"]').first.click(timeout=15000)
            page.wait_for_timeout(2000)
            # Some layouts open a small menu first (Post / Reel / ...): pick "Post" when it is there.
            menu = page.get_by_role("link", name="Post")
            if menu.count():
                menu.first.click(timeout=5000)
                page.wait_for_timeout(2000)
            with page.expect_file_chooser(timeout=15000) as chooser:
                page.get_by_role("button", name="Select from computer").click()
            chooser.value.set_files(str(video))
            page.wait_for_timeout(8000)
            ok = page.get_by_role("button", name="OK")
            if ok.count():
                ok.first.click()
                page.wait_for_timeout(1500)
            for _ in range(2):  # crop, then edit
                page.get_by_role("button", name="Next").first.click(timeout=20000)
                page.wait_for_timeout(4000)
            if caption:
                page.locator('div[aria-label="Write a caption..."]').first.fill(caption)
                page.wait_for_timeout(500)
            page.get_by_role("button", name="Share").first.click(timeout=15000)
        except PlaywrightTimeout as exc:
            raise UploadError(f"Interface d'Instagram inattendue, rien n'a été publié : {str(exc)[:200]}") from exc

        # Closing the browser while Instagram says "Sharing" can cancel the post: wait until the
        # Reel shows up on the profile.
        waited = 0.0
        while waited < self.share_timeout:
            page.wait_for_timeout(POLL_INTERVAL_S * 1000)
            waited += POLL_INTERVAL_S
            new = self._profile_reels(profile_path) - before
            if new:
                reel = sorted(new)[0]
                return {"url": f"https://www.instagram.com{reel}", "username": profile_path.strip("/")}
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        raise UploadUncertain(
            f"Le Reel n'est pas apparu sur le profil après {int(self.share_timeout)} s : il est peut-être publié. "
            "Vérifie le profil avant de relancer."
        )


def _source_of(root: Path, filename: str) -> dict:
    """The ledger entry (source Reel) of a downloaded file."""
    path = root / FETCHED_FILE
    if not path.exists():
        return {}
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            entry = json.loads(line)
            if entry.get("file") == filename:
                return entry
    return {}


def post_next(
    root: Path,
    config: dict,
    *,
    dry_run: bool = False,
    uploader: BrowserUploader | None = None,
    **fetch_kwargs,
) -> int:
    """Download a Reel from the index (unless a video is already waiting) and publish it on the upload account."""
    root = Path(root)
    settings = config["upload"]
    input_dir = root / config["queue"]["input_dir"]
    waiting = list_videos(input_dir)

    if dry_run:
        if waiting:
            _log(f"Prochaine vidéo : {waiting[0].name} (déjà dans input/, pas de nouveau téléchargement).")
            _log(f"Légende : {caption_for(waiting[0], config)!r}")
            return 0
        return fetch_next(root, config, dry_run=True, **fetch_kwargs)

    cookies_path = root / settings["cookies_file"]
    # A setup problem must not burn a Reel: check the upload account before downloading anything.
    problem = cookies_problem(cookies_path)
    if problem:
        _log(f"Configuration incomplète, aucun Reel téléchargé : {problem}")
        return 1

    if waiting:
        _log(f"{len(waiting)} vidéo(s) déjà dans input/ : pas de nouveau téléchargement.")
    else:
        code = fetch_next(root, config, **fetch_kwargs)
        if code != 0:
            return code
        waiting = list_videos(input_dir)
        if not waiting:
            _log("Le téléchargement n'a produit aucun fichier vidéo.")
            return 1

    video = waiting[0]
    sha256 = file_sha256(video)
    archive = Archive(root / ARCHIVE_FILE)
    if archive.contains(sha256):
        disposition = finish_video(video, config, root)
        _log(f"{video.name} a déjà été publiée (même fichier dans l'archive) : ignorée ({disposition}).")
        return 0

    caption = caption_for(video, config)
    _log(f"Vidéo : {video.name} ({video.stat().st_size / 1e6:.1f} Mo) – légende : {caption!r}")
    uploader = uploader or BrowserUploader(
        load_cookies(cookies_path),
        executable_path=settings["browser_path"],
        headless=settings["headless"],
        share_timeout=settings["share_timeout"],
    )
    try:
        with uploader:
            result = uploader.upload(video, caption)
    except UploadError as exc:
        _log(f"ERREUR : {exc}")
        _log(f"{video.name} reste dans input/.")
        return 1

    source = _source_of(root, video.name)
    disposition = finish_video(video, config, root)
    archive.add(
        {
            "name": video.name,
            "sha256": sha256,
            "done_at": now_iso(),
            "disposition": disposition,
            "source_url": source.get("url"),
            "source_account": source.get("account"),
            "platforms": {
                PLATFORM: {"status": "published", "post_id": result["url"], "at": now_iso(), "details": result}
            },
        }
    )
    _log(f"Publié sur @{result['username']} : {result['url']}")
    _log(f"{video.name} terminé, fichier {'déplacé' if disposition == 'moved' else 'supprimé'}.")
    return 0
