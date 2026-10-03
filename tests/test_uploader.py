import json
import random

import pytest

from autotiktok import uploader
from autotiktok.fetcher import FETCHED_FILE
from autotiktok.uploader import UploadError, UploadUncertain, cookies_problem, post_next

SESSION = (
    "# Netscape HTTP Cookie File\n"
    ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\tabc\n"
    ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tcsrftoken\ttok\n"
)
REEL_URL = "https://www.instagram.com/triple.t.polyester/reel/NEW1/"


class FakeUploader:
    def __init__(self, error=None):
        self.error = error
        self.uploads = []
        self.covers = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def upload(self, video, caption, cover=None):
        if self.error:
            raise self.error
        self.uploads.append((video.name, caption))
        self.covers.append(cover)
        return {"url": REEL_URL, "username": "triple.t.polyester"}


class FakeYDL:
    def __init__(self, input_dir, downloads):
        self.input_dir = input_dir
        self.downloads = downloads

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        code = urls[0].rstrip("/").rsplit("/", 1)[1]
        self.downloads.append(code)
        (self.input_dir / f"ig_{code}.mp4").write_bytes(b"video-" + code.encode())


@pytest.fixture
def project(tmp_path, config):
    (tmp_path / "cookies_upload.txt").write_text(SESSION, encoding="utf-8")
    (tmp_path / "input").mkdir()
    index = tmp_path / config["fetch"]["index_file"]
    index.parent.mkdir(parents=True, exist_ok=True)
    reel = {
        "url": "https://www.instagram.com/reel/SRC1/",
        "account": "kujo__o",
        "views": 2_000_000,
        "caption": "Trop drôle",
    }
    index.write_text(json.dumps({"updated_at": None, "reels": {"SRC1": reel}}), encoding="utf-8")
    return tmp_path


def run(project, config, uploader_obj, downloads, **kwargs):
    factory = lambda _opts: FakeYDL(project / "input", downloads)  # noqa: E731
    return post_next(project, config, uploader=uploader_obj, ydl_factory=factory, **kwargs)


def archive_entries(project):
    path = project / "state" / "archive.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_cookies_problem(tmp_path):
    path = tmp_path / "cookies_upload.txt"
    assert "introuvable" in cookies_problem(path)
    path.write_text("# Netscape HTTP Cookie File\n# rien encore\n", encoding="utf-8")
    assert "sessionid" in cookies_problem(path)
    path.write_text(SESSION, encoding="utf-8")
    assert cookies_problem(path) is None


def test_post_next_downloads_uploads_and_archives(project, config):
    downloads, fake = [], FakeUploader()
    assert run(project, config, fake, downloads) == 0
    assert downloads == ["SRC1"]
    assert fake.uploads == [("ig_SRC1.mp4", "Trop drôle #funny #humour #fyp")]
    assert not list((project / "input").glob("*.mp4"))  # deleted once published (after_publish: delete)
    (entry,) = archive_entries(project)
    assert entry["source_url"] == "https://www.instagram.com/reel/SRC1/"
    assert entry["source_account"] == "kujo__o"
    assert entry["platforms"]["instagram"]["post_id"] == REEL_URL
    assert json.loads((project / FETCHED_FILE).read_text(encoding="utf-8"))["id"] == "SRC1"


def test_post_next_never_downloads_when_upload_account_is_not_ready(project, config):
    (project / "cookies_upload.txt").write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    downloads = []
    assert run(project, config, FakeUploader(), downloads) == 1
    assert downloads == []
    assert not (project / FETCHED_FILE).exists()


def test_post_next_keeps_the_video_when_upload_fails(project, config):
    downloads = []
    assert run(project, config, FakeUploader(error=UploadError("interface inattendue")), downloads) == 1
    assert (project / "input" / "ig_SRC1.mp4").exists()
    assert archive_entries(project) == []
    # Next run retries the video that is waiting, without downloading another one.
    fake = FakeUploader()
    assert run(project, config, fake, downloads) == 0
    assert downloads == ["SRC1"]
    assert [name for name, _ in fake.uploads] == ["ig_SRC1.mp4"]


def test_post_next_uncertain_share_keeps_the_video(project, config):
    assert run(project, config, FakeUploader(error=UploadUncertain("peut-être publié")), []) == 1
    assert (project / "input" / "ig_SRC1.mp4").exists()
    assert archive_entries(project) == []


def test_post_next_skips_a_file_already_in_the_archive(project, config):
    run(project, config, FakeUploader(), [])
    (project / "input" / "again.mp4").write_bytes(b"video-SRC1")  # same content as the published one
    fake = FakeUploader()
    assert run(project, config, fake, []) == 0
    assert fake.uploads == []
    assert not (project / "input" / "again.mp4").exists()


def test_post_next_dry_run_touches_nothing(project, config):
    downloads, fake = [], FakeUploader()
    assert run(project, config, fake, downloads, dry_run=True) == 0
    assert downloads == [] and fake.uploads == []
    assert not (project / FETCHED_FILE).exists()


def test_post_next_stops_when_the_index_is_empty(project, config):
    (project / config["fetch"]["index_file"]).unlink()
    assert run(project, config, FakeUploader(), []) == 1
    assert uploader  # module imported


def test_post_next_uses_the_caption_file_for_every_post(project, config):
    (project / "legende.txt").write_text("Le meilleur de l'internet 🔥\n#bestof\n", encoding="utf-8")
    config["upload"]["caption_file"] = "legende.txt"
    fake = FakeUploader()
    assert run(project, config, fake, []) == 0
    # The file replaces the original caption and the config hashtags, line breaks included.
    assert fake.uploads == [("ig_SRC1.mp4", "Le meilleur de l'internet 🔥\n#bestof")]


def test_post_next_sends_the_cover_image(project, config):
    (project / "cover.jpg").write_bytes(b"jpg")
    config["upload"]["cover_file"] = "cover.jpg"
    fake = FakeUploader()
    assert run(project, config, fake, []) == 0
    assert fake.covers == [project / "cover.jpg"]


def test_post_next_without_cover_setting_sends_none(project, config):
    fake = FakeUploader()
    assert run(project, config, fake, []) == 0
    assert fake.covers == [None]


@pytest.mark.parametrize("key", ["cover_file", "caption_file"])
def test_post_next_stops_before_downloading_when_a_configured_file_is_missing(project, config, key):
    config["upload"][key] = "absent.txt"
    downloads = []
    assert run(project, config, FakeUploader(), downloads) == 1
    assert downloads == []
    assert not (project / FETCHED_FILE).exists()


def test_post_next_empty_caption_file_falls_back_to_the_usual_caption(project, config):
    (project / "legende.txt").write_text("  \n", encoding="utf-8")
    config["upload"]["caption_file"] = "legende.txt"
    fake = FakeUploader()
    assert run(project, config, fake, []) == 0
    assert fake.uploads == [("ig_SRC1.mp4", "Trop drôle #funny #humour #fyp")]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Note... | triple.t.polyester | 31 posts | 2 followers | 4 following", 31),
        ("name | 1 post | 0 followers", 1),
        ("name | 1,234 posts | 9 followers", 1234),
        ("name | no counter here", None),
    ],
)
def test_parse_post_count(header, expected):
    assert uploader.parse_post_count(header) == expected


def test_new_reel_needs_a_new_tile_and_a_bigger_post_count():
    before = {"/a/reel/OLD1/", "/a/reel/OLD2/"}
    after = ["/a/reel/NEW1/", "/a/reel/OLD1/", "/a/reel/OLD2/"]
    assert uploader.new_reel(before, after, 31, 32) == "/a/reel/NEW1/"
    assert uploader.new_reel(before, after, None, None) == "/a/reel/NEW1/"  # counter unreadable: tiles decide
    assert uploader.new_reel(before, after, 31, 31) is None  # the count did not grow: nothing was published
    assert uploader.new_reel(before, after[1:], 31, 32) is None  # no new tile


def test_new_reel_ignores_older_reels_that_were_just_not_loaded_before():
    first_load = [f"/a/reel/R{i}/" for i in range(12)]
    before = set(first_load)
    scrolled_further = [*first_load, *[f"/a/reel/OLDER{i}/" for i in range(20)]]
    assert uploader.new_reel(before, scrolled_further, 31, 32) is None  # only older Reels appeared: not a new post
    published = ["/a/reel/FRESH/", *scrolled_further]
    assert uploader.new_reel(before, published, 31, 32) == "/a/reel/FRESH/"  # the newest tile, not the first by name


@pytest.mark.parametrize(
    ("text", "failed"),
    [
        ("Sharing", False),
        ("Reel shared | Your reel has been shared.", False),
        ("Your post couldn't be shared. Try again.", True),
        ("Something went wrong", True),
        ("", False),
    ],
)
def test_share_failure(text, failed):
    assert (uploader.share_failure(text) is not None) is failed


def test_share_confirmed():
    assert uploader.share_confirmed("Your reel has been shared.")
    assert not uploader.share_confirmed("Sharing")
    assert not uploader.share_confirmed("Your post couldn't be shared")  # failure text is checked first anyway


class Waits:
    def __init__(self):
        self.seconds = []

    def __call__(self, seconds):
        self.seconds.append(seconds)


def test_post_next_waits_a_random_time_before_publishing(project, config):
    config["upload"]["delay_before"] = [60, 1200]
    waits, fake = Waits(), FakeUploader()
    assert run(project, config, fake, [], sleep=waits, delay_rng=random.Random(1)) == 0
    assert len(waits.seconds) == 1 and 60 <= waits.seconds[0] <= 1200
    assert len(fake.uploads) == 1


def test_post_next_no_delay_skips_the_wait(project, config):
    config["upload"]["delay_before"] = [60, 1200]
    waits = Waits()
    assert run(project, config, FakeUploader(), [], sleep=waits, no_delay=True) == 0
    assert waits.seconds == []


def test_post_next_dry_run_never_waits(project, config):
    config["upload"]["delay_before"] = [60, 1200]
    waits = Waits()
    assert run(project, config, FakeUploader(), [], sleep=waits, dry_run=True) == 0
    assert waits.seconds == []


def test_post_next_delay_is_off_by_default(project, config):
    waits = Waits()
    assert run(project, config, FakeUploader(), [], sleep=waits) == 0
    assert waits.seconds == []


def test_post_next_does_not_wait_when_the_upload_account_is_not_ready(project, config):
    (project / "cookies_upload.txt").write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    config["upload"]["delay_before"] = [60, 1200]
    waits = Waits()
    assert run(project, config, FakeUploader(), [], sleep=waits) == 1
    assert waits.seconds == []
