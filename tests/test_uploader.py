import json

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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def upload(self, video, caption):
        if self.error:
            raise self.error
        self.uploads.append((video.name, caption))
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
