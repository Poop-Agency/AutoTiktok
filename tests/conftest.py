import httpx
import pytest

from autotiktok.config import DEFAULTS, _merge
from autotiktok.tokens import generate_key


@pytest.fixture
def config():
    return _merge(DEFAULTS, {})


@pytest.fixture
def client():
    with httpx.Client() as c:
        yield c


@pytest.fixture
def key(monkeypatch):
    k = generate_key()
    monkeypatch.setenv("TOKENS_KEY", k)
    return k


@pytest.fixture
def video_file(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * 1000)
    return path


def no_sleep(_seconds):
    pass
