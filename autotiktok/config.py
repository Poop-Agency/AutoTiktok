"""Loads ``config.yaml`` and fills in defaults."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

PLATFORMS = ("instagram", "tiktok", "youtube")

DEFAULTS: dict = {
    "platforms": {"instagram": True, "tiktok": True, "youtube": True},
    "caption": {"default": "😂", "hashtags": "#funny #humour #fyp"},
    "instagram": {"share_to_feed": True},
    "tiktok": {
        "mode": "draft",
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "disable_comment": False,
        "disable_duet": False,
        "disable_stitch": False,
        "is_aigc": False,
    },
    "youtube": {
        "title": "{caption}",
        "privacy": "public",
        "category_id": "23",
        "made_for_kids": False,
        "tags": ["funny", "humour", "shorts"],
    },
    "queue": {
        "input_dir": "input",
        "done_dir": "done",
        "order": "alphabetical",
        "after_publish": "move",
        "max_attempts": 3,
    },
    "fetch": {
        "pool_file": "account_pools.txt",
        "cookies_file": "cookies_browse.txt",
        "index_file": "state/index.json",
        "min_views": 1_000_000,
        "max_reels_per_account": 0,
        "request_delay": [3, 6],
        "user_agent": "",
    },
}


class ConfigError(Exception):
    pass


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path) -> dict:
    data = {}
    if Path(path).exists():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} doit contenir un dictionnaire YAML.")
    config = _merge(DEFAULTS, data)

    if config["tiktok"]["mode"] not in ("draft", "direct"):
        raise ConfigError("tiktok.mode doit valoir 'draft' ou 'direct'.")
    if config["youtube"]["privacy"] not in ("public", "unlisted", "private"):
        raise ConfigError("youtube.privacy doit valoir 'public', 'unlisted' ou 'private'.")
    if config["queue"]["after_publish"] not in ("move", "delete"):
        raise ConfigError("queue.after_publish doit valoir 'move' ou 'delete'.")
    if config["queue"]["order"] not in ("alphabetical", "random"):
        raise ConfigError("queue.order doit valoir 'alphabetical' ou 'random'.")
    fetch = config["fetch"]
    for key in ("min_views", "max_reels_per_account"):
        if not isinstance(fetch[key], int) or fetch[key] < 0:
            raise ConfigError(f"fetch.{key} doit être un entier positif ou nul.")
    delay = fetch["request_delay"]
    if not (isinstance(delay, list) and len(delay) == 2 and 0 <= delay[0] <= delay[1]):
        raise ConfigError("fetch.request_delay doit être [min, max] en secondes, par exemple [3, 6].")
    unknown = set(config["platforms"]) - set(PLATFORMS)
    if unknown:
        raise ConfigError(f"Plateformes inconnues dans 'platforms' : {', '.join(sorted(unknown))}.")
    return config


def enabled_platforms(config: dict) -> list[str]:
    return [p for p in PLATFORMS if config["platforms"].get(p)]
