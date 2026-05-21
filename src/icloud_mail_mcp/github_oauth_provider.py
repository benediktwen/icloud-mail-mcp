"""GitHub OAuth provider with Upstash Redis token persistence.

Pattern shared across all Render-hosted MCPs — change only TOKEN_STORE_KEY.
"""

import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from upstash_redis import Redis

TOKEN_STORE_KEY: str = os.getenv("TOKEN_STORE_KEY", "icloud-mail-mcp")
GITHUB_ALLOWED_USER: str = os.getenv("GITHUB_ALLOWED_USER", "benediktwen")
GITHUB_CLIENT_ID: str = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET: str = os.getenv("GITHUB_CLIENT_SECRET", "")
SERVER_URL: str = os.getenv("SERVER_URL", "http://localhost:8000")

TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days
CODE_TTL = 60 * 10  # 10 minutes


def _redis() -> Redis:
    return Redis(
        url=os.environ["UPSTASH_REDIS_REST_URL"],
        token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
    )


def _key(suffix: str) -> str:
    return f"{TOKEN_STORE_KEY}:{suffix}"


class GitHubOAuthProvider(OAuthAuthorizationServerProvider):
    """OAuth provider that delegates identity to GitHub and restricts to one user."""

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = _redis().get(_key(f"client:{client_id}"))
        if raw is None:
            return None
        return OAuthClientInformationFull.model_validate_json(raw)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        _redis().set(
            _key(f"client:{client_info.client_id}"),
            client_info.model_dump_json(),
            ex=TOKEN_TTL,
        )

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        state = secrets.token_urlsafe(32)
        pending: dict[str, Any] = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri) if params.redirect_uri else None,
            "scope": list(params.scopes or []),
            "code_challenge": params.code_challenge,
            "code_challenge_method": params.code_challenge_method,
            "resource": str(params.resource) if params.resource else None,
            "mcp_state": params.state,
        }
        _redis().set(_key(f"pending:{state}"), json.dumps(pending), ex=CODE_TTL)

        github_params = {
            "client_id": GITHUB_CLIENT_ID,
            "redirect_uri": f"{SERVER_URL}/auth/callback",
            "scope": "read:user",
            "state": state,
        }
        return f"https://github.com/login/oauth/authorize?{urlencode(github_params)}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        raw = _redis().get(_key(f"code:{authorization_code}"))
        if raw is None:
            return None
        data = json.loads(raw)
        return AuthorizationCode(
            code=authorization_code,
            client_id=data["client_id"],
            redirect_uri=data.get("redirect_uri"),
            redirect_uri_provided_explicitly=bool(data.get("redirect_uri")),
            scopes=data.get("scope", []),
            expires_at=data["expires_at"],
            code_challenge=data.get("code_challenge"),
            code_challenge_method=data.get("code_challenge_method"),
            resource=data.get("resource"),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        now = int(time.time())

        token_data: dict[str, Any] = {
            "client_id": client.client_id,
            "scopes": list(authorization_code.scopes or []),
            "expires_at": now + TOKEN_TTL,
            "resource": authorization_code.resource,
        }
        _redis().set(_key(f"token:{access_token}"), json.dumps(token_data), ex=TOKEN_TTL)
        _redis().set(_key(f"refresh:{refresh_token}"), json.dumps(token_data), ex=TOKEN_TTL)
        _redis().delete(_key(f"code:{authorization_code.code}"))

        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=TOKEN_TTL,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes or []),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        raw = _redis().get(_key(f"token:{token}"))
        if raw is None:
            return None
        data = json.loads(raw)
        if int(time.time()) > data.get("expires_at", 0):
            _redis().delete(_key(f"token:{token}"))
            return None
        return AccessToken(
            token=token,
            client_id=data["client_id"],
            scopes=data.get("scopes", []),
            expires_at=data.get("expires_at"),
            resource=data.get("resource"),
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        raw = _redis().get(_key(f"refresh:{refresh_token}"))
        if raw is None:
            return None
        data = json.loads(raw)
        if int(time.time()) > data.get("expires_at", 0):
            _redis().delete(_key(f"refresh:{refresh_token}"))
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data.get("scopes", []),
            expires_at=data.get("expires_at"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        new_access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        now = int(time.time())

        token_data: dict[str, Any] = {
            "client_id": client.client_id,
            "scopes": scopes or list(refresh_token.scopes or []),
            "expires_at": now + TOKEN_TTL,
        }
        _redis().set(_key(f"token:{new_access}"), json.dumps(token_data), ex=TOKEN_TTL)
        _redis().set(_key(f"refresh:{new_refresh}"), json.dumps(token_data), ex=TOKEN_TTL)
        _redis().delete(_key(f"refresh:{refresh_token.token}"))

        return OAuthToken(
            access_token=new_access,
            token_type="bearer",
            expires_in=TOKEN_TTL,
            refresh_token=new_refresh,
            scope=" ".join(token_data["scopes"]),
        )

    async def handle_github_callback(self, code: str, state: str) -> str:
        """Exchange GitHub code for our token. Returns redirect URI for the browser."""
        raw = _redis().get(_key(f"pending:{state}"))
        if raw is None:
            raise ValueError("Unknown or expired state")
        pending = json.loads(raw)
        _redis().delete(_key(f"pending:{state}"))

        async with httpx.AsyncClient() as http:
            resp = await http.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "client_secret": GITHUB_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": f"{SERVER_URL}/auth/callback",
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            github_token = resp.json().get("access_token")

            user_resp = await http.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/json",
                },
            )
            user_resp.raise_for_status()
            github_login = user_resp.json().get("login", "")

        if github_login.lower() != GITHUB_ALLOWED_USER.lower():
            raise PermissionError(f"User {github_login!r} is not authorised")

        auth_code = secrets.token_urlsafe(32)
        code_data: dict[str, Any] = {
            "client_id": pending["client_id"],
            "redirect_uri": pending.get("redirect_uri"),
            "scope": pending.get("scope", []),
            "expires_at": int(time.time()) + CODE_TTL,
            "code_challenge": pending.get("code_challenge"),
            "code_challenge_method": pending.get("code_challenge_method"),
            "resource": pending.get("resource"),
        }
        _redis().set(_key(f"code:{auth_code}"), json.dumps(code_data), ex=CODE_TTL)

        redirect_uri = pending.get("redirect_uri") or ""
        mcp_state = pending.get("mcp_state", "")
        sep = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{sep}code={auth_code}&state={mcp_state}"
