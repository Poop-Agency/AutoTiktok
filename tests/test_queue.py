import random

from autotiktok.queue import (
    FAILED,
    PENDING,
    PUBLISHED,
    Archive,
    State,
    caption_for,
    finish_video,
    list_videos,
    pick_video,
)


def test_list_videos_sorted_and_filtered(tmp_path):
    for name in ["b.mp4", "A.MOV", "c.txt", "a2.mp4"]:
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in list_videos(tmp_path)] == ["A.MOV", "a2.mp4", "b.mp4"]
    assert list_videos(tmp_path / "missing") == []


def test_caption_from_sidecar_or_default(tmp_path, config):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    assert caption_for(video, config) == "😂 #funny #humour #fyp"
    video.with_suffix(".txt").write_text("Mon chat 🐱\n", encoding="utf-8")
    assert caption_for(video, config) == "Mon chat 🐱 #funny #humour #fyp"
    config["caption"]["hashtags"] = ""
    assert caption_for(video, config) == "Mon chat 🐱"


def test_state_failure_then_give_up(tmp_path):
    state = State(tmp_path / "s.json")
    assert state.record_failure("v.mp4", "tiktok", "boom", retryable=True, max_attempts=2) == PENDING
    assert not state.is_final("v.mp4", "tiktok")
    assert state.record_failure("v.mp4", "tiktok", "boom", retryable=True, max_attempts=2) == FAILED
    assert state.is_final("v.mp4", "tiktok")
    assert state.record_failure("w.mp4", "tiktok", "bad", retryable=False, max_attempts=5) == FAILED


def test_state_persists_success(tmp_path):
    state = State(tmp_path / "s.json")
    state.record_success("v.mp4", "youtube", "abc", {"url": "u"})
    reloaded = State(tmp_path / "s.json")
    entry = reloaded.platform("v.mp4", "youtube")
    assert entry["status"] == PUBLISHED and entry["post_id"] == "abc" and entry["details"] == {"url": "u"}


def test_finish_video_move_and_delete(tmp_path, config):
    (tmp_path / "input").mkdir()
    video = tmp_path / "input" / "v.mp4"
    video.write_bytes(b"x")
    video.with_suffix(".txt").write_text("hi")
    assert finish_video(video, config, tmp_path) == "moved"
    assert (tmp_path / "done" / "v.mp4").exists() and (tmp_path / "done" / "v.txt").exists()
    assert not video.exists()

    video.write_bytes(b"x")
    config["queue"]["after_publish"] = "delete"
    assert finish_video(video, config, tmp_path) == "deleted"
    assert not video.exists()


def test_pick_video(tmp_path):
    videos = [tmp_path / n for n in ("a.mp4", "b.mp4", "c.mp4")]
    state = State(tmp_path / "s.json")
    assert pick_video([], state, "random") is None
    assert pick_video(videos, state, "alphabetical").name == "a.mp4"
    picks = {pick_video(videos, state, "random", random.Random(seed)).name for seed in range(30)}
    assert picks == {"a.mp4", "b.mp4", "c.mp4"}
    state.start("c.mp4", "h")
    assert pick_video(videos, state, "random").name == "c.mp4"


def test_state_resets_when_name_reused_by_other_file(tmp_path):
    state = State(tmp_path / "s.json")
    state.start("v.mp4", "h1")
    state.record_success("v.mp4", "tiktok", "id", {})
    state.start("v.mp4", "h2")
    assert not state.is_final("v.mp4", "tiktok")


def test_archive_roundtrip(tmp_path):
    archive = Archive(tmp_path / "a.jsonl")
    archive.add({"name": "v.mp4", "sha256": "abc"})
    assert Archive(tmp_path / "a.jsonl").contains("abc")
    assert not archive.contains("def")
