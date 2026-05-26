"""iCloud IMAP client with reconnect logic.

iCloud drops idle IMAP connections after ~30 minutes. All operations go through
_with_reconnect(), which catches imaplib.abort and re-establishes the connection.
"""

import email
import imaplib
import os
import re
import time
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid, parseaddr, parsedate_to_datetime

IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993

# iCloud folder name constants
TRASH_FOLDER  = "Deleted Messages"
DRAFTS_FOLDER = "Drafts"


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


class IMAPClient:
    def __init__(self) -> None:
        self._conn: imaplib.IMAP4_SSL | None = None
        self._selected_folder: str | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn.login(
            os.environ["ICLOUD_USERNAME"],
            os.environ["ICLOUD_APP_PASSWORD"],
        )
        return conn

    def _ensure_connected(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            self._conn = self._connect()
            self._selected_folder = None
        return self._conn

    def _reconnect(self) -> imaplib.IMAP4_SSL:
        try:
            if self._conn:
                self._conn.logout()
        except Exception:
            pass
        self._conn = None
        self._selected_folder = None
        self._conn = self._connect()
        return self._conn

    def _with_reconnect(self, fn, *args, **kwargs):
        _RETRIABLE = (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, TimeoutError)
        for attempt in range(2):
            try:
                conn = self._ensure_connected()
                return fn(conn, *args, **kwargs)
            except _RETRIABLE as exc:
                print(f"IMAP connection error ({type(exc).__name__}), reconnecting...")
                self._conn = None
                self._selected_folder = None
                if attempt == 1:
                    raise

    def _select(self, conn: imaplib.IMAP4_SSL, folder: str) -> None:
        if self._selected_folder != folder:
            typ, _ = conn.select(f'"{folder}"')
            if typ != "OK":
                raise ValueError(f"Cannot select folder {folder!r}")
            self._selected_folder = folder

    # ------------------------------------------------------------------
    # Folder operations
    # ------------------------------------------------------------------

    def list_folders(self) -> list[str]:
        def _run(conn):
            typ, data = conn.list()
            if typ != "OK":
                return []
            folders = []
            for item in data:
                if not item:
                    continue
                decoded = item.decode("utf-8") if isinstance(item, bytes) else item
                m = re.search(r'"([^"]+)"$', decoded)
                if m:
                    folders.append(m.group(1))
                else:
                    parts = decoded.rsplit(" ", 1)
                    if len(parts) == 2:
                        folders.append(parts[-1].strip('"'))
            return sorted(folders)

        return self._with_reconnect(_run)

    # ------------------------------------------------------------------
    # Flag operations
    # ------------------------------------------------------------------

    def mark_read(self, uid: str, folder: str = "INBOX") -> None:
        def _run(conn):
            self._select(conn, folder)
            conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
        self._with_reconnect(_run)

    def mark_unread(self, uid: str, folder: str = "INBOX") -> None:
        def _run(conn):
            self._select(conn, folder)
            conn.uid("STORE", uid, "-FLAGS", r"(\Seen)")
        self._with_reconnect(_run)

    def flag_email(self, uid: str, folder: str = "INBOX") -> None:
        def _run(conn):
            self._select(conn, folder)
            conn.uid("STORE", uid, "+FLAGS", r"(\Flagged)")
        self._with_reconnect(_run)

    def unflag_email(self, uid: str, folder: str = "INBOX") -> None:
        def _run(conn):
            self._select(conn, folder)
            conn.uid("STORE", uid, "-FLAGS", r"(\Flagged)")
        self._with_reconnect(_run)

    # ------------------------------------------------------------------
    # Move / delete
    # ------------------------------------------------------------------

    def move_to_folder(self, uid: str, destination: str, source_folder: str = "INBOX") -> None:
        def _run(conn):
            self._select(conn, source_folder)
            conn.uid("COPY", uid, f'"{destination}"')
            conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            conn.expunge()
        self._with_reconnect(_run)

    def delete_email(self, uid: str, folder: str = "INBOX") -> None:
        """Move email to Trash (Deleted Messages)."""
        def _run(conn):
            self._select(conn, folder)
            # Try iCloud trash folder; fall back to permanent delete if not found
            typ, _ = conn.uid("COPY", uid, f'"{TRASH_FOLDER}"')
            if typ == "OK":
                conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
                conn.expunge()
            else:
                # Trash folder not accessible — permanent delete
                conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
                conn.expunge()
        self._with_reconnect(_run)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_message(self, conn, uid: str, folder: str) -> email.message.Message:
        """Fetch a full message using a three-step fallback chain.

        iCloud is more reliable with BODY.PEEK[] than RFC822 (other MCP
        implementations confirm this). BODY.PEEK[] also avoids silently marking
        messages as read, which RFC822 does. If both full-body fetches return NIL,
        header + text parts are fetched separately and stitched together.
        """
        for fetch_spec in ("(BODY.PEEK[])", "(RFC822)"):
            typ, msg_data = conn.uid("FETCH", uid, fetch_spec)
            if typ == "OK" and msg_data and isinstance(msg_data[0], tuple) and isinstance(msg_data[0][1], bytes):
                return email.message_from_bytes(msg_data[0][1])

        # Both full-body fetches returned NIL — fetch parts separately
        typ_h, hdr_data = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER])")
        if typ_h != "OK" or not hdr_data or not isinstance(hdr_data[0], tuple):
            raise ValueError(f"Message {uid!r} not found in {folder!r}")

        hdr_raw = hdr_data[0][1] if isinstance(hdr_data[0][1], bytes) else b""
        body_raw = b""
        typ_t, txt_data = conn.uid("FETCH", uid, "(BODY.PEEK[TEXT])")
        if typ_t == "OK" and txt_data and isinstance(txt_data[0], tuple) and isinstance(txt_data[0][1], bytes):
            body_raw = txt_data[0][1]

        return email.message_from_bytes(hdr_raw + b"\r\n" + body_raw)

    # ------------------------------------------------------------------
    # Draft creation
    # ------------------------------------------------------------------

    def create_draft(self, to: str, subject: str, body: str, cc: str = "") -> dict:
        """Append a new draft email to the Drafts folder."""

        def _run(conn):
            my_address = os.environ["ICLOUD_USERNAME"]
            draft = MIMEText(body, "plain", "utf-8")
            draft["From"]       = my_address
            draft["To"]         = to
            draft["Subject"]    = subject
            draft["Date"]       = formatdate(localtime=True)
            draft["Message-ID"] = make_msgid()
            if cc:
                draft["Cc"] = cc
            raw = draft.as_bytes()
            conn.append(
                f'"{DRAFTS_FOLDER}"',
                "\\Draft",
                imaplib.Time2Internaldate(time.time()),
                raw,
            )
            return {"ok": True, "draft_to": to, "subject": subject}

        return self._with_reconnect(_run)

    def create_draft_reply(self, uid: str, folder: str, reply_text: str) -> dict:
        """Fetch original message and append a reply draft to the Drafts folder."""

        def _run(conn):
            self._select(conn, folder)
            original = self._fetch_message(conn, uid, folder)

            orig_from    = _decode_header(original.get("From", ""))
            orig_subject = _decode_header(original.get("Subject", ""))
            orig_date    = original.get("Date", "")
            orig_msg_id  = original.get("Message-ID", "")

            # Format subject
            subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"

            # Format attribution line (Apple Mail style)
            try:
                dt = parsedate_to_datetime(orig_date)
                formatted_date = dt.strftime("%-d %b %Y, at %H:%M")
            except Exception:
                formatted_date = orig_date
            attribution = f"On {formatted_date}, {orig_from} wrote:"

            # Quote original body
            orig_body = _extract_body(original)
            quoted = "\n".join(f"> {line}" for line in orig_body.splitlines())

            # Assemble body
            body = f"{reply_text}\n\n{attribution}\n\n{quoted}"

            # Derive from address from the original message's To/Delivered-To header
            # so replies go out from the address the mail was actually received on.
            fallback = os.environ["ICLOUD_USERNAME"]
            _, to_addr = parseaddr(_decode_header(original.get("Delivered-To", "")))
            if not to_addr:
                _, to_addr = parseaddr(_decode_header(original.get("To", "")))
            my_address = to_addr if to_addr else fallback
            draft = MIMEText(body, "plain", "utf-8")
            draft["From"]    = my_address
            draft["To"]      = orig_from
            draft["Subject"] = subject
            draft["Date"]    = formatdate(localtime=True)
            draft["Message-ID"] = make_msgid()
            if orig_msg_id:
                draft["In-Reply-To"] = orig_msg_id
                draft["References"]  = orig_msg_id

            raw = draft.as_bytes()
            conn.append(
                f'"{DRAFTS_FOLDER}"',
                "\\Draft",
                imaplib.Time2Internaldate(time.time()),
                raw,
            )

            return {
                "ok": True,
                "draft_to": orig_from,
                "subject": subject,
            }

        return self._with_reconnect(_run)

    # ------------------------------------------------------------------
    # Search / fetch
    # ------------------------------------------------------------------

    def search(
        self,
        query: str = "",
        from_address: str = "",
        folder: str = "INBOX",
        unread_only: bool = False,
        since: str | None = None,
        before: str | None = None,
        max_results: int = 20,
    ) -> list[dict]:
        """Search messages. Returns list of {id, subject, from, date, snippet}."""

        def _run(conn):
            self._select(conn, folder)

            criteria: list[str] = []
            if unread_only:
                criteria.append("UNSEEN")
            if since:
                criteria.append(f'SINCE "{since}"')
            if before:
                criteria.append(f'BEFORE "{before}"')
            if from_address:
                criteria.append(f'FROM "{from_address}"')
            if query:
                criteria.append(f'TEXT "{query}"')
            if not criteria:
                criteria.append("ALL")

            search_str = " ".join(criteria)
            typ, data = conn.uid("SEARCH", None, search_str)
            if typ != "OK" or not data or not data[0]:
                return []

            uids = data[0].split()
            uids = uids[-max_results:]

            if not uids:
                return []

            uid_list = b",".join(uids)
            typ, msg_data = conn.uid(
                "FETCH", uid_list, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
            )
            if typ != "OK":
                return []

            results = []
            for i in range(0, len(msg_data), 2):
                part = msg_data[i]
                if not isinstance(part, tuple):
                    continue
                uid_str = _extract_uid(msg_data[i][0])
                raw = part[1]
                msg = email.message_from_bytes(raw)
                snippet = _fetch_snippet(conn, uid_str)
                results.append(
                    {
                        "id": uid_str,
                        "subject": _decode_header(msg.get("Subject", "")),
                        "from": _decode_header(msg.get("From", "")),
                        "date": msg.get("Date", ""),
                        "snippet": snippet,
                    }
                )

            results.reverse()
            return results

        return self._with_reconnect(_run)

    def get_email(self, uid: str, folder: str = "INBOX") -> dict:
        """Fetch a full message by UID."""

        def _run(conn):
            self._select(conn, folder)
            msg = self._fetch_message(conn, uid, folder)
            return {
                "id": uid,
                "subject": _decode_header(msg.get("Subject", "")),
                "from": _decode_header(msg.get("From", "")),
                "to": _decode_header(msg.get("To", "")),
                "date": msg.get("Date", ""),
                "body": _extract_body(msg),
                "attachments": _list_attachments(msg),
            }

        return self._with_reconnect(_run)

    def get_pdf_text(self, uid: str, filename: str, folder: str = "INBOX", max_pages: int = 50) -> dict:
        """Extract text from a named PDF attachment."""

        def _run(conn):
            self._select(conn, folder)
            msg = self._fetch_message(conn, uid, folder)
            for part in msg.walk():
                if part.get_filename() == filename and "pdf" in (part.get_content_type() or "").lower():
                    pdf_bytes = part.get_payload(decode=True)
                    if not isinstance(pdf_bytes, bytes):
                        raise ValueError(f"Could not read attachment {filename!r}")
                    return _extract_pdf_text(pdf_bytes, max_pages)
            raise ValueError(f"PDF attachment {filename!r} not found in message {uid!r}")

        return self._with_reconnect(_run)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _extract_uid(fetch_response_line: bytes) -> str:
    m = re.search(rb"UID (\d+)", fetch_response_line)
    return m.group(1).decode() if m else ""


def _fetch_snippet(conn: imaplib.IMAP4_SSL, uid: str, length: int = 200) -> str:
    try:
        typ, data = conn.uid("FETCH", uid, f"(BODY.PEEK[TEXT]<0.{length}>)")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return ""
        raw = data[0][1]
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        return text[:length].replace("\r\n", " ").replace("\n", " ").strip()
    except Exception:
        return ""


def _extract_body(msg: email.message.Message) -> str:
    """Return plain text body, falling back to HTML stripped of tags."""
    plain = ""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not plain:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    plain = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ct == "text/html" and not html:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html = text
            else:
                plain = text

    if plain:
        return plain
    if html:
        return re.sub(r"<[^>]+>", "", html)
    return ""


def _list_attachments(msg: email.message.Message) -> list[dict]:
    """Return metadata for all MIME parts with Content-Disposition: attachment."""
    attachments = []
    for part in msg.walk():
        if part.get_content_disposition() != "attachment":
            continue
        filename = part.get_filename() or "unknown"
        payload = part.get_payload(decode=True)
        attachments.append({
            "filename": filename,
            "content_type": part.get_content_type(),
            "size_bytes": len(payload) if isinstance(payload, bytes) else 0,
        })
    return attachments


def _extract_pdf_text(pdf_bytes: bytes, max_pages: int = 50) -> dict:
    """Extract plain text from a PDF, up to max_pages pages."""
    try:
        import io
        from pypdf import PdfReader
    except ImportError:
        return {"error": "pypdf not installed", "text": "", "page_count": 0}

    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    pages_to_read = min(total, max_pages)
    texts = []
    for i in range(pages_to_read):
        text = reader.pages[i].extract_text() or ""
        if text.strip():
            texts.append(text)
    return {
        "page_count": total,
        "pages_read": pages_to_read,
        "truncated": total > max_pages,
        "text": "\n\n".join(texts),
    }
