import json

import pytest

from autotiktok import publisher as publisher_mod
from autotiktok.platforms.base import PermanentError, PublishResult, RetryableError
from autotiktok.publisher import ARCHIVE_FILE, STATE_FILE, TOKENS_FILE, publish_next
from autotiktok.tokens import TokenStore


class FakePublisher:
    """Behaviour per platform is driven by the class-level ``plan`` dict."""

    plan: dict = {}
    calls: list = []

    def __init__(self, config, token, client, env=None, sleep=None):
        self.token = dict(token)
        self.platform = token["platform"]

    def refresh_if_needed(self):
        if self.plan.get(f"{self.platform}:refresh"):
            self.token["access_token"] = "refreshed"
            return True
        return False

    def publish(self, video):
        FakePublisher.calls.append((self.platform, video.path.name, video.caption))
        outcome = self.plan.get(self.platform, "ok")
        if outcome == "retry":
            raise RetryableError("temporary")
        if outcome == "fail":
            raise PermanentError("rejected")
        if outcome == "crash":
            raise ValueError("unexpected")
        return PublishResult(post_id=f"{self.platform}-id", details={})


@pytest.fixture
def project(tmp_path, key, monkeypatch):
    for name in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setattr(publisher_mod, "PUBLISHERS", {p: FakePublisher for p in ("instagram", "tiktok", "youtube")})
    FakePublisher.plan = {}
    FakePublisher.calls = []
    (tmp_path / "input").mkdir()
    for name in ("002.mp4", "001.mp4"):
        (tmp_path / "input" / name).write_bytes(b"x" * 10)
    (tmp_path / "input" / "001.txt").write_text("premier")
    store = TokenStore(tmp_path / TOKENS_FILE, key)
    for platform in ("instagram", "tiktok", "youtube"):
        store.set(platform, {"platform": platform, "access_token": "at"})
    store.save()
    return tmp_path


def state(root):
    return json.loads((root / STATE_FILE).read_text())["videos"]


def archive(root):
    lines = (root / ARCHIVE_FILE).read_text().splitlines()
    return {e["name"]: e for e in map(json.loads, lines)}


def test_publishes_oldest_video_everywhere_and_moves_it(project, config):
    assert publish_next(project, config, client=object()) == 0
    assert sorted(c[0] for c in FakePublisher.calls) == ["instagram", "tiktok", "youtube"]
    assert {c[1] for c in FakePublisher.calls} == {"001.mp4"}
    assert FakePublisher.calls[0][2] == "premier #funny #humour #fyp"
    assert (project / "done" / "001.mp4").exists() and (project / "done" / "001.txt").exists()
    assert (project / "input" / "002.mp4").exists()
    assert archive(project)["001.mp4"]["disposition"] == "moved"
    assert state(project) == {}


def test_partial_failure_retries_only_failed_platform(project, config):
    FakePublisher.plan = {"youtube": "retry"}
    assert publish_next(project, config, client=object()) == 1
    assert (project / "input" / "001.mp4").exists()
    assert state(project)["001.mp4"]["platforms"]["youtube"]["status"] == "pending"

    FakePublisher.plan = {}
    FakePublisher.calls = []
    assert publish_next(project, config, client=object()) == 0
    assert [c[0] for c in FakePublisher.calls] == ["youtube"]  # no duplicate on the others
    assert (project / "done" / "001.mp4").exists()


def test_gives_up_after_max_attempts(project, config):
    FakePublisher.plan = {"tiktok": "crash"}
    config["queue"]["max_attempts"] = 2
    publish_next(project, config, client=object())
    assert (project / "input" / "001.mp4").exists()
    publish_next(project, config, client=object())
    assert not (project / "input" / "001.mp4").exists()
    assert archive(project)["001.mp4"]["platforms"]["tiktok"]["status"] == "failed"


def test_permanent_failure_does_not_block_queue(project, config):
    FakePublisher.plan = {"instagram": "fail"}
    assert publish_next(project, config, client=object()) == 1
    assert (project / "done" / "001.mp4").exists()


def test_disabled_platform_is_skipped(project, config):
    config["platforms"]["tiktok"] = False
    assert publish_next(project, config, client=object()) == 0
    assert "tiktok" not in archive(project)["001.mp4"]["platforms"]
    assert (project / "done" / "001.mp4").exists()


def test_missing_setup_stops_without_consuming_videos(project, config, key, monkeypatch, capsys):
    store = TokenStore(project / TOKENS_FILE, key)
    store._data.pop("youtube")
    store.dirty = True
    store.save()
    monkeypatch.delenv("TIKTOK_CLIENT_SECRET")
    assert publish_next(project, config, client=object()) == 1
    out = capsys.readouterr().out
    assert "youtube : compte non connecté" in out and "TIKTOK_CLIENT_SECRET" in out
    assert FakePublisher.calls == []
    assert (project / "input" / "001.mp4").exists()
    assert not (project / STATE_FILE).exists()


def test_refreshed_tokens_are_saved_even_with_empty_queue(project, config, key):
    for path in (project / "input").iterdir():
        path.unlink()
    FakePublisher.plan = {"instagram:refresh": True}
    assert publish_next(project, config, client=object()) == 0
    assert TokenStore(project / TOKENS_FILE, key).get("instagram")["access_token"] == "refreshed"


def test_dry_run_touches_nothing(project, config, monkeypatch, capsys):
    monkeypatch.delenv("TOKENS_KEY")
    assert publish_next(project, config, dry_run=True) == 0
    assert "001.mp4" in capsys.readouterr().out
    assert FakePublisher.calls == []
    assert not (project / STATE_FILE).exists()


def test_random_order_finishes_started_video_first(project, config):
    config["queue"]["order"] = "random"
    FakePublisher.plan = {"youtube": "retry"}
    publish_next(project, config, client=object())
    started = next(iter(state(project)))
    FakePublisher.plan = {}
    FakePublisher.calls = []
    publish_next(project, config, client=object())
    assert {c[1] for c in FakePublisher.calls} == {started}


def test_same_file_is_never_published_twice(project, config):
    config["queue"]["after_publish"] = "delete"
    (project / "input" / "002.mp4").write_bytes(b"y" * 10)
    publish_next(project, config, client=object())
    publish_next(project, config, client=object())
    assert len(archive(project)) == 2
    # Same bytes put back under a new name: skipped, not published.
    (project / "input" / "copy.mp4").write_bytes(b"x" * 10)
    FakePublisher.calls = []
    assert publish_next(project, config, client=object()) == 0
    assert FakePublisher.calls == []
    assert not (project / "input" / "copy.mp4").exists()
