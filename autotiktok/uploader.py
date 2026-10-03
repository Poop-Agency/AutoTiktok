"""``post-next``: take a Reel from the index, download it, and publish it on the upload account.

The upload goes through instagram.com in headless Chromium, logged in with the cookies of the upload
account (``upload.cookies_file``), the same way the browsing account is read by ``refresh-index``.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable
from pathlib import Path

from .fetcher import FETCHED_FILE, FetchError, fetch_next, load_cookies, playwright_cookies
from .publisher import ARCHIVE_FILE
from .queue import Archive, caption_for, file_sha256, finish_video, list_videos, now_iso

PLATFORM = "instagram"
PROFILE_SETTLE_MS = 6000
DIALOG_POLL_S = 5
CONFIRM_POLL_S = 10
CONFIRM_ATTEMPTS = 6
RECENT_REELS = 6  # a new Reel shows up among the first tiles of the profile
FAILURE_PHRASES = ("couldn't be shared", "could not be shared", "went wrong", "try again", "failed", "error")


class UploadError(Exception):
    pass


class UploadUncertain(UploadError):
    """Share was clicked but the Reel was not seen on the profile: it may or may not be published."""


def _log(message: str) -> None:
    print(message, flush=True)


def parse_post_count(header: str) -> int | None:
    """``"... | 31 posts | 2 followers ..."`` -> 31."""
    match = re.search(r"([\d.,\s]+)\s*posts?\b", header)
    digits = re.sub(r"\D", "", match.group(1)) if match else ""
    return int(digits) if digits else None


def new_reel(before: set[str], after: list[str], posts_before: int | None, posts_after: int | None) -> str | None:
    """The Reel that appeared on the profile, from the tiles before and after (newest tile first).

    Only the first tiles count: the grid loads lazily, so older Reels that were simply not loaded the first
    time must not be taken for a new one. When both post counts are known, the count must have grown.
    """
    if posts_before is not None and posts_after is not None and posts_after <= posts_before:
        return None
    return next((href for href in after[:RECENT_REELS] if href not in before), None)


def share_failure(dialog_text: str) -> str | None:
    """The text of Instagram's dialog when it reports that the post failed (None otherwise)."""
    lowered = dialog_text.lower()
    return dialog_text.strip() if any(phrase in lowered for phrase in FAILURE_PHRASES) else None


def share_confirmed(dialog_text: str) -> bool:
    """Instagram's dialog says the Reel was shared (and not that it could not be)."""
    return share_failure(dialog_text) is None and re.search(r"\bshared\b", dialog_text.lower()) is not None


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
        debug_dir: Path | None = None,
    ):
        self.cookies = cookies
        self.executable_path = executable_path
        self.headless = headless
        self.share_timeout = share_timeout
        self.debug_dir = debug_dir

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

    def _profile_state(self, profile_path: str) -> tuple[list[str], int | None]:
        """The Reel tiles of the profile (newest first, as displayed) and its post count."""
        page = self._page
        page.goto(f"https://www.instagram.com{profile_path}reels/", wait_until="domcontentloaded")
        page.wait_for_timeout(PROFILE_SETTLE_MS)
        hrefs = [href for link in page.locator('a[href*="/reel/"]').all() if (href := link.get_attribute("href"))]
        header = page.locator("header").first
        return list(dict.fromkeys(hrefs)), parse_post_count(header.inner_text()) if header.count() else None

    def _dialog_text(self) -> str:
        dialog = self._page.locator("div[role=dialog]")
        try:
            return dialog.last.inner_text(timeout=2000) if dialog.count() else ""
        except Exception:  # noqa: BLE001 - the dialog may close while it is being read
            return ""

    def _save_debug(self, note: str) -> None:
        """Keep a screenshot and the dialog text of a failed upload, to understand what Instagram showed."""
        if not self.debug_dir:
            return
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            self._page.screenshot(path=str(self.debug_dir / "upload_failure.png"))
            (self.debug_dir / "upload_failure.txt").write_text(f"{note}\n\n{self._dialog_text()}\n", encoding="utf-8")
            _log(f"Capture de l'écran d'Instagram : {self.debug_dir / 'upload_failure.png'}")
        except Exception:  # noqa: BLE001 - debugging aid, never the cause of a failure
            pass

    def _compose(self, video: Path, caption: str, cover: Path | None) -> None:
        """Walk the "new post" dialog up to the details page (caption filled, cover set), without sharing."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: PLC0415

        page = self._page
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
            # Instagram's web composer crops a video from the computer to a square by default, which cuts off the
            # top and the bottom of a vertical Reel: ask for the original ratio.
            page.locator('div[role="dialog"] svg[aria-label="Select crop"]').first.click(timeout=15000)
            page.wait_for_timeout(1000)
            page.get_by_text("Original", exact=True).first.click(timeout=15000)
            page.wait_for_timeout(1500)
            page.get_by_role("button", name="Next").first.click(timeout=20000)  # crop -> edit
            page.wait_for_timeout(4000)
            if cover:
                # The edit page has a "Cover photo" block with its own "Select from computer" button.
                with page.expect_file_chooser(timeout=15000) as chooser:
                    page.get_by_role("button", name="Select from computer").click()
                chooser.value.set_files(str(cover))
                page.wait_for_timeout(4000)
            page.get_by_role("button", name="Next").first.click(timeout=20000)  # edit -> details
            page.wait_for_timeout(4000)
            if caption:
                page.locator('div[role="textbox"][aria-label*="caption" i]').first.fill(caption)
                page.wait_for_timeout(500)
        except PlaywrightTimeout as exc:
            raise UploadError(f"Interface d'Instagram inattendue, rien n'a été publié : {str(exc)[:200]}") from exc

    def upload(self, video: Path, caption: str, cover: Path | None = None) -> dict:
        """Publish ``video`` as a Reel. Returns ``{"url": ..., "username": ...}``."""
        try:
            return self._upload(video, caption, cover)
        except UploadError as exc:
            self._save_debug(str(exc))
            raise

    def _upload(self, video: Path, caption: str, cover: Path | None) -> dict:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: PLC0415

        page = self._page
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        if "/accounts/login" in page.url:
            raise UploadError("Instagram demande de se connecter : cookies du compte d'upload expirés.")
        self._dismiss_popups()
        profile_path = self._profile_path()
        tiles_before, posts_before = self._profile_state(profile_path)
        before = set(tiles_before)

        self._compose(video, caption, cover)
        try:
            page.get_by_role("button", name="Share").first.click(timeout=15000)
        except PlaywrightTimeout as exc:
            raise UploadError(f"Bouton « Share » introuvable, rien n'a été publié : {str(exc)[:200]}") from exc

        # Closing the browser while Instagram says "Sharing" cancels the post: stay on the dialog until
        # Instagram confirms, reports a failure, or the delay runs out.
        waited = 0.0
        while waited < self.share_timeout:
            page.wait_for_timeout(DIALOG_POLL_S * 1000)
            waited += DIALOG_POLL_S
            text = self._dialog_text()
            failure = share_failure(text)
            if failure:
                raise UploadError(f"Instagram a refusé la publication : {failure[:300]!r}")
            if share_confirmed(text) or not text:
                break

        # Confirm on the profile (and get the URL of the new Reel).
        for _ in range(CONFIRM_ATTEMPTS):
            tiles_after, posts_after = self._profile_state(profile_path)
            reel = new_reel(before, tiles_after, posts_before, posts_after)
            if reel:
                return {"url": f"https://www.instagram.com{reel}", "username": profile_path.strip("/")}
            page.wait_for_timeout(CONFIRM_POLL_S * 1000)
        raise UploadUncertain(
            f"Le Reel n'est pas apparu sur le profil (avant : {posts_before} posts) : il n'est peut-être pas publié, "
            "ou Instagram le traite encore. Vérifie le profil avant de relancer."
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


def _optional_file(root: Path, name: str) -> Path | None:
    return root / name if name else None


def _upload_problem(root: Path, settings: dict) -> str | None:
    """Why the upload cannot start yet (cookies, cover image, caption file)."""
    problem = cookies_problem(root / settings["cookies_file"])
    if problem:
        return problem
    for key, label in (("cover_file", "miniature"), ("caption_file", "légende")):
        path = _optional_file(root, settings[key])
        if path and not path.is_file():
            return (
                f"{settings[key]} introuvable (fichier de {label}) : crée-le, ou vide `upload.{key}` "
                "dans config.yaml pour ne pas l'utiliser."
            )
    return None


def caption_of(video: Path, config: dict, root: Path) -> str:
    """The text of ``upload.caption_file`` when set (the same for every post), else the usual caption."""
    path = _optional_file(root, config["upload"]["caption_file"])
    text = path.read_text(encoding="utf-8").strip() if path else ""
    return text or caption_for(video, config)


def post_next(
    root: Path,
    config: dict,
    *,
    dry_run: bool = False,
    uploader: BrowserUploader | None = None,
    no_delay: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    delay_rng: random.Random | None = None,
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
            _log(f"Légende : {caption_of(waiting[0], config, root)!r}")
            return 0
        return fetch_next(root, config, dry_run=True, **fetch_kwargs)

    cookies_path = root / settings["cookies_file"]
    # A setup problem must not burn a Reel: check the upload account before downloading anything.
    problem = _upload_problem(root, settings)
    if problem:
        _log(f"Configuration incomplète, aucun Reel téléchargé : {problem}")
        return 1

    low, high = settings["delay_before"]
    if not no_delay and high > 0:
        # A random wait, so that publications do not happen at the same minute every day.
        wait = (delay_rng or random.SystemRandom()).uniform(low, high)
        _log(f"Attente aléatoire de {int(wait // 60)} min {int(wait % 60)} s avant de publier.")
        sleep(wait)

    if waiting:
        _log(f"{len(waiting)} vidéo(s) déjà dans input/ : pas de nouveau téléchargement.")
    else:
        code = fetch_next(root, config, sleep=sleep, **fetch_kwargs)
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

    caption = caption_of(video, config, root)
    cover = _optional_file(root, settings["cover_file"])
    _log(
        f"Vidéo : {video.name} ({video.stat().st_size / 1e6:.1f} Mo) – légende : {caption!r}"
        + (f" – miniature : {cover.name}" if cover else "")
    )
    uploader = uploader or BrowserUploader(
        load_cookies(cookies_path),
        executable_path=settings["browser_path"],
        headless=settings["headless"],
        share_timeout=settings["share_timeout"],
        debug_dir=root / "logs",
    )
    try:
        with uploader:
            result = uploader.upload(video, caption, cover)
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
