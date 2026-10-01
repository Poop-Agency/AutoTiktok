import httpx
import pytest
import respx

from autotiktok.platforms import tiktok
from autotiktok.platforms.base import PermanentError, RetryableError, Video
from autotiktok.platforms.tiktok import API_BASE, TikTokPublisher, chunk_plan

from .conftest import no_sleep

ENV = {"TIKTOK_CLIENT_KEY": "ck", "TIKTOK_CLIENT_SECRET": "cs"}
OK = {"error": {"code": "ok", "message": ""}}


def make(config, client, **token):
    return TikTokPublisher(config, {"access_token": "at", **token}, client, env=ENV, sleep=no_sleep)


def test_chunk_plan():
    assert chunk_plan(1000) == (1000, 1)
    assert chunk_plan(64_000_000) == (64_000_000, 1)
    assert chunk_plan(95_000_000) == (10_000_000, 9)
    with pytest.raises(PermanentError):
        chunk_plan(0)


@respx.mock
def test_draft_mode_uploads_to_inbox(config, client, video_file):
    init = respx.post(f"{API_BASE}/post/publish/inbox/video/init/").respond(
        json={"data": {"publish_id": "p1", "upload_url": "https://upload.tiktok/u"}, **OK}
    )
    put = respx.put("https://upload.tiktok/u").respond(201)
    respx.post(f"{API_BASE}/post/publish/status/fetch/").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"status": "PROCESSING_UPLOAD"}, **OK}),
            httpx.Response(200, json={"data": {"status": "SEND_TO_USER_INBOX"}, **OK}),
        ]
    )
    result = make(config, client).publish(Video(video_file, "hello"))

    body = init.calls.last.request.read()
    assert b"post_info" not in body and b'"video_size":1000' in body
    assert put.calls.last.request.headers["Content-Range"] == "bytes 0-999/1000"
    assert result.post_id == "p1"
    assert result.details["tiktok_status"] == "SEND_TO_USER_INBOX"


@respx.mock
def test_direct_mode_multi_chunk(config, client, video_file, monkeypatch):
    monkeypatch.setattr(tiktok, "SINGLE_CHUNK_MAX", 500)
    monkeypatch.setattr(tiktok, "CHUNK_SIZE", 300)
    config["tiktok"]["mode"] = "direct"
    respx.post(f"{API_BASE}/post/publish/creator_info/query/").respond(
        json={"data": {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]}, **OK}
    )
    init = respx.post(f"{API_BASE}/post/publish/video/init/").respond(
        json={"data": {"publish_id": "p2", "upload_url": "https://upload.tiktok/u"}, **OK}
    )
    put = respx.put("https://upload.tiktok/u").respond(206)
    respx.post(f"{API_BASE}/post/publish/status/fetch/").respond(
        json={"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [123]}, **OK}
    )
    result = make(config, client).publish(Video(video_file, "caption"))

    assert b'"total_chunk_count":3' in init.calls.last.request.read()
    assert b'"title":"caption"' in init.calls.last.request.read()
    ranges = [c.request.headers["Content-Range"] for c in put.calls]
    assert ranges == ["bytes 0-299/1000", "bytes 300-599/1000", "bytes 600-999/1000"]
    assert result.post_id == "123"


@respx.mock
def test_direct_mode_unaudited_is_permanent(config, client, video_file):
    config["tiktok"]["mode"] = "direct"
    respx.post(f"{API_BASE}/post/publish/creator_info/query/").respond(
        json={"data": {"privacy_level_options": ["SELF_ONLY"]}, **OK}
    )
    with pytest.raises(PermanentError, match="audit"):
        make(config, client).publish(Video(video_file, "c"))


@respx.mock
def test_error_classification(config, client, video_file):
    route = respx.post(f"{API_BASE}/post/publish/inbox/video/init/")
    route.respond(429, json={"error": {"code": "rate_limit_exceeded", "message": "slow down"}})
    with pytest.raises(RetryableError):
        make(config, client).publish(Video(video_file, "c"))
    route.respond(400, json={"error": {"code": "scope_not_authorized", "message": "no"}})
    with pytest.raises(PermanentError):
        make(config, client).publish(Video(video_file, "c"))


@respx.mock
def test_failed_processing_is_permanent(config, client, video_file):
    respx.post(f"{API_BASE}/post/publish/inbox/video/init/").respond(
        json={"data": {"publish_id": "p", "upload_url": "https://upload.tiktok/u"}, **OK}
    )
    respx.put("https://upload.tiktok/u").respond(201)
    respx.post(f"{API_BASE}/post/publish/status/fetch/").respond(
        json={"data": {"status": "FAILED", "fail_reason": "duration_check_failed"}, **OK}
    )
    with pytest.raises(PermanentError, match="duration_check_failed"):
        make(config, client).publish(Video(video_file, "c"))


@respx.mock
def test_status_timeout_counts_as_submitted(config, client, video_file):
    respx.post(f"{API_BASE}/post/publish/inbox/video/init/").respond(
        json={"data": {"publish_id": "p", "upload_url": "https://upload.tiktok/u"}, **OK}
    )
    respx.put("https://upload.tiktok/u").respond(201)
    respx.post(f"{API_BASE}/post/publish/status/fetch/").respond(json={"data": {"status": "PROCESSING_UPLOAD"}, **OK})
    result = make(config, client).publish(Video(video_file, "c"))
    assert result.post_id == "p" and result.details["tiktok_status"] == "PROCESSING_UPLOAD"


@respx.mock
def test_refresh_rotates_tokens(config, client):
    route = respx.post(tiktok.TOKEN_URL).respond(
        json={"access_token": "new", "expires_in": 86400, "refresh_token": "r2", "refresh_expires_in": 100}
    )
    pub = make(config, client, refresh_token="r1", expires_at=0)
    assert pub.refresh_if_needed(now=1000)
    assert b"refresh_token=r1" in route.calls.last.request.read()
    assert pub.token["access_token"] == "new" and pub.token["refresh_token"] == "r2"
    assert pub.token["expires_at"] == 1000 + 86400
    assert not pub.refresh_if_needed(now=1000)


@respx.mock
def test_refresh_rejected_is_permanent(config, client):
    respx.post(tiktok.TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    with pytest.raises(PermanentError, match="Reconnecte"):
        make(config, client, refresh_token="r1", expires_at=0).refresh_if_needed(now=1000)
