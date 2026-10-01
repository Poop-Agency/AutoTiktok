"""One-time account connection, run locally on the user's computer.

TikTok and Google use the OAuth "loopback" flow: a tiny local web server
receives the redirect after the user clicks "Authorize" in their browser.
Instagram uses a token generated from the Meta app dashboard.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import webbrowser
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .platforms import instagram, tiktok, youtube
from .platforms.instagram import LONG_LIVED_TTL
from .tokens import TokenStore, expires_at

TIKTOK_PORT = 8765
TIKTOK_REDIRECT = f"http://localhost:{TIKTOK_PORT}/callback/"
GOOGLE_PORT = 8766
GOOGLE_REDIRECT = f"http://127.0.0.1:{GOOGLE_PORT}/"


class AuthError(Exception):
    pass


def _require(env: Mapping[str, str], *names: str) -> list[str]:
    missing = [n for n in names if not env.get(n)]
    if missing:
        raise AuthError(f"Variables d'environnement manquantes : {', '.join(missing)}")
    return [env[n] for n in names]


def wait_for_redirect(port: int, auth_url: str, expected_state: str, timeout: float = 300) -> str:
    """Open ``auth_url`` in the browser and return the ``code`` received on the loopback server."""
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            if "code" not in params and "error" not in params:
                self.send_response(404)
                self.end_headers()
                return
            received.update(params)
            ok = "code" in params and params.get("state") == expected_state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            text = "Compte connecté, tu peux fermer cet onglet." if ok else "Échec de la connexion."
            self.wfile.write(f"<h1>{text}</h1>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    print(f"\nOuvre ce lien s'il ne s'ouvre pas tout seul :\n{auth_url}\n", flush=True)
    webbrowser.open(auth_url)
    deadline = time.time() + timeout
    try:
        while not received and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not received:
        raise AuthError("Aucune réponse reçue du navigateur (délai dépassé).")
    if "error" in received:
        raise AuthError(f"Autorisation refusée : {received.get('error_description') or received['error']}")
    if received.get("state") != expected_state:
        raise AuthError("Paramètre 'state' invalide : recommence la connexion.")
    return received["code"]


def _verifier() -> str:
    return secrets.token_urlsafe(48)[:64]


def auth_tiktok(store: TokenStore, env: Mapping[str, str], client: httpx.Client) -> str:
    client_key, client_secret = _require(env, "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET")
    verifier = _verifier()
    state = secrets.token_urlsafe(16)
    params = {
        "client_key": client_key,
        "scope": ",".join(tiktok.SCOPES),
        "response_type": "code",
        "redirect_uri": TIKTOK_REDIRECT,
        "state": state,
        # TikTok's PKCE variant: hex-encoded SHA256 of the verifier (not base64url).
        "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(),
        "code_challenge_method": "S256",
    }
    code = wait_for_redirect(TIKTOK_PORT, f"{tiktok.AUTH_URL}?{urlencode(params)}", state)
    resp = client.post(
        tiktok.TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": TIKTOK_REDIRECT,
            "code_verifier": verifier,
        },
    )
    body = resp.json()
    if "access_token" not in body:
        raise AuthError(f"TikTok a refusé l'échange du code : {body}")
    store.set("tiktok", tiktok.token_from_response(body))
    store.save()
    granted = body.get("scope", "")
    return f"TikTok connecté (scopes : {granted})."


def auth_youtube(store: TokenStore, env: Mapping[str, str], client: httpx.Client) -> str:
    client_id, client_secret = _require(env, "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
    verifier = _verifier()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": GOOGLE_REDIRECT,
        "response_type": "code",
        "scope": " ".join(youtube.SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    code = wait_for_redirect(GOOGLE_PORT, f"{youtube.AUTH_URL}?{urlencode(params)}", state)
    resp = client.post(
        youtube.TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": GOOGLE_REDIRECT,
            "code_verifier": verifier,
        },
    )
    body = resp.json()
    if "refresh_token" not in body:
        raise AuthError(f"Google n'a pas renvoyé de refresh token : {body}")
    store.set(
        "youtube",
        {
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
            "expires_at": expires_at(body.get("expires_in")),
        },
    )
    store.save()
    return "YouTube connecté."


def auth_instagram(store: TokenStore, env: Mapping[str, str], client: httpx.Client, token: str) -> str:
    token = token.strip()
    if not token:
        raise AuthError("Passe le jeton généré dans le tableau de bord Meta avec --token.")
    now = time.time()
    ttl: float = LONG_LIVED_TTL
    app_secret = env.get("IG_APP_SECRET", "")
    if app_secret:
        exchanged = instagram.exchange_for_long_lived(client, token, app_secret)
        if exchanged:
            token = exchanged["access_token"]
            ttl = float(exchanged.get("expires_in") or LONG_LIVED_TTL)
    account = instagram.fetch_account(client, token)
    user_id = str(account.get("user_id") or account.get("id") or "")
    if not user_id:
        raise AuthError(f"Impossible de lire l'identifiant du compte Instagram : {account}")
    store.set(
        "instagram",
        {"access_token": token, "user_id": user_id, "expires_at": now + ttl, "issued_at": now},
    )
    store.save()
    return f"Instagram connecté (@{account.get('username', '?')})."
