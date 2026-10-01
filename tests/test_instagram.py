import httpx
import pytest
import respx

from autotiktok.platforms.base import PermanentError, RetryableError, Video
from autotiktok.platforms.instagram import GRAPH, REFRESH_URL, RUPLOAD, InstagramPublisher

from .conftest import no_sleep


def make(config, client, **token):
    base = {"access_token": "at", "user_id": "42", "expires_at": 10**12, "issued_at": 0}
    return InstagramPublisher(config, {**base, **token}, client, sleep=no_sleep)


@respx.mock
def test_publish_reel(config, client, video_file):
    create = respx.post(f"{GRAPH}/42/media").respond(json={"id": "c1"})
    upload = respx.post(f"{RUPLOAD}/c1").respond(json={"success": True})
    respx.get(f"{GRAPH}/c1").mock(
        side_effect=[
            httpx.Response(200, json={"status_code": "IN_PROGRESS"}),
            httpx.Response(200, json={"status_code": "FINISHED"}),
        ]
    )
    publish = respx.post(f"{GRAPH}/42/media_publish").respond(json={"id": "m1"})
    respx.get(f"{GRAPH}/m1").respond(json={"permalink": "https://instagram.com/reel/x"})

    result = make(config, client).publish(Video(video_file, "lol"))

    form = create.calls.last.request.read()
    assert b"media_type=REELS" in form and b"upload_type=resumable" in form and b"caption=lol" in form
    headers = upload.calls.last.request.headers
    assert headers["Authorization"] == "OAuth at" and headers["file_size"] == "1000" and headers["offset"] == "0"
    assert b"creation_id=c1" in publish.calls.last.request.read()
    assert result.post_id == "m1" and result.details["url"] == "https://instagram.com/reel/x"


@respx.mock
def test_container_error_is_permanent(config, client, video_file):
    respx.post(f"{GRAPH}/42/media").respond(json={"id": "c1"})
    respx.post(f"{RUPLOAD}/c1").respond(json={"success": True})
    respx.get(f"{GRAPH}/c1").respond(json={"status_code": "ERROR", "status": "bad codec"})
    with pytest.raises(PermanentError, match="bad codec"):
        make(config, client).publish(Video(video_file, "c"))


@respx.mock
def test_error_classification(config, client, video_file):
    route = respx.post(f"{GRAPH}/42/media")
    route.respond(400, json={"error": {"message": "Invalid token", "code": 190}})
    with pytest.raises(PermanentError, match="reconnecte"):
        make(config, client).publish(Video(video_file, "c"))
    route.respond(400, json={"error": {"message": "limit", "code": 4}})
    with pytest.raises(RetryableError):
        make(config, client).publish(Video(video_file, "c"))
    route.respond(400, json={"error": {"message": "nope", "code": 100}})
    with pytest.raises(PermanentError):
        make(config, client).publish(Video(video_file, "c"))


@respx.mock
def test_refresh_only_when_close_to_expiry(config, client):
    route = respx.get(REFRESH_URL).respond(json={"access_token": "new", "expires_in": 5_184_000})
    day = 86400
    assert not make(config, client, expires_at=100 * day).refresh_if_needed(now=10 * day)
    pub = make(config, client, expires_at=20 * day, issued_at=0)
    assert pub.refresh_if_needed(now=10 * day)
    assert pub.token["access_token"] == "new" and pub.token["issued_at"] == 10 * day
    assert route.call_count == 1
