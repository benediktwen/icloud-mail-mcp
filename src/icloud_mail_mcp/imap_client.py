"""iCloud IMAP client with reconnect logic.

iCloud drops idle IMAP connections after ~30 minutes. All operations go through
_with_reconnect(), which catches imaplib.abort and re-establishes the connection.
"""

import base64
import email
import imaplib
import os
import re
import ssl
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formatdate, make_msgid, parsedate_to_datetime

from . import reply_builder

IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993

# iCloud folder name constants
TRASH_FOLDER  = "Deleted Messages"
DRAFTS_FOLDER = "Drafts"


def _decode_header(value: str | None) -> str:
    return reply_builder.decode_mime_header(value)


class IMAPClient:
    def __init__(self) -> None:
        self._conn: imaplib.IMAP4_SSL | None = None
        self._selected_folder: str | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> imaplib.IMAP4_SSL:
        # imaplib's default context does NOT verify the server certificate
        # (ssl._create_stdlib_context); use a verifying one explicitly.
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context())
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

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        attachments: list[dict] | None = None,
    ) -> dict:
        """Append a new draft email to the Drafts folder."""

        def _run(conn):
            my_address = os.environ["ICLOUD_USERNAME"]
            if attachments:
                msg: email.message.Message = MIMEMultipart()
                msg.attach(MIMEText(body, "plain", "utf-8"))
                for att in attachments:
                    _attach_part(msg, att)
            else:
                msg = MIMEText(body, "plain", "utf-8")
            msg["From"]       = my_address
            msg["To"]         = to
            msg["Subject"]    = subject if subject.isascii() else Header(subject, "utf-8")
            msg["Date"]       = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid()
            if cc:
                msg["Cc"] = cc
            conn.append(
                f'"{DRAFTS_FOLDER}"',
                "\\Draft",
                imaplib.Time2Internaldate(time.time()),
                msg.as_bytes(),
            )
            return {"ok": True, "draft_to": to, "subject": subject, "attachment_count": len(attachments) if attachments else 0}

        return self._with_reconnect(_run)

    def create_draft_reply(
        self,
        uid: str,
        folder: str,
        reply_text: str = "",
        attachments: list[dict] | None = None,
        reply_html: str = "",
        latest_in_thread: bool = True,
    ) -> dict:
        """Fetch the original message and append an Apple-Mail-style reply draft.

        The draft is multipart/alternative (text/plain + text/html) with the
        original quoted in <blockquote type="cite">. Replying to an own mail
        (e.g. from "Sent Messages") addresses the original recipients.

        With latest_in_thread (default) the reply goes to the newest message of
        the thread across all mail folders, like pressing Reply on the last
        message in Apple Mail — also when that last message is an own mail.
        """
        if not reply_text and not reply_html:
            raise ValueError("reply_text or reply_html is required")

        def _run(conn):
            self._select(conn, folder)
            original = self._fetch_message(conn, uid, folder)
            reply_folder, reply_uid = folder, uid
            if latest_in_thread:
                latest = self._latest_in_thread(conn, original)
                if latest:
                    reply_folder, reply_uid = latest
                    self._select(conn, reply_folder)
                    original = self._fetch_message(conn, reply_uid, reply_folder)
            draft, summary = reply_builder.build_reply(
                original,
                reply_text=reply_text,
                reply_html=reply_html,
                own=reply_builder.own_addresses(),
                original_is_own=self._is_sent_folder(conn, reply_folder),
            )
            draft = reply_builder.wrap_with_attachments(
                draft, [_attachment_part(att) for att in attachments or []]
            )

            typ, data = conn.append(
                f'"{DRAFTS_FOLDER}"',
                "\\Draft",
                imaplib.Time2Internaldate(time.time()),
                draft.as_bytes(),
            )
            if typ != "OK":
                raise ValueError(f"Could not save draft: {data!r}")

            draft_uid = _appended_uid(data) or self._find_by_message_id(
                conn, DRAFTS_FOLDER, str(draft["Message-ID"])
            )
            return {
                "ok": True,
                "uid": draft_uid,
                "folder": DRAFTS_FOLDER,
                **summary,
                "replied_to": {
                    "folder": reply_folder,
                    "uid": reply_uid,
                    "from": _decode_header(original.get("From", "")),
                    "date": original.get("Date", ""),
                },
                "switched_to_latest": (reply_folder, reply_uid) != (folder, uid),
                "attachment_count": len(attachments) if attachments else 0,
            }

        return self._with_reconnect(_run)

    def _thread_folders(self, conn) -> list[str]:
        """Mail folders a thread can live in (no Drafts, Trash, Junk)."""
        skip = {DRAFTS_FOLDER.lower(), TRASH_FOLDER.lower(), "junk", "notes"}
        typ, data = conn.list()
        folders = []
        for item in data or []:
            if not isinstance(item, bytes):
                continue
            if any(flag in item for flag in (b"\\Trash", b"\\Junk", b"\\Drafts", b"\\Noselect")):
                continue
            m = re.search(rb'"([^"]+)"$', item)
            name = m.group(1).decode("utf-8", "replace") if m else ""
            if name and name.lower() not in skip:
                folders.append(name)
        return folders

    def _latest_in_thread(self, conn, original: email.message.Message, max_hops: int = 20) -> tuple[str, str] | None:
        """(folder, uid) of the newest reply in the thread below ``original``, or None.

        Follows References / In-Reply-To downwards across all mail folders:
        each later message of a thread names the earlier ones there.
        """
        current_id = " ".join((original.get("Message-ID") or "").split())
        if not current_id:
            return None
        folders = self._thread_folders(conn)
        best: tuple[str, str] | None = None
        seen = {current_id}
        for _ in range(max_hops):
            newest = None
            for folder in folders:
                try:
                    self._select(conn, folder)
                except ValueError:
                    continue
                uids: set[bytes] = set()
                for header in ("References", "In-Reply-To"):
                    typ, data = conn.uid("SEARCH", None, "HEADER", header, f'"{current_id}"')
                    if typ == "OK" and data and data[0]:
                        uids.update(data[0].split())
                if not uids:
                    continue
                typ, msg_data = conn.uid(
                    "FETCH", b",".join(sorted(uids, key=int)),
                    "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE)])",
                )
                for part in msg_data or []:
                    if not isinstance(part, tuple):
                        continue
                    hdr = email.message_from_bytes(part[1])
                    mid = " ".join((hdr.get("Message-ID") or "").split())
                    if not mid or mid in seen:
                        continue
                    date = _parse_date(hdr.get("Date"))
                    if newest is None or date > newest[0]:
                        newest = (date, mid, folder, _extract_uid(part[0]))
            if newest is None:
                break
            _, current_id, folder_found, uid_found = newest
            seen.add(current_id)
            best = (folder_found, uid_found)
        return best

    def _find_by_message_id(self, conn, folder: str, message_id: str) -> str:
        """UID of the newest message with this Message-ID (iCloud has no UIDPLUS)."""
        try:
            self._select(conn, folder)
            typ, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", f'"{message_id}"')
            if typ == "OK" and data and data[0]:
                return data[0].split()[-1].decode()
        except Exception:
            pass
        return ""

    def _is_sent_folder(self, conn, folder: str) -> bool:
        """True if the folder carries the \\Sent special-use flag (or is called so)."""
        if folder.strip('"').lower() in ("sent messages", "sent", "gesendet", "sent items"):
            return True
        try:
            typ, data = conn.list('""', f'"{folder}"')
        except Exception:
            return False
        if typ != "OK":
            return False
        return any(b"\\Sent" in item for item in data if isinstance(item, bytes))

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
            # Free-text values go as UTF-8 literals (CHARSET UTF-8), so umlauts,
            # "ß" and quotes work. imaplib sends one literal per command, so each
            # such criterion is its own SEARCH and the results are intersected.
            literal_criteria = [(key, value) for key, value in (("FROM", from_address), ("TEXT", query)) if value]

            if not literal_criteria:
                typ, data = conn.uid("SEARCH", None, " ".join(criteria) or "ALL")
                if typ != "OK" or not data or not data[0]:
                    return []
                found = data[0].split()
            else:
                found_sets = []
                for key, value in literal_criteria:
                    conn.literal = value.encode("utf-8")
                    typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *criteria, key)
                    if typ != "OK" or not data or not data[0]:
                        return []
                    found_sets.append(set(data[0].split()))
                found = sorted(set.intersection(*found_sets), key=int)

            data = [b" ".join(found)]
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


def _parse_date(value: str | None) -> float:
    """POSIX timestamp of a Date header; 0 if missing or unreadable."""
    try:
        return parsedate_to_datetime(value).timestamp() if value else 0.0
    except Exception:
        return 0.0


def _appended_uid(append_response) -> str:
    """UID of an APPENDed message from the APPENDUID response code, if any."""
    for item in append_response or []:
        if isinstance(item, bytes):
            m = re.search(rb"APPENDUID \d+ (\d+)", item)
            if m:
                return m.group(1).decode()
    return ""


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
    """Return plain text body, falling back to HTML converted to readable text."""
    return reply_builder.body_text(msg)


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


def _attach_part(msg: email.message.Message, att: dict) -> None:
    """Decode a base64 attachment dict and attach it to a MIMEMultipart message."""
    msg.attach(_attachment_part(att))


def _attachment_part(att: dict) -> MIMEBase:
    """MIME part for a base64 attachment dict {filename, data_base64, content_type}."""
    filename = att["filename"]
    content_type = att.get("content_type", "application/octet-stream")
    data = base64.b64decode(att["data_base64"])
    maintype, subtype = content_type.split("/", 1) if "/" in content_type else ("application", "octet-stream")
    part = MIMEBase(maintype, subtype)
    part.set_payload(data)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    return part


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
