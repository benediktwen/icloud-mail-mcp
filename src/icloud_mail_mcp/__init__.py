"""iCloud Mail MCP server."""

import os

import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import RedirectResponse

from .github_oauth_provider import SERVER_URL, GitHubOAuthProvider
from .mail_tools import register_tools

_PROVIDER = GitHubOAuthProvider()


def _build_app() -> FastMCP:
    mcp = FastMCP(
        "icloud-mail-mcp",
        auth_server_provider=_PROVIDER,
        auth=AuthSettings(
            issuer_url=SERVER_URL,
            resource_server_url=SERVER_URL,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["mail"],
                default_scopes=["mail"],
            ),
            required_scopes=["mail"],
        ),
    )

    # GitHub OAuth callback — browser lands here after approving on github.com
    @mcp.custom_route("/auth/callback", methods=["GET"])
    async def github_callback(request: Request) -> RedirectResponse:
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        redirect_uri = await _PROVIDER.handle_github_callback(code=code, state=state)
        return RedirectResponse(url=redirect_uri)

    register_tools(mcp)
    return mcp


def main() -> None:
    app = _build_app()
    uvicorn.run(
        app.streamable_http_app(),
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )
