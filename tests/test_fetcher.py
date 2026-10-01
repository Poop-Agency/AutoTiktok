import json
import random

import pytest
from yt_dlp.utils import DownloadError

from autotiktok import fetcher
from autotiktok.fetcher import (
    FETCHED_FILE,
    FetchError,
    RateLimited,
    load_fetched,
    load_index,
    parse_reels,
    read_pool,
    record_fetched,
    refresh_index,
    username_of,
)

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\tabc\n"
    ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tcsrftoken\ttok\n"
)


def reel(code, views, caption="Trop drôle"):
    return {"id": code, "url": f"https://www.instagram.com/reel/{code}/", "views": views, "caption": caption}


class FakeLister:
    """Stands in for BrowserLister: ``pages[username]`` is a list of pages, or an exception to raise."""

    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def reels(self, username):
        result = self.pages[username]
        if isinstance(result, Exception):
            raise result
        yield from result


def graphql(edges, has_next=None):
    connection = {"edges": [{"node": {"media": media}} for media in edges]}
    if has_next is not None:
        connection["page_info"] = {"has_next_page": has_next, "end_cursor": "x"}
    return {"data": {"fetch__XDTUserDict": {"clips_connection": connection}}}


class FakeYDL:
    def __init__(self, input_dir, downloads, failing=()):
        self.input_dir = input_dir
        self.downloads = downloads
        self.failing = failing

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        code = urls[0].rstrip("/").rsplit("/", 1)[1]
        if code in self.failing:
            raise DownloadError("Video unavailable")
        self.downloads.append(urls[0])
        (self.input_dir / f"ig_{code}.mp4").write_bytes(b"x")


def ydl_factory(input_dir, downloads, failing=()):
    return lambda _opts: FakeYDL(input_dir, downloads, failing)


def entry(reel_id, views=2_000_000, account="a", **extra):
    return {
        "url": f"https://www.instagram.com/reel/{reel_id}/",
        "account": account,
        "views": views,
        "caption": "Trop drôle",
        **extra,
    }


@pytest.fixture
def project(tmp_path):
    (tmp_path / "account_pools.txt").write_text("https://www.instagram.com/a/\n", encoding="utf-8")
    (tmp_path / "cookies_browse.txt").write_text(NETSCAPE, encoding="utf-8")
    (tmp_path / "input").mkdir()
    return tmp_path


def write_index(project, config, reels):
    path = project / config["fetch"]["index_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated_at": None, "reels": reels}), encoding="utf-8")


def test_read_pool_skips_blanks_and_comments(tmp_path):
    pool = tmp_path / "pool.txt"
    pool.write_text("https://www.instagram.com/a/\n\n# off\n  https://www.instagram.com/b  \n", encoding="utf-8")
    assert read_pool(pool) == ["https://www.instagram.com/a/", "https://www.instagram.com/b"]
    assert read_pool(tmp_path / "missing.txt") == []


def test_username_of_handles_missing_trailing_slash():
    assert username_of("https://www.instagram.com/qweezyreacts") == "qweezyreacts"
    assert username_of("https://www.instagram.com/kujo__o/") == "kujo__o"


def test_fetched_ledger_round_trip(tmp_path):
    path = tmp_path / "state" / "fetched.jsonl"
    assert load_fetched(path) == set()
    record_fetched(path, {"id": "A1"})
    record_fetched(path, {"id": "B2"})
    assert load_fetched(path) == {"A1", "B2"}


def test_parse_reels_reads_plays_and_pagination():
    payload = graphql(
        [
            {"code": "AAA", "play_count": 1_500_000, "view_count": 3, "caption": {"text": " Trop drôle "}},
            {"code": "BBB", "play_count": None, "view_count": 10, "caption": None},
            {"code": None, "play_count": 5},
        ],
        has_next=True,
    )
    reels, has_next = parse_reels(payload)
    assert reels == [reel("AAA", 1_500_000), reel("BBB", 10, caption="")]
    assert has_next is True
    assert parse_reels(graphql([], has_next=False)) == ([], False)


def test_parse_reels_ignores_unrelated_answers():
    assert parse_reels({"data": {"viewer": {"id": "1"}}}) == ([], None)
    assert parse_reels({}) == ([], None)


def test_refresh_index_keeps_only_popular_and_merges(project, config):
    write_index(project, config, {"old": entry("old", views=1_200_000, account="b")})
    lister = FakeLister({"a": [[reel("low", 999_999), reel("ok", 1_000_000)], [reel("big", 9_000_000)]]})
    assert refresh_index(project, config, lister=lister) == 0
    reels = load_index(project / config["fetch"]["index_file"])["reels"]
    assert set(reels) == {"old", "ok", "big"}  # earlier entries are kept, "low" is below the threshold
    assert reels["ok"]["account"] == "a"
    assert reels["big"]["views"] == 9_000_000
    # A second run updates the views instead of duplicating.
    refresh_index(project, config, lister=FakeLister({"a": [[reel("ok", 1_100_000, caption="")]]}))
    reels = load_index(project / config["fetch"]["index_file"])["reels"]
    assert len(reels) == 3
    assert reels["ok"]["views"] == 1_100_000
    assert reels["ok"]["caption"] == "Trop drôle"  # an empty caption never erases a known one


def test_refresh_index_saves_progress_and_stops_on_rate_limit(project, config):
    (project / "account_pools.txt").write_text(
        "https://www.instagram.com/a/\nhttps://www.instagram.com/b/\nhttps://www.instagram.com/c/\n",
        encoding="utf-8",
    )
    lister = FakeLister({"a": [[reel("ok", 2_000_000)]], "b": RateLimited("429"), "c": [[reel("never", 5_000_000)]]})
    assert refresh_index(project, config, lister=lister) == 1
    assert set(load_index(project / config["fetch"]["index_file"])["reels"]) == {"ok"}


def test_refresh_index_skips_failing_account(project, config):
    (project / "account_pools.txt").write_text(
        "https://www.instagram.com/bad/\nhttps://www.instagram.com/good/\n", encoding="utf-8"
    )
    lister = FakeLister({"bad": FetchError("Compte introuvable"), "good": [[reel("ok", 2_000_000)]]})
    assert refresh_index(project, config, lister=lister) == 1
    assert set(load_index(project / config["fetch"]["index_file"])["reels"]) == {"ok"}


def test_refresh_index_needs_pool_and_cookies(tmp_path, config):
    assert refresh_index(tmp_path, config) == 1  # no pool
    (tmp_path / "account_pools.txt").write_text("https://www.instagram.com/a/\n", encoding="utf-8")
    assert refresh_index(tmp_path, config) == 1  # no cookies


def test_fetch_next_downloads_and_records(project, config):
    write_index(project, config, {"ok": entry("ok")})
    downloads = []
    factory = ydl_factory(project / "input", downloads)
    assert fetcher.fetch_next(project, config, ydl_factory=factory) == 0
    assert downloads == ["https://www.instagram.com/reel/ok/"]
    assert (project / "input" / "ig_ok.mp4").exists()
    assert (project / "input" / "ig_ok.txt").read_text(encoding="utf-8") == "Trop drôle\n"
    assert load_fetched(project / FETCHED_FILE) == {"ok"}
    # Second run: the only Reel of the index was already taken.
    assert fetcher.fetch_next(project, config, ydl_factory=factory) == 1
    assert len(downloads) == 1


def test_fetch_next_marks_unavailable_reels_gone(project, config):
    write_index(project, config, {"dead": entry("dead"), "ok": entry("ok")})
    downloads = []
    factory = ydl_factory(project / "input", downloads, failing=("dead",))
    assert fetcher.fetch_next(project, config, ydl_factory=factory, rng=random.Random(0)) == 0
    assert downloads == ["https://www.instagram.com/reel/ok/"]
    reels = load_index(project / config["fetch"]["index_file"])["reels"]
    assert reels["ok"].get("gone") is None
    assert load_fetched(project / FETCHED_FILE) == {"ok"}
    if reels["dead"].get("gone"):  # drawn first with this seed or not, a dead Reel is never drawn again
        write_index(project, config, {"dead": reels["dead"]})
        assert fetcher.fetch_next(project, config, ydl_factory=factory) == 1


def test_fetch_next_dry_run_downloads_nothing(project, config):
    write_index(project, config, {"ok": entry("ok")})
    downloads = []
    assert fetcher.fetch_next(project, config, dry_run=True, ydl_factory=ydl_factory(project / "input", downloads)) == 0
    assert downloads == []
    assert not list((project / "input").iterdir())
    assert not (project / FETCHED_FILE).exists()


def test_fetch_next_empty_index(project, config):
    assert fetcher.fetch_next(project, config) == 1


@pytest.fixture
def publishing(monkeypatch):
    """post_next with the account setup check and the real publication stubbed out."""
    calls = {"publish": 0, "problems": []}
    monkeypatch.setattr(fetcher, "TokenStore", lambda *a, **k: None)
    monkeypatch.setattr(fetcher, "key_from_env", lambda: b"")
    monkeypatch.setattr(fetcher, "missing_setup", lambda *a, **k: calls["problems"])

    def fake_publish(root, config, **kwargs):
        calls["publish"] += 1
        return 0

    monkeypatch.setattr(fetcher, "publish_next", fake_publish)
    return calls


def test_post_next_downloads_then_publishes(project, config, publishing):
    write_index(project, config, {"ok": entry("ok")})
    downloads = []
    factory = ydl_factory(project / "input", downloads)
    assert fetcher.post_next(project, config, ydl_factory=factory) == 0
    assert downloads == ["https://www.instagram.com/reel/ok/"]
    assert publishing["publish"] == 1


def test_post_next_does_not_download_when_setup_incomplete(project, config, publishing):
    write_index(project, config, {"ok": entry("ok")})
    publishing["problems"] = ["instagram : compte non connecté"]
    downloads = []
    assert fetcher.post_next(project, config, ydl_factory=ydl_factory(project / "input", downloads)) == 1
    assert downloads == []
    assert not (project / FETCHED_FILE).exists()
    assert publishing["publish"] == 0


def test_post_next_retries_the_video_already_waiting(project, config, publishing):
    write_index(project, config, {"ok": entry("ok")})
    (project / "input" / "leftover.mp4").write_bytes(b"x")
    downloads = []
    assert fetcher.post_next(project, config, ydl_factory=ydl_factory(project / "input", downloads)) == 0
    assert downloads == []
    assert publishing["publish"] == 1


def test_post_next_stops_when_nothing_to_download(project, config, publishing):
    assert fetcher.post_next(project, config) == 1
    assert publishing["publish"] == 0


def test_post_next_dry_run_touches_nothing(project, config, publishing):
    write_index(project, config, {"ok": entry("ok")})
    downloads = []
    assert fetcher.post_next(project, config, dry_run=True, ydl_factory=ydl_factory(project / "input", downloads)) == 0
    assert downloads == []
    assert publishing["publish"] == 0


def test_rate_limited_is_a_fetch_error():
    assert issubclass(RateLimited, FetchError)
