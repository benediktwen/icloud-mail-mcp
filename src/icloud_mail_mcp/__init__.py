"""iCloud Mail MCP server."""

import base64
import logging
import os
import sys

import anyio
import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.types import Icon
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .github_oauth_provider import GitHubOAuthProvider
from .mail_tools import register_tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SERVER_URL = os.getenv("SERVER_URL", "")

_PROVIDER: GitHubOAuthProvider | None = None

_FAVICON_B64 = (
    "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"
)
_FAVICON_PNG = base64.b64decode(_FAVICON_B64)
_FAVICON_URI = f"data:image/png;base64,{_FAVICON_B64}"


def _build_app() -> FastMCP:
    global _PROVIDER

    github_client_id     = os.getenv("GITHUB_CLIENT_ID", "")
    github_client_secret = os.getenv("GITHUB_CLIENT_SECRET", "")

    if not github_client_id or not github_client_secret:
        logger.error("GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET must be set.")
        sys.exit(1)

    if not SERVER_URL:
        logger.error("SERVER_URL must be set to the public base URL of this service.")
        sys.exit(1)

    _PROVIDER = GitHubOAuthProvider(
        github_client_id=github_client_id,
        github_client_secret=github_client_secret,
        server_url=SERVER_URL,
    )

    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(SERVER_URL),
        resource_server_url=AnyHttpUrl(SERVER_URL),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["mail"],
            default_scopes=["mail"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )

    mcp = FastMCP(
        "iCloud Mail MCP",
        icons=[Icon(src=_FAVICON_URI, mimeType="image/png", sizes=["32x32"])],
        auth_server_provider=_PROVIDER,
        auth=auth_settings,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/favicon.ico", methods=["GET"])
    async def favicon(_request: Request) -> Response:
        return Response(content=_FAVICON_PNG, media_type="image/png")

    @mcp.custom_route("/auth/callback", methods=["GET"])
    async def github_callback(request: Request):
        return await _PROVIDER.handle_github_callback(request)

    register_tools(mcp)
    return mcp


async def _serve(mcp: FastMCP) -> None:
    config = uvicorn.Config(
        mcp.streamable_http_app(),
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level="info",
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    mcp = _build_app()
    logger.info("GitHub OAuth enabled — only '%s' can authenticate.", os.getenv("GITHUB_ALLOWED_USER", "(not configured)"))
    anyio.run(_serve, mcp)
