"""iCloud Mail MCP server."""

import logging
import os
import sys

import anyio
import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse

from .github_oauth_provider import GitHubOAuthProvider
from .mail_tools import register_tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SERVER_URL = os.getenv("SERVER_URL", "")

_PROVIDER: GitHubOAuthProvider | None = None


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
        auth_server_provider=_PROVIDER,
        auth=auth_settings,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

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
