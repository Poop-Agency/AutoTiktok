import json
import random

import pytest
from yt_dlp.utils import DownloadError

from autotiktok import fetcher
from autotiktok.fetcher import (
    FETCHED_FILE,
    FetchError,
    PoolAccount,
    RateLimited,
    effective_min_views,
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
    write_pool(tmp_path, "a")
    (tmp_path / "cookies_browse.txt").write_text(NETSCAPE, encoding="utf-8")
    (tmp_path / "input").mkdir()
    return tmp_path


def write_pool(root, *accounts):
    """``accounts``: a name, or a ``(name, min_views)`` pair, written as account_pools.json."""
    entries = []
    for account in accounts:
        name, min_views = account if isinstance(account, tuple) else (account, None)
        entries.append({"account": f"https://www.instagram.com/{name}/", "min_views": min_views})
    (root / "account_pools.json").write_text(json.dumps(entries), encoding="utf-8")


def write_index(project, config, reels):
    path = project / config["fetch"]["index_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated_at": None, "reels": reels}), encoding="utf-8")


def test_read_pool_json_with_per_account_thresholds(tmp_path):
    pool = tmp_path / "pool.json"
    pool.write_text(
        json.dumps(
            [
                {"account": "https://www.instagram.com/a/", "min_views": 250_000},
                {"account": "https://www.instagram.com/b", "min_views": None},
                {"account": "@c"},
                "d",
            ]
        ),
        encoding="utf-8",
    )
    assert read_pool(pool) == [PoolAccount("a", 250_000), PoolAccount("b"), PoolAccount("c"), PoolAccount("d")]
    assert read_pool(tmp_path / "missing.json") == []


def test_read_pool_legacy_text_file_skips_blanks_and_comments(tmp_path):
    pool = tmp_path / "pool.txt"
    pool.write_text("https://www.instagram.com/a/\n\n# off\n  https://www.instagram.com/b  \n", encoding="utf-8")
    assert read_pool(pool) == [PoolAccount("a"), PoolAccount("b")]


@pytest.mark.parametrize(
    "content",
    [
        "pas du json",
        '{"account": "a"}',
        '[{"min_views": 5}]',
        '[{"account": "a", "min_views": "beaucoup"}]',
        '[{"account": "a", "min_views": -1}]',
        '[{"account": "a", "min_views": true}]',
    ],
)
def test_read_pool_rejects_a_malformed_json_file(tmp_path, content):
    pool = tmp_path / "pool.json"
    pool.write_text(content, encoding="utf-8")
    with pytest.raises(FetchError):
        read_pool(pool)


def test_effective_min_views_priority():
    own, bare = PoolAccount("a", 500), PoolAccount("b")
    assert effective_min_views(own, 100, 1_000_000) == 500  # the account's own value always wins
    assert effective_min_views(own, None, 1_000_000) == 500
    assert effective_min_views(bare, 100, 1_000_000) == 100  # then the command's --min-views
    assert effective_min_views(bare, None, 1_000_000) == 1_000_000  # then config.yaml
    assert effective_min_views(PoolAccount("c", 0), 100, 1_000_000) == 0  # 0 is a real value, not "empty"


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
    write_pool(project, "a", "b", "c")
    lister = FakeLister({"a": [[reel("ok", 2_000_000)]], "b": RateLimited("429"), "c": [[reel("never", 5_000_000)]]})
    assert refresh_index(project, config, lister=lister) == 1
    assert set(load_index(project / config["fetch"]["index_file"])["reels"]) == {"ok"}


def test_refresh_index_skips_failing_account(project, config):
    write_pool(project, "bad", "good")
    lister = FakeLister({"bad": FetchError("Compte introuvable"), "good": [[reel("ok", 2_000_000)]]})
    assert refresh_index(project, config, lister=lister) == 1
    assert set(load_index(project / config["fetch"]["index_file"])["reels"]) == {"ok"}


def test_refresh_index_needs_pool_and_cookies(tmp_path, config):
    assert refresh_index(tmp_path, config) == 1  # no pool
    write_pool(tmp_path, "a")
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


def test_rate_limited_is_a_fetch_error():
    assert issubclass(RateLimited, FetchError)


def pool_of(project, *names):
    write_pool(project, *names)


class RecordingLister(FakeLister):
    def __init__(self, pages):
        super().__init__(pages)
        self.asked = []

    def reels(self, username):
        self.asked.append(username)
        return super().reels(username)


def test_refresh_index_new_only_lists_unknown_accounts(project, config):
    pool_of(project, "a", "b", "c")
    write_index(project, config, {"x": entry("x", account="a")})
    lister = RecordingLister({"b": [[reel("b1", 2_000_000)]], "c": [[reel("c1", 10)]]})
    assert refresh_index(project, config, only_new=True, lister=lister) == 0
    assert lister.asked == ["b", "c"]
    # "c" had nothing above the threshold but is remembered, so it is not listed again.
    again = RecordingLister({})
    assert refresh_index(project, config, only_new=True, lister=again) == 0
    assert again.asked == []


def test_refresh_index_single_account(project, config):
    pool_of(project, "a", "b")
    lister = RecordingLister({"b": [[reel("b1", 2_000_000)]]})
    assert refresh_index(project, config, account="https://www.instagram.com/b/", lister=lister) == 0
    assert lister.asked == ["b"]
    lister = RecordingLister({"a": [[reel("a1", 2_000_000)]]})
    assert refresh_index(project, config, account="@A", lister=lister) == 0
    assert lister.asked == ["a"]


def test_refresh_index_account_must_be_in_pool(project, config):
    pool_of(project, "a")
    lister = RecordingLister({})
    assert refresh_index(project, config, account="stranger", lister=lister) == 1
    assert lister.asked == []


def test_refresh_index_min_views_override(project, config):
    pool_of(project, "a")
    lister = RecordingLister({"a": [[reel("small", 150_000), reel("tiny", 99_999)]]})
    assert refresh_index(project, config, min_views=100_000, lister=lister) == 0
    index = load_index(project / config["fetch"]["index_file"])
    assert set(index["reels"]) == {"small"}
    assert index["accounts"]["a"]["min_views"] == 100_000


def test_refresh_index_threshold_priority_per_account(project, config):
    write_pool(project, ("own", 150_000), "plain")
    lister = RecordingLister(
        {
            "own": [[reel("o1", 2_000_000), reel("o2", 200_000), reel("o3", 120_000)]],
            "plain": [[reel("p1", 2_000_000), reel("p2", 200_000), reel("p3", 120_000), reel("p4", 50_000)]],
        }
    )
    # --min-views 100000 applies to "plain" only: "own" keeps its own 150000.
    assert refresh_index(project, config, min_views=100_000, lister=lister) == 0
    index = load_index(project / config["fetch"]["index_file"])
    assert set(index["reels"]) == {"o1", "o2", "p1", "p2", "p3"}
    assert index["accounts"]["own"]["min_views"] == 150_000
    assert index["accounts"]["plain"]["min_views"] == 100_000


def test_refresh_index_falls_back_to_config_threshold(project, config):
    write_pool(project, "plain")
    lister = RecordingLister({"plain": [[reel("big", 1_000_000), reel("small", 999_999)]]})
    assert refresh_index(project, config, lister=lister) == 0
    index = load_index(project / config["fetch"]["index_file"])
    assert set(index["reels"]) == {"big"}
    assert index["accounts"]["plain"]["min_views"] == 1_000_000


def test_refresh_index_stops_on_a_malformed_pool(project, config):
    (project / "account_pools.json").write_text("pas du json", encoding="utf-8")
    lister = RecordingLister({})
    assert refresh_index(project, config, lister=lister) == 1
    assert lister.asked == []
