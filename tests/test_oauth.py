import threading
import time
import urllib.request

import pytest

from autotiktok import oauth


def _hit(port, query):
    def run():
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/callback/?{query}", timeout=2).read()
                return
            except OSError:
                time.sleep(0.05)

    threading.Thread(target=run, daemon=True).start()


def test_loopback_returns_code(monkeypatch):
    monkeypatch.setattr(oauth.webbrowser, "open", lambda url: _hit(18765, "code=abc&state=s1"))
    assert oauth.wait_for_redirect(18765, "https://auth", "s1", timeout=10) == "abc"


def test_loopback_rejects_bad_state(monkeypatch):
    monkeypatch.setattr(oauth.webbrowser, "open", lambda url: _hit(18766, "code=abc&state=evil"))
    with pytest.raises(oauth.AuthError, match="state"):
        oauth.wait_for_redirect(18766, "https://auth", "s1", timeout=10)


def test_loopback_reports_denial(monkeypatch):
    monkeypatch.setattr(oauth.webbrowser, "open", lambda url: _hit(18767, "error=access_denied&state=s1"))
    with pytest.raises(oauth.AuthError, match="access_denied"):
        oauth.wait_for_redirect(18767, "https://auth", "s1", timeout=10)
