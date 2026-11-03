"""
Tests for the GitHub OAuth provider's hardening against open dynamic client
registration: redirect-URI allowlist, consent page, bounded in-memory state and
resource (audience) checks.
"""
import asyncio
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from starlette.requests import Request

from icloud_mail_mcp import github_oauth_provider as gop

SERVER_URL = "https://example.invalid/svc"
CLAUDE_CB  = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        "TOKEN_STORE_FILE", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
        "OAUTH_ALLOWED_REDIRECT_URIS", "GITHUB_ALLOWED_USER",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITHUB_ALLOWED_USER_ID", "42")


def _provider():
    return gop.GitHubOAuthProvider("client-id", "client-secret", SERVER_URL, service_name="Test MCP")


def _client(client_id="claude-client", redirect_uris=(CLAUDE_CB,), name="Claude"):
    return OAuthClientInformationFull(client_id=client_id, client_name=name, redirect_uris=list(redirect_uris))


def _params(redirect_uri=CLAUDE_CB, resource=None, state="mcp-state"):
    return AuthorizationParams(
        state=state, scopes=["mcp"], code_challenge="challenge",
        redirect_uri=redirect_uri, redirect_uri_provided_explicitly=True, resource=resource,
    )


def _get(query: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/auth/callback", "scheme": "https",
        "server": ("example.invalid", 443), "query_string": query.encode(), "headers": [],
    })


def _post_form(body: str) -> Request:
    data = body.encode()

    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}

    return Request({
        "type": "http", "method": "POST", "path": "/auth/consent", "scheme": "https",
        "server": ("example.invalid", 443), "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded"),
                    (b"content-length", str(len(data)).encode())],
    }, receive)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeGitHub:
    """Stands in for httpx.AsyncClient: GitHub token exchange + user lookup."""

    def __init__(self, user):
        self._user = user

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        return _FakeResponse({"access_token": "gh-token"})

    async def get(self, url, **kwargs):
        return _FakeResponse(self._user)


@pytest.fixture
def github(monkeypatch):
    def _install(user_id=42, login="ben"):
        monkeypatch.setattr(gop.httpx, "AsyncClient", _FakeGitHub({"id": user_id, "login": login}))
    _install()
    return _install


def _start_flow(provider, client=None, params=None):
    client = client or _client()
    asyncio.run(provider.register_client(client))
    url = asyncio.run(provider.authorize(client, params or _params()))
    assert url.startswith(gop.GITHUB_AUTHORIZE_URL)
    return client, parse_qs(urlsplit(url).query)["state"][0]


def _callback(provider, state):
    return asyncio.run(provider.handle_github_callback(_get(f"code=gh-code&state={state}")))


def _consent_id(body: str) -> str:
    return re.search(r'name="consent_id" value="([^"]+)"', body).group(1)


def _consent(provider, consent_id, decision="allow"):
    return asyncio.run(provider.handle_consent(_post_form(f"consent_id={consent_id}&decision={decision}")))


# ---------------------------------------------------------------------- #
# Redirect-URI allowlist                                                   #
# ---------------------------------------------------------------------- #

class TestRedirectAllowlist:
    def test_default_allowlist_accepts_claude_callbacks(self):
        provider = _provider()
        asyncio.run(provider.register_client(_client("a", [CLAUDE_CB])))
        asyncio.run(provider.register_client(_client("b", ["https://claude.com/api/mcp/auth_callback"])))
        assert set(provider._clients) == {"a", "b"}

    def test_rejects_unknown_redirect_uri(self):
        provider = _provider()
        with pytest.raises(RegistrationError) as exc:
            asyncio.run(provider.register_client(_client("evil", ["https://attacker.example/cb"])))
        assert exc.value.error == "invalid_redirect_uri"
        assert "attacker.example" in exc.value.error_description
        assert "evil" not in provider._clients

    def test_rejects_if_any_uri_is_disallowed(self):
        provider = _provider()
        with pytest.raises(RegistrationError):
            asyncio.run(provider.register_client(_client("mixed", [CLAUDE_CB, "https://attacker.example/cb"])))
        assert "mixed" not in provider._clients

    def test_env_override_replaces_default_list(self, monkeypatch):
        monkeypatch.setenv("OAUTH_ALLOWED_REDIRECT_URIS", "https://other.example/cb, https://two.example/cb")
        provider = _provider()
        asyncio.run(provider.register_client(_client("ok", ["https://two.example/cb"])))
        with pytest.raises(RegistrationError):
            asyncio.run(provider.register_client(_client("claude", [CLAUDE_CB])))
        assert set(provider._clients) == {"ok"}

    def test_loopback_entry_without_port_matches_any_port(self, monkeypatch):
        monkeypatch.setenv("OAUTH_ALLOWED_REDIRECT_URIS", "http://127.0.0.1/callback")
        allowed = gop._allowed_redirect_uris()
        assert gop.redirect_uri_allowed("http://127.0.0.1:53123/callback", allowed)
        assert gop.redirect_uri_allowed("http://127.0.0.1/callback", allowed)
        assert not gop.redirect_uri_allowed("http://127.0.0.1:53123/other", allowed)
        assert not gop.redirect_uri_allowed("http://localhost:53123/callback", allowed)
        assert not gop.redirect_uri_allowed("https://127.0.0.1:53123/callback", allowed)
        assert not gop.redirect_uri_allowed("http://attacker.example/callback", allowed)

    def test_loopback_entry_with_port_is_exact(self):
        allowed = ("http://localhost:3000/oauth/callback",)
        assert gop.redirect_uri_allowed("http://localhost:3000/oauth/callback", allowed)
        assert not gop.redirect_uri_allowed("http://localhost:3001/oauth/callback", allowed)

    def test_non_loopback_never_wildcards(self):
        allowed = ("https://claude.ai/api/mcp/auth_callback",)
        assert not gop.redirect_uri_allowed("https://claude.ai:8443/api/mcp/auth_callback", allowed)
        assert not gop.redirect_uri_allowed("https://claude.ai.attacker.example/api/mcp/auth_callback", allowed)
        assert not gop.redirect_uri_allowed("https://claude.ai/api/mcp/auth_callback?x=1", allowed)

    def test_persisted_client_with_disallowed_redirect_is_dropped(self, tmp_path, monkeypatch):
        path = tmp_path / "token_store.json"
        monkeypatch.setenv("TOKEN_STORE_FILE", str(path))
        future = int(time.time()) + 3600
        path.write_text(json.dumps({
            "clients": {
                "good": _client("good").model_dump(mode="json"),
                "evil": _client("evil", ["https://attacker.example/cb"]).model_dump(mode="json"),
            },
            "access_tokens": {
                "tok-good": {"token": "tok-good", "client_id": "good", "scopes": ["mcp"], "expires_at": future},
                "tok-evil": {"token": "tok-evil", "client_id": "evil", "scopes": ["mcp"], "expires_at": future},
            },
            "refresh_tokens": {
                "rt-evil": {"token": "rt-evil", "client_id": "evil", "scopes": ["mcp"], "expires_at": future},
            },
        }))

        provider = _provider()
        assert set(provider._clients) == {"good"}
        assert set(provider._access_tokens) == {"tok-good"}
        assert provider._refresh_tokens == {}
        # and the purge is written back
        assert "evil" not in json.loads(path.read_text())["clients"]


# ---------------------------------------------------------------------- #
# Consent page                                                             #
# ---------------------------------------------------------------------- #

class TestConsent:
    def test_callback_shows_consent_page_instead_of_redirecting(self, github):
        provider = _provider()
        _, state = _start_flow(provider)
        resp = _callback(provider, state)
        body = resp.body.decode()
        assert resp.status_code == 200
        assert "Claude" in body and CLAUDE_CB in body and "ben" in body and "Test MCP" in body
        assert 'action="https://example.invalid/svc/auth/consent"' in body
        assert resp.headers["cache-control"] == "no-store"
        assert provider._auth_codes == {}  # nothing issued before the user clicks Allow
        assert state not in provider._pending  # GitHub state is single-use

    def test_allow_issues_code_and_redirects_to_client(self, github):
        provider = _provider()
        client, state = _start_flow(provider)
        consent_id = _consent_id(_callback(provider, state).body.decode())

        resp = _consent(provider, consent_id, "allow")
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith(CLAUDE_CB + "?")
        q = parse_qs(urlsplit(location).query)
        assert q["state"] == ["mcp-state"]
        code = provider._auth_codes[q["code"][0]]
        assert code.client_id == client.client_id
        assert code.code_challenge == "challenge"
        assert str(code.redirect_uri) == CLAUDE_CB

    def test_deny_redirects_with_access_denied(self, github):
        provider = _provider()
        _, state = _start_flow(provider)
        consent_id = _consent_id(_callback(provider, state).body.decode())

        resp = _consent(provider, consent_id, "deny")
        assert resp.status_code == 302
        q = parse_qs(urlsplit(resp.headers["location"]).query)
        assert q["error"] == ["access_denied"] and q["state"] == ["mcp-state"]
        assert "code" not in q
        assert provider._auth_codes == {}

    def test_consent_is_single_use(self, github):
        provider = _provider()
        _, state = _start_flow(provider)
        consent_id = _consent_id(_callback(provider, state).body.decode())
        assert _consent(provider, consent_id).status_code == 302
        assert _consent(provider, consent_id).status_code == 400

    def test_unknown_or_missing_consent_id_is_rejected(self, github):
        provider = _provider()
        assert _consent(provider, "nope").status_code == 400
        assert asyncio.run(provider.handle_consent(_post_form("decision=allow"))).status_code == 400
        assert provider._auth_codes == {}

    def test_expired_consent_is_rejected(self, github):
        provider = _provider()
        _, state = _start_flow(provider)
        consent_id = _consent_id(_callback(provider, state).body.decode())
        provider._consents[consent_id]["expires_at"] = time.time() - 1
        assert _consent(provider, consent_id).status_code == 400
        assert provider._auth_codes == {}

    def test_wrong_github_user_gets_no_consent_page(self, github):
        github(user_id=7, login="mallory")
        provider = _provider()
        _, state = _start_flow(provider)
        resp = _callback(provider, state)
        assert resp.status_code == 403
        assert provider._consents == {} and provider._auth_codes == {}

    def test_consent_page_escapes_client_supplied_values(self, github):
        provider = _provider()
        client = _client(name='<script>alert(1)</script>')
        _, state = _start_flow(provider, client=client)
        body = _callback(provider, state).body.decode()
        assert "<script>" not in body
        assert "&lt;script&gt;" in body

    def test_unknown_state_is_rejected(self, github):
        provider = _provider()
        assert _callback(provider, "bogus").status_code == 400


# ---------------------------------------------------------------------- #
# Bounded in-memory state                                                  #
# ---------------------------------------------------------------------- #

class TestBoundedState:
    def test_pending_flow_expires(self, github):
        provider = _provider()
        _, state = _start_flow(provider)
        provider._pending[state]["expires_at"] = time.time() - 1
        assert _callback(provider, state).status_code == 400

    def test_pending_flows_are_capped(self, monkeypatch):
        monkeypatch.setattr(gop, "MAX_PENDING", 3)
        provider = _provider()
        client = _client()
        asyncio.run(provider.register_client(client))
        states = []
        for _ in range(10):
            url = asyncio.run(provider.authorize(client, _params()))
            states.append(parse_qs(urlsplit(url).query)["state"][0])
        assert len(provider._pending) == 3
        assert states[-1] in provider._pending and states[0] not in provider._pending

    def test_consents_are_capped(self, github, monkeypatch):
        monkeypatch.setattr(gop, "MAX_CONSENTS", 2)
        provider = _provider()
        client = _client()
        asyncio.run(provider.register_client(client))
        for _ in range(5):
            url = asyncio.run(provider.authorize(client, _params()))
            state = parse_qs(urlsplit(url).query)["state"][0]
            assert _callback(provider, state).status_code == 200
        assert len(provider._consents) == 2

    def test_sweep_drops_expired_entries_everywhere(self):
        provider = _provider()
        past, future = time.time() - 1, time.time() + 3600
        provider._pending["p-old"] = {"expires_at": past}
        provider._pending["p-new"] = {"expires_at": future}
        provider._consents["c-old"] = {"expires_at": past}
        provider._auth_codes["old"] = AuthorizationCode(
            code="old", client_id="c", scopes=[], expires_at=past, code_challenge="x",
            redirect_uri=CLAUDE_CB, redirect_uri_provided_explicitly=True,
        )
        provider._access_tokens["at-old"] = AccessToken(token="at-old", client_id="c", scopes=[], expires_at=int(past))
        provider._access_tokens["at-new"] = AccessToken(token="at-new", client_id="c", scopes=[], expires_at=int(future))
        provider._refresh_tokens["rt-old"] = RefreshToken(token="rt-old", client_id="c", scopes=[], expires_at=int(past))

        provider._sweep(force=True)

        assert set(provider._pending) == {"p-new"}
        assert provider._consents == {} and provider._auth_codes == {} and provider._refresh_tokens == {}
        assert set(provider._access_tokens) == {"at-new"}

    def test_sweep_is_rate_limited_unless_forced(self):
        provider = _provider()
        provider._last_sweep = time.time()
        provider._pending["p-old"] = {"expires_at": time.time() - 1}
        provider._sweep()
        assert "p-old" in provider._pending
        provider._sweep(force=True)
        assert provider._pending == {}

    def test_token_exchange_sweeps_expired_tokens(self):
        provider = _provider()
        client = _client()
        provider._access_tokens["stale"] = AccessToken(token="stale", client_id="c", scopes=[], expires_at=int(time.time()) - 1)
        code = AuthorizationCode(
            code="code", client_id=client.client_id, scopes=["mcp"], expires_at=time.time() + 60,
            code_challenge="x", redirect_uri=CLAUDE_CB, redirect_uri_provided_explicitly=True,
        )
        provider._auth_codes["code"] = code
        token = asyncio.run(provider.exchange_authorization_code(client, code))
        assert "stale" not in provider._access_tokens
        assert token.access_token in provider._access_tokens
        assert "code" not in provider._auth_codes


# ---------------------------------------------------------------------- #
# Resource indicator (audience)                                            #
# ---------------------------------------------------------------------- #

class TestResource:
    def test_authorize_rejects_resource_for_another_server(self):
        provider = _provider()
        client = _client()
        asyncio.run(provider.register_client(client))
        with pytest.raises(AuthorizeError) as exc:
            asyncio.run(provider.authorize(client, _params(resource="https://other.example/mcp")))
        assert exc.value.error == "invalid_request"
        assert provider._pending == {}

    @pytest.mark.parametrize("resource", [None, SERVER_URL, SERVER_URL + "/", SERVER_URL + "/mcp"])
    def test_authorize_accepts_this_server(self, resource):
        provider = _provider()
        client = _client()
        asyncio.run(provider.register_client(client))
        asyncio.run(provider.authorize(client, _params(resource=resource)))
        assert len(provider._pending) == 1

    def test_prefix_must_be_a_path_boundary(self):
        provider = _provider()
        assert not provider._resource_allowed("https://example.invalid/svc2")
        assert not provider._resource_allowed("https://example.invalid/svcx/mcp")

    def test_load_access_token_rejects_foreign_resource(self):
        provider = _provider()
        future = int(time.time()) + 3600
        provider._access_tokens["ok"] = AccessToken(token="ok", client_id="c", scopes=[], expires_at=future, resource=SERVER_URL + "/mcp")
        provider._access_tokens["foreign"] = AccessToken(token="foreign", client_id="c", scopes=[], expires_at=future, resource="https://other.example/mcp")
        assert asyncio.run(provider.load_access_token("ok")) is not None
        assert asyncio.run(provider.load_access_token("foreign")) is None
        assert "foreign" not in provider._access_tokens
