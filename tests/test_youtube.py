import pytest
import respx

from autotiktok.platforms.base import PermanentError, RetryableError, Video
from autotiktok.platforms.youtube import TOKEN_URL, UPLOAD_URL, YouTubePublisher, build_title

ENV = {"GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "secret"}


def make(config, client, **token):
    base = {"access_token": "at", "refresh_token": "rt", "expires_at": 10**12}
    return YouTubePublisher(config, {**base, **token}, client, env=ENV)


def test_build_title():
    assert build_title("{caption}", "Mon <chat>") == "Mon chat #Shorts"
    assert build_title("{caption}", "") == "😂 #Shorts"
    assert build_title("{caption}", "déjà #shorts") == "déjà #shorts"
    assert len(build_title("{caption}", "a" * 300)) == 100


@respx.mock
def test_resumable_upload(config, client, video_file):
    init = respx.post(UPLOAD_URL).respond(200, headers={"Location": "https://upload.google/session"})
    put = respx.put("https://upload.google/session").respond(
        json={"id": "vid1", "status": {"privacyStatus": "private"}}
    )
    result = make(config, client).publish(Video(video_file, "drôle"))

    request = init.calls.last.request
    assert request.url.params["uploadType"] == "resumable"
    assert request.headers["X-Upload-Content-Length"] == "1000"
    assert b'"privacyStatus":"public"' in request.read()
    assert put.calls.last.request.headers["Authorization"] == "Bearer at"
    assert result.post_id == "vid1"
    assert result.details == {"url": "https://youtube.com/shorts/vid1", "privacy": "private"}


@respx.mock
def test_quota_is_retryable_bad_request_is_not(config, client, video_file):
    route = respx.post(UPLOAD_URL)
    route.respond(403, json={"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
    with pytest.raises(RetryableError):
        make(config, client).publish(Video(video_file, "c"))
    route.respond(400, json={"error": {"message": "bad", "errors": [{"reason": "invalidTitle"}]}})
    with pytest.raises(PermanentError):
        make(config, client).publish(Video(video_file, "c"))


@respx.mock
def test_refresh(config, client):
    respx.post(TOKEN_URL).respond(json={"access_token": "new", "expires_in": 3600})
    pub = make(config, client, expires_at=0)
    assert pub.refresh_if_needed(now=100)
    assert pub.token["access_token"] == "new" and pub.token["refresh_token"] == "rt"

    respx.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    with pytest.raises(PermanentError, match="7 jours"):
        make(config, client, expires_at=0).refresh_if_needed(now=100)
