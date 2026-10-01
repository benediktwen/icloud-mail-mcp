"""
Tests for OAuth token-store persistence (file store, priority over Redis).
"""
import asyncio
import os
import stat
import time

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.shared.auth import OAuthClientInformationFull

from icloud_mail_mcp import github_oauth_provider as gop


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("TOKEN_STORE_FILE", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def _provider():
    return gop.GitHubOAuthProvider("client-id", "client-secret", "https://example.invalid")


def _client(client_id="claude-client"):
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
    )


class TestFileStore:
    def test_missing_file_returns_none(self, tmp_path):
        store = gop._FileStore(str(tmp_path / "state" / "token_store.json"))
        assert store.get("any-key") is None

    def test_roundtrip_with_owner_only_permissions(self, tmp_path):
        path = tmp_path / "state" / "token_store.json"
        store = gop._FileStore(str(path))
        store.set("any-key", '{"a": 1}')

        assert store.get("any-key") == '{"a": 1}'
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700

    def test_overwrite_leaves_no_temp_files(self, tmp_path):
        path = tmp_path / "token_store.json"
        store = gop._FileStore(str(path))
        store.set("k", "first")
        store.set("k", "second")

        assert store.get("k") == "second"
        assert [p.name for p in tmp_path.iterdir()] == ["token_store.json"]


class TestProviderPersistence:
    def test_tokens_survive_restart_with_file_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOKEN_STORE_FILE", str(tmp_path / "token_store.json"))

        first = _provider()
        asyncio.run(first.register_client(_client()))
        first._access_tokens["tok"] = AccessToken(
            token="tok", client_id="claude-client", scopes=["mcp"],
            expires_at=int(time.time()) + 3600,
        )
        first._save_store()

        restarted = _provider()
        assert "claude-client" in restarted._clients
        assert "tok" in restarted._access_tokens

    def test_file_store_takes_precedence_over_redis(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOKEN_STORE_FILE", str(tmp_path / "token_store.json"))
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://redis.example.invalid")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "unused")

        assert isinstance(_provider()._store, gop._FileStore)

    def test_redis_used_when_no_file_configured(self, monkeypatch):
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://redis.example.invalid")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "unused")
        monkeypatch.setattr(gop._UpstashRedis, "get", lambda self, key: None)

        assert isinstance(_provider()._store, gop._UpstashRedis)

    def test_memory_only_without_any_store(self):
        provider = _provider()
        assert provider._store is None
        asyncio.run(provider.register_client(_client()))  # must not raise
