"""Platform publishers. Each exposes ``refresh_if_needed()``, ``publish(video)`` and ``token``."""

from .instagram import InstagramPublisher
from .tiktok import TikTokPublisher
from .youtube import YouTubePublisher

PUBLISHERS = {
    "instagram": InstagramPublisher,
    "tiktok": TikTokPublisher,
    "youtube": YouTubePublisher,
}
