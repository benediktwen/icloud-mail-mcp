"""
GitHub OAuth provider for FastMCP.

Flow:
  1. Claude → GET /authorize  → redirect to GitHub login
  2. GitHub → GET /auth/callback → verify user id → show consent page
  3. User  → POST /auth/consent  → approve → redirect back to Claude with a code
  4. Claude → POST /token → exchange code for access token
  5. Claude uses Bearer access token on every MCP request

Dynamic client registration (RFC 7591) is open — any party can register a
client — so two things keep an attacker-registered client from ever receiving
a token for the allowed GitHub user:
  - a client may only register redirect URIs from an allowlist (by default the
    Claude callback URLs; override with OAUTH_ALLOWED_REDIRECT_URIS), and
  - after the GitHub login the user sees a consent page naming the client and
    the redirect URI, and must approve it before a code is issued.

Tokens and client registrations are persisted so re-authentication is not
required after container restarts:
  - TOKEN_STORE_FILE set → local JSON file (self-hosted, persistent volume)
  - else UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN → Upstash Redis
  - else in-memory only (tokens lost on restart)
_pending, _consents and _auth_codes are always in-memory (short-lived, tied to
an active browser session) and are bounded by a TTL and a size cap.
"""

import contextlib
import html
import json
import logging
import os
import secrets
import tempfile
import time
from urllib.parse import urlencode, urlsplit

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL     = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL      = "https://api.github.com/user"

ACCESS_TOKEN_TTL  = 3600 * 24 * 30    # 30 days
REFRESH_TOKEN_TTL = 3600 * 24 * 30    # 30 days
AUTH_CODE_TTL     = 300               # 5 minutes (exchanged immediately)
PENDING_TTL       = 600               # 10 minutes to finish the GitHub login
CONSENT_TTL       = 300               # 5 minutes to click "Allow"
MAX_CLIENTS       = 100               # cap dynamically-registered clients (open DCR ⇒ bound the store)
MAX_PENDING       = 500               # cap unfinished /authorize flows (unauthenticated ⇒ bound memory)
MAX_CONSENTS      = 100               # cap consent pages waiting for a click
SWEEP_INTERVAL    = 60                # seconds between sweeps of expired in-memory entries

# Redirect URIs a dynamically registered client may use. Registration is open,
# so without this an attacker registers a client pointing at their own server,
# sends the allowed user a crafted /authorize link and — holding the PKCE
# verifier — turns the resulting code into an access token. Exact match;
# loopback entries (http://127.0.0.1/..., http://localhost/...) match any port
# (RFC 8252 §7.3). Override with OAUTH_ALLOWED_REDIRECT_URIS (comma-separated).
DEFAULT_ALLOWED_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _model_to_dict(obj) -> dict:
    """Serialize a Pydantic v1/v2 model or dataclass to a plain dict."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "dict"):
        return obj.dict()
    return obj.__dict__


def _allowed_redirect_uris() -> tuple[str, ...]:
    raw = os.getenv("OAUTH_ALLOWED_REDIRECT_URIS", "")
    configured = tuple(u.strip() for u in raw.split(",") if u.strip())
    return configured or DEFAULT_ALLOWED_REDIRECT_URIS


def redirect_uri_allowed(uri: str, allowed: tuple[str, ...]) -> bool:
    """Exact match against the allowlist; loopback entries ignore the port."""
    uri = str(uri)
    if uri in allowed:
        return True
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.scheme != "http" or parts.hostname not in _LOOPBACK_HOSTS:
        return False
    for entry in allowed:
        e = urlsplit(entry)
        if (
            e.scheme == "http"
            and e.hostname in _LOOPBACK_HOSTS
            and e.port is None
            and e.hostname == parts.hostname
            and e.path == parts.path
            and e.query == parts.query
        ):
            return True
    return False


class _UpstashRedis:
    """Minimal synchronous Upstash Redis REST client (no extra dependencies).

    Uses the Upstash command API (POST / with ["CMD", args...]) to avoid
    JSON double-encoding issues that occur with the /set/{key} endpoint.
    """

    def __init__(self, url: str, token: str) -> None:
        self._url   = url.rstrip("/")
        self._token = token

    def _cmd(self, *args) -> object:
        with httpx.Client(timeout=5) as client:
            r = client.post(
                self._url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                content=json.dumps(list(args)),
            )
            r.raise_for_status()
            return r.json().get("result")

    def get(self, key: str) -> str | None:
        result = self._cmd("GET", key)
        return result  # None if key doesn't exist

    def set(self, key: str, value: str) -> None:
        self._cmd("SET", key, value)


class _FileStore:
    """Token store in a local JSON file, for self-hosted deployments with a
    persistent volume. Same get/set interface as _UpstashRedis (one file per
    service, so the key is ignored). Writes are atomic and owner-only (0600 file
    in a 0700 directory) because the file holds live access tokens.
    """

    def __init__(self, path: str) -> None:
        self._path = os.path.abspath(os.path.expanduser(path))

    def get(self, key: str) -> str | None:
        try:
            with open(self._path, encoding="utf-8") as f:
                return f.read() or None
        except FileNotFoundError:
            return None

    def set(self, key: str, value: str) -> None:
        directory = os.path.dirname(self._path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".token_store.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(value)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


_CONSENT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Authorize {service}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;color:#222}}
dl{{display:grid;grid-template-columns:max-content 1fr;gap:.4rem 1rem}}dt{{font-weight:600}}dd{{margin:0;word-break:break-all}}
button{{font:inherit;padding:.6rem 1.4rem;border-radius:.4rem;border:1px solid #888;background:#fff;cursor:pointer}}
button.allow{{background:#1a7f37;border-color:#1a7f37;color:#fff}}form{{display:inline;margin-right:.6rem}}
</style></head><body>
<h1>Authorize access to {service}</h1>
<p>Signed in to GitHub as <b>{login}</b>. An application is asking for access:</p>
<dl>
<dt>Application</dt><dd>{client_name}</dd>
<dt>Client ID</dt><dd><code>{client_id}</code></dd>
<dt>Redirects to</dt><dd><code>{redirect_uri}</code></dd>
<dt>Scopes</dt><dd>{scopes}</dd>
</dl>
<p>Only continue if you started this connection yourself.</p>
<form method="post" action="{action}"><input type="hidden" name="consent_id" value="{consent_id}">
<button class="allow" name="decision" value="allow">Allow</button></form>
<form method="post" action="{action}"><input type="hidden" name="consent_id" value="{consent_id}">
<button name="decision" value="deny">Deny</button></form>
</body></html>
"""

_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class GitHubOAuthProvider(OAuthAuthorizationServerProvider):
    """
    MCP OAuth Authorization Server backed by GitHub as upstream identity provider.

    Only the GitHub account set in GITHUB_ALLOWED_USER_ID (legacy fallback:
    GITHUB_ALLOWED_USER) can complete the OAuth flow and receive an MCP access token.

    Tokens and client registrations are persisted to TOKEN_STORE_FILE or Upstash
    Redis so they survive container restarts (see module docstring).
    """

    def __init__(
        self,
        github_client_id: str,
        github_client_secret: str,
        server_url: str,
        service_name: str = "MCP server",
    ) -> None:
        self._github_client_id     = github_client_id
        self._github_client_secret = github_client_secret
        self._server_url           = server_url.rstrip("/")
        self._callback_url         = self._server_url + "/auth/callback"
        self._consent_url          = self._server_url + "/auth/consent"
        self._service_name         = service_name
        self._allowed_user         = os.getenv("GITHUB_ALLOWED_USER", "").lower()
        self._allowed_user_id      = os.getenv("GITHUB_ALLOWED_USER_ID", "").strip()
        self._redis_key            = os.getenv("TOKEN_STORE_KEY", "mcp:icloud-mail:token_store")
        self._allowed_redirects    = _allowed_redirect_uris()
        logger.info("Allowed OAuth redirect URIs: %s", ", ".join(self._allowed_redirects))

        store_file  = os.getenv("TOKEN_STORE_FILE")
        redis_url   = os.getenv("UPSTASH_REDIS_REST_URL")
        redis_token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
        if store_file:
            self._store = _FileStore(store_file)
            logger.info("✅ File token store configured (%s)", store_file)
        elif redis_url and redis_token:
            self._store = _UpstashRedis(redis_url, redis_token)
            logger.info("✅ Upstash Redis token store configured (key: %s)", self._redis_key)
        else:
            self._store = None
            logger.warning("⚠️  No token store configured — tokens stored in memory only (lost on restart)")

        # Short-lived — in-memory only (tied to active browser sessions), bounded by TTL + cap
        self._pending:    dict[str, dict]                      = {}
        self._consents:   dict[str, dict]                      = {}
        self._auth_codes: dict[str, AuthorizationCode]        = {}
        self._last_sweep  = 0.0

        # Persisted across restarts via the token store
        self._clients:        dict[str, OAuthClientInformationFull] = {}
        self._access_tokens:  dict[str, AccessToken]                = {}
        self._refresh_tokens: dict[str, RefreshToken]               = {}

        self._load_store()

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #

    def _load_store(self) -> None:
        if not self._store:
            return
        try:
            raw = self._store.get(self._redis_key)
            if not raw:
                logger.info("No saved token store found — starting fresh.")
                return
            data = json.loads(raw) if isinstance(raw, str) else raw
            clients = {
                k: OAuthClientInformationFull.model_validate(v)
                for k, v in data.get("clients", {}).items()
            }
            # Clients registered before the redirect-URI allowlist existed (or
            # under a different allowlist) are dropped together with their tokens.
            rejected = {k for k, c in clients.items() if not self._client_redirects_allowed(c)}
            for k in rejected:
                logger.warning("Dropping persisted client %s: redirect URI not allowed", k)
                clients.pop(k)
            self._clients = clients
            self._access_tokens = {
                k: AccessToken(**v)
                for k, v in data.get("access_tokens", {}).items()
                if v.get("client_id") not in rejected
            }
            self._refresh_tokens = {
                k: RefreshToken(**v)
                for k, v in data.get("refresh_tokens", {}).items()
                if v.get("client_id") not in rejected
            }
            logger.info(
                "✅ Token store loaded (%d clients, %d access tokens, %d refresh tokens)",
                len(self._clients), len(self._access_tokens), len(self._refresh_tokens),
            )
            if rejected:
                self._save_store()
        except Exception as exc:
            logger.warning("Could not load token store: %s — starting fresh.", exc)

    def _save_store(self) -> None:
        if not self._store:
            return
        try:
            now = time.time()
            valid_access  = {k: v for k, v in self._access_tokens.items()
                             if v.expires_at is None or v.expires_at > now}
            valid_refresh = {k: v for k, v in self._refresh_tokens.items()
                             if v.expires_at is None or v.expires_at > now}
            data = {
                "clients":        {k: _model_to_dict(v) for k, v in self._clients.items()},
                "access_tokens":  {k: _model_to_dict(v) for k, v in valid_access.items()},
                "refresh_tokens": {k: _model_to_dict(v) for k, v in valid_refresh.items()},
                "saved_at":       now,
            }
            self._store.set(self._redis_key, json.dumps(data, default=str))
        except Exception as exc:
            logger.error("Failed to save token store: %s", exc)

    # ------------------------------------------------------------------ #
    # Bounded in-memory state                                              #
    # ------------------------------------------------------------------ #

    def _sweep(self, force: bool = False) -> None:
        """Drop expired entries from every in-memory dict (rate-limited)."""
        now = time.time()
        if not force and now - self._last_sweep < SWEEP_INTERVAL:
            return
        self._last_sweep = now
        for d in (self._pending, self._consents):
            for k in [k for k, v in d.items() if v["expires_at"] <= now]:
                d.pop(k, None)
        for d in (self._auth_codes, self._access_tokens, self._refresh_tokens):
            for k in [k for k, v in d.items() if v.expires_at is not None and v.expires_at <= now]:
                d.pop(k, None)

    @staticmethod
    def _cap(d: dict, limit: int) -> None:
        """FIFO-evict so that one more entry fits under the limit."""
        while len(d) >= limit:
            d.pop(next(iter(d)), None)

    # ------------------------------------------------------------------ #
    # Client registration (dynamic — Claude registers itself on first use) #
    # ------------------------------------------------------------------ #

    def _client_redirects_allowed(self, client: OAuthClientInformationFull) -> bool:
        uris = client.redirect_uris or []
        return bool(uris) and all(redirect_uri_allowed(str(u), self._allowed_redirects) for u in uris)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        bad = [str(u) for u in (client_info.redirect_uris or [])
               if not redirect_uri_allowed(str(u), self._allowed_redirects)]
        if bad or not client_info.redirect_uris:
            logger.warning("Rejected client registration with redirect URIs %s", bad or "[]")
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="redirect_uri not allowed: " + (", ".join(bad) or "(none)"),
            )
        logger.info("Registering OAuth client: %s", client_info.client_id)
        self._sweep()
        self._clients[client_info.client_id] = client_info
        # Open dynamic registration ⇒ bound the persisted store (FIFO eviction).
        while len(self._clients) > MAX_CLIENTS:
            oldest = next(iter(self._clients))
            if oldest == client_info.client_id:
                break
            self._clients.pop(oldest, None)
        self._save_store()

    # ------------------------------------------------------------------ #
    # Authorization: redirect user to GitHub                              #
    # ------------------------------------------------------------------ #

    def _resource_allowed(self, resource: str | None) -> bool:
        """A token may only be requested for this server (RFC 8707 audience)."""
        if resource is None:
            return True
        r = str(resource).rstrip("/")
        return r == self._server_url or r.startswith(self._server_url + "/")

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self._resource_allowed(params.resource):
            raise AuthorizeError(
                error="invalid_request",
                error_description=f"resource must be {self._server_url} or a path below it",
            )
        self._sweep()
        self._cap(self._pending, MAX_PENDING)
        github_state = secrets.token_urlsafe(32)
        self._pending[github_state] = {
            "client_id":                        client.client_id,
            "code_challenge":                   params.code_challenge,
            "redirect_uri":                     str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "scopes":                           params.scopes or [],
            "mcp_state":                        params.state,
            "resource":                         params.resource,
            "expires_at":                       time.time() + PENDING_TTL,
        }
        github_params = {
            "client_id":    self._github_client_id,
            "redirect_uri": self._callback_url,
            "scope":        "read:user",
            "state":        github_state,
        }
        url = GITHUB_AUTHORIZE_URL + "?" + urlencode(github_params)
        logger.info("Redirecting client %s to GitHub OAuth", client.client_id)
        return url

    # ------------------------------------------------------------------ #
    # GitHub callback — registered as a custom FastMCP route              #
    # ------------------------------------------------------------------ #

    async def handle_github_callback(self, request: Request) -> Response:
        code         = request.query_params.get("code")
        github_state = request.query_params.get("state")
        error        = request.query_params.get("error")

        if error:
            logger.warning("GitHub OAuth error: %s", error)
            return HTMLResponse(f"<h1>Authorization failed</h1><p>GitHub error: {html.escape(error)}</p>", status_code=400)

        if not code or not github_state:
            return HTMLResponse("<h1>Invalid callback</h1><p>Missing code or state.</p>", status_code=400)

        pending = self._pending.pop(github_state, None)
        if not pending or pending["expires_at"] <= time.time():
            return HTMLResponse(
                "<h1>Invalid state</h1><p>State not found or expired. Please try connecting again.</p>",
                status_code=400,
            )

        # Exchange GitHub code → GitHub access token
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.post(
                    GITHUB_TOKEN_URL,
                    data={
                        "client_id":     self._github_client_id,
                        "client_secret": self._github_client_secret,
                        "code":          code,
                        "redirect_uri":  self._callback_url,
                    },
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                token_data = resp.json()
        except Exception as exc:
            logger.error("GitHub token exchange failed: %s", exc)
            return HTMLResponse("<h1>GitHub error</h1><p>Could not exchange authorization code.</p>", status_code=502)

        github_access_token = token_data.get("access_token")
        if not github_access_token:
            logger.error("No access_token in GitHub response: %s", token_data)
            return HTMLResponse("<h1>GitHub error</h1><p>No access token received.</p>", status_code=502)

        # Fetch GitHub user and enforce allowlist
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(
                    GITHUB_USER_URL,
                    headers={"Authorization": f"Bearer {github_access_token}", "Accept": "application/json"},
                )
                resp.raise_for_status()
                user_data = resp.json()
        except Exception as exc:
            logger.error("GitHub user lookup failed: %s", exc)
            return HTMLResponse("<h1>GitHub error</h1><p>Could not fetch user info.</p>", status_code=502)

        github_username = user_data.get("login", "").lower()
        github_user_id  = str(user_data.get("id", ""))
        logger.info("GitHub OAuth: user='%s' id='%s' allowed_id='%s'", github_username, github_user_id, self._allowed_user_id)

        # Authorize on the immutable numeric GitHub id when configured (logins are
        # renameable and re-registerable). Fall back to the legacy login allowlist
        # only if no id is set. Deny if neither is configured (fail closed).
        if self._allowed_user_id:
            authorized = github_user_id == self._allowed_user_id
        else:
            authorized = bool(self._allowed_user) and github_username == self._allowed_user

        if not authorized:
            logger.warning("Access denied for GitHub user: %s (id=%s)", github_username, github_user_id)
            safe_login = html.escape(user_data.get("login") or "")
            return HTMLResponse(
                f"<h1>Access Denied</h1><p>GitHub account <b>{safe_login}</b> is not authorized.</p>",
                status_code=403,
            )

        client = self._clients.get(pending["client_id"])
        if client is None:
            return HTMLResponse("<h1>Unknown client</h1><p>Please try connecting again.</p>", status_code=400)

        # Identity verified — ask the user to confirm the client before issuing a code.
        # The consent id is a one-time secret known only to this browser page, so a
        # cross-site POST cannot approve on the user's behalf.
        self._sweep()
        self._cap(self._consents, MAX_CONSENTS)
        consent_id = secrets.token_urlsafe(32)
        self._consents[consent_id] = {
            **pending,
            "github_login": user_data.get("login") or "",
            "expires_at":   time.time() + CONSENT_TTL,
        }
        page = _CONSENT_PAGE.format(
            service=html.escape(self._service_name),
            login=html.escape(user_data.get("login") or ""),
            client_name=html.escape(client.client_name or "(unnamed client)"),
            client_id=html.escape(client.client_id),
            redirect_uri=html.escape(pending["redirect_uri"]),
            scopes=html.escape(" ".join(pending["scopes"]) or "(default)"),
            action=html.escape(self._consent_url),
            consent_id=consent_id,
        )
        return HTMLResponse(page, headers=_NO_STORE)

    # ------------------------------------------------------------------ #
    # Consent decision — registered as a custom FastMCP route (POST)      #
    # ------------------------------------------------------------------ #

    async def handle_consent(self, request: Request) -> Response:
        try:
            form = await request.form()
        except Exception:
            return HTMLResponse("<h1>Invalid request</h1>", status_code=400)
        consent_id = str(form.get("consent_id") or "")
        decision   = str(form.get("decision") or "")

        consent = self._consents.pop(consent_id, None) if consent_id else None
        if not consent or consent["expires_at"] <= time.time():
            return HTMLResponse(
                "<h1>Invalid or expired</h1><p>This consent page is no longer valid. Please try connecting again.</p>",
                status_code=400,
            )

        if decision != "allow":
            logger.info("Consent denied for client %s", consent["client_id"])
            url = construct_redirect_uri(consent["redirect_uri"], error="access_denied", state=consent.get("mcp_state"))
            return RedirectResponse(url, status_code=302, headers=_NO_STORE)

        # Issue MCP authorization code
        self._sweep()
        mcp_code = secrets.token_urlsafe(32)
        self._auth_codes[mcp_code] = AuthorizationCode(
            code=mcp_code,
            client_id=consent["client_id"],
            scopes=consent["scopes"],
            expires_at=time.time() + AUTH_CODE_TTL,
            code_challenge=consent["code_challenge"],
            redirect_uri=consent["redirect_uri"],  # type: ignore[arg-type]
            redirect_uri_provided_explicitly=consent["redirect_uri_provided_explicitly"],
            resource=consent.get("resource"),
        )

        redirect_url = construct_redirect_uri(consent["redirect_uri"], code=mcp_code, state=consent.get("mcp_state"))
        logger.info("GitHub OAuth complete for '%s' — redirecting to client", consent["github_login"])
        return RedirectResponse(redirect_url, status_code=302, headers=_NO_STORE)

    # ------------------------------------------------------------------ #
    # Token exchange                                                       #
    # ------------------------------------------------------------------ #

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        obj = self._auth_codes.get(authorization_code)
        if obj and obj.expires_at > time.time():
            return obj
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)  # one-time use

        access_token  = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)

        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + ACCESS_TOKEN_TTL,
            resource=authorization_code.resource,
        )
        self._refresh_tokens[refresh_token] = RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + REFRESH_TOKEN_TTL,
        )
        self._sweep(force=True)
        self._save_store()

        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        rt = self._refresh_tokens.get(refresh_token)
        if rt and (rt.expires_at is None or rt.expires_at > time.time()):
            return rt
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._refresh_tokens.pop(refresh_token.token, None)  # rotate

        access_token     = secrets.token_urlsafe(32)
        new_refresh      = secrets.token_urlsafe(32)
        effective_scopes = scopes or refresh_token.scopes

        resource = getattr(refresh_token, "resource", None)
        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=effective_scopes,
            expires_at=int(time.time()) + ACCESS_TOKEN_TTL,
            resource=resource,
        )
        self._refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=client.client_id,
            scopes=effective_scopes,
            expires_at=int(time.time()) + REFRESH_TOKEN_TTL,
        )
        self._sweep(force=True)
        self._save_store()

        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=new_refresh,
            scope=" ".join(effective_scopes),
        )

    # ------------------------------------------------------------------ #
    # Token verification (called on every MCP request)                    #
    # ------------------------------------------------------------------ #

    async def load_access_token(self, token: str) -> AccessToken | None:
        self._sweep()
        at = self._access_tokens.get(token)
        if at and (at.expires_at is None or at.expires_at > time.time()):
            # Audience: tokens are only ever issued by this process for this
            # server (checked in authorize()), so this is belt and braces.
            if self._resource_allowed(at.resource):
                return at
            logger.warning("Rejecting access token issued for resource %r", at.resource)
        self._access_tokens.pop(token, None)
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
        else:
            self._refresh_tokens.pop(token.token, None)
        self._save_store()
