"""The IMAP connection must verify iCloud's certificate (imaplib does not by default)."""
import ssl

from icloud_mail_mcp import imap_client


def test_imap_connect_uses_verifying_tls_context(monkeypatch):
    captured = {}

    class FakeIMAP:
        def __init__(self, host, port, **kwargs):
            captured.update(host=host, port=port, **kwargs)

        def login(self, user, password):
            captured["login"] = (user, password)

    monkeypatch.setattr(imap_client.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setenv("ICLOUD_USERNAME", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "app-pw")

    imap_client.IMAPClient()._connect()

    assert (captured["host"], captured["port"]) == ("imap.mail.me.com", 993)
    ctx = captured["ssl_context"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert captured["login"] == ("me@icloud.com", "app-pw")
