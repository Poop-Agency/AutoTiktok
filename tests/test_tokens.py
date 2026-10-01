import pytest

from autotiktok.tokens import TokenStore, TokenStoreError, expires_soon, generate_key


def test_roundtrip_is_encrypted(tmp_path):
    key = generate_key()
    store = TokenStore(tmp_path / "t.enc", key)
    store.set("tiktok", {"access_token": "secret-value"})
    store.save()
    assert b"secret-value" not in (tmp_path / "t.enc").read_bytes()
    assert TokenStore(tmp_path / "t.enc", key).get("tiktok") == {"access_token": "secret-value"}


def test_wrong_key_is_reported(tmp_path):
    store = TokenStore(tmp_path / "t.enc", generate_key())
    store.set("x", {"a": 1})
    store.save()
    with pytest.raises(TokenStoreError):
        TokenStore(tmp_path / "t.enc", generate_key())
    with pytest.raises(TokenStoreError):
        TokenStore(tmp_path / "t.enc", "not-a-key")


def test_expires_soon():
    assert expires_soon({}, margin=10, now=0)
    assert expires_soon({"expires_at": 100}, margin=10, now=95)
    assert not expires_soon({"expires_at": 100}, margin=10, now=50)
