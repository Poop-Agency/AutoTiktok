"""The video queue (``input/``) and the per-platform publish state (``state/published.json``)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

VIDEO_EXTENSIONS = (".mp4", ".mov")

PUBLISHED = "published"
FAILED = "failed"
PENDING = "pending"
FINAL_STATUSES = (PUBLISHED, FAILED)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def list_videos(input_dir: Path) -> list[Path]:
    """Videos waiting in the queue, in publishing order (alphabetical, case-insensitive)."""
    if not input_dir.is_dir():
        return []
    videos = [p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
    return sorted(videos, key=lambda p: p.name.lower())


def caption_for(video: Path, config: dict) -> str:
    """Caption from ``<video>.txt`` when present, else the default caption. Hashtags are appended."""
    sidecar = video.with_suffix(".txt")
    text = sidecar.read_text(encoding="utf-8").strip() if sidecar.exists() else ""
    if not text:
        text = str(config["caption"].get("default") or "").strip()
    hashtags = str(config["caption"].get("hashtags") or "").strip()
    return f"{text} {hashtags}".strip() if hashtags else text


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {"videos": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.setdefault("videos", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def video(self, name: str) -> dict:
        return self.data["videos"].setdefault(name, {"platforms": {}})

    def platform(self, name: str, platform: str) -> dict:
        return self.video(name)["platforms"].get(platform, {})

    def is_final(self, name: str, platform: str) -> bool:
        return self.platform(name, platform).get("status") in FINAL_STATUSES

    def record_success(self, name: str, platform: str, post_id: str, details: dict) -> None:
        entry = self.video(name)["platforms"].setdefault(platform, {})
        entry.update(
            status=PUBLISHED,
            post_id=post_id,
            at=now_iso(),
            attempts=entry.get("attempts", 0) + 1,
            error=None,
            **({"details": details} if details else {}),
        )
        self.save()

    def record_failure(self, name: str, platform: str, error: str, *, retryable: bool, max_attempts: int) -> str:
        entry = self.video(name)["platforms"].setdefault(platform, {})
        attempts = entry.get("attempts", 0) + 1
        status = PENDING if retryable and attempts < max_attempts else FAILED
        entry.update(status=status, at=now_iso(), attempts=attempts, error=error[:1000])
        self.save()
        return status

    def mark_done(self, name: str, disposition: str) -> None:
        video = self.video(name)
        video["done_at"] = now_iso()
        video["disposition"] = disposition
        self.save()


def finish_video(video: Path, config: dict, root: Path) -> str:
    """Move (or delete) a fully handled video and its caption file. Returns the disposition."""
    companions = [video, video.with_suffix(".txt")]
    if config["queue"]["after_publish"] == "delete":
        for path in companions:
            path.unlink(missing_ok=True)
        return "deleted"
    done_dir = root / config["queue"]["done_dir"]
    done_dir.mkdir(parents=True, exist_ok=True)
    for path in companions:
        if path.exists():
            shutil.move(str(path), str(done_dir / path.name))
    return "moved"
