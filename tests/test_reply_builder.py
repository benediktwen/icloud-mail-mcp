"""
Tests for Apple-Mail-style reply drafts (reply_builder) and the UTF-8 search.
All addresses are fictitious (example.com / example.org).
"""
import email
from email import policy
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from icloud_mail_mcp import reply_builder as rb
from icloud_mail_mcp.imap_client import IMAPClient

OWN = {"me@example.com", "alias@example.org"}
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00"
    b"\x00\x00IEND\xaeB`\x82"
)


def _html_mail(with_image=True, extra_cid=False):
    html = (
        "<html><head><style>p{color:red}</style></head><body lang=DE>"
        "<p>Hallo Herr Beispiel,</p><ul><li>Punkt eins</li><li>Punkt zwei</li></ul>"
        '<script>alert(1)</script><p onclick="x()">Grüße</p>'
        + ('<img src="cid:logo@x">' if with_image else "")
        + ('<img src="cid:missing@x" alt="weg">' if extra_cid else "")
        + "</body></html>"
    )
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Hallo Herr Beispiel,\n\n* Punkt eins\n* Punkt zwei\n\nGrüße", "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    if with_image:
        msg = MIMEMultipart("related")
        msg.attach(alt)
        img = MIMEImage(PNG, "png")
        img.add_header("Content-ID", "<logo@x>")
        img.add_header("Content-Disposition", "inline", filename="logo.png")
        msg.attach(img)
    else:
        msg = alt
    msg["From"] = "Erika Muster <erika@example.net>"
    msg["To"] = "Ben <me@example.com>"
    msg["Subject"] = "AW: Re: AW: Rückmeldung zum Termin"
    msg["Date"] = "Thu, 5 Nov 2026 08:26:30 +0000"
    msg["Message-ID"] = "\r\n <orig-2@example.net>"
    msg["References"] = "<root@example.com>\r\n <orig-1@example.net>"
    return email.message_from_bytes(msg.as_bytes())


def _parts(draft):
    parsed = email.message_from_bytes(draft.as_bytes(), policy=policy.default)
    plain = parsed.get_body(("plain",)).get_content()
    html = parsed.get_body(("html",)).get_content()
    return parsed, plain, html


def test_reply_to_html_mail_is_apple_mail_layout():
    draft, summary = rb.build_reply(_html_mail(), reply_text="Danke!\n\nZweiter Absatz\nmit Umbruch", own=OWN)
    parsed, plain, html = _parts(draft)

    assert parsed.get_content_type() == "multipart/alternative"
    assert '<blockquote type="cite">' in html
    # Attribution in Europe/Berlin (08:26 UTC -> 09:26 CET), Apple Mail wording.
    assert "On 05.11.2026, at 09:26, Erika Muster &lt;erika@example.net&gt; wrote:" in html
    assert "On 05.11.2026, at 09:26, Erika Muster <erika@example.net> wrote:" in plain
    # Original formatting kept, script/style/handlers gone.
    assert "<ul><li>Punkt eins</li>" in html
    assert "<script" not in html and "p{color:red}" not in html and "onclick" not in html
    # Typed text as Apple Mail divs.
    assert "<div>Danke!</div><div><br></div><div>Zweiter Absatz</div><div>mit Umbruch</div>" in html
    # Plain part: quoted, no markup.
    assert "> Hallo Herr Beispiel," in plain and "<" not in plain.split("wrote:")[1].replace("<erika", "")
    # Inline image travels in multipart/related next to the HTML.
    related = [p for p in parsed.walk() if p.get_content_type() == "multipart/related"]
    assert related and any(p.get("Content-ID") == "<logo@x>" for p in related[0].iter_parts())
    assert summary["inline_images"] == 1


def test_missing_cid_images_are_dropped():
    draft, _ = rb.build_reply(_html_mail(with_image=False, extra_cid=True), reply_text="x", own=OWN)
    _, _, html = _parts(draft)
    assert "cid:" not in html
    parsed = email.message_from_bytes(draft.as_bytes())
    assert not any(p.get_content_type() == "multipart/related" for p in parsed.walk())


def test_headers_thread_and_single_re():
    draft, summary = rb.build_reply(_html_mail(), reply_text="x", own=OWN)
    parsed = email.message_from_bytes(draft.as_bytes(), policy=policy.default)
    assert parsed["Subject"] == "Re: Rückmeldung zum Termin"
    assert summary["subject"] == "Re: Rückmeldung zum Termin"
    assert "=?utf-8?" in draft.as_string().split("Subject:")[1].splitlines()[0].lower()
    assert parsed["In-Reply-To"] == "<orig-2@example.net>"
    assert parsed["References"] == "<root@example.com> <orig-1@example.net> <orig-2@example.net>"
    assert parsed["To"] == "Erika Muster <erika@example.net>"
    assert parsed["From"] == "me@example.com"
    assert parsed["Cc"] is None
    assert parsed["Message-ID"].endswith("@example.com>")


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("Hallo", "Re: Hallo"),
        ("Re: Hallo", "Re: Hallo"),
        ("RE: AW: Re: Hallo", "Re: Hallo"),
        ("Aw:  Antw: Hallo", "Re: Hallo"),
        ("WG: Hallo", "Re: WG: Hallo"),
        ("Re[2]: Hallo", "Re: Hallo"),
        ("Reise nach Rom", "Re: Reise nach Rom"),
    ],
)
def test_reply_subject(subject, expected):
    assert rb.reply_subject(subject) == expected


def test_reply_to_own_sent_mail_addresses_original_recipients():
    msg = MIMEText("Hallo Erika,\nanbei.", "plain", "utf-8")
    msg["From"] = "Ben <alias@example.org>"
    msg["To"] = "Erika Muster <erika@example.net>, Ben <me@example.com>"
    msg["Cc"] = "Max <max@example.net>, alias@example.org"
    msg["Subject"] = "Unterlagen"
    msg["Date"] = "Thu, 5 Nov 2026 12:38:31 +0100"
    msg["Message-ID"] = "<sent-1@example.org>"
    original = email.message_from_bytes(msg.as_bytes())

    draft, summary = rb.build_reply(original, reply_text="Nachtrag", own=OWN)
    parsed = email.message_from_bytes(draft.as_bytes(), policy=policy.default)
    assert parsed["To"] == "Erika Muster <erika@example.net>"
    assert parsed["Cc"] == "Max <max@example.net>"
    assert parsed["From"] == "alias@example.org"
    assert "example.com" not in (parsed["To"] + parsed["Cc"])


def test_reply_to_unknown_own_address_in_sent_folder():
    msg = MIMEText("x", "plain", "utf-8")
    msg["From"] = "new-alias@example.org"
    msg["To"] = "erika@example.net"
    msg["Subject"] = "x"
    original = email.message_from_bytes(msg.as_bytes())
    to, cc, sender = rb.reply_recipients(original, OWN, original_is_own=True)
    assert to == [("", "erika@example.net")] and cc == [] and sender == "new-alias@example.org"


def test_reply_to_header_wins_and_own_address_is_never_recipient():
    msg = MIMEText("x", "plain", "utf-8")
    msg["From"] = "Shop <noreply@example.net>"
    msg["Reply-To"] = "Support <support@example.net>, me@example.com"
    msg["To"] = "alias@example.org"
    msg["Subject"] = "x"
    original = email.message_from_bytes(msg.as_bytes())
    to, cc, sender = rb.reply_recipients(original, OWN)
    assert to == [("Support", "support@example.net")]
    assert sender == "alias@example.org"


def test_reply_to_plain_text_mail():
    msg = MIMEText("Zeile 1 <keine Markup>\nZeile 2 & mehr\n\nAbsatz", "plain", "utf-8")
    msg["From"] = "erika@example.net"
    msg["To"] = "me@example.com"
    msg["Subject"] = "Frage"
    msg["Date"] = "Mon, 9 Nov 2026 10:00:00 +0100"
    original = email.message_from_bytes(msg.as_bytes())
    draft, _ = rb.build_reply(original, reply_text="Antwort", own=OWN)
    _, plain, html = _parts(draft)
    assert "Zeile 1 &lt;keine Markup&gt;<br>Zeile 2 &amp; mehr<br><br>Absatz" in html
    assert plain.startswith("Antwort\n\nOn 09.11.2026, at 10:00, erika@example.net wrote:")
    assert "> Zeile 1 <keine Markup>\n> Zeile 2 & mehr\n>\n> Absatz" in plain


def test_reply_html_replaces_generated_html():
    draft, _ = rb.build_reply(_html_mail(), reply_html="<div>Liste:</div><ul><li>A</li></ul>", own=OWN)
    _, plain, html = _parts(draft)
    assert html.index("<ul><li>A</li></ul>") < html.index("<blockquote")
    assert plain.startswith("Liste:\n\n• A")


def test_attachments_wrap_into_mixed():
    from icloud_mail_mcp.imap_client import _attachment_part

    draft, _ = rb.build_reply(_html_mail(), reply_text="x", own=OWN)
    att = _attachment_part({"filename": "a.txt", "data_base64": "aGFsbG8=", "content_type": "text/plain"})
    mixed = rb.wrap_with_attachments(draft, [att])
    parsed = email.message_from_bytes(mixed.as_bytes(), policy=policy.default)
    assert parsed.get_content_type() == "multipart/mixed"
    assert parsed["Subject"] == "Re: Rückmeldung zum Termin"
    assert [a.get_filename() for a in parsed.iter_attachments()] == ["a.txt"]
    assert mixed.as_bytes().count(b"MIME-Version") >= 1


def test_html_to_text_lists_and_entities():
    assert rb.html_to_text("<p>A&amp;B</p><ul><li>eins</li><li>zwei</li></ul>x<br>y") == "A&B\n\n• eins\n• zwei\nx\ny"


# ----------------------------------------------------------------------
# search_emails with non-ASCII
# ----------------------------------------------------------------------

class _FakeConn:
    def __init__(self):
        self.literal = None
        self.calls = []

    def select(self, folder):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            self.calls.append((args, self.literal))
            lit = self.literal
            self.literal = None
            if lit == "Gartengeräte".encode():
                return "OK", [b"3 5 7"]
            if lit == "müller@example.net".encode():
                return "OK", [b"5 7 9"]
            return "OK", [b"1 2"]
        if command == "FETCH":
            return "OK", []
        raise AssertionError(command)


def _client_with(conn):
    client = IMAPClient()
    client._conn = conn
    return client


def test_search_sends_query_as_utf8_literal():
    conn = _FakeConn()
    _client_with(conn).search(query="Gartengeräte", unread_only=True)
    args, literal = conn.calls[0]
    assert args == ("CHARSET", "UTF-8", "UNSEEN", "TEXT")
    assert literal == "Gartengeräte".encode()


def test_search_intersects_two_literal_criteria():
    conn = _FakeConn()
    seen = {}

    def fetch(command, *args):
        if command == "FETCH":
            seen["uids"] = args[0]
            return "OK", []
        return _FakeConn.uid(conn, command, *args)

    conn.uid = fetch
    _client_with(conn).search(query="Gartengeräte", from_address="müller@example.net")
    assert len(conn.calls) == 2
    assert seen["uids"] == b"5,7"


def test_search_without_text_keeps_plain_search():
    conn = _FakeConn()
    _client_with(conn).search(since="01-Nov-2026")
    args, literal = conn.calls[0]
    assert args == (None, 'SINCE "01-Nov-2026"') and literal is None


# ----------------------------------------------------------------------
# Quote colours and "latest message of the thread"
# ----------------------------------------------------------------------

def test_strip_black_text_keeps_other_colours():
    markup = (
        '<span style="font-size:11pt;color: rgb(0, 0, 0);font-family:Arial">a</span>'
        '<span style="color:black !important">b</span><span style="COLOR:#000000">c</span>'
        '<span style="color:windowtext;">d</span><font color="#000">e</font>'
        '<span style="color: rgb(10, 26, 107); background-color: rgb(0, 0, 0)">f</span>'
    )
    out = rb.strip_black_text(markup)
    assert "rgb(0, 0, 0);" not in out.split("background-color")[0]
    assert "black" not in out and "#000" not in out and "windowtext" not in out
    assert "font-size:11pt;font-family:Arial" in out
    assert "color: rgb(10, 26, 107)" in out and "background-color: rgb(0, 0, 0)" in out


def test_quoted_outlook_black_is_removed_in_draft():
    original = _html_mail(with_image=False)
    html_part = [p for p in original.walk() if p.get_content_type() == "text/html"][0]
    html_part.set_payload(
        '<html><body><p style="color:rgb(0,0,0)">Text</p><p style="color:#c04a2c">Marke</p></body></html>',
        "utf-8",
    )
    draft, _ = rb.build_reply(original, reply_text="x", own=OWN)
    _, _, html = _parts(draft)
    assert "rgb(0,0,0)" not in html and "#c04a2c" in html


def _header_msg(mid, date, refs="", irt=""):
    lines = [f"Message-ID: {mid}", f"Date: {date}"]
    if refs:
        lines.append(f"References: {refs}")
    if irt:
        lines.append(f"In-Reply-To: {irt}")
    return email.message_from_string("\r\n".join(lines) + "\r\n\r\nbody")


class _ThreadConn:
    """Fake IMAP with a thread spread over INBOX and Sent Messages."""

    def __init__(self, mailbox):
        self.mailbox = mailbox  # {folder: {uid: message}}
        self.selected = None

    def list(self, *args):
        return "OK", [
            b'(\\Noinferiors) "/" "INBOX"', b'(\\Sent) "/" "Sent Messages"',
            b'() "/" "Drafts"', b'(\\Trash) "/" "Deleted Messages"', b'() "/" "Junk"',
        ]

    def select(self, folder):
        self.selected = folder.strip('"')
        return "OK", [b"1"]

    def uid(self, command, *args):
        box = self.mailbox.get(self.selected, {})
        if command == "SEARCH":
            _, _, header, value = args
            value = value.strip('"')
            hits = [u for u, m in box.items() if value in (m.get(header) or "")]
            return "OK", [" ".join(hits).encode()]
        if command == "FETCH":
            out = []
            for u in args[0].decode().split(","):
                out.append((f"1 (UID {u} BODY[HEADER] {{1}}".encode(), box[u].as_bytes()))
                out.append(b")")
            return "OK", out
        raise AssertionError(command)


def test_latest_in_thread_follows_replies_across_folders():
    root = _header_msg("<a@x>", "Thu, 5 Nov 2026 08:26:30 +0000")
    mailbox = {
        "INBOX": {"10": root},
        "Sent Messages": {
            "139": _header_msg("<b@x>", "Thu, 5 Nov 2026 12:38:31 +0100", refs="<a@x>", irt="<a@x>"),
            "143": _header_msg("<c@x>", "Mon, 9 Nov 2026 12:57:01 +0100", refs="<a@x> <b@x>", irt="<b@x>"),
        },
        "Drafts": {"205": _header_msg("<d@x>", "Tue, 10 Nov 2026 09:00:00 +0100", refs="<a@x> <b@x> <c@x>")},
    }
    client = IMAPClient()
    assert client._latest_in_thread(_ThreadConn(mailbox), root) == ("Sent Messages", "143")


def test_latest_in_thread_none_when_message_is_newest():
    last = _header_msg("<c@x>", "Mon, 9 Nov 2026 12:57:01 +0100", refs="<a@x> <b@x>")
    mailbox = {"INBOX": {}, "Sent Messages": {"143": last}}
    assert IMAPClient()._latest_in_thread(_ThreadConn(mailbox), last) is None
